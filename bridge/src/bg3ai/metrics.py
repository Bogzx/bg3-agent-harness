"""Latency metrics: does the AI play like a skilled human, live?

Stdlib only: the bridge (recording), runner/play.py (stream timing, session summary) and the dashboard
(live panel) all import it.

Three record streams, all JSONL:
* Sxxx.metrics.jsonl (written by the bridge, path in $BG3AI_METRICS_PATH, default runtime/metrics.jsonl):
    {"type":"tool", "tool", "t0", "t1", "ms", "rpc_ms", "rpc_n", "job_ms", "busy", "nested", "ok", "bg", "async"}
      one per tool call. t0/t1 are wall times. ms is the bridge round trip of the whole tool. rpc_ms is the
      time spent in Lua RPCs (TCP eval round trips). job_ms is the time spent waiting on game jobs
      (walking, casting...). nested marks the steps inside do_actions. busy means the party was visibly
      doing something during the call. bg (top-level calls only) is how many background activities
      (async jobs, action queues) were already running when the call started; async marks a call that
      started one and returned while it runs (move_to, travel_to, queue_actions, wait=false...).
    {"type":"sample", "t", "moving", "job", "dlg", "combat", "pturn"} / {"type":"sample", "t", "load": true}
      ~1/s from the activity sampler (nav.activity on its own connection). pturn (in combat only, newer mods):
      a party member has the active turn; false means enemies/NPCs are acting and the agent can only wait.
      load: nav.activity did not answer (a loading screen: load_game, an area transition, a fast travel).
* Sxxx.turns.jsonl (written by the runner from claude's stream-json, arrival times):
    {"type":"use", "t", "id", "name", "steps"} / {"type":"result", "t", "id"} / {"type":"assistant", "t"}
* Sxxx.latency.json: summarize() at session end. One line per session is also appended to
  runtime/sessions/<playthrough>/latency_history.jsonl.

Definitions:
* action tools: tools that make the party do something visible (ACTION_TOOLS). Inside do_actions the
  steps count, not the wrapper.
* idle gap: from the end of one action to the start of the next. It's tagged "dialogue" when the next
  action answers a dialogue or roll (or the party is in dialogue during the gap), "combat" when the
  party is in combat during the gap, else "explore". Its idle time "s" excludes the seconds the party was
  visibly busy (an async walk) and, in turn-based combat, the seconds it was NOT a party member's turn
  (enemy turns: a human waits through them too). So a combat gap's "s" is the party-turn idle, the agent's
  own think time on its turn; "gap_s" keeps the raw time. Whose turn: from the samples' pturn; for records
  without it, a top-level wait_for in combat that blocked >= WAIT_TURN_MIN_S counts the gap up to its end
  as enemy turns (the agent ended its turn and waited for the next one). Loading screens are left out of
  "s" the same way, in every kind of gap ("load_s"): the time of a top-level load_game call, and the time
  the sampler got no answer (load samples, each vouching for up to LOAD_SPAN_S since the one before).
* wrap-up: from the first "⏱ … WRAP UP NOW" (or "⏱ SESSION OVER") clock line in a tool result to the session end
  (a "wrapup" record in Sxxx.turns.jsonl). Gaps in it are kind "wrapup" (save + notes, not play): reported as
  idle_s.wrapup and wrapup_s, left out of idle_s.all, long gaps and the targets. A gap that straddles the
  first clock line counts up to it.
* think time: from a tool_result reaching the model to its next assistant message (stream-json arrival).
* busy %: share of samples where a party member moved or a game job was running. Dialogue share is
  reported separately: the server can't tell playback from waiting on an answer.
* pipelining %: share of the agent's own action calls (top level: action tools, macros, do_actions,
  queue_actions) issued ahead, like a human clicking ahead: while another job or queue of the party was
  still running (the bridge's bg field; for older records without it, a busy sample in the 1.5 s before
  the call and after the previous top-level action ended), or, in combat, during an enemy turn (the
  plan is sent while enemies act and waits for the party's turn: the last sample before the call has
  pturn false). Dialogue answers (DIALOGUE_TOOLS, or the party in dialogue at the call) are left out:
  the game waits on the agent there and nothing can run to overlap ("dialogue" counts them). strict_pct
  is the older definition (bg only, over every issued call; S015: 2%, versus 23.5% with this one).
  async % is the share of the issued calls that returned at once while their own job kept running.
* think overlap %: of the agent's own working time out of combat and dialogue (from the end of one
  top-level call to the start of the next, plus observation calls; not wait_for or blocking actions),
  the share of samples where the party was busy: the character acts while the agent thinks.
"""

from __future__ import annotations

