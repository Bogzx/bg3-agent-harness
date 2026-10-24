"""The real adapters behind stress_exec's Protocols: the clock, RAM and heat, the Story save folder, game.lock, and
the three ways a check drives the game (tools/dev/live_window.py, a probe script under a game the live window's
steps start, runner/play.py run). Only exercised live; the logic they feed is tested with fakes."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import ModuleType

from bg3bench import facts as bfacts
from bg3bench.scenario_v1 import save_digest
from stress_exec import AccOutcome, LockBusyError, ProbeOutcome, SaveError, SessionOutcome, wait_loaded

REPO = Path(__file__).resolve().parents[2]
SAVE_FOLDER_RE = re.compile(r"^(?P<owner>.+)-(?P<num>\d+)__(?P<label>.+)$")
COPY_PREFIX = "SCN-stress_"  # the only save folders this tool ever replaces
LOCK_WAIT_S = 1800


def load_file(name: str, path: Path) -> ModuleType:
    """A module from a file of another tool (stdlib-only at import), without touching sys.path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def acceptance_sections(repo: Path = REPO) -> list[str]:
    return list(load_file("stress_acc_core", repo / "tools" / "dev" / "acceptance" / "core.py").SECTIONS)


def read_profile(path: str, repo: Path = REPO) -> dict:
    with open(repo / path, "rb") as f:
        return tomllib.load(f)


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ProcHost:
    """MemAvailable, the hottest CPU sensor (k10temp/coretemp/zenpower, as tools/orchestrator/thermal_log.sh), and
    runtime/stress/STOP (touch it to stop before the next step)."""

    CPU_SENSORS = ("k10temp", "coretemp", "zenpower")

    def __init__(self, stop_file: Path, proc: Path = Path("/proc"), hwmon: Path = Path("/sys/class/hwmon")):
        self.stop_file, self.proc, self.hwmon = stop_file, proc, hwmon

    def mem_available_gb(self) -> float:
        m = re.search(r"^MemAvailable:\s+(\d+) kB", (self.proc / "meminfo").read_text(), re.M)
        return int(m.group(1)) / 1024**2 if m else 0.0

    def cpu_temp_c(self) -> float | None:
        temps = []
        for h in self.hwmon.glob("hwmon*"):
            try:
                if (h / "name").read_text().strip() not in self.CPU_SENSORS:
                    continue
                temps += [int(t.read_text()) / 1000 for t in h.glob("temp*_input")]
            except (OSError, ValueError):
                continue
        return max(temps) if temps else None

    def stop_requested(self) -> bool:
        return self.stop_file.exists()


class DirSaves:
    """The Story save folder. copy() makes `<owner>-<id>__SCN-stress_<row>` from the newest folder with the label
    (as load_game picks), checked byte-equal (.lsv sha1) to it; the original is only read. The copy's .lsv and .WebP
    are renamed after its label, as the game names them (scenario_v1.save_lsv): BG3 sits at 0 % on the loading
    screen for good on a folder whose .lsv keeps the source's label (live5 §0.3)."""

    def __init__(self, save_dir: Path):
        self.dir = save_dir

    def find(self, label: str) -> Path:
        hits = [
            p
            for p in self.dir.iterdir()
            if (m := SAVE_FOLDER_RE.match(p.name)) and m.group("label") == label and m.group("owner") != "PROBE"
        ]
        if not hits:
            raise SaveError(f"no save labelled {label!r} in {self.dir}")
        return max(hits, key=lambda p: p.stat().st_mtime)

    def copy(self, label: str, new_label: str) -> tuple[str, str]:
        if not new_label.startswith(COPY_PREFIX):
            raise SaveError(f"refusing to write a save named {new_label!r} (only {COPY_PREFIX}* copies)")
        src = self.find(label)
        m = SAVE_FOLDER_RE.match(src.name)
        assert m
        dst = self.dir / f"{m.group('owner')}-{m.group('num')}__{new_label}"
        want = save_digest(self.dir, src.name)
        if want is None:
            raise SaveError(f"{src.name} has no single .lsv")
        if dst.exists() and save_digest(self.dir, dst.name) != want:
            shutil.rmtree(dst)  # our own older copy of another start (the name is checked above)
        if not dst.exists():
            shutil.copytree(src, dst)
        for f in list(dst.iterdir()):
            if f.suffix in (".lsv", ".WebP") and f.stem != new_label:
                f.rename(dst / f"{new_label}{f.suffix}")
        if save_digest(self.dir, dst.name) != want:
            raise SaveError(f"the copy {dst.name} differs from {src.name}")
        return dst.name, m.group("owner")


