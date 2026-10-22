"""The runs.jsonl records of a series as tables: the runs table, the series summary, two series compared
(play.py runs; moved from runner/runs.py, which re-exports them).

The record fields read here are the run record's contract: failures[].class is one of bg3harness.runs' failure
classes (it tags them), model_time is bg3harness.model_time.model_time() (HEADLINE, api_floor.p10_s), tokens are the
token counts by kind. The class names, HEADLINE, FLOOR_DIFF_S, token_total and time_to_milestone are imported from
there.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter

from bg3harness.model_time import FLOOR_DIFF_S, HEADLINE
from bg3harness.runs import AGENT, HARNESS, UNKNOWN, time_to_milestone, token_total

# The naut series (3 runs, Claude, before observation compaction): result tokens per run (vis, research/impl-vis.md
# "Result tokens metric"; raw stream backfill). A run is coloured against their mean (total, and per result).
RESULT_TOK_BASELINE = {"naut": 41944, "naut-r2": 38027, "naut-r3": 46255}
RESULT_TOK_BASELINE_N = {"naut": 115, "naut-r2": 111, "naut-r3": 95}  # results counted
RESULT_TOK_BASELINE_TOTAL = round(sum(RESULT_TOK_BASELINE.values()) / len(RESULT_TOK_BASELINE), 1)  # 42075.3
RESULT_TOK_BASELINE_PER_RESULT = round(sum(RESULT_TOK_BASELINE.values()) / sum(RESULT_TOK_BASELINE_N.values()), 1)
# … 393.2 (pooled: all results of the three runs)
RESULT_TOK_BETTER, RESULT_TOK_WORSE = 0.9, 1.1  # ratio to the baseline mean: ≤ better (green), ≥ worse (red)


def record_result_tokens(r: dict) -> dict | None:
    return r.get("result_tokens") if isinstance(r.get("result_tokens"), dict) else None


def result_tok_cell(r: dict) -> str:
    """runs_table's cell: total k (mean per result), "-" when the record has no result_tokens (play.py runs
    --backfill adds them to older records from the streams)."""
    rt = record_result_tokens(r)
    if not rt or rt.get("total") is None:
        return "-"
    return f"{rt['total'] / 1000:.1f}" + (f" ({rt['per_result']:.0f})" if rt.get("per_result") is not None else "")


def result_tok_grade(value: float | None, baseline: float | None) -> str | None:
    """ "better" / "same" / "worse" against the naut baseline (fewer tokens is better); None: nothing to compare."""
    if value is None or not baseline:
        return None
    ratio = value / baseline
    return "better" if ratio <= RESULT_TOK_BETTER else "worse" if ratio >= RESULT_TOK_WORSE else "same"


def class_counts(failures: list[dict]) -> Counter:
    c = Counter()
    for f in failures or []:
        c[f.get("class", UNKNOWN)] += int(f.get("n") or 1)
    return c


def clean_streak(records: list[dict]) -> int:
    """Clean runs in a row at the end of the series (the launch bar: 3)."""
    n = 0
    for r in reversed(records):
        if not r.get("clean"):
            break
        n += 1
    return n


def best_streak(records: list[dict]) -> int:
    best = cur = 0
    for r in records:
        cur = cur + 1 if r.get("clean") else 0
        best = max(best, cur)
    return best


def runs_table(records: list[dict]) -> str:
    """Plain-text table of a series' runs (play.py runs --playthrough <base>)."""
    head = (
        "run",
        "playthrough",
        "outcome",
        "clean",
        "wall min",
        "to M min",
        "sessions",
        "harness",
        "agent",
        "unknown",
        "cost $",
        "tokens M",
        "idle med s",
        "persona",
        "adj model min",
        "API floor s",
        "result tok k (/res)",
        "top failures",
    )
    rows = []
    for r in records:
        c = class_counts(r.get("failures"))
        kinds = Counter()
        for f in r.get("failures") or []:
            kinds[f"{f.get('kind')}({f.get('class', '?')[0]})"] += int(f.get("n") or 1)
        top = ", ".join(f"{k}×{n}" if n > 1 else k for k, n in kinds.most_common(4))
        outcome = r.get("outcome") or ("reached" if r.get("milestone") else "?")
        ttm, idle, adj, floor = record_ttm(r), record_idle(r), record_adjusted_model_s(r), record_api_floor(r)
        outcome += f" ({r['cap']})" if r.get("cap") and outcome != "timeout" else ""
        rows.append(
            (
                str(r.get("run") or 1),
                str(r.get("playthrough") or ""),
                outcome,
                "yes" if r.get("clean") else "no",
                f"{r.get('wall_min', 0):g}",
                "-" if ttm is None else f"{ttm / 60:.1f}",
                str(r.get("sessions", "")),
                str(c[HARNESS]),
                str(c[AGENT]),
                str(c[UNKNOWN]),
                f"{r.get('cost_usd', 0):.2f}",
                f"{token_total(r.get('tokens')) / 1e6:.2f}",
                "-" if idle is None else f"{idle:g}",
                persona_cell(r),
                "-" if adj is None else f"{adj / 60:.1f}",
                "-" if floor is None else f"{floor:.2f}",
                result_tok_cell(r),
                top,
            )
        )
    widths = [max(len(x) for x in col) for col in zip(head, *rows)] if rows else [len(h) for h in head]
    lines = ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in [head, *rows]]
    lines.insert(1, "  ".join("-" * w for w in widths))
    n = clean_streak(records)
    clean = sum(1 for r in records if r.get("clean"))
    lines += [
        "",
        (f"{len(records)} run(s), {clean} clean; {n} clean in a row (latest), best streak {best_streak(records)}"),
    ]
    return "\n".join(lines)