import json
import os
import re
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
QUEUE_TOOL = "queue_actions"
# Not observations, so not in the read round-trip target: waits on the game (wait_for blocks until a job ends,
# S009: 26 calls, median 8.6 s), save/load (seconds by nature), screen captures, queue control.
NOT_READ = {"wait_for", "wait", "save_game", "load_game", "screenshot", "zoom", QUEUE_TOOL, "cancel_queue"}
OPS_MAX = 12  # tool records list their evals ("ops") when they made at most this many
# Top-level calls that issue actions (pipelining is measured over these); waits don't count.
ISSUE_TOOLS = (ACTION_TOOLS | BATCH_TOOLS | {QUEUE_TOOL}) - NOT_APM
BG_DEFAULT_S = 10.0  # an async job with no ETA counts as running this long unless its end is seen
BG_GRACE_S = 1.5     # ETAs are estimates: an async job counts as running a little past its ETA
PIPE_SAMPLE_S = 1.5  # fallback: a busy sample at most this long before the call
LONG_GAP_S = 10.0
SAMPLE_SPAN_S = 1.5  # a busy activity sample vouches for at most this long before it (sampler: 1 s)
WAIT_TURN_MIN_S = 1.0  # fallback: an in-combat wait_for that blocked this long waited through enemy turns
LOAD_SPAN_S = 5.0  # a load sample covers this long before it (a failed poll: up to its 3 s timeout + 1 s)
MOVE_EPS_M = 0.25  # a party member moved at least this far since the previous sample → moving

TARGETS = {
    "median_idle_explore_s": 2.0,   # median idle gap outside combat (and outside dialogue choices)
    "long_gaps_non_dialogue": 0,    # gaps > 10 s, except deliberate dialogue choices
    "bridge_rt_median_ms": 100.0,   # median round trip of observation tools
}


# The bridge's clock line once wrap-up has begun (server.py session clock), not the playbooks quoting it.
WRAPUP_RE = re.compile(r"⏱ (?:[^\n·]*·\s*\d+m left: WRAP UP NOW|SESSION OVER)")


def _result_text(c: dict) -> str:
    body = c.get("content")
    if isinstance(body, list):
        return " ".join(b.get("text", "") for b in body if isinstance(b, dict) and isinstance(b.get("text"), str))
    return body if isinstance(body, str) else ""


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
        self.rpcs: deque = deque(maxlen=5000)   # (t0, t1, op, owner): owner = the tool thread it ran for
        self._tl = threading.local()            # .owner: the tool thread a helper thread works for
        self.jobs: deque = deque(maxlen=500)    # (t0, t1, op, status)
        self.last_active = 0.0                  # wall time of the latest busy sample
        self.active_now = False
        self.bg: dict[str, list] = {}           # key -> [t_start, t_end or None, t_expected_end or None]

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

    def owner_token(self) -> int:
        """Who this thread's RPCs count for: itself, or the tool thread it was lent to (adopt)."""
        return getattr(self._tl, "owner", None) or threading.get_ident()

    def adopt(self, token: int | None) -> None:
        """Game.parallel helper threads: count this thread's RPCs for the tool thread `token` (None: stop)."""
        self._tl.owner = token

    def rpc_done(self, op: str, t0: float, t1: float) -> None:
        owner = self.owner_token()
        with self.lock:
            self.rpcs.append((t0, t1, op, owner))

    def job_done(self, op: str, t0: float, t1: float, status: str) -> None:
        with self.lock:
            self.jobs.append((t0, t1, op, status))

    def bg_start(self, key: str, eta_s: float | None = None, t: float | None = None) -> None:
        """A background activity began: an async job (j12) or a character's action queue."""
        t = time.time() if t is None else t
        exp = t + (float(eta_s) + BG_GRACE_S if eta_s is not None else BG_DEFAULT_S)
        with self.lock:
            old = self.bg.get(key)
            if old and old[1] is None and old[0] <= t:  # a queue that got more steps: still the same run
                old[2] = None if key.startswith("queue") else max(old[2] or exp, exp)
                return
            self.bg[key] = [t, None, None if key.startswith("queue") else exp]
            if len(self.bg) > 200:  # forget the oldest finished ones
                for k, _ in sorted(self.bg.items(), key=lambda kv: kv[1][0])[:100]:
                    if self.bg[k][1] is not None or (self.bg[k][2] or t) < t:
                        del self.bg[k]

    def bg_end(self, key: str, t: float | None = None) -> None:
        t = time.time() if t is None else t
        with self.lock:
            v = self.bg.get(key)
            if v and v[1] is None:
                v[1] = t

    def _bg_open_at(self, t: float, exclude_from: float | None = None) -> int:
        """Background activities running at time t (started before it, not yet ended)."""
        n = 0
        for t0, t1, exp in self.bg.values():
            if exclude_from is not None and t0 >= exclude_from:
                continue
            end = t1 if t1 is not None else exp
            if t0 < t and (end is None or end > t):
                n += 1
        return n

    def tool_done(self, tool: str, ms: float, nested: bool = False, ok: bool = True,
                  t1: float | None = None, extra: dict | None = None) -> dict | None:
        if tool in ("autoskip",):
            return None
        t1 = time.time() if t1 is None else t1
        t0 = t1 - ms / 1000.0
        me = self.owner_token()
        with self.lock:
            # Only this call's own RPCs: queue workers, wait polls and other tools share the recorder.
            rpc = [(a, b, op) for a, b, op, o in self.rpcs if o == me and a >= t0 - 0.001 and b <= t1 + 0.001]
            job_ms = sum((min(b, t1) - max(a, t0)) * 1000 for a, b, _, _ in self.jobs if b > t0 and a < t1)
            busy = job_ms > 0 or self.last_active >= t0 or self.active_now
            bg = None if nested else self._bg_open_at(t0, exclude_from=t0)
            started = not nested and any(t0 <= v[0] <= t1 + 0.001 for v in self.bg.values())
        rec = {"type": "tool", "tool": tool, "t0": round(t0, 3), "t1": round(t1, 3), "ms": round(ms, 1),
               "rpc_ms": round(sum((b - a) * 1000 for a, b, _ in rpc), 1), "rpc_n": len(rpc),
               "job_ms": round(max(job_ms, 0.0), 1), "busy": bool(busy)}
        if nested:
            rec["nested"] = True
        if rpc and len(rpc) <= OPS_MAX and (not nested or not ok):
            # Which evals the call made (ops joined by + ran in one eval), for read-latency triage. A failed
            # nested call keeps them too: S038's jump_to inside do_actions hung in nav.landing unnamed.
            rec["ops"] = [[op[:60], round((b - a) * 1000, 1)] for a, b, op in rpc]
        if not ok:
            rec["ok"] = False
        if bg is not None:
            rec["bg"] = bg
        if started:
            rec["async"] = True
        if extra:
            rec.update(extra)  # e.g. wait_for's noop: why it never blocked
        self.write(rec)
        return rec

    def load_sample(self, t: float) -> dict:
        """No answer from the game (loading screen): not idle time (see gaps)."""
        with self.lock:
            self.active_now = False
        rec = {"type": "sample", "t": round(t, 2), "load": True}
        self.write(rec)
        return rec

    def sample(self, t: float, moving: bool, job: int, dlg: bool, combat: bool,
               party_turn: bool | None = None) -> dict:
        active = bool(moving or job)
        with self.lock:
            self.active_now = active
            if active:
                self.last_active = t
        rec = {"type": "sample", "t": round(t, 2), "moving": bool(moving), "job": int(job), "dlg": bool(dlg),
               "combat": bool(combat)}
        if combat and party_turn is not None:
            rec["pturn"] = bool(party_turn)
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


