"""Run report for one series: per-run verdicts, curves, method attribution, cost, tokens, thinking vs wall time, the
luck covariate and links to each run's events and facts, as Markdown and as one self-contained HTML page.

`build(pairs)` takes the series' (manifest v1, result v1) pairs (manifest.py, results.py) and returns a plain dict;
`markdown` and `html` render it. The CLI reads a run tree as the matrix leaves it:

    python -m bg3bench.report RUNS_DIR OUT_DIR [--series NAME] [--links '{"facts": "runs/{run_id}/facts.jsonl"}']

where RUNS_DIR/<run_id>/ holds manifest.json and result.json; it writes OUT_DIR/report.md and OUT_DIR/report.html.
Invalid runs are listed with their reason and never counted in the success rate (BENCHMARK-PLAN.md).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from html import escape
from pathlib import Path
from statistics import mean, median

from bg3bench import manifest as manifest_mod
from bg3bench import results as results_mod
from bg3bench import stats
from bg3bench.methods import METHODS
from bg3bench.site.build import CSS

LINKS = {
    "manifest": "{run_id}/manifest.json",
    "events": "{run_id}/events.jsonl",
    "facts": "{run_id}/facts.jsonl",
}


def _median(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return median(vals) if vals else None


def verdict(res: Mapping) -> str:
    if not res["valid"]:
        return f"invalid ({res['invalid_reason']})"
    return "pass" if res["passed"] else f"fail ({res['outcome']})"


def _run(man: Mapping, res: Mapping, links: Mapping[str, str]) -> dict:
    grade = res["grade"] or {}
    m = res["methods"]
    wall, think = res["wall_s"], res["think_s"]
    return {
        "run_id": res["run_id"],
        "run": man["run"],
        "model": man["model"]["slug"],
        "condition": man["condition"],
        "verdict": verdict(res),
        "valid": res["valid"],
        "passed": res["valid"] and res["passed"],  # an invalid run never shows as passed
        "t_pass_s": res["t_pass_s"] if res["valid"] else None,
        "curve": grade.get("curve"),
        "progress": grade.get("progress"),
        "violations": len(grade.get("violations") or ()),
        "methods": None
        if m is None
        else {
            "gold": {k: m["gold_by_method"][k] for k in METHODS},
            **{k: m[k] for k in ("spent", "crimes", "kills", "traders_killed", "pickpockets")},
        },
        "cost_usd": res["cost_usd"],
        "tokens": res["tokens"],
        "wall_s": wall,
        "think_s": think,
        "think_share": think / wall if think is not None and wall else None,
        "luck": res["luck"],
        "links": {k: v.format(run_id=res["run_id"]) for k, v in links.items()},
    }


def build(pairs: Iterable[tuple[dict, dict]], links: Mapping[str, str] = LINKS) -> dict:
    """(manifest, result) pairs of one series -> the report model."""
    pairs = sorted(pairs, key=lambda p: p[0]["run"])
    if not pairs:
        raise ValueError("no runs to report")
    for man, res in pairs:
        if man["run_id"] != res["run_id"]:
            raise ValueError(f"manifest {man['run_id']!r} paired with result {res['run_id']!r}")
    if len({m["series"] for m, _ in pairs}) != 1:
        raise ValueError("a report covers one series")
    man0 = pairs[0][0]
    runs = [_run(m, r, links) for m, r in pairs]
    valid = [r for r in runs if r["valid"]]
    k = sum(r["passed"] for r in valid)
    costs = [r["cost_usd"] for r in valid]
    lucks = [r["luck"]["z"] for r in valid if r["luck"] is not None]
    return {
        "series": man0["series"],
        "scenario": man0["scenario"]["name"],
        "timing": man0["scenario"]["timing"],
        "seed": man0["seed"],
        "runs": runs,
        "summary": {
            "n_runs": len(runs),
            "n_valid": len(valid),
            "k": k,
            "rate": k / len(valid) if valid else None,
            "wilson": list(stats.wilson(k, len(valid))),
            "cost_total": sum(costs) if costs and None not in costs else None,
            "cost_median": median(costs) if costs and None not in costs else None,
            "tokens_median": _median(sum(r["tokens"].values()) for r in valid),
            "wall_median": _median(r["wall_s"] for r in valid),
            "think_median": _median(r["think_s"] for r in valid),
            "luck_mean_z": mean(lucks) if lucks else None,
        },
    }


# --- formatting ---------------------------------------------------------------------------------------------------


def _min(s: float | None) -> str:
    return "–" if s is None else f"{s / 60:.1f} min"


def _usd(x: float | None) -> str:
    return "–" if x is None else f"${x:.2f}"


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def _tok(t: float | None) -> str:
    if t is None:
        return "–"
    return f"{t / 1e6:.2f} M" if t >= 1e6 else f"{t / 1e3:.0f} k"


def _luck(luck: Mapping | None) -> str:
    return "–" if luck is None else f"z {luck['z']:+.2f} (n={luck['n']})"


def _curve_text(curve: Mapping | None) -> str:
    if not curve:
        return "–"
    pts = ", ".join(f"{p['at_s'] // 60}m {'–' if p['value'] is None else p['value']}" for p in curve["points"])
    return f"{curve['fact']}: {pts}"


def _methods_text(m: Mapping | None) -> str:
    if m is None:
        return "–"
    gold = ", ".join(f"{k} {v}" for k, v in m["gold"].items() if v) or "no gold made"
    extra = ", ".join(f"{k} {m[k]}" for k in ("crimes", "kills", "pickpockets", "traders_killed") if m[k])
    return gold + (f"; {extra}" if extra else "")


def _head(rep: Mapping) -> list[tuple[str, str]]:
    s = rep["summary"]
    lo, hi = s["wilson"]
    rows = [
        ("Scenario", rep["scenario"]),
        ("Timing", rep["timing"] or "unknown"),
        ("Dice seed", "–" if rep["seed"] is None else str(rep["seed"])),
        ("Success", f"{s['k']}/{s['n_valid']} valid runs, {_pct(s['rate'])} [95% Wilson {lo:.0%}, {hi:.0%}]"),
        ("Invalid runs", str(s["n_runs"] - s["n_valid"])),
        ("Cost", f"{_usd(s['cost_total'])} total, {_usd(s['cost_median'])} median per run"),
        ("Tokens", f"{_tok(s['tokens_median'])} median per run"),
        ("Wall / thinking", f"{_min(s['wall_median'])} / {_min(s['think_median'])} median per run"),
        ("Luck", "–" if s["luck_mean_z"] is None else f"mean z {s['luck_mean_z']:+.2f} (0 = average dice)"),
    ]
    return rows


RUN_COLS = ("Run", "Model", "Verdict", "Time to goal", "Cost", "Tokens", "Wall", "Thinking", "Luck", "Curve",
            "Methods", "Links")  # fmt: skip


def _cells(r: Mapping) -> list[str]:
    share = "" if r["think_share"] is None else f" ({r['think_share']:.0%})"
    return [
        str(r["run"]),
        r["model"] + (f" · {r['condition']}" if r["condition"] else ""),
        r["verdict"],
        _min(r["t_pass_s"]),
        _usd(r["cost_usd"]),
        _tok(sum(r["tokens"].values())),
        _min(r["wall_s"]),
        _min(r["think_s"]) + share,
        _luck(r["luck"]),
        _curve_text(r["curve"]),
        _methods_text(r["methods"]),
    ]


def markdown(rep: Mapping) -> str:
    lines = [f"# Series {rep['series']}", ""]
    lines += [f"- **{k}:** {v}" for k, v in _head(rep)]
    if rep["timing"] == "realtime":
        lines += ["", "Real-time scenario: the game keeps running while the model thinks (no pause, D7)."]
    lines += ["", "| " + " | ".join(RUN_COLS) + " |", "|" + "---|" * len(RUN_COLS)]
    for r in rep["runs"]:
        links = " ".join(f"[{k}]({v})" for k, v in r["links"].items())
        cells = [c.replace("|", "\\|") for c in _cells(r)] + [links]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


W, H, PAD = 560, 220, 40


def curves_svg(rep: Mapping) -> str:
    """Every run's curve on one plot (value against minutes); empty when no run has a curve value."""
    series = []
    for r in rep["runs"]:
        pts = [(p["at_s"], p["value"]) for p in ((r["curve"] or {}).get("points") or ()) if p["value"] is not None]
        if pts:
            series.append((r, pts))
    if not series:
        return ""
    fact = series[0][0]["curve"]["fact"]
    xmax = max(t for _, pts in series for t, _ in pts) or 1
    ymax = max(v for _, pts in series for _, v in pts) or 1

    def xy(t: float, v: float) -> tuple[float, float]:
        return PAD + t / xmax * (W - 2 * PAD), H - PAD - v / ymax * (H - 2 * PAD)

    parts = [
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{escape(fact)} over the run, per run">',
        f'<line class="axis" x1="{PAD}" y1="{H - PAD}" x2="{W - PAD}" y2="{H - PAD}"/>',
        f'<line class="axis" x1="{PAD}" y1="{PAD}" x2="{PAD}" y2="{H - PAD}"/>',
        f'<text class="tick" x="{PAD - 6}" y="{PAD + 4}" text-anchor="end">{ymax:g}</text>',
        f'<text class="tick" x="{W - PAD}" y="{H - PAD + 16}" text-anchor="end">{xmax / 60:g} min</text>',
    ]
    for r, pts in series:
        cls = "front" if r["passed"] else "run"
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(t, v) for t, v in [(0, pts[0][1]), *pts]))
        parts.append(f'<polyline class="{cls}" points="{path}"><title>run {r["run"]}</title></polyline>')
    parts.append("</svg>")
    return "\n".join(parts)