class FlockLock:
    """runtime/game.lock (the fd is close-on-exec: the game never inherits the hold)."""

    def __init__(self, path: Path, wait_s: float = LOCK_WAIT_S, clock: SystemClock | None = None):
        self.path, self.wait_s, self.clock = path, wait_s, clock or SystemClock()

    @contextmanager
    def hold(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            end = self.clock.now() + self.wait_s
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.clock.now() >= end:
                        raise LockBusyError(f"{self.path} held for {self.wait_s:.0f} s by another process") from None
                    self.clock.sleep(5)
            yield


class JsonStore:
    def __init__(self, path: Path, new: dict):
        self.path, self.new = path, new

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return json.loads(json.dumps(self.new))

    def save(self, results: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)


def run_logged(
    argv: list[str], log: Path, cwd: Path, env: dict, timeout_s: float, header: str = ""
) -> tuple[int | None, str]:
    """argv with its output in log (after the header line, if any); (exit code or None on a timeout, the log). The
    whole process group is killed on a timeout."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as lf:
        if header:
            lf.write(header + "\n")
            lf.flush()
        p = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            rc: int | None = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            rc = None
    return rc, log.read_text(errors="replace")


class LiveWindowAcc:
    """tools/dev/live_window.py --only <sections>: it takes game.lock (waiting up to LOCK_WAIT_S), launches,
    runs the acceptance, stops the game; its window folder (in the machine's runtime/live) holds
    acceptance/results.json."""

    def __init__(self, repo: Path, runtime: Path, machine: Path, timeout_s: float = 4 * 3600):
        self.repo, self.runtime, self.machine, self.timeout_s = repo, runtime, machine, timeout_s

    def run(self, sections: tuple[str, ...]) -> AccOutcome:
        live = self.machine / "live"
        before = set(live.iterdir()) if live.is_dir() else set()
        argv = [
            "python3",
            str(self.repo / "tools/dev/live_window.py"),
            "--only",
            ",".join(sections),
            "--lock-wait",
            str(LOCK_WAIT_S),
        ]
        rc, out = run_logged(argv, self.runtime / "stress" / "acc.log", self.repo, dict(os.environ), self.timeout_s)
        new = sorted(set(live.iterdir()) - before) if live.is_dir() else []
        if not new:
            return AccOutcome(None, str(self.runtime / "stress" / "acc.log"), f"live_window exit {rc}: no window")
        where = new[-1]
        try:
            return AccOutcome(json.loads((where / "acceptance" / "results.json").read_text()), str(where))
        except (OSError, ValueError):
            last = [ln for ln in out.splitlines() if "FAIL" in ln][-1:] or [f"exit {rc}"]
            return AccOutcome(None, str(where), f"no acceptance results ({last[0].strip()[:200]})")


class WindowProbe:
    """A probe script against a game started on the save copy by live_window's own steps (display, Steam
    offline, bg3le, ramwatch, launch), then stopped the same way. The launch returns at the main menu, so the probe
    starts once the save is in play (stress_exec.wait_loaded; the wait is the probe log's first line). The caller
    holds game.lock. The window is the machine's (its ramwatch.lock, game.pid, logs, runtime/live folder): with
    the run's own --runtime it saw no ramwatch and started a second one, which refused (live6)."""

    def __init__(self, repo: Path, runtime: Path, machine: Path):
        self.repo, self.runtime, self.machine = repo, runtime, machine

    def run(
        self, script: str, args: list[str], save_folder: str | None, out_dir: str, timeout_s: float
    ) -> ProbeOutcome:
        path = self.repo / script
        out = self.runtime / out_dir
        log = out / "probe.log"
        if not path.is_file():
            return ProbeOutcome(None, "", str(log), f"no probe script {script}")
        lw = load_file("stress_live_window", self.repo / "tools/dev/live_window.py")
        w = lw.Window(lw.parse(["--save", save_folder] if save_folder else []), repo=self.repo, runtime=self.machine)
        w.dir.mkdir(parents=True, exist_ok=True)
        c = w.commands()
        up = (
            w.display()
            and w.simple("steam", c["steam"], "Steam offline", "Steam is not offline", 60)
            and w.procs.steam_running()
            and w.simple("bg3le", c["bg3le"], "dist complete", "stale bg3le dist", 60)
        )
        if up:
            w.ramwatch()
            up = w.launch()
        if not up:
            w.stop()
            bad = [r for r in w.rows if r["verdict"] == "FAIL"]
            return ProbeOutcome(None, "", str(w.dir), f"game not up: {bad[-1]['evidence'] if bad else 'no Steam'}")
        from bg3harness.game import io as gio

        loaded, waited = wait_loaded(gio.server_ready, gio.ui_screen, SystemClock())
        if not loaded:
            w.stop()
            return ProbeOutcome(None, "", str(w.dir), f"game up, but {waited}")
        env = dict(w.env, PYTHONPATH=str(path.parent), BG3AI_STRESS_SAVE=save_folder or "")
        runner = ["bash"] if path.suffix == ".sh" else ["uv", "run", "--project", str(self.repo / "bridge"), "python"]
        try:
            rc, text = run_logged([*runner, str(path), *args], log, out, env, timeout_s, header=f"stress: {waited}")
        finally:
            w.stop()
        return ProbeOutcome(rc, text, str(log))


class PlaySession:
    """runner/play.py run --scenario on the resolved scenario (written to runtime/stress/scenarios/, which
    BG3AI_SCENARIOS points play.py at), one run, no judge or keeper pass; the run's runs.jsonl line and
    facts.jsonl from the machine's runtime/sessions/<playthrough>/ (where play.py writes them)."""

    MARGIN_S = 1800  # launch, load and wrap-up beyond the run's own cap

    def __init__(self, repo: Path, runtime: Path, machine: Path):
        self.repo, self.runtime, self.machine = repo, runtime, machine

    def run(self, scenario: dict, profile: str, max_cost: float | None, playthrough: str) -> SessionOutcome:
        sc_dir = self.runtime / "stress" / "scenarios"
        sc_dir.mkdir(parents=True, exist_ok=True)
        (sc_dir / f"{scenario['name']}.json").write_text(json.dumps(scenario, indent=1, ensure_ascii=False) + "\n")
        hours = scenario["caps"]["run_hours"]
        argv = [
            "python3",
            str(self.repo / "runner/play.py"),
            "run",
            "--scenario",
            scenario["name"],
            "--playthrough",
            playthrough,
            "--runs",
            "1",
            "--profile",
            profile,
            "--no-judge",
            "--no-keeper",
            "--hours",
            f"{hours + self.MARGIN_S / 3600:.3f}",
        ]
        if max_cost is not None:
            argv += ["--max-cost", f"{max_cost:g}"]
        env = dict(os.environ, BG3AI_SCENARIOS=str(sc_dir))
        pt_dir = self.machine / "sessions" / playthrough
        log = self.runtime / "stress" / f"{playthrough}.log"
        rc, out = run_logged(argv, log, self.repo, env, hours * 3600 + self.MARGIN_S)
        if rc == 2:  # play.py refused before playing (its reason is the last line)
            last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
            return SessionOutcome(None, [], str(log), f"play.py refused: {last.strip()[:250]}")
        lines = []
        with suppress(OSError, ValueError):
            lines = [json.loads(ln) for ln in (pt_dir / "runs.jsonl").read_text().splitlines() if ln.strip()]
        record = next((r for r in reversed(lines) if r.get("playthrough") == playthrough), None)
        try:
            facts = bfacts.load(pt_dir / "facts.jsonl")
        except (OSError, ValueError):
            facts = []
        return SessionOutcome(record, facts, f"{pt_dir} (exit {rc}, log {log})")