# Client game states that are play. Anything else (LoadLevel, LoadSession, Sync, ...) is a loading screen even
# when the server still answers nav.activity (S023 06:29:38: ~17 s of the camp -> trail loading screen counted
# as idle). "Save" is the ~1 s of a save (ours or the game's).
PLAY_STATES = {"Running", "Paused", "Save"}
_GS_CODE = "return tostring(Ext.Utils.GetGameState and Ext.Utils.GetGameState() or '?')"


def client_state(game) -> str | None:
    """The client's game state (None: unknown, e.g. an older Game object or no answer)."""
    ev = getattr(game, "eval", None)
    if ev is None:
        return None
    try:
        out = ev(_GS_CODE, context="client", timeout=3.0)
    except Exception:  # noqa: BLE001 - the caller treats it as unknown
        return None
    out = str(out or "").strip().strip('"')
    return out if out and out not in ("?", "nil") else None


# Memory samples (S038 20:48: the game's RSS went 5 -> 13 GB in ~30 s and the OOM kill took every agent down; the
# per-minute ramwatch line could not say whether it was our Lua or the engine):
#   {"type":"mem", "t", "rss_mb", "lua_server_mb"?, "lua_client_mb"?, "high"?, "why"?}
# every MEM_EVERY_S, and at once (why="rss_jump") when the game's RSS grew MEM_JUMP_MB since the last one; the RSS
# is read from /proc every sample (one small file read), the Lua heaps from sys.mem in both contexts.
MEM_EVERY_S = 60.0
MEM_JUMP_MB = 1024
GAME_COMM = "bg3"


def game_pid(proc: Path = Path("/proc")) -> int | None:
    """The running game's pid (its comm is "bg3"), or None. The Steam runtime starts two "bg3" processes, a ~60 MB
    parent and the real game as its child (S039: the mem records said rss_mb 59 for a 5 GB game): the biggest wins."""
    me = os.getuid()
    best: tuple[int, int] | None = None
    try:
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                if d.stat().st_uid == me and (d / "comm").read_text().strip() == GAME_COMM:
                    cand = (rss_mb(int(d.name), proc) or 0, int(d.name))
                    best = cand if best is None or cand > best else best
            except OSError:
                continue
    except OSError:
        pass
    return best[1] if best else None


def rss_mb(pid: int | None, proc: Path = Path("/proc")) -> int | None:
    if not pid:
        return None
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


PID_RECHECK_S = 60.0


