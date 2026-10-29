"""The screen recorder: one ffmpeg process that records the game into rolling MP4 segments and tees a low-rate MJPEG
stream for the live view. Moved from the playthrough app (bg3play.recorder is this module) so a bench run can record
too.

    ffmpeg -f x11grab [-window_id <id>] -i :1  ->  scale 1280x720, h264_nvenc, 15 fps (playthrough.toml [recording])
        -> <out>/rec-<t0>-0000.mp4, -0001.mp4 ...  (fragmented MP4: a crash keeps what was written)
        -> <out>/rec-<t0>.csv                       (ffmpeg's segment list: file, start s, end s)
        -> stdout: multipart JPEG at 3 fps          (MjpegParser -> LiveFrame -> /api/v1/screen.mjpg)
    <out>/index.json: {"v": 1, "segments": [{file, run, start, end, bytes}], "events": [{t, ev, ...}]} with
    wall-clock (epoch s) start/end, so replay maps an event time to (segment, offset). start = the run's spawn time +
    ffmpeg's segment start. events: why the recording has a gap: {"ev": "start", "after"} (what it waited for),
    {"ev": "stop", "reason"} (the guard's reason carries its numbers), {"ev": "exit", "code"} (ffmpeg crashed).

`Recorder.poll()` (one thread calls it every second; tests call it with a fake clock) starts ffmpeg while the game
runs, the recording is enabled and the guard is quiet (GUARD_COOLDOWN_S after it clears); stops it when the game
exits, the guard trips (RAM/heat/disk), it
is disabled or its format changed (`Recorder.configure`: a playthrough's [recording] enabled, fps and height);
restarts a crashed ffmpeg with backoff; keeps the index; deletes the oldest finished segments above the disk cap.
ffmpeg needs DISPLAY/XAUTHORITY like the game (memory: runner display env).

Live probe 2026-10-21 (research/ovh/live1.md 7): `-window_id` on the BG3 window ("Baldur's Gate 3 (1920x1080) -
(Vulkan) ...") gives real frames under rootless Xwayland; the root window is black, so "window" is the default mode.
Cost with the game running: ffmpeg ~60 % of one core, RSS ~300 MB, +162 MiB VRAM, NVENC 0-4 %.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any, Protocol, TypeGuard

INDEX_VERSION = 1
GAME_COMM = "bg3"
GAME_TITLE_RE = re.compile(r"Baldur's Gate 3")
BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)
STABLE_RUN_S = 60.0  # a run that lasted this long resets the crash backoff
GUARD_COOLDOWN_S = 5.0  # after the guard trips, stay stopped this long after it clears (its hysteresis keeps a
# level hovering at a limit from restarting ffmpeg every few seconds). Was 120 s: launch pilot 2026-10-29, Opus's run
# lost its last 81 s (the win) to a RAM dip.


def is_obj(v: object) -> TypeGuard[dict[str, Any]]:
    """A parsed JSON object (isinstance on an Any gives pyright dict[Unknown, Unknown])."""
    return isinstance(v, dict)


@dataclass(frozen=True)
class Capture:
    """What to grab: the whole X display, or one window on it."""

    display: str = ":1"
    window_id: str | None = None


@dataclass(frozen=True)
class RecordSpec:
    out_dir: Path
    width: int = 1280
    height: int = 720
    fps: int = 15
    encoder: str = "h264_nvenc"  # or libx264 (CPU fallback)
    segment_s: int = 600
    live_fps: float = 3.0
    live_width: int = 640
    live: bool = True
    source: tuple[str, ...] = ()  # input args instead of x11grab (tests: -f lavfi -i testsrc)
    duration_s: float | None = None  # stop after this long (tests)


ENCODER_ARGS: dict[str, tuple[str, ...]] = {
    "h264_nvenc": ("-preset", "p4", "-rc", "vbr", "-cq", "28", "-b:v", "0"),
    "libx264": ("-preset", "ultrafast", "-crf", "28"),
}


def sized(spec: RecordSpec, fps: int, height: int) -> RecordSpec:
    """The spec at a playthrough's [recording] fps and height, 16:9 (an even width, as h264 needs)."""
    return replace(spec, fps=fps, height=height, width=2 * round(height * 8 / 9))


def run_prefix(t0: float) -> str:
    return f"rec-{int(t0)}"


