import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bg3play import recorder as R
from bg3play.recorder import Capture, Recorder, RecordSpec

FAKE = str(Path(__file__).parent / "bin" / "fake-ffmpeg")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------ the command line


def test_args_window_capture_nvenc(tmp_path: Path):
    a = R.ffmpeg_args(RecordSpec(tmp_path), Capture(":1", "0x2a00007"), "rec-100")
    s = " ".join(a)
    assert a[0] == "ffmpeg" and "-f x11grab -framerate 15 -draw_mouse 0 -window_id 0x2a00007 -i :1" in s
    assert "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720" in s and "fps=15," in s
    assert "-c:v h264_nvenc -preset p4" in s and "-g 30" in s
    assert "-segment_time 600 -reset_timestamps 1 -segment_format mp4" in s
    assert f"-segment_list {tmp_path}/rec-100.csv -segment_list_type csv" in s
    assert f"{tmp_path}/rec-100-%04d.mp4" in a
    assert a[-6:] == ["-c:v", "mjpeg", "-q:v", "7", "-f", "mpjpeg", "pipe:1"][-6:]
    assert "split=2[rec][lv0];[lv0]fps=3,scale=640:-2[lv]" in s


def test_args_full_display_x264_no_live(tmp_path: Path):
    a = R.ffmpeg_args(RecordSpec(tmp_path, encoder="libx264", live=False, segment_s=60, fps=10), Capture(":0"), "p")
    s = " ".join(a)
    assert "-window_id" not in a and "-i :0" in s and "-framerate 10" in s
    assert "-c:v libx264 -preset ultrafast" in s and "-segment_time 60" in s
    assert "mpjpeg" not in a and "split" not in s


def test_args_source_override_and_bad_encoder(tmp_path: Path):
    spec = RecordSpec(tmp_path, source=("-f", "lavfi", "-i", "testsrc"), duration_s=3)
    a = R.ffmpeg_args(spec, Capture(), "p")
    assert "x11grab" not in a and a[a.index("-t") + 1 : a.index("-t") + 6] == ["3", "-f", "lavfi", "-i", "testsrc"]
    with pytest.raises(ValueError):
        R.ffmpeg_args(RecordSpec(tmp_path, encoder="vp9"), Capture(), "p")


# ------------------------------------------------------------ index, cap, locate


def test_segment_list_and_run_rows():
    listed = R.parse_segment_list("rec-1-0000.mp4,0.000000,600.000000\n/x/rec-1-0001.mp4,600.0,1200.0\nbad\n")
    assert listed == [("rec-1-0000.mp4", 0.0, 600.0), ("rec-1-0001.mp4", 600.0, 1200.0)]
    files = ["rec-1-0002.mp4", "rec-1-0000.mp4", "rec-1-0001.mp4", "rec-2-0000.mp4", "rec-1.csv"]
    rows = R.run_segments("rec-1", 1000.0, listed, files, {"rec-1-0002.mp4": 7}, None)
    assert [(r["file"], r["start"], r["end"]) for r in rows] == [
        ("rec-1-0000.mp4", 1000.0, 1600.0),
        ("rec-1-0001.mp4", 1600.0, 2200.0),
        ("rec-1-0002.mp4", 2200.0, None),
    ]
    assert rows[2]["bytes"] == 7 and rows[0]["bytes"] == 0
    crashed = R.run_segments("rec-1", 1000.0, [], ["rec-1-0000.mp4"], {}, 1042.5)
    assert crashed[0]["end"] == 1042.5


def seg(f: str, start: float, end: float | None, n: int) -> dict:
    return {"file": f, "run": "r", "start": start, "end": end, "bytes": n}


def test_over_cap_deletes_oldest_finished_only():
    segs = [seg("c", 30, None, 50), seg("a", 10, 20, 40), seg("b", 20, 30, 40)]
    assert R.over_cap(segs, 200) == []
    assert [s["file"] for s in R.over_cap(segs, 100)] == ["a"]
    assert [s["file"] for s in R.over_cap(segs, 10)] == ["a", "b"]  # never the open one


