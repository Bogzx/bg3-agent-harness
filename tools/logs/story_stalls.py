#!/usr/bin/env python3
"""Story progress per session and the stretches without progress (> MIN minutes), with where the party was and
what the agent said it was doing (from runtime/mined calls). Points by kind per session.
Usage: python3 tools/logs/story_stalls.py claude-run-2 [min_minutes=4] [sids...]
"""
import json, os, sys, re, glob, time, collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WHERE_RX = re.compile(r"WHERE ([^|\\\n]+?)(?: \||\\n|\n| ->)")


def main():
    run = sys.argv[1]
    mn = float(sys.argv[2]) if len(sys.argv) > 2 else 4
    d = os.path.join(ROOT, "runtime/sessions", run)
    sids = sys.argv[3:] or sorted(os.path.basename(p)[:-15] for p in glob.glob(os.path.join(d, "S*.progress.jsonl")))
    stall_where = collections.Counter()
    for sid in sids:
        pj = os.path.join(d, sid + ".progress.jsonl")
        cp = os.path.join(ROOT, "runtime/mined", run, sid + ".calls.jsonl")
        if not os.path.exists(pj) or not os.path.exists(cp):
            continue
        ev = [json.loads(l) for l in open(pj)]
        prog = [e for e in ev if e.get("type") == "progress"]
        calls = [json.loads(l) for l in open(cp)]
        calls = [c for c in calls if c["t0"]]
        if not calls:
            continue
        start, end = calls[0]["t0"], calls[-1]["t0"]
        kinds = collections.Counter()
        for e in prog:
            kinds[e["kind"]] += e.get("points", 0)
        pts = sum(kinds.values())
        mins = (end - start) / 60
        story = sum(v for k, v in kinds.items() if k != "explore")
        print(f"== {sid}: {pts} pts in {mins:.0f} min ({60 * pts / max(mins, 1):.0f}/h; non-explore {60 * story / max(mins, 1):.0f}/h) "
              + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
        marks = [start] + [e["t"] for e in prog if e["kind"] != "explore"] + [end]
        for a, b in zip(marks, marks[1:]):
            if (b - a) / 60 < mn:
                continue
            win = [c for c in calls if a <= c["t0"] <= b]
            where = None
            for c in win:
                m = WHERE_RX.search(c["res"])
                if m:
                    where = m.group(1).strip()
            tools = collections.Counter(c["tool"] for c in win)
            said = [c["said"] for c in win if c["said"]]
            stall_where[where or "?"] += (b - a) / 60
            print(f"  stall {time.strftime('%H:%M', time.localtime(a))}-{time.strftime('%H:%M', time.localtime(b))} "
                  f"{(b - a) / 60:.1f} min @ {where} | top: {', '.join(f'{k}×{v}' for k, v in tools.most_common(5))}")
            for s in said[:: max(1, len(said) // 4)][:4]:
                print("     -", s[:150].replace("\n", " "))
    print("\nminutes without non-explore progress, by place:")
    for k, v in stall_where.most_common(15):
        print(f"  {v:6.1f}  {k}")


if __name__ == "__main__":
    main()
