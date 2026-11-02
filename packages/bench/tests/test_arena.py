# pyright: basic
"""Arena files (arena.v1) and the match grader over synthetic facts."""

import copy
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from bg3bench import arena, facts, stats, suite

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
REPO = PKG.parents[1]
FIXTURES = HERE / "fixtures" / "arena"
ARENA_SCHEMA = json.loads((PKG / "schema" / "arena.v1.schema.json").read_text())
EVENT_SCHEMA = json.loads((PKG / "schema" / "arena-facts.v1.schema.json").read_text())
FACTS_SCHEMA = json.loads((REPO / "packages" / "harness" / "schemas" / "facts.v1.schema.json").read_text())
DRAFT = json.loads((PKG / "arenas" / "draft_open_field.json").read_text())
SAVE = {"name": "Tav-1234__SCN-arena_open_field", "sha1": "0123456789abcdef0123456789abcdef01234567"}
READY = {k: v for k, v in {**DRAFT, "save": SAVE}.items() if k != "draft"}
ARENA = arena.with_defaults(READY)
TEAMS = arena.team_names(ARENA)
SIDES = {"A": "pi/model-x", "B": "pi/model-y"}


def ev(t, name, **kw):
    return {"v": 1, "kind": "event", "t": float(t), "ev": name, **kw}


def match(winner="A", reason="wipe", hp=(0.75, 0.0), rounds=2, start=None, turns=None, ends=None, forced=(), **kw):
    """A match's facts: run_start, arena_start, the turns ((side, character, round[, members]); one member by
    default; those at the indices in `forced` forced to an end), arena_end (+ kw), run_end."""
    recs = [{"v": 1, "kind": "mark", "t": 0.0, "mark": "run_start", "detail": None}]
    recs.append(start or ev(1, "arena_start", arena=ARENA["name"], seed=ARENA["seed"], teams=TEAMS, sides=SIDES))
    for i, (side, who, rnd, *members) in enumerate(turns or [("A", "Karlach", 1), ("B", "Wyll", 1), ("A", "Gale", 2)]):
        names = members[0] if members else [who]
        phase = {"members": names, "names": names, "phase": 1, "group": "g1", "shared": True}
        recs.append(ev(10 + i, "arena_turn", side=side, character=who, round=rnd, **phase))
        if i in forced:
            recs.append(ev(10.5 + i, "arena_turn_forced", side=side, character=who))
    end = {"winner": winner, "rounds": rounds, "hp_left_A": hp[0], "hp_left_B": hp[1], "reason": reason, **kw}
    for e in ends if ends is not None else [end]:
        recs.append(ev(50, "arena_end", **e))
    recs.append({"v": 1, "kind": "mark", "t": 60.0, "mark": "run_end", "detail": None})
    return facts.check(recs)


# ---------------------------------------------------------------------------------------------------- arena files


def test_every_arena_file_is_valid_and_the_placeholder_is_a_draft():
    files = sorted((PKG / "arenas").glob("*.json"))
    assert [p.stem for p in files] == ["crash-companions-v1", "draft_open_field"]
    for p in files:
        a = json.loads(p.read_text())
        jsonschema.validate(a, ARENA_SCHEMA)
        assert arena.validate(a, p.stem) == []
    assert "draft" in arena.load_named("draft_open_field")


def test_the_companions_arena_is_the_crash_site_save_with_every_member_named_and_identified():
    a = arena.load_named("crash-companions-v1")
    saves = json.loads((PKG / "saves.json").read_text())
    rows = [r for suite in saves["suites"].values() for r in suite["saves"] if r["name"] == a["save"]["name"]]
    sha1s = {r["save_sha1"] for r in rows}
    assert sha1s == {a["save"]["sha1"]} and "crash_site_grove" in a["save"]["name"]
    assert arena.team_names(a) == {
        "A": ["Shadowheart", "Gale", "Astarion", "Lae'zel"],
        "B": ["Karlach", "Wyll", "Nightwarden Minthara", "Minsc"],
    }
    assert all("uuid" in m for s in ("A", "B") for m in a["teams"][s])
    assert (a["level"], a["seed"], a["round_cap"], a["wall_cap_min"], a["turn_cap_s"]) == (2, 2026, 20, 30, 60)
    assert "draft" not in a