class MemSampler:
    """Decides when to write a mem record; rss() and lua(context) are injectable for tests."""

    def __init__(self, rec: Recorder, game, every_s: float = MEM_EVERY_S, jump_mb: int = MEM_JUMP_MB,
                 rss=None, now=time.time):
        self.rec, self.game, self.every_s, self.jump_mb, self.now = rec, game, every_s, jump_mb, now
        self._pid: int | None = None
        self._pid_t = 0.0
        self.rss = rss or self._game_rss
        self.last_t = now()  # the first record a minute in (a fresh bridge's game is still loading)
        self.last_rss: int | None = None

    def _game_rss(self) -> int | None:
        r = rss_mb(self._pid)
        if r is None or self.now() - self._pid_t >= PID_RECHECK_S:
            # again now and then: at a game start the parent can be the bigger one for a moment
            self._pid, self._pid_t = game_pid(), self.now()
            r = rss_mb(self._pid)
        return r

    def lua(self, context: str) -> dict | None:
        try:
            r = self.game.rpc("sys.mem", context=context, timeout=2.0)
        except Exception:  # noqa: BLE001 - an older mod, a load, or the game frozen
            return None
        return r if isinstance(r, dict) else None

    def tick(self) -> dict | None:
        t = self.now()
        rss = self.rss()
        jump = rss is not None and self.last_rss is not None and rss - self.last_rss >= self.jump_mb
        if not jump and t - self.last_t < self.every_s:
            return None
        rec: dict = {"type": "mem", "t": round(t, 2), "rss_mb": rss}
        if jump:
            rec["why"] = "rss_jump"
        for ctx in ("server", "client"):
            m = self.lua(ctx)
            if m and isinstance(m.get("lua_mb"), (int, float)):
                rec[f"lua_{ctx}_mb"] = m["lua_mb"]
                if m.get("high"):
                    rec["high"] = True
        self.last_t, self.last_rss = t, rss
        self.rec.write(rec)
        return rec


def sampler_loop(rec: Recorder, game, stop: threading.Event, interval: float = 1.0, mem=None) -> None:
    """Poll nav.activity on a dedicated Game connection and record busy/idle samples; a load sample when the
    client shows a loading screen (its game state is not play) or the server doesn't answer. Memory records
    (MemSampler) ride along."""
    prev = None
    mem = mem if mem is not None else MemSampler(rec, game)
    while not stop.wait(interval):
        try:
            mem.tick()
        except Exception:  # noqa: BLE001 - never let a memory sample stop the activity sampler
            pass
        gs = client_state(game)
        if gs is not None and gs not in PLAY_STATES:
            prev = None
            rec.load_sample(time.time())
            continue
        try:
            a = game.rpc("nav.activity", context="server", timeout=3.0)
        except Exception:  # noqa: BLE001 - loading screens, reloads, old mod without the op
            prev = None
            rec.load_sample(time.time())
            continue
        if not isinstance(a, dict):
            continue
        pos = a.get("pos") if isinstance(a.get("pos"), dict) else {}
        pt = a.get("party_turn")  # absent with an older mod
        rec.sample(time.time(), moved(prev, pos), int(a.get("jobs") or 0), bool(a.get("in_dialogue")),
                   bool(a.get("in_combat")), None if pt is None else bool(pt))
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
        msg = ev.get("message") or {}
        a: dict = {"type": "assistant", "t": round(t, 3)}
        est = _content_tokens(msg.get("content"))
        if msg.get("id"):
            a["mid"] = msg["id"]
        if est:
            a["est"] = est
        out.append(a)
        for c in msg.get("content") or []:
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
                if WRAPUP_RE.search(_result_text(c)):
                    out.append({"type": "wrapup", "t": round(t, 3)})
    elif typ == "system" and ev.get("subtype") == "thinking_tokens":
        out.append({"type": "think_tok", "t": round(t, 3), "n": ev.get("estimated_tokens") or 0})
    elif typ == "result":  # the run's end: exact API time and output tokens for the whole session
        u = ev.get("usage") or {}
        out.append({"type": "session_result", "t": round(t, 3), "api_ms": ev.get("duration_api_ms"),
                    "out": u.get("output_tokens"), "turns": ev.get("num_turns")})
    return out


def _content_tokens(content: Any) -> int:
    """Output tokens of one stream-json assistant event, estimated from its size (chars / 3.5): the stream's
    per-message usage.output_tokens is partial. Thinking text is usually redacted; think_tok records count it."""
    n = 0
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            n += len(c.get("text") or "")
        elif c.get("type") == "tool_use":
            n += len(json.dumps(c.get("input"), separators=(",", ":"))) + len(c.get("name") or "") + 10
        elif c.get("type") == "thinking":
            n += len(c.get("thinking") or "")
    return round(n / 3.5)


def api_speed(turns: list[dict]) -> dict:
    """API speed next to think time, so an API slowdown shows up as such (impl-act: think rose 1.2 -> 2.9 s
    while tokens per turn stayed flat and ms per output token went 13 -> 22). Per model turn: API seconds =
    the previous tool result to the turn's last streamed block; output tokens = the content estimate plus the
    thinking estimate. The session's final `result` event (exact duration_api_ms / output_tokens / num_turns)
    replaces the estimate when present. The estimate reads ~1.4x high on ms/token (thinking is redacted in the
    stream; S003 21 vs 12.7 exact, S013 31 vs 21.9) but follows the same trend."""
    exact = [r for r in turns if r.get("type") == "session_result" and r.get("api_ms") and r.get("out")]
    if exact:
        r = exact[-1]
        n = r.get("turns") or 0
        return {"source": "result", "ms_per_out_tok": round(r["api_ms"] / r["out"], 1),
                "api_s_per_turn": round(r["api_ms"] / 1000.0 / n, 2) if n else None,
                "out_tok_per_turn": round(r["out"] / n) if n else None, "turns": n}
    per: list[dict] = []
    last_result, cur, think = None, None, 0
    for r in sorted(turns, key=lambda r: r.get("t", 0)):
        typ = r.get("type")
        if typ == "result":
            last_result, cur = r["t"], None
        elif typ == "think_tok":
            think = max(think, r.get("n") or 0)
        elif typ == "assistant" and last_result is not None:
            mid = r.get("mid")
            if cur is None or (mid and cur["mid"] and mid != cur["mid"]):
                cur = {"mid": mid, "t0": last_result, "t1": r["t"], "tok": think}
                think = 0
                per.append(cur)
            cur["t1"] = r["t"]
            cur["tok"] += r.get("est") or 0
    per = [p for p in per if p["tok"] > 0]
    if not per:
        return {"source": "none", "ms_per_out_tok": None, "api_s_per_turn": None, "out_tok_per_turn": None,
                "turns": 0}
    secs = sum(p["t1"] - p["t0"] for p in per)
    toks = sum(p["tok"] for p in per)
    return {"source": "stream", "ms_per_out_tok": round(1000.0 * secs / toks, 1),
            "api_s_per_turn": round(secs / len(per), 2), "out_tok_per_turn": round(toks / len(per)),
            "turns": len(per)}


