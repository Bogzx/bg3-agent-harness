# pyright: basic
"""The real SessionRunner's agent loop (run_claude) on a fake clock with a fake agent CLI (harness analysis O5).

The agent is a real process (a script printing claude's stream-json init, then playing deaf to SIGINT/SIGTERM or
exiting); the loop's clock and pause are the test's: each 2 s pause advances the fake clock, so a session's minutes,
the hard stop's graces and the hang watchdog play out in milliseconds, and every assertion is on fake times. The only
real wait is for the agent's first line (its signal handlers are set by then), never a fixed sleep.

Before: run_claude polled with time.sleep(2) on time.time(), so this path was only covered through the legacy CLI on
the real clock (runner/test_crash.py, 6-17 s per test)."""

from __future__ import annotations

import json
import math
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from bg3harness import runs
from bg3harness.game import io as game
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.runner.core import SessionRunner

T0 = 2_000_000_000.0
INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "conv-1",
    "tools": ["mcp__bg3__get_status"],
    "mcp_servers": [{"name": "bg3", "status": "connected"}],
}
AGENT = r"""
import json, signal, sys, time
mode = sys.argv[1]
if mode != "obeys":  # deaf: it says it got the signal and plays on
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda n, f: print("GOT " + signal.Signals(n).name, flush=True))
print(sys.argv[2], flush=True)
if mode == "exits":
    print(json.dumps({"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0.01,
                      "usage": {"input_tokens": 10, "output_tokens": 2}}), flush=True)
    sys.exit(0)
if mode == "auth":  # claude -p with a bad key: an error result, exit 0
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0,
                      "result": "Invalid API key · Please run /login"}), flush=True)
    sys.exit(0)
while True:
    time.sleep(3600)
"""


class FakeClock:
    """The loop's clock: pause(s) advances it. Before it advances it waits (really, on a condition) for the agent to
    have caught up: its first stream line (its signal handlers are set by then), and after each signal the loop sent
    either the agent's "GOT <signal>" line or its exit, and after its result line its exit. So no fake time runs out
    while the real process is still reacting."""

    def __init__(self, stream: Path, sent: list[tuple[str, float]]):
        self.t, self.stream, self.sent, self.seen = T0, stream, sent, -1
        self.runner: Any = None  # its agent_proc is the play process

    def now(self) -> float:
        return self.t

    def _caught_up(self) -> bool:
        text = self.stream.read_text() if self.stream.exists() else ""
        proc = self.runner.agent_proc
        if proc is None or proc.poll() is not None:
            return True
        if '"result"' in text:  # it said it is done: it exits next
            return False
        if self.seen < 0:
            return '"init"' in text
        name = self.sent[-1][0]
        return name != "SIGKILL" and text.count("GOT " + name) >= sum(1 for n, _ in self.sent if n == name)

    def pause(self, s: float) -> None:
        if self.seen < len(self.sent) or '"result"' in (self.stream.read_text() if self.stream.exists() else ""):
            end = time.monotonic() + 60
            while not self._caught_up():
                assert time.monotonic() < end, f"the fake agent didn't react (signals {self.sent})"
                time.sleep(0.005)  # polls a condition; no timing assumption
            self.seen = max(self.seen + 1, len(self.sent)) if self.seen >= 0 else 0
        self.t += s