def test_load_fills_the_defaults(tmp_path):
    bare = {k: v for k, v in READY.items() if k not in ("round_cap", "wall_cap_min", "condition", "level")}
    (tmp_path / f"{bare['name']}.json").write_text(json.dumps(bare))
    a = arena.load(tmp_path / f"{bare['name']}.json")
    assert (a["round_cap"], a["wall_cap_min"], a["condition"], a["level"]) == (20, 30, "rules", 2)


def _with(**kw):
    a = copy.deepcopy(READY)
    for k, v in kw.items():
        if v is None:
            a.pop(k)
        else:
            a[k] = v
    return a


BAD = {
    "no save outside a draft": _with(save={"name": None, "sha1": None}),
    "short sha1": _with(save={**SAVE, "sha1": "abc"}),
    "not a save folder": _with(save={**SAVE, "name": "no_owner_label"}),
    "one spawn": _with(location={"level": "WLD_Main_A", "spawns": {"A": [0, 0, 0]}}),
    "a 2d spawn": _with(location={"level": "WLD_Main_A", "spawns": {"A": [0, 0], "B": [1, 0, 0]}}),
    "five a side": _with(teams={"A": [{"name": f"a{i}"} for i in range(5)], "B": [{"name": "b"}]}),
    "an empty side": _with(teams={"A": [], "B": [{"name": "b"}]}),
    "an unknown member key": _with(teams={"A": [{"name": "a", "hp": 9}], "B": [{"name": "b"}]}),
    "a member uuid that isn't one": _with(teams={"A": [{"name": "a", "uuid": "Shadowheart"}], "B": [{"name": "b"}]}),
    "level 0": _with(level=0),
    "level 13": _with(level=13),
    "a level that isn't a number": _with(level="2"),
    "no seed": _with(seed=None),
    "a negative seed": _with(seed=-1),
    "round cap 0": _with(round_cap=0),
    "wall cap 0": _with(wall_cap_min=0),
    "an unknown tools list": _with(tools_off="everything"),
    "an unknown key": _with(effort="high"),
    "an empty draft reason": _with(draft=""),
    "karmic dice on": _with(karmic=True),
    "no karmic": _with(karmic=None),
    "no turn cap": _with(turn_cap_s=None),
    "a turn cap of 0": _with(turn_cap_s=0),
}


@pytest.mark.parametrize("why", sorted(BAD))
def test_a_bad_arena_file_is_refused_by_the_validator_and_the_schema(why):
    a = BAD[why]
    assert arena.validate(a, a["name"]) != []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(a, ARENA_SCHEMA)


def test_validator_only_checks():
    same = _with(location={"level": "WLD_Main_A", "spawns": {"A": [1, 0, 0], "B": [1, 0, 0]}})
    assert any("same point" in e for e in arena.validate(same, READY["name"]))
    twice = _with(teams={"A": [{"name": "Karlach"}], "B": [{"name": "Karlach"}]})
    assert any("listed twice" in e for e in arena.validate(twice, READY["name"]))
    u = "3ed74f06-3c60-42dc-83f6-f034cb47c679"
    same_uuid = _with(teams={"A": [{"name": "Shadowheart", "uuid": u}], "B": [{"name": "Wyll", "uuid": u}]})
    assert any(f"uuid {u} is listed twice" in e for e in arena.validate(same_uuid, READY["name"]))
    assert any("file's name" in e for e in arena.validate(READY, "other_name"))
    assert any("reloads" in e for e in arena.validate(_with(condition="rules+reloads:2"), READY["name"]))
    assert any("condition" in e for e in arena.validate(_with(condition="coaching"), READY["name"]))
    assert arena.validate(_with(condition="rules+wiki"), READY["name"]) == []
    assert arena.validate([], "x") == ["an arena file is a JSON object"]


