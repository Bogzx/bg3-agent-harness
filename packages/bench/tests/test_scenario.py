# pyright: basic
import json
from pathlib import Path

import pytest

from bg3bench import scenario

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schema"
REPO_SCENARIOS = HERE.parents[2] / "scenarios"
TODAY = sorted(REPO_SCENARIOS.glob("*.json"))


def validator():
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    resources = []
    for name in ("goal.v1.schema.json", "scenario.v2.schema.json"):
        doc = json.loads((SCHEMAS / name).read_text())
        resources.append((doc["$id"], referencing.Resource.from_contents(doc)))
    registry = referencing.Registry().with_resources(resources)
    return jsonschema.Draft202012Validator(
        json.loads((SCHEMAS / "scenario.v2.schema.json").read_text()), registry=registry
    )


def gold_save():
    return {
        "schema": 2,
        "name": "act1_gold_1000",
        "title": "Act 1 → 1000 gold",
        "family": "sandbox",
        "holdout": True,
        "start": {
            "kind": "save",
            "save": "Tav-123__SCN-act1_gold_1000",
            "save_sha1": None,
            "note": {"where": "Emerald Grove, out of combat.", "objective": "Get the party 1000 gold."},
        },
        "goal": {"gold": {"gte": 1000}},
        "caps": {"run_hours": 0.75, "max_cost": 30},
        "profile": {"character": {"name": "Tav", "brief": "A neutral fighter."}},
        "game_settings": {
            "difficulty": "Balanced",
            "karmic_dice": False,
            "seed": 1729,
            "resolution": "1920x1080",
            "ui_scale": 100,
        },
        "timing": "realtime",
        "requires": {"vision": True, "min_context": 128000},
        "conditions": ["rules", "rules+coaching@1a2b3c4d"],
    }


@pytest.mark.parametrize("path", TODAY, ids=lambda p: p.stem)
def test_todays_scenarios_convert_unchanged(path):
    raw = path.read_text()
    v1 = json.loads(raw)
    v2 = scenario.from_v1(v1)
    assert scenario.validate(v2, path.stem) == []
    assert not list(validator().iter_errors(v2))
    loaded = scenario.load(path)
    assert path.read_text() == raw  # the file is read, never rewritten
    assert loaded["schema"] == 2 and loaded["family"] == "exploration" and loaded["holdout"] is False
    assert loaded["until"] == v1["until"] and scenario.goal_of(loaded) == {"milestone": v1["until"]}
    assert loaded["game_settings"]["difficulty"] == v1["profile"].get("difficulty")
    assert loaded["game_settings"]["karmic_dice"] is False and "difficulty" not in loaded["profile"]
    assert (loaded["timing"], loaded["conditions"]) == ("realtime", ["rules"])
    assert loaded["game_settings"]["seed"] is None
    assert loaded["requires"] == {"vision": False, "min_context": None}
    if v1["start"]["kind"] == "save" and v1["start"].get("save") is None:
        assert loaded["start"]["needs_save"] == v1["start"]["note"]["where"]
    assert "needs_save" not in v1["start"]
    assert loaded["caps"] == v1["caps"] and loaded["reset"] == scenario.RESETS[v1["start"]["kind"]]


def test_there_are_scenarios_to_convert():
    assert {p.stem for p in TODAY} >= {"nautiloid", "crash_site_grove"}


def test_a_full_v2_scenario(tmp_path):
    sc = gold_save()
    assert scenario.validate(sc, "act1_gold_1000") == []
    assert not list(validator().iter_errors(sc))
    (tmp_path / "act1_gold_1000.json").write_text(json.dumps(sc))
    loaded = scenario.load(tmp_path / "act1_gold_1000.json")
    assert loaded["runs"] == 3 and loaded["caps"] == {"run_hours": 0.75, "max_cost": 30, "max_tokens": None}
    assert loaded["game_settings"]["language"] is None and loaded["profile"]["character"]["roleplay"] == "strict"
    assert scenario.goal_of(loaded) == {"gold": {"gte": 1000}}
    assert scenario.digest(sc) == scenario.digest(json.loads(json.dumps(sc))) and len(scenario.digest(sc)) == 16


