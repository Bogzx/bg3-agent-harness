#!/usr/bin/env python3
"""Per-tool latency split from Sxxx.metrics.jsonl: total tool ms = RPC (transport + tick wait + Lua) + job wait
(the game doing the action) + bridge-side rest (Python, sleeps/polls, rendering).

Also per-op RPC distributions, and the RPC floor (p50 of the cheapest ops: events.since/ui.state/job.poll alone),
so Lua cost per op ~= op p50 - floor.
Usage: python3 tools/logs/tool_latency.py claude-run-2 S025 S026 [...]
"""
import json, os, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))] if v else 0


def main():
    run, sids = sys.argv[1], sys.argv[2:]
    tools = collections.defaultdict(lambda: {"ms": [], "rpc": [], "job": [], "rest": [], "n_rpc": []})
    ops = collections.defaultdict(list)
    for sid in sids:
        p = os.path.join(ROOT, "runtime/sessions", run, sid + ".metrics.jsonl")
        for line in open(p):
            if '"tool"' not in line[:20]:
                continue
            e = json.loads(line)
            if e.get("nested"):
                continue
            t = tools[e["tool"]]
            t["ms"].append(e["ms"]); t["rpc"].append(e["rpc_ms"]); t["job"].append(e["job_ms"])
            t["rest"].append(max(0, e["ms"] - e["rpc_ms"] - e["job_ms"])); t["n_rpc"].append(e["rpc_n"])
            for op, ms in e.get("ops") or []:
                ops[op].append(ms)
    floor = q(ops.get("events.since", []) + ops.get("ui.state", []) + ops.get("job.poll", []), .5)
    print(f"RPC floor (p50 of events.since/ui.state/job.poll alone): {floor:.1f} ms\n")
    print(f"{'tool':20}{'n':>5}{'p50':>8}{'p90':>8}{'sum_s':>8} | {'rpc%':>5}{'job%':>5}{'rest%':>6}{'rpc/call':>9}{'rest_p50':>9}")
    for k, t in sorted(tools.items(), key=lambda kv: -sum(kv[1]["ms"]))[:30]:
        s = sum(t["ms"]) or 1
        print(f"{k:20}{len(t['ms']):5}{q(t['ms'], .5):8.0f}{q(t['ms'], .9):8.0f}{s / 1000:8.1f} | "
              f"{100 * sum(t['rpc']) / s:5.0f}{100 * sum(t['job']) / s:5.0f}{100 * sum(t['rest']) / s:6.0f}"
              f"{q(t['n_rpc'], .5):9.0f}{q(t['rest'], .5):9.0f}")
    print(f"\n{'op':55}{'n':>5}{'p50':>7}{'p90':>7}{'max':>7}{'lua~':>7}")
    for k, v in sorted(ops.items(), key=lambda kv: -sum(kv[1]))[:25]:
        print(f"{k[:55]:55}{len(v):5}{q(v, .5):7.0f}{q(v, .9):7.0f}{max(v):7.0f}{max(0, q(v, .5) - floor):7.0f}")


if __name__ == "__main__":
    main()
