#!/usr/bin/env python3
"""Think-while-acting: could the model's next turn have overlapped the game's remaining time, and which model
turns need not exist at all?

    tools/vis/overlap.py naut2 naut2-r2 naut2-r3        # playthrough names (runtime/sessions/<name>)
    tools/vis/overlap.py runtime/sessions/naut2/S001     # one session (stem, or its .jsonl)
    tools/vis/overlap.py naut2 -v                         # + every blocking call and every unneeded turn

Input: the runner's Sxxx.jsonl (stream-json) + Sxxx.turns.jsonl (arrival times), read with bg3harness/triage.py.
A session dir is every Sxxx*.jsonl with a .turns.jsonl beside it (keeper transcripts have none).

Overlap, per blocking call (>= BLOCK_S): upper = min(next think, call time); realistic = only when the next call
could have been issued without the result (notes/wiki; a walk/climb tail with a predictable next step, capped at one
model turn LEAD_S). Enemy turns, interrupts, loading screens: 0, the next decision needs them.

Unneeded model turns, one category per call (first match):
  refusal    a call the bridge refused (FAILED/REFUSED/NOT READY as the call's own answer), plus the reads and
             further refusals after it, up to the next game action that worked (that one is the needed turn)
  no-change  a read answered "No change" / "No map change"
  re-wait    wait_for right after a wait_for that woke early
  chainable  a trip issued right after an interact that worked (one queue_actions could have held both)
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runner"))  # runs.py: raw streams
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
sys.path.insert(0, str(REPO / "bridge" / "src"))
sys.path.insert(0, str(REPO / "tools"))
from bg3harness import triage  # noqa: E402
import repo_paths  # noqa: E402

SESSIONS = repo_paths.runtime_dir() / "sessions"  # where playthrough names resolve (the main tree's from a worktree)
BLOCK_S = 1.5   # a call shorter than this blocks nothing worth overlapping
FOLD_S = 3.0   # the table folds tools that blocked nothing and cost less model time than this
LEAD_S = 2.0    # what an early return / a queued next step can overlap at most: one model turn (median ~2 s)
INDEP = {"note", "memory_write", "memory_edit", "memory_append", "memory_read", "wiki_page", "wiki_search",
         "ToolSearch"}
READS = {"get_map", "get_status", "look", "get_frontier", "route", "get_nearby", "get_party", "get_dialogue",
         "get_combat_state", "path_preview", "look_at", "screenshot"}
WALKS = {"interact", "wait_for", "travel_to", "walk_toward", "move_to"}
TRIPS = {"travel_to", "walk_toward", "move_to"}
NO_CHANGE = ("No change", "No map change")
_MARK = r"(FAILED|REFUSED|NOT READY)"
_REFUSED = re.compile(rf"^(?:[^:\n]{{1,70}}:\s*)?{_MARK}\b\s*[—:-]?\s*(.*)")
_STEM = re.compile(r"^S\d+(-[\w]+)?$")  # S001, S001-fix; not S001.fights / S001-keeper
_STEP_LINE = ("TURN ", "move ", "action:", "then", "STOPPED", "EVENTS", "CHANGES", "NOW")


def is_read(c: dict) -> bool:
    return c["name"] in READS and not (c["name"] == "route" and (c["input"] or {}).get("go"))


def refusal(c: dict) -> str | None:
    """The call's own refusal message, or None. A failed step inside a turn/queue whose fallback ran
    ("action: X: FAILED → Y: done") is not a refusal of the call."""
    t = c["text"].strip()
    if t.startswith("<tool_use_error>"):
        return re.sub(r"</?tool_use_error>", "", t).strip()
    for line in [ln.strip() for ln in t.splitlines() if ln.strip()][:3]:
        if line.startswith(_STEP_LINE):
            continue
        m = _REFUSED.match(line)
        if m:
            return f"{m.group(1)}: {m.group(2)}"
    return None


def refusal_key(tool: str, msg: str) -> str:
    """Group key: tool + the reason's head (ids, numbers, quoted values masked; cut at the first ( ; :)."""
    mark, _, why = msg.partition(": ")
    why = re.split(r"\s[(;:]|[;:]\s|\s—\s", why, maxsplit=1)[0]
    return f"{tool}: {mark} {triage.error_key(why)}".strip()


def load(stem: Path) -> list[dict]:
    """Timed calls of one session in order, each with dur (call time) and think (model time before it)."""
    calls, _ = triage.load_calls(stem.parent / (stem.name + ".jsonl"), stem.parent / (stem.name + ".turns.jsonl"))
    out, prev = [], None
    for c in calls:
        if c["t"] is None or c["t_res"] is None:
            continue
        c["dur"] = c["t_res"] - c["t"]
        c["think"] = max(0.0, c["t"] - prev) if prev is not None else 0.0
        c["refusal"] = refusal(c)
        out.append(c)
        prev = c["t_res"]
    return out


