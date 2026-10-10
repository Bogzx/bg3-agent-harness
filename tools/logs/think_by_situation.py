#!/usr/bin/env python3
"""Think time (gap from the previous result to the next call) by game situation and by the previous tool.

Situation comes from the 1 Hz samples in Sxxx.metrics.jsonl nearest to the call's start: combat, dialogue,
explore_busy (party moving / job running: thinking overlaps play) or explore_idle (thinking = idle party).
Usage: python3 tools/logs/think_by_situation.py claude-run-2 S025 S026 [...]   (needs mine_session.py output)
"""
import json, os, sys, bisect, collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))] if v else 0


def main():
    run, sids = sys.argv[1], sys.argv[2:]
    sit = collections.defaultdict(list)
    prev = collections.defaultdict(list)
    for sid in sids:
        ts, st = [], []
        for line in open(os.path.join(ROOT, "runtime/sessions", run, sid + ".metrics.jsonl")):
            if '"sample"' not in line[:20]:
                continue
            e = json.loads(line)
            ts.append(e["t"])
            if e.get("load"):
                s = "loading"
            elif e.get("combat"):
                s = "combat"
            elif e.get("dlg"):
                s = "dialogue"
            elif e.get("moving") or e.get("job"):
                s = "explore_busy"
            else:
                s = "explore_idle"
            st.append(s)
        last_tool = None
        for line in open(os.path.join(ROOT, "runtime/mined", run, sid + ".calls.jsonl")):
            c = json.loads(line)
            if c["think"] is not None and c["t0"] and ts:
                i = min(len(ts) - 1, bisect.bisect_left(ts, c["t0"] - c["think"] / 2))
                sit[st[i]].append(c["think"])
                if last_tool:
                    prev[last_tool].append(c["think"])
            last_tool = c["tool"]
    print(f"{'situation':14}{'n':>6}{'p50':>6}{'p90':>6}{'sum_s':>8}")
    for k, v in sorted(sit.items(), key=lambda kv: -sum(kv[1])):
        print(f"{k:14}{len(v):6}{q(v, .5):6.1f}{q(v, .9):6.1f}{sum(v):8.0f}")
    print(f"\n{'after tool':20}{'n':>6}{'p50':>6}{'p90':>6}{'sum_s':>8}")
    for k, v in sorted(prev.items(), key=lambda kv: -sum(kv[1]))[:25]:
        print(f"{k:20}{len(v):6}{q(v, .5):6.1f}{q(v, .9):6.1f}{sum(v):8.0f}")


if __name__ == "__main__":
    main()
