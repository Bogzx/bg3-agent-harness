# pyright: basic
"""The suite score and rank ranges: stats.rank_ranges (a bootstrap over whole scenarios, seeded) and the release's
ranking (results.build_results: who is ranked, the common set, choice and maximise scenarios left out)."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

import pytest
from test_manifest_results import assert_valid, naut2_pairs

from bg3bench import results, stats

SCORED = [f"s{i:02}" for i in range(8)]
CHOICE = ["choice_a", "choice_b", "choice_c"]
GOLD = "gold_max"


def test_rank_ranges_separate_a_clear_gap_and_share_a_tie():
    good = {s: [1.0, 1.0, 1.0] for s in SCORED}
    bad = {s: [0.0, 0.0, 0.0] for s in SCORED}
    got = stats.rank_ranges({"a": good, "b": bad}, random.Random(0), b=200)
    assert got == {"a": {"rank": (1, 1), "ci": (1.0, 1.0)}, "b": {"rank": (2, 2), "ci": (0.0, 0.0)}}
    tie = stats.rank_ranges({"a": good, "b": dict(good)}, random.Random(0), b=200)
    assert tie["a"]["rank"] == tie["b"]["rank"] == (1, 1)
    assert stats.rank_ranges({"a": good}, random.Random(0), b=50)["a"]["rank"] == (1, 1)
    assert stats.rank_ranges({}, random.Random(0)) == {}


def test_rank_ranges_overlap_on_close_models_and_are_seeded():
    a = {s: [1.0, 0.0, 1.0] if i % 2 else [0.0, 1.0, 0.0] for i, s in enumerate(SCORED)}
    b = {s: [1.0, 1.0, 0.0] if i % 3 else [0.0, 0.0, 1.0] for i, s in enumerate(SCORED)}
    got = stats.rank_ranges({"a": a, "b": b}, random.Random(7), b=500)
    assert (got["a"]["rank"], got["b"]["rank"]) == ((1, 2), (1, 2))
    lo, hi = got["a"]["ci"]
    assert lo < sum(map(sum, a.values())) / 24 < hi  # the interval holds the estimate
    assert got == stats.rank_ranges({"a": a, "b": b}, random.Random(7), b=500)
    with pytest.raises(ValueError, match="different scenarios"):
        stats.rank_ranges({"a": a, "b": {s: b[s] for s in SCORED[:4]}}, random.Random(0))


def pairs_for(
    model: str, cells: Mapping[str, Sequence[bool | None]], kind: dict[str, str] | None = None, client: str = "pi"
):
    """(manifest, result) pairs of `model` through `client`: per scenario its runs' passed (None: an invalid run);
    kind marks a scenario's runs as choice or maximise."""
    (m0, r0) = naut2_pairs()[0]
    out = []
    for s, runs in cells.items():
        for i, p in enumerate(runs):
            rid = f"{client}-{model}-{s}-{i}"
            man = {**m0, "run_id": rid, "model": {**m0["model"], "slug": model}, "condition": "rules", "judge": None}
            man["client"] = {**m0["client"], "name": client}
            man["scenario"] = {**m0["scenario"], "name": s}
            res = {**r0, "run_id": rid, "valid": p is not None, "passed": bool(p), "t_pass_s": None}
            if p is None:
                res["invalid_reason"] = "operator"
            if (kind or {}).get(s) == "choice":
                res["choices"] = {"picks": [], "fit": None}
            if (kind or {}).get(s) == "maximise":
                res |= {"passed": None, "score": {"fact": "gold", "start": 0, "end": 5, "gain": 5, "peak": 5}}
            out.append((man, res))
    return out