class Loop(SessionRunner):
    """SessionRunner with the game stubbed (no progress poll, alive or not as the test says)."""

    alive = True

    def progress_loop(self, *a: Any) -> None:  # the 15 s poll reads the game: nothing here
        return None

    def game_alive(self) -> bool:
        return self.alive


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(renv, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(renv, "PLAYTHROUGHS", tmp_path / "playthroughs")
    monkeypatch.setattr(renv, "PLAY_DIR", tmp_path / "play")
    monkeypatch.setattr(renv, "log", lambda msg: None)
    monkeypatch.setattr(renv, "AGENT_SILENT_MINUTES", 60)  # the deaf agent is silent: no hang unless a test wants one
    (tmp_path / "play").mkdir()
    script = tmp_path / "agent.py"
    script.write_text(AGENT)

    def make(mode: str, minutes: float = 1) -> tuple[Loop, FakeClock, list[tuple[str, float]]]:
        r = Loop(rstate.Options("loop-test", None, minutes, "opus", save_owner="Tester"))
        sent: list[tuple[str, float]] = []
        clock = FakeClock(r.state.dir / "S001.jsonl", sent)
        clock.runner = r
        r.clock, r.pause = clock.now, clock.pause  # pyright: ignore[reportAttributeAccessIssue]
        r.agent_cmd = lambda req: [sys.executable, str(script), mode, json.dumps(INIT)]  # pyright: ignore[reportAttributeAccessIssue]
        real = r._signal  # pyright: ignore[reportPrivateUsage]

        def spy(proc: Any, sig: int) -> None:
            sent.append((signal.Signals(sig).name, clock.t))
            real(proc, sig)

        r._signal = spy  # pyright: ignore[reportAttributeAccessIssue]
        return r, clock, sent

    return make


def tick(t: float) -> float:
    """The first of the loop's checks (every 2 fake seconds from T0) at or after t."""
    return T0 + 2 * math.ceil((t - T0) / 2)


def events(r: Loop) -> list[dict[str, Any]]:
    path = r.state.dir / "runner_events.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def test_the_hard_stop_sends_sigint_then_sigterm_then_sigkill_on_the_fake_clock(loop: Any):
    r, _, sent = loop("deaf")
    res = r.run_claude("S001", "KICKOFF", 1, None, needs_game=False)
    hard = tick(T0 + 60 + renv.HARD_STOP_GRACE)  # the deadline (1 min) + the grace, at the loop's next check
    assert (res.reason, res.exit_code, res.claude_session) == ("hard_stop", -signal.SIGKILL, "conv-1")
    term = tick(hard + renv.SIGINT_GRACE)
    kill = tick(term + renv.SIGTERM_GRACE)
    assert sent == [("SIGINT", hard), ("SIGTERM", term), ("SIGKILL", kill)]
    assert res.started == T0 and kill <= res.ended <= kill + 2
    assert not (renv.RUNTIME / "agent.json").exists()  # its group is gone


def test_an_agent_that_obeys_sigint_ends_at_the_first_signal(loop: Any):
    r, _, sent = loop("obeys")
    res = r.run_claude("S001", "KICKOFF", 1, None, needs_game=False)
    assert (res.reason, res.exit_code) == ("hard_stop", -signal.SIGINT)
    assert sent == [("SIGINT", tick(T0 + 60 + renv.HARD_STOP_GRACE))]  # it died of it: nothing after


def test_an_auth_error_is_logged_as_a_harness_failure_event(loop: Any):
    # app review of 0e5fc92: the "auth" API failure was only logged, so a run that played a while before it was valid
    r, _, _ = loop("auth")
    res = r.run_claude("S001", "KICKOFF", 1, None, needs_game=False)
    assert res.reason == "exited"
    auth = [e for e in events(r) if e["kind"] == "api_auth"]
    assert [(e["session"], e["error"]) for e in auth] == [("S001", "Invalid API key · Please run /login")]
    assert [(f["kind"], f["class"]) for f in runs.event_failures(auth)[0]] == [("api_auth", "harness")]


def test_a_silent_agent_is_an_agent_hang(loop: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(renv, "AGENT_SILENT_MINUTES", 2)
    r, _, sent = loop("deaf", minutes=30)
    res = r.run_claude("S001", "KICKOFF", 30, None, needs_game=False)
    assert res.reason == "agent_hang" and "no claude output for 2.0 min" in res.detail
    assert res.fault_at is not None and T0 + 120 <= res.fault_at <= T0 + 124  # the first check past 2 min
    assert sent[0] == ("SIGINT", res.fault_at)
    assert [e["fault"] for e in events(r) if e["kind"] == "fault"] == ["agent_hang"]


def test_a_dead_game_is_a_crash_at_the_first_health_check(loop: Any):
    r, _, sent = loop("deaf", minutes=30)
    r.alive = False
    res = r.run_claude("S001", "KICKOFF", 30, None)
    first_check = T0 + renv.HEALTH_INTERVAL
    assert res.reason == "crash" and res.fault_at is not None and first_check <= res.fault_at < first_check + 2
    assert sent[0] == ("SIGINT", res.fault_at)


def test_a_fight_at_wrap_up_holds_the_hard_stop(loop: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(game, "game_state", lambda timeout=None: {"ok": True})
    monkeypatch.setattr(game, "mod_loaded", lambda timeout=None: True)
    r, _, sent = loop("deaf")
    held = T0 + 60 + 300  # the bg3 server held the session 5 min past its deadline (a fight at WRAP UP)
    hold = rpolicy.fight_hold_path(r.state.dir, "S001")
    hold.parent.mkdir(parents=True, exist_ok=True)
    hold.write_text(json.dumps({"deadline": held}))
    res = r.run_claude("S001", "KICKOFF", 1, None)
    assert res.reason == "hard_stop" and sent[0] == ("SIGINT", tick(held + renv.HARD_STOP_GRACE))


def test_an_agent_that_ends_on_its_own_exits(loop: Any):
    r, _, sent = loop("exits")
    res = r.run_claude("S001", "KICKOFF", 1, None, needs_game=False)
    assert (res.reason, res.exit_code, sent) == ("exited", 0, [])