BAD = [
    ({"schema": 1}, "schema must be 2"),
    ({"name": "other"}, "must be the file name"),
    ({"extra": 1}, "unknown keys: extra"),
    ({"title": " "}, "title is required"),
    ({"task": " "}, "task must be a non-empty text"),
    ({"scored_by": 3}, "scored_by must be a non-empty text"),
    ({"family": "showcase"}, "family must be"),
    ({"holdout": "yes"}, "holdout must be"),
    ({"until": "grove"}, "exactly one of goal or until"),
    ({"goal": {"no_combat": True}}, "D8"),
    ({"goal": {"gold": "lots"}}, "gold"),
    ({"runs": 0}, "runs must be"),
    ({"caps": {"run_hours": None}}, "run_hours is required"),
    ({"caps": {"run_hours": 1, "max_cost": -1}}, "caps must be"),
    ({"profile": {"character": {"name": "Tav"}}}, "brief is required"),
    ({"profile": {"character": {"name": "Tav", "brief": "b"}, "difficulty": "Balanced"}}, "profile takes only"),
    ({"profile": {"character": {"name": "Tav", "brief": "b", "roleplay": "method"}}}, "roleplay must be"),
    ({"profile": {"character": {"brief": "b"}}}, "needs profile.character.name"),
    ({"profile": {"character": {"name": "Ana", "brief": "b"}}}, "must match the start save's character 'Tav'"),
    ({"game_settings": {"karmic_dice": True}}, "D9"),
    ({"game_settings": {"difficulty": "Hard"}}, "difficulty must be"),
    ({"game_settings": {"resolution": "big"}}, "resolution"),
    ({"game_settings": {"ui_scale": 0}}, "ui_scale"),
    ({"game_settings": {"language": ""}}, "language"),
    ({"game_settings": {"fov": 90}}, "game_settings must be"),
    ({"game_settings": {"autoskip": "no"}}, "autoskip must be"),
    ({"think_pause": True}, "think_pause was dropped"),
    ({"think_pause": False}, "think_pause was dropped"),
    ({"timing": "paused"}, "timing must be"),
    ({"methods": "on"}, "methods must be"),
    ({"timing": "turn_based"}, "a sandbox scenario is realtime"),
    ({"game_settings": {"seed": -1}}, "seed must be"),
    ({"game_settings": {"seed": 2**31}}, "seed must be"),
    ({"game_settings": {"seed": True}}, "seed must be"),
    ({"game_settings": {"seed": "42"}}, "seed must be"),
    ({"requires": {"vision": "yes"}}, "requires must be"),
    ({"requires": {"min_context": 1.5}}, "requires must be"),
    ({"conditions": []}, "conditions must be"),
    ({"conditions": ["Rules!"]}, "conditions must be"),
    ({"conditions": ["rules", "rules"]}, "duplicates"),
    ({"conditions": ["rules+wikis"]}, "unknown part"),
    ({"reset": {"saves": "never"}}, "reset must be the standard"),
]


@pytest.mark.parametrize("change,msg", BAD, ids=[m for _, m in BAD])
def test_invalid_v2(change, msg):
    sc = {**gold_save(), **change}
    errs = scenario.validate(sc, "act1_gold_1000")
    assert any(msg in e for e in errs), errs
    # every one of these is a real error; the schema rejects the ones it can express
    if msg not in (
        "must be the file name",
        "D8",
        "needs profile.character.name",
        "must match the start save's character 'Tav'",
        "unknown part",
    ):
        assert not validator().is_valid(sc), msg


