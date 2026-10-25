# pyright: basic
"""Static leaderboard: a release's results.json v1 -> one self-contained HTML page (no JS, inline SVG).

One table per scenario family (never one number across families): per (model, condition), valid runs only,
k/n with the Wilson interval, pass^K (K = the fewest valid runs any scenario of the family has), median cost per
run, cost per success, median tokens, median wall time and median time to goal, and links to every run's manifest.
A family with a realtime scenario (D7: the world runs while the model thinks) is labelled real-time and shows the
median thinking time next to the wall time.
Each family has a success-vs-cost plot with the Pareto frontier (cheaper and at least as good).
Choice scenarios add a cross-model choice table (choices.table): per scene, which option each model picked in how
many of its valid runs, and the median persona fit when a judge scored the picks. Picks are compared, never scored.

    python -m bg3bench.site.build results.json out/index.html [--families scenario_families.json]
        [--manifest-href 'runs/{run_id}/manifest.json']
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from statistics import median

from bg3bench import choices as choices_mod
from bg3bench import results as results_mod
from bg3bench import stats

DEFAULT_FAMILY = "all"
MANIFEST_HREF = "runs/{run_id}/manifest.json"


def _median(xs: Sequence[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return median(vals) if vals else None


def rows(release: Mapping, families: Mapping[str, str] | None = None) -> dict[str, list[dict]]:
    """family -> one row per (model, condition) that ran a scenario of it, sorted by success rate then cost. A
    maximise scenario's runs (passed null: no pass) have no success rate and are left out."""
    families = families or {}
    runs_by: dict[tuple[str, str, str | None], list[dict]] = {}
    for run in release["runs"]:
        if run["passed"] is None:
            continue
        fam = families.get(run["scenario"], DEFAULT_FAMILY)
        runs_by.setdefault((fam, run["model"], run["condition"]), []).append(run)
    out: dict[str, list[dict]] = {}
    for (fam, model, condition), runs in runs_by.items():
        valid = [r for r in runs if r["valid"]]
        n, k = len(valid), sum(1 for r in valid if r["passed"])
        per_scenario: dict[str, list[bool]] = {}
        for r in valid:
            per_scenario.setdefault(r["scenario"], []).append(r["passed"])
        kk = min((len(v) for v in per_scenario.values()), default=0)
        pass_hat = (
            sum(stats.pass_hat_k(sum(v), len(v), kk) for v in per_scenario.values()) / len(per_scenario) if kk else None
        )
        costs = [r["cost_usd"] for r in valid]
        known_cost = bool(costs) and None not in costs
        out.setdefault(fam, []).append(
            {
                "model": model,
                "condition": condition,
                "scenarios": sorted(per_scenario),
                "n": n,
                "k": k,
                "rate": k / n if n else None,
                "wilson": list(stats.wilson(k, n)),
                "pass_k": kk,
                "pass_hat": pass_hat,
                "cost_per_run": median(costs) if known_cost else None,
                "cost_per_success": sum(costs) / k if known_cost and k else None,
                "tokens": _median([sum(r["tokens"].values()) for r in valid]),
                "wall_s": _median([r["wall_s"] for r in valid]),
                "think_s": _median([r["think_s"] for r in valid]),
                "realtime": any(r["timing"] == "realtime" for r in runs),
                "t_pass_s": _median([r["t_pass_s"] for r in valid if r["passed"]]),
                "n_invalid": len(runs) - n,
                "run_ids": [r["run_id"] for r in runs],
                "harness": sorted({h for r in runs if (h := r["versions"].get("harness"))}),
            }
        )
    for fam_rows in out.values():
        fam_rows.sort(key=lambda r: (-(r["rate"] or 0.0), r["cost_per_run"] is None, r["cost_per_run"] or 0.0))
    return dict(sorted(out.items()))


def pareto(points: Sequence[tuple[float, float]]) -> list[int]:
    """Indices of the (cost, rate) points no other point dominates (cost <= and rate >=, one of them strictly),
    sorted by cost. Duplicates are all kept."""
    front = []
    for i, (c, r) in enumerate(points):
        if not any((c2 <= c and r2 >= r) and (c2 < c or r2 > r) for c2, r2 in points):
            front.append(i)
    return sorted(front, key=lambda i: (points[i][0], -points[i][1]))


def _label(row: Mapping) -> str:
    return row["model"] if row["condition"] is None else f"{row['model']} · {row['condition']}"


def _fmt(x: float | None, spec: str, prefix: str = "") -> str:
    return "–" if x is None else f"{prefix}{x:{spec}}"


def _minutes(s: float | None) -> str:
    return "–" if s is None else f"{s / 60:.1f} min"