def overlap(calls: list[dict]) -> tuple[dict, list[tuple]]:
    """({tool: [calls, model s, blocking calls, blocked s, upper s, realistic s]}, verbose rows)."""
    by = collections.defaultdict(lambda: [0, 0.0, 0, 0.0, 0.0, 0.0])
    rows = []
    for k, c in enumerate(calls):
        b = by[c["name"]]
        b[0] += 1
        b[1] += c["think"]
        if c["dur"] < BLOCK_S or k + 1 >= len(calls):
            continue
        nx = calls[k + 1]
        up = min(nx["think"], c["dur"])
        r = 0.0
        if nx["name"] in INDEP:
            r, why = up, "independent next (notes/wiki)"
        elif is_read(nx) and any(s in nx["text"] for s in NO_CHANGE):
            why = "redundant read (waste, not overlap)"
        elif c["name"] == "wait_for" and "early wake" in c["text"]:
            why = "early wake, then waited again (-1 turn)"
        elif c["name"] in WALKS and not c["refusal"] and not any(
                s in c["text"] for s in ("INTERRUPTED", "STOPPED", "FAILED")):
            r, why = min(up, LEAD_S), "walk/climb tail: next step queueable behind it"
        elif c["name"] in ("end_turn", "combat_turn") and "waited" in c["text"]:
            why = "enemy turns: next decision needs them"
        else:
            why = "next step needs the result"
        b[2] += 1
        b[3] += c["dur"]
        b[4] += up
        b[5] += r
        rows.append((c, nx, up, r, why))
    return by, rows


def unneeded(calls: list[dict]) -> list[dict]:
    """[{cat, calls: [call], think, key, closed_by}] in order; see the module doc for the categories."""
    out, used = [], set()
    for k, c in enumerate(calls):
        if k in used:
            continue
        if c["refusal"]:
            ep, j = [k], k + 1
            while j < len(calls) and (calls[j]["refusal"] or is_read(calls[j]) or calls[j]["name"] in INDEP):
                if calls[j]["name"] not in INDEP:  # notes are independent work, not waste
                    ep.append(j)
                j += 1
            used.update(ep)
            out.append(dict(cat="refusal", calls=[calls[i] for i in ep], think=sum(calls[i]["think"] for i in ep),
                            key=refusal_key(c["name"], c["refusal"]),
                            refusals=[refusal_key(calls[i]["name"], calls[i]["refusal"])
                                      for i in ep if calls[i]["refusal"]],
                            closed_by=calls[j]["name"] if j < len(calls) else None))
            continue
        prev = calls[k - 1] if k else None
        if is_read(c) and any(s in c["text"] for s in NO_CHANGE):
            cat, key = "no-change", f"{c['name']} after {prev['name'] if prev else '-'}"
        elif c["name"] == "wait_for" and prev and prev["name"] == "wait_for" and "early wake" in prev["text"]:
            cat, key = "re-wait", "wait_for after an early wake"
        elif c["name"] in TRIPS and prev and prev["name"] == "interact" and not prev["refusal"]:
            cat, key = "chainable", f"{c['name']} after interact"
        else:
            continue
        used.add(k)
        out.append(dict(cat=cat, calls=[c], think=c["think"], key=key, refusals=[], closed_by=None))
    return out


def stems(target: str) -> list[Path]:
    """Session stems of a playthrough name, a session dir, or one session (stem or .jsonl)."""
    p = Path(target)
    if not p.exists():
        p = SESSIONS / target if (SESSIONS / target).is_dir() else p
    if p.is_dir():
        return sorted(p / f.name[:-6] for f in p.glob("S*.jsonl")
                      if _STEM.match(f.name[:-6]) and (p / (f.name[:-6] + ".turns.jsonl")).exists())
    p = p.with_name(p.name[:-6]) if p.name.endswith(".jsonl") else p
    if not (p.parent / (p.name + ".turns.jsonl")).exists():
        raise SystemExit(f"{target}: no {p.name}.turns.jsonl")
    return [p]


