# pyright: basic
"""The runner and a match's reaction prompts (split from test_arena_match.py for the line cap), on its ArenaModel and
agents."""

from __future__ import annotations

import types
from collections.abc import Callable

import pytest
from bg3harness.arena import match as am
from test_arena_match import SIDES, ArenaModel, Game, Run, arena_events, grade, my_turn, setup


def patient(game: Game, side: str, run: Run) -> None:
    """Ends its turn; while a reaction prompt holds the game, its end_turn waits (no model time)."""
    if (char := my_turn(game, side)) and game.rpc("act.end_turn", {"char": char}, side)["status"] == "done":
        run.model += 5


def open_at_round_2(model: ArenaModel, who: str) -> Callable[[], None]:
    def hook() -> None:
        if model.arena is not None and model.mod_round == 2 and not model.reacted and model.reaction is None:
            model.open_reaction(who)

    return hook


def test_an_unowned_reaction_prompt_is_declined_once_and_recorded(tmp_path):
    """A prompt for someone on neither team holds the game: neither side may answer it, so the runner declines it
    (with no side) and records it. Turns that move on cost no ui.state read."""
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wolf"))
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wolf", False)]
    assert game.calls.count(("ui.react", None)) == 1
    assert game.calls.count(("ui.state", None)) == 1  # read only once the held turn was UNOWNED_CHECK_S old
    unowned = [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_unowned"]
    assert [(e["round"], e["detail"]) for e in unowned] == [(2, "Wolf | Shield | Choose how to react.")]
    assert out.end is not None and out.end["reason"] == "round_cap" and out.invalid is None
    assert grade(tmp_path)["valid"]


def test_a_reaction_prompt_a_team_owns_is_left_to_that_side(tmp_path):
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))

    def answer() -> None:  # side B answers its prompt once the runner has looked at it
        if model.reaction is not None and ("ui.state", None) in game.calls:
            game.rpc("ui.react", {"use": True}, "B", context="client")

    clock.tick_hooks.append(answer)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wyll", True)]
    assert ("ui.react", None) not in game.calls
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_unowned"]


def test_a_prompt_its_owner_leaves_open_is_declined_within_the_bound(tmp_path):
    """Live L5: an unanswered prompt holds all 8. The runner declines it for its side, and the match goes on."""
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))
    opened: list[float] = []
    declined: list[float] = []

    def timing() -> None:
        if model.reaction is not None and not opened:
            opened.append(clock.now())
        if model.reacted and not declined:
            declined.append(clock.now())

    clock.tick_hooks.append(timing)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wyll", False)]
    rows = [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_declined"]
    assert [(e["round"], e["side"]) for e in rows] == [(2, "B")]
    assert spec.turn_cap_s <= declined[0] - opened[0] <= am.UNOWNED_CHECK_S + spec.turn_cap_s + 5
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_unowned"]
    assert out.end is not None and out.end["reason"] == "round_cap" and out.invalid is None
    assert grade(tmp_path)["valid"]


def test_a_hold_with_no_prompt_on_screen_ends_the_match_as_a_fault(tmp_path):
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})

    def hold() -> None:
        if model.arena is not None and model.mod_round == 2:
            model.held_reaction = {"key": "g1|b:Interrupt_Counterspell", "side": "B", "interrupts": ["x"]}

    clock.tick_hooks.insert(0, hold)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is not None and "reaction held the game" in out.invalid
    assert ("ui.react", None) not in game.calls  # no popup: nothing to send


def test_an_answer_after_the_stuck_time_with_the_hold_lingering_is_no_fault(tmp_path):
    """REVIEW-reactions M1: an owner answers at 65 s and the hold outlives the popup by one read. Time with the prompt
    on screen isn't stuck time: it read fault at 66, an invalid end for the slow models the bound is there for."""
    read = {"reaction": {"prompt": "x", "owner": "B", "options": []}, "screen": "reaction"}
    key = {"held_reaction": {"key": "k", "side": "B", "interrupts": ["x"]}}
    replies = [{**read, **key}] * 2 + [key]

    class Reads:
        def rpc(self, op, args=None, context="server"):
            assert op == "ui.state"
            return replies.pop(0)

    spec = types.SimpleNamespace(name="a", turn_cap_s=90.0)
    hold = am._Hold()
    for now in (0.0, 64.0, 66.0):
        assert am._reaction_check(spec, Reads(), None, print, hold, now) == "B"  # type: ignore[arg-type]
    replies.append(key)
    with pytest.raises(am._Ended):  # still stuck 60 s after the prompt's last read
        am._reaction_check(spec, Reads(), None, print, hold, 124.0)  # type: ignore[arg-type]


def test_a_prompt_a_decline_doesnt_close_ends_the_match_as_a_fault(tmp_path):
    """S092: Do Not React left a prompt with a row per level up. Whatever does that can't hang a match."""
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})
    model.decline_sticks = True
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wyll", False)]  # declined once, not once a poll
    assert out.invalid is not None and "reaction held the game" in out.invalid


def test_a_held_phase_isnt_capped_and_its_side_waiting_isnt_its_time(tmp_path):
    """Load 2: a cap's end during a pause would leave the attack behind. The runner reads for a hold before it caps, so
    a cap reached before its first read of the hold sends no end, and what the acting side spends waiting on the other
    side's answer isn't its thinking: no cap when the hold ends either."""
    ends_held: list[dict] = []
    after = [0]

    def waiter(game: Game, side: str, run: Run) -> None:
        if model.reaction is None and model.reacted and after[0] < 5:
            after[0] += 1  # still deciding when the pause ends: the time it waited mustn't cap it now
        elif model.reaction is None:
            patient(game, side, run)
        elif my_turn(game, side):  # polls through the hold, as an agent may
            run.model += 100

    model, game, clock, agents, spec = setup({"A": waiter, "B": waiter})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))
    real = game.rpc

    def rpc(op, args, side=None, context="server"):
        if op == "act.end_turn" and model.reaction is not None and args.get("forced"):
            ends_held.append(args)
        return real(op, args, side, context=context)

    game.rpc = rpc
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wyll", False)]
    assert ends_held == []
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert out.end is not None and out.end["reason"] == "round_cap" and out.invalid is None
