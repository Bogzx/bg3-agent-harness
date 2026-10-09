"""Latency metrics: does the AI play like a skilled human, live?

Stdlib only: the bridge (recording), runner/play.py (stream timing, session summary) and the dashboard
(live panel) all import it.

Three record streams, all JSONL:
* Sxxx.metrics.jsonl (written by the bridge, path in $BG3AI_METRICS_PATH, default runtime/metrics.jsonl):
    {"type":"tool", "tool", "t0", "t1", "ms", "rpc_ms", "rpc_n", "job_ms", "busy", "nested", "ok"}
      one per tool call. t0/t1 are wall times. ms is the bridge round trip of the whole tool. rpc_ms is the
      time spent in Lua RPCs (TCP eval round trips). job_ms is the time spent waiting on game jobs
      (walking, casting...). nested marks the steps inside do_actions. busy means the party was visibly
      doing something during the call.
    {"type":"sample", "t", "moving", "job", "dlg", "combat"}
      ~1/s from the activity sampler (nav.activity on its own connection).
* Sxxx.turns.jsonl (written by the runner from claude's stream-json, arrival times):
    {"type":"use", "t", "id", "name", "steps"} / {"type":"result", "t", "id"} / {"type":"assistant", "t"}
* Sxxx.latency.json: summarize() at session end. One line per session is also appended to
  runtime/sessions/<playthrough>/latency_history.jsonl.

Definitions:
* action tools: tools that make the party do something visible (ACTION_TOOLS). Inside do_actions the
  steps count, not the wrapper.
* idle gap: from the end of one action to the start of the next. It's tagged "dialogue" when the next
  action answers a dialogue or roll (or the party is in dialogue during the gap), "combat" when the
  party is in combat during the gap, else "explore".
* think time: from a tool_result reaching the model to its next assistant message (stream-json arrival).
* busy %: share of samples where a party member moved or a game job was running. Dialogue share is
  reported separately: the server can't tell playback from waiting on an answer.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

ACTION_TOOLS = {
    "move_to", "walk_toward", "interact", "pick_up", "loot", "loot_corpse", "talk_to", "jump_to", "use_ability",
    "end_turn", "choose_dialogue", "roll_check", "react", "lockpick", "disarm", "pickpocket", "click_on",
    "fast_travel", "camp", "short_rest", "long_rest", "trade", "level_up", "set_party", "click", "press_key",
    "manage_inventory", "wait", "dialogue_advance",
}
NOT_APM = {"wait"}  # counted as an action for gaps (it waits on the game), not as an action per minute
DIALOGUE_TOOLS = {"choose_dialogue", "roll_check", "dialogue_advance"}
BATCH_TOOL = "do_actions"
# Wrappers whose steps run as nested tool records (like do_actions): combat_turn and the macros.
BATCH_TOOLS = {BATCH_TOOL, "combat_turn", "loot_area", "travel_to", "regroup", "read_all", "rest_if_needed"}
LONG_GAP_S = 10.0
MOVE_EPS_M = 0.25  # a party member moved at least this far since the previous sample → moving

TARGETS = {
    "median_idle_explore_s": 2.0,   # median idle gap outside combat (and outside dialogue choices)
    "long_gaps_non_dialogue": 0,    # gaps > 10 s, except deliberate dialogue choices
    "bridge_rt_median_ms": 100.0,   # median round trip of observation tools
}


def _bare(name: str) -> str:
    return (name or "").removeprefix("mcp__bg3__")


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def percentile(values: Iterable[float], p: float) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(values: Iterable[float], nd: int = 2) -> dict:
    xs = [v for v in values if v is not None]
    if not xs:
        return {"n": 0}
    r = lambda v: None if v is None else round(v, nd)  # noqa: E731
    return {"n": len(xs), "median": r(percentile(xs, 50)), "p90": r(percentile(xs, 90)),
            "mean": r(sum(xs) / len(xs)), "max": r(max(xs))}


# ---------------------------------------------------------------------------
# bridge side: recorder + activity sampler
# ---------------------------------------------------------------------------

class Recorder:
    """Collects RPC/job timings from game.Game and writes one record per tool call."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.rpcs: deque = deque(maxlen=5000)   # (t0, t1, op)
        self.jobs: deque = deque(maxlen=500)    # (t0, t1, op, status)
        self.last_active = 0.0                  # wall time of the latest busy sample
        self.active_now = False

    @classmethod
    def from_env(cls, runtime: Path) -> "Recorder":
        p = os.environ.get("BG3AI_METRICS_PATH")
        return cls(Path(p) if p else runtime / "metrics.jsonl")

    def write(self, rec: dict) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def rpc_done(self, op: str, t0: float, t1: float) -> None:
        with self.lock:
            self.rpcs.append((t0, t1, op))

    def job_done(self, op: str, t0: float, t1: float, status: str) -> None:
        with self.lock:
            self.jobs.append((t0, t1, op, status))

    def tool_done(self, tool: str, ms: float, nested: bool = False, ok: bool = True,
                  t1: float | None = None) -> dict | None:
        if tool in ("autoskip",):
            return None
        t1 = time.time() if t1 is None else t1
        t0 = t1 - ms / 1000.0
        with self.lock:
            rpc = [(a, b) for a, b, _ in self.rpcs if a >= t0 - 0.001 and b <= t1 + 0.001]
            job_ms = sum((min(b, t1) - max(a, t0)) * 1000 for a, b, _, _ in self.jobs if b > t0 and a < t1)
            busy = job_ms > 0 or self.last_active >= t0 or self.active_now
        rec = {"type": "tool", "tool": tool, "t0": round(t0, 3), "t1": round(t1, 3), "ms": round(ms, 1),
               "rpc_ms": round(sum((b - a) * 1000 for a, b in rpc), 1), "rpc_n": len(rpc),
               "job_ms": round(max(job_ms, 0.0), 1), "busy": bool(busy)}
        if nested:
            rec["nested"] = True
        if not ok:
            rec["ok"] = False
        self.write(rec)
        return rec

    def sample(self, t: float, moving: bool, job: int, dlg: bool, combat: bool) -> dict:
        active = bool(moving or job)
        with self.lock:
            self.active_now = active
            if active:
                self.last_active = t
        rec = {"type": "sample", "t": round(t, 2), "moving": bool(moving), "job": int(job), "dlg": bool(dlg),
               "combat": bool(combat)}
        self.write(rec)
        return rec


