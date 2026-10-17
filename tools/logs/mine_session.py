#!/usr/bin/env python3
"""Mine one play session (or all) into per-call records + a summary.

Joins Sxxx.jsonl (tool_use input, tool_result text, assistant text) with Sxxx.turns.jsonl (wall times by tool id)
and Sxxx.metrics.jsonl (server-side tool ms, per-op RPC ms). Streaming, low RAM.

Usage:
  python3 tools/logs/mine_session.py claude-run-2 S026          # one session
  python3 tools/logs/mine_session.py claude-run-2 all           # every session, skips up-to-date ones
Writes runtime/mined/<run>/<sid>.calls.jsonl and <sid>.summary.json; prints a short summary.
"""
import json, os, re, sys, statistics, collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "runner"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import triage  # noqa: E402
import repo_paths  # noqa: E402
SESS = os.path.join(repo_paths.runtime_dir(), "sessions")   # the main tree's from a worktree
OUT = os.path.join(repo_paths.runtime_dir(), "mined")

FAIL_RX = re.compile(r"\bFAILED\b|^(ERROR|Error|STILL IN|STOPPED)|TIMEOUT|timed out after|Connection closed|Tool .* not found|No such tool")
HAND_TOOLS = {"click", "screenshot", "press_key", "zoom", "camera"}
MEM_RX = re.compile(r"/memory/|/knowledge/|play/memory|playthroughs/")


def short(name):
    return name[len("mcp__bg3__"):] if name.startswith("mcp__bg3__") else name


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)


def mine(run, sid):
    base = os.path.join(SESS, run, sid)
    uses, results, texts = {}, {}, []
    order = []
    msg_of = {}
    said, last_text = {}, [""]
    with open(base + ".jsonl", errors="replace") as f:
        for line in f:
            if '"tool_use"' not in line and '"tool_result"' not in line and '"text"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") or {}
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                bt = b.get("type")
                if d.get("type") == "assistant" and bt == "tool_use":
                    uses[b["id"]] = (short(b["name"]), b.get("input", {}))
                    said[b["id"]] = last_text[0]
                    last_text[0] = ""
                    order.append(b["id"])
                    msg_of[b["id"]] = m.get("id")
                elif d.get("type") == "user" and bt == "tool_result":
                    # triage.result_text: the old bridge's {"result": "..."} string and the new text blocks alike
                    results[b["tool_use_id"]] = (triage.result_text(b), bool(b.get("is_error")))
                elif d.get("type") == "assistant" and bt == "text":
                    texts.append((m.get("id"), b.get("text", "")))
                    last_text[0] = b.get("text", "")[:300]
                elif d.get("type") == "user" and bt == "text" and "Stop hook" in b.get("text", ""):
                    texts.append(("STOPHOOK", b.get("text", "")[:200]))
    # times
    t_use, t_res, events = {}, {}, []
    tpath = base + ".turns.jsonl"
    if os.path.exists(tpath):
        for line in open(tpath):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "use":
                t_use[e["id"]] = e["t"]
            elif e.get("type") == "result":
                t_res[e["id"]] = e["t"]
            if e.get("type") in ("use", "result"):
                events.append((e["t"], e["type"], e["id"]))
    events.sort()
    # think = time from the latest result before this use (any tool) to this use
    think = {}
    last_res = None
    last_use_msg = None
    for t, kind, tid in events:
        if kind == "result":
            last_res = t
        else:
            if last_res is not None and msg_of.get(tid) != last_use_msg:
                think[tid] = round(t - last_res, 2)
            last_use_msg = msg_of.get(tid)
    # server-side metrics per tool (match by name+time)
    srv = collections.defaultdict(list)
    ops = collections.defaultdict(list)
    mpath = base + ".metrics.jsonl"
    if os.path.exists(mpath):
        for line in open(mpath):
            if '"tool"' not in line[:20]:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            srv[e.get("tool")].append(e)
            for op, ms in e.get("ops") or []:
                ops[op].append(ms)
    calls = []
    os.makedirs(os.path.join(OUT, run), exist_ok=True)
    with open(os.path.join(OUT, run, sid + ".calls.jsonl"), "w") as out:
        for tid in order:
            name, inp = uses[tid]
            txt, err = results.get(tid, ("", False))
            t0, t1 = t_use.get(tid), t_res.get(tid)
            head = txt[:400]
            failed = err or bool(FAIL_RX.search(head[:200]))
            # failed steps inside batches
            step_fails = len(re.findall(r"FAILED|refused|not reached", txt)) if name in ("do_actions", "queue_actions", "combat_turn", "loot_area") else 0
            rec = {
                "id": tid, "tool": name, "t0": t0, "dur": round(t1 - t0, 2) if t0 and t1 else None,
                "think": think.get(tid), "failed": failed, "step_fails": step_fails,
                "args": json.dumps(inp, ensure_ascii=False)[:300], "res": head,
                "mem": (name in ("Read", "Edit", "Write", "Glob", "Grep") and bool(MEM_RX.search(json.dumps(inp)))) or name.startswith("memory_"),
                "hand": name in HAND_TOOLS, "said": said.get(tid, ""),
            }
            calls.append(rec)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    summ = summarize(sid, calls, ops, texts)
    with open(os.path.join(OUT, run, sid + ".summary.json"), "w") as f:
        json.dump(summ, f, indent=1, ensure_ascii=False)
    return summ


