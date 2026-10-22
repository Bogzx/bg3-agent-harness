# pyright: basic
"""bg3bench.scenario.resolve and `python -m bg3bench.scenario resolve`: the runner-facing file (v1 shape plus
game_settings and timing) that play.py run --scenario-file and the matrix's legacy CLI adapter read."""

import json
from pathlib import Path

import pytest

from bg3bench import predicates, scenario, scenario_v1

HERE = Path(__file__).resolve().parent
V2 = HERE.parent / "scenarios"
GOLDEN = HERE / "fixtures" / "resolved"


def v1_problems(resolved: dict) -> list[str]:
    return scenario_v1.validate_scenario(
        resolved, resolved["name"], list(predicates.MILESTONES), scenario.ROLEPLAYS, scenario.DIFFICULTIES
    )


def write(tmp_path: Path, sc: dict) -> Path:
    path = tmp_path / f"{sc['name']}.json"
    path.write_text(json.dumps(sc), encoding="utf-8")
    return path


def gold_with_a_milestone(**over) -> dict:
    """act1_gold_1000 as a save start whose save isn't made yet, with a milestone goal instead of its predicate."""
    sc = json.loads((V2 / "act1_gold_1000.json").read_text(encoding="utf-8"))
    del sc["goal"]
    note = sc["start"]["note"]
    sc["start"] = {"kind": "save", "save": None, "save_sha1": None, "needs_save": "Act 1, the Grove.", "note": note}
    return {**sc, "until": "grove", **over}


def test_nautiloid_resolves_to_the_golden_file():
    golden = json.loads((GOLDEN / "nautiloid.json").read_text(encoding="utf-8"))
    assert scenario.resolve(scenario.load(V2 / "nautiloid.json")) == golden
    assert golden["game_settings"] == {"difficulty": "Balanced", "karmic_dice": False, "seed": 1001}
    assert golden["profile"]["difficulty"] == "Balanced" and golden["timing"] == "realtime"
    assert v1_problems(golden) == []


def test_the_cli_prints_the_golden_file(capsys):
    assert scenario.main(["resolve", str(V2 / "nautiloid.json")]) == 0
    assert capsys.readouterr().out == (GOLDEN / "nautiloid.json").read_text(encoding="utf-8")


def test_a_goal_that_is_not_one_milestone_is_refused(capsys):
    with pytest.raises(scenario.ScenarioError, match="not one milestone"):
        scenario.resolve(scenario.load(V2 / "fight_act1_windmill.json"))
    assert scenario.main(["resolve", str(V2 / "fight_act1_windmill.json")]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "refusing: fight_act1_windmill: goal" in out.err


def test_an_invalid_file_is_refused(tmp_path, capsys):
    assert scenario.main(["resolve", str(write(tmp_path, gold_with_a_milestone(timing="paused")))]) == 2
    assert "timing must be" in capsys.readouterr().err


def test_a_save_start_drops_needs_save_and_keeps_the_note(tmp_path):
    sc = gold_with_a_milestone()
    resolved = scenario.resolve(scenario.load(write(tmp_path, sc)))
    assert resolved["start"] == {k: v for k, v in sc["start"].items() if k != "needs_save"}
    assert resolved["start"]["save"] is None  # v1's placeholder: the runner says it needs a start save
    assert resolved["until"] == "grove" and resolved["reset"] == scenario.RESETS["save"]
    assert resolved["profile"]["character"]["name"] == "Tav"
    assert set(resolved) == {
        "name", "title", "about", "start", "until", "runs", "caps", "profile", "reset", "game_settings", "timing",
    }  # fmt: skip
    assert v1_problems(resolved) == []
    loaded = scenario_v1.load_scenario(
        write(tmp_path, resolved), list(predicates.MILESTONES), scenario.ROLEPLAYS, scenario.DIFFICULTIES
    )
    assert scenario_v1.scenario_status(loaded, set())[0] == scenario_v1.NEEDS_SAVE


def test_a_save_start_drops_not_ready(tmp_path):
    sc = gold_with_a_milestone()
    sc["start"]["not_ready"] = "not made: the nurse turns hostile"
    loaded = scenario.load(write(tmp_path, sc))
    assert scenario.not_ready(loaded) == "not made: the nurse turns hostile"
    resolved = scenario.resolve(loaded)
    assert "not_ready" not in resolved["start"] and v1_problems(resolved) == []


def test_a_milestone_goal_resolves_to_until(tmp_path):
    sc = gold_with_a_milestone()
    del sc["until"]
    sc["goal"] = {"milestone": "grove"}
    assert scenario.resolve(scenario.load(write(tmp_path, sc)))["until"] == "grove"


def test_each_v2_scenario_resolves_or_is_refused_for_its_goal():
    for path in sorted(V2.glob("*.json")):
        sc = scenario.load(path)
        if set(scenario.goal_of(sc)) == {"milestone"}:
            assert v1_problems(scenario.resolve(sc)) == [], path.name
        else:
            with pytest.raises(scenario.ScenarioError, match="not one milestone"):
                scenario.resolve(sc)


def test_autoskip_reaches_the_runner_file_only_when_off(tmp_path):
    """Default on: the resolved file is what it was before the setting existed (a series' frozen digest holds);
    off: the runner reads game_settings.autoskip = false and the v1 check takes it."""
    sc = gold_with_a_milestone()
    on = scenario.resolve(scenario.load(write(tmp_path, sc)))
    assert "autoskip" not in on["game_settings"]
    off = scenario.resolve(scenario.load(write(tmp_path, {**sc, "game_settings": {"autoskip": False}})))
    assert off["game_settings"]["autoskip"] is False
    assert v1_problems(off) == []