def test_load_names_the_file_and_every_problem(tmp_path):
    p = tmp_path / "draft_open_field.json"
    p.write_text(json.dumps({**READY, "seed": -1, "round_cap": 0}))
    with pytest.raises(arena.ArenaError, match=r"draft_open_field\.json: .*seed.*round_cap"):
        arena.load(p)
    p.write_text("{")
    with pytest.raises(arena.ArenaError, match="draft_open_field.json"):
        arena.load(p)


def test_arena_files_and_modules_are_outside_the_suite_digest(tmp_path):
    assert suite.check() == []
    assert not [f for f in suite.suite_files() if "arena" in f]
    root = tmp_path / "bench"
    for rel in suite.suite_files():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PKG / rel, root / rel)
    before = suite.digest(root)
    shutil.copytree(PKG / "arenas", root / "arenas")
    shutil.copy(PKG / "src" / "bg3bench" / "arena.py", root / "src" / "bg3bench" / "arena.py")
    assert suite.digest(root) == before


# ---------------------------------------------------------------------------------------------------- the event schema


def test_the_fixture_match_fits_the_facts_schemas_and_grades():
    recs = facts.load(FIXTURES / "wipe_a.facts.jsonl")
    for r in recs:
        if str(r.get("ev", "")).startswith("arena_"):
            jsonschema.validate(r, EVENT_SCHEMA)
        else:
            jsonschema.validate(r, FACTS_SCHEMA)
    g = arena.grade(recs, arena.with_defaults(DRAFT))
    assert g == {
        "arena": "draft_open_field",
        "valid": True,
        "invalid_reason": None,
        "sides": {"A": "pi/model-x", "B": "pi/model-y"},
        "result": "A",
        "reason": "wipe",
        "forfeit": None,
        "rounds": 2,
        "hp_left": {"A": 0.75, "B": 0},
        "turns": {"A": 2, "B": 1},
        "forced": {"A": 0, "B": 0},
        "luck": None,
    }


TURN = ev(
    1,
    "arena_turn",
    side="A",
    character="Karlach",
    members=["Karlach"],
    names=["Karlach"],
    round=1,
    phase=1,
    group="g",
    shared=True,
)


@pytest.mark.parametrize(
    "rec",
    [
        ev(1, "arena_turn", side="C", character="Karlach", round=1),
        ev(1, "arena_turn", side="A", character="Karlach", round=0),
        ev(1, "arena_turn", side="A", character="Karlach", round=1),  # no phase fields
        {k: v for k, v in TURN.items() if k != "names"},
        {
            **TURN,
            "members": [],
        },
        {
            **TURN,
            "phase": 0,
        },
        {
            **TURN,
            "shared": "yes",
        },
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1.5, hp_left_B=0, reason="wipe"),
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="timeout"),
        ev(1, "arena_start", arena="x", seed=1, teams={"A": []}, sides={"A": "m", "B": "n"}),
        ev(1, "arena_turn_forced", side="A"),
        ev(1, "arena_end", winner="B", rounds=2, hp_left_A=1, hp_left_B=1, reason="forfeit"),  # no forfeit, cause
        ev(1, "arena_end", winner="B", rounds=2, hp_left_A=1, hp_left_B=1, reason="forfeit", forfeit="A", cause="x"),
        ev(1, "arena_end", winner="A", rounds=20, hp_left_A=1, hp_left_B=0.5, reason="round_cap"),  # a cap's winner
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="wipe", detail=3),
        ev(1, "arena_reaction_unowned", round=2),
        ev(1, "arena_reaction_unowned", round=-1, detail="Wolf | Shield"),
    ],
)
def test_the_event_schema_refuses_malformed_referee_events(rec):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(rec, EVENT_SCHEMA)


