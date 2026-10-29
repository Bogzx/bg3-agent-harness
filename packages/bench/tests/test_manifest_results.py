# pyright: basic
import json
import tomllib
from pathlib import Path

import pytest

from bg3bench import facts, graders, manifest, methods, results, stats, suite

HERE = Path(__file__).resolve().parent
SCHEMAS = HERE.parent / "schema"
NAUT2 = HERE / "fixtures" / "naut2"
SHA = "ab" * 32
NOW = suite.suite_version()  # the installed bench's suite: what its manifests record


def load_series():
    series = json.loads((NAUT2 / "series.json").read_text())
    records = [json.loads(line) for line in (NAUT2 / "runs.jsonl").read_text().splitlines()]
    return series, records


def validator(name):
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(
        json.loads((SCHEMAS / name).read_text()), format_checker=jsonschema.FormatChecker()
    )


def assert_valid(name, data):
    errors = list(validator(name).iter_errors(data))
    assert not errors, f"{name}: {errors[0].json_path}: {errors[0].message}"


def naut2_pairs():
    """naut2's runs as pi runs: its series played through Claude Code, whose runs never enter a release
    (results.PILOT_CLIENTS)."""
    series, records = load_series()
    pairs = []
    for rec in records:
        man = manifest.from_series(series, rec)
        man["client"]["name"] = "pi"
        pairs.append((man, results.from_record(rec, man["run_id"])))
    return pairs


def test_manifest_from_the_naut2_series(tmp_path):
    series, records = load_series()
    man = manifest.from_series(series, records[0], bench_tag="bench-v0.1", condition="rules")
    assert man["run_id"] == "naut2-r001" and (man["seed"], man["scenario"]["timing"]) == (None, None)
    assert man["created"] == 1792169546.4
    assert man["scenario"] == {
        "name": "nautiloid",
        "digest": "f4db191ceaad1415",
        "start_save_sha1": None,
        "timing": None,
    }
    assert man["client"] == {"name": "claude", "version": None}
    assert man["model"] == {"slug": "opus", "provider_requested": None, "reasoning_effort": "medium", "served": None}
    assert man["caps"] == {"run_hours": 4, "max_cost": None, "max_tokens": None}
    assert man["pins"]["game_build"] == "25605617"
    assert (man["prompt"], man["tools"], man["price_sheet"], man["sandbox"]) == (None, None, None, "none")
    assert_valid("manifest.v1.schema.json", man)
    manifest.write(man, tmp_path / "manifest.json")
    assert manifest.load(tmp_path / "manifest.json") == man


def test_manifest_with_the_fields_older_series_lack():
    series, records = load_series()
    man = manifest.from_series(
        series,
        records[1],
        prompt={"sha256": SHA, "parts": [{"file": "rules/core.md", "sha256": SHA}]},
        tools={"allowlist_sha256": SHA, "count": 41},
        price_sheet={"date": "2026-10-21", "usd_per_mtok": {"in": 3.0}},
        provider="anthropic",
        client_version="1.3.0",
        grader_version="2",
        seed=1729,
        timing="realtime",
        harness_version="0.1.0",
        bench_version="0.1.0",
    )
    assert man["run_id"] == "naut2-r002" and man["grader"] == {"version": "2"}
    assert man["versions"] == {"harness": "0.1.0", "bench": "0.1.0", "suite": "bench-v0.1"}
    assert (man["seed"], man["scenario"]["timing"]) == (1729, "realtime")
    assert_valid("manifest.v1.schema.json", man)
    v = validator("manifest.v1.schema.json")
    assert not v.is_valid({**man, "prompt": {"sha256": "short", "parts": []}})
    assert not v.is_valid({**man, "price_sheet": {"date": "yesterday", "usd_per_mtok": {}}})
    assert not v.is_valid({**man, "extra": 1})
    assert not v.is_valid({**man, "seed": -1})
    assert not v.is_valid({k: x for k, x in man.items() if k != "versions"})
    assert not v.is_valid({**man, "versions": {"harness": "0.1.0", "bench": None}})
    assert not v.is_valid({**man, "scenario": {**man["scenario"], "timing": "paused"}})


