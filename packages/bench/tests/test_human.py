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
    name="fight_act1_windmill",
    n=1,
    *,
    passed: object = True,
    time_s=900.0,
    t_pass_s: float | None = 840.0,
    reloads=0,
    **kw,
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
        ({**results_file(), "game_version": 4}, "game_version must be"),
        ({**results_file(), "attempts": {}}, "attempts must be a list"),
        ([], "not an object"),
        (results_file("windmill"), "attempts[0]: not an object"),
        (results_file(attempt(n=0)), "attempt must be an integer"),
        (results_file(attempt(passed="yes")), "passed must be true or false"),
        (results_file(attempt(time_s=-1)), "time_s must be"),
        (results_file(attempt("choice_act1_karlach", picks=["a"])), "list of {scene, pick}"),
        (results_file(attempt(notes=3)), "notes must be a string"),
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


def test_load_suite_reads_the_package_scenarios():
    sc = human.load_suite()
    assert "nautiloid" in sc and "fight_act1_windmill" in sc
    assert sc["fight_act1_windmill"]["family"] == "fight"


def test_load_suite_empty_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no scenario files"):
        human.load_suite(tmp_path)


def test_report_ok_and_errors():
    data = results_file(attempt(), attempt(n=2, passed=False))
    assert human.report(data, SCENARIOS) == {"ok": True, "errors": [], "player": "owner", "attempts": 2}
    bad = human.report(results_file(attempt(), attempt(n=3)), SCENARIOS)
    assert bad["ok"] is False and bad["attempts"] == 2
    assert any("numbered 1..2" in e for e in bad["errors"])
    assert human.report([], SCENARIOS) == {"ok": False, "errors": ["not an object"], "player": None, "attempts": None}


def test_check_cli_ok(tmp_path, capsys):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(results_file(attempt(), attempt(n=2, passed=False))))
    assert human.main(["check", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "ok: owner, 2 attempts"


def test_check_cli_one_attempt_is_singular(tmp_path, capsys):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(results_file(attempt())))
    assert human.main(["check", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "ok: owner, 1 attempt"


def test_check_cli_refuses(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(results_file(attempt(), attempt(n=3))))
    assert human.main(["check", str(path)]) == 2
    err = capsys.readouterr().err
    assert "refusing:" in err and "numbered 1..2" in err


def test_check_cli_json(tmp_path, capsys):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(results_file(attempt())))
    assert human.main(["check", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": [], "player": "owner", "attempts": 1}
    path.write_text(json.dumps(results_file(attempt(date="23.10.2026"))))
    assert human.main(["check", str(path), "--json"]) == 2
    got = json.loads(capsys.readouterr().out)
    assert got["ok"] is False and any("YYYY-MM-DD" in e for e in got["errors"])


def test_check_cli_not_json_utf8_or_missing(tmp_path, capsys):
    missing = tmp_path / "gone.json"
    assert human.main(["check", str(missing)]) == 2
    assert "refusing:" in capsys.readouterr().err
    path = tmp_path / "owner.json"
    path.write_text("{")
    assert human.main(["check", str(path)]) == 2
    assert "not JSON" in capsys.readouterr().err
    path.write_bytes(b"\xff\xfe{}")
    assert human.main(["check", str(path)]) == 2
    assert "not UTF-8" in capsys.readouterr().err


def test_check_cli_scenarios_dir(tmp_path, capsys):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(results_file(attempt())))
    empty = tmp_path / "none"
    empty.mkdir()
    assert human.main(["check", str(path), "--scenarios", str(empty)]) == 2
    assert "no scenario files" in capsys.readouterr().err
    assert human.main(["check", str(path), "--scenarios", str(HERE.parent / "scenarios")]) == 0
    assert capsys.readouterr().out.strip() == "ok: owner, 1 attempt"


def test_the_doc_names_the_check_command():
    assert "python -m bg3bench.human check" in DOC.read_text(encoding="utf-8")


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
