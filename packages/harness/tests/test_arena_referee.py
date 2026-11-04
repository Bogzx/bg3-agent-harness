# pyright: basic
"""The match referee's translation of the mod's ring records (split from test_arena_match.py for the 1000-line cap),
on test_arena_match's ArenaModel and agents."""

from __future__ import annotations

import json

from bg3harness import facts as hfacts
from bg3harness.arena import match as am
from test_arena_match import SIDES, arena_events, chip_once, passer, setup


def test_ring_records_of_the_mod_are_translated_or_left_out(tmp_path):
    """The mod's own arena_start and a runner end's arena_end never reach facts; a phase names its characters; a
    phase's members are named (a summon by the record's names) and a phase naming the other team's character or
    without members and names is not written; the mod's own end of any reason is the match's, and nothing of the arena's
    after an end is written."""
    _model, _game, clock, _agents, spec = setup({"A": passer, "B": passer})
    ref = am.Referee(spec, SIDES, hfacts.FactsWriter(tmp_path / "f.jsonl", clock=clock.now))
    ref.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]})
    assert ref.translate({"ev": "arena_start", "teams": {}}) is None
    phase = {"ev": "arena_turn", "side": "A", "round": 1, "phase": 2, "group": "g", "shared": True}
    two = {**phase, "character": "ua2", "members": ["ua2", "UA1"], "names": ["Gale", "Karlach"]}
    assert ref.translate(two) == {**two, "character": "Gale", "members": ["Gale", "Karlach"]}
    assert ref.turn is not None and ref.turn.members == ["ua2", "UA1"]
    # a team member's name is the arena file's (the game may name it otherwise); a summon's is the record's
    with_summon = {**phase, "character": "summon-1", "members": ["summon-1", "ua1"], "names": ["Wolf", "Karla"]}
    assert ref.translate(with_summon) == {**with_summon, "character": "Wolf", "members": ["Wolf", "Karlach"]}
    assert ref.turn.members == ["summon-1", "ua1"] and (ref.turn.uuid, ref.turn.name) == ("summon-1", "Wolf")
    alone = {**phase, "character": "summon-1", "members": ["summon-1"], "names": ["Wolf"]}
    assert ref.translate(alone) == {**alone, "members": ["Wolf"], "character": "Wolf"}
    assert ref.translate({**phase, "character": "ua1", "members": ["ua1", "ub1"], "names": ["Karlach", "Wyll"]}) is None
    assert ref.translate({**phase, "character": "ua1", "members": ["ua1"]}) is None  # no names
    assert ref.translate({"ev": "arena_turn", "side": "A", "character": "ua1", "round": 1}) is None
    assert len(ref.turns) == 3
    assert ref.translate({"ev": "arena_combat_ended", "rounds": 1}) is None and ref.combat_ended == {
        "ev": "arena_combat_ended",
        "rounds": 1,
    }
    assert ref.translate({"ev": "kill", "name": "x"}) == {"ev": "kill", "name": "x"}
    passed = {"ev": "arena_turn_passed", "side": "A", "character": "ua2", "name": "Gale (downed)", "why": "downed"}
    assert ref.translate({**passed, "round": 1}) == {**passed, "round": 1, "character": "Gale"}  # the arena's name
    summon = {**passed, "character": "summon-1", "name": "Wolf", "why": "dead", "round": 1}
    assert ref.translate(summon) == {**summon, "character": "Wolf"}  # not a team member: the record's name
    # the mod's end is the match's, held until the agents are stopped (write_end), then written once with the money
    end = {"ev": "arena_end", "reason": "round_cap", "rounds": 4, "hp_left_A": 1, "hp_left_B": 1}
    assert ref.translate(end) is None and ref.mod_end == ref.pending_end == {"winner": None, **end}
    assert ref.translate({**phase, "character": "ua1", "members": ["ua1"], "names": ["Karlach"], "round": 4}) is None
    assert ref.translate({"ev": "arena_end", "reason": "wipe", "rounds": 4}) is None
    assert ref.translate({**passed, "round": 4}) is None  # after the end

    ref2 = am.Referee(spec, SIDES, hfacts.FactsWriter(tmp_path / "f2.jsonl", clock=clock.now))
    ref2.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]})
    ref2.end(winner="A", reason="forfeit", rounds=1, hp_left_A=1, hp_left_B=1, forfeit="B", cause="forced_turns")
    assert ref2.translate({"ev": "arena_end", "reason": "forfeit", "rounds": 1}) is None  # the runner's own end
    assert ref2.mod_end is None
    ref.write_end()
    ref.write_end()  # once
    written = [json.loads(line) for line in (tmp_path / "f.jsonl").read_text().splitlines()]
    assert [w["reason"] for w in written if w.get("ev") == "arena_end"] == ["round_cap"]