def test_manifest_cites_the_installed_bench_version_by_default():
    series, records = load_series()
    man = manifest.from_series(series, records[0])
    assert man["versions"] == {
        "harness": None,
        "bench": manifest.installed_version(),
        "suite": suite.suite_version(manifest.installed_version()),
    }
    assert (
        manifest.installed_version()
        == tomllib.loads((HERE.parent / "pyproject.toml").read_text())["project"]["version"]
    )


def test_manifest_takes_the_versions_the_run_recorded():
    """A record's pins (pins.version_pins) carry the versions it played with; arguments override them."""
    series, records = load_series()
    rec = {**records[0], "pins": {**(records[0].get("pins") or {}), "versions": {"harness": "0.2.1", "bench": "0.3.4"}}}
    man = manifest.from_series(series, rec)
    assert man["versions"] == {"harness": "0.2.1", "bench": "0.3.4", "suite": "bench-v0.3"}
    assert_valid("manifest.v1.schema.json", man)
    man = manifest.from_series(series, rec, harness_version="0.5.0", bench_version="1.0.2")
    assert man["versions"] == {"harness": "0.5.0", "bench": "1.0.2", "suite": "bench-v1.0"}


def test_manifest_rejects_mismatched_inputs(tmp_path):
    series, records = load_series()
    with pytest.raises(ValueError, match="series"):
        manifest.from_series(series, {**records[0], "series": "other"})
    with pytest.raises(ValueError, match="scenario"):
        manifest.from_series({**series, "scenario": None}, records[0])
    (tmp_path / "m.json").write_text('{"schema": 2}')
    with pytest.raises(ValueError):
        manifest.load(tmp_path / "m.json")


def test_result_from_a_naut2_record(tmp_path):
    _, records = load_series()
    res = results.from_record(records[0], "naut2-r001")
    assert (res["outcome"], res["valid"], res["invalid_reason"], res["passed"]) == ("reached", True, None, True)
    assert res["t_pass_s"] == 545.9
    assert res["cost_usd"] == 3.349
    assert res["tokens"] == {"input": 186, "output": 15172, "cache_read": 4291143, "cache_write": 80131}
    assert (res["wall_s"], res["play_s"], res["reloads"]) == (600.0, 456.0, 0)
    assert res["deaths"] == {"fights": 2, "downs": 0, "deaths": 0, "wipes": 0}
    assert res["fairness"] == {"leak": 0, "check": 0, "blocks": 0}
    assert res["grade"] is None and res["methods"] is None
    assert_valid("result.v1.schema.json", res)
    results.write(res, tmp_path / "result.json")
    assert results.load(tmp_path / "result.json") == res


def test_result_with_a_grade_and_methods_from_facts():
    _, records = load_series()
    tl = facts.load(HERE / "fixtures" / "timelines" / "naut2_S001.facts.jsonl")
    grade = graders.grade({"milestone": "nautiloid_escaped"}, tl, clock="wall")
    res = results.from_record(records[0], "naut2-r001", grade=grade, methods=methods.attribute(tl))
    assert res["passed"] and res["t_pass_s"] == pytest.approx(545.9, abs=0.1)
    assert res["grade"]["curve"]["fact"] == "gold" and res["grade"]["violations"] == []
    assert res["methods"]["kills"] == 10  # 9 kills + the story kill in the trimmed fixture
    assert_valid("result.v1.schema.json", res)
    failed = results.from_record(records[0], "x", grade={**grade, "passed": False, "t_pass_s": None})
    assert (failed["passed"], failed["t_pass_s"]) == (False, None)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"outcome": "runner_error"}, "runner_error"),
        ({"harness_failures": 1}, "harness_failure"),
        ({"operator": [{"t": 1, "what": "reloaded by hand"}]}, "operator"),
        # bench B3: the main menu's Continue loaded another run's save (run_host.SaveWatch); a pass there scores nothing
        ({"outcome": "reached", "foreign_save": "the game loaded a save the harness didn't"}, "foreign_save"),
        ({"outcome": "gave_up", "harness_failures": 1, "foreign_save": "load_game loaded S001-x"}, "foreign_save"),
    ],
)
def test_invalid_runs(change, reason):
    _, records = load_series()
    res = results.from_record({**records[0], **change}, "r")
    assert (res["valid"], res["invalid_reason"]) == (False, reason)
    assert_valid("result.v1.schema.json", res)