def norm_fail(res):
    s = res.split("\n")[0]
    s = re.sub(r"e\d+", "eN", s)
    s = re.sub(r"-?\d+(\.\d+)?", "#", s)
    s = re.sub(r"'[^']{0,40}'", "'…'", s)
    return s[:140]


def summarize(sid, calls, ops, texts):
    by = collections.defaultdict(lambda: {"n": 0, "fail": 0, "dur": [], "think": []})
    fails = collections.Counter()
    fail_ex = {}
    for c in calls:
        b = by[c["tool"]]
        b["n"] += 1
        b["fail"] += c["failed"]
        if c["dur"] is not None:
            b["dur"].append(c["dur"])
        if c["think"] is not None:
            b["think"].append(c["think"])
        if c["failed"]:
            k = c["tool"] + ": " + norm_fail(c["res"])
            fails[k] += 1
            fail_ex.setdefault(k, c["t0"])
    tools = {k: {"n": v["n"], "fail": v["fail"], "dur_sum": round(sum(v["dur"]), 1), "dur_p50": pct(v["dur"], .5),
                 "dur_p90": pct(v["dur"], .9), "think_sum": round(sum(v["think"]), 1), "think_p50": pct(v["think"], .5)}
             for k, v in by.items()}
    allthink = [c["think"] for c in calls if c["think"] is not None]
    t0s = [c["t0"] for c in calls if c["t0"]]
    return {
        "sid": sid, "calls": len(calls), "span_min": round((max(t0s) - min(t0s)) / 60, 1) if t0s else 0,
        "think_sum_s": round(sum(allthink), 1), "think_p50": pct(allthink, .5), "think_p90": pct(allthink, .9),
        "think_over10": sum(1 for x in allthink if x > 10), "think_over10_s": round(sum(x for x in allthink if x > 10), 1),
        "hand": sum(c["hand"] for c in calls), "mem_calls": sum(c["mem"] for c in calls),
        "mem_s": round(sum((c["dur"] or 0) + (c["think"] or 0) for c in calls if c["mem"]), 1),
        "toolsearch": by["ToolSearch"]["n"] if "ToolSearch" in by else 0,
        "failed": sum(c["failed"] for c in calls), "step_fails": sum(c["step_fails"] for c in calls),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1]["dur_sum"] - kv[1]["think_sum"])),
        "fails": [[k, n, fail_ex[k]] for k, n in fails.most_common(40)],
        "ops": {k: {"n": len(v), "p50": pct(v, .5), "p90": pct(v, .9), "max": round(max(v), 1)}
                for k, v in sorted(ops.items(), key=lambda kv: -sum(kv[1]))[:30]},
        "stophook": sum(1 for m, _ in texts if m == "STOPHOOK"),
    }


def main():
    run, which = sys.argv[1], sys.argv[2]
    d = os.path.join(SESS, run)
    sids = sorted(f[:-6] for f in os.listdir(d) if re.match(r"S\d+(-r\d+|-fix)?\.jsonl$", f)) if which == "all" else [which]
    for sid in sids:
        src = os.path.join(d, sid + ".jsonl")
        dst = os.path.join(OUT, run, sid + ".summary.json")
        if which == "all" and os.path.exists(dst) and os.path.getmtime(dst) > os.path.getmtime(src):
            continue
        s = mine(run, sid)
        print(f"{sid}: {s['calls']} calls {s['span_min']} min think {s['think_sum_s']}s (p50 {s['think_p50']}) "
              f"fail {s['failed']} hand {s['hand']} mem {s['mem_calls']}/{s['mem_s']}s toolsearch {s['toolsearch']} stophook {s['stophook']}")


if __name__ == "__main__":
    main()
