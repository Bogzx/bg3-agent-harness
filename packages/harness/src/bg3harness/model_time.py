# pyright: basic
"""Model time vs harness time: a session's live feel split into what the harness spent (tool calls: the bridge, the
game, MCP transport) and what the model spent (tool result -> its next message), with the model part normalised to a
reference API speed so runs on different providers / hours / models compare.

    uv run python -m bg3harness.model_time <session_dir> [--run K]

Stdlib only (plus the repo's bridge/src/bg3ai/metrics.py, itself stdlib only). Reads what a session leaves behind:
Sxxx.turns.jsonl (runner arrival times of every assistant block, tool_use and tool_result; else the claude stream's
own timestamps), the session's events (bg3harness/events.py: Sxxx.events.jsonl, else the raw stream Sxxx.jsonl; claude's
thinking_duration_ms per message, the final result event's exact duration_api_ms / output_tokens / thinking_tokens)
and Sxxx.metrics.jsonl (the bridge's tool records and activity
samples, for the idle gaps). Play sessions and their resumes only (-fix / -keeper are not the player).

Per session, then summed over the run:
  harness_s        union of the tool calls' [tool_use, tool_result] spans                                   measured
  model_s          per model message: from its trigger (the later of the last tool_result before it and the
                   previous message's last block) to its last block; summed (messages never overlap)        measured
  split            wall time cut four ways: harness only / model only / overlap (tools already running while
                   the same message still streams) / other (startup before the first message, the tail)     measured
  idle             the idle gaps of latency.json (bg3ai.metrics.gaps: between visible actions, minus async
                   busy time, enemy turns, loading screens) attributed to the same four buckets            measured
  ttft_s           time to first token, on messages that start with a thinking block: first block - trigger
                   - thinking_duration_ms (the CLI's own thinking timer). Includes the CLI's request
                   overhead and the prompt prefill. The median stands in for messages without thinking      measured
  out_tok          output tokens per message: the stream's size estimate (content chars / 3.5, the CLI's
                   thinking-token estimate), scaled so the session sums to the result event's exact
                   output_tokens (content and thinking scaled apart). No result event: the raw estimate     estimate
  out_tok_s        output speed: exact output_tokens / (duration_api_ms - messages x ttft median); without a
                   result event: out_tok / (model_s - turns x ttft median) over the timeline                derived
  adjusted_model_s model_s - out_tok / out_tok_s + out_tok / REF_OUT_TOK_S   (TTFT kept as measured)        estimate
  adjusted_model_s_ref_ttft   the same with TTFT also normalised: - turns x ttft median + turns x REF_TTFT_S estimate
                   = the headline "adj model min" of run records and `play.py runs` (HEADLINE): the model's own
                   work at a fixed API, so a slower API hour does not read as a regression
  api_floor        the API's fixed cost per request: the first-block latency (trigger -> first block) of the
                   messages without thinking; p10_s = the fastest 10 %, the floor every turn pays (naut 1.00 s,
                   naut2 1.49-1.60 s: +0.6 s x ~85 turns was the whole naut -> naut2 model-time rise). A series
                   comparison whose floors differ by more than FLOOR_DIFF_S says "API floor differs"     measured

What the logs cannot separate (NOT_SEPARABLE): thinking vs visible tokens per message are estimates (only the session
total is exact); TTFT on messages without thinking is the median, not a measurement; CLI overhead between a
tool_result and the next request sits inside ttft; time a message spends streaming a tool_use input while the tool
of an earlier block already runs is "overlap"; a provider's server-side queueing cannot be told from prefill.

model_time(state_dir, since=None, until=None) is what play.py adds to a run record.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from bg3ai import metrics as M

from . import events

REF_OUT_TOK_S = 100.0   # the reference output speed (tokens/s) adjusted model time is normalised to: a fixed yardstick,
REF_TTFT_S = 0.75       # never changed between runs that are compared (both are recorded in "ref")
HEADLINE = "adjusted_model_s_ref_ttft"   # the "adj model min" of run records, tables and the dashboard
FLOOR_DIFF_S = 0.3      # API floors (api_floor.p10_s) further apart than this: model-time differences are the API's
WINDOW_SLACK_S = 5.0    # as judge.py: the run record is written a little after the last call
# harness_by_tool keeps every tool (was the top 8: naut2's dialogue_advance, ~1-3 s after the helm skip, dropped
# out of all 3 run records). The dashboard picks its own top few.

PLAY_STREAM_RE = re.compile(r"^S\d{3,}(?:-[rc]\d+)?\.jsonl$")   # as runs.PLAY_STREAM_RE (play sessions + resumes)
RESUME_RE = re.compile(r"^(S\d{3,})-([rc])(\d+)\.jsonl$")
BUCKETS = ("harness_only_s", "model_only_s", "overlap_s", "other_s")
NOT_SEPARABLE = [
    "thinking vs visible tokens per message (estimated; only the session total is exact)",
    "TTFT on messages without a thinking block (the session median stands in)",
    "CLI overhead between a tool_result and the next request (inside ttft)",
    "provider queueing vs prompt prefill (both inside ttft)",
]
MEASURED = ["harness_s", "model_s", "split", "idle", "ttft_s", "think_s", "wall_s", "api_floor"]
ESTIMATED = ["out_tok (per message; total exact when tokens_source=exact_scaled)", "out_tok_s", "gen_s",
             "residual_s", "adjusted_model_s", "adjusted_model_s_ref_ttft"]


# ======================================================================== spans

def _union(spans) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for lo, hi in sorted(s for s in spans if s[1] > s[0]):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _length(spans) -> float:
    return sum(hi - lo for lo, hi in _union(spans))


def _intersect(a, b) -> list[tuple[float, float]]:
    a, b, out, i, j = _union(a), _union(b), [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(a, b) -> list[tuple[float, float]]:
    out = []
    b = _union(b)
    for lo, hi in _union(a):
        cur = lo
        for blo, bhi in b:
            if bhi <= cur or blo >= hi:
                continue
            if blo > cur:
                out.append((cur, blo))
            cur = max(cur, bhi)
        if cur < hi:
            out.append((cur, hi))
    return out


def _clip(spans, lo: float, hi: float) -> list[tuple[float, float]]:
    return [(max(a, lo), min(b, hi)) for a, b in spans if min(b, hi) > max(a, lo)]


def split(model: list, harness: list, lo: float, hi: float) -> dict:
    """[lo, hi] cut into harness only / model only / overlap / other (seconds)."""
    m, h = _union(_clip(model, lo, hi)), _union(_clip(harness, lo, hi))
    both = _length(_intersect(m, h))
    hm, mm = _length(h), _length(m)
    return {"harness_only_s": hm - both, "model_only_s": mm - both, "overlap_s": both,
            "other_s": max(hi - lo - (hm + mm - both), 0.0)}


# ======================================================================== the files

def _iso(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def stream_info(stream: Path) -> dict:
    """From the session's events: the thinking time per message id and the last result event's exact totals; and
    turns-like records rebuilt from the raw claude stream's own timestamps (used when Sxxx.turns.jsonl is missing;
    only claude's lines carry timestamps)."""
    think_ms: dict[str, float] = {}
    result, records, t = None, [], None
    for ev in events.load_events(Path(str(stream)[:-len(".jsonl")]), kinds={"thinking", "result"}):
        if ev.kind == "thinking" and ev.message_id and ev.think_ms:
            think_ms[ev.message_id] = think_ms.get(ev.message_id, 0.0) + ev.think_ms
        elif ev.kind == "result":
            result = {"api_ms": ev.api_ms, "out": (ev.tokens or {}).get("output"), "think": ev.think_tokens,
                      "turns": ev.turns, "wall_ms": ev.duration_ms}
    for ev in M.read_jsonl(stream):
        typ = ev.get("type")
        if ev.get("timestamp"):
            t = _iso(ev["timestamp"]) or t
        if t is not None and typ in ("assistant", "user", "system"):
            records.extend(M.stream_records(ev, t))
    return {"think_ms": think_ms, "result": result, "records": records}


