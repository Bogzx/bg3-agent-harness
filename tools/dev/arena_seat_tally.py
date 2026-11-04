"""Seat tally over arena run folders: grades every match with the bench (arena.grade + arena.results) and prints the
seat measurement of the website repo's ops/logs/arena-fight-analysis.md ("Before baseline from the matches on
record", "Measurement plan after O1"). Reads files only (no game).

    uv run -q python tools/dev/arena_seat_tally.py runtime/sessions [more dirs] \
        [--arena packages/bench/arenas/arena-v0.json] [--snake-at 28] [--ambiguous-from 25] [--min-n 30] [--json f]

Each argument is a run folder (holding facts.jsonl) or a folder of them. A folder without an arena_start record is
skipped. The arena is the --arena file with each run's own name and seed. A match's pairing is its seed and its two
sides, so a self-pair's later matches on a seed are replays and count once (arena._distinct, REVIEW-7 f2).

Void checks, per valid match (plan "Checks before reading the result"; `o1_problems`, empty = the match played the
mod's O1 turn order, arena/turn-order a55109d9). The phases are its arena_turn and arena_turn_passed records (a
passed slot counts in the alternation), each with order, opening and rule:
- every phase names one character and carries order (alternate | snake), opening and rule; one order all match long,
  arena_start's turn_order when it has one (a match from before O1 fails here: the old order is void by design);
- per round and turn group (arena_turn's group; a passed phase belongs to the group before it in the round, or the
  next one when its rule is "opening"): one opening; the first phase is the opening side (rule opening) or the other
  side (rule only_side_left); each next phase is the other side (rule alternate) or the same side (only_side_left);
  the same character again (a readmitted member, a phase recorded twice) continues its phase;
- snake: every group of a round opens with the same side, and that side flips from each round to the next;
  alternate: no round-to-round rule (a dead first member legitimately moves the opening to the other side);
- arena_round's first is the round's first phase's opening.

Verdict on the counted matches, a draw = half (opener = round 1's opening: arena_round, else round 1's first phase's
opening, else its first actor):
too_few (n < --min-n) | void (a counted match fails a void check) | snake (opener >= --snake-at) | letter (side A
or B >= --snake-at) | ambiguous (opener >= --ambiguous-from) | keep_o1. Defaults are the 40-seed plan.
"""

from __future__ import annotations

import argparse
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
    return {
        **g,
        "dir": d.name,
        "seed": st["seed"],
        "pairing": f"seed{st['seed']}:{sides['A']}|{sides['B']}",
        "o1_problems": o1_problems(recs),
        "opener": opener(recs, g),
    }


def graded(paths: list[Path], base: dict) -> list[dict]:
    """Every arena run folder's grade under the arguments (run_dirs order)."""
    return [g for d in run_dirs(paths) if (g := grade_dir(d, base)) is not None]


PHASES = ("arena_turn", "arena_turn_passed")
ORDERS = ("alternate", "snake")


def _phases(records: list[dict]) -> list[dict]:
    return [
        r for r in records if r.get("kind") == "event" and r.get("ev") in PHASES and isinstance(r.get("round"), int)
    ]


def _groups(phases: list[dict]) -> dict[int, list[tuple[Any, list[dict]]]]:
    """Per round, its groups in first-seen order, each (group id, its phases in order). A passed phase (no group of its
    own) joins the group before it in the round, or the next arena_turn's when its rule is "opening" or none came
    before."""
    out: dict[int, list[tuple[Any, list[dict]]]] = {}
    for i, r in enumerate(phases):
        rnd = out.setdefault(r["round"], [])
        if r["ev"] == "arena_turn":
            g = r.get("group")
        elif rnd and r.get("rule") != "opening":
            g = rnd[-1][0]
        else:
            nxt = next((x for x in phases[i + 1 :] if x["round"] == r["round"] and x["ev"] == "arena_turn"), None)
            g = nxt.get("group") if nxt else ("passed", i)
        for gid, ps in rnd:
            if gid == g:
                ps.append(r)
                break
        else:
            rnd.append((g, [r]))
    return out