def test_timeout_is_a_valid_failure_and_unknown_outcomes_raise():
    _, records = load_series()
    res = results.from_record({**records[0], "outcome": "timeout", "time_to_milestone_s": None}, "r")
    assert (res["valid"], res["passed"], res["t_pass_s"]) == (True, False, None)
    with pytest.raises(ValueError):
        results.from_record({**records[0], "outcome": "won"}, "r")


def test_release_results_round_trip(tmp_path):
    pairs = naut2_pairs()
    out = results.build_results(pairs, seed=0, created=1792300000.0, bench_tag=NOW, b=500)
    assert_valid("results.v1.schema.json", out)
    assert [r["run_id"] for r in out["runs"]] == ["naut2-r001", "naut2-r002", "naut2-r003"]
    assert {tuple(r["versions"].items()) for r in out["runs"]} == {
        (("harness", None), ("bench", manifest.installed_version()), ("suite", NOW))
    }
    assert out["suite"] == NOW
    assert out["runs"][0]["tokens"] == {"input": 186, "output": 15172, "cache_read": 4291143, "cache_write": 80131}
    (group,) = out["groups"]
    assert (group["model"], group["condition"], group["n_runs"], group["n_invalid"]) == ("opus", None, 3, 0)
    naut = group["stats"]["per_scenario"]["nautiloid"]
    assert (naut["n"], naut["k"], naut["rate"]) == (3, 3, 1.0)
    assert naut["wilson"] == list(stats.wilson(3, 3))
    assert group["stats"]["aggregate"]["mean_rate"] == 1.0
    assert group["cost_usd"]["median_per_run"] == 3.3406
    assert group["cost_usd"]["per_success"] == pytest.approx((3.349 + 3.3406 + 3.2074) / 3)
    assert group["wall_s"]["median"] == 630.0
    results.write(out, tmp_path / "results.json")
    assert results.load(tmp_path / "results.json") == out
    assert out == results.build_results(pairs, seed=0, created=1792300000.0, bench_tag=NOW, b=500)


def test_release_is_of_one_suite_version_and_lists_harness_versions():
    pairs = naut2_pairs()
    (m1, r1), (m2, r2), (m3, r3) = pairs
    old = {**m1, "versions": {"harness": None, "bench": m1["versions"]["bench"]}}  # a manifest before the suite field
    mixed = [(old, r1), ({**m2, "versions": {**m2["versions"], "harness": "0.2.0"}}, r2), (m3, r3)]
    out = results.build_results(mixed, seed=0, created=1.0, bench_tag=NOW, b=20)
    assert_valid("results.v1.schema.json", out)
    assert (out["suite"], out["runs"][0]["versions"]["suite"]) == (NOW, NOW)
    assert out["groups"][0]["harness"] == ["0.2.0"]
    newer = {**m3, "versions": {"harness": "0.2.0", "bench": "9.9.0", "suite": "bench-v9.9"}}
    with pytest.raises(ValueError, match="2 suite versions"):
        results.build_results([(m1, r1), (newer, r3)], seed=0, created=1.0, b=20)
    with pytest.raises(ValueError, match=f"release bench-v9.9 holds runs of {NOW}"):
        results.build_results(pairs, seed=0, created=1.0, bench_tag="bench-v9.9", b=20)
    assert results.build_results([], seed=0, created=1.0, b=20)["suite"] is None