def resume_parent(stream: Path) -> Path | None:
    """The stream a resume continues: S024-r1 -> S024, S024-r2 -> S024-r1 (None: not a resume)."""
    m = RESUME_RE.match(stream.name)
    if not m:
        return None
    n = int(m[3])
    return stream.with_name(f"{m[1]}-{m[2]}{n - 1}.jsonl" if n > 1 else f"{m[1]}.jsonl")


def own_api_ms(stream: Path, res: dict) -> float | None:
    """This process's API time. A resumed claude session reports duration_api_ms summed with the session it resumed
    (S024-r1: 1344 s of API in 755 s of wall; S024 had 994 s), output_tokens not: more API time than wall time means
    the parent's is in it, so take it off; None when that does not make sense either (then: the timeline speed)."""
    api, wall = res.get("api_ms"), res.get("wall_ms")
    if not api or not wall or api <= wall:
        return api
    parent = resume_parent(stream)
    pres = stream_info(parent)["result"] if parent and parent.is_file() else None
    if pres and pres.get("api_ms") and 0 < api - pres["api_ms"] <= wall:
        return api - pres["api_ms"]
    return None


def session_files(state_dir: Path) -> list[tuple[str, Path]]:
    try:
        files = sorted(Path(state_dir).iterdir()) if Path(state_dir).is_dir() else []
    except OSError:
        files = []
    return [(f.name[:-len(".jsonl")], f) for f in files if PLAY_STREAM_RE.match(f.name)]


