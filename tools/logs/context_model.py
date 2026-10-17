#!/usr/bin/env python3
"""Context-cost model (vis): replay recorded sessions under other context policies.

Per session (Sxxx.jsonl + Sxxx.turns.jsonl + Sxxx.progress.jsonl):
  calls: API call i (assistant message id) with its context tokens (input + cache read + cache write) and time.
  prices: fitted on the CLI's reported session cost: cost = r x cache_read + w x cache_write (+ output, small).
  think latency: a tool result -> the next assistant event (the model's turn), by context bucket.
  progress: story points per 10-min bucket of session time; handoff overhead: wrap-up + restart + orientation.
Policies replayed on the same call sequence (each call re-reads its whole context from the cache):
  session L min  : context restarts at the start context every L minutes (+ HANDOFF_TOK of notes);
                   cost of a restart = writing that start context into the cache; lost play = OVERHEAD_MIN.
  autocompact X  : at X tokens the context drops to start + COMPACT_SUMMARY_TOK; one summary call
                   (reads X, writes ~COMPACT_OUT_TOK output) = a stall of COMPACT_STALL_S (a >10 s gap).
  drop results K : (not available in Claude Code; upper bound) tool results/images older than the last K
                   calls are not in the context.
Usage: python3 tools/logs/context_model.py claude-run-2 S021 S023 S024 S025 S026
"""
import collections, json, math, os, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HANDOFF_TOK = 4000          # HANDOFF.md + Now line read at the start, on top of the start injection
COMPACT_SUMMARY_TOK = 15000  # Claude Code's compaction summary (typ. 8-20k)
COMPACT_OUT_TOK = 8000       # output tokens to write it
COMPACT_STALL_S = 60.0       # summary call wall time (output at ~15 ms/tok = 120 s; with a fast model ~60 s)
IMAGE_TOK = 1200             # one screenshot in the context
CHARS_PER_TOK = 3.3


