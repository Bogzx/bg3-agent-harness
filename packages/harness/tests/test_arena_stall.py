# pyright: basic
"""run_arena's stall rule offline (bg3harness.arena.match, "Stall"): a phase idle for its stall time is capped when
its side still holds a member, a fault when the game holds the turn; on test_arena_match's ArenaModel and agents."""

from __future__ import annotations

import json
import types
from pathlib import Path

import jsonschema
import pytest
from bg3harness.arena import match as am
from test_arena_match import (
    BENCH_FACTS_SCHEMA,
    FACTS_SCHEMA,
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


def off_turn_thinker(game: Game, side: str, run: Run) -> None:
    """Thinks all the time, 1 s of model time a second, its own phase or not (plans during the other side's), and
    ends each of its members' turns once."""
    run.model += 1.0
    if (char := my_turn(game, side)) and char not in run.ended:
        run.ended.add(char)
        game.rpc("act.end_turn", {"char": char}, side)


def test_the_other_side_thinking_doesnt_excuse_a_phase_its_side_holds(tmp_path):
    """Review R1 (review-arena-v0.md): A holds its phase with no model time while B thinks on through it. B's model
    time is no progress of A's phase: A is stalled (held) and capped, three in a row forfeit, not a 30 min wall-cap
    draw."""
    _model, game, clock, agents, spec = setup({"A": looper, "B": off_turn_thinker})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None and out.end is not None
    assert (out.end["winner"], out.end["reason"], out.end.get("forfeit")) == ("B", "forfeit", "A"), out.end
    assert [(e["stall"], e["side"]) for e in stalls(tmp_path)] == [("held", "A")] * 3


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
        karlach = model.match().member("Karlach")
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
        u = model.match().member("Karlach").uuid
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


def test_a_readmitted_member_reaches_the_facts_named_and_is_no_longer_passed(tmp_path):
    """arena_turn_readmitted (a member the mod passed stood up with its turn back): written named as in arena_turn,
    and the phase's passed set loses it, so the runner counts it as one who can act again."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    sent: list[bool] = []

    def pass_then_back() -> None:
        if model.arena is not None and not sent:
            sent.append(True)
            u = model.match().member("Karlach").uuid
            model.ref_record(
                "arena_turn_passed",
                side="A",
                character=u,
                name="Karlach (0)",
                why="downed",
                round=1,
                death_save="rolled",
            )
            model.ref_record(
                "arena_turn_readmitted",
                side="A",
                character=u,
                name="Karlach (0)",
                round=1,
                why="stood up after its turn was passed",
            )

    clock.tick_hooks.append(pass_then_back)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    [p] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_passed"]
    [r] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_readmitted"]
    assert (p["character"], p["death_save"]) == ("Karlach", "rolled")
    assert (r["character"], r["side"], r["round"]) == ("Karlach", "A", 1)
    for e in (p, r):
        jsonschema.validate(e, FACTS_SCHEMA)
        jsonschema.validate(e, BENCH_FACTS_SCHEMA)


def test_the_mods_round_first_side_reaches_the_facts_as_given(tmp_path):
    """arena_round (a round's first phase: its side, turn or passed; the seat audit) is written to the facts as the mod
    gave it and fits both facts schemas."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    sent: list[bool] = []

    def first_side() -> None:
        if model.arena is not None and not sent:
            sent.append(True)
            model.ref_record("arena_round", round=2, first="A", how="passed")

    clock.tick_hooks.append(first_side)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    [r] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_round"]
    assert (r["round"], r["first"], r["how"]) == (2, "A", "passed")
    jsonschema.validate(r, FACTS_SCHEMA)
    jsonschema.validate(r, BENCH_FACTS_SCHEMA)


def test_the_mods_stand_up_reaches_the_facts_named(tmp_path):
    """arena_stood_up (a prone member stood up at its phase start by the mod, REVIEW-12 (a)) is written with
    `character` named as in arena_turn and fits both facts schemas."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    sent: list[bool] = []

    def stood() -> None:
        if model.arena is not None and not sent:
            sent.append(True)
            u = model.match().member("Karlach").uuid
            model.ref_record(
                "arena_stood_up",
                side="A",
                character=u,
                name="Karlach (0)",
                round=2,
                status="Prone",
                movement_before=9,
                movement_after=4.5,
                movement_max=9,
                deducted=4.5,
                game_took=False,
            )

    clock.tick_hooks.append(stood)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    [r] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_stood_up"]
    assert (r["character"], r["round"], r["deducted"], r["game_took"]) == ("Karlach", 2, 4.5, False)
    jsonschema.validate(r, FACTS_SCHEMA)
    jsonschema.validate(r, BENCH_FACTS_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({k: v for k, v in r.items() if k != "deducted"}, FACTS_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({k: v for k, v in r.items() if k != "deducted"}, BENCH_FACTS_SCHEMA)


def test_translate_readmitted_takes_the_member_out_of_the_phases_passed_set():
    ref = am.Referee.__new__(am.Referee)
    ref.names, ref.done = {"uk": ("A", "Karlach")}, False
    ref.turns = [am.Turn("A", "uw", "Wyll", 1, ["uw", "uk"])]
    out = ref.translate({"ev": "arena_turn_passed", "side": "A", "character": "UK", "why": "downed", "round": 1})
    turn = ref.turn
    assert out is not None and out["character"] == "Karlach" and turn is not None and turn.passed == {"uk"}
    out = ref.translate({"ev": "arena_turn_readmitted", "side": "A", "character": "UK", "round": 1})
    turn = ref.turn
    assert out is not None and out["character"] == "Karlach" and turn is not None and turn.passed == set()


def test_the_mods_group_end_write_reaches_the_facts_with_its_members_named(tmp_path):
    """arena_group_end_written (the mod wrote RequestedEndTurn on a shared group the game kept open after every end) is
    written to the facts like arena_turn: character and members by the arena's names, names and errors as given."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    sent: list[bool] = []

    def write_once() -> None:
        if model.arena is not None and not sent:
            sent.append(True)
            k, w = model.match().member("Karlach").uuid, model.match().member("Wyll").uuid
            names = ["Karlach (0)", "Wyll"]
            model.ref_record(
                "arena_group_end_written", round=1, group="team-1", character=w, members=[k, w], names=names,
                errors={w: "Replicate failed"}, how="requested_end_write",
            )  # fmt: skip

    clock.tick_hooks.append(write_once)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    [rec] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_group_end_written"]
    assert (rec["character"], rec["members"], rec["names"]) == ("Wyll", ["Karlach", "Wyll"], ["Karlach (0)", "Wyll"])
    assert (
        rec["round"] == 1
        and rec["how"] == "requested_end_write"
        and list(rec["errors"].values()) == ["Replicate failed"]
    )
    jsonschema.validate(rec, FACTS_SCHEMA)
    jsonschema.validate(rec, BENCH_FACTS_SCHEMA)


def slow_ends(game: Game, clock, settle_s: float, outcome: str) -> list[tuple[str, float]]:
    """Side A's act.end_turn jobs run `settle_s` s, then end: outcome "done" (the game takes the end then) or the
    mod's "the game ignored the end-turn order". Returns the log of (what, when) for the runner's ends."""
    rpc, jobs, log = game.rpc, {}, []

    def slow(op, args, side=None, context="server"):
        if op == "act.end_turn" and side == "A":
            jid = 9000 + len(jobs)
            jobs[jid] = (clock.now(), args)
            log.append(("sent", clock.now()))
            return {"job": jid, "status": "running"}
        if op == "job.poll" and side == "A" and args.get("id") in jobs:
            t0, a = jobs[args["id"]]
            if clock.now() - t0 < settle_s:
                return {"id": args["id"], "status": "running"}
            del jobs[args["id"]]
            log.append((outcome, clock.now()))
            if outcome == "done":
                return rpc("act.end_turn", a, side)
            return {"id": args["id"], "status": "failed", "error": "the game ignored the end-turn order"}
        return rpc(op, args, side)

    game.rpc = slow  # type: ignore[method-assign]
    return log


def test_a_cap_end_still_running_at_10_s_that_the_game_then_ignores_is_never_forced(tmp_path):
    """Review C1 (arena-fight-analysis.md): the mod's end of a group's last member runs up to 21 s. A cap's end
    still running at 15 s, then ignored, is no end: no arena_turn_forced, the ends sent again once, then the stall
    finds the side still holding the phase (held, not the game's fault at once)."""
    _model, game, clock, agents, spec = setup({"A": staller, "B": passer})
    log = slow_ends(game, clock, 15.0, "ignored")
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert [w for w, _t in log][:4] == ["sent", "ignored", "sent", "ignored"]  # the cap's two tries
    assert [e["stall"] for e in stalls(tmp_path)][:1] == ["held"]
    assert out.end is not None


def test_a_cap_end_done_at_15_s_is_forced_once_it_is_done(tmp_path):
    """A cap's end that the game takes at 15 s: arena_turn_forced is written after it is done, not at 10 s."""
    _model, game, clock, agents, spec = setup({"A": staller, "B": passer})
    log = slow_ends(game, clock, 15.0, "done")
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    forced = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced and log[:2] and log[1][0] == "done"
    assert forced[0]["t"] >= log[1][1] >= log[0][1] + 15


def test_the_caps_end_turn_says_which_cap_ended_the_turn(tmp_path):
    """Review C4: the runner's cap ends carry `forced` (the mod's turn_forced event for the side and its
    out-of-phase refusals name it): the model-time cap with its seconds, the stall cap with its idle time."""
    sent: list[str] = []

    def watch(game: Game) -> None:
        rpc = game.rpc

        def w(op, args, side=None, context="server"):
            if op == "act.end_turn" and "forced" in args:
                sent.append(args["forced"])
            return rpc(op, args, side)

        game.rpc = w  # type: ignore[method-assign]

    _m, game, clock, agents, spec = setup({"A": staller, "B": passer})
    watch(game)
    am.run_arena(spec, SIDES, game, agents, tmp_path / "cap", clock)
    assert sent[0] == f"the per-turn time cap ({spec.turn_cap_s:g} s of model time)"
    sent.clear()
    _m, game, clock, agents, spec = setup({"A": looper, "B": passer})
    watch(game)
    am.run_arena(spec, SIDES, game, agents, tmp_path / "stall", clock)
    assert sent[0].startswith("the stall cap (no progress for 30") and sent[0].endswith(" s)"), sent


def test_the_other_sides_reaction_prompt_is_not_the_acting_sides_stall(tmp_path):
    """Fairness audit S2-1: in B's phase the game opens A's reaction prompt (Karlach's Shield) and waits for A, who
    thinks 1 s a second and never answers. That waiting is the prompt's: A's model time is no idle of B's phase, so B
    is never stalled or capped (it was: held at 300 s, a forced turn toward B's forfeit), and the prompt is declined
    at the turn cap."""
    waited: set[tuple[int, str]] = set()

    def b_waits_a_tick(game: Game, side: str, run: Run) -> None:
        """Ends each turn on its second tick in it (no model time); while the prompt holds the game the end fails."""
        if (char := my_turn(game, side)) is None:
            return
        if (game.model.mod_round, char) not in waited:
            waited.add((game.model.mod_round, char))
            return
        game.rpc("act.end_turn", {"char": char}, side)

    model, game, clock, agents, spec = setup({"A": passer, "B": b_waits_a_tick})
    opened: list[float] = []

    def open_in_bs_phase() -> None:
        a = model.arena
        if a is None or opened or model.mod_round != 2 or a.side_of(a.acting.uuid) != "B":
            return
        model.open_reaction("Karlach")
        opened.append(clock.now())

    def a_answers_late() -> None:
        if model.reaction is None or not opened:
            return
        agents.runs["A"].model += 1.0

    clock.tick_hooks.insert(0, open_in_bs_phase)
    clock.tick_hooks.insert(1, a_answers_late)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert opened and model.reacted == [("Karlach", False)]
    assert stalls(tmp_path) == []
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert out.end is not None and out.end["reason"] == "round_cap" and out.invalid is None


def test_both_schemas_take_the_turn_order_fields_and_refuse_other_values():
    """arena_start's turn_order; arena_turn's and arena_turn_passed's order, opening and rule (a passed slot counts in
    the alternation, so it says where it stood)."""
    base = {"v": 1, "kind": "event", "t": 1.0}
    start = {**base, "ev": "arena_start", "arena": "a", "seed": 1, "teams": {"A": [], "B": []}}
    start["sides"] = {"A": "pi/x", "B": "pi/y"}
    turn = {**base, "ev": "arena_turn", "side": "B", "character": "Wyll", "members": ["Wyll"], "names": ["Wyll"]}
    turn.update(round=1, phase=2, shared=True)
    passed = {**base, "ev": "arena_turn_passed", "side": "A", "character": "Gale", "why": "downed", "round": 1}
    why = {"order": "snake", "opening": "A", "rule": "alternate"}
    goods = [{**start, "turn_order": "snake"}, {**turn, **why}, {**passed, **why}]
    goods += [{**turn, **why, "rule": "only_side_left"}, {**passed, **why, "rule": "opening"}]
    bads = [{**start, "turn_order": "initiative"}]
    for rec in (turn, passed):
        bads += [{**rec, **why, "order": "team"}, {**rec, **why, "opening": "C"}, {**rec, **why, "rule": "first"}]
    for schema in (FACTS_SCHEMA, BENCH_FACTS_SCHEMA):
        for good in goods:
            jsonschema.validate(good, schema)
        for bad in bads:
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)


@pytest.mark.parametrize("status", ["interrupted", "timeout"])
def test_a_cap_end_that_reached_a_final_status_returns_at_once(status):
    """pK's S5 s5-a30wc: a superseded end stays `interrupted` for good, and the runner polled it for END_SETTLE_S with
    no cap, hold or stall check. Every status but running is the job's last."""
    polls, slept = [], []

    def rpc(op, args, side=None):
        if op == "job.poll":
            polls.append(args["id"])
            return {"status": status, "error": "superseded by a new action (move_to)"}
        return {"job": 7, "status": "running"}

    clock = types.SimpleNamespace(now=lambda: sum(slept), sleep=slept.append)
    err = am._end_turn(types.SimpleNamespace(rpc=rpc), clock, "u", "B", "the cap")  # type: ignore[arg-type]
    assert err == f"act.end_turn {status}: superseded by a new action (move_to)"
    assert polls == [7] and sum(slept) == am.END_POLL_S