# ======================================================================== one session

def model_turns(turns: list[dict], think_ms: dict | None = None) -> list[dict]:
    """One entry per model message (assistant blocks grouped by message id), in order: trigger (None for the first
    message: its prompt time is not recorded), first / last block arrival, content-token estimate, the CLI's
    thinking-token estimate, the CLI's thinking time and, on a thinking message, the TTFT."""
    think_ms = think_ms or {}
    out: list[dict] = []
    last_result, pending_think, cur = None, 0, None
    for r in sorted(turns, key=lambda r: r.get("t", 0)):
        typ = r.get("type")
        if typ == "result":
            last_result = r["t"]
        elif typ == "think_tok":
            pending_think = max(pending_think, r.get("n") or 0)
        elif typ == "assistant":
            mid = r.get("mid")
            if cur is None or not mid or mid != cur["mid"]:
                prev_end = cur["t_last"] if cur else None
                trig = None
                if prev_end is not None:
                    trig = max(prev_end, last_result) if last_result is not None else prev_end
                cur = {"mid": mid, "trigger": trig, "t_first": r["t"], "t_last": r["t"], "est": 0,
                       "think_est": pending_think}
                pending_think = 0
                ms = think_ms.get(mid) if mid else None
                if ms:
                    cur["think_s"] = ms / 1000.0
                    if trig is not None and r["t"] - trig - cur["think_s"] >= 0:
                        cur["ttft_s"] = r["t"] - trig - cur["think_s"]
                out.append(cur)
            cur["t_last"] = r["t"]
            cur["est"] += r.get("est") or 0
    return out


def tool_spans(turns: list[dict], end: float) -> list[dict]:
    """[tool_use, tool_result] per call (a call with no result runs to `end`)."""
    res = {r.get("id"): r["t"] for r in turns if r.get("type") == "result" and "t" in r}
    return [{"name": r.get("name") or "?", "t0": r["t"], "t1": max(res.get(r.get("id"), end), r["t"])}
            for r in turns if r.get("type") == "use" and "t" in r]