def html(rep: Mapping, title: str | None = None) -> str:
    title = title or f"Series {rep['series']}"
    head = "".join(f"<tr><th scope='row'>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in _head(rep))
    rows = []
    for r in rep["runs"]:
        links = " ".join(f'<a href="{escape(v)}">{escape(k)}</a>' for k, v in r["links"].items())
        cells = "".join(f"<td>{escape(c)}</td>" for c in _cells(r))
        rows.append(f'<tr class="{"ok" if r["passed"] else "no"}">{cells}<td class="runs">{links}</td></tr>')
    cols = "".join(f'<th scope="col">{c}</th>' for c in RUN_COLS)
    svg = curves_svg(rep)
    fig = (
        f"<figure>{svg}<figcaption>{escape(rep['runs'][0]['curve']['fact'] if svg else '')} at fixed run times; "
        "highlighted lines passed.</figcaption></figure>"
        if svg
        else ""
    )
    note = (
        '<p class="note">Real-time scenario: the game keeps running while the model thinks (no pause, D7).</p>'
        if rep["timing"] == "realtime"
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}{EXTRA_CSS}</style></head><body><main>"
        f"<h1>{escape(title)}</h1>{note}"
        f'<div class="scroll"><table class="head"><tbody>{head}</tbody></table></div>'
        f"<h2>Runs</h2>"
        f'<div class="scroll"><table><thead><tr>{cols}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        f"{fig}</main></body></html>\n"
    )


