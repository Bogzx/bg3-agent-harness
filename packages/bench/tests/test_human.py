# pyright: basic
"""The human baseline (bg3bench.human, docs/HUMAN-BASELINE.md): the results file reader and the "human (owner)"
leaderboard row next to the models'."""

import json
import re
from pathlib import Path

import pytest

from bg3bench import human, results, scenario
from bg3bench.site import build

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schema"
SCENARIOS = {p.stem: scenario.load(p) for p in (HERE.parent / "scenarios").glob("*.json")}
DOC = HERE.parents[2] / "docs" / "HUMAN-BASELINE.md"


def attempt(
    name="fight_act1_windmill", n=1, *, passed=True, time_s=900.0, t_pass_s: float | None = 840.0, reloads=0, **kw
):
    return {
        "scenario": name,
        "attempt": n,
        "date": "2026-10-23",
        "passed": passed,
        "time_s": time_s,
        "t_pass_s": t_pass_s if passed else None,
        "reloads": reloads,
        "void": None,
        **kw,
    }


def results_file(*attempts):
    return {"schema": 1, "player": "owner", "game_version": "4.1.1.6758295", "attempts": list(attempts)}


def schema_errors(data, name):
    jsonschema = pytest.importorskip("jsonschema")
    return [
        e.message for e in jsonschema.Draft202012Validator(json.loads((SCHEMAS / name).read_text())).iter_errors(data)
    ]


def test_the_doc_example_is_valid():
    m = re.search(r"```json\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.DOTALL)
    assert m
    example = json.loads(m[1])
    assert human.validate(example, SCENARIOS) == []
    assert schema_errors(example, "human.v1.schema.json") == []


def test_valid_file_loads(tmp_path):
    data = results_file(attempt(), attempt(n=2, passed=False), attempt("nautiloid"))
    path = tmp_path / "human.json"
    path.write_text(json.dumps(data))
    assert human.load(path, SCENARIOS) == data
    assert schema_errors(data, "human.v1.schema.json") == []


@pytest.mark.parametrize(
    ("data", "msg"),
    [
        (results_file(attempt(), attempt(n=3)), "numbered 1..2"),
        (results_file(attempt(), attempt()), "numbered 1..2"),
        (results_file(attempt("owlbear")), "unknown scenario"),
        (results_file(attempt(t_pass_s=None)), "needs t_pass_s"),
        (results_file(attempt(t_pass_s=901.0)), "after the attempt ended"),
        (results_file({**attempt(passed=False), "t_pass_s": 5.0}), "only set on a passed"),
        (results_file(attempt(reloads=-1)), "reloads"),
        (results_file(attempt(date="23.10.2026")), "YYYY-MM-DD"),
        (results_file(attempt(picks=[{"scene": "a", "pick": "b"}])), "only belong to a choice"),
        (results_file(attempt("choice_act1_karlach", picks=[{"scene": "a"}])), "list of {scene, pick}"),
        (results_file(attempt(void="")), "void must be"),
        (results_file(attempt(seconds=3)), "unknown key 'seconds'"),
        ({**results_file(), "player": "The Owner"}, "player"),
        ({**results_file(), "schema": 2}, "schema must be 1"),
    ],
)
def test_bad_files_are_refused(tmp_path, data, msg):
    assert any(msg in e for e in human.validate(data, SCENARIOS)), human.validate(data, SCENARIOS)
    path = tmp_path / "human.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=re.escape(msg)):
        human.load(path, SCENARIOS)


def test_headline_is_first_try_no_reload_inside_the_cap():
    windmill = SCENARIOS["fight_act1_windmill"]
    cap_s = windmill["caps"]["run_hours"] * 3600
    assert human.headline_pass(attempt(), windmill)
    assert not human.headline_pass(attempt(reloads=1), windmill)  # passed after a reload: not the headline
    assert not human.headline_pass(attempt(time_s=cap_s + 60, t_pass_s=cap_s + 1), windmill)
    assert human.headline_pass(attempt(time_s=cap_s, t_pass_s=cap_s), windmill)
    assert not human.headline_pass(attempt(passed=False), windmill)


def test_pairs_score_and_void():
    data = results_file(
        attempt(),
        attempt(n=2, reloads=2),
        attempt(n=3, void="the game crashed at 12 min"),
        attempt("choice_act1_karlach", picks=[{"scene": "Karlach", "pick": "Recruit her"}], t_pass_s=300.0),
    )
    got = human.pairs(data, SCENARIOS, "0.1.0")
    man, res = got[0]
    assert man["model"]["slug"] == "human (owner)" and man["condition"] is None
    assert man["run_id"] == res["run_id"] == "human-owner-fight_act1_windmill-a01"
    assert man["scenario"] == {"name": "fight_act1_windmill", "timing": "turn_based"}
    assert res["passed"] and res["valid"] and res["t_pass_s"] == 840.0 and res["cost_usd"] is None
    assert not got[1][1]["passed"] and got[1][1]["t_pass_s"] is None and got[1][1]["reloads"] == 2
    assert not got[2][1]["valid"] and got[2][1]["invalid_reason"] == "operator" and not got[2][1]["passed"]
    assert got[3][1]["choices"] == {"picks": [{"scene": "Karlach", "pick": "Recruit her"}], "fit": None}


def model_run(i, passed):
    rid = f"opus-fight_act1_windmill-{i}"
    man = {
        "run_id": rid,
        "model": {"slug": "opus"},
        "condition": "rules",
        "versions": {"harness": "0.1.0", "bench": "0.1.0"},
        "scenario": {"name": "fight_act1_windmill", "timing": "turn_based"},
    }
    res = {
        "run_id": rid,
        "valid": True,
        "passed": passed,
        "t_pass_s": 600.0 if passed else None,
        "cost_usd": 2.0,
        "wall_s": 700.0,
        "think_s": None,
        "luck": None,
        "tokens": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1},
    }
    return man, res


def test_the_human_row_on_the_leaderboard(tmp_path):
    data = results_file(attempt(), attempt(n=2, reloads=1), attempt(n=3, void="crash"), attempt(n=4))
    pairs = [model_run(i, i == 0) for i in range(3)] + human.pairs(data, SCENARIOS, "0.1.0")
    release = results.build_results(pairs, seed=0, created=1792300000.0, b=100)
    assert schema_errors(release, "results.v1.schema.json") == []
    rows = {r["model"]: r for r in build.rows(release, {"fight_act1_windmill": "fights"})["fights"]}
    row = rows["human (owner)"]
    assert (row["k"], row["n"], row["n_invalid"], row["condition"]) == (2, 3, 1, None)
    assert row["cost_per_run"] is None and row["cost_per_success"] is None and row["t_pass_s"] == 840.0
    assert [r["model"] for r in build.rows(release, {"fight_act1_windmill": "fights"})["fights"]] == [
        "human (owner)",
        "opus",
    ]
    group = next(g for g in release["groups"] if g["model"] == "human (owner)")
    assert group["n_runs"] == 4 and group["cost_usd"] == {"median_per_run": None, "per_success": None}
    page = tmp_path / "index.html"
    results.write(release, tmp_path / "results.json")
    assert build.main([str(tmp_path / "results.json"), str(page)]) == 0
    assert "human (owner)" in page.read_text(encoding="utf-8")
