# pyright: basic
"""The launch campaign's matrix files (packages/bench/matrices/launch-v1-*.toml): the scenario set, the per-run caps,
and that every one plans as written: the Claude profiles are verified by the pilot (2026-10-29)."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path

import pytest
from bg3harness import profiles

from bg3bench import matrix_cli, results, scenario

REPO = Path(__file__).resolve().parents[3]
MATRICES = REPO / "packages" / "bench" / "matrices"
SCORED = {
    "crash_site_grove",
    "act1_goblin_camp_no_fight",
    "act1_gold_1000",
    "act1_level_3",
    "fight_act1_grove_gate",
    "fight_act1_windmill",
    "fight_act2_balthazar",
    "fight_act2_moonrise_assault",
}
UNSCORED = {"choice_act1_karlach", "choice_act1_minthara", "choice_act2_nightsong", "act3_gold_max"}


def raw(name: str) -> dict:
    return tomllib.loads((MATRICES / f"launch-v1-{name}.toml").read_text())


def test_the_campaign_covers_every_family_with_saves_and_caps_per_scenario():
    sets = {m: set(raw(m)["scenarios"]) for m in ("haiku", "sonnet", "opus", "deepseek", "deepseek-rest")}
    assert sets["haiku"] == sets["sonnet"] == SCORED | UNSCORED
    assert sets["opus"] == SCORED
    # DeepSeek's $2.80: the fewest scored scenarios a ranked model needs
    assert sets["deepseek"] < SCORED and len(sets["deepseek"]) == math.ceil(results.RANKED_SHARE * len(SCORED))
    # and the rest of the launch list after the top-up, each scenario once
    assert sets["deepseek"] | sets["deepseek-rest"] == SCORED | UNSCORED
    assert not sets["deepseek"] & sets["deepseek-rest"]
    families = set()
    for name in SCORED | UNSCORED:
        sc = scenario.load(MATRICES.parent / "scenarios" / f"{name}.json")
        assert sc["start"]["save"] and not sc.get("holdout") and not scenario.not_ready(sc), name
        families.add(sc["family"])
        assert ("maximise" in sc or sc["family"] == "choice") == (name in UNSCORED), name
    assert families == {"exploration", "sandbox", "fight", "choice"}
    for m, scs in sets.items():
        r = raw(m)
        assert set(r["budget"]["max_cost"]) == scs and r["repeats"] == 3 and r["conditions"] == ["rules"]
        assert r["allow_gaps"] is False and r["allow_unverified"] is False
    for m in ("haiku", "sonnet", "opus"):  # each its own share, all within the shared Anthropic budget
        assert raw(m)["budget"]["file"] == f"runtime/budget-{m}.json"
        assert raw(m)["budget"]["shared"] == ["runtime/budget-anthropic.json"]


@pytest.mark.parametrize("name", ["deepseek", "deepseek-rest"])
def test_the_deepseek_matrix_plans_18_jobs_cell_by_cell_cheapest_reserve_last(name):
    m = matrix_cli.load(MATRICES / f"launch-v1-{name}.toml", REPO)
    assert not m["skipped"] and len(m["jobs"]) == 18
    assert [(j["scenario"], j["repeat"]) for j in m["jobs"]] == [
        (s, r) for s in raw(name)["scenarios"] for r in (1, 2, 3)
    ]
    assert {j["client"] for j in m["jobs"]} == {"pi"} and all(j["max_cost"] for j in m["jobs"])
    assert m["budget"] == REPO / "runtime" / "budget-deepseek.json" and m["shared"] == []
    reserves = [j["max_cost"] for j in m["jobs"]]
    assert reserves == sorted(reserves, reverse=True)  # a job starts only if spent + its reserve fits the cap
    for j in m["jobs"]:  # the time cap x $0.0123 per minute x 1.25
        sc = scenario.load(MATRICES.parent / "scenarios" / f"{j['scenario']}.json")
        assert j["max_cost"] == round(sc["caps"]["run_hours"] * 60 * 0.0123 * 1.25, 2), j["id"]


def test_both_deepseek_matrices_play_one_results_row():
    # results.build_results groups by (client, model, condition), so the two matrices' runs make one row
    played = {
        (j["client"], j["model"], j["effort"], j["condition"], j["profile"])
        for name in ("deepseek", "deepseek-rest")
        for j in matrix_cli.load(MATRICES / f"launch-v1-{name}.toml", REPO)["jobs"]
    }
    assert played == {("pi", "deepseek-flash", "medium", "rules", str(REPO / "profiles" / "deepseek-flash.toml"))}


def test_the_pilot_plans_one_run_per_claude_model_on_the_anthropic_api():
    m = matrix_cli.load(MATRICES / "launch-v1-pilot.toml", REPO)
    assert not m["skipped"] and len(m["jobs"]) == 3
    assert {j["client"] for j in m["jobs"]} == {"pi"} and {j["max_cost"] for j in m["jobs"]} == {20.98}
    for name in ("haiku", "sonnet", "opus"):
        p = profiles.load(REPO / "profiles" / f"claude-{name}-5-5.toml")
        assert p.endpoint is not None
        assert (p.model, p.endpoint.api, p.status) == (f"claude-{name}-5-5", "anthropic-messages", "verified")


@pytest.mark.parametrize(("name", "jobs"), [("haiku", 36), ("sonnet", 36), ("opus", 24)])
def test_the_claude_campaign_plans_on_its_verified_profiles(name, jobs):
    m = matrix_cli.load(MATRICES / f"launch-v1-{name}.toml", REPO)  # allow_unverified = false
    assert not m["skipped"] and len(m["jobs"]) == jobs and all(j["max_cost"] for j in m["jobs"])
    assert {j["client"] for j in m["jobs"]} == {"pi"} and {j["model"] for j in m["jobs"]} == {f"claude-{name}-5-5"}