class StreamTiming:
    """Runner helper: timestamps stream-json events into Sxxx.turns.jsonl."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        self.wrapup_seen = False

    def on_event(self, ev: dict, t: float | None = None) -> None:
        recs = stream_records(ev, time.time() if t is None else t)
        if self.wrapup_seen:  # only the first clock line marks the start of wrap-up
            recs = [r for r in recs if r["type"] != "wrapup"]
        elif any(r["type"] == "wrapup" for r in recs):
            self.wrapup_seen = True
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


def wrapup_start(turns: list[dict]) -> float | None:
    """Arrival time of the first WRAP UP NOW clock line (None: the session never reached wrap-up)."""
    return min((r["t"] for r in turns if r.get("type") == "wrapup" and "t" in r), default=None)


def wrapup_from_stream(stream_path: Path | None, turns: list[dict]) -> list[dict]:
    """Backfill for sessions recorded before "wrapup" records: find the first tool_result with the clock line in
    the raw stream (Sxxx.jsonl, no timestamps) and time it by its "result" record in turns.jsonl."""
    if wrapup_start(turns) is not None or not stream_path:
        return []
    at = {r.get("id"): r["t"] for r in turns if r.get("type") == "result" and "t" in r}
    for ev in read_jsonl(stream_path):
        if ev.get("type") != "user":
            continue
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in at \
                    and WRAPUP_RE.search(_result_text(c)):
                return [{"type": "wrapup", "t": at[c["tool_use_id"]]}]
    return []


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


def _spans(samples: list[dict], a: float, b: float, pred, span: float = SAMPLE_SPAN_S) -> list[tuple[float, float]]:
    """Parts of [a, b] vouched for by samples matching pred: a sample covers the time since the sample before
    it, at most `span`."""
    out = []
    prev_t = None
    for s in samples:
        t = s.get("t")
        if t is None:
            continue
        if t > a and pred(s):
            lo = max(t - min(t - prev_t if prev_t is not None else span, span), a)
            hi = min(t, b)
            if hi > lo:
                out.append((lo, hi))
        prev_t = t
        if t >= b + span:
            break
    return out


def _union_len(spans: Iterable[tuple[float, float]]) -> float:
    total, end = 0.0, None
    for lo, hi in sorted(spans):
        if end is None or lo > end:
            total += hi - lo
            end = hi
        elif hi > end:
            total += hi - end
            end = hi
    return total


def _is_busy(s: dict) -> bool:
    return bool(s.get("moving") or s.get("job"))


def _busy_in(samples: list[dict], a: float, b: float) -> float:
    """Seconds of [a, b] in which the party visibly did something: an async walk returns at once, so its whole
    walk lies inside the gap after it and is not idle."""
    return _union_len(_spans(samples, a, b, _is_busy))


def _enemy_turn_spans(samples: list[dict], waits: list[dict], a: float, b: float) -> list[tuple[float, float]]:
    """Parts of [a, b] in turn-based combat when no party member had the turn (enemies acting). From the
    samples' pturn when the gap has any; else from top-level wait_for calls that blocked in combat."""
    inside = [s for s in samples if a <= s.get("t", 0) <= b + SAMPLE_SPAN_S]
    if any("pturn" in s for s in inside):
        return _spans(samples, a, b, lambda s: s.get("combat") and s.get("pturn") is False)
    if not any(s.get("combat") for s in inside):
        return []
    out = []
    for w in waits:
        if a <= w["t0"] < b and w["t1"] - w["t0"] >= WAIT_TURN_MIN_S:
            out.append((a, min(w["t1"], b)))
    return out