def idle_spans(actions: list[dict], samples: list[dict], tools: list[dict], wrap_t: float | None) -> list[tuple]:
    """(kind, idle spans) per idle gap: the same gaps, kinds and exclusions as bg3ai.metrics.gaps (kept in step
    with it; test_model_time checks the totals match), as spans instead of seconds."""
    out = []
    samples = sorted((s for s in samples if "t" in s), key=lambda s: s["t"])
    waits = sorted((r for r in tools if r.get("tool") == "wait_for" and not r.get("nested") and "t0" in r),
                   key=lambda r: r["t0"])
    loads = [(r["t0"], r["t1"]) for r in tools if r.get("tool") == "load_game" and not r.get("nested") and "t0" in r]
    for prev, nxt in zip(actions, actions[1:]):
        a, b = prev["t1"], nxt["t0"]
        wrap = wrap_t is not None and a >= wrap_t
        if wrap_t is not None and a < wrap_t < b:
            b = wrap_t
        if b - a <= 0:
            continue
        inside = M._samples_between(samples, a, b)
        if wrap:
            kind = "wrapup"
        elif nxt["tool"] in M.DIALOGUE_TOOLS or any(s.get("dlg") for s in inside):
            kind = "dialogue"
        elif any(s.get("combat") for s in inside) or nxt["tool"] == "end_turn":
            kind = "combat"
        else:
            kind = "explore"
        excl = M._spans(samples, a, b, M._is_busy)
        if kind in ("combat", "dialogue"):
            excl += M._enemy_turn_spans(samples, waits, a, b)
        excl += [(max(lo, a), min(hi, b)) for lo, hi in loads if lo < b and hi > a]
        excl += M._spans(samples, a, b, lambda s: s.get("load"), M.LOAD_SPAN_S)
        out.append((kind, _subtract([(a, b)], excl)))
    return out


def _in(r: dict, since: float | None, until: float | None) -> bool:
    t = r.get("t", r.get("t0"))
    return t is None or ((since is None or t >= since) and (until is None or t <= until))


def analyze_session(name: str, stream: Path, since: float | None = None, until: float | None = None) -> dict | None:
    """The raw split of one play session (seconds unrounded, per-message lists kept for the run sums); None: no
    timing in the window."""
    base = str(stream)[:-len(".jsonl")]
    info = stream_info(stream)
    turns_all = [r for r in M.read_jsonl(Path(base + ".turns.jsonl")) if "t" in r]
    if info["records"] and not any(r.get("mid") for r in turns_all if r.get("type") == "assistant"):
        turns_all = info["records"]   # no turns file, or an old one without message ids: the stream's timestamps
    turns = [r for r in turns_all if _in(r, since, until)]
    if not any(r.get("type") == "assistant" for r in turns):
        return None
    clipped = len(turns) < len(turns_all)
    turns += [r for r in M.wrapup_from_stream(stream, turns) if _in(r, since, until)]
    lo, hi = min(r["t"] for r in turns), max(r["t"] for r in turns)

    msgs = model_turns(turns, info["think_ms"])
    counted = [m for m in msgs if m["trigger"] is not None]
    calls = tool_spans(turns, hi)
    model_sp = [(m["trigger"], m["t_last"]) for m in counted]
    harness_sp = [(c["t0"], c["t1"]) for c in calls]

    # tokens: per-message estimates scaled to the exact session totals when the result event is there
    res = dict(info["result"]) if info["result"] and not clipped else None
    if res:
        res["api_ms"] = own_api_ms(stream, res)
    est_c, est_t = sum(m["est"] for m in msgs), sum(m["think_est"] for m in msgs)
    source = "estimate"
    cf = tf = 1.0
    if res and res.get("out"):
        source = "exact_scaled"
        think = res.get("think") or 0
        if est_t and think:
            cf = (res["out"] - think) / est_c if est_c else 0.0
            tf = think / est_t
        elif est_c + est_t:
            cf = tf = res["out"] / (est_c + est_t)
    for m in msgs:
        m["tok"] = m["est"] * cf + m["think_est"] * tf

    ttfts = [m["ttft_s"] for m in counted if "ttft_s" in m]
    first_blocks = [m["t_first"] - m["trigger"] for m in counted if "think_s" not in m]
    ttft_med = M.percentile(ttfts, 50) if ttfts else None
    model_s = sum(m["t_last"] - m["trigger"] for m in counted)
    tok = sum(m["tok"] for m in counted)
    if res and res.get("api_ms") and res.get("out"):
        speed_src = "api" if ttft_med is not None else "api_incl_ttft"
        gen_basis = res["api_ms"] / 1000.0 - len(msgs) * (ttft_med or 0.0)
        tps = res["out"] / gen_basis if gen_basis > 0 else None
    else:
        speed_src = "timeline"
        gen_basis = model_s - len(counted) * (ttft_med or 0.0)
        tps = tok / gen_basis if gen_basis > 0 and tok else None

    idle = []
    mrecs = M.read_jsonl(Path(base + ".metrics.jsonl"))
    tools = [r for r in mrecs if r.get("type") == "tool" and "t0" in r and _in(r, since, until)]
    samples = [r for r in mrecs if r.get("type") == "sample" and _in(r, since, until)]
    acts = M.action_intervals(tools) or M.stream_actions(turns)
    wrap_t = M.wrapup_start(turns)
    for kind, spans in idle_spans(acts, samples, tools, wrap_t):
        idle.append((kind, spans, _sum_splits([split(model_sp, harness_sp, a, b) for a, b in spans])))

    by_tool: dict[str, list] = {}
    for c in calls:
        by_tool.setdefault(c["name"], []).append(c["t1"] - c["t0"])
    return {"session": name, "lo": lo, "hi": hi, "msgs": msgs, "counted": counted, "ttfts": ttfts,
            "first_blocks": first_blocks,
            "ttft_med": ttft_med, "model_s": model_s, "harness_s": _length(harness_sp),
            "harness_call_s": sum(c["t1"] - c["t0"] for c in calls), "by_tool": by_tool,
            "bridge_tool_s": sum(r.get("ms", 0) for r in tools if not r.get("nested")) / 1000.0,
            "split": split(model_sp, harness_sp, lo, hi), "tok": tok, "tokens_source": source,
            "thinking_tok": (res.get("think") if res and res.get("think") is not None
                             else sum(m["think_est"] for m in msgs)),
            "tps": tps, "speed_source": speed_src,
            "api_s": res["api_ms"] / 1000.0 if res and res.get("api_ms") else None,
            "idle": idle}


