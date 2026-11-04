"""Seat tally over arena run folders: grades every match with the bench (arena.grade + arena.results) and prints the
seat measurement of the website repo's ops/logs/arena-fight-analysis.md ("Before baseline from the matches on
record", "Measurement plan after O1"). Reads files only (no game).

    uv run -q python tools/dev/arena_seat_tally.py runtime/sessions [more dirs] \
        [--arena packages/bench/arenas/arena-v0.json] [--snake-at 28] [--ambiguous-from 25] [--min-n 30] [--json f]

Each argument is a run folder (holding facts.jsonl) or a folder of them. A folder without an arena_start record is
skipped. The arena is the --arena file with each run's own name and seed. A match's pairing is its seed and its two
sides, so a self-pair's later matches on a seed are replays and count once (arena._distinct, REVIEW-7 f2).

Void checks, per valid match (plan "Checks before reading the result"): `alternating` = round 1 alternates single
phases (A1|B1|.. or B1|A1|..); `first_constant` = every round's first phase is the same side. Both only mean
something on an O1 head; on the old order they fail by design.

Verdict on the counted matches, a draw = half (opener = round 1's first actor):
too_few (n < --min-n) | void (a counted match fails a void check) | snake (opener >= --snake-at) | letter (side A
or B >= --snake-at) | ambiguous (opener >= --ambiguous-from) | keep_o1. Defaults are the 40-seed plan.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bg3bench import arena, facts

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ARENA = REPO / "packages" / "bench" / "arenas" / "arena-v0.json"


def run_dirs(paths: list[Path]) -> list[Path]:
    """The run folders under the arguments, sorted: a folder with facts.jsonl, or the ones one level down."""
    out: set[Path] = set()
    for p in paths:
        if (p / "facts.jsonl").is_file():
            out.add(p)
        elif p.is_dir():
            out.update(d for d in p.iterdir() if (d / "facts.jsonl").is_file())
    return sorted(out)


def grade_dir(d: Path, base: dict) -> dict | None:
    """One run folder's grade with dir, seed and pairing; None without an arena_start."""
    recs = facts.load(d / "facts.jsonl")
    st = next((r for r in recs if r.get("ev") == "arena_start"), None)
    if st is None:
        return None
    ar = arena.with_defaults({**base, "name": st["arena"], "seed": st["seed"]})
    rolls_f = d / "rolls.jsonl"
    rolls = (
        [json.loads(line) for line in rolls_f.read_text().splitlines() if line.strip()] if rolls_f.is_file() else None
    )
    g = arena.grade(recs, ar, rolls=rolls)
    sides = g["sides"]
    return {**g, "dir": d.name, "seed": st["seed"], "pairing": f"seed{st['seed']}:{sides['A']}|{sides['B']}"}


def graded(paths: list[Path], base: dict) -> list[dict]:
    """Every arena run folder's grade under the arguments (run_dirs order)."""
    return [g for d in run_dirs(paths) if (g := grade_dir(d, base)) is not None]


def alternating(pattern: str | None) -> bool:
    phases = arena._phases(pattern)
    return bool(phases) and all(n == 1 for _, n in phases) and all(a[0] != b[0] for a, b in itertools.pairwise(phases))


def first_constant(round_first: list[dict]) -> bool:
    return len({r["first"] for r in round_first}) == 1


def points(m: Mapping[str, Any], side: str | None) -> float:
    return 1.0 if m["result"] == side else 0.5 if m["result"] == "draw" else 0.0


def tally(matches: list[dict], snake_at: float, ambiguous_from: float, min_n: int) -> dict:
    """The counted matches' points for the opener, A and B, the void checks, the bench table's rates and a verdict."""
    counted = arena._distinct(matches)
    opener = sum(points(m, m["first_actor"]) for m in counted)
    side = {s: sum(points(m, s) for m in counted) for s in arena.SIDES}
    void = [m["dir"] for m in counted if not (alternating(m["r1_pattern"]) and first_constant(m["round_first"]))]
    if len(counted) < min_n:
        verdict = "too_few"
    elif void:
        verdict = "void"
    elif opener >= snake_at:
        verdict = "snake"
    elif max(side.values()) >= snake_at:
        verdict = "letter"
    elif opener >= ambiguous_from:
        verdict = "ambiguous"
    else:
        verdict = "keep_o1"
    valid = [m for m in matches if m["valid"]]
    r = arena.results(valid, seed=1, b=200) if valid else None
    return {
        "n_dirs": len(matches),
        "n_valid": len(valid),
        "n_counted": len(counted),
        "opener_points": opener,
        "side_points": side,
        "void_dirs": void,
        "verdict": verdict,
        "first_actor": r and r["first_actor"],
        "side_a": r and r["side_a"],
        "favoured": r and r["favoured"],
        "by_pattern": r and r["by_pattern"],
    }


ROW_KEYS = ("dir", "seed", "valid", "invalid_reason", "result", "reason", "rounds", "r1_pattern", "first_actor")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--arena", type=Path, default=DEFAULT_ARENA)
    ap.add_argument("--snake-at", type=float, default=28)
    ap.add_argument("--ambiguous-from", type=float, default=25)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    base = json.loads(a.arena.read_text())
    matches = graded(a.paths, base)
    if not matches:
        print("no arena run folders", file=sys.stderr)
        return 2
    t = tally(matches, a.snake_at, a.ambiguous_from, a.min_n)
    seen = {id(m) for m in arena._distinct(matches)}
    for m in matches:
        row = {k: m[k] for k in ROW_KEYS}
        row["round_first"] = "".join(str(r["first"]) for r in m["round_first"])
        row["counted"] = id(m) in seen
        print(json.dumps(row))
    print(json.dumps({k: v for k, v in t.items() if k != "by_pattern"}))
    if a.json:
        a.json.write_text(json.dumps({"matches": matches, "tally": t}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
