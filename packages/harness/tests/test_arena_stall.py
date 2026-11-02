# pyright: basic
"""run_arena's stall rule offline (bg3harness.arena.match, "Stall"): a phase idle for its stall time is capped when
its side still holds a member, a fault when the game holds the turn; on test_arena_match's ArenaModel and agents."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from bg3harness.arena import match as am
from test_arena_match import (
    BENCH_FACTS_SCHEMA,
    SIDES,
    Game,
    Run,
    arena_events,
    grade,
    my_turn,
    passer,
    setup,
    staller,
)


def ender_once(game: Game, side: str, run: Run) -> None:
    """Ends each of its members' turns once (a little model time), then only reads while the turn stays."""
    if (char := my_turn(game, side)) and char not in run.ended:
        run.ended.add(char)
        run.model += 0.5
        game.rpc("act.end_turn", {"char": char}, side)


def looper(game: Game, side: str, run: Run) -> None:
    """Reads its own turn every tick and never ends it, thinking for no time (live 7: wait_for/get_combat_state)."""
    my_turn(game, side)


def thinker(game: Game, side: str, run: Run) -> None:
    """Thinks slowly on its turn, 0.18 s of model time a second (the rest is tool time), and never ends it: its
    60 s cap comes at 334 s, the stall's 300 s idle (wall less model time) at 366 s."""
    if my_turn(game, side):
        run.model += 0.18


def stalls(out: Path) -> list[dict]:
    return [e for e in arena_events(out) if e["ev"] == "arena_stall"]