def _tokens(t: float | None) -> str:
    if t is None:
        return "–"
    return f"{t / 1e6:.2f} M" if t >= 1e6 else f"{t / 1e3:.0f} k"


W, H, PAD = 560, 300, 44


def plot_svg(fam_rows: Sequence[Mapping]) -> str:
    pts = [(r["cost_per_run"], r["rate"]) for r in fam_rows if r["cost_per_run"] is not None and r["rate"] is not None]
    labels = [_label(r) for r in fam_rows if r["cost_per_run"] is not None and r["rate"] is not None]
    if not pts:
        return '<p class="note">No run in this family has a known cost.</p>'
    xmax = max(c for c, _ in pts) * 1.15 or 1.0

    def xy(c: float, r: float) -> tuple[float, float]:
        return PAD + c / xmax * (W - 2 * PAD), H - PAD - r * (H - 2 * PAD)

    front = pareto(pts)
    parts = [
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Success rate against cost per run">',
        f'<line class="axis" x1="{PAD}" y1="{H - PAD}" x2="{W - PAD}" y2="{H - PAD}"/>',
        f'<line class="axis" x1="{PAD}" y1="{PAD}" x2="{PAD}" y2="{H - PAD}"/>',
    ]
    for frac in (0.0, 0.5, 1.0):
        _, y = xy(0, frac)
        parts.append(f'<line class="grid" x1="{PAD}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD - 6}" y="{y + 4:.1f}" text-anchor="end">{frac:.0%}</text>')
    for frac in (0.0, 0.5, 1.0):
        x, _ = xy(xmax * frac, 0)
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{H - PAD + 16}" text-anchor="middle">${xmax * frac:.2f}</text>'
        )
    parts.append(f'<text class="tick" x="{W / 2}" y="{H - 6}" text-anchor="middle">cost per run (USD)</text>')
    if len(front) > 1:
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(*pts[i]) for i in front))
        parts.append(f'<polyline class="front" points="{path}"/>')
    for i, ((c, r), label) in enumerate(zip(pts, labels, strict=True)):
        x, y = xy(c, r)
        cls = "pt on" if i in front else "pt"
        parts.append(f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="5"><title>{escape(label)}</title></circle>')
        # labels right of the middle sit left of their point, so they stay inside the plot
        lx, anchor = (x - 8, "end") if x > W / 2 else (x + 8, "start")
        parts.append(f'<text class="lbl" x="{lx:.1f}" y="{y - 8:.1f}" text-anchor="{anchor}">{escape(label)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _row_html(row: Mapping, manifest_href: str, realtime: bool) -> str:
    lo, hi = row["wilson"]
    links = " ".join(
        f'<a href="{escape(manifest_href.format(run_id=rid))}">{escape(rid)}</a>' for rid in row["run_ids"]
    )
    links = f"<details><summary>{len(row['run_ids'])} runs</summary>{links}</details>"
    harness = f"<small>harness {escape(', '.join(row['harness']))}</small>" if row["harness"] else ""
    rate = "–" if row["rate"] is None else f"{row['rate']:.0%}"
    pass_hat = "–" if row["pass_hat"] is None else f"{row['pass_hat']:.0%} <small>k={row['pass_k']}</small>"
    cells = [
        f'<th scope="row">{escape(row["model"])}<small>{escape(row["condition"] or "")}</small>{harness}</th>',
        f"<td>{row['k']}/{row['n']}</td>",
        f"<td>{rate} <small>[{lo:.0%}, {hi:.0%}]</small></td>",
        f"<td>{pass_hat}</td>",
        f"<td>{_fmt(row['cost_per_run'], '.2f', '$')}</td>",
        f"<td>{_fmt(row['cost_per_success'], '.2f', '$')}</td>",
        f"<td>{_tokens(row['tokens'])}</td>",
        f"<td>{_minutes(row['wall_s'])}</td>",
        *([f"<td>{_minutes(row['think_s'])}</td>"] if realtime else []),
        f"<td>{_minutes(row['t_pass_s'])}</td>",
        f"<td>{row['n_invalid']}</td>",
        f'<td class="runs">{links}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


HEAD = (
    "Model",
    "k/n",
    "Success [95% Wilson]",
    "pass^k",
    "Cost / run",
    "Cost / success",
    "Tokens / run",
    "Wall / run",
    "Thinking / run",
    "Time to goal",
    "Invalid",
    "Manifests",
)

REALTIME_NOTE = (
    ". Real-time: the game keeps running while the model thinks, so a slow model loses game time; thinking time "
    "is the model's own time per run."
)

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#dedbd2;--card:#fff;--accent:#2f6f5e;--pt:#9a978d}
@media (prefers-color-scheme: dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a3a198;--line:#34332f;
--card:#1f1f1c;--accent:#6cc3a8;--pt:#77756d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.2rem;margin:36px 0 8px}
.meta,.note,small{color:var(--muted)}small{display:block;font-size:.78rem}td small{display:inline}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:top}
th[scope=row],thead th:first-child{text-align:left}thead th{font-size:.8rem;color:var(--muted);font-weight:600}
tbody tr:last-child>*{border-bottom:0}
td.runs{text-align:left;white-space:normal;min-width:110px;font-size:.8rem}
td.runs a{display:block}summary{cursor:pointer;color:var(--accent)}a{color:var(--accent)}
figure{margin:16px 0 0;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
figure svg{width:100%;max-width:640px;height:auto;display:block}
figcaption{font-size:.85rem;color:var(--muted)}
.axis{stroke:var(--muted)}.grid{stroke:var(--line);stroke-dasharray:3 3}.tick,.lbl{fill:var(--muted);font-size:11px}
.lbl{fill:var(--fg)}.pt{fill:var(--pt)}.pt.on{fill:var(--accent)}
.front{fill:none;stroke:var(--accent);stroke-width:1.5}
h2 .tag{display:inline;font-size:.75rem;font-weight:600;margin-left:6px}
h3{font-size:1rem;margin:20px 0 6px}h3 small{display:inline;font-weight:400}
"""


CHOICES_NOTE = (
    "Who picked what at each scene: runs that picked the option / valid runs that reached the scene. Picks are "
    "compared across models, never scored right or wrong."
)


def choices_html(table: Sequence[Mapping]) -> str:
    """The cross-model choice tables (choices.table), one per scene, or "" when no run made a choice."""
    if not table:
        return ""
    parts = [f'<section id="choices"><h2>Choices</h2><p class="meta">{CHOICES_NOTE}</p>']
    for t in table:
        cols = t["columns"]
        head = '<th scope="col">Pick</th>' + "".join(f'<th scope="col">{escape(c)}</th>' for c in cols)
        body = [
            f'<tr><th scope="row">{escape(r["pick"])}</th>'
            + "".join(f"<td>{r['counts'].get(c, 0)}/{t['runs'][c]}</td>" for c in cols)
            + "</tr>"
            for r in t["rows"]
        ]
        if any(v is not None for v in t["fit"].values()):
            body.append(
                '<tr><th scope="row"><small>persona fit (median, 0-10)</small></th>'
                + "".join(f"<td>{_fmt(t['fit'][c], '.1f')}</td>" for c in cols)
                + "</tr>"
            )
        parts.append(
            f"<h3>{escape(t['scenario'])}: <small>{escape(t['scene']) or '(no scene line)'}</small></h3>"
            f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        )
    return "".join(parts) + "</section>"


def render(
    release: Mapping,
    families: Mapping[str, str] | None = None,
    *,
    manifest_href: str = MANIFEST_HREF,
    title: str = "BG3 benchmark",
) -> str:
    tag = release["bench_tag"] or "untagged"
    sections = []
    for fam, fam_rows in rows(release, families).items():
        scenarios = sorted({s for r in fam_rows for s in r["scenarios"]})
        realtime = any(r["realtime"] for r in fam_rows)
        body = "\n".join(_row_html(r, manifest_href, realtime) for r in fam_rows)
        head = "".join(f'<th scope="col">{h}</th>' for h in HEAD if realtime or h != "Thinking / run")
        label = ' <small class="tag">real-time</small>' if realtime else ""
        timing_note = REALTIME_NOTE if realtime else ""
        sections.append(
            f'<section id="family-{escape(fam)}"><h2>{escape(fam)}{label}</h2>'
            f'<p class="meta">Scenarios: {escape(", ".join(scenarios)) or "none with a valid run"}{timing_note}</p>'
            f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>\n{body}\n</tbody></table></div>'
            f"<figure>{plot_svg(fam_rows)}<figcaption>Success rate against median cost per run; the line is "
            f"the Pareto frontier (no other entry is both cheaper and at least as successful).</figcaption>"
            f"</figure></section>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body><main>"
        f"<h1>{escape(title)}</h1>"
        f'<p class="meta">Release {escape(tag)} · suite {escape(release.get("suite") or "unknown")} · bootstrap seed {release["seed"]} · valid runs only; '
        f"invalid runs are counted, never scored.</p>"
        + "".join(sections)
        + choices_html(choices_mod.table(release["runs"]))
        + "</main></body></html>\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("results")
    ap.add_argument("out")
    ap.add_argument("--families", help="JSON object: scenario name -> family")
    ap.add_argument("--manifest-href", default=MANIFEST_HREF)
    args = ap.parse_args(argv)
    families = json.loads(Path(args.families).read_text(encoding="utf-8")) if args.families else None
    html = render(results_mod.load(args.results), families, manifest_href=args.manifest_href)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(html, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