def test_locate():
    segs = [seg("b", 600, None, 1), seg("a", 0, 600, 1)]
    assert R.locate(segs, -1) is None
    assert R.locate(segs, 12.5)[0]["file"] == "a" and R.locate(segs, 12.5)[1] == 12.5
    assert R.locate(segs, 600)[0]["file"] == "b" and R.locate(segs, 9999)[1] == 9399


# ------------------------------------------------------------ mjpeg, probe, guard


def test_mjpeg_parser_split_chunks():
    part = b"--ffmpeg\r\nContent-type: image/jpeg\r\nContent-length: %d\r\n\r\n%s\r\n"
    data = part % (3, b"abc") + part % (5, b"hello")
    p = R.MjpegParser()
    frames = []
    for i in range(0, len(data), 7):
        frames += p.feed(data[i : i + 7])
    assert frames == [b"abc", b"hello"]


def test_live_frame_wait():
    lf = R.LiveFrame()
    assert lf.get() == (0, None)
    lf.put(b"x")
    assert lf.get(after=0, timeout=5) == (1, b"x")
    lf.close()
    assert lf.get(after=1, timeout=5) == (1, b"x")  # closed: returns at once


def test_find_window():
    tree = """
     0x200001 "Steam": ("steam" "Steam")  800x600+0+0  +0+0
     0x2a00007 "Baldur's Gate 3 (4.1.1.6758295)": ("bg3" "bg3")  1920x1080+0+0  +0+0
     0x2a00003 "Baldur's Gate 3": ("bg3" "bg3")  10x10+0+0  +0+0
    """
    assert R.find_window(tree) == "0x2a00007"
    assert R.find_window('0x1 "Konsole": ()  10x10+0+0') is None


def test_game_probe_modes():
    tree = '0x5 "Baldur\'s Gate 3": ()  1920x1080+0+0'
    up = {"v": False}
    probe = R.game_probe(":1", "window", running=lambda: up["v"], tree=lambda d: tree)
    assert probe() is None
    up["v"] = True
    assert probe() == Capture(":1", "0x5")
    assert R.game_probe(":1", "window", running=lambda: True, tree=lambda d: "")() is None  # window not up yet
    assert R.game_probe(":2", "display", running=lambda: True, tree=lambda d: "")() == Capture(":2")


def test_game_running(tmp_path: Path):
    (tmp_path / "123").mkdir()
    (tmp_path / "123" / "comm").write_text("bash\n")
    assert not R.game_running(tmp_path)
    (tmp_path / "456").mkdir()
    (tmp_path / "456" / "comm").write_text("bg3\n")
    assert R.game_running(tmp_path)


def test_thermal_and_meminfo_parsers():
    line = "2026-10-21 22:44:18 cpu=80 acpi=80 amdgpu=71 nvme=51 nv=[70,34.96,1155,100,0x4] profile=balanced"
    assert R.thermal_reading(line) == (80, 70)
    assert R.thermal_reading("2026-10-21 22:44:18 cpu=- acpi=- amdgpu=55 nvme=- nv=[] profile=") == (None, 55)
    assert R.thermal_reading("garbage") == (None, None)
    assert R.mem_available_mb("MemTotal: 16000000 kB\nMemAvailable:    2048000 kB\n") == 2000


def test_resource_guard(tmp_path: Path):
    mem, therm = tmp_path / "meminfo", tmp_path / "thermal.log"
    mem.write_text("MemAvailable: 4096000 kB\n")
    clock = Clock(time.time())
    guard = R.resource_guard(therm, R.GuardLimits(), clock, mem)
    assert guard() is None  # no thermal log
    therm.write_text("old\n2026-10-21 22:44:18 cpu=91 acpi=80 amdgpu=71 nvme=51 nv=[70,1,1,1,0x4] profile=b\n")
    assert guard() == "heat: cpu 91 C >= 90"
    therm.write_text("2026-10-21 22:44:18 cpu=60 acpi=80 amdgpu=71 nvme=51 nv=[83,1,1,1,0x4] profile=b\n")
    assert guard() == "heat: gpu 83 C >= 82"
    clock.t = therm.stat().st_mtime + 31  # stale thermal line: ignored
    assert guard() is None
    mem.write_text("MemAvailable: 1024000 kB\n")
    assert guard() == "ram: 1000 MB available < 1500"