def test_the_event_schema_takes_a_declined_unowned_prompt_and_an_end_the_runner_came_after():
    phase = {"members": ["Karlach", "Gale"], "names": ["Karlach", "Gale"], "round": 1, "phase": 2, "shared": True}
    jsonschema.validate(ev(1, "arena_turn", side="A", character="Karlach", **phase), EVENT_SCHEMA)
    jsonschema.validate(
        ev(1, "arena_reaction_unowned", round=2, detail="Wolf | Shield | Choose how to react."), EVENT_SCHEMA
    )
    late = "the runner's end came after it: wall_cap"
    end = ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="wipe", detail=late)
    jsonschema.validate(end, EVENT_SCHEMA)


# ---------------------------------------------------------------------------------------------------- the grader


@pytest.mark.parametrize(
    ("winner", "reason", "hp", "result"),
    [
        ("A", "wipe", (0.4, 0.0), "A"),
        ("B", "wipe", (0.0, 0.1), "B"),
        (None, "round_cap", (0.3, 0.9), "draw"),  # the HP is recorded, never a tiebreak
        (None, "wall_cap", (0.5, 0.2), "draw"),
        (None, "round_cap", (0.5, 0.5), "draw"),
    ],
)
def test_grade_reads_the_result(winner, reason, hp, result):
    g = arena.grade(match(winner, reason, hp), ARENA)
    assert (g["valid"], g["result"], g["reason"], g["hp_left"]) == (True, result, reason, {"A": hp[0], "B": hp[1]})
    assert g["sides"] == SIDES


def _start(**kw):
    return ev(1, "arena_start", **{"arena": ARENA["name"], "seed": ARENA["seed"], "teams": TEAMS, "sides": SIDES, **kw})


END = {"winner": "A", "rounds": 2, "hp_left_A": 0.5, "hp_left_B": 0.0, "reason": "wipe"}
INVALID = {
    "no_start": {"start": {"v": 1, "kind": "mark", "t": 1.0, "mark": "settings", "detail": {}}},
    "wrong_arena": {"start": _start(arena="other")},
    "unseeded": {"start": _start(seed=7)},
    "scripted": {"start": _start(sides={"A": "scripted/none", "B": "pi/model-y"})},
    "off_side_turn": {"turns": [("A", "Wyll", 1)]},
    "no_end": {"ends": []},
    "past_round_cap": {"rounds": 21},
    "abort": {"winner": None, "reason": "abort", "hp": (0.5, 0.5)},
    "fault": {"winner": None, "reason": "fault", "hp": (0.5, 0.5)},
}
INCONSISTENT = {
    "wipe with the loser standing": {"hp": (0.5, 0.1)},
    "wipe without a winner": {"winner": None},
    "a round cap with a winner": {"winner": "A", "reason": "round_cap", "hp": (0.5, 0.2)},
    "a wall cap with a winner": {"winner": "B", "reason": "wall_cap", "hp": (0.2, 0.5)},
    "a forfeit without a side": {"winner": "B", "reason": "forfeit", "cause": "client_fault"},
    "a forfeit without a cause": {"winner": "B", "reason": "forfeit", "forfeit": "A"},
    "a forfeit won by the forfeiter": {"winner": "A", "reason": "forfeit", "forfeit": "A", "cause": "client_fault"},
    "a forced-turns forfeit with no forced turns": {
        "winner": "B",
        "reason": "forfeit",
        "forfeit": "A",
        "cause": "forced_turns",
    },
    "two ends": {"ends": [END, END]},
    "an unknown reason": {"reason": "timeout"},
    "an hp over 1": {"hp": (1.5, 0.0)},
    "no rounds": {"ends": [{k: v for k, v in END.items() if k != "rounds"}]},
    "a bad winner": {"winner": "C"},
}


@pytest.mark.parametrize("why", sorted(INVALID))
def test_grade_invalid_matches(why):
    g = arena.grade(match(**INVALID[why]), ARENA)
    assert (g["valid"], g["invalid_reason"], g["result"]) == (False, why, None)


@pytest.mark.parametrize("members", [["Karlach", "Wyll"], None])
def test_grade_a_phase_with_a_member_of_the_other_team_is_an_off_side_turn(members):
    g = arena.grade(match(turns=[("A", "Karlach", 1, members), ("B", "Wyll", 1)]), ARENA)
    assert (g["valid"], g["invalid_reason"]) == (False, "off_side_turn")