def report(name: str, calls: list[dict], verbose: bool = False) -> tuple[list[str], list[dict]]:
    by, rows = overlap(calls)
    un = unneeded(calls)
    out = []
    if not calls:
        return [f"{name}: no timed calls"], un
    wall = calls[-1]["t_res"] - calls[0]["t"]
    think = sum(c["think"] for c in calls)
    tot = [sum(b[i] for b in by.values()) for i in range(6)]
    out.append(f"{name}: {len(calls)} calls, wall {wall:.0f} s, model {think:.0f} s, blocking calls {tot[2]} = "
               f"{tot[3]:.0f} s; overlap upper {tot[4]:.0f} s, realistic {tot[5]:.0f} s")
    out.append(f"  {'tool':18} {'calls':>5} {'model s':>8} {'blocking':>8} {'blocked s':>9} {'upper':>6} {'real':>6}")
    rest = [0, 0.0, 0, 0.0, 0.0, 0.0]
    small = [t for t, b in by.items() if b[2] == 0 and b[1] < FOLD_S]
    for t, b in sorted(by.items(), key=lambda x: -(x[1][3] + x[1][1])):
        if len(small) > 1 and t in small:
            rest = [x + y for x, y in zip(rest, b)]
            continue
        out.append(f"  {t[:18]:18} {b[0]:5} {b[1]:8.1f} {b[2]:8} {b[3]:9.1f} {b[4]:6.1f} {b[5]:6.1f}")
    if len(small) > 1:
        out.append(f"  {f'({len(small)} others)':18} {rest[0]:5} {rest[1]:8.1f} {'':8} {'':9} {'':6} {'':6}")
    out.append("  " + unneeded_summary(un))
    for u in un:
        c0 = u["calls"][0]
        at = c0["t"] - calls[0]["t"]
        chain = " → ".join(c["name"] + ("!" if c["refusal"] else "") for c in u["calls"])
        tail = f" → {u['closed_by']}" if u["cat"] == "refusal" and u["closed_by"] else ""
        out.append(f"    +{at:5.0f}s {u['cat']:9} {len(u['calls']):2} turns {u['think']:5.1f}s  {chain}{tail}"
                   + (f"   [{u['key']}]" if u["cat"] == "refusal" else ""))
    ew = [c for c in calls if c["name"] == "wait_for" and "early wake" in c["text"]]
    nw = [c for c in calls if c["name"] == "wait_for"]
    out.append(f"  early-wake wait_for: {len(ew)}; lead_s=0 passed on "
               f"{sum(1 for c in nw if (c['input'] or {}).get('lead_s') == 0)} of {len(nw)} wait_for")
    if verbose:
        out.append("  blocking calls:")
        for c, nx, up, r, why in rows:
            out.append(f"    {c['name']:16} {c['dur']:5.1f}s -> {nx['name']:16} think {nx['think']:4.1f}  "
                       f"up {up:4.1f} real {r:4.1f}  {why}")
        out.append("  refusals (full text):")
        for c in calls:
            if c["refusal"]:
                out.append(f"    {c['name']}: {c['refusal'][:200]}")
    return out, un


def unneeded_summary(un: list[dict]) -> str:
    """'unneeded model turns: N calls, S s model (cat calls/s, …)'."""
    cats = collections.defaultdict(lambda: [0, 0.0])
    for u in un:
        cats[u["cat"]][0] += len(u["calls"])
        cats[u["cat"]][1] += u["think"]
    n_un = sum(v[0] for v in cats.values())
    return (f"unneeded model turns: {n_un} calls, {sum(v[1] for v in cats.values()):.1f} s model "
            f"({', '.join(f'{k} {v[0]}/{v[1]:.1f}s' for k, v in sorted(cats.items(), key=lambda x: -x[1][1]))})")


def top_refusals(per_target: dict[str, list[dict]]) -> list[str]:
    """Refusal keys across targets: count per target, total, model s of their episodes."""
    names = list(per_target)
    cnt = collections.defaultdict(lambda: collections.Counter())
    secs = collections.Counter()
    for name, un in per_target.items():
        for u in un:
            if u["cat"] != "refusal":
                continue
            for key in u["refusals"]:
                cnt[key][name] += 1
            secs[u["key"]] += u["think"]
    if not cnt:
        return ["top refusals: none"]
    out = [f"top refusals: total, per target ({' / '.join(names)}), model s of the episodes this refusal opened"]
    for key, c in sorted(cnt.items(), key=lambda kv: (-sum(kv[1].values()), -secs[kv[0]])):
        out.append(f"  {sum(c.values()):3}  {' / '.join(str(c[n]) for n in names):>{4 * len(names)}}  "
                   f"{secs[key]:5.1f}s  {key[:120]}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("targets", nargs="+", help="playthrough name, session dir, or session (stem or .jsonl)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    per_target = {}
    for target in a.targets:
        un_all = []
        for stem in stems(target):
            lines, un = report(f"{stem.parent.name}/{stem.name}", load(stem), a.verbose)
            print("\n".join(lines))
            un_all += un
        per_target[target] = un_all
    print("\n".join(top_refusals(per_target)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