def moved(prev: dict | None, cur: dict | None, eps: float = MOVE_EPS_M) -> bool:
    """Any party member (uuid -> [x,y,z]) moved more than eps between two nav.activity samples."""
    if not isinstance(prev, dict) or not isinstance(cur, dict):
        return False
    for u, p in cur.items():
        q = prev.get(u)
        if isinstance(p, list) and isinstance(q, list) and len(p) >= 3 and len(q) >= 3:
            if sum((p[i] - q[i]) ** 2 for i in range(3)) ** 0.5 > eps:
                return True
    return False


def sampler_loop(rec: Recorder, game, stop: threading.Event, interval: float = 1.0) -> None:
    """Poll nav.activity on a dedicated Game connection and record busy/idle samples."""
    prev = None
    while not stop.wait(interval):
        try:
            a = game.rpc("nav.activity", context="server", timeout=3.0)
        except Exception:  # noqa: BLE001 - loading screens, reloads, old mod without the op
            prev = None
            continue
        if not isinstance(a, dict):
            continue
        pos = a.get("pos") if isinstance(a.get("pos"), dict) else {}
        rec.sample(time.time(), moved(prev, pos), int(a.get("jobs") or 0), bool(a.get("in_dialogue")),
                   bool(a.get("in_combat")))
        prev = pos


# ---------------------------------------------------------------------------
# runner side: stream-json timing
# ---------------------------------------------------------------------------

def _batch_steps(inp: Any) -> int:
    acts = inp.get("actions") if isinstance(inp, dict) else None
    if isinstance(acts, str):
        try:
            acts = json.loads(acts)
        except ValueError:
            return 0
    if isinstance(acts, dict):
        acts = acts.get("actions", [acts])
    return len(acts) if isinstance(acts, list) else 0


