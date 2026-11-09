"""A run its agent left with time on the clock, as bg3harness.runner.run_host records it (`agent_exited`, not the
time cap: v011-pilot act1_gold_max and act3_gold_max read `timeout` at 39.1 and 42.5 of 45 min, cap_hit null)."""

from __future__ import annotations

from typing import Any, cast

from bg3harness.runner.run_host import undecided_why
from bg3harness.session import Recovery, SessionSpec, run_session
from test_run_host import JOB, MAX_SC, FakeRunner, Rig, called, run_dir
from test_run_host import rig as rig  # the fixture


def exit_after(minutes: float):
    def play(r: FakeRunner, tag: str) -> tuple[str, float]:
        called(r, "get_status", "move_to")
        return "exited", minutes * 60.0

    return play


def floored(rig: Rig, name: str):
    """The maximise run's session as bg3bench plays it: under 8 min left, a stop isn't resumed."""
    st = rig.h.start(JOB, MAX_SC, run_dir(name))
    recovery = Recovery(max_resumes=0, max_stop_resumes=None, stop_resume_min_minutes=Recovery.resume_min_minutes)
    return run_session(SessionSpec(name, st.prompt, 45.0, st.client_session, recovery), st.hooks)


def test_a_run_the_agent_left_with_time_on_the_clock_is_not_a_timeout(rig: Rig):
    rig.prepare = lambda r: setattr(r, "play", exit_after(39.0))
    out = floored(rig, "m-gold")
    assert out.result.reason == "exited"
    rec = rig.h.record(JOB, MAX_SC, run_dir("m-gold"), out)
    assert rec["outcome"] == "agent_exited" and rec.get("cap") is None
    assert [(f["kind"], f["class"]) for f in rec["failures"]] == [("agent_exited", "agent")]
    assert rec["failures"][0]["detail"].endswith("the agent ended its turn with 6.0 min of the run left")


def test_under_five_minutes_left_it_is_still_not_the_cap(rig: Rig):
    # act3_gold_max: 2.5 of 45 left, under EARLY_EXIT_MINUTES
    rig.prepare = lambda r: setattr(r, "play", exit_after(42.5))
    out = floored(rig, "m-gold")
    assert rig.h.record(JOB, MAX_SC, run_dir("m-gold"), out)["outcome"] == "agent_exited"


def test_a_maximise_resume_says_the_game_over_screen_ends_it_not_a_party_wipe():
    class R:
        until_hit = cap_hit = stop_requested = None

    why = undecided_why(cast(Any, R()), MAX_SC, 1e12, 1000.0)
    assert why is not None and "at the Game Over screen" in why and "party wipe" not in why