def ffmpeg_args(spec: RecordSpec, capture: Capture, prefix: str, ffmpeg: str = "ffmpeg") -> list[str]:
    """The ffmpeg command line (pure). Outputs go to spec.out_dir/<prefix>-NNNN.mp4 and <prefix>.csv; the live MJPEG
    goes to stdout."""
    if spec.encoder not in ENCODER_ARGS:
        raise ValueError(f"unknown encoder {spec.encoder!r} (one of {sorted(ENCODER_ARGS)})")
    if spec.source:
        src = list(spec.source)
    else:
        src = ["-f", "x11grab", "-framerate", str(spec.fps), "-draw_mouse", "0"]
        if capture.window_id:
            src += ["-window_id", capture.window_id]
        src += ["-i", capture.display]
    w, h = spec.width, spec.height
    rec = (
        f"fps={spec.fps},scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    if spec.live:
        graph = f"[0:v]{rec},split=2[rec][lv0];[lv0]fps={spec.live_fps:g},scale={spec.live_width}:-2[lv]"
    else:
        graph = f"[0:v]{rec}[rec]"
    out = Path(spec.out_dir)
    args = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    if spec.duration_s is not None:
        args += ["-t", f"{spec.duration_s:g}"]
    args += [
        *src,
        "-filter_complex",
        graph,
        "-map",
        "[rec]",
        "-c:v",
        spec.encoder,
        *ENCODER_ARGS[spec.encoder],
        "-g",
        str(spec.fps * 2),
        "-f",
        "segment",
        "-segment_time",
        str(spec.segment_s),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-segment_list",
        str(out / f"{prefix}.csv"),
        "-segment_list_type",
        "csv",
        "-segment_list_flags",
        "live",
        str(out / f"{prefix}-%04d.mp4"),
    ]
    if spec.live:
        args += ["-map", "[lv]", "-c:v", "mjpeg", "-q:v", "7", "-f", "mpjpeg", "pipe:1"]
    return args


# ------------------------------------------------------------ segment index (pure)


def parse_segment_list(text: str) -> list[tuple[str, float, float]]:
    """ffmpeg's csv segment list -> (file, start s, end s) per finished segment."""
    out: list[tuple[str, float, float]] = []
    for line in text.splitlines():
        parts = line.strip().rsplit(",", 2)
        if len(parts) != 3:
            continue
        try:
            out.append((Path(parts[0]).name, float(parts[1]), float(parts[2])))
        except ValueError:
            continue
    return out


def run_segments(
    prefix: str,
    t0: float,
    listed: list[tuple[str, float, float]],
    files: list[str],
    sizes: dict[str, int],
    ended_at: float | None,
) -> list[dict[str, Any]]:
    """One run's index rows: the listed (finished) segments, then files on disk the list doesn't have yet (the one
    being written; after a crash the last one, closed at `ended_at`)."""
    rows: list[dict[str, Any]] = [
        {"file": f, "run": prefix, "start": round(t0 + s, 3), "end": round(t0 + e, 3), "bytes": sizes.get(f, 0)}
        for f, s, e in listed
    ]
    seen = {r["file"] for r in rows}
    start = t0 + (listed[-1][2] if listed else 0.0)
    for f in sorted(x for x in files if x.startswith(prefix + "-") and x.endswith(".mp4") and x not in seen):
        rows.append(
            {
                "file": f,
                "run": prefix,
                "start": round(start, 3),
                "end": round(ended_at, 3) if ended_at is not None else None,
                "bytes": sizes.get(f, 0),
            }
        )
    return rows


def over_cap(segments: list[dict[str, Any]], cap_bytes: int) -> list[dict[str, Any]]:
    """The oldest finished segments to delete so the total fits under cap_bytes (the open one is never deleted)."""
    total = sum(s["bytes"] for s in segments)
    out: list[dict[str, Any]] = []
    for s in sorted(segments, key=lambda s: s["start"]):
        if total <= cap_bytes:
            break
        if s["end"] is None:
            continue
        out.append(s)
        total -= s["bytes"]
    return out


def locate(segments: list[dict[str, Any]], t: float) -> tuple[dict[str, Any], float] | None:
    """The segment showing wall-clock time t and the offset into it (s), or None when nothing was recorded then."""
    for s in sorted(segments, key=lambda s: s["start"]):
        end = s["end"] if s["end"] is not None else float("inf")
        if s["start"] <= t < end:
            return s, round(t - s["start"], 3)
    return None


# ------------------------------------------------------------ live MJPEG


class MjpegParser:
    """Splits ffmpeg's mpjpeg output (boundary, headers with Content-length, JPEG bytes) into frames."""

    def __init__(self, max_frame: int = 4 * 1024 * 1024):
        self.buf = b""
        self.max_frame = max_frame

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames: list[bytes] = []
        while True:
            head_end = self.buf.find(b"\r\n\r\n")
            if head_end < 0:
                if len(self.buf) > 64 * 1024:  # garbage without headers
                    self.buf = b""
                return frames
            m = re.search(rb"(?i)content-length:\s*(\d+)", self.buf[:head_end])
            if not m:
                self.buf = self.buf[head_end + 4 :]
                continue
            n = int(m.group(1))
            if n > self.max_frame:
                self.buf = self.buf[head_end + 4 :]
                continue
            body = head_end + 4
            if len(self.buf) < body + n:
                return frames
            frames.append(self.buf[body : body + n])
            self.buf = self.buf[body + n :]


class LiveFrame:
    """The newest live JPEG; readers wait for the next one."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.seq = 0
        self.data: bytes | None = None
        self.closed = False

    def put(self, data: bytes) -> None:
        with self.cond:
            self.seq += 1
            self.data = data
            self.cond.notify_all()

    def get(self, after: int = -1, timeout: float = 0.0) -> tuple[int, bytes | None]:
        """(seq, frame) newer than `after`; with a timeout waits for one, else returns the current one."""
        with self.cond:
            if timeout > 0:
                self.cond.wait_for(lambda: self.seq > after or self.closed, timeout)
            return self.seq, self.data

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def pump_mjpeg(stream: IO[bytes], frame: LiveFrame) -> None:
    parser = MjpegParser()
    read = getattr(stream, "read1", stream.read)
    while chunk := read(65536):
        for f in parser.feed(chunk):
            frame.put(f)


# ------------------------------------------------------------ process, probe, guard


class Proc(Protocol):
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def spawn_ffmpeg(args: list[str], log: Path) -> Proc:
    with log.open("ab") as err:
        return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)


def game_running(proc: Path = Path("/proc")) -> bool:
    """A process of this user whose comm is "bg3" exists (bg3ai.metrics.game_pid's test, without the RSS pick)."""
    me = os.getuid()
    try:
        for d in proc.iterdir():
            try:
                if d.name.isdigit() and d.stat().st_uid == me and (d / "comm").read_text().strip() == GAME_COMM:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def find_window(tree: str, title: re.Pattern[str] = GAME_TITLE_RE) -> str | None:
    """The id of the biggest window whose name matches, from `xwininfo -root -tree` output."""
    best: tuple[int, str] | None = None
    for line in tree.splitlines():
        m = re.match(r'\s*(0x[0-9a-fA-F]+) "(.*)":.*?(\d+)x(\d+)\+', line)
        if m and title.search(m.group(2)):
            area = int(m.group(3)) * int(m.group(4))
            best = (area, m.group(1)) if best is None or area > best[0] else best
    return best[1] if best else None


def game_probe(
    display: str = ":1",
    mode: str = "window",
    running: Callable[[], bool] = game_running,
    tree: Callable[[str], str] | None = None,
) -> Callable[[], Capture | None]:
    """The default probe: None while the game isn't running (or its window isn't up yet in window mode)."""

    def xwininfo(d: str) -> str:
        r = subprocess.run(
            ["xwininfo", "-root", "-tree", "-display", d], capture_output=True, text=True, timeout=5, check=False
        )
        return r.stdout

    get_tree = tree or xwininfo

    def probe() -> Capture | None:
        if not running():
            return None
        if mode == "display":
            return Capture(display)
        wid = find_window(get_tree(display))
        return Capture(display, wid) if wid else None

    return probe


THERMAL_RX = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) cpu=(\S+) acpi=\S+ amdgpu=(\S+) nvme=\S+ nv=\[([^\]]*)\]")


