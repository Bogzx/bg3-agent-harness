# pyright: basic
"""The runner and a match's reaction prompts (split from test_arena_match.py for the line cap), on its ArenaModel and
agents."""

from __future__ import annotations

import contextlib
import types
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
from bg3harness.arena import match as am
from test_arena_match import ARENA, HELD_REFUSAL, SIDES, ArenaModel, Game, Run, arena_events, grade, my_turn, setup


def patient(game: Game, side: str, run: Run) -> None:
    """Ends its turn; while a reaction prompt holds the game, its end_turn waits (no model time)."""
    if (char := my_turn(game, side)) and game.rpc("act.end_turn", {"char": char}, side)["status"] == "done":
        run.model += 5


def open_at_round_2(model: ArenaModel, who: str) -> Callable[[], None]:
    def hook() -> None:
        if model.arena is not None and model.mod_round == 2 and not model.reacted and model.reaction is None:
            model.open_reaction(who)

    return hook


def _hold_reads(reads: list[tuple[int, dict]]) -> list[str]:
    """The real _reaction_check on (seconds, ui.state) reads with a 90 s cap; the ops it sent."""
    calls: list[str] = []

    def rpc(op, args=None, context=None):
        calls.append(op)
        return {"status": "running"} if op == "ui.react" else game.ui

    game, hold = types.SimpleNamespace(ui={}, rpc=rpc), am._Hold()
    ref = types.SimpleNamespace(round=1, event=lambda *a, **k: None)
    spec = types.SimpleNamespace(name="t", turn_cap_s=90)
    for t, ui in reads:
        game.ui = ui
        am._reaction_check(spec, game, ref, lambda m: None, hold, 1000.0 + t)  # type: ignore[arg-type]
    return [c for c in calls if c == "ui.react"]


def test_the_bound_runs_from_the_prompt_first_on_screen_not_from_the_hold():
    # pQ T2: a popup 30 s late doesn't lose those 30 s, and left open it's still declined
    held = {"key": "cast1|red:Counterspell", "side": "B", "interrupts": ["Counterspell"]}
    late = [(t, {"screen": "game", "held_reaction": held}) for t in range(0, 30, 2)]
    up = {
        "screen": "reaction",
        "held_reaction": held,
        "reaction": {"prompt": "Counterspell?", "options": [{"i": 1, "who": "Red Cedar"}], "owner": "B"},
    }
    assert _hold_reads(late + [(t, up) for t in range(30, 118, 2)]) == []  # 116 s from the hold, 86 s on screen
    assert _hold_reads(late + [(t, up) for t in range(30, 124, 2)]) == ["ui.react"]  # 90 s on screen at 120 s


L5 = {**ARENA, "turn_cap_s": 90, "wall_cap_min": 60, "round_cap": 20}


def _two_forced_then(trigger: Callable[[ArenaModel], None], tmp_path, decline_fails: bool = False):
    """A takes two forced turns, slow but within the rules, then `trigger` fires on its third turn; A then idles."""
    forced = [0]

    def a_play(game: Game, side: str, run: Run) -> None:
        if forced[0] < 2 and my_turn(game, side) and model.reaction is None:
            run.model += 100
        elif forced[0] >= 2:
            patient(game, side, run)

    model, game, clock, agents, spec = setup({"A": a_play, "B": patient}, L5)
    real = game.rpc

    def rpc(op, args, side=None, context="server"):
        if decline_fails and op == "ui.react" and side is None:
            raise RuntimeError("no command")
        return real(op, args, side, context=context)

    game.rpc = rpc  # type: ignore[method-assign]
    fired = [False]

    def hook() -> None:
        with contextlib.suppress(OSError):
            forced[0] = sum(e["ev"] == "arena_turn_forced" for e in arena_events(tmp_path))
        if forced[0] >= 2 and not fired[0] and my_turn(game, "A"):
            fired[0] = True
            trigger(model)

    clock.tick_hooks.insert(0, hook)
    return am.run_arena(spec, SIDES, game, agents, tmp_path, clock), game


def _stalls(out_dir) -> list:
    return [e["stall"] for e in arena_events(out_dir) if e["ev"] == "arena_stall"]


def test_a_stall_in_a_stale_pause_is_the_games_fault_not_the_acting_sides_forfeit(tmp_path):
    """pQ H1, S5b: the mod refuses every end with no hold to read. A did nothing wrong and can't end: it read a valid
    forfeit to B at its third forced turn. A fault reruns."""

    def stale(model: ArenaModel) -> None:
        model.refuse_end = HELD_REFUSAL

    out, _game = _two_forced_then(stale, tmp_path)
    assert _stalls(tmp_path) == ["game"]
    assert out.end is not None and out.end["reason"] == "fault" and out.end.get("winner") is None
    assert out.invalid is not None


