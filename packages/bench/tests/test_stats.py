# pyright: basic
import random

import pytest

from bg3bench import stats


def approx6(pair):
    return tuple(round(x, 6) for x in pair)


@pytest.mark.parametrize(
    "k,n,want",
    [
        (2, 5, (0.117621, 0.769276)),
        (0, 10, (0.0, 0.277533)),
        (9, 9, (0.700855, 1.0)),
        (7, 20, (0.181192, 0.567146)),
        (1, 3, (0.061492, 0.792340)),
    ],
)
def test_wilson_known_values(k, n, want):
    assert approx6(stats.wilson(k, n)) == want


def test_wilson_no_trials_and_bad_input():
    assert stats.wilson(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        stats.wilson(3, 2)
    with pytest.raises(ValueError):
        stats.wilson(-1, 2)


def test_wilson_matches_scipy():
    sp = pytest.importorskip("scipy.stats")
    for k, n in [(2, 5), (0, 10), (9, 9), (7, 20), (1, 3), (13, 40)]:
        ci = sp.binomtest(k, n).proportion_ci(confidence_level=0.95, method="wilson")
        assert stats.wilson(k, n) == pytest.approx((ci.low, ci.high), abs=1e-9)


def test_pass_hat_k_and_pass_at_k_known_values():
    assert [stats.pass_hat_k(3, 5, k) for k in range(1, 6)] == pytest.approx([0.6, 0.3, 0.1, 0.0, 0.0])
    assert [stats.pass_at_k(3, 5, k) for k in range(1, 6)] == pytest.approx([0.6, 0.9, 1.0, 1.0, 1.0])
    for n in range(1, 7):
        for c in range(n + 1):
            assert stats.pass_hat_k(c, n, 1) == pytest.approx(c / n)
        for k in range(1, n + 1):
            assert stats.pass_hat_k(n, n, k) == 1.0
            assert stats.pass_at_k(0, n, k) == 0.0


@pytest.mark.parametrize("c,n,k", [(3, 5, 6), (3, 5, 0), (6, 5, 1), (-1, 5, 1)])
def test_pass_k_rejects_bad_input(c, n, k):
    with pytest.raises(ValueError):
        stats.pass_hat_k(c, n, k)
    with pytest.raises(ValueError):
        stats.pass_at_k(c, n, k)


def test_quantile_interpolates_like_numpy():
    assert stats.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert stats.quantile([5], 0.9) == 5
    assert stats.quantile([0, 10], 0.25) == 2.5
    with pytest.raises(ValueError):
        stats.quantile([], 0.5)


def test_bootstrap_degenerate_inputs():
    ones = {"a": [1.0] * 3, "b": [1.0] * 5}
    zeros = {"a": [0.0] * 3, "b": [0.0] * 5}
    assert stats.bootstrap_mean(ones, random.Random(0), b=200) == (1.0, 1.0, 1.0)
    assert stats.bootstrap_mean(zeros, random.Random(0), b=200) == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        stats.bootstrap_mean({}, random.Random(0))
    with pytest.raises(ValueError):
        stats.bootstrap_mean({"a": []}, random.Random(0))


def test_bootstrap_is_seeded_and_contains_estimate():
    runs = {"s1": [1, 0, 1, 1, 0], "s2": [0, 0, 1, 0, 0], "s3": [1, 1, 1, 0, 1]}
    one = stats.bootstrap_mean(runs, random.Random(7), b=2000)
    two = stats.bootstrap_mean(runs, random.Random(7), b=2000)
    assert one == two
    est, lo, hi = one
    assert est == pytest.approx((0.6 + 0.2 + 0.8) / 3)
    assert lo < est < hi


def test_bootstrap_estimate_weights_scenarios_equally():
    est, _, _ = stats.bootstrap_mean({"a": [1.0], "b": [0.0] * 9}, random.Random(0), b=10)
    assert est == 0.5


def test_paired_diff_identical_data_straddles_zero():
    est, lo, hi = stats.paired_diff({"s": [1, 0]}, {"s": [1, 0]}, random.Random(0), bt=2000)
    assert est == 0.0
    assert (lo, hi) == (-1.0, 1.0)
    assert stats.verdict(lo, hi) == "not separable"


def test_paired_diff_swap_negates_and_uses_shared_scenarios_only():
    a = {"s1": [1, 1, 1, 0], "s2": [1, 0, 1, 1], "only_a": [0, 0]}
    b = {"s1": [0, 0, 1, 0], "s2": [0, 0, 0, 1], "only_b": [1]}
    est, lo, hi = stats.paired_diff(a, b, random.Random(3), bt=3000)
    assert est == pytest.approx(((0.75 - 0.25) + (0.75 - 0.25)) / 2)
    nest, nlo, nhi = stats.paired_diff(b, a, random.Random(3), bt=3000)
    assert nest == -est
    assert lo <= est <= hi and nlo <= nest <= nhi
    with pytest.raises(ValueError):
        stats.paired_diff({"x": [1]}, {"y": [1]}, random.Random(0))


def test_paired_diff_separates_a_clear_gap():
    a = {f"s{i}": [1, 1, 1, 1, 1] for i in range(10)}
    b = {f"s{i}": [0, 0, 0, 0, 1] for i in range(10)}
    est, lo, hi = stats.paired_diff(a, b, random.Random(1), bt=2000)
    assert est == pytest.approx(0.8)
    assert stats.verdict(lo, hi) == "a>b"
    assert stats.verdict(-hi, -lo) == "b>a"


def test_luck_z():
    assert stats.luck_z([]) == 0.0
    assert stats.luck_z([10, 11]) == 0.0
    assert stats.luck_z([20]) == pytest.approx(9.5 / (399 / 12) ** 0.5)
    assert stats.luck_z([1, 1, 1, 1]) == pytest.approx(-4 * 9.5 / (4 * 399 / 12) ** 0.5)
    assert stats.luck_z([6], sides=6) == pytest.approx(2.5 / (35 / 12) ** 0.5)
    with pytest.raises(ValueError):
        stats.luck_z([21])


def test_luck_slope_and_adjustment_recover_a_linear_effect():
    luck = {"a": [-1.0, 0.0, 1.0], "b": [2.0, -2.0]}
    runs = {"a": [0.5 - 0.2, 0.5, 0.5 + 0.2], "b": [0.1 + 0.4, 0.1 - 0.4]}
    assert stats.luck_slope(runs, luck) == pytest.approx(0.2)
    adj = stats.adjust_for_luck(runs, luck)
    assert adj["a"] == pytest.approx([0.5, 0.5, 0.5])
    assert adj["b"] == pytest.approx([0.1, 0.1])
    assert stats.adjust_for_luck(runs, luck, slope=0.0) == {s: list(runs[s]) for s in sorted(runs)}
    assert stats.luck_slope({"a": [1, 0]}, {"a": [0.0, 0.0]}) == 0.0
    with pytest.raises(ValueError):
        stats.adjust_for_luck({"a": [1, 0]}, {"a": [0.0]}, slope=0.1)


def test_luck_adjusted_interval_is_narrower():
    # Synthetic runs where luck explains about half the score variance (research/arch/game.md T4).
    gen = random.Random(42)
    runs, luck = {}, {}
    for s in range(20):
        base = gen.uniform(0.3, 0.7)
        xs = [gen.gauss(0, 1) for _ in range(5)]
        luck[f"s{s}"] = xs
        runs[f"s{s}"] = [base + 0.15 * x + gen.gauss(0, 0.15) for x in xs]
    _, lo, hi = stats.bootstrap_mean(runs, random.Random(0), b=3000)
    adj = stats.adjust_for_luck(runs, luck)
    _, alo, ahi = stats.bootstrap_mean(adj, random.Random(0), b=3000)
    assert stats.luck_slope(runs, luck) == pytest.approx(0.15, abs=0.05)
    assert ahi - alo < 0.9 * (hi - lo)


def test_summarize_shape_and_values():
    out = stats.summarize({"naut": [True, True, False], "gold": [False, False]}, random.Random(5), seed=5, b=500)
    assert out["schema"] == 1 and out["seed"] == 5 and out["b"] == 500
    naut = out["per_scenario"]["naut"]
    assert (naut["n"], naut["k"]) == (3, 2)
    assert naut["rate"] == pytest.approx(2 / 3)
    assert naut["wilson"] == list(stats.wilson(2, 3))
    assert naut["pass_hat"] == pytest.approx({"1": 2 / 3, "2": 1 / 3, "3": 0.0})
    agg = out["aggregate"]
    assert agg["mean_rate"] == pytest.approx(1 / 3)
    assert (agg["n_scenarios"], agg["n_runs"]) == (2, 5)
    assert agg["ci"][0] <= agg["mean_rate"] <= agg["ci"][1]
    assert out == stats.summarize(
        {"naut": [True, True, False], "gold": [False, False]}, random.Random(5), seed=5, b=500
    )