def test_a_mirror_arenas_surface_reads_reach_the_facts_report_only(tmp_path):
    """Cross-review 7828a6f8: arena_start carries the setup's surface check; the mod's arena_surfaces re-reads (round 2,
    the end) are written as they are, the end's after the match's end too; both fit the facts schemas."""
    import jsonschema
    from test_arena_match import BENCH_FACTS_SCHEMA, FACTS_SCHEMA

    _model, _game, clock, _agents, spec = setup({"A": passer, "B": passer})
    path = tmp_path / "f.jsonl"
    ref = am.Referee(spec, SIDES, hfacts.FactsWriter(path, clock=clock.now))
    checked = {
        "tiles_total": 207,
        "tiles_read": 206,
        "radius": 10.75,
        "off": 0,
        "wide": {"tiles_total": 596, "tiles_read": 590, "radius": 22.0, "off": 1, "tiles": ["(0.0, -12.0) ..."]},
    }
    ref.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]}, checked)
    [start] = [r for r in map(json.loads, path.read_text().splitlines()) if r.get("ev") == "arena_start"]
    assert start["surfaces"] == checked
    reread = {
        "ev": "arena_surfaces",
        "when": "round 2",
        "round": 2,
        "tiles_total": 803,
        "tiles_read": 803,
        "radius": 22.0,
        "off": 0,
        "tiles": [],
        "appeared": 1,
        "appeared_tiles": ["(3.0, 1.0) on A's half reads Fire/None, its mirror (3.0, 9.0) None/None"],
    }
    assert ref.translate(reread) == reread
    ref.translate({"ev": "arena_end", "winner": "A", "reason": "wipe"})
    assert ref.done
    end = {**reread, "when": "end"}
    assert ref.translate(end) == end
    for rec in (start, {**reread, "v": 1, "kind": "event", "t": 1.0}):
        jsonschema.validate(rec, FACTS_SCHEMA)
        jsonschema.validate(rec, BENCH_FACTS_SCHEMA)
    plain = am.Referee(spec, SIDES, hfacts.FactsWriter(tmp_path / "g.jsonl", clock=clock.now))
    plain.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]})
    rows = [json.loads(line) for line in (tmp_path / "g.jsonl").read_text().splitlines()]
    assert "surfaces" not in next(r for r in rows if r.get("ev") == "arena_start")


def test_a_runner_ended_match_still_gets_the_mods_end_surface_read(tmp_path):
    """Cross-review af5b2a91 (3): a match the runner ends (here its round cap over a mod without one) sends arena.end;
    the mod's Ar.finish reads the surfaces before its arena_end, and the runner's last poll writes that read."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer}, caps=False)
    clock.tick_hooks.insert(0, chip_once(model))
    finish = model.finish

    def finish_with_read(winner, reason):  # as Arena.lua's Ar.finish: the end read, then arena_end
        if model.ended is None:
            model.ref_record(
                "arena_surfaces",
                when="end",
                round=model.mod_round,
                tiles_total=803,
                tiles_read=803,
                radius=22.0,
                off=0,
                tiles=[],
                appeared=0,
                appeared_tiles=[],
            )
        finish(winner, reason)

    model.finish = finish_with_read
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "round_cap" and model.ends == [{"reason": "round_cap"}]
    reads = [e for e in arena_events(tmp_path) if e["ev"] == "arena_surfaces"]
    assert [(r["when"], r["tiles_read"]) for r in reads] == [("end", 803)]
