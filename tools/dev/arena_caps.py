"""Each model's per-side money cap from measured arena matches (owner R4, 2026-11-05; docs/ARENA-RESULTS.md "Money
caps"), with its over-budget count. Reads run folders' arena.json and raw streams only (no game, no API).

    uv run -q python tools/dev/arena_caps.py runtime/sessions [more dirs] [--json f]

Each argument is a run folder (holding arena.json) or a folder of them. Rows are per model and arena: a side's cost
depends on the fight. The sample is every side of a match that ran to its end: a valid match, or an over_budget one
(its cost is a floor; leaving it out would drop exactly the long matches). A match the game or a client cut short is
left out and counted as `cut`. A scripted side is no model.

A side's figure is the larger of what it was billed and its list price (pi's own estimate per response, the
`usage.cost.total` of the raw stream): DeepSeek bills half off-peak, and a cap has to hold at peak. The cap is
CAP_FACTOR x the nearest-rank p95, rounded up to a cent. Below MIN_SIDES sides p95 is the sample's maximum, so the
row has no cap: `cap` is None and `thin` says so.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bg3bench.arena import OVER_BUDGET_FLAG, SIDES

CAP_FACTOR = 3
MIN_SIDES = 20  # math.ceil(0.95 * n) < n from here on: below it p95 is the maximum
NOT_STREAMS = (".events.jsonl", ".fights.jsonl", ".metrics.jsonl")


def run_dirs(paths: Iterable[Path]) -> list[Path]:
    """The run folders under the arguments, sorted: a folder with arena.json, or the ones one level down."""
    out: set[Path] = set()
    for p in paths:
        if (p / "arena.json").is_file():
            out.add(p)
        elif p.is_dir():
            out.update(d for d in p.iterdir() if (d / "arena.json").is_file())
    return sorted(out)


def list_price(d: Path, side: str) -> float:
    """The side's cost at list price: every assistant response's usage.cost.total in its raw streams (a resume's
    process has its own: A.jsonl, A-r1.jsonl, ...)."""
    total = 0.0
    for f in sorted([*d.glob(f"{side}.jsonl"), *d.glob(f"{side}-r*.jsonl")]):
        if f.name.endswith(NOT_STREAMS):
            continue
        for line in f.read_text(errors="replace").splitlines():
            if '"message_end"' not in line or '"assistant"' not in line:
                continue
            try:
                msg = json.loads(line).get("message") or {}
            except ValueError:
                continue  # a raw line that isn't JSON (pi's warnings)
            if msg.get("role") == "assistant":
                total += ((msg.get("usage") or {}).get("cost") or {}).get("total") or 0.0
    return total


def p95(values: Iterable[float]) -> float:
    """The nearest-rank 95th percentile."""
    v = sorted(values)
    return v[math.ceil(0.95 * len(v)) - 1]


def caps(dirs: Iterable[Path]) -> list[dict[str, Any]]:
    """Per (player, arena), sorted: {player, arena, attempts (matches it played a side of, a match against itself
    once), matches and sides (the sample), cut, billed_p95, list_p95, max, cap, thin, over_budget, over_budget_rate,
    over_budget_flag}. The p95 fields and max are None with no sides; `cap` is None and `thin` True below MIN_SIDES."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for d in dirs:
        rec = json.loads((d / "arena.json").read_text())
        end = rec.get("end") or {}
        over = end.get("reason") == "over_budget"
        whole = over or rec.get("invalid") is None
        capped = end.get("capped", list(SIDES)) if over else []
        seen: set[str] = set()
        for s in SIDES:
            side = rec["sides"][s]
            if side.get("model") == "none":
                continue
            p = side.get("player") or side["model"]
            row = rows.setdefault(
                (p, rec["arena"]),
                {
                    "player": p,
                    "arena": rec["arena"],
                    "attempts": 0,
                    "cut": 0,
                    "over_budget": 0,
                    "billed": [],
                    "list": [],
                },
            )
            if p not in seen:
                row["attempts"] += 1
                row["cut"] += not whole
                row["over_budget"] += any(rec["sides"][c].get("player", rec["sides"][c]["model"]) == p for c in capped)
                seen.add(p)
            if whole:
                billed = float((rec.get("costs") or {}).get(s) or 0.0)
                row["billed"].append(billed)
                row["list"].append(max(billed, list_price(d, s)))
    return [_row(rows[k]) for k in sorted(rows)]


def _row(r: dict[str, Any]) -> dict[str, Any]:
    billed, listed = r.pop("billed"), r.pop("list")
    n = len(listed)
    top = p95(listed) if listed else None
    r["over_budget_rate"] = round(r["over_budget"] / r["attempts"], 4)
    return {
        **r,
        "matches": r["attempts"] - r["cut"],
        "sides": n,
        "billed_p95": p95(billed) if billed else None,
        "list_p95": top,
        "max": max(listed) if listed else None,
        "cap": math.ceil(round(CAP_FACTOR * top * 100, 6)) / 100 if top is not None and n >= MIN_SIDES else None,
        "thin": n < MIN_SIDES,
        "over_budget_flag": r["over_budget_rate"] > OVER_BUDGET_FLAG,
    }


def show(r: dict[str, Any]) -> str:
    money = "no sides"
    if r["sides"]:
        money = f"billed p95 ${r['billed_p95']:.4f}, list p95 ${r['list_p95']:.4f}, max ${r['max']:.4f}"
    cap = f"cap ${r['cap']:.2f}" if r["cap"] is not None else f"too thin for a cap ({r['sides']} of {MIN_SIDES} sides)"
    flag = " FLAG" if r["over_budget_flag"] else ""
    return (
        f"{r['player']} on {r['arena']}: {r['sides']} sides of {r['matches']} matches ({r['cut']} cut short), {money}: "
        f"{cap}; over budget {r['over_budget']} of {r['attempts']}{flag}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, help="write the rows here too")
    a = ap.parse_args(argv)
    rows = caps(run_dirs(a.paths))
    for r in rows:
        print(show(r))
    if a.json:
        a.json.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