def gaps(actions: list[dict], samples: list[dict], tools: list[dict] | None = None,
         wrap_t: float | None = None) -> list[dict]:
    """Idle gaps between visible actions: "s" is the idle time (the raw gap "gap_s" minus "busy_s", the
    seconds the party was busy with an earlier async action, and "turn_s", the seconds in combat when it was
    not a party member's turn); "own_s" (combat gaps) is the same as "s": the agent's own time on its turn.
    tools: the bridge tool records (fallback for whose turn it was; see _enemy_turn_spans).
    wrap_t: start of the wrap-up phase: gaps from it on are kind "wrapup", a gap across it is cut at it."""
    out = []
    samples = sorted((s for s in samples if "t" in s), key=lambda s: s["t"])
    waits = sorted((r for r in tools or () if r.get("tool") == "wait_for" and not r.get("nested") and "t0" in r),
                   key=lambda r: r["t0"])
    loads = [(r["t0"], r["t1"]) for r in tools or () if r.get("tool") == "load_game" and not r.get("nested")
             and "t0" in r]
    for prev, nxt in zip(actions, actions[1:]):
        a, b = prev["t1"], nxt["t0"]
        wrap = wrap_t is not None and a >= wrap_t
        if wrap_t is not None and a < wrap_t < b:
            b = wrap_t  # the play part of a gap that runs into wrap-up
        g = b - a
        if g <= 0:
            continue
        inside = _samples_between(samples, a, b)
        busy_sp = _spans(samples, a, b, _is_busy)
        busy = min(_union_len(busy_sp), g)
        if wrap:
            kind = "wrapup"
        elif nxt["tool"] in DIALOGUE_TOOLS or any(s.get("dlg") for s in inside):
            kind = "dialogue"
        elif any(s.get("combat") for s in inside) or nxt["tool"] == "end_turn":
            kind = "combat"
        else:
            kind = "explore"
        r = {"t": round(a, 2), "s": round(g - busy, 2), "gap_s": round(g, 2), "busy_s": round(busy, 2),
             "kind": kind, "next": nxt["tool"]}
        turn_sp = _enemy_turn_spans(samples, waits, a, b) if kind in ("combat", "dialogue") else []
        load_sp = [(max(lo, a), min(hi, b)) for lo, hi in loads if lo < b and hi > a] \
            + _spans(samples, a, b, lambda s: s.get("load"), LOAD_SPAN_S)
        if turn_sp:
            excl = min(_union_len(busy_sp + turn_sp), g)
            r["turn_s"] = round(excl - busy, 2)  # enemy-turn seconds not already counted as busy
            r["s"] = round(g - excl, 2)
        if load_sp:
            excl = min(_union_len(busy_sp + turn_sp + load_sp), g)
            r["load_s"] = round(excl - g + r["s"], 2)  # loading-screen seconds not already left out
            r["s"] = round(g - excl, 2)
        if kind == "combat":
            r["own_s"] = r["s"]
        out.append(r)
    return out


def _last_sample(samples: list[dict], t: float) -> dict:
    """The newest activity sample before t ({} if none); samples sorted by t."""
    last: dict = {}
    for s in samples:
        if s["t"] >= t:
            break
        last = s
    return last


def pipelining(tools: list[dict], samples: list[dict]) -> dict:
    """How often the agent issued its next action ahead: while the party was still busy with an earlier one,
    or during an enemy turn (a combat turn planned while enemies act). Dialogue answers are left out."""
    top = sorted((r for r in tools if not r.get("nested") and "t0" in r), key=lambda r: r["t0"])
    issued = [r for r in top if r.get("tool") in ISSUE_TOOLS]
    samples = sorted((s for s in samples if "t" in s), key=lambda s: s["t"])
    busy_t = [s["t"] for s in samples if s.get("moving") or s.get("job")]
    n_pipe = n_async = n_strict = n_dlg = n_ahead = 0
    source = "bridge" if any("bg" in r for r in issued) else "samples"
    prev_end = None
    for r in issued:
        if "bg" in r:
            busy = r["bg"] > 0
        else:
            lo = max(r["t0"] - PIPE_SAMPLE_S, prev_end if prev_end is not None else float("-inf"))
            busy = any(lo < t < r["t0"] for t in busy_t)
        prev_end = r["t1"] if prev_end is None else max(prev_end, r["t1"])
        n_strict += bool(busy)
        n_async += bool(r.get("async"))
        last = _last_sample(samples, r["t0"])
        if r.get("tool") in DIALOGUE_TOOLS or last.get("dlg"):
            n_dlg += 1
            continue
        ahead = bool(last.get("combat")) and last.get("pturn") is False
        n_ahead += ahead and not busy
        n_pipe += busy or ahead
    n = len(issued)
    pct = (lambda k: round(100.0 * k / n, 1)) if n else (lambda k: None)
    m = n - n_dlg
    return {"issued": n, "dialogue": n_dlg, "pipelined": n_pipe,
            "pct": round(100.0 * n_pipe / m, 1) if m else None, "enemy_turn": n_ahead,
            "strict_pct": pct(n_strict), "async": n_async, "async_pct": pct(n_async),
            "queue_calls": sum(1 for r in issued if r.get("tool") == QUEUE_TOOL),
            "batch_calls": sum(1 for r in issued if r.get("tool") in BATCH_TOOLS),
            "source": source}


def think_overlap(tools: list[dict], samples: list[dict]) -> dict:
    """Out of combat and dialogue: the samples taken while the agent was working (between top-level calls,
    or in an observation call) and how many of them had the party busy."""
    top = sorted((r for r in tools if not r.get("nested") and "t0" in r), key=lambda r: r["t0"])
    blocked = [(r["t0"], r["t1"]) for r in top
               if r.get("tool") in ACTION_TOOLS or r.get("tool") in BATCH_TOOLS or r.get("tool") in NOT_READ]
    lo = top[0]["t0"] if top else 0.0
    hi = top[-1]["t1"] if top else 0.0
    n = n_busy = 0
    for s in samples:
        t = s.get("t")
        if t is None or not lo <= t <= hi or s.get("combat") or s.get("dlg") or s.get("load"):
            continue
        if any(a <= t < b for a, b in blocked):
            continue
        n += 1
        n_busy += bool(s.get("moving") or s.get("job"))
    return {"samples": n, "busy": n_busy, "pct": round(100.0 * n_busy / n, 1) if n else None}