def test_the_release_ranks_on_the_common_pass_fail_set_and_leaves_partial_models_unranked():
    kind = {**dict.fromkeys(CHOICE, "choice"), GOLD: "maximise"}
    full = SCORED + CHOICE + [GOLD]  # the 12 launch scenarios
    sonnet = {s: [True, True, False] for s in full}
    # no choice or gold runs and no valid run of s07: 7 of the 8 pass/fail scenarios, ranked (6 needed); s07 leaves
    # the common set
    haiku: dict[str, list[bool | None]] = {s: [False, False, True] for s in SCORED}
    haiku["s07"] = [None, None, None]
    deepseek = {s: [False, False, False] for s in SCORED}  # 8 of 8: ranked
    opus = {s: [True, True, True] for s in SCORED[:4]}  # 4 of 8: listed, not ranked
    pairs = [
        *pairs_for("sonnet", sonnet, kind),
        *pairs_for("haiku", haiku),
        *pairs_for("deepseek", deepseek),
        *pairs_for("opus", opus),
    ]
    out = results.build_results(pairs, seed=3, created=0.0, b=300)
    assert_valid("results.v1.schema.json", out)
    (rank,) = out["ranking"]
    assert rank == {
        "condition": "rules",
        "scenarios": SCORED[:7],  # no choice, no maximise, not s07 (haiku has no valid run of it)
        "of": 8,
        "min_cells": 6,
        "models": [{"client": "pi", "model": m} for m in ("sonnet", "haiku", "deepseek", "opus")],
    }
    g = {x["model"]: x for x in out["groups"]}
    assert g["sonnet"]["suite_score"] == pytest.approx(2 / 3) and g["haiku"]["suite_score"] == pytest.approx(1 / 3)
    assert g["deepseek"]["suite_score"] == 0.0 and g["sonnet"]["suite_n"] == 7
    assert (g["sonnet"]["rank_range"], g["haiku"]["rank_range"], g["deepseek"]["rank_range"]) == (
        [1, 1],
        [2, 2],
        [3, 3],
    )
    assert all(g[m]["ranked"] for m in ("sonnet", "haiku", "deepseek"))
    assert (g["opus"]["ranked"], g["opus"]["rank_range"], g["opus"]["suite_n"]) == (False, None, 4)
    assert g["opus"]["suite_score"] == 1.0  # over its own cells of the common set: "partial", not a rank
    assert g["opus"]["suite_score_ci"] == [1.0, 1.0] and g["deepseek"]["suite_score_ci"] == [0.0, 0.0]
    lo, hi = g["sonnet"]["suite_score_ci"]
    assert lo < 2 / 3 < hi <= 1.0
    # the per-model pooled stats and per-cell Wilson intervals stay as they were
    assert g["sonnet"]["stats"]["per_scenario"]["s00"]["wilson"] == list(stats.wilson(2, 3))
    assert out == results.build_results(pairs, seed=3, created=0.0, b=300)


def test_a_model_below_the_share_is_unranked_and_scores_only_on_the_common_set():
    pairs = pairs_for("opus", {s: [True] for s in SCORED[:2]}) + pairs_for("haiku", {s: [False] for s in SCORED[2:]})
    out = results.build_results(pairs, seed=0, created=0.0, b=50)
    (rank,) = out["ranking"]
    assert (rank["of"], rank["min_cells"], rank["scenarios"]) == (8, 6, SCORED[2:])  # 6 of 8: ceil(2/3 of 8)
    g = {x["model"]: x for x in out["groups"]}
    assert (g["haiku"]["ranked"], g["haiku"]["rank_range"], g["haiku"]["suite_score"]) == (True, [1, 1], 0.0)
    assert (g["opus"]["ranked"], g["opus"]["suite_score"], g["opus"]["suite_n"]) == (False, None, 0)
    assert g["opus"]["suite_score_ci"] is None
    assert results.build_results([], seed=0, created=0.0, b=10)["ranking"] == []


def test_a_row_is_a_model_through_one_client_and_claude_code_runs_stay_out():
    cells = {s: [True, True, False] for s in SCORED}
    worse = {s: [False, False, True] for s in SCORED}
    pairs = (
        pairs_for("claude-opus-5-5", cells)
        + pairs_for("claude-opus-5-5", worse, client="codex")
        + pairs_for("claude-opus-5-5", cells, client="claude")
    )
    out = results.build_results(pairs, seed=0, created=1.0, b=200)
    assert_valid("results.v1.schema.json", out)
    assert {r["client"] for r in out["runs"]} == {"pi", "codex"} and len(out["runs"]) == 2 * 3 * len(SCORED)
    by = {(g["client"], g["model"]): g for g in out["groups"]}
    assert set(by) == {("pi", "claude-opus-5-5"), ("codex", "claude-opus-5-5")}
    assert by["pi", "claude-opus-5-5"]["n_runs"] == by["codex", "claude-opus-5-5"]["n_runs"] == 3 * len(SCORED)
    assert by["pi", "claude-opus-5-5"]["suite_score"] == pytest.approx(2 / 3)
    assert by["codex", "claude-opus-5-5"]["suite_score"] == pytest.approx(1 / 3)
    (rank,) = out["ranking"]
    assert rank["models"] == [
        {"client": "pi", "model": "claude-opus-5-5"},
        {"client": "codex", "model": "claude-opus-5-5"},
    ]
    assert (
        results.build_results(pairs_for("claude-opus-5-5", cells, client="claude"), seed=0, created=1.0, b=10)["groups"]
        == []
    )
