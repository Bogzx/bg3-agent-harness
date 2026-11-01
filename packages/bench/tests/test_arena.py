# pyright: basic
"""Arena files (arena.v1) and the match grader over synthetic facts."""

import copy
import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from bg3bench import arena, facts, suite

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


def match(winner="A", reason="wipe", hp=(0.75, 0.0), rounds=2, start=None, turns=None, ends=None):
    """A match's facts: run_start, arena_start, the turns, arena_end, run_end."""
    recs = [{"v": 1, "kind": "mark", "t": 0.0, "mark": "run_start", "detail": None}]
    recs.append(start or ev(1, "arena_start", arena=ARENA["name"], seed=ARENA["seed"], teams=TEAMS, sides=SIDES))
    for i, (side, who, rnd) in enumerate(turns or [("A", "Karlach", 1), ("B", "Wyll", 1), ("A", "Gale", 2)]):
        recs.append(ev(10 + i, "arena_turn", side=side, character=who, round=rnd))
    end = {"winner": winner, "rounds": rounds, "hp_left_A": hp[0], "hp_left_B": hp[1], "reason": reason}
    for e in ends if ends is not None else [end]:
        recs.append(ev(50, "arena_end", **e))
    recs.append({"v": 1, "kind": "mark", "t": 60.0, "mark": "run_end", "detail": None})
    return facts.check(recs)


# ---------------------------------------------------------------------------------------------------- arena files


def test_every_arena_file_is_valid_and_the_placeholder_is_a_draft():
    files = sorted((PKG / "arenas").glob("*.json"))
    assert [p.stem for p in files] == ["draft_open_field"]
    for p in files:
        a = json.loads(p.read_text())
        jsonschema.validate(a, ARENA_SCHEMA)
        assert arena.validate(a, p.stem) == []
    assert "draft" in arena.load_named("draft_open_field")


def test_load_fills_the_defaults(tmp_path):
    bare = {k: v for k, v in READY.items() if k not in ("round_cap", "wall_cap_min", "condition")}
    (tmp_path / f"{bare['name']}.json").write_text(json.dumps(bare))
    a = arena.load(tmp_path / f"{bare['name']}.json")
    assert (a["round_cap"], a["wall_cap_min"], a["condition"]) == (20, 30, "rules")


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
    "no seed": _with(seed=None),
    "a negative seed": _with(seed=-1),
    "round cap 0": _with(round_cap=0),
    "wall cap 0": _with(wall_cap_min=0),
    "an unknown tools list": _with(tools_off="everything"),
    "an unknown key": _with(effort="high"),
    "an empty draft reason": _with(draft=""),
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
        "rounds": 2,
        "hp_left": {"A": 0.75, "B": 0},
        "turns": {"A": 2, "B": 1},
    }


@pytest.mark.parametrize(
    "rec",
    [
        ev(1, "arena_turn", side="C", character="Karlach", round=1),
        ev(1, "arena_turn", side="A", character="Karlach", round=0),
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1.5, hp_left_B=0, reason="wipe"),
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="timeout"),
        ev(1, "arena_start", arena="x", seed=1, teams={"A": []}, sides={"A": "m", "B": "n"}),
    ],
)
def test_the_event_schema_refuses_malformed_referee_events(rec):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(rec, EVENT_SCHEMA)


# ---------------------------------------------------------------------------------------------------- the grader


@pytest.mark.parametrize(
    ("winner", "reason", "hp", "result"),
    [
        ("A", "wipe", (0.4, 0.0), "A"),
        ("B", "wipe", (0.0, 0.1), "B"),
        ("B", "round_cap", (0.3, 0.31), "B"),
        ("A", "wall_cap", (0.5, 0.2), "A"),
        (None, "round_cap", (0.5, 0.5), "draw"),
        (None, "wall_cap", (0.0, 0.0), "draw"),
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
    "off_side_turn": {"turns": [("A", "Wyll", 1)]},
    "no_end": {"ends": []},
    "past_round_cap": {"rounds": 21},
    "abort": {"winner": None, "reason": "abort", "hp": (0.5, 0.5)},
    "fault": {"winner": None, "reason": "fault", "hp": (0.5, 0.5)},
}
INCONSISTENT = {
    "wipe with the loser standing": {"hp": (0.5, 0.1)},
    "wipe without a winner": {"winner": None},
    "cap winner against the HP": {"winner": "A", "reason": "round_cap", "hp": (0.2, 0.3)},
    "cap draw that isn't a tie": {"winner": None, "reason": "round_cap", "hp": (0.2, 0.3)},
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