def stream_records(ev: dict, t: float) -> list[dict]:
    """Timing records for one stream-json event (arrival time t)."""
    out: list[dict] = []
    typ = ev.get("type")
    if typ == "assistant":
        out.append({"type": "assistant", "t": round(t, 3)})
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                name = _bare(c.get("name", ""))
                r = {"type": "use", "t": round(t, 3), "id": c.get("id"), "name": name}
                if name == BATCH_TOOL:
                    r["steps"] = _batch_steps(c.get("input"))
                out.append(r)
    elif typ == "user":
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                out.append({"type": "result", "t": round(t, 3), "id": c.get("tool_use_id")})
    return out


class StreamTiming:
    """Runner helper: timestamps stream-json events into Sxxx.turns.jsonl."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []

    def on_event(self, ev: dict, t: float | None = None) -> None:
        recs = stream_records(ev, time.time() if t is None else t)
        if not recs:
            return
        self.records.extend(recs)
        if self.path:
            try:
                with open(self.path, "a") as f:
                    for r in recs:
                        f.write(json.dumps(r, separators=(",", ":")) + "\n")
            except OSError:
                pass


def think_times(turns: list[dict]) -> list[float]:
    """Seconds from the last tool_result to the next assistant message, per model turn."""
    out, last_result, waiting = [], None, False
    for r in sorted(turns, key=lambda r: r.get("t", 0)):
        if r.get("type") == "result":
            last_result, waiting = r["t"], True
        elif r.get("type") == "assistant" and waiting and last_result is not None:
            out.append(r["t"] - last_result)
            waiting = False
    return out


def stream_actions(turns: list[dict]) -> list[dict]:
    """Action intervals from stream timing alone (fallback when the bridge metrics file is missing)."""
    uses = {r["id"]: r for r in turns if r.get("type") == "use" and r.get("id")}
    out = []
    for r in turns:
        if r.get("type") == "result" and r.get("id") in uses:
            u = uses[r["id"]]
            if u["name"] in ACTION_TOOLS or u["name"] in BATCH_TOOLS:
                out.append({"tool": u["name"], "t0": u["t"], "t1": r["t"],
                            "steps": u.get("steps", 1) if u["name"] == BATCH_TOOL else 1})
    return sorted(out, key=lambda a: a["t0"])


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def read_jsonl(path: Path | None) -> list[dict]:
    if not path:
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    out.append(v)
    except OSError:
        pass
    return out


def action_intervals(tools: list[dict]) -> list[dict]:
    """Visible-action intervals from bridge tool records: do_actions steps and top-level action tools."""
    out = []
    for r in tools:
        name = r.get("tool")
        if name in ACTION_TOOLS:
            out.append({"tool": name, "t0": r["t0"], "t1": r["t1"], "steps": 1, "nested": r.get("nested", False)})
    if not out:  # no nested step records (old bridge?): fall back to the wrappers
        out = [{"tool": r["tool"], "t0": r["t0"], "t1": r["t1"], "steps": 1}
               for r in tools if r.get("tool") in BATCH_TOOLS]
    return sorted(out, key=lambda a: a["t0"])


def _samples_between(samples: list[dict], a: float, b: float) -> list[dict]:
    return [s for s in samples if a <= s.get("t", 0) <= b]


def gaps(actions: list[dict], samples: list[dict]) -> list[dict]:
    out = []
    for prev, nxt in zip(actions, actions[1:]):
        g = nxt["t0"] - prev["t1"]
        if g <= 0:
            continue
        inside = _samples_between(samples, prev["t1"], nxt["t0"])
        if nxt["tool"] in DIALOGUE_TOOLS or any(s.get("dlg") for s in inside):
            kind = "dialogue"
        elif any(s.get("combat") for s in inside) or nxt["tool"] == "end_turn":
            kind = "combat"
        else:
            kind = "explore"
        out.append({"t": round(prev["t1"], 2), "s": round(g, 2), "kind": kind, "next": nxt["tool"]})
    return out


def _target(value, limit, lower_is_better=True):
    if value is None:
        return {"value": None, "target": limit, "pass": None}
    return {"value": value, "target": limit, "pass": value < limit if lower_is_better else value >= limit}


def summarize(tools: list[dict], samples: list[dict], turns: list[dict],
              start: float | None = None, end: float | None = None) -> dict:
    """Per-session latency summary from bridge tool records, activity samples and stream timing."""
    tool_recs = [r for r in tools if r.get("type", "tool") == "tool" and "t0" in r]
    times = [r["t0"] for r in tool_recs] + [r["t1"] for r in tool_recs] + [s["t"] for s in samples if "t" in s] \
        + [r["t"] for r in turns if "t" in r]
    start = start if start is not None else (min(times) if times else 0.0)
    end = end if end is not None else (max(times) if times else start)
    minutes = max((end - start) / 60.0, 1e-9)

    acts = action_intervals(tool_recs)
    source = "bridge"
    if not acts:
        acts, source = stream_actions(turns), "stream"
    gl = gaps(acts, samples)
    by_kind = {k: [g["s"] for g in gl if g["kind"] == k] for k in ("explore", "combat", "dialogue")}
    long_ = [g for g in gl if g["s"] > LONG_GAP_S]
    long_kind = {k: sum(1 for g in long_ if g["kind"] == k) for k in ("explore", "combat", "dialogue")}

    top = [r for r in tool_recs if not r.get("nested")]
    reads = [r for r in top if r["tool"] not in ACTION_TOOLS and r["tool"] not in BATCH_TOOLS]
    rt = [r["ms"] - r.get("job_ms", 0) for r in top]
    n_actions = sum(a.get("steps", 1) for a in acts if a["tool"] not in NOT_APM)

    tt = think_times(turns)
    samp = [s for s in samples if s.get("type", "sample") == "sample"]
    busy = sum(1 for s in samp if s.get("moving") or s.get("job"))
    dlg = sum(1 for s in samp if s.get("dlg"))
    pct = (lambda n: round(100.0 * n / len(samp), 1)) if samp else (lambda n: None)

    med_explore = percentile(by_kind["explore"], 50)
    read_med = percentile([r["ms"] for r in reads], 50)
    return {
        "start": round(start, 1), "end": round(end, 1), "minutes": round(minutes, 2),
        "source": source,
        "idle_s": {"all": stats(g["s"] for g in gl), **{k: stats(v) for k, v in by_kind.items()}},
        "long_gaps": {"threshold_s": LONG_GAP_S, "total": len(long_), **long_kind,
                      "worst": sorted(long_, key=lambda g: -g["s"])[:5]},
        "think_s": stats(tt),
        "turns": len(tt),
        "tool_rt_ms": {"all": stats(rt, 1), "read": stats((r["ms"] for r in reads), 1),
                       "rpc": stats((r.get("rpc_ms", 0) / max(r.get("rpc_n", 1), 1) for r in top if r.get("rpc_n")), 1)},
        "tool_calls": len(top),
        "actions": n_actions,
        "apm": round(n_actions / minutes, 2),
        "busy_pct": pct(busy), "dialogue_pct": pct(dlg), "busy_or_dialogue_pct": pct(
            sum(1 for s in samp if s.get("moving") or s.get("job") or s.get("dlg"))),
        "samples": len(samp),
        "targets": {
            "median_idle_explore_s": _target(None if med_explore is None else round(med_explore, 2),
                                             TARGETS["median_idle_explore_s"]),
            "long_gaps_non_dialogue": {"value": long_kind["explore"] + long_kind["combat"],
                                       "target": TARGETS["long_gaps_non_dialogue"],
                                       "pass": (long_kind["explore"] + long_kind["combat"]) == 0 if gl else None},
            "bridge_rt_median_ms": _target(None if read_med is None else round(read_med, 1),
                                           TARGETS["bridge_rt_median_ms"]),
        },
    }


def summarize_files(metrics_path: Path | None, turns_path: Path | None,
                    start: float | None = None, end: float | None = None) -> dict:
    recs = read_jsonl(metrics_path)
    tools = [r for r in recs if r.get("type") == "tool"]
    samples = [r for r in recs if r.get("type") == "sample"]
    return summarize(tools, samples, read_jsonl(turns_path), start, end)


def write_summary(summary: dict, out_path: Path, history_path: Path | None, session: str) -> None:
    out_path.write_text(json.dumps(dict(summary, session=session), indent=1))
    if history_path:
        t = summary.get("targets", {})
        row = {"session": session, "start": summary.get("start"), "minutes": summary.get("minutes"),
               "idle_median_s": summary["idle_s"]["all"].get("median"),
               "idle_p90_s": summary["idle_s"]["all"].get("p90"),
               "idle_explore_median_s": summary["idle_s"]["explore"].get("median"),
               "long_gaps": summary["long_gaps"]["total"], "long_gaps_dialogue": summary["long_gaps"]["dialogue"],
               "think_median_s": summary["think_s"].get("median"), "rt_read_median_ms": summary["tool_rt_ms"]["read"].get("median"),
               "rt_read_p90_ms": summary["tool_rt_ms"]["read"].get("p90"), "apm": summary.get("apm"),
               "busy_pct": summary.get("busy_pct"),
               "pass": {k: v.get("pass") for k, v in t.items()}}
        with open(history_path, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def live_view(tools: list[dict], samples: list[dict], turns: list[dict], now: float,
              window_s: float = 600.0, timeline_s: float = 300.0) -> dict:
    """What the dashboard's latency panel shows: current idle gap, rolling stats, APM, a timeline."""
    lo = now - window_s
    recent_tools = [r for r in tools if r.get("t1", 0) >= lo]
    recent_samples = [s for s in samples if s.get("t", 0) >= lo]
    recent_turns = [r for r in turns if r.get("t", 0) >= lo]
    first = min([r["t0"] for r in recent_tools] + [x["t"] for x in recent_samples] + [r["t"] for r in recent_turns],
                default=lo)
    s = summarize(recent_tools, recent_samples, recent_turns, start=max(lo, first), end=now)  # young session: real span
    acts = action_intervals(tools) or stream_actions(turns)
    last_end = max((a["t1"] for a in acts), default=None)
    last_busy = max((x["t"] for x in samples if x.get("moving") or x.get("job")), default=None)
    ref = max([t for t in (last_end, last_busy) if t is not None], default=None)
    cur = samples[-1] if samples else {}
    busy_now = bool(cur.get("moving") or cur.get("job")) and now - cur.get("t", 0) < 5
    in_dlg = bool(cur.get("dlg")) and now - cur.get("t", 0) < 5
    # Timeline: 5-s buckets, "act" if an action interval or busy sample overlaps it, "dlg", else "idle".
    lo_t = now - timeline_s
    buckets = []
    step = 5.0
    tl_acts = [a for a in acts if a["t1"] >= lo_t]
    tl_s = [x for x in samples if x.get("t", 0) >= lo_t]
    t = lo_t
    while t < now:
        b = t + step
        state = "idle"
        if any(a["t0"] < b and a["t1"] > t for a in tl_acts) or any(t <= x["t"] < b and (x.get("moving") or x.get("job")) for x in tl_s):
            state = "act"
        elif any(t <= x["t"] < b and x.get("dlg") for x in tl_s):
            state = "dlg"
        if not tl_acts and not tl_s:
            state = "none"
        buckets.append(state)
        t = b
    return {
        "now": now,
        "current_idle_s": None if busy_now or ref is None else round(now - ref, 1),
        "busy_now": busy_now, "in_dialogue": in_dlg, "in_combat": bool(cur.get("combat")),
        "window_s": window_s,
        "idle_median_s": s["idle_s"]["all"].get("median"), "idle_p90_s": s["idle_s"]["all"].get("p90"),
        "idle_explore_median_s": s["idle_s"]["explore"].get("median"),
        "think_median_s": s["think_s"].get("median"),
        "rt_read_median_ms": s["tool_rt_ms"]["read"].get("median"), "rt_read_p90_ms": s["tool_rt_ms"]["read"].get("p90"),
        "apm": s["apm"], "busy_pct": s["busy_pct"], "long_gaps": s["long_gaps"],
        "targets": s["targets"],
        "timeline": {"step_s": step, "buckets": buckets},
    }
