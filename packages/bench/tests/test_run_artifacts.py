# pyright: basic
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


def readout(seed: int | None) -> dict:
    """A run record's settings readout (bg3harness ChecksSide.game_setup) after the mod applied `seed`; {} unseeded."""
    if seed is None:
        return {}
    return {"game_settings": {"seed": {"seed": seed, "applied": {"seed": seed, "base_seed": seed, "streams": 6}}}}


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
    assert (man["scenario"]["timing"], man["seed"], man["autoskip"]) == ("realtime", None, True)
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
    rec = records[0] | readout(7)
    man, res = run_artifacts.write_run_artifacts(tmp_path, series, rec, None, None, scenario_spec=sandbox)
    assert man["seed"] == 7 and res["methods"] is not None and res["luck"]["n"] == 1
    no_skip = {**sandbox, "game_settings": {"seed": 7, "autoskip": False}}
    man, _ = run_artifacts.write_run_artifacts(tmp_path, series, rec, None, None, scenario_spec=no_skip)
    assert man["autoskip"] is False  # the run's wall time had the cutscenes in it
    assert_valid("manifest.v1.schema.json", man)


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


@pytest.mark.parametrize(
    "game_settings",
    [
        None,  # no readout: the runner never seeded
        {"seed": {"seed": 7, "error": "the server context runs once a save is loaded"}},  # live7's nautiloid
        {"seed": {"seed": 7, "applied": {"seed": 8}}},  # another seed
    ],
)
def test_a_seeded_scenario_whose_seed_didnt_apply_is_invalid(tmp_path, game_settings):
    series, records = load_series()
    write_facts(tmp_path)
    spec = {**series["scenario"]["spec"], "schema": 2, "family": "exploration", "game_settings": {"seed": 7}}
    rec = {**records[0], "game_settings": game_settings}
    man, res = run_artifacts.write_run_artifacts(tmp_path, series, rec, None, None, scenario_spec=spec)
    assert (res["valid"], res["invalid_reason"], res["passed"]) == (False, "unseeded", True)  # graded, not scored
    assert man["seed"] == (8 if game_settings and "applied" in game_settings["seed"] else None)  # what the game had
    assert_valid("result.v1.schema.json", res)
    assert_valid("manifest.v1.schema.json", man)
    unseeded_scenario = {**spec, "game_settings": {}}
    _, res = run_artifacts.write_run_artifacts(tmp_path, series, rec, None, None, scenario_spec=unseeded_scenario)
    assert res["valid"] is True  # a scenario that fixes no seed needs none


def test_a_maximise_scenario_is_scored_by_its_gain_with_no_pass(tmp_path):
    series, records = load_series()
    (tmp_path / "facts.jsonl").write_text((TL / "gold_spend.facts.jsonl").read_text())
    base = {k: v for k, v in series["scenario"]["spec"].items() if k != "until"}
    spec = base | {"schema": 2, "family": "sandbox", "maximise": {"fact": "gold"}}
    rec = {**records[0], "outcome": "timeout"}  # a maximise run ends at its time cap
    _, res = run_artifacts.write_run_artifacts(tmp_path, series, rec, None, None, scenario_spec=spec)
    assert (res["passed"], res["t_pass_s"]) == (None, None)
    assert res["score"] == {"fact": "gold", "start": 50, "end": 1100, "gain": 1050, "peak": 1100}
    assert res["grade"]["curve"]["fact"] == "gold" and res["methods"] is not None
    assert_valid("result.v1.schema.json", res)
    assert report.verdict(res) == "+1050 gold (50 -> 1100)"


def test_result_deaths_are_the_party_losses_in_the_facts(tmp_path):
    # diverse-2026-10-25 Haiku x fight_act2_balthazar: the bench record has no deaths, result.json said 0 of each
    series, records = load_series()
    (tmp_path / "facts.jsonl").write_text((TL / "balthazar_haiku_wipe.facts.jsonl").read_text())
    _, res = run_artifacts.write_run_artifacts(tmp_path, series, records[0], None, None)
    # Tav down then dead, Wyll down, revived, down, dead, Shadowheart down then dead, Karlach down: all down at once
    assert res["deaths"] == {"fights": 1, "downs": 5, "deaths": 3, "wipes": 1}
    (tmp_path / "none").mkdir()
    _, res = run_artifacts.write_run_artifacts(tmp_path / "none", series, records[0], None, None)
    assert res["deaths"] == {"fights": 2, "downs": 0, "deaths": 0, "wipes": 0} == records[0]["deaths"]  # the record's


def test_party_losses_count_a_death_only_a_snapshot_saw_and_no_summons():
    snap = {"v": 1, "kind": "snapshot", "gold": 0, "inv": [], "region": None, "areas": [], "quests": {}, "combat": True}
    tl = [
        snap | {"t": 1.0, "party": [{"name": "Wyll"}, {"name": "Tav"}, {"name": "Scratch", "summon": True}]},
        {"v": 1, "kind": "event", "t": 2.0, "ev": "downed", "name": "Wyll", "party": True},
        {"v": 1, "kind": "event", "t": 2.5, "ev": "died", "name": "Scratch", "party": True, "summon": True},
        # DeepSeek x Balthazar: Wyll dead in a snapshot, no died event; the summon down does not wipe the party
        snap
        | {"t": 3.0, "party": [{"name": "Wyll", "dead": True}, {"name": "Tav"}, {"name": "Scratch", "summon": True}]},
        snap | {"t": 4.0, "party": [{"name": "Wyll", "dead": True}, {"name": "Tav", "downed": True}]},
        {"v": 1, "kind": "event", "t": 5.0, "ev": "revived", "name": "Tav", "party": True},
        {"v": 1, "kind": "event", "t": 6.0, "ev": "downed", "name": "Tav", "party": True},
    ]
    assert facts.party_losses(tl) == {"fights": 1, "downs": 3, "deaths": 1, "wipes": 2}