# ======================================================================== the series summary


def record_ttm(r: dict) -> float | None:
    """time_to_milestone_s of a record (derived from reached_at - started for records written before the field)."""
    if "time_to_milestone_s" in r:
        return r["time_to_milestone_s"]
    return time_to_milestone(r.get("started"), r.get("reached_at"))


def record_idle(r: dict) -> float | None:
    return (r.get("latency") or {}).get("idle_median_s")


PERSONA_SHORT = {"judge_failed": "fail", "no_decisions": "n/d", "no_character": "n/c"}


def record_persona(r: dict) -> float | None:
    """The persona judge's 0-10 score of a record (None: not judged, or the judge didn't score it)."""
    p = r.get("persona") or {}
    return p.get("score") if p.get("status") == "ok" else None


def persona_cell(r: dict) -> str:
    p = r.get("persona")
    if not p:
        return "-"
    score = record_persona(r)
    return f"{score:g}" if score is not None else PERSONA_SHORT.get(p.get("status"), str(p.get("status") or "?"))


def record_adjusted_model_s(r: dict) -> float | None:
    """The run's "adj model min" in seconds: model time with output speed and TTFT both at the reference API
    (HEADLINE). adjusted_model_s kept the measured TTFT, so a slower API hour
    read as a regression (naut -> naut2: +18 % there, -13 % here)."""
    return (r.get("model_time") or {}).get(HEADLINE)


def record_api_floor(r: dict) -> float | None:
    """The run's API floor: p10 first-block latency of its messages without thinking (model_time.api_floor)."""
    return ((r.get("model_time") or {}).get("api_floor") or {}).get("p10_s")


def floor_note(floors: list[tuple[str, float]]) -> str | None:
    """ "API floor differs …" when the API floors of runs (or series) are further apart than FLOOR_DIFF_S:
    their raw model-time difference is then (partly) the API's; the TTFT-normalised adj model min compares."""
    if len(floors) < 2:
        return None
    lo, hi = min(floors, key=lambda x: x[1]), max(floors, key=lambda x: x[1])
    if abs(hi[1] - lo[1]) <= FLOOR_DIFF_S + 1e-9:  # 1.3 - 1.0 = 0.30000000000000004
        return None
    return (
        f"API floor differs ({lo[0]} {lo[1]:.2f} s … {hi[0]} {hi[1]:.2f} s per request, > "
        f"{FLOOR_DIFF_S:g} s): raw model time differences are the API's, not a regression; "
        f"compare adj model min (TTFT-normalised)"
    )


def stats(values: list) -> dict | None:
    """{n, min, max, mean, stddev (sample; 0 for one value)} of the non-None values, or None."""
    v = [float(x) for x in values if x is not None]
    if not v:
        return None
    return {
        "n": len(v),
        "min": min(v),
        "max": max(v),
        "mean": statistics.fmean(v),
        "stddev": statistics.stdev(v) if len(v) > 1 else 0.0,
    }


SUMMARY_METRICS = (  # (label, value of a record, format)
    ("wall min", lambda r: r.get("wall_min"), "{:.1f}"),
    ("to milestone min", lambda r: None if record_ttm(r) is None else record_ttm(r) / 60, "{:.1f}"),
    ("cost $", lambda r: r.get("cost_usd"), "{:.2f}"),
    ("tokens M", lambda r: token_total(r.get("tokens")) / 1e6 if r.get("tokens") else None, "{:.2f}"),
    ("idle median s", record_idle, "{:.2f}"),
    ("persona score", record_persona, "{:.1f}"),
    (
        "adj model min",
        lambda r: None if record_adjusted_model_s(r) is None else record_adjusted_model_s(r) / 60,
        "{:.1f}",
    ),
    ("judge $", lambda r: (r.get("persona") or {}).get("cost_usd"), "{:.2f}"),
    (
        "result tok k",
        lambda r: (record_result_tokens(r) or {}).get("total", 0) / 1000 if record_result_tokens(r) else None,
        "{:.1f}",
    ),
    ("tok/result", lambda r: (record_result_tokens(r) or {}).get("per_result"), "{:.0f}"),
    ("API floor s", record_api_floor, "{:.2f}"),
)


