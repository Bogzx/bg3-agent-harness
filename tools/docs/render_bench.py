"""Render the diverse-2026-10-25 pilot results as SVGs for the README (light and dark).

Usage: python3 tools/docs/render_bench.py <bench-diverse-2026-10-25.json> <out dir>

Writes bench-pilot-grid-{light,dark}.svg (models x scenarios) and bench-pilot-gold-{light,dark}.svg (party gold
over time in the two gold scenarios). The input is the read-only analysis JSON (its keys are documented in the
report next to it). Standard library only.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

FONT = 'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif'
MODELS = [
    ("Opus 5.5", "Claude Code"),
    ("Sonnet 5.5", "Claude Code"),
    ("Haiku 5.5", "Claude Code"),
    ("DeepSeek V4.1 Flash", "pi"),
]
SCENARIOS = [
    ("crash_site_grove", "Crash site to Grove", "Act 1 · 60 min"),
    ("act1_gold_max", "Most gold, Act 1", "Act 1 · 45 min"),
    ("act3_gold_max", "Most gold, Act 3", "Act 3 · 45 min"),
    ("act1_level_3", "Party to level 3", "Act 1 · 60 min"),
    ("fight_act1_windmill", "Windmill fight", "Act 1 · 60 min"),
    ("fight_act2_balthazar", "Balthazar fight", "Act 2 · 90 min"),
]
# Footnote marks for the cells whose result needs one; the texts are in grid_svg's footer.
MARKS = {
    ("Haiku 5.5", "fight_act1_windmill"): "1",
    ("Sonnet 5.5", "act1_gold_max"): "2",
    ("Haiku 5.5", "act1_gold_max"): "2",
    ("Haiku 5.5", "act3_gold_max"): "2",
    ("Opus 5.5", "act3_gold_max"): "3",
    ("Opus 5.5", "act1_gold_max"): "4",
}
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "surface": "#fcfcfb",
        "border": "#e1e0d9",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#6b6a65",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "pass_bg": "#e4f3e4",
        "fail_bg": "#f8e3e3",
        "score_bg": "#f0efec",
        "good": "#0a8a0a",
        "critical": "#d03b3b",
        "banner_bg": "#fdf0d5",
        "banner_ink": "#6b4a00",
        "s1": "#2a78d6",
        "s2": "#eb6834",
        "s3": "#1baf7a",
        "s4": "#eda100",
    },
    "dark": {
        "surface": "#1a1a19",
        "border": "#2c2c2a",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#9a9891",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "pass_bg": "#1b3320",
        "fail_bg": "#3b2020",
        "score_bg": "#262624",
        "good": "#0ca30c",
        "critical": "#e66767",
        "banner_bg": "#3a2f12",
        "banner_ink": "#f3d48a",
        "s1": "#3987e5",
        "s2": "#d95926",
        "s3": "#199e70",
        "s4": "#c98500",
    },
}
BANNER = "PILOT: one run each, not a ranking. Rules condition with known gaps; the harness changed during the matrix."


def text(x: float, y: float, s: str, size: float, fill: str, weight: int = 400, anchor: str = "start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}">{escape(s)}</text>'
    )


def frame(width: int, height: int, t: dict[str, str], title: str, body: list[str]) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" font-family=\'{FONT}\' role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" fill="{t["surface"]}" '
            f'stroke="{t["border"]}"/>',
            *body,
            "</svg>",
            "",
        ]
    )


def banner(t: dict[str, str], y: float, width: int) -> list[str]:
    return [
        f'<rect x="24" y="{y:.1f}" width="{width - 48}" height="26" rx="6" fill="{t["banner_bg"]}"/>',
        text(36, y + 17.5, BANNER, 12.5, t["banner_ink"], 600),
    ]


def counted_jobs(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """The job that counts per (model, scenario): a B4 rerun comes after B3 in `jobs` and replaces it."""
    return {(j["model"], j["scenario"]): j for j in data["jobs"]}


def wipe_markers(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """The party-wipe marker of each counted gold run that has one (its score counts the gold at the wipe)."""
    return {
        (tl["model"], tl["scenario"]): tl["markers"][0]
        for tl in data["gold_timelines"]
        if tl["counted"] and tl.get("markers")
    }


def cell_lines(job: dict[str, Any], wipe: dict[str, Any] | None = None) -> tuple[str, str, str]:
    """(kind, headline, detail) for one job: kind is pass, fail or score."""
    label = job["label"] or ""
    mins = job["minutes"]
    if job["outcome"] == "score":
        gain = job["score"]
        head = f"{gain:+,} gold"
        if wipe:
            return "score", head, f"wiped at {wipe['t_min']:.1f} min"
        if "wiped" in label:
            return "score", head, f"wiped at {mins:.1f} min"
        if "resume cap" in label:
            return "score", head, f"cut at {mins:.1f} min"
        if "arrested" in label:
            return "score", head, "arrested, gold taken"
        return "score", head, f"{mins:.0f} min"
    if job["outcome"] == "pass":
        return "pass", "pass", f"{job['t_pass_min']:.1f} min"
    if "cost cap" in label:
        return "fail", "fail", f"cost cap at {mins:.1f} min"
    if "wiped" in label:
        return "fail", "fail", f"party wiped, {mins:.1f} min"
    return "fail", "fail", f"{mins:.1f} min"


def grid_svg(data: dict[str, Any], mode: str) -> str:
    t = THEMES[mode]
    jobs, wipes = counted_jobs(data), wipe_markers(data)
    width, left, top, col_w, row_h = 960, 172, 132, 128, 66
    body = [
        text(24, 36, "Benchmark pilot, 2026-10-25: 4 models x 6 scenarios", 19, t["ink"], 650),
        text(
            24,
            58,
            "Pass or fail on a goal read from game state, with minutes of play to the pass. The two gold",
            13,
            t["ink2"],
        ),
        text(24, 76, "scenarios have no pass: the score is the party's gold gain in 45 minutes.", 13, t["ink2"]),
        *banner(t, 88, width),
    ]
    for i, (_, name, sub) in enumerate(SCENARIOS):
        cx = left + i * col_w + col_w / 2
        body.append(text(cx, top - 2, name, 12.5, t["ink"], 600, "middle"))
        body.append(text(cx, top + 14, sub, 11, t["muted"], 400, "middle"))
    y0 = top + 26
    for r, (model, client) in enumerate(MODELS):
        y = y0 + r * row_h
        body.append(text(24, y + 29, model, 14, t["ink"], 600))
        body.append(text(24, y + 47, client, 12, t["muted"]))
        for c, (scenario, _, _) in enumerate(SCENARIOS):
            kind, head, detail = cell_lines(jobs[(model, scenario)], wipes.get((model, scenario)))
            x = left + c * col_w
            body.append(
                f'<rect x="{x + 2}" y="{y + 2}" width="{col_w - 4}" height="{row_h - 4}" rx="6" '
                f'fill="{t[kind + "_bg"]}"/>'
            )
            mark = MARKS.get((model, scenario), "")
            cx = x + col_w / 2
            if kind == "score":
                body.append(text(cx, y + 30, head, 15, t["ink"], 650, "middle"))
            else:
                icon, color = ("✓", t["good"]) if kind == "pass" else ("✗", t["critical"])
                body.append(
                    f'<text x="{cx:.1f}" y="{y + 30:.1f}" font-size="15" font-weight="650" text-anchor="middle">'
                    f'<tspan fill="{color}">{icon}</tspan><tspan fill="{t["ink"]}"> {head}</tspan></text>'
                )
            suffix = f" ({mark})" if mark else ""
            body.append(text(cx, y + 49, detail + suffix, 11.5, t["ink2"], 400, "middle"))
    fy = y0 + len(MODELS) * row_h + 26
    cost = {m: v["cost_usd"] for m, v in data["models"].items()}
    notes = [
        "(1) A rerun with the goal fixed to name the windmill goblins. The first run was graded a pass at 1.0 min for",
        "     killing a sleeping bugbear, because the goal then passed any won fight.",
        "(2) Stopped early by a limit of 6 resumes after the agent ended its turn (since removed for gold runs).",
        "(3) Recorded invalid over one move timeout counted as a harness failure; valid on a read-only re-grade.",
        "(4) The agent reloaded after the wipe and ended at +853; a wipe now ends the run, so the score is the gold at",
        "     the wipe.",
        "Times are minutes of play. Claude Code ran on a subscription; at list price the counted runs would have cost",
        f"${cost['Opus 5.5']:.2f} (Opus), ${cost['Sonnet 5.5']:.2f} (Sonnet) and ${cost['Haiku 5.5']:.2f} (Haiku). "
        f"DeepSeek's six runs billed ${cost['DeepSeek V4.1 Flash']:.2f} in all.",
    ]
    for i, line in enumerate(notes):
        body.append(text(24, fy + i * 18, line, 12, t["ink2"]))
    height = int(fy + len(notes) * 18 + 10)
    return frame(width, height, t, "Benchmark pilot results, 2026-10-25", body)


def step_path(points: list[list[float]], sx: Any, sy: Any) -> str:
    d = f"M{sx(points[0][0]):.1f},{sy(points[0][1]):.1f}"
    for (_, g0), (m1, g1) in itertools.pairwise(points):
        d += f" H{sx(m1):.1f}"
        if g1 != g0:
            d += f" V{sy(g1):.1f}"
    return d


def gold_panel(
    data: dict[str, Any], t: dict[str, str], scenario: str, title: str, x0: float, y0: float, ymax: int, step: int
) -> list[str]:
    pw, ph = 330, 230
    out = [text(x0, y0 - 14, title, 13.5, t["ink"], 600)]

    def sx(m: float) -> float:
        return x0 + m / 45 * pw

    def sy(g: float) -> float:
        return y0 + ph - g / ymax * ph

    for g in range(0, ymax + 1, step):
        out.append(f'<line x1="{x0}" x2="{x0 + pw}" y1="{sy(g):.1f}" y2="{sy(g):.1f}" stroke="{t["grid"]}"/>')
        out.append(text(x0 - 6, sy(g) + 4, f"{g:,}", 11, t["muted"], 400, "end"))
    out.append(f'<line x1="{x0}" x2="{x0 + pw}" y1="{sy(0):.1f}" y2="{sy(0):.1f}" stroke="{t["axis"]}"/>')
    for m in (0, 15, 30, 45):
        out.append(text(sx(m), y0 + ph + 16, f"{m}", 11, t["muted"], 400, "middle"))
    out.append(text(x0 + pw, y0 + ph + 32, "minutes of play", 11, t["muted"], 400, "end"))
    lines = {tl["model"]: tl for tl in data["gold_timelines"] if tl["scenario"] == scenario and tl["counted"]}
    jobs, wipes = counted_jobs(data), wipe_markers(data)
    labels: list[tuple[float, float, str, str]] = []
    for i, (model, _) in enumerate(MODELS):
        tl, color = lines[model], t[f"s{i + 1}"]
        pts, wipe = tl["points"], wipes.get((model, scenario))
        style = f'fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"'
        if wipe:  # counted up to the wipe; the play after it (a reload) is drawn dashed
            before = [p for p in pts if p[0] < wipe["t_min"]] + [[wipe["t_min"], wipe["gold"]]]
            after = [[wipe["t_min"], wipe["gold"]]] + [p for p in pts if p[0] > wipe["t_min"]]
            out.append(f'<path d="{step_path(before, sx, sy)}" {style}/>')
            out.append(f'<path d="{step_path(after, sx, sy)}" {style} stroke-dasharray="3 4" opacity="0.7"/>')
            end_pt = before[-1]
        else:
            out.append(f'<path d="{step_path(pts, sx, sy)}" {style}/>')
            end_pt = pts[-1]
        ex, ey = sx(end_pt[0]), sy(end_pt[1])
        out.append(
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="{color}" stroke="{t["surface"]}" stroke-width="2"/>'
        )
        _, _, detail = cell_lines(jobs[(model, scenario)], wipe)
        end = "" if detail.endswith(" min") and not detail.startswith(("wiped", "cut")) else f", {detail}"
        labels.append((ex, ey, model.split(" ")[0], f"{tl['score_counted']:+,}{end}"))
    # Direct labels at the line ends, nudged apart vertically where two ends sit close together.
    labels.sort(key=lambda lb: lb[1])
    placed: list[float] = []
    for ex, ey, name, value in labels:
        ly = ey + 4
        while any(abs(ly - p) < 26 for p in placed):
            ly += 6
        placed.append(ly)
        lx, anchor = ex + 8, "start"
        if lx + len(value) * 6.2 > x0 + pw + 90:  # would run into the next panel: label to the left, below the end
            # (above it when the end sits on the axis)
            lx, anchor, ly = ex - 4, "end", ly - 20 if ey > y0 + ph - 40 else ly + 48
        out.append(text(lx, ly - 3, name, 11.5, t["ink"], 600, anchor))
        out.append(text(lx, ly + 10, value, 11, t["ink2"], 400, anchor))
    return out


def gold_svg(data: dict[str, Any], mode: str) -> str:
    t = THEMES[mode]
    width = 960
    intro = [
        "One run per model, 45-minute cap. Score = gold at the end minus gold at the start. A line that stops",
        "early ended early: the party wiped, or the resume limit cut the run. Opus's Act 1 party wiped at 41.7 min",
        "and it reloaded (dashed); the score counts the gold at the wipe. Sonnet's drop to 0 in Act 1: its gold was",
        "confiscated on an arrest, then taken back.",
    ]
    body = [text(24, 36, "Party gold over time in the two gold scenarios", 19, t["ink"], 650)]
    body += [text(24, 58 + i * 18, line, 13, t["ink2"]) for i, line in enumerate(intro)]
    body += banner(t, 124, width)
    body += gold_panel(data, t, "act1_gold_max", "Act 1 (starts with 133 gold)", 80, 198, 1400, 200)
    body += gold_panel(data, t, "act3_gold_max", "Act 3, Lower City (starts with 410 gold)", 545, 198, 14000, 2000)
    ly = 198 + 230 + 56
    x = 24.0
    for i, (model, client) in enumerate(MODELS):
        body.append(
            f'<line x1="{x}" x2="{x + 18}" y1="{ly - 4}" y2="{ly - 4}" stroke="{t[f"s{i + 1}"]}" '
            f'stroke-width="3" stroke-linecap="round"/>'
        )
        label = f"{model} ({client})"
        body.append(text(x + 24, ly, label, 12, t["ink2"]))
        x += 24 + len(label) * 6.6 + 22
    return frame(width, int(ly + 22), t, "Party gold over time in the pilot's gold scenarios", body)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: render_bench.py <bench-diverse-2026-10-25.json> <out dir>", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[1]).read_text())
    out = Path(argv[2])
    for mode in THEMES:
        (out / f"bench-pilot-grid-{mode}.svg").write_text(grid_svg(data, mode))
        (out / f"bench-pilot-gold-{mode}.svg").write_text(gold_svg(data, mode))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