def o1_problems(records: list[dict]) -> list[str]:
    """What in a match's facts breaks the O1 turn order (module docstring), [] when nothing."""
    phases = _phases(records)
    if not phases:
        return ["no phases"]
    bad: list[str] = []
    orders = {r.get("order") for r in phases}
    for r in phases:
        if r["ev"] == "arena_turn" and len(r.get("members") or []) != 1:
            bad.append(f"round {r['round']}: a phase of {len(r.get('members') or [])} characters")
        if r.get("order") not in ORDERS or r.get("opening") not in arena.SIDES or r.get("rule") is None:
            return [*bad, f"round {r['round']}: a phase without O1's order/opening/rule"]
    start = next((r for r in records if r.get("ev") == "arena_start"), {})
    if len(orders) > 1 or start.get("turn_order", next(iter(orders))) not in orders:
        bad.append(f"turn order {sorted(map(str, orders))} (arena_start {start.get('turn_order')})")
    order = next(iter(orders))
    openings: dict[int, set[str]] = {}
    for rnd, groups in sorted(_groups(phases).items()):
        for gid, ps in groups:
            where = f"round {rnd} group {gid}"
            ops = {r["opening"] for r in ps}
            if len(ops) > 1:
                bad.append(f"{where}: openings {sorted(ops)}")
            op = ps[0]["opening"]
            openings.setdefault(rnd, set()).add(op)
            prev = None
            for r in ps:
                if prev is not None and r.get("character") == prev.get("character"):
                    continue  # its phase goes on (readmitted, or recorded again)
                side, rule = r.get("side"), r.get("rule")
                if prev is None:
                    ok = (rule == "opening" and side == op) or (rule == "only_side_left" and side != op)
                else:
                    ok = (rule == "alternate" and side != prev["side"]) or (
                        rule == "only_side_left" and side == prev["side"]
                    )
                if not ok:
                    after = f"after {prev['side']}" if prev else f"opening {op}"
                    bad.append(f"{where}: {side} {rule} {after}")
                prev = r
    if order == "snake":
        last = None
        for rnd, ops in sorted(openings.items()):
            if len(ops) > 1:
                bad.append(f"round {rnd}: snake groups open with {sorted(ops)}")
            op = next(iter(ops))
            if last is not None and last[0] == rnd - 1 and op == last[1]:
                bad.append(f"round {rnd}: snake opens with {op} again")
            last = (rnd, op)
    first = {}
    for r in phases:
        first.setdefault(r["round"], r["opening"])
    for r in records:
        if r.get("ev") == "arena_round" and r.get("round") in first and r.get("first") != first[r["round"]]:
            bad.append(f"round {r['round']}: arena_round first {r.get('first')}, opening {first[r['round']]}")
    return bad


def opener(records: list[dict], g: Mapping[str, Any]) -> str | None:
    """Round 1's opening side: arena_round's first, else round 1's first phase's opening, else grade's first_actor."""
    for r in records:
        if r.get("ev") == "arena_round" and r.get("round") == 1 and r.get("first") in arena.SIDES:
            return r["first"]
    ones = [r for r in _phases(records) if r["round"] == 1 and r.get("opening") in arena.SIDES]
    return ones[0]["opening"] if ones else g.get("first_actor")


def points(m: Mapping[str, Any], side: str | None) -> float:
    return 1.0 if m["result"] == side else 0.5 if m["result"] == "draw" else 0.0


def tally(matches: list[dict], snake_at: float, ambiguous_from: float, min_n: int) -> dict:
    """The counted matches' points for the opener, A and B, the void checks, the bench table's rates and a verdict."""
    counted = arena._distinct(matches)
    opener = sum(points(m, m["opener"]) for m in counted)
    side = {s: sum(points(m, s) for m in counted) for s in arena.SIDES}
    void = [m["dir"] for m in counted if m["o1_problems"]]
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


ROW_KEYS = ("dir", "seed", "valid", "invalid_reason", "result", "reason", "rounds", "r1_pattern", "opener")


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
        row["o1_problems"] = m["o1_problems"][:3]
        print(json.dumps(row))
    print(json.dumps({k: v for k, v in t.items() if k != "by_pattern"}))
    if a.json:
        a.json.write_text(json.dumps({"matches": matches, "tally": t}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
