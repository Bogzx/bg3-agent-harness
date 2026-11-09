#!/usr/bin/env python3
# pyright: basic
"""A stalled stream is infrastructure, not play (owner, 2026-11-09; v011-pilot L12 m1: one stalled DeepSeek response
was charged as three forced turns and lost the match): turns forced through the silence or in progress at the client's
fault count toward no forfeit, a second fault is still invalid, and a side's model that writes and overruns still
forfeits."""

from __future__ import annotations

import pytest
from bg3harness.arena import match as am
from test_arena_match import SIDES, Game, Run, arena_events, grade, my_turn, passer, setup, staller


def silent(game: Game, side: str, run: Run) -> None:
    """Thinks on its turn with its stream silent: 20 s of model time a tick."""
    if my_turn(game, side):
        run.model += 20
        run.silent += 20


def forced_rows(tmp_path):
    return [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]


def test_a_side_silent_on_every_cap_loses_each_turn_but_is_not_forfeited(tmp_path):
    """It gets no forfeit, and the match goes to its normal end."""
    _model, game, clock, agents, spec = setup({"A": passer, "B": silent})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.invalid is None
    assert out.end["reason"] == "round_cap" and out.end.get("forfeit") is None
    rows = forced_rows(tmp_path)
    assert len(rows) > am.FORFEIT_TURNS and {(e["side"], e["excused"]) for e in rows} == {("B", "stream_silent")}
    g = grade(tmp_path)
    assert g["valid"] and g["forfeit"] is None and g["forced"]["B"] == len(rows)


def pilots_stall():
    """v011-pilot L12 m1's side B: silent through two caps and into a third phase, killed there, resumed; the resumed
    process thinks on with its stream live until that phase is capped, then plays."""
    state = {"killed": False, "left": False, "char": None, "phases": 0, "last": None}

    def play(game: Game, side: str, run: Run) -> None:
        char = my_turn(game, side)
        if not state["killed"]:
            if char:
                if char != state["last"]:
                    state.update(last=char, phases=state["phases"] + 1)
                run.model += 20
                run.silent += 20
                if state["phases"] == 3:  # two phases capped silent, killed in the third
                    state.update(killed=True, char=char)
                    run.end("agent_hang", -2)
            return
        if char == state["char"] and not state["left"]:
            run.model += 20
            return
        state["left"] = True
        passer(game, side, run)

    return play


def test_the_pilots_stall_is_no_forfeit_and_the_match_goes_on(tmp_path):
    _model, game, clock, agents, spec = setup({"A": passer, "B": pilots_stall()})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.invalid is None
    assert out.end["reason"] == "round_cap" and out.end.get("forfeit") is None
    assert [e["excused"] for e in forced_rows(tmp_path)] == ["stream_silent", "stream_silent", "client_fault"]
    assert out.exits["B"][0].reason == "agent_hang"
    assert agents.started[2][1].startswith("The fight is on. Your client stopped and was restarted; the game kept")
    g = grade(tmp_path)
    assert g["valid"] and g["forfeit"] is None


def test_the_turn_in_progress_at_a_fault_is_excused_and_a_full_cap_after_it_still_counts(tmp_path):
    hit = []

    def play(game: Game, side: str, run: Run) -> None:
        if not my_turn(game, side):
            return
        if not hit:  # part of its cap with output, then the client dies
            hit.append(1)
            run.model += 40
            run.end("api_error", 1)
            return
        run.model += 20  # the resumed process overruns, stream live

    _model, game, clock, agents, spec = setup({"A": passer, "B": play})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    rows = forced_rows(tmp_path)
    assert rows[0]["excused"] == "client_fault" and all("excused" not in e for e in rows[1:])
    # the first is excused and breaks the run, so three more are the forfeit
    assert out.end is not None and (out.end["reason"], out.end["forfeit"]) == ("forfeit", "B")
    assert len(rows) == 1 + am.FORFEIT_TURNS
    g = grade(tmp_path)
    assert g["valid"] and g["forfeit"] == {"side": "B", "cause": "forced_turns"}


def test_a_fault_on_the_other_sides_turn_excuses_nothing(tmp_path):
    """B's client dies while A is thinking: A's turn is A's, and A's three full caps still forfeit."""
    done = []

    def dies_once(game: Game, side: str, run: Run) -> None:
        if not done and not my_turn(game, side):
            done.append(1)
            run.end("api_error", 1)

    _model, game, clock, agents, spec = setup({"A": staller, "B": dies_once})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["forfeit"]) == ("forfeit", "A")
    assert all("excused" not in e for e in forced_rows(tmp_path))
    assert agents.started[2][0] == "B" and agents.started[2][1].endswith("Now round 1, the other side's turn.")


