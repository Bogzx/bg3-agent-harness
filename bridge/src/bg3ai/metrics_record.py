"""Latency metrics, bridge side: the Recorder (one record per tool call), the activity sampler and the game
memory sampler (record shapes: metrics.py). Stdlib only. metrics.py re-exports these names."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterable

OPS_MAX = 12  # tool records list their evals ("ops") when they made at most this many
BG_DEFAULT_S = 10.0  # an async job with no ETA counts as running this long unless its end is seen
BG_GRACE_S = 1.5     # ETAs are estimates: an async job counts as running a little past its ETA
MOVE_EPS_M = 0.25  # a party member moved at least this far since the previous sample → moving


class Recorder:
    """Collects RPC/job timings from game.Game and writes one record per tool call."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.rpcs: deque = deque(maxlen=5000)   # (t0, t1, op, owner): owner = the tool thread it ran for
        self.wrpcs: deque = deque(maxlen=5000)  # the same for the interrupt watcher's connection (watch_view)
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

    def watch_view(self) -> "_WatchView":
        """The recorder for the watcher's own Game connection: its evals count in rt_n/watch_n, not rpc_n."""
        return _WatchView(self)

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
            wrpc = [(a, b) for a, b, _, o in self.wrpcs if o == me and a >= t0 - 0.001 and b <= t1 + 0.001]
            job_ms = sum((min(b, t1) - max(a, t0)) * 1000 for a, b, _, _ in self.jobs if b > t0 and a < t1)
            busy = job_ms > 0 or self.last_active >= t0 or self.active_now
            bg = None if nested else self._bg_open_at(t0, exclude_from=t0)
            started = not nested and any(t0 <= v[0] <= t1 + 0.001 for v in self.bg.values())
        rec = {"type": "tool", "tool": tool, "t0": round(t0, 3), "t1": round(t1, 3), "ms": round(ms, 1),
               "rpc_ms": round(sum((b - a) * 1000 for a, b, _ in rpc), 1), "rpc_n": len(rpc),
               "job_ms": round(max(job_ms, 0.0), 1), "busy": bool(busy),
               "rt_n": round_trips([(a, b) for a, b, _ in rpc] + wrpc)}
        if wrpc:
            rec["watch_n"] = len(wrpc)
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
               party_turn: bool | None = None, lead: list | None = None) -> dict:
        active = bool(moving or job)
        with self.lock:
            self.active_now = active
            if active:
                self.last_active = t
        rec = {"type": "sample", "t": round(t, 2), "moving": bool(moving), "job": int(job), "dlg": bool(dlg),
               "combat": bool(combat)}
        if combat and party_turn is not None:
            rec["pturn"] = bool(party_turn)
        if lead is not None:
            rec["lead"] = lead   # the selected member's [x, y, z]: the path the party walked, per second
        self.write(rec)
        return rec


class _WatchView:
    """Recorder.watch_view(): the parent's owner/adopt/jobs, RPCs kept apart (wrpcs)."""

    def __init__(self, parent: Recorder):
        self.parent = parent

    def owner_token(self) -> int:
        return self.parent.owner_token()

    def adopt(self, token: int | None) -> None:
        self.parent.adopt(token)

    def rpc_done(self, op: str, t0: float, t1: float) -> None:
        owner = self.parent.owner_token()
        with self.parent.lock:
            self.parent.wrpcs.append((t0, t1, op, owner))

    def job_done(self, op: str, t0: float, t1: float, status: str) -> None:
        self.parent.job_done(op, t0, t1, status)


def round_trips(spans: Iterable[tuple[float, float]]) -> int:
    """Serial round trips in a set of eval intervals: overlapping ones (a parallel read) count once."""
    n, end = 0, None
    for a, b in sorted(spans):
        if end is None or a >= end:
            n += 1
            end = b
        else:
            end = max(end, b)
    return n


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
                nz = m.get("noesis")
                if isinstance(nz, dict):  # client: bg3le's Noesis release counters (BACKLOG "parked" watch)
                    for k in ("parked", "leaked", "gen"):
                        if isinstance(nz.get(k), (int, float)):
                            rec[f"noesis_{k}"] = nz[k]
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
        pos = a.get("pos")
        pos = pos if isinstance(pos, dict) else {}
        pt = a.get("party_turn")  # absent with an older mod
        lead = pos.get(a.get("active"))  # absent with an older mod (no `active`)
        rec.sample(time.time(), moved(prev, pos), int(a.get("jobs") or 0), bool(a.get("in_dialogue")),
                   bool(a.get("in_combat")), None if pt is None else bool(pt),
                   lead if isinstance(lead, list) and len(lead) == 3 else None)
        prev = pos