def test_grade_a_summon_on_neither_team_counts_on_its_phases_side():
    turns = [("A", "Karlach", 1, ["Karlach", "Wolf"]), ("B", "Owl", 1, ["Owl"]), ("B", "Wyll", 1)]
    g = arena.grade(match(turns=turns), ARENA)
    assert (g["valid"], g["turns"]) == (True, {"A": 1, "B": 2})


def test_grade_counts_a_phase_of_several_members_as_one_turn():
    g = arena.grade(
        match(turns=[("A", "Karlach", 1, ["Karlach", "Gale"]), ("B", "Wyll", 1, ["Wyll", "Halsin"])]), ARENA
    )
    assert (g["valid"], g["turns"]) == (True, {"A": 1, "B": 1})


@pytest.mark.parametrize("why", sorted(INCONSISTENT))
def test_grade_refuses_an_end_that_contradicts_itself(why):
    g = arena.grade(match(**INCONSISTENT[why]), ARENA)
    assert (g["valid"], g["invalid_reason"]) == (False, "inconsistent_end")


def test_grade_wrong_teams_or_two_starts_is_the_wrong_arena():
    swapped = {"A": TEAMS["B"], "B": TEAMS["A"]}
    assert arena.grade(match(start=_start(teams=swapped)), ARENA)["invalid_reason"] == "wrong_arena"
    assert arena.grade(match(start=_start(sides={"A": "pi/x"})), ARENA)["invalid_reason"] == "wrong_arena"
    twice = match()
    twice.insert(2, _start())
    assert arena.grade(twice, ARENA)["invalid_reason"] == "wrong_arena"


def test_grade_counts_turns_up_to_the_end_only():
    recs = match()
    recs.insert(-1, ev(60, "arena_turn", side="B", character="Not On Any Team", round=3))
    g = arena.grade(recs, ARENA)
    assert (g["valid"], g["turns"]) == (True, {"A": 2, "B": 1})


@pytest.mark.parametrize(
    ("record", "why"),
    [
        ({"outcome": "runner_error"}, "runner_error"),
        ({"outcome": "reached", "harness_failures": 1}, "harness_failure"),
        ({"outcome": "reached", "operator": "stop"}, "operator"),
        ({"outcome": "reached", "off_route": "B: openrouter"}, "off_route"),
        ({"outcome": "reached", "game_settings": {"seed": {"applied": {"seed": 5}}}}, "unseeded"),
    ],
)
def test_grade_takes_the_run_records_invalid_rules(record, why):
    g = arena.grade(match(), ARENA, record)
    assert (g["valid"], g["invalid_reason"], g["result"]) == (False, why, None)


def test_grade_a_valid_run_record_leaves_the_facts_verdict():
    rec = {"outcome": "reached", "game_settings": {"seed": {"applied": {"seed": ARENA["seed"]}}}}
    assert arena.grade(match(), ARENA, rec)["valid"] is True
    assert arena.grade(match(rounds=21), ARENA, rec)["invalid_reason"] == "past_round_cap"


# ---------------------------------------------------------------------------------------------------- forfeits

SIX = [
    ("A", "Karlach", 1),
    ("B", "Wyll", 1),
    ("A", "Gale", 2),
    ("B", "Halsin", 2),
    ("A", "Astarion", 3),
    ("B", "Wyll", 3),
]
FORFEIT_A = {"winner": "B", "reason": "forfeit", "forfeit": "A", "hp": (1.0, 1.0)}


def test_three_forced_turns_in_a_row_forfeit():
    g = arena.grade(match(turns=SIX, forced=(0, 2, 4), cause="forced_turns", **FORFEIT_A), ARENA)
    assert (g["valid"], g["result"], g["reason"]) == (True, "B", "forfeit")
    assert (g["forfeit"], g["forced"]) == ({"side": "A", "cause": "forced_turns"}, {"A": 3, "B": 0})