def test_a_plain_exit_excuses_nothing_and_a_full_third_cap_forfeits(tmp_path):
    """Two counted caps, the agent ends its process (exit 0) inside the third phase, the resumed one overruns it."""
    seen = {"last": None, "phases": 0, "left": False}

    def play(game: Game, side: str, run: Run) -> None:
        char = my_turn(game, side)
        if not char:
            return
        if char != seen["last"]:
            seen.update(last=char, phases=seen["phases"] + 1)
        if seen["phases"] == 3 and not seen["left"]:
            seen["left"] = True
            run.end()
            return
        run.model += 20

    _model, game, clock, agents, spec = setup({"A": passer, "B": play})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["forfeit"]) == ("forfeit", "B")
    assert all("excused" not in e for e in forced_rows(tmp_path))


def test_three_caps_with_the_stream_writing_still_forfeit(tmp_path):
    _model, game, clock, agents, spec = setup({"A": passer, "B": staller})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["forfeit"]) == ("forfeit", "B")
    assert all("excused" not in e for e in forced_rows(tmp_path))


def test_a_third_cap_the_stream_was_silent_through_is_no_forfeit(tmp_path):
    """Two full caps with the stream writing, then a whole cap of silence: the third is excused, the match goes on."""
    seen = {"last": None, "phases": 0}

    def play(game: Game, side: str, run: Run) -> None:
        char = my_turn(game, side)
        if not char:
            return
        if char != seen["last"]:
            seen.update(last=char, phases=seen["phases"] + 1)
        if seen["phases"] > 3:
            passer(game, side, run)
            return
        run.model += 20
        if seen["phases"] == 3:
            run.silent += 20

    _model, game, clock, agents, spec = setup({"A": passer, "B": play})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "round_cap"
    assert [e.get("excused") for e in forced_rows(tmp_path)] == [None, None, "stream_silent"]
    assert grade(tmp_path)["valid"]


@pytest.mark.parametrize(
    ("turn_side", "forced", "excused"), [("B", False, "client_fault"), ("A", False, None), ("B", True, None)]
)
def test_a_fault_excuses_only_its_sides_phase_still_in_progress(turn_side, forced, excused):
    turn = am.Turn(turn_side, "u", "X", 1, ["u"], forced=forced)
    am.excuse_fault(turn, "B")
    assert turn.excused == excused
    am.excuse_fault(None, "B")  # no phase yet: nothing to excuse


def test_a_cap_with_output_inside_it_is_not_excused(tmp_path):
    def nearly(game: Game, side: str, run: Run) -> None:
        if my_turn(game, side):
            run.model += 20
            run.silent = min(run.silent + 20, 59.0)  # a line came just inside the cap

    _model, game, clock, agents, spec = setup({"A": passer, "B": nearly})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "forfeit"


FELL = (
    "The fight is on. Your client stopped and was restarted; the game kept running, and an order sent before it "
    "stopped may have run."
)


@pytest.mark.parametrize(
    ("fault", "turn", "side", "text"),
    [
        (True, am.Turn("B", "u", "Blue Dune", 5, ["u"]), "B", f"{FELL} Now round 5, Blue Dune's turn (side B)."),
        (
            False,
            am.Turn("A", "u", "Red Dune", 5, ["u"]),
            "A",
            "The fight is on. Now round 5, Red Dune's turn (side A).",
        ),
        (False, am.Turn("A", "u", "Red Rogue", 3, ["u"]), "B", "The fight is on. Now round 3, the other side's turn."),
        (True, am.Turn("A", "u", "Red Rogue", 3, ["u"]), "B", f"{FELL} Now round 3, the other side's turn."),
        (True, None, "B", FELL),
        (False, am.Turn("A", "u", "Red Dune", 5, ["u"], capped=True), "A", "The fight is on."),
        (False, am.Turn("A", "u", "Red Dune", 5, ["u"], capped=True), "B", "The fight is on."),
    ],
)
def test_a_resumed_agent_is_told_the_facts(fault, turn, side, text):
    assert am.resume_prompt(fault, turn, side) == text