def test_release_refuses_a_cell_whose_runs_differ_in_game_pins():
    pairs = naut2_pairs()
    r3 = pairs[2][1]
    pinned = {"game_build": "20981537", "bg3le": {"sha256": "a" * 64}, "mod": {"sha1": "b" * 40}}
    same = [({**m, "pins": {**m.get("pins", {}), **pinned}}, r) for m, r in pairs]
    assert results.build_results(same, seed=0, created=1.0, b=20)["groups"][0]["n_runs"] == 3
    rebuilt = {**same[2][0], "pins": {**same[2][0]["pins"], "mod": {"sha1": "c" * 40}}}
    with pytest.raises(ValueError, match=r"different game pins: nautiloid/pi/opus/None \(mod_sha1\)"):
        results.build_results([*same[:2], (rebuilt, r3)], seed=0, created=1.0, b=20)
    # another cell may play another setup: only the runs of one cell are compared
    other = {**rebuilt, "model": {**rebuilt["model"], "slug": "deepseek"}}
    assert len(results.build_results([*same[:2], (other, r3)], seed=0, created=1.0, b=20)["groups"]) == 2


def test_release_groups_invalid_runs_and_missing_costs():
    pairs = naut2_pairs()
    (m1, r1), (m2, r2), (m3, r3) = pairs
    other = {**m3, "model": {**m3["model"], "slug": "deepseek"}, "condition": "rules"}
    pairs = [
        (m1, {**r1, "valid": False, "invalid_reason": "operator"}),
        (m2, {**r2, "cost_usd": None}),
        (other, {**r3, "passed": False, "t_pass_s": None}),
    ]
    out = results.build_results(pairs, seed=1, created=0.0, b=200)
    assert_valid("results.v1.schema.json", out)
    deepseek, opus = out["groups"]
    assert (opus["n_runs"], opus["n_invalid"], opus["invalid_rate"]) == (2, 1, 0.5)
    assert opus["stats"]["aggregate"]["n_runs"] == 1  # the invalid run is not scored
    assert opus["cost_usd"] == {"median_per_run": None, "per_success": None}
    assert deepseek["condition"] == "rules"
    assert deepseek["cost_usd"]["per_success"] is None and deepseek["stats"]["aggregate"]["mean_rate"] == 0.0
    only_invalid = results.build_results(
        [(m1, {**r1, "valid": False, "invalid_reason": "operator"})], seed=0, created=0.0, b=10
    )
    assert only_invalid["groups"][0]["stats"] is None
    assert only_invalid["groups"][0]["wall_s"] == {"median": None}
    assert_valid("results.v1.schema.json", only_invalid)


def test_release_rejects_bad_pairs(tmp_path):
    (m1, r1), (_, r2), _ = naut2_pairs()
    with pytest.raises(ValueError, match="paired"):
        results.build_results([(m1, r2)], seed=0, created=0.0)
    with pytest.raises(ValueError, match="twice"):
        results.build_results([(m1, r1), (m1, r1)], seed=0, created=0.0, b=10)
    (tmp_path / "r.json").write_text('{"schema": 9}')
    with pytest.raises(ValueError):
        results.load(tmp_path / "r.json")


def test_thinking_time_timing_and_luck_reach_the_release():
    series, records = load_series()
    luck = {"n": 12, "z": -1.25, "party": 9, "other": 3, "skipped": 2, "nat20": 1, "nat1": 2}
    pairs = []
    for i, rec in enumerate(records):
        man = manifest.from_series(series, rec, seed=1729, timing="realtime")
        man["client"]["name"] = "pi"
        rec = {**rec, "model_time": {"model_s": 100.0 + i, "wall_s": 600.0}}
        res = results.from_record(rec, man["run_id"], luck=luck if i == 0 else None)
        assert_valid("result.v1.schema.json", res)
        pairs.append((man, res))
    assert (pairs[0][1]["think_s"], pairs[0][1]["luck"], pairs[1][1]["luck"]) == (100.0, luck, None)
    assert results.from_record(records[0], "r")["think_s"] is None  # older records have no model_time
    out = results.build_results(pairs, seed=0, created=0.0, b=50)
    assert_valid("results.v1.schema.json", out)
    assert [(r["think_s"], r["timing"], r["luck_z"]) for r in out["runs"]] == [
        (100.0, "realtime", -1.25),
        (101.0, "realtime", None),
        (102.0, "realtime", None),
    ]
    assert out["groups"][0]["think_s"] == {"median": 101.0}
    assert results.build_results(naut2_pairs(), seed=0, created=0.0, b=10)["groups"][0]["think_s"] == {"median": None}