START_BAD = [
    ({"kind": "boat"}, "start.kind must be"),
    ({"kind": "new_game", "save": "x"}, "takes no save"),
    ({"kind": "save", "note": {"where": "w", "objective": "o"}, "seed": 1}, "unknown start keys"),
    ({"kind": "save", "save": "nofolder", "note": {"where": "w", "objective": "o"}}, "not a save folder"),
    ({"kind": "save", "save": "Tav-1__S004-mine", "note": {"where": "w", "objective": "o"}}, "Sxxx- label"),
    ({"kind": "save", "save_sha1": 5, "note": {"where": "w", "objective": "o"}}, "save_sha1 must be"),
    ({"kind": "save", "note": {"where": "w", "objective": "o"}}, "needs start.needs_save"),
    ({"kind": "save", "save": None, "needs_save": " ", "note": {"where": "w", "objective": "o"}}, "needs_save"),
    (
        {"kind": "save", "save": "Tav-1__SCN-x", "needs_save": "here", "note": {"where": "w", "objective": "o"}},
        "only for a start whose save is not made yet",
    ),
    ({"kind": "save", "save": "Tav-1__SCN-x", "not_ready": " ", "note": {"where": "w", "objective": "o"}}, "not_ready"),
    ({"kind": "save", "note": {"where": "w"}}, "where and objective"),
    ({"kind": "save", "note": {"where": "w", "objective": "o", "mood": "x"}}, "unknown start.note keys"),
    ({"kind": "save", "note": {"where": "w", "objective": "o", "party": "Gale"}}, "party must be a list"),
    ({"kind": "save", "note": {"where": "w" * 1600, "objective": "o"}}, "chars >"),
    ({"kind": "save", "note": {"where": "as in S012", "objective": "o"}}, "neutral game facts"),
]


@pytest.mark.parametrize("start,msg", START_BAD, ids=[m for _, m in START_BAD])
def test_invalid_starts(start, msg):
    assert any(msg in e for e in scenario.validate({**gold_save(), "start": start}, "act1_gold_1000"))


def test_new_game_start_and_non_objects(tmp_path):
    sc = {**gold_save(), "start": {"kind": "new_game"}, "profile": {"character": {"brief": "b"}}}
    assert scenario.validate(sc, "act1_gold_1000") == []
    assert scenario.validate([], "x") == ["not a JSON object"]
    assert any("file name" in e for e in scenario.validate({**sc, "name": "Bad Name"}, "Bad Name"))
    until = {k: v for k, v in sc.items() if k != "goal"} | {"until": "nowhere"}
    assert any("not a milestone" in e for e in scenario.validate(until, "act1_gold_1000"))
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(scenario.ScenarioError):
        scenario.load(tmp_path / "bad.json")
    (tmp_path / "act1_gold_1000.json").write_text(json.dumps({**sc, "family": "x"}))
    with pytest.raises(scenario.ScenarioError, match="family"):
        scenario.load(tmp_path / "act1_gold_1000.json")


def test_not_ready():
    sc = scenario.with_defaults(gold_save())
    made = {"kind": "save", "save": "Tav-1__SCN-act1_gold_1000", "save_sha1": "ab", "note": sc["start"]["note"]}
    assert scenario.not_ready({**sc, "start": made}) is None
    assert scenario.not_ready({**sc, "start": {**made, "not_ready": "the inspector refused it"}}) == (
        "the inspector refused it"
    )
    unmade = {**made, "save": None, "save_sha1": None, "needs_save": "Act 1"}
    assert "no start save yet" in scenario.not_ready({**sc, "start": unmade})  # type: ignore[operator]
    assert scenario.not_ready({**sc, "start": {"kind": "new_game"}}) is None


def test_applicable():
    sc = gold_save()
    assert scenario.applicable(sc, vision=True, context=200000) == (True, None)
    assert scenario.applicable(sc, vision=False, context=200000) == (False, "needs vision")
    assert scenario.applicable(sc, vision=True, context=64000)[0] is False
    assert scenario.applicable(sc, vision=True, context=None)[0] is False
    assert scenario.applicable({}, vision=False, context=None) == (True, None)


def test_timing_defaults_by_family(tmp_path):
    fight = {k: v for k, v in gold_save().items() if k != "timing"} | {"family": "fight"}
    assert scenario.validate(fight, "act1_gold_1000") == []
    (tmp_path / "act1_gold_1000.json").write_text(json.dumps(fight))
    assert scenario.load(tmp_path / "act1_gold_1000.json")["timing"] == "turn_based"
    assert scenario.validate({**fight, "timing": "realtime"}, "act1_gold_1000") == []
    sandbox = {k: v for k, v in gold_save().items() if k != "timing"}
    (tmp_path / "act1_gold_1000.json").write_text(json.dumps(sandbox))
    assert scenario.load(tmp_path / "act1_gold_1000.json")["timing"] == "realtime"
    assert not validator().is_valid({**sandbox, "family": "exploration", "timing": "turn_based"})
    assert validator().is_valid({**fight, "timing": "turn_based"})