EXTRA_CSS = """
table.head th,table.head td{text-align:left;white-space:normal}
td{white-space:nowrap}.run{fill:none;stroke:var(--pt);stroke-width:1.2}
td.runs a{display:inline;margin-right:6px}
td:nth-child(2),td:nth-child(3),td:nth-child(10),td:nth-child(11),thead th:nth-child(-n+3),
thead th:nth-child(10),thead th:nth-child(11){text-align:left}
tr.no td:nth-child(3){color:var(--muted)}
"""


# --- CLI ----------------------------------------------------------------------------------------------------------


def load_runs(runs_dir: str | Path, series: str | None = None) -> list[tuple[dict, dict]]:
    """(manifest, result) of every RUNS_DIR/<run_id>/ that has both, optionally of one series."""
    pairs = []
    for d in sorted(Path(runs_dir).iterdir()):
        if (d / "manifest.json").is_file() and (d / "result.json").is_file():
            man = manifest_mod.load(d / "manifest.json")
            if series is None or man["series"] == series:
                pairs.append((man, results_mod.load(d / "result.json")))
    return pairs


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--series")
    ap.add_argument("--links", help="JSON object: link name -> template with {run_id}")
    args = ap.parse_args(argv)
    links = json.loads(args.links) if args.links else LINKS
    rep = build(load_runs(args.runs_dir, args.series), links)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(markdown(rep), encoding="utf-8")
    (out / "report.html").write_text(html(rep), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
