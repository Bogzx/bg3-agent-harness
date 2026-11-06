# pyright: basic
"""The per-turn cap's clock across a cap's end (bg3harness.arena.match, "Per-turn cap"): a phase the game starts
while the capped phase's act.end_turn is still settling is timed from its start (arena fairness audit S3-7); on
test_arena_match's ArenaModel and agents."""

from __future__ import annotations

from typing import Any

import pytest
from bg3harness.arena import match as am
from test_arena_match import ALT4, ARENA4, SIDES, Game, Run, arena_events, my_turn, setup


@pytest.mark.parametrize(("capped", "next_"), [("Karlach", "Halsin"), ("Wyll", "Karlach")])  # A capped, B capped
def test_a_phase_that_begins_while_a_caps_end_settles_is_timed_from_its_start(tmp_path, capped, next_):
    """The game starts the next phase before the capped one's act.end_turn settles (live: TurnEnded up to 18 s
    late): the next side thinks from its start, so its cap counts from there, not from when the ends went through
    (S3-7), whichever side it is; arena_turn_forced still names the capped phase."""
    model, game, clock, agents, spec = setup({}, ARENA4, order=ALT4)
    started: dict[str, float] = {}  # name -> its side's model time when its first turn began

    def play(game: Game, side: str, run: Run) -> None:
        if not (char := my_turn(game, side)):
            return
        name = model.match().acting.name
        started.setdefault(name, run.model)
        if name in (capped, next_):
            run.model += 1  # thinks on, never ends it
        else:
            game.rpc("act.end_turn", {"char": char}, side)

    agents.plays.update(A=play, B=play)
    rpc = game.rpc
    ends: dict[str, float] = {}
    polls = {"left": 0}

    def slow(op: str, args: dict[str, Any], side: str | None = None) -> Any:
        if op == "act.end_turn" and args.get("forced"):
            name = model.match().member(args["char"]).name
            assert side is not None
            ends.setdefault(name, agents.runs[side].model)
            r = rpc(op, args, side)  # the game ends the turn: the next phase begins now
            if name == capped and r["status"] == "done":
                polls["left"] = 40  # 10 s of END_POLL_S before the job settles
                return {"job": r["job"], "status": "running"}
            return r
        if op == "job.poll" and polls["left"]:
            polls["left"] -= 1
            return {"id": args["id"], "status": "running" if polls["left"] else "done"}
        return rpc(op, args, side)

    game.rpc = slow  # type: ignore[method-assign]
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert spec.turn_cap_s <= ends[next_] - started[next_] < spec.turn_cap_s + 5  # base: + the 40 s while settling
    forced = [e["character"] for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced[:2] == [capped, next_]