def _sum_splits(splits: list[dict]) -> dict:
    return {k: sum(s[k] for s in splits) for k in BUCKETS}


# ======================================================================== the report

def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _stats(values: list[float]) -> dict:
    return M.stats(values) if values else {"n": 0}


def adjusted(model_s: float, turns: int, tok: float, gen_s: float | None, ttft_med: float | None,
             ref_tps: float = REF_OUT_TOK_S, ref_ttft: float = REF_TTFT_S) -> dict:
    """The API-speed adjustment. gen_s = tok / measured speed (None: speed unknown, nothing adjusted).
        adjusted_model_s          = model_s - gen_s + tok / ref_tps
        adjusted_model_s_ref_ttft = adjusted_model_s - turns x ttft_med + turns x ref_ttft
    ttft_med None (no thinking timer in the stream): the speed was taken over the whole API time, so gen_s holds
    the TTFT too; only the fully normalised value exists then (adjusted_model_s None)."""
    none = {"gen_s": gen_s, "ttft_total_s": None, "residual_s": None, "adjusted_model_s": None,
            "adjusted_model_s_ref_ttft": None}
    if gen_s is None:
        return none
    if ttft_med is None:
        return dict(none, adjusted_model_s_ref_ttft=model_s - gen_s + tok / ref_tps + turns * ref_ttft)
    ttft_total = turns * ttft_med
    adj = model_s - gen_s + tok / ref_tps
    return {"gen_s": gen_s, "ttft_total_s": ttft_total,
            "residual_s": model_s - gen_s - ttft_total,   # CLI overhead, network, estimate error
            "adjusted_model_s": adj, "adjusted_model_s_ref_ttft": adj - ttft_total + turns * ref_ttft}


def api_floor(first_blocks: list[float], ttfts: list[float]) -> dict | None:
    """The API's fixed cost per request: p10 / median of the first-block latencies of messages without thinking
    (TTFT + the first block's own few tokens), and the measured TTFT median of the thinking messages beside it."""
    if not first_blocks:
        return None
    return {"n": len(first_blocks), "p10_s": _r(M.percentile(first_blocks, 10)),
            "median_s": _r(M.percentile(first_blocks, 50)),
            "ttft_median_s": _r(M.percentile(ttfts, 50)) if ttfts else None}