def test_a_phase_the_game_holds_after_its_sides_end_is_a_fault_with_the_stuck_member(tmp_path):
    """The side ended its member, the game never moved the turn on: after max(3 x 60 x 1, 300) s idle, a fault
    (invalid, rerun) with an arena_stall {stall: game} naming the phase, the last read and each side's last calls; the
    runner sends no act.end_turn of its own."""
    model, game, clock, agents, spec = setup({"A": ender_once, "B": ender_once})
    model.hold = True
    tmp_path.mkdir(exist_ok=True)
    calls = [
        {"kind": "tool_call", "tool": "end_turn", "call_id": "c1"},
        {"kind": "tool_result", "text": "Karlach's turn ended", "call_id": "c1"},
        {"kind": "tool_call", "tool": "wait_for", "call_id": "c2"},
    ]
    (tmp_path / "A.events.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls) + "not json\n")
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault" and model.ends == [{"reason": "fault"}]
    assert out.invalid is not None
    assert out.invalid.startswith("fault: stall: Karlach's phase (side A, round 1) idle 30"), out.invalid
    assert out.invalid.endswith("s, the game holds the turn")
    [st] = stalls(tmp_path)
    assert (st["stall"], st["side"], st["character"], st["members"], st["round"]) == (
        "game",
        "A",
        "Karlach",
        ["Karlach"],
        1,
    )
    assert 300 <= st["idle_s"] < 302 and st["status"]["turn"]["side"] == "A" and st["status"]["hp"] == {"A": 1, "B": 1}
    assert st["last"] == {
        "A": [{"tool": "end_turn", "result": "Karlach's turn ended"}, {"tool": "wait_for", "result": None}],
        "B": [],
    }
    assert game.calls.count(("act.end_turn", "A")) == 1  # the side's own: the runner sent none
    assert grade(tmp_path)["invalid_reason"] == "fault"


def test_a_side_holding_its_own_phase_without_thinking_is_capped_at_the_stall_and_forfeits(tmp_path):
    """A client looping reads in its own phase (no model time, never an end) can't turn its stall into a rerun: at
    the stall, arena_stall stall held, the runner ends the phase as at the cap, and three in a row forfeit."""
    _model, game, clock, agents, spec = setup({"A": looper, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None and out.end is not None
    assert (out.end["winner"], out.end["reason"], out.end["forfeit"], out.end["cause"]) == (
        "B",
        "forfeit",
        "A",
        "forced_turns",
    )
    assert [(e["stall"], e["character"]) for e in stalls(tmp_path)] == [
        ("held", "Karlach"),
        ("held", "Gale"),
        ("held", "Karlach"),
    ]
    forced = [e["character"] for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced == ["Karlach", "Gale", "Karlach"]
    assert game.calls.count(("act.end_turn", "A")) == 2  # the runner's ends; the third is the forfeit
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forfeit"]) == (True, "B", {"side": "A", "cause": "forced_turns"})


def test_a_slow_thinker_is_capped_by_its_model_time_never_stalled(tmp_path):
    _model, game, clock, agents, spec = setup({"A": thinker, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["forfeit"]) == ("forfeit", "A")
    assert stalls(tmp_path) == []


def test_once_capped_its_sides_model_time_no_longer_excuses_a_phase_the_game_holds(tmp_path):
    """The model-time cap ended Karlach's phase, the game took the end and held the turn, and A's client thinks on:
    the stall clock runs on the other side's idle, and the match ends as the game's fault."""
    model, game, clock, agents, spec = setup({"A": staller, "B": passer})
    model.hold = True
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault"
    assert [e["character"] for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"] == ["Karlach"]
    assert [(e["stall"], e["character"]) for e in stalls(tmp_path)] == [("game", "Karlach")]


def test_a_second_stall_of_a_phase_whose_ends_the_game_refuses_is_the_games(tmp_path):
    model, game, clock, agents, spec = setup({"A": looper, "B": passer})
    model.refuse_end = "the game ignored the end-turn order"
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault"
    assert [(e["stall"], e["character"]) for e in stalls(tmp_path)] == [("held", "Karlach"), ("game", "Karlach")]
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]


def test_a_stall_whose_combat_read_fails_is_the_games(tmp_path):
    model, game, clock, agents, spec = setup({"A": looper, "B": passer})
    model.combat_error = "busy"
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault"
    [st] = stalls(tmp_path)
    assert st["stall"] == "game" and st["detail"] == "state.combat failed: busy"


def test_a_phase_whose_game_still_rolls_is_not_stalled(tmp_path):
    """A new roll ring row every 200 s is progress: a phase that holds no model time but whose game moves runs on
    to the wall cap with no stall."""
    model, game, clock, agents, spec = setup({"A": looper, "B": passer})

    def roll_every_200_s() -> None:
        if int(clock.t) % 200 == 0:
            model.roll(True)

    clock.tick_hooks.append(roll_every_200_s)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "wall_cap"
    assert stalls(tmp_path) == []


def test_a_match_whose_first_phase_never_comes_is_the_games_fault(tmp_path):
    """No arena_turn ever comes and nothing moves: the same idle clock, max(3 x 60, 300) s, ends the match as the
    game's fault (invalid, rerun), not a wall-cap draw; its arena_stall names no phase."""
    model, game, clock, agents, spec = setup({"A": looper, "B": looper})
    model.phases = False
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault" and model.ends == [{"reason": "fault"}]
    assert out.invalid is not None
    assert out.invalid.startswith("fault: stall: the match before its first phase idle 30"), out.invalid
    [st] = stalls(tmp_path)
    assert (st["stall"], st["round"], "side" in st, "members" in st) == ("game", 0, False, False)
    assert 300 <= st["idle_s"] < 302
    jsonschema.validate(st, BENCH_FACTS_SCHEMA)
    assert grade(tmp_path)["invalid_reason"] == "fault"
    assert not [c for c in game.calls if c[0] == "act.end_turn"]


def at_zero_hp(model, then):
    """Karlach drops to 0 HP in her own phase without the mod passing her (live 10 m1 a2: Lae'zel at 0 HP without
    DOWNED, so the mod's cantAct let her act); her state.combat row reads downed (StateWorld's party hp fallback).
    `then` plays the side on."""

    def play(game: Game, side: str, run: Run) -> None:
        karlach = model._match().member("Karlach")
        if my_turn(game, side) == karlach.uuid:
            karlach.hp = 0
        then(game, side, run)

    return play


def test_a_member_state_combat_reads_downed_but_the_mod_never_passed_is_held_and_capped(tmp_path):
    """Who can't act is the mod's call (its arena_turn_passed), not state.combat's downed: a 0-HP member the mod
    never passed, still current and not ended, is held by its side, so its stall is capped, not a fault."""
    model, game, clock, agents, spec = setup({"A": looper, "B": passer})
    agents.plays["A"] = at_zero_hp(model, looper)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None, out.invalid
    assert out.end is not None and out.end["hp_left_A"] < 1  # Karlach did drop to 0
    first = stalls(tmp_path)[0]
    assert (first["stall"], first["character"]) == ("held", "Karlach")
    forced = [e["character"] for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced[0] == "Karlach"


def test_a_member_the_mod_passed_that_the_game_keeps_current_is_the_games_fault(tmp_path):
    """The mod passed Karlach (arena_turn_passed why zero_hp) but the game keeps her turn current: nobody the side
    could end, so the stall is the game's (fault), with no act.end_turn from the runner."""
    model, game, clock, agents, spec = setup({"A": looper, "B": passer})

    def passed(game: Game, side: str, run: Run) -> None:
        u = model._match().member("Karlach").uuid
        if my_turn(game, side) == u and not run.ended:
            run.ended.add(u)
            model.ref_record("arena_turn_passed", side="A", character=u, name="Karlach", why="zero_hp", round=1)

    agents.plays["A"] = at_zero_hp(model, passed)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault", out.end
    [st] = stalls(tmp_path)
    assert (st["stall"], st["character"]) == ("game", "Karlach")
    assert ("act.end_turn", "A") not in game.calls
    rec = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_passed"]
    assert [(e["character"], e["why"]) for e in rec] == [("Karlach", "zero_hp")]
    for e in arena_events(tmp_path):
        jsonschema.validate(e, BENCH_FACTS_SCHEMA)
