"""run_host.stop_on_signals in a real process: play.py bench's Ctrl-C (live7 §1b: a KeyboardInterrupt left no record and
the game up). A child process runs one bench run on the fake runner of test_run_host and waits, with signal.pause(),
for the session to end the way the runner's agent loop ends it on a stop; the test sends the signals only after the
child said it is ready for them (no sleeps).

- one SIGINT (or SIGTERM): the run is recorded (operator stop, invalid), its game stopped, nav files back, exit 0;
- a second SIGINT while the session doesn't end: KeyboardInterrupt, and the finally's host.abort() kills the play
  process and stops the game; no record."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

CHILD = r"""
import json, signal, sys
sys.path.insert(0, sys.argv[1])
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H
from bg3harness.runner import state as rstate
from bg3harness.session import SessionOutcome
from test_run_host import JOB, SAVE_SC, FakeRunner

calls, runners = [], []
renv.SE_BG3AI_DIR.mkdir(parents=True, exist_ok=True)
for n in ("nav_aa.json", "other.json"):
    (renv.SE_BG3AI_DIR / n).write_text("{}")

def make(opt):
    runners.append(FakeRunner(opt, calls))
    return runners[-1]

def log(msg):
    print("LOG " + msg, flush=True)

host = H.LiveRunHost(make, run_files=lambda *a: {}, env={})
H.stop_on_signals(host, log)
run_dir = renv.RUNTIME / "sessions" / "m-grove"
run_dir.mkdir(parents=True)
try:
    host.start(JOB, SAVE_SC, run_dir)
    print("READY", flush=True)
    while not (runners[-1].stop_requested and sys.argv[2] == "ends"):  # the agent loop's check
        signal.pause()
    res = rstate.SessionResult("S001", "c", None, "stopped", 0.0, 60.0)
    rec = host.record(JOB, SAVE_SC, run_dir, SessionOutcome(res, 0.0))
    print("RECORD " + json.dumps({k: rec[k] for k in ("outcome", "reason", "operator")}), flush=True)
finally:
    host.abort()
    print("CALLS " + json.dumps(calls), flush=True)
    print("SE " + json.dumps(sorted(p.name for p in renv.SE_BG3AI_DIR.iterdir())), flush=True)
"""


def child(tmp_path: Path, mode: str) -> subprocess.Popen[str]:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    env = dict(os.environ)
    env.update(
        BG3AI_RUNTIME=str(tmp_path / "runtime"),
        BG3AI_SE_DIR=str(tmp_path / "se"),
        BG3AI_SAVE_DIR=str(tmp_path / "saves"),
        BG3AI_PROFILE_DIR=str(tmp_path / "profile"),
    )
    return subprocess.Popen(
        [sys.executable, str(script), str(TESTS), mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env,
    )  # fmt: skip


def read_until(p: subprocess.Popen[str], prefix: str) -> str:
    assert p.stdout is not None
    for line in p.stdout:
        if line.startswith(prefix):
            return line
    raise AssertionError(f"child ended before {prefix!r}: {p.wait()} {p.stderr.read() if p.stderr else ''}")


def lines(out: str) -> dict[str, object]:
    return {ln.split(" ", 1)[0]: json.loads(ln.split(" ", 1)[1]) for ln in out.splitlines() if ln[:1].isupper()
            and ln.split(" ", 1)[0] in ("RECORD", "CALLS", "SE")}  # fmt: skip


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_one_signal_ends_the_run_recorded_with_its_game_stopped(tmp_path: Path, sig: signal.Signals):
    p = child(tmp_path, "ends")
    read_until(p, "READY")
    p.send_signal(sig)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, err
    got = lines(out)
    assert got["RECORD"] == {"outcome": "gave_up", "reason": "stopped", "operator": "stop"}
    assert got["CALLS"][-2:] == ["end_facts(gave_up)", "end_run_game(run end)"]  # pyright: ignore[reportIndexIssue]
    assert got["SE"] == ["nav_aa.json", "other.json"]  # the moved nav file is back


def test_a_second_signal_exits_at_once_and_still_stops_the_game(tmp_path: Path):
    p = child(tmp_path, "hangs")  # a session that doesn't end on the stop
    read_until(p, "READY")
    p.send_signal(signal.SIGINT)
    read_until(p, "LOG signal")  # the first one was taken: host.stop()
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=60)
    assert p.returncode != 0 and "KeyboardInterrupt" in err
    got = lines(out)
    assert "RECORD" not in got
    assert got["CALLS"][-2:] == ["kill_agent", "end_run_game(operator exit)"]  # pyright: ignore[reportIndexIssue]
    assert got["SE"] == ["nav_aa.json", "other.json"]
