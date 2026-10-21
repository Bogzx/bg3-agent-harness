import json
from pathlib import Path

import pytest

from bg3bench import facts, manifest, report, results, run_artifacts, scenario

HERE = Path(__file__).resolve().parent
NAUT2 = HERE / "fixtures" / "naut2"
TL = HERE / "fixtures" / "timelines"
SCHEMAS = HERE.parent / "schema"
SHA = "cd" * 32


def load_series():
    series = json.loads((NAUT2 / "series.json").read_text())
    records = [json.loads(line) for line in (NAUT2 / "runs.jsonl").read_text().splitlines()]
    return series, records


def write_facts(run_dir: Path) -> None:
    """naut2 S001's timeline as the runner writes it now: game_t on every record (seconds since run_start)."""
    tl = facts.load(TL / "naut2_S001.facts.jsonl")
    t0 = tl[0]["t"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "facts.jsonl").write_text("".join(json.dumps({**r, "game_t": round(r["t"] - t0, 1)}) + "\n" for r in tl))


def assert_valid(name, data):
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(json.loads((SCHEMAS / name).read_text()))
    errors = list(v.iter_errors(data))
    assert not errors, f"{name}: {errors[0].json_path}: {errors[0].message}"


def test_grades_facts_and_writes_both_files(tmp_path):
    series, records = load_series()
    run_dir = tmp_path / "sessions" / "naut2"
    write_facts(run_dir)
    parts = [{"file": "play/CLAUDE.md", "sha256": "ef" * 32}]
    man, res = run_artifacts.write_run_artifacts(
        run_dir, series, records[0], "0.1.0", SHA, prompt_parts=parts, condition="legacy"
    )
    assert manifest.load(run_dir / "manifest.json") == man and results.load(run_dir / "result.json") == res
    assert man["run_id"] == "naut2-r001" and man["versions"]["harness"] == "0.1.0"
    assert man["prompt"] == {"sha256": SHA, "parts": parts} and man["condition"] == "legacy"
    assert (man["scenario"]["timing"], man["seed"]) == ("realtime", None)
    assert res["grade"] is not None and res["passed"] is True and res["t_pass_s"] is not None
    assert res["methods"] is None and res["luck"] is None  # exploration: no attribution; no roll log
    assert_valid("manifest.v1.schema.json", man)
    assert_valid("result.v1.schema.json", res)
    assert report.load_runs(tmp_path / "sessions") == [(man, res)]


def test_the_goal_decides_not_the_runner_verdict(tmp_path):
    # the runner says reached, but the facts never meet the goal: the grade fails the run
    series, records = load_series()
    run_dir = tmp_path / "r"
    write_facts(run_dir)
    spec = {k: v for k, v in series["scenario"]["spec"].items() if k != "until"} | {"goal": {"gold": {"gte": 10**6}}}
    _, res = run_artifacts.write_run_artifacts(run_dir, series, records[0], None, None, scenario_spec=spec)
    assert res["outcome"] == "reached" and res["passed"] is False and res["t_pass_s"] is None


def test_without_facts_the_milestone_verdict_and_a_sandbox_gets_methods_and_luck(tmp_path):
    series, records = load_series()
    _, res = run_artifacts.write_run_artifacts(tmp_path, series, records[0], None, None)
    assert res["grade"] is None and res["passed"] is True  # the runner's milestone verdict
    write_facts(tmp_path)
    rolls = [{"v": 1, "t": 1.0, "natural": 20, "sides": 20, "party": True}]
    (tmp_path / "rolls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rolls))
    sandbox = {**series["scenario"]["spec"], "schema": 2, "family": "sandbox", "game_settings": {"seed": 7}}
    man, res = run_artifacts.write_run_artifacts(tmp_path, series, records[0], None, None, scenario_spec=sandbox)
    assert man["seed"] == 7 and res["methods"] is not None and res["luck"]["n"] == 1


def test_bad_inputs_raise(tmp_path):
    series, records = load_series()
    (tmp_path / "facts.jsonl").write_text('{"v": 1, "kind": "snapshot"}\n')
    with pytest.raises(facts.FactsError):  # a broken facts file is a harness bug, not a milestone verdict
        run_artifacts.write_run_artifacts(tmp_path, series, records[0], None, None)
    assert not (tmp_path / "result.json").exists()
    with pytest.raises(ValueError, match="no scenario spec"):
        run_artifacts.series_scenario({"series": "x", "scenario": {"name": "x"}})
    with pytest.raises(scenario.ScenarioError):
        run_artifacts.series_scenario({"series": "x", "scenario": {"spec": {"name": "x"}}})
