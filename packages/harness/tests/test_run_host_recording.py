# pyright: basic
# ruff: noqa: F811  (the tests take test_run_host's rig fixture by name)
"""bg3harness.runner.run_host's recording: a bench run with recording on is screen-recorded from the game's start to
its run_end into <run_dir>/recording/, and a run whose recorder isn't recording doesn't start (fake ffmpeg process,
no game, no display)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO

import pytest
from bg3harness import recorder as hrec
from bg3harness.runner import run_host as H  # noqa: N812
from test_run_host import JOB, SAVE_SC, Rig, outcome, rig, run_dir  # noqa: F401  (rig: fixture)


class FakeProc:
    stdout: IO[bytes] | None = None

    def __init__(self, calls: list[str], exit_after: str | None):
        self.calls, self.exit_after, self.code = calls, exit_after, None

    def poll(self) -> int | None:
        return 1 if self.exit_after is not None and self.exit_after in self.calls else self.code

    def terminate(self) -> None:
        self.calls.append("ffmpeg stop")
        self.code = 0

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


def recording_on(
    rig: Rig, window: str | None = "0x5", exit_after: str | None = None, spawn_error: OSError | None = None
) -> list[list[str]]:
    """Recording on for rig's host: the game's window is `window`; ffmpeg exits once rig.calls has `exit_after`."""
    spawned: list[list[str]] = []

    def spawn(args: list[str], log: Path) -> FakeProc:
        if spawn_error is not None:
            raise spawn_error
        spawned.append(args)
        rig.calls.append("ffmpeg start")
        return FakeProc(rig.calls, exit_after)

    def recording(out: Path) -> hrec.RunRecording:
        spec = hrec.RecordSpec(out, live=False)
        probe = (lambda: hrec.Capture(":1", window)) if window else (lambda: None)
        return hrec.RunRecording(hrec.Recorder(spec, probe, lambda: 1000.0, spawn=spawn), interval=1e9)

    rig.h.recording = recording
    return spawned


def test_a_bench_run_is_recorded_from_the_game_up_to_its_run_end(rig: Rig):
    spawned = recording_on(rig)
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    assert rig.calls == ["snapshot", "ensure_game(True)", "ffmpeg start", "reload", "check_settings", "settings"]
    (args,) = spawned
    out = run_dir("m-grove") / "recording"
    assert args[args.index("-window_id") + 1] == "0x5" and args[-1] == str(out / "rec-1000-%04d.mp4")
    assert "pipe:1" not in args  # no live view
    rig.calls.clear()
    rig.h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    # after run_end, before the game stops (the recording covers the whole run)
    assert rig.calls == ["end_facts(timeout)", "ffmpeg stop", "scenario_undo", "end_run_game(run end)"]
    idx = json.loads((out / "index.json").read_text())
    assert idx["segments"] == [] and [e["ev"] for e in idx["events"]] == ["start", "stop"]  # the fake wrote none
    assert idx["events"][-1]["reason"] == "shutdown"
    assert rig.h.recordings == {}


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"window": None}, "recorder isn't recording: game not running"),  # no game window to record
        ({"exit_after": "ffmpeg start"}, r"ffmpeg exited \(1\)"),  # ffmpeg died at once (a bad encoder, no display)
        ({"exit_after": "check_settings"}, r"ffmpeg exited \(1\)"),  # died while the settings were checked
        ({"spawn_error": FileNotFoundError("ffmpeg")}, "recorder can't start: ffmpeg"),  # no ffmpeg binary
    ],
)
def test_a_run_whose_recorder_isnt_recording_doesnt_start(rig: Rig, kw: dict, why: str):
    recording_on(rig, **kw)
    with pytest.raises(hrec.RecordingError, match=why):
        rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    # start raised (no kickoff): the game it brought up stopped, an ffmpeg it started stopped before it
    assert rig.calls[-2:] == ["scenario_undo", "end_run_game(start failed)"]
    assert ("ffmpeg start" in rig.calls) == ("ffmpeg stop" in rig.calls)
    assert (rig.h.runs, rig.h.recordings) == ({}, {})


def test_an_abort_stops_the_recording(rig: Rig):
    recording_on(rig)
    rig.h.start(JOB, SAVE_SC, run_dir("m-grove"))
    rig.calls.clear()
    rig.h.abort()
    assert rig.calls[0] == "ffmpeg stop" and rig.h.recordings == {}


def test_play_py_bench_records_every_run_unless_turned_off():
    assert H.bench_recording({}) is not None and H.bench_recording({"BG3AI_BENCH_RECORD": "1"}) is not None
    assert H.bench_recording({"BG3AI_BENCH_RECORD": "0"}) is None


def test_the_bench_encoder_comes_from_the_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen: list[str] = []

    def run_recording(out: Path, env: object, runtime: Path, encoder: str) -> None:
        seen.append(encoder)

    monkeypatch.setattr(hrec, "run_recording", run_recording)
    for env in ({}, {"BG3AI_BENCH_RECORD_ENCODER": "libx264"}):
        make = H.bench_recording(env)
        assert make is not None
        make(tmp_path)
    assert seen == ["h264_nvenc", "libx264"]
    with pytest.raises(ValueError, match="BG3AI_BENCH_RECORD_ENCODER='vp9'"):
        H.bench_recording({"BG3AI_BENCH_RECORD_ENCODER": "vp9"})