def test_the_other_sides_turns_do_not_break_a_streak_but_a_played_turn_does():
    # A's turns 0, 2, 4 forced with B's turns between: three in a row. A's 0, 2 forced and 4 played: no forfeit.
    assert arena.grade(match(turns=SIX, forced=(0, 1, 2, 4), cause="forced_turns", **FORFEIT_A), ARENA)["valid"]
    g = arena.grade(match(turns=[*SIX, ("A", "Gale", 4)], forced=(0, 2, 6), cause="forced_turns", **FORFEIT_A), ARENA)
    assert g["invalid_reason"] == "inconsistent_end"


def test_a_match_that_goes_on_past_a_forfeit_contradicts_itself():
    g = arena.grade(match(turns=SIX, forced=(0, 2, 4), hp=(0.0, 0.5), winner="B"), ARENA)
    assert g["invalid_reason"] == "inconsistent_end"
    other = {"winner": "A", "reason": "forfeit", "forfeit": "B", "cause": "client_fault", "hp": (1.0, 1.0)}
    assert arena.grade(match(turns=SIX, forced=(0, 2, 4), **other), ARENA)["invalid_reason"] == "inconsistent_end"


def test_a_client_fault_forfeit_needs_no_forced_turns():
    g = arena.grade(match(winner="A", reason="forfeit", forfeit="B", cause="client_fault", hp=(0.6, 0.9)), ARENA)
    assert (g["valid"], g["result"], g["forfeit"]) == (True, "A", {"side": "B", "cause": "client_fault"})


def test_a_forced_turn_must_name_the_turn_in_progress():
    recs = match(turns=SIX[:2])
    recs.insert(4, ev(11.5, "arena_turn_forced", side="B", character="Halsin"))
    assert arena.grade(recs, ARENA)["invalid_reason"] == "inconsistent_turns"
    twice = match(turns=SIX[:2], forced=(1,))
    twice.insert(5, ev(11.6, "arena_turn_forced", side="B", character="Wyll"))
    assert arena.grade(twice, ARENA)["invalid_reason"] == "inconsistent_turns"
    before = match()
    before.insert(2, ev(1, "arena_turn_forced", side="A", character="Karlach"))
    assert arena.grade(before, ARENA)["invalid_reason"] == "inconsistent_turns"


def test_forced_and_forfeit_events_fit_the_event_schema():
    recs = match(turns=SIX, forced=(0, 2, 4), cause="forced_turns", **FORFEIT_A)
    for r in recs:
        if str(r.get("ev", "")).startswith("arena_"):
            jsonschema.validate(r, EVENT_SCHEMA)


# ---------------------------------------------------------------------------------------------------- luck

UUIDS = {"A": ["a-1", "a-2"], "B": ["b-1"]}


def roll(roller, natural, **kw):
    return {"roller": roller, "natural": natural, **kw}


def test_luck_is_each_sides_z_over_its_own_rolls():
    rolls = [
        roll("a-1", 20),
        roll("a-2", 20),
        roll("b-1", 1),
        roll("a-1", 1, advantage=True),  # not uniform: left out
        roll("b-1", None),  # unseen: left out
        roll("summon-9", 7),  # neither side
    ]
    out = arena.luck(rolls, UUIDS)
    assert out == {
        "A": {"n": 2, "z": round(stats.luck_z([20, 20]), 4)},
        "B": {"n": 1, "z": round(stats.luck_z([1]), 4)},
        "other": 1,
    }
    assert out["A"]["z"] > 0 > out["B"]["z"]


def test_grade_reports_luck_with_rolls_and_uuids():
    start = _start(uuids=UUIDS)
    jsonschema.validate(start, EVENT_SCHEMA)
    g = arena.grade(match(start=start), ARENA, rolls=[roll("b-1", 15)])
    assert g["luck"] == {"A": {"n": 0, "z": 0.0}, "B": {"n": 1, "z": round(stats.luck_z([15]), 4)}, "other": 0}
    assert arena.grade(match(), ARENA, rolls=[roll("b-1", 15)])["luck"] is None  # no uuids: can't attribute
    assert arena.grade(match(start=start), ARENA)["luck"] is None
