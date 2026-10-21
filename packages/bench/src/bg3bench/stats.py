"""Statistics for benchmark results: pure Python, no numpy/scipy.

Runs are grouped by scenario: `runs = {"<scenario>": [1.0, 0.0, ...]}` (one score per run, 0/1 for pass/fail).
Every resampling function takes a seeded `random.Random`, so a report is reproducible from its seed.

- `wilson(k, n)`: 95% Wilson score interval for k successes in n runs.
- `pass_hat_k(c, n, k)`: tau-bench pass^k, the unbiased estimate of "all k tries succeed", C(c,k)/C(n,k).
- `pass_at_k(c, n, k)`: "at least one of k tries succeeds", 1 - C(n-c,k)/C(n,k).
- `bootstrap_mean(runs, rng)`: mean over scenarios of the per-scenario mean, with a two-level percentile bootstrap
  (resample scenarios, then runs inside each). Counting the run noise at both levels makes the interval somewhat wide;
  at 3-5 runs per scenario that is the side to err on.
- `paired_diff(a, b, rng)`: model A minus model B over the scenarios both ran, paired by scenario only (runs of two
  models are not pairs), with the same two-level bootstrap and a verdict string.
- Luck covariate (research/arch/game.md T4, decision D9): `luck_z(rolls)` turns a run's hidden d20 rolls into a
  z-score; `adjust_for_luck(runs, luck)` removes the part of each score that the pooled within-scenario slope on
  luck explains, before the bootstrap. The slope is estimated once and held fixed in the bootstrap (approximation).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from math import comb

Z95 = 1.959963984540054

Runs = Mapping[str, Sequence[float]]


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when there are no trials."""
    if not 0 <= k <= max(n, 0):
        raise ValueError(f"need 0 <= k <= n, got k={k} n={n}")
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    den = 1 + z2 / n
    ctr = (p + z2 / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den
    return (max(0.0, ctr - half), min(1.0, ctr + half))


def _check_ckn(c: int, n: int, k: int) -> None:
    if not 1 <= k <= n:
        raise ValueError(f"need 1 <= k <= n, got k={k} n={n}")
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got c={c} n={n}")


def pass_hat_k(c: int, n: int, k: int) -> float:
    """pass^k: unbiased estimate of the chance that k independent tries all succeed (c of n succeeded)."""
    _check_ckn(c, n, k)
    return comb(c, k) / comb(n, k)


def pass_at_k(c: int, n: int, k: int) -> float:
    """pass@k: unbiased estimate of the chance that at least one of k tries succeeds."""
    _check_ckn(c, n, k)
    return 1.0 - comb(n - c, k) / comb(n, k)


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of a non-empty sequence (numpy's default method)."""
    if not xs:
        raise ValueError("quantile of an empty sequence")
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _check_runs(runs: Runs) -> list[str]:
    if not runs:
        raise ValueError("no scenarios")
    for name, xs in runs.items():
        if not xs:
            raise ValueError(f"scenario {name!r} has no runs")
    return sorted(runs)


def _resampled_mean(xs: Sequence[float], rng: random.Random) -> float:
    return _mean(rng.choices(xs, k=len(xs)))


def _ci(draws: list[float], alpha: float) -> tuple[float, float]:
    return (quantile(draws, alpha / 2), quantile(draws, 1 - alpha / 2))


def bootstrap_mean(runs: Runs, rng: random.Random, b: int = 10000, alpha: float = 0.05) -> tuple[float, float, float]:
    """(estimate, lo, hi): mean of per-scenario means, two-level percentile bootstrap interval."""
    names = _check_runs(runs)
    est = _mean([_mean(runs[s]) for s in names])
    draws = []
    for _ in range(b):
        picked = rng.choices(names, k=len(names))
        draws.append(_mean([_resampled_mean(runs[s], rng) for s in picked]))
    lo, hi = _ci(draws, alpha)
    return (est, lo, hi)


def verdict(lo: float, hi: float) -> str:
    """'a>b' / 'b>a' when the interval of A - B excludes 0, else 'not separable'."""
    if lo > 0:
        return "a>b"
    if hi < 0:
        return "b>a"
    return "not separable"


def paired_diff(
    a: Runs, b: Runs, rng: random.Random, bt: int = 10000, alpha: float = 0.05
) -> tuple[float, float, float]:
    """(estimate, lo, hi) of mean_A - mean_B over the scenarios both have, paired by scenario."""
    shared = sorted(set(a) & set(b))
    if not shared:
        raise ValueError("no shared scenarios")
    _check_runs({s: a[s] for s in shared})
    _check_runs({s: b[s] for s in shared})
    est = _mean([_mean(a[s]) - _mean(b[s]) for s in shared])
    draws = []
    for _ in range(bt):
        picked = rng.choices(shared, k=len(shared))
        draws.append(_mean([_resampled_mean(a[s], rng) - _resampled_mean(b[s], rng) for s in picked]))
    lo, hi = _ci(draws, alpha)
    return (est, lo, hi)


# --- luck covariate ---------------------------------------------------------------------------------------------


def luck_z(rolls: Sequence[int], sides: int = 20) -> float:
    """z-score of the sum of fair `sides`-sided rolls against its expectation; 0.0 for no rolls."""
    if any(not 1 <= r <= sides for r in rolls):
        raise ValueError(f"a roll is outside 1..{sides}")
    n = len(rolls)
    if n == 0:
        return 0.0
    mean = (sides + 1) / 2
    var = (sides * sides - 1) / 12
    return (sum(rolls) - n * mean) / math.sqrt(n * var)


def luck_slope(runs: Runs, luck: Runs) -> float:
    """Pooled within-scenario least-squares slope of score on luck; 0.0 when luck does not vary."""
    names = _check_runs(runs)
    sxy = sxx = 0.0
    for s in names:
        ys, xs = runs[s], luck.get(s, ())
        if len(xs) != len(ys):
            raise ValueError(f"scenario {s!r}: {len(ys)} scores but {len(xs)} luck values")
        my, mx = _mean(ys), _mean(xs)
        sxy += sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sxx += sum((x - mx) ** 2 for x in xs)
    return sxy / sxx if sxx else 0.0


def adjust_for_luck(runs: Runs, luck: Runs, slope: float | None = None) -> dict[str, list[float]]:
    """Scores with the luck effect removed: y - slope * luck (luck is a z-score, so its expectation is 0).

    Pass `slope` to apply one estimated elsewhere (e.g. pooled over every model of a release)."""
    estimated = luck_slope(runs, luck)  # also checks that every score has a luck value
    k = estimated if slope is None else slope
    return {s: [y - k * x for y, x in zip(runs[s], luck[s])] for s in sorted(runs)}


# --- summary ----------------------------------------------------------------------------------------------------


def summarize(
    runs: Mapping[str, Sequence[bool]], rng: random.Random, seed: int, b: int = 10000, alpha: float = 0.05
) -> dict:
    """Per-scenario and aggregate success statistics for one (model, condition).

    `seed` is the seed `rng` was made from; it is recorded so the numbers can be reproduced."""
    names = _check_runs(runs)
    per = {}
    for s in names:
        n, c = len(runs[s]), sum(1 for x in runs[s] if x)
        lo, hi = wilson(c, n)
        per[s] = {
            "n": n,
            "k": c,
            "rate": c / n,
            "wilson": [lo, hi],
            "pass_hat": {str(k): pass_hat_k(c, n, k) for k in range(1, n + 1)},
        }
    est, lo, hi = bootstrap_mean({s: [1.0 if x else 0.0 for x in runs[s]] for s in names}, rng, b, alpha)
    return {
        "schema": 1,
        "seed": seed,
        "b": b,
        "alpha": alpha,
        "per_scenario": per,
        "aggregate": {
            "mean_rate": est,
            "ci": [lo, hi],
            "n_scenarios": len(names),
            "n_runs": sum(len(runs[s]) for s in names),
        },
    }