# ------------------------------------------------------------ the recorder over a fake ffmpeg


@pytest.fixture
def fake_env(monkeypatch):
    for k in ("FAKE_SEGMENTS", "FAKE_BYTES", "FAKE_EXIT"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def make(tmp_path: Path, clock: Clock, game: dict, guard: dict | None = None, **kw) -> Recorder:
    guard = guard if guard is not None else {"why": None}
    return Recorder(
        RecordSpec(tmp_path / "video"),
        probe=lambda: game.get("cap"),
        clock=clock,
        guard=lambda: guard["why"],
        ffmpeg=FAKE,
        **kw,
    )


def test_recorder_follows_the_game_and_indexes(tmp_path: Path, fake_env):
    fake_env.setenv("FAKE_SEGMENTS", "3")
    clock, game = Clock(1000.0), {"cap": None}
    rec = make(tmp_path, clock, game)
    rec.poll()
    assert rec.run is None and rec.reason == "game not running"
    game["cap"] = Capture(":1", "0x5")
    rec.poll()
    assert rec.run is not None and rec.reason == "recording" and rec.state()["recording"]
    assert rec.live.get(after=0, timeout=10) == (1, b"JPG1")  # fake wrote its segments before the frame
    rec.poll()
    idx = json.loads((tmp_path / "video" / "index.json").read_text())
    assert [(s["file"], s["start"], s["end"]) for s in idx["segments"]] == [
        ("rec-1000-0000.mp4", 1000.0, 1600.0),
        ("rec-1000-0001.mp4", 1600.0, 2200.0),
        ("rec-1000-0002.mp4", 2200.0, None),
    ]
    game["cap"] = None
    clock.t = 3000.0
    rec.poll()  # game gone: SIGTERM, the fake lists the last segment like ffmpeg does
    assert rec.run is None and rec.reason == "game not running"
    assert rec.segments[-1]["end"] == 2205.0 and R.read_index(tmp_path / "video")[-1]["end"] == 2205.0


def test_recorder_restarts_a_crashed_ffmpeg_with_backoff(tmp_path: Path, fake_env):
    fake_env.setenv("FAKE_SEGMENTS", "1")
    fake_env.setenv("FAKE_EXIT", "1")
    clock, game = Clock(1000.0), {"cap": Capture()}
    rec = make(tmp_path, clock, game)
    rec.poll()
    rec.run.proc.wait(10)
    clock.t = 1002.0
    rec.poll()
    assert rec.run is None and rec.crashes == 1 and rec.reason == "restart backoff (ffmpeg exited 1, 1 crashes)"
    assert rec.segments[0]["end"] == 1002.0  # an unlisted last segment closes at the crash
    clock.t = 1006.0
    rec.poll()
    assert rec.run is None and rec.reason.startswith("restart backoff")
    clock.t = 1007.0
    rec.poll()
    assert rec.run is not None and rec.run.prefix == "rec-1007"
    rec.run.proc.wait(10)
    clock.t = 1008.0
    rec.poll()
    assert rec.crashes == 2 and rec.next_start == 1018.0
    clock.t = 1018.0
    fake_env.delenv("FAKE_EXIT")
    rec.poll()
    assert rec.run is not None
    rec.live.get(after=2, timeout=10)
    rec.stop()
    assert [s["run"] for s in rec.segments] == ["rec-1000", "rec-1007", "rec-1018"]


def test_recorder_disk_cap_deletes_oldest(tmp_path: Path, fake_env):
    fake_env.setenv("FAKE_SEGMENTS", "4")
    fake_env.setenv("FAKE_BYTES", "1000")
    clock, game = Clock(1000.0), {"cap": Capture()}
    rec = make(tmp_path, clock, game, cap_bytes=2500)
    rec.poll()
    rec.live.get(after=0, timeout=10)
    rec.poll()
    out = tmp_path / "video"
    assert sorted(f.name for f in out.glob("*.mp4")) == ["rec-1000-0002.mp4", "rec-1000-0003.mp4"]
    assert [s["file"] for s in R.read_index(out)] == ["rec-1000-0002.mp4", "rec-1000-0003.mp4"]
    rec.stop()


def test_recorder_guard_stops_and_cools_down(tmp_path: Path, fake_env):
    clock, game, guard = Clock(1000.0), {"cap": Capture()}, {"why": None}
    rec = make(tmp_path, clock, game, guard)
    rec.poll()
    assert rec.run is not None
    rec.live.get(after=0, timeout=10)
    guard["why"] = "heat: gpu 83 C >= 82"
    clock.t = 1010.0
    rec.poll()
    assert rec.run is None and rec.reason == "guard: heat: gpu 83 C >= 82"
    guard["why"] = None
    clock.t = 1100.0
    rec.poll()
    assert rec.run is None and rec.reason == "guard cooldown"
    clock.t = 1130.0
    rec.poll()
    assert rec.run is not None
    rec.enabled = False
    rec.poll()
    assert rec.run is None and rec.reason == "disabled"


def test_recorder_restarts_when_the_window_changes(tmp_path: Path, fake_env):
    clock, game = Clock(1000.0), {"cap": Capture(":1", "0x5")}
    rec = make(tmp_path, clock, game)
    rec.poll()
    rec.live.get(after=0, timeout=10)
    first = rec.run
    game["cap"] = Capture(":1", "0x9")  # the game restarted: new window
    clock.t = 1100.0
    rec.poll()
    assert rec.run is not first and rec.run.capture.window_id == "0x9"
    rec.stop()


def test_recorder_keeps_the_index_across_restarts(tmp_path: Path, fake_env):
    clock, game = Clock(1000.0), {"cap": Capture()}
    rec = make(tmp_path, clock, game)
    rec.poll()
    rec.live.get(after=0, timeout=10)
    rec.stop()
    again = make(tmp_path, clock, game)
    assert [s["file"] for s in again.segments] == ["rec-1000-0000.mp4", "rec-1000-0001.mp4"]


# ------------------------------------------------------------ one real ffmpeg run


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_real_ffmpeg_writes_a_playable_mp4(tmp_path: Path):
    out = tmp_path / "video"
    spec = RecordSpec(
        out,
        encoder="libx264",
        segment_s=2,
        duration_s=3,
        source=("-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=15"),
    )
    rec = Recorder(spec, probe=lambda: Capture(), clock=time.time)
    rec.poll()
    assert rec.run.proc.wait(60) == 0
    rec.poll()
    segs = R.read_index(out)
    assert [s["file"] for s in segs] == [f"{segs[0]['run']}-0000.mp4", f"{segs[0]['run']}-0001.mp4"]
    assert all(s["end"] is not None for s in segs) and rec.live.seq >= 3
    assert rec.live.data[:2] == b"\xff\xd8"
    total = 0.0
    for s in segs:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,pix_fmt:format=duration",
                "-of",
                "json",
                str(out / s["file"]),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=os.environ,
        )
        info = json.loads(probe.stdout)
        st = info["streams"][0]
        assert (st["codec_name"], st["width"], st["height"], st["pix_fmt"]) == ("h264", 1280, 720, "yuv420p")
        total += float(info["format"]["duration"])
    assert 2.5 <= total <= 3.5
    dec = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out / segs[0]["file"]), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dec.returncode == 0 and dec.stderr == ""