def read_by_tool(reads: list[dict]) -> dict:
    """{tool: {n, median, max}} of the observation tools, slowest median first."""
    by: dict[str, list] = {}
    for r in reads:
        by.setdefault(r["tool"], []).append(r["ms"])
    out = {t: {"n": len(v), "median": round(percentile(v, 50), 1), "max": round(max(v), 1)} for t, v in by.items()}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["median"]))


# Observation reads for the calls/min mix (the round trips a result could have carried; 2026-10-10 task).
OBS_READS = {"look", "get_nearby", "get_frontier", "get_layout", "screenshot", "zoom", "get_status", "route", "wiki_search",
             "wiki_page", "look_at", "get_inventory", "get_combat_state", "get_dialogue", "get_journal", "get_map",
             "get_party", "recall", "ui_dump", "get_actions", "ability_preview", "get_level_up_options"}


def call_rates(turns: list[dict], tools: list[dict], start: float, end: float) -> dict:
    """Round trips per minute of play (wrap-up left out): every model tool call (Read/Edit/ToolSearch too: each is
    a think+call round trip), observation reads, action calls (incl. batches/queues). From the stream's tool uses;
    the bridge's top-level calls when there is no stream."""
    uses = [{"name": r.get("name"), "t": r["t"]} for r in turns if r.get("type") == "use" and "t" in r]
    src = "stream"
    if not uses:
        uses, src = [{"name": r["tool"], "t": r["t0"]} for r in tools if not r.get("nested")], "bridge"
    uses = [u for u in uses if start <= u["t"] < end]
    mins = max((end - start) / 60.0, 1e-9)
    reads = [u["name"] for u in uses if u["name"] in OBS_READS]
    acts = [u for u in uses if u["name"] in ACTION_TOOLS or u["name"] in BATCH_TOOLS or u["name"] == QUEUE_TOOL]
    by: dict[str, int] = {}
    for n in reads:
        by[n] = by.get(n, 0) + 1
    return {"source": src, "minutes": round(mins, 2), "calls": len(uses), "reads": len(reads), "action_calls": len(acts),
            "calls_pm": round(len(uses) / mins, 2), "reads_pm": round(len(reads) / mins, 2),
            "actions_pm": round(len(acts) / mins, 2),
            "reads_by": dict(sorted(by.items(), key=lambda kv: -kv[1])[:8])}


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
    wrap_t = wrapup_start(turns)
    if wrap_t is not None and not start <= wrap_t < end:
        wrap_t = None
    all_gl = gaps(acts, samples, tool_recs, wrap_t)
    gl = [g for g in all_gl if g["kind"] != "wrapup"]  # play only: wrap-up is save + notes
    wrap_gl = [g for g in all_gl if g["kind"] == "wrapup"]
    by_kind = {k: [g["s"] for g in gl if g["kind"] == k] for k in ("explore", "combat", "dialogue")}
    combat = [g for g in gl if g["kind"] == "combat"]
    long_ = [g for g in gl if g["s"] > LONG_GAP_S]
    long_kind = {k: sum(1 for g in long_ if g["kind"] == k) for k in ("explore", "combat", "dialogue")}

    top = [r for r in tool_recs if not r.get("nested")]
    reads = [r for r in top if r["tool"] not in ACTION_TOOLS and r["tool"] not in BATCH_TOOLS
             and r["tool"] not in NOT_READ]
    waits = [r for r in top if r["tool"] in ("wait_for",)]
    rt = [r["ms"] - r.get("job_ms", 0) for r in top]
    n_actions = sum(a.get("steps", 1) for a in acts if a["tool"] not in NOT_APM)

    tt = think_times(turns)
    samp = [s for s in samples if s.get("type", "sample") == "sample" and not s.get("load")]
    busy = sum(1 for s in samp if s.get("moving") or s.get("job"))
    dlg = sum(1 for s in samp if s.get("dlg"))
    pct = (lambda n: round(100.0 * n / len(samp), 1)) if samp else (lambda n: None)

    med_explore = percentile(by_kind["explore"], 50)
    read_med = percentile([r["ms"] for r in reads], 50)
    pipe = pipelining(tool_recs, samp)
    overlap = think_overlap(tool_recs, samp)
    return {
        "start": round(start, 1), "end": round(end, 1), "minutes": round(minutes, 2),
        "source": source,
        "idle_s": {"all": stats(g["s"] for g in gl), **{k: stats(v) for k, v in by_kind.items()},
                   # combat: the party-turn idle (own think time on its turn); this: the same gaps with the
                   # enemy turns left in (what the idle metric was before turns were excluded)
                   "combat_incl_enemy": stats(g["gap_s"] - g["busy_s"] for g in combat),
                   "wrapup": stats(g["s"] for g in wrap_gl)},
        # the wrap-up phase (first WRAP UP NOW clock line to the end), excluded from the idle/gap targets
        "wrapup_s": None if wrap_t is None else round(end - wrap_t, 1),
        "wrapup_at": None if wrap_t is None else round(wrap_t, 1),
        "enemy_turn_s": round(sum(g.get("turn_s", 0) for g in gl), 1),
        "loading_s": round(sum(g.get("load_s", 0) for g in gl), 1),  # loading screens left out of idle
        "long_gaps": {"threshold_s": LONG_GAP_S, "total": len(long_), **long_kind,
                      "wrapup": sum(1 for g in wrap_gl if g["s"] > LONG_GAP_S),
                      "worst": sorted(long_, key=lambda g: -g["s"])[:5]},
        "think_s": stats(tt),
        "api": api_speed(turns),
        "turns": len(tt),
        "tool_rt_ms": {"all": stats(rt, 1), "read": stats((r["ms"] for r in reads), 1),
                       "read_by_tool": read_by_tool(reads), "wait_for": stats((r["ms"] for r in waits), 1),
                       "rpc": stats((r.get("rpc_ms", 0) / max(r.get("rpc_n", 1), 1) for r in top if r.get("rpc_n")), 1)},
        # wait_for calls that never blocked (the state already answered it, or nothing could cause it)
        "wait_for_noop": {"n": sum(1 for r in waits if r.get("noop")), "of": len(waits),
                          "ms": round(sum(r["ms"] for r in waits if r.get("noop")), 1),
                          "by": {k: sum(1 for r in waits if r.get("noop") == k)
                                 for k in sorted({r["noop"] for r in waits if r.get("noop")})}},
        "tool_calls": len(top),
        "rates": call_rates(turns, tool_recs, start, wrap_t if wrap_t is not None else end),
        "actions": n_actions,
        "apm": round(n_actions / minutes, 2),
        "busy_pct": pct(busy), "dialogue_pct": pct(dlg), "busy_or_dialogue_pct": pct(
            sum(1 for s in samp if s.get("moving") or s.get("job") or s.get("dlg"))),
        "samples": len(samp),
        "pipelining": pipe,
        "pipelining_pct": pipe["pct"],
        "think_overlap": overlap,
        "think_overlap_pct": overlap["pct"],
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
    turns = read_jsonl(turns_path)
    if turns_path:  # older sessions: no "wrapup" record yet, find the clock line in the raw stream next to it
        stream = Path(str(turns_path).removesuffix(".turns.jsonl") + ".jsonl")
        if stream != Path(turns_path):
            turns += wrapup_from_stream(stream, turns)
    return summarize(tools, samples, turns, start, end)


def write_summary(summary: dict, out_path: Path, history_path: Path | None, session: str) -> None:
    out_path.write_text(json.dumps(dict(summary, session=session), indent=1))
    if history_path:
        t = summary.get("targets", {})
        row = {"session": session, "start": summary.get("start"), "minutes": summary.get("minutes"),
               "idle_median_s": summary["idle_s"]["all"].get("median"),
               "idle_p90_s": summary["idle_s"]["all"].get("p90"),
               "idle_explore_median_s": summary["idle_s"]["explore"].get("median"),
               "idle_party_turn_median_s": summary["idle_s"]["combat"].get("median"),
               "long_gaps": summary["long_gaps"]["total"], "long_gaps_dialogue": summary["long_gaps"]["dialogue"],
               "wrapup_s": summary.get("wrapup_s"),
               "think_median_s": summary["think_s"].get("median"),
               "ms_per_out_tok": (summary.get("api") or {}).get("ms_per_out_tok"),
               "api_s_per_turn": (summary.get("api") or {}).get("api_s_per_turn"),
               "rt_read_median_ms": summary["tool_rt_ms"]["read"].get("median"),
               "rt_read_p90_ms": summary["tool_rt_ms"]["read"].get("p90"), "apm": summary.get("apm"),
               "busy_pct": summary.get("busy_pct"), "pipelining_pct": summary.get("pipelining_pct"),
               "think_overlap_pct": summary.get("think_overlap_pct"),
               "calls_pm": (summary.get("rates") or {}).get("calls_pm"),
               "reads_pm": (summary.get("rates") or {}).get("reads_pm"),
               "actions_pm": (summary.get("rates") or {}).get("actions_pm"),
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
        "ms_per_out_tok": s["api"].get("ms_per_out_tok"), "api_s_per_turn": s["api"].get("api_s_per_turn"),
        "api_source": s["api"].get("source"),
        "rt_read_median_ms": s["tool_rt_ms"]["read"].get("median"), "rt_read_p90_ms": s["tool_rt_ms"]["read"].get("p90"),
        "apm": s["apm"], "busy_pct": s["busy_pct"], "pipelining_pct": s["pipelining_pct"],
        "think_overlap_pct": s["think_overlap_pct"],
        "calls_pm": s["rates"]["calls_pm"], "reads_pm": s["rates"]["reads_pm"], "actions_pm": s["rates"]["actions_pm"],
        "long_gaps": s["long_gaps"],
        "targets": s["targets"],
        "timeline": {"step_s": step, "buckets": buckets},
    }
