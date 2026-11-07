# pyright: basic
"""A phase whose character the game runs itself (bg3harness.arena.match, "Per-turn cap": arena.status's
turn.lost_control, live.md "## Goaded read": the game's AI cast, spent and ended Goaded turns with no order, and a
player can't control such a turn either): no cap and no forfeit count on it, one arena_control_lost, and one that hangs
is the game's stall. On test_arena_match's ArenaModel and agents."""

from __future__ import annotations

from bg3harness.arena import match as am
from test_arena_match import SIDES, Game, Run, arena_events, grade, my_turn, setup

LOST = "Karlach"  # side A's first phase


def goaded(game: Game, lost: set[str]) -> None:
    """arena.status reads lost_control on the acting phase while its character is in `lost`, as the mod's Arena.status
    does from the game's LoseControlComponent."""
    rpc = game.rpc

    def read(op, args, side=None, context="server"):
        r = rpc(op, args, side, context)
        if op == "arena.status" and r.get("turn") and game.model.match().acting.name in lost:
            r = {**r, "turn": {**r["turn"], "lost_control": {"name": "Goaded"}}}
        return r

    game.rpc = read  # type: ignore[method-assign]


def play(game_ends_at: float | None, lost: set[str]):
    """Side A thinks on through the game's turn (20 s of model time a tick); the game ends it itself once the side's
    model time reaches game_ends_at (never when None), and the status goes with that turn. Every other turn: a little
    thought, then end_turn."""

    def fn(game: Game, side: str, run: Run) -> None:
        if not (char := my_turn(game, side)):
            return
        if game.model.match().acting.name in lost:
            run.model += 20
            if game_ends_at is not None and run.model >= game_ends_at:
                lost.clear()  # Goaded ends at that turn's end; the game's AI ends it, no call through the runner
                game.model.reply_side("server", "act.end_turn", {"char": char}, side)
            return
        run.model += 5
        game.rpc("act.end_turn", {"char": char}, side)

    return fn


def test_the_games_turn_has_no_cap_and_no_forced_end_and_is_written_once(tmp_path):
    lost = {LOST}
    model, game, clock, agents, spec = setup({"A": play(4 * 90.0, lost), "B": play(None, lost)})
    goaded(game, lost)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    evs = arena_events(tmp_path)
    assert [e["character"] for e in evs if e["ev"] == "arena_turn_forced"] == []
    lost = [e for e in evs if e["ev"] == "arena_control_lost"]
    assert [(e["side"], e["character"], e["status"]) for e in lost] == [("A", LOST, "Goaded")]
    assert lost[0]["uuid"] == model.match().member(LOST).uuid
    assert [e for e in evs if e["ev"] == "arena_stall"] == []
    g = grade(tmp_path)  # the facts schema takes the event; valid isn't touched by it
    assert g["valid"] is True
    assert [(x["side"], x["character"], x["status"]) for x in g["lost_control"]] == [("A", LOST, "Goaded")]


def test_a_games_turn_that_hangs_is_the_games_stall_whatever_its_side_thinks(tmp_path):
    lost = {LOST}
    _model, game, clock, agents, spec = setup({"A": play(None, lost), "B": play(None, lost)})
    goaded(game, lost)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault"
    evs = arena_events(tmp_path)
    assert [(e["stall"], e["character"]) for e in evs if e["ev"] == "arena_stall"] == [("game", LOST)]
    assert [e for e in evs if e["ev"] == "arena_turn_forced"] == []
    assert ("act.end_turn", "A") not in game.calls  # nothing a client sends: no cap's end either