def test_a_stall_with_a_hold_on_record_is_the_games_fault_not_the_acting_sides_forfeit(tmp_path):
    """pQ H1, S3b: the decline errors, the hold stays on record, and the stall path has no excuse for it."""

    def held(model: ArenaModel) -> None:
        model.open_reaction("Wyll")

    # the hold's own clock is pushed past the stall's, so the stall path is what decides here; the mod's gate says no
    # pause, as for a held cast by someone on neither team: the hold on record is enough
    with mock.patch.object(am, "REACTION_STUCK_S", 10_000.0), mock.patch.object(am, "_paused", lambda g, s: False):
        out, _game = _two_forced_then(held, tmp_path, decline_fails=True)
    assert _stalls(tmp_path) == ["game"]
    assert out.end is not None and out.end["reason"] == "fault"


def test_only_the_gates_pause_words_read_as_a_pause():
    def paused(reply: Exception | None) -> bool:
        def rpc(op, args, side):
            assert (op, side) == ("guard.side_gate", "A")
            if reply is not None:
                raise reply
            return {"ok": True}

        return am._paused(types.SimpleNamespace(rpc=rpc), "A")  # type: ignore[arg-type]

    assert paused(RuntimeError(HELD_REFUSAL)) is True
    assert paused(RuntimeError("not your turn (side B is acting)")) is False
    assert paused(TimeoutError("guard.side_gate: no answer")) is False
    assert paused(None) is False


@pytest.mark.parametrize("how", ["raises", "job_failed"])
def test_a_decline_that_keeps_failing_is_a_fault_not_an_hour_of_tokens(tmp_path, how):
    """pQ H2, S3a: with the prompt up, the stuck clock never ran and a thinking side kept the idle clock at 0: a valid
    wall_cap draw after 60 min. The bound's retries go on until REACTION_STUCK_S past the first failure."""
    tries: list[float] = []
    opened: list[float] = []

    def a_play(game: Game, side: str, run: Run) -> None:
        if model.reaction is not None:
            run.model += am.POLL_S  # thinking non-stop
        else:
            patient(game, side, run)

    model, game, clock, agents, spec = setup({"A": a_play, "B": patient}, L5)
    real = game.rpc

    def rpc(op, args, side=None, context="server"):
        if op == "ui.react" and side is None:
            tries.append(clock.now())
            if how == "raises":
                raise RuntimeError("no command")
            return {"status": "failed", "error": "no command"}
        return real(op, args, side, context=context)

    game.rpc = rpc  # type: ignore[method-assign]

    def hook() -> None:
        if model.mod_round == 2 and not opened and my_turn(game, "A"):
            model.open_reaction("Wyll")
            opened.append(clock.now())

    clock.tick_hooks.insert(0, hook)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault" and out.invalid is not None
    assert "decline keeps failing" in out.invalid
    assert len(tries) > 2  # it kept trying
    assert tries[-1] - tries[0] < am.REACTION_STUCK_S + 2 * am.REACTION_POLL_S
    assert clock.now() - opened[0] < 200  # 90 s + the first read's lateness + 60 s, not 60 min


def test_a_decline_failing_once_starts_no_clock_for_the_next_hold():
    """The failure's clock goes with its key: a new prompt gets its own bound and its own tries."""
    hold = am._Hold(key="k", since=0.0, opened=0.0, shown=0.0, decline_failed=1.0)

    class Reads:
        def rpc(self, op, args=None, context="server"):
            return {"reaction": {"prompt": "other", "owner": "B", "options": []}, "screen": "reaction"}

    spec = types.SimpleNamespace(name="a", turn_cap_s=90.0)
    assert am._reaction_check(spec, Reads(), None, print, hold, 500.0) == "B"  # type: ignore[arg-type]
    assert hold.key == "other" and hold.decline_failed is None


def test_the_pause_words_are_the_mods():
    """The stall reads Guard.side_gate's refusal by its words."""
    guard = Path(am.__file__).parents[5] / "mod/Mods/BG3AI/ScriptExtender/Lua/Server/Guard.lua"
    assert am.PAUSED_REFUSAL in guard.read_text()
    assert HELD_REFUSAL.startswith(am.PAUSED_REFUSAL)


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
