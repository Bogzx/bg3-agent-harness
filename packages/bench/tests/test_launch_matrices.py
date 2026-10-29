# pyright: basic
"""The launch campaign's matrix files (packages/bench/matrices/launch-v1-*.toml): the scenario set, the per-run caps,
that the DeepSeek one and the pilot plan as written, and that the Claude campaign waits for its profiles to be
verified (the pilot)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from bg3harness import profiles

from bg3bench import matrix_cli, scenario

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
    sets = {m: set(raw(m)["scenarios"]) for m in ("haiku", "sonnet", "opus", "deepseek")}
    assert sets["haiku"] == sets["sonnet"] == SCORED | UNSCORED
    assert sets["deepseek"] == SCORED and len(sets["opus"]) == 4 and sets["opus"] <= SCORED
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
    assert {raw(m)["budget"]["file"] for m in ("haiku", "sonnet", "opus")} == {"runtime/budget-anthropic.json"}


def test_the_deepseek_matrix_plans_24_jobs_repeat_major():
    m = matrix_cli.load(MATRICES / "launch-v1-deepseek.toml", REPO)
    assert not m["skipped"] and len(m["jobs"]) == 24
    assert [j["repeat"] for j in m["jobs"]] == sorted(j["repeat"] for j in m["jobs"])
    assert {j["client"] for j in m["jobs"]} == {"pi"} and all(j["max_cost"] for j in m["jobs"])


def test_the_pilot_plans_one_run_per_claude_model_on_the_anthropic_api():
    m = matrix_cli.load(MATRICES / "launch-v1-pilot.toml", REPO)
    assert not m["skipped"] and len(m["jobs"]) == 3
    assert {j["client"] for j in m["jobs"]} == {"pi"} and {j["max_cost"] for j in m["jobs"]} == {20.98}
    for name in ("haiku", "sonnet", "opus"):
        p = profiles.load(REPO / "profiles" / f"claude-{name}-5-5.toml")
        assert p.endpoint is not None
        assert (p.model, p.endpoint.api, p.status) == (f"claude-{name}-5-5", "anthropic-messages", "offline")


@pytest.mark.parametrize(("name", "jobs"), [("haiku", 36), ("sonnet", 36), ("opus", 12)])
def test_the_claude_campaign_waits_for_verified_profiles(tmp_path, name, jobs):
    with pytest.raises(matrix_cli.MatrixFileError, match="status 'offline'"):
        matrix_cli.load(MATRICES / f"launch-v1-{name}.toml", REPO)
    verified = tmp_path / f"launch-v1-{name}.toml"
    text = (MATRICES / verified.name).read_text().replace("allow_unverified = false", "allow_unverified = true")
    verified.write_text(text)
    m = matrix_cli.load(verified, REPO)
    assert not m["skipped"] and len(m["jobs"]) == jobs and all(j["max_cost"] for j in m["jobs"])
