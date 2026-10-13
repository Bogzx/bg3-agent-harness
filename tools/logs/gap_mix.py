#!/usr/bin/env python3
"""Attribute each long idle gap (> 10 s, from Sxxx.triage.md) to what the agent did inside it.

Uses runtime/mined/<run>/Sxxx.calls.jsonl (run mine_session.py first). For each gap window, sums
think + duration of the calls started in it, by category. Prints per-session and total seconds by category.
Usage: python3 tools/logs/gap_mix.py claude-run-2 [S025 S026 ...]
"""
import json, os, re, sys, glob, collections, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GAP_RX = re.compile(r"^- (\d\d:\d\d:\d\d) \*\*([\d.]+) s\*\* idle \((\w+)")

CATS = [
    ("memory", lambda c: c["mem"] or c["tool"] in ("note", "session_clock", "session_start", "end_session")),
    ("hand_ui", lambda c: c["tool"] in ("click", "press_key", "screenshot", "zoom", "camera")),
    ("wiki", lambda c: c["tool"].startswith("wiki")),
    ("nav_reads", lambda c: c["tool"] in ("route", "get_frontier", "get_map", "look", "get_nearby", "look_at", "get_status", "get_party", "get_inventory", "get_journal", "get_dialogue", "get_combat_state")),
    ("wait", lambda c: c["tool"] == "wait_for"),
    ("save_load", lambda c: c["tool"] in ("save_game", "load_game", "list_saves")),
    ("other_read", lambda c: c["tool"] in ("Read", "Grep", "Glob", "ToolSearch", "Edit", "Write")),
]


def cat(c):
    for name, f in CATS:
        if f(c):
            return name
    return "action"


def main():
    run = sys.argv[1]
    d = os.path.join(ROOT, "runtime/sessions", run)
    sids = sys.argv[2:] or sorted(os.path.basename(p)[:-10] for p in glob.glob(os.path.join(d, "S*.triage.md")))
    tot = collections.Counter()
    totn = collections.Counter()
    for sid in sids:
        tp = os.path.join(d, sid + ".triage.md")
        cp = os.path.join(ROOT, "runtime/mined", run, sid + ".calls.jsonl")
        if not (os.path.exists(tp) and os.path.exists(cp)):
            continue
        calls = [json.loads(l) for l in open(cp)]
        if not calls or not calls[0]["t0"]:
            continue
        day = datetime.datetime.fromtimestamp(next(c["t0"] for c in calls if c["t0"])).date()
        mix = collections.Counter()
        kinds = collections.Counter()
        n = 0
        for line in open(tp):
            m = GAP_RX.match(line)
            if not m:
                continue
            n += 1
            t = datetime.datetime.combine(day, datetime.time.fromisoformat(m.group(1))).timestamp()
            s = float(m.group(2))
            kinds[m.group(3)] += s
            win = [c for c in calls if c["t0"] and t - 1 <= c["t0"] <= t + s + 1]
            spent = collections.Counter()
            for c in win:
                spent[cat(c)] += (c["think"] or 0) + min(c["dur"] or 0, s)
            # scale to the gap length (think overlaps busy time)
            k = s / sum(spent.values()) if spent and sum(spent.values()) > s else 1
            for kk, v in spent.items():
                mix[kk] += v * k
            mix["unattributed"] += max(0, s - sum(spent.values()) * k)
        if n:
            print(f"{sid}: {n} gaps {sum(kinds.values()):.0f}s ({', '.join(f'{k} {v:.0f}' for k, v in kinds.items())}) | "
                  + ", ".join(f"{k} {v:.0f}" for k, v in mix.most_common()))
            tot.update(mix)
            totn[sid] = n
    print("TOTAL", sum(totn.values()), "gaps |", ", ".join(f"{k} {v:.0f}s" for k, v in tot.most_common()))


if __name__ == "__main__":
    main()