def series_summary(records: list[dict]) -> str:
    """The series in numbers (play.py runs): n, reached, and min/max/mean/stddev of wall time, time to the
    milestone (reached runs only), cost, tokens, the idle-gap median, the persona score, the API-adjusted model
    time and the judge's cost (apart from the run's); the pins, caps and judge models when they vary."""
    reached = sum(1 for r in records if (r.get("outcome") or ("reached" if r.get("milestone") else "")) == "reached")
    lines = [f"series: n={len(records)}, reached {reached}/{len(records)}"]
    head = ("metric", "n", "min", "max", "mean", "stddev")
    rows = []
    for label, get, fmt in SUMMARY_METRICS:
        st = stats([get(r) for r in records])
        rows.append(
            (label, "0", "-", "-", "-", "-")
            if st is None
            else (label, str(st["n"]), *(fmt.format(st[k]) for k in ("min", "max", "mean", "stddev")))
        )
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in [head, *rows]]
    lines.insert(2, "  ".join("-" * w for w in widths))
    drift = [f"run {r.get('run') or 1}: " + "; ".join(r["version_drift"]) for r in records if r.get("version_drift")]
    if drift:
        lines += ["version drift (--allow-version-drift): " + " | ".join(drift)]
    caps = {json.dumps(r.get("caps"), sort_keys=True) for r in records if r.get("caps")}
    if len(caps) > 1:
        lines += ["caps differ between runs: " + " | ".join(sorted(caps))]
    graded = [(r.get("run") or 1, record_result_tokens(r)) for r in records if record_result_tokens(r)]
    if graded:
        lines += [
            f"result tokens vs the naut baseline ({RESULT_TOK_BASELINE_TOTAL / 1000:.1f}k/run, "
            f"{RESULT_TOK_BASELINE_PER_RESULT:.0f}/result): "
            + ", ".join(
                f"run {k} {result_tok_grade(rt.get('total'), RESULT_TOK_BASELINE_TOTAL)}/"
                f"{result_tok_grade(rt.get('per_result'), RESULT_TOK_BASELINE_PER_RESULT)}"
                for k, rt in graded
            )
            + " (total/per result; ±10 % = same)"
        ]
    note = floor_note(
        [(f"run {r.get('run') or 1}", record_api_floor(r)) for r in records if record_api_floor(r) is not None]
    )
    if note:
        lines += [note]
    judges = {(r.get("persona") or {}).get("judge_model") for r in records} - {None}
    if len(judges) > 1:
        lines += ["judge models differ between runs (scores don't compare): " + " | ".join(sorted(judges))]
    return "\n".join(lines)


COMPARE_METRICS = (  # (label, value of a record, format): play.py runs --vs
    ("wall min", lambda r: r.get("wall_min"), "{:.1f}"),
    ("model min (raw)", lambda r: ((r.get("model_time") or {}).get("model_s") or 0) / 60 or None, "{:.2f}"),
    (
        "adj model min",
        lambda r: None if record_adjusted_model_s(r) is None else record_adjusted_model_s(r) / 60,
        "{:.2f}",
    ),
    ("API floor s", record_api_floor, "{:.2f}"),
    ("model turns", lambda r: (r.get("model_time") or {}).get("model_turns"), "{:.0f}"),
    ("idle median s", record_idle, "{:.2f}"),
    ("cost $", lambda r: r.get("cost_usd"), "{:.2f}"),
)


def series_compare(name_a: str, recs_a: list[dict], name_b: str, recs_b: list[dict]) -> str:
    """Two series side by side (means, b - a), and "API floor differs" when their mean floors are further apart
    than FLOOR_DIFF_S: the raw model-time delta is then the API's, the adj model min one is ours."""
    head = ("metric", name_a, name_b, "delta")
    rows = []
    for label, get, fmt in COMPARE_METRICS:
        sa, sb = stats([get(r) for r in recs_a]), stats([get(r) for r in recs_b])
        cell = lambda st, fmt=fmt: "-" if st is None else fmt.format(st["mean"])
        d = (
            "-"
            if sa is None or sb is None
            else (
                fmt.format(sb["mean"] - sa["mean"])
                if sb["mean"] < sa["mean"]
                else "+" + fmt.format(sb["mean"] - sa["mean"])
            )
        )
        if sa and sb and sa["mean"]:
            d += f" ({100 * (sb['mean'] - sa['mean']) / sa['mean']:+.0f} %)"
        rows.append((label, cell(sa), cell(sb), d))
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    lines = [f"{name_a} (n={len(recs_a)}) vs {name_b} (n={len(recs_b)}), means"]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in [head, *rows]]
    lines.insert(2, "  ".join("-" * w for w in widths))
    fa, fb = (stats([record_api_floor(r) for r in rs]) for rs in (recs_a, recs_b))
    note = floor_note([(name_a, fa["mean"]), (name_b, fb["mean"])]) if fa and fb else None
    if note:
        lines += [note]
    elif not (fa and fb):
        lines += ["API floor unknown for a series (records before the field: play.py runs --backfill)"]
    return "\n".join(lines)