def floor_of(mt: dict | None) -> float | None:
    """api_floor.p10_s of a model_time dict (a run record's); None for records written before the field."""
    return ((mt or {}).get("api_floor") or {}).get("p10_s")


def headline_s(mt: dict | None) -> float | None:
    """The headline adjusted model time (HEADLINE: output speed and TTFT both at the reference API)."""
    return (mt or {}).get(HEADLINE)


def floor_differs(a: float | None, b: float | None, limit: float = FLOOR_DIFF_S) -> bool:
    return a is not None and b is not None and abs(a - b) > limit + 1e-9   # 1.3 - 1.0 = 0.30000000000000004


def report(sessions: list[dict], ref_tps: float = REF_OUT_TOK_S, ref_ttft: float = REF_TTFT_S,
           per_session: bool = True) -> dict:
    """Sum raw session splits into the reported dict (one session or a whole run)."""
    counted = [m for s in sessions for m in s["counted"]]
    ttfts = [x for s in sessions for x in s["ttfts"]]
    ttft_med = M.percentile(ttfts, 50) if ttfts else None
    model_s = sum(s["model_s"] for s in sessions)
    tok = sum(s["tok"] for s in sessions)
    # each session adjusts with its own speed and TTFT (providers / hours differ between sessions), then summed;
    # a session with tokens but no speed of its own (too short to measure) borrows the run's pooled speed
    known = [s for s in sessions if s["tps"] and s["tok"]]
    pooled = sum(s["tok"] for s in known) / sum(s["tok"] / s["tps"] for s in known) if known else None
    speed = {id(s): s["tps"] or pooled for s in sessions}
    adj = [adjusted(s["model_s"], len(s["counted"]), s["tok"],
                    0.0 if not s["tok"] else (s["tok"] / speed[id(s)] if speed[id(s)] else None),
                    s["ttft_med"], ref_tps, ref_ttft) for s in sessions]
    summed = {k: (sum(a[k] for a in adj) if adj and all(a[k] is not None for a in adj) else None) for k in
              ("gen_s", "ttft_total_s", "residual_s", "adjusted_model_s", "adjusted_model_s_ref_ttft")}
    tps = tok / summed["gen_s"] if summed["gen_s"] else None
    idle: dict[str, dict] = {}
    for s in sessions:
        for kind, spans, sp in s["idle"]:
            for key in (kind, "all") if kind != "wrapup" else (kind,):
                d = idle.setdefault(key, {"n": 0, "idle_s": 0.0, **{b: 0.0 for b in BUCKETS}})
                d["n"] += 1
                d["idle_s"] += _length(spans)
                for b in BUCKETS:
                    d[b] += sp[b]
    by_tool: dict[str, list] = {}
    for s in sessions:
        for k, v in s["by_tool"].items():
            by_tool.setdefault(k, []).extend(v)
    top = sorted(by_tool.items(), key=lambda kv: -sum(kv[1]))   # every tool, most time first
    sources = sorted({s["tokens_source"] for s in sessions})
    n = len(counted)
    out = {
        "sessions": len(sessions),
        "ref": {"out_tok_s": ref_tps, "ttft_s": ref_ttft},
        "wall_s": _r(sum(s["hi"] - s["lo"] for s in sessions)),
        "harness_s": _r(sum(s["harness_s"] for s in sessions)),
        "model_s": _r(model_s),
        "split": {b: _r(sum(s["split"][b] for s in sessions)) for b in BUCKETS},
        "harness_call_s": _r(sum(s["harness_call_s"] for s in sessions)),   # per-call sum (parallel calls add up)
        "bridge_tool_s": _r(sum(s["bridge_tool_s"] for s in sessions)),     # the bridge's own tool time
        "harness_by_tool": {k: {"n": len(v), "s": _r(sum(v))} for k, v in top},
        "model_turns": n,
        "model_s_per_turn": _stats([m["t_last"] - m["trigger"] for m in counted]),
        "think_s": _stats([m["think_s"] for m in counted if "think_s" in m]),
        "ttft_s": dict(_stats(ttfts), source="measured (thinking messages)") if ttfts else None,
        "api_floor": api_floor([x for s in sessions for x in s.get("first_blocks", [])], ttfts),
        "out_tok": round(tok),
        "out_tok_per_turn": _stats([m["tok"] for m in counted]),
        "thinking_tok": round(sum(s["thinking_tok"] or 0 for s in sessions)),
        "tokens_source": sources[0] if len(sources) == 1 else "mixed",
        "out_tok_s": _r(tps, 1),
        "ms_per_out_tok": _r(1000.0 / tps, 1) if tps else None,
        "speed_source": "+".join(sorted({s["speed_source"] for s in sessions})),
        "api_s": _r(sum(s["api_s"] for s in sessions)) if all(s["api_s"] for s in sessions) else None,
        **{k: _r(v) for k, v in summed.items()},
        "adjusted_s_per_turn": _r(summed["adjusted_model_s"] / n) if n and summed["adjusted_model_s"] is not None
        else None,
        "idle": {k: {kk: (_r(vv) if kk != "n" else vv) for kk, vv in d.items()} for k, d in idle.items()},
        "measured": MEASURED, "estimated": ESTIMATED, "not_separable": NOT_SEPARABLE,
    }
    if ttft_med is None:
        out["ttft_s"] = None
    if per_session and len(sessions) > 1:
        out["per_session"] = [
            {"session": s["session"], "model_s": _r(s["model_s"]), "harness_s": _r(s["harness_s"]),
             "turns": len(s["counted"]), "out_tok_s": _r(s["tps"], 1), "ttft_median_s": _r(s["ttft_med"]),
             "adjusted_model_s": _r(a["adjusted_model_s"]),
             "adjusted_model_s_ref_ttft": _r(a["adjusted_model_s_ref_ttft"]),
             "api_floor_p10_s": (api_floor(s.get("first_blocks", []), s["ttfts"]) or {}).get("p10_s")}
            for s, a in zip(sessions, adj)]
    return out