def test_manifest_names_the_judge():
    series, records = load_series()
    rec = {**records[0], "persona": {"judge_model": "deepseek-flash"}}
    assert manifest.from_series(series, rec)["judge"] == {"model": "deepseek-flash"}
    assert manifest.from_series({**series, "judge_model": "j"}, records[0])["judge"] == {"model": "j"}
    man = manifest.from_series({**series, "judge_model": None}, {**records[0], "persona": None})
    assert man["judge"] is None
    assert_valid("manifest.v1.schema.json", man)


def test_release_refuses_a_judge_that_is_a_contestant():
    pairs = naut2_pairs()  # the contestant is opus
    judged = [({**m, "judge": {"model": "claude-sonnet-5-5"}}, r) for m, r in pairs]
    assert results.build_results(judged, seed=0, created=0.0, b=10)["runs"]
    clash = [({**m, "judge": {"model": "claude-opus-5-5"}}, r) for m, r in pairs]
    with pytest.raises(ValueError, match="its judge claude-opus-5-5 is a contestant"):
        results.build_results(clash, seed=0, created=0.0, b=10)
    # a held-out scenario's run is not in the release, so its judge is not checked
    assert results.build_results(clash, seed=0, created=0.0, b=10, holdout={"nautiloid"})["runs"] == []


SCORE = {"fact": "gold", "start": 133, "end": 650, "gain": 517, "peak": 900}
NO_PASS = {
    "passed": None,
    "t_pass_s": None,
    "violations": [],
    "curve": {"fact": "gold", "points": []},
    "progress": None,
}


def test_a_maximise_result_has_no_pass_and_carries_its_score():
    _, records = load_series()
    res = results.from_record({**records[0], "outcome": "timeout"}, "r", grade=NO_PASS, score=SCORE)
    assert (res["passed"], res["t_pass_s"], res["score"]) == (None, None, SCORE)
    assert_valid("result.v1.schema.json", res)
    assert "score" not in results.from_record(records[0], "r")


def test_a_result_carries_the_agents_first_end_of_turn():
    _, records = load_series()
    res = results.from_record({**records[0], "first_stop_s": 1254.3}, "r")
    assert res["first_stop_s"] == 1254.3
    assert_valid("result.v1.schema.json", res)
    assert results.from_record(records[0], "r")["first_stop_s"] is None  # never ended its turn, or an older record


def test_a_release_leaves_maximise_runs_out_of_the_pass_rates_and_lists_their_score():
    (m1, r1), (m2, r2), _ = naut2_pairs()
    gold = {**m2, "scenario": {**m2["scenario"], "name": "act1_gold_max"}}
    out = results.build_results(
        [(m1, r1), (gold, {**r2, "passed": None, "t_pass_s": None, "score": SCORE})], seed=0, created=1.0, b=50
    )
    assert_valid("results.v1.schema.json", out)
    assert [(r["scenario"], r["passed"], r.get("score")) for r in out["runs"]] == [
        ("nautiloid", True, None),
        ("act1_gold_max", None, SCORE),
    ]
    (group,) = out["groups"]
    assert set(group["stats"]["per_scenario"]) == {"nautiloid"} and group["n_runs"] == 2
    only_max = results.build_results([(gold, {**r2, "passed": None, "score": SCORE})], seed=0, created=1.0, b=50)
    assert only_max["groups"][0]["stats"] is None