def load(run, sid):
    base = os.path.join(ROOT, "runtime/sessions", run, sid)
    calls, order, cost = {}, [], None
    res_tok_after = collections.defaultdict(float)   # tool-result tokens added after call i
    last_mid = None
    for line in open(base + ".jsonl", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "assistant":
            m = d["message"]
            u = m.get("usage") or {}
            if m["id"] not in calls:
                order.append(m["id"])
            calls[m["id"]] = u
            last_mid = m["id"]
        elif t == "user" and last_mid:
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if b.get("type") != "tool_result":
                        continue
                    cc = b.get("content")
                    if isinstance(cc, list):
                        for x in cc:
                            res_tok_after[last_mid] += IMAGE_TOK if x.get("type") == "image" else len(x.get("text", "")) / CHARS_PER_TOK
                    elif isinstance(cc, str):
                        res_tok_after[last_mid] += len(cc) / CHARS_PER_TOK
        elif t == "result":
            cost = d.get("total_cost_usd", cost)
    tmid, lat = {}, []
    last_res = None
    for line in open(base + ".turns.jsonl"):
        d = json.loads(line)
        if d["type"] == "assistant":
            tmid.setdefault(d["mid"], d["t"])
            if last_res is not None:
                lat.append((d["mid"], d["t"] - last_res))
                last_res = None
        elif d["type"] == "result":
            last_res = d["t"]
    seq = []
    for mid in order:
        u = calls[mid]
        ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        if mid in tmid and ctx:
            seq.append({"mid": mid, "t": tmid[mid], "ctx": ctx, "read": u.get("cache_read_input_tokens", 0),
                        "write": u.get("cache_creation_input_tokens", 0), "res": res_tok_after.get(mid, 0.0)})
    pts = []
    if os.path.exists(base + ".progress.jsonl"):
        for line in open(base + ".progress.jsonl"):
            d = json.loads(line)
            if d.get("type") == "progress":
                pts.append((d["t"], d.get("points", 0)))
    latd = dict(lat)
    return {"sid": sid, "seq": seq, "cost": cost, "lat": [(next(c["ctx"] for c in seq if c["mid"] == m), s)
                                                        for m, s in latd.items() if any(c["mid"] == m for c in seq)],
            "pts": pts}


def fit_prices(sessions):
    """least squares, no intercept: cost = r*R + w*W (R, W in M tokens)."""
    a = b = c = d = e = 0.0
    for s in sessions:
        R = sum(x["read"] for x in s["seq"]) / 1e6
        W = sum(x["write"] for x in s["seq"]) / 1e6
        y = s["cost"] or 0
        a += R * R; b += R * W; c += W * W; d += R * y; e += W * y
    det = a * c - b * b
    if abs(det) < 1e-9 * max(a * c, 1e-12):  # one session (or proportional ones): fit one scale, write = 12.5 x read
        k = (d + 12.5 * e) / (a + 25 * b + 156.25 * c) if a + c else 0.0  # (cache write 1.25x input, read 0.1x)
        return k, 12.5 * k
    return (d * c - b * e) / det, (a * e - b * d) / det


def replay(s, r, w, policy, arg):
    """-> (cost $, per-call context list, stalls)"""
    seq = s["seq"]
    t0 = seq[0]["t"]
    start = seq[0]["ctx"]
    cost, ctxs, stalls = 0.0, [], 0
    base_off = 0.0        # tokens removed from the recorded context so far
    seg = 0
    window = collections.deque()
    for i, c in enumerate(seq):
        ctx = c["ctx"] - base_off
        if policy == "session":
            k = int((c["t"] - t0) // (arg * 60))
            if k > seg:
                seg = k
                base_off = c["ctx"] - (start + HANDOFF_TOK)
                ctx = start + HANDOFF_TOK
                cost += w * ctx / 1e6          # the new session writes its start context
        elif policy == "compact":
            if ctx > arg:
                cost += r * ctx / 1e6 + COMPACT_OUT_TOK * 25 / 1e6   # summary call (output ~$25/M est.)
                base_off = c["ctx"] - (start + COMPACT_SUMMARY_TOK)
                ctx = start + COMPACT_SUMMARY_TOK
                cost += w * ctx / 1e6
                stalls += 1
        elif policy == "drop":
            window.append(c["res"])
            if len(window) > arg:
                base_off += window.popleft()
                ctx = c["ctx"] - base_off
        cost += r * max(ctx - c["write"], 0) / 1e6 + w * c["write"] / 1e6
        ctxs.append(ctx)
    return cost, ctxs, stalls


def main():
    run, sids = sys.argv[1], sys.argv[2:]
    S = [load(run, sid) for sid in sids]
    S = [s for s in S if s["seq"] and s["cost"]]
    r, w = fit_prices(S)
    print(f"fitted prices: cache read ${r:.3f}/M, cache write ${w:.2f}/M  (reported vs fitted per session below)")
    hours = sum((s["seq"][-1]["t"] - s["seq"][0]["t"]) / 3600 for s in S)
    # latency vs context
    buckets = collections.defaultdict(list)
    for s in S:
        for ctx, lat in s["lat"]:
            if 0 < lat < 120:
                buckets[min(int(ctx // 100_000), 4)].append(lat)
    print("think latency (tool result -> next model event) by context:")
    for k in sorted(buckets):
        v = buckets[k]
        print(f"  {k*100:>3}-{(k+1)*100 if k < 4 else '':>3}k: median {statistics.median(v):.2f}s  p90 {sorted(v)[int(.9*len(v))]:.2f}s  n={len(v)}")
    # progress by session minute
    pb = collections.defaultdict(float)
    for s in S:
        t0 = s["seq"][0]["t"]
        for t, p in s["pts"]:
            pb[min(int((t - t0) // 600), 5)] += p
    print("story points by 10-min bucket of session time (all sessions): "
          + ", ".join(f"{k*10}-{k*10+10}m {pb[k]:.0f}" for k in sorted(pb)))
    orient = []
    for s in S:
        if s["pts"]:
            orient.append((s["pts"][0][0] - s["seq"][0]["t"]) / 60)
    print(f"minutes to the first story point: {', '.join(f'{x:.1f}' for x in orient)}")
    print(f"\nrecorded: {len(S)} sessions, {hours:.2f} h, ${sum(s['cost'] for s in S):.2f} reported "
          f"(${sum(s['cost'] for s in S)/hours:.1f}/h)")
    rows = [("as recorded (60 min, no compaction)", "none", 0)]
    rows += [(f"sessions of {m} min + handoff", "session", m) for m in (20, 30, 45)]
    rows += [(f"autocompact at {x//1000}k", "compact", x) for x in (150_000, 200_000, 300_000)]
    rows += [(f"drop tool results older than {k} calls (not available)", "drop", k) for k in (20, 50)]
    print(f"{'policy':52} {'$/h':>6} {'ctx p50':>8} {'ctx max':>8} {'extra stalls/h':>14}")
    for label, pol, arg in rows:
        tot, allc, st = 0.0, [], 0
        for s in S:
            c, ctxs, stalls = replay(s, r, w, pol, arg)
            tot += c; allc += ctxs; st += stalls
        print(f"{label:52} {tot/hours:6.1f} {statistics.median(allc)/1000:7.0f}k {max(allc)/1000:7.0f}k {st/hours:14.1f}")
    for s in S:
        c, _, _ = replay(s, r, w, "none", 0)
        print(f"  {s['sid']}: reported ${s['cost']:.2f}, model ${c:.2f}")


if __name__ == "__main__":
    main()