def model_time(state_dir, since: float | None = None, until: float | None = None,
               ref_tps: float = REF_OUT_TOK_S, ref_ttft: float = REF_TTFT_S) -> dict:
    """The model/harness split of every play session in state_dir (runtime/sessions/<playthrough>) with timing in
    [since, until] (None: unbounded), summed. Never raises: {"sessions": 0, "error": ...} on a failure."""
    try:
        sessions = [s for s in (analyze_session(n, p, since, until) for n, p in session_files(Path(state_dir))) if s]
        if not sessions:
            return {"sessions": 0}
        return report(sessions, ref_tps, ref_ttft)
    except Exception as e:   # the run record must still be written
        return {"sessions": 0, "error": f"{type(e).__name__}: {e}"}


# ======================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    from . import judge  # the run lookup (runs.jsonl, run k > 1 beside the series dir)
    ap = argparse.ArgumentParser(description="Split a run's live-feel time into harness and model time, with the "
                                             "model time normalised to a reference API speed.")
    ap.add_argument("session_dir", type=Path, help="runtime/sessions/<playthrough> (the series or the run's dir)")
    ap.add_argument("--run", type=int, default=None, help="run number in runs.jsonl (default: the whole dir)")
    ap.add_argument("--ref-tok-s", type=float, default=REF_OUT_TOK_S)
    ap.add_argument("--ref-ttft", type=float, default=REF_TTFT_S)
    a = ap.parse_args(argv)
    since = until = None
    sdir = a.session_dir
    if a.run is not None:
        record = judge.find_record(a.session_dir, a.run)
        if record is None:
            print(f"no run {a.run} in {a.session_dir}/runs.jsonl", file=sys.stderr)
            return 2
        sdir = judge.streams_dir(a.session_dir, record)
        since, end = record.get("started"), record.get("t") or record.get("reached_at")
        until = end + WINDOW_SLACK_S if isinstance(end, (int, float)) else None
    print(json.dumps(model_time(sdir, since, until, a.ref_tok_s, a.ref_ttft), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