def _int(s: str) -> int | None:
    try:
        return int(float(s))
    except ValueError:
        return None


def thermal_reading(line: str) -> tuple[int | None, int | None]:
    """(CPU C, GPU C) of a runtime/thermal.log line (bg3harness/ramwatch.py thermal_line), (None, None) otherwise."""
    m = THERMAL_RX.match(line)
    if not m:
        return None, None
    nv = m.group(4)
    gpu = _int(nv.split(",")[0]) if nv else None
    return _int(m.group(2)), gpu if gpu is not None else _int(m.group(3))


def mem_available_mb(meminfo: str) -> int | None:
    m = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, re.MULTILINE)
    return int(m.group(1)) // 1024 if m else None


def disk_free_mb(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free // 1024**2
    except OSError:
        return None


@dataclass(frozen=True)
class GuardLimits:
    min_avail_mb: int = 1500
    max_cpu_c: int = 90  # below ramwatch's heat pause (92 / 85): recording is the first thing to give up
    max_gpu_c: int = 82
    stale_s: float = 30.0  # thermal lines older than this are ignored
    min_disk_mb: int = 1024  # free space on the recording's filesystem
    # Hysteresis: once tripped, the guard clears only this far inside the limits (ffmpeg's own ~300 MB RSS would
    # otherwise trip it again as soon as it restarts)
    resume_ram_mb: int = 400
    resume_heat_c: int = 3
    resume_disk_mb: int = 512


def resource_guard(
    thermal_log: Path,
    limits: GuardLimits | None = None,
    clock: Callable[[], float] | None = None,
    meminfo: Path = Path("/proc/meminfo"),
    disk: Path | None = None,
    free_mb: Callable[[Path], int | None] | None = None,
) -> Callable[[], str | None]:
    """The default guard: a reason to stop recording (low RAM, hot CPU/GPU from thermal.log, little free space on
    `disk`'s filesystem), or None. A reason says its numbers; while tripped, the limits are the resume levels."""
    now = clock or time.time
    limits = limits or GuardLimits()
    held: list[str | None] = [None]

    def check() -> str | None:
        on = held[0] is not None
        floor = limits.min_avail_mb + (limits.resume_ram_mb if on else 0)
        try:
            avail = mem_available_mb(meminfo.read_text())
        except OSError:
            avail = None
        if avail is not None and avail < floor:
            return f"ram: {avail} MB available < {floor}"
        if disk is not None:
            free = (free_mb or disk_free_mb)(disk)
            dfloor = limits.min_disk_mb + (limits.resume_disk_mb if on else 0)
            if free is not None and free < dfloor:
                return f"disk: {free} MB free < {dfloor}"
        try:
            st = thermal_log.stat()
            if now() - st.st_mtime > limits.stale_s:
                return None
            with thermal_log.open("rb") as f:
                f.seek(max(0, st.st_size - 512))
                last = f.read().decode("utf-8", "replace").strip().splitlines()[-1:]
        except OSError:
            return None
        cpu, gpu = thermal_reading(last[0]) if last else (None, None)
        cool = limits.resume_heat_c if on else 0
        if cpu is not None and cpu >= limits.max_cpu_c - cool:
            return f"heat: cpu {cpu} C >= {limits.max_cpu_c - cool}"
        if gpu is not None and gpu >= limits.max_gpu_c - cool:
            return f"heat: gpu {gpu} C >= {limits.max_gpu_c - cool}"
        return None

    def guard() -> str | None:
        held[0] = check()
        return held[0]

    return guard


# ------------------------------------------------------------ the recorder


@dataclass
class Run:
    prefix: str
    t0: float
    capture: Capture
    proc: Proc
    spec: RecordSpec
    ended_at: float | None = None
    pump: threading.Thread | None = None


@dataclass
class Recorder:
    spec: RecordSpec
    probe: Callable[[], Capture | None]
    clock: Callable[[], float]
    guard: Callable[[], str | None] = lambda: None
    spawn: Callable[[list[str], Path], Proc] = spawn_ffmpeg
    ffmpeg: str = "ffmpeg"
    cap_bytes: int = 20 * 1024**3
    enabled: bool = True
    live: LiveFrame = field(default_factory=LiveFrame)
    run: Run | None = None
    reason: str = "starting"
    crashes: int = 0
    last_exit: int | None = None
    next_start: float = 0.0
    guard_until: float = 0.0
    segments: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def __post_init__(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        try:
            idx = json.loads((self.out / "index.json").read_text())
            self.segments = [s for s in idx.get("segments", []) if (self.out / s["file"]).exists()]
            self.events = [e for e in idx.get("events", []) if is_obj(e)]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.segments, self.events = [], []

    @property
    def out(self) -> Path:
        return Path(self.spec.out_dir)

    # -------------------------------------------------- control

    def poll(self) -> None:
        now = self.clock()
        target = self.probe()
        why = self.guard()
        if why:
            self.guard_until = now + GUARD_COOLDOWN_S
        run = self.run
        if run is not None and run.proc.poll() is not None:
            self._ended(run, now, crashed=True)
        elif run is not None:
            stop_why = (
                "disabled"
                if not self.enabled
                else "game not running"
                if target is None
                else why
                if why
                else "capture changed"
                if target != run.capture
                else "format changed"
                if run.spec != self.spec
                else None
            )
            if stop_why:
                self.stop(stop_why)
        if self.run is None:
            if not self.enabled:
                self.reason = "disabled"
            elif target is None:
                self.reason = "game not running"
            elif why or now < self.guard_until:
                self.reason = f"guard: {why}" if why else "guard cooldown"
            elif now < self.next_start:
                self.reason = f"restart backoff (ffmpeg exited {self.last_exit}, {self.crashes} crashes)"
            else:
                self._start(target, now)
        self._collect()
        self._enforce_cap()

    def _start(self, capture: Capture, now: float) -> None:
        prefix = run_prefix(now)
        args = ffmpeg_args(self.spec, capture, prefix, self.ffmpeg)
        proc = self.spawn(args, self.out / "ffmpeg.log")
        self.run = Run(prefix, now, capture, proc, self.spec)
        self._event(now, "start", after=self.reason)
        if self.spec.live and proc.stdout is not None:
            self.run.pump = threading.Thread(
                target=pump_mjpeg, args=(proc.stdout, self.live), daemon=True, name="mjpeg"
            )
            self.run.pump.start()
        self.reason = "recording"

    def configure(self, enabled: bool, fps: int, height: int) -> None:
        """A playthrough's [recording]: on or off, and its format. A capture in another format restarts in the new one
        at the next poll (ffmpeg closes its open segment first)."""
        self.enabled = enabled
        self.spec = sized(self.spec, fps, height)

    def stop(self, reason: str = "stopped") -> None:
        """Stops ffmpeg (SIGTERM: it closes the open segment and its list row; SIGKILL after 10 s)."""
        run = self.run
        if run is None:
            return
        run.proc.terminate()
        try:
            run.proc.wait(10)
        except subprocess.TimeoutExpired:
            run.proc.kill()
            run.proc.wait()
        now = self.clock()
        self._ended(run, now, crashed=False)
        self.reason = reason
        self._event(now, "stop", reason=reason)

    def _ended(self, run: Run, now: float, crashed: bool) -> None:
        if run.pump is not None:
            run.pump.join(5)
        run.ended_at = now
        self._collect(run)
        self.run = None
        if crashed:
            self.crashes = 0 if now - run.t0 >= STABLE_RUN_S else self.crashes + 1
            delay = BACKOFF_S[min(self.crashes, len(BACKOFF_S)) - 1] if self.crashes else 0.0
            self.next_start = now + delay
            self.last_exit = run.proc.poll()
            self.reason = f"ffmpeg exited ({self.last_exit})"
            self._event(now, "exit", code=self.last_exit, restart_in=round(delay, 1))

    # -------------------------------------------------- index, cap

    def _collect(self, run: Run | None = None) -> None:
        run = run or self.run
        if run is None:
            return
        try:
            listed = parse_segment_list((self.out / f"{run.prefix}.csv").read_text())
        except OSError:
            listed = []
        files = [f.name for f in self.out.glob(f"{run.prefix}-*.mp4")]
        sizes: dict[str, int] = {}

        for f in files:
            try:
                sizes[f] = (self.out / f).stat().st_size
            except OSError:
                sizes[f] = 0
        rows = run_segments(run.prefix, run.t0, listed, files, sizes, run.ended_at)
        self.segments = [s for s in self.segments if s["run"] != run.prefix] + rows
        self._write_index()

    def _enforce_cap(self) -> None:
        drop = over_cap(self.segments, self.cap_bytes)
        if not drop:
            return
        for s in drop:
            with contextlib.suppress(FileNotFoundError):
                (self.out / s["file"]).unlink()
        gone = {s["file"] for s in drop}
        self.segments = [s for s in self.segments if s["file"] not in gone]
        self._write_index()

    def _event(self, t: float, ev: str, **data: Any) -> None:
        """Why the recording starts, stops or has a gap, into index.json's events."""
        self.events.append({"t": round(t, 3), "ev": ev, **data})
        self._write_index()

    def _write_index(self) -> None:
        tmp = self.out / "index.json.tmp"
        tmp.write_text(json.dumps({"v": INDEX_VERSION, "segments": self.segments, "events": self.events}, indent=1))
        tmp.replace(self.out / "index.json")

    def state(self) -> dict[str, Any]:
        return {
            "recording": self.run is not None,
            "enabled": self.enabled,
            "reason": self.reason,
            "crashes": self.crashes,
            "segments": len(self.segments),
            "bytes": sum(s["bytes"] for s in self.segments),
            "cap_bytes": self.cap_bytes,
            "capture": vars(self.run.capture) if self.run else None,
            "live_seq": self.live.seq,
        }

    def loop(self, stop: threading.Event, interval: float = 1.0) -> None:
        while not stop.is_set():
            self.poll()
            stop.wait(interval)
        self.stop("shutdown")
        self.live.close()


def read_index(out_dir: Path) -> list[dict[str, Any]]:
    """The recorded segments (index.json) whose files still exist, oldest first."""
    try:
        idx = json.loads((out_dir / "index.json").read_text())
        segs = [s for s in idx.get("segments", []) if is_obj(s) and (out_dir / str(s.get("file"))).exists()]
    except (OSError, ValueError, AttributeError):
        return []
    return sorted(segs, key=lambda s: s.get("start") or 0)


# ------------------------------------------------------------ a bench run's recording


class RecordingError(RuntimeError):
    """Recording is on but ffmpeg isn't recording the game."""


@dataclass
class RunRecording:
    """One run's recorder on its own thread (a bench run: LiveRunHost). start() polls once with the game up and raises
    RecordingError unless ffmpeg is running, so a run with recording on never starts unrecorded; check() raises it if
    ffmpeg has exited since (before the kickoff); stop() ends the thread, which stops ffmpeg (the open segment is
    closed and indexed)."""

    rec: Recorder
    interval: float = 1.0
    halt: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self.rec.poll()
        except OSError as e:  # no ffmpeg binary, an unwritable out dir
            raise RecordingError(f"recording is on but the recorder can't start: {e}") from e
        self.check()
        self.thread = threading.Thread(target=self.rec.loop, args=(self.halt, self.interval), daemon=True)
        self.thread.start()

    def check(self) -> None:
        run = self.rec.run
        if run is None or run.proc.poll() is not None:
            why = self.rec.reason if run is None else f"ffmpeg exited ({run.proc.poll()})"
            raise RecordingError(f"recording is on but the recorder isn't recording: {why} (see {self.rec.out})")

    def stop(self) -> None:
        self.halt.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        else:
            self.rec.stop("run end")


# A bench run is recorded at the evidence video's game size (bg3harness.video.draw GAME_W x GAME_H), so the render
# places it unscaled
RUN_W, RUN_H = 1408, 792


def run_recording(out_dir: Path, env: Mapping[str, str], runtime: Path, encoder: str = "h264_nvenc") -> RunRecording:
    """A bench run's recording into out_dir: the game's window on env's DISPLAY at RUN_W x RUN_H, no live view, the
    resource guard on runtime/thermal.log and out_dir's free space (low RAM, heat or under 1 GB free stops it until it
    clears: the run's video then has a gap with its reason in index.json's events, which the evidence video shows as
    a pause card; bg3harness.video.timeline.cuts)."""
    spec = RecordSpec(out_dir, RUN_W, RUN_H, encoder=encoder, live=False)
    probe = game_probe(env.get("DISPLAY") or ":1")
    guard = resource_guard(runtime / "thermal.log", disk=out_dir)
    return RunRecording(Recorder(spec, probe, time.time, guard))
