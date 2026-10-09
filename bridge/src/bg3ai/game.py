"""RPC access to the BG3AI mod running inside the game.

Every call evaluates `return Mods.BG3AI.BG3AI.rpc([==[<json>]==])` in the server or client Lua
context through the LuaDebug channel (see docs/PROTOCOL.md) and decodes the JSON reply.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .luadebug import LuaDebugClient, LuaDebugError

RPC_EXPR = "return Mods.BG3AI.BG3AI.rpc([{eq}[{payload}]{eq}])"


class GameError(Exception):
    """The game refused or failed an operation; the message is fit to show the agent."""


class NotReady(GameError):
    """The game (or the requested Lua context) isn't available right now."""


def _long_bracket(payload: str, fn: str | None = None) -> str:
    """The eval expression for one rpc payload, or with fn just `fn([=[payload]=])` (for multi())."""
    eq = "="
    while f"]{eq}]" in payload:
        eq += "="
    if fn:
        return f"{fn}([{eq}[{payload}]{eq}])"
    return RPC_EXPR.format(eq=eq, payload=payload)


@dataclass
class JobResult:
    status: str  # done | failed | interrupted | timeout | still_running
    result: Any = None
    error: str | None = None
    events: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0


POOL_MAX = max(1, int(os.environ.get("BG3AI_RPC_CONNS", "4")))


class Game:
    """RPCs over a small pool of LuaDebug connections.

    bg3le serves each connection on its own thread, one eval at a time, but runs every queued eval of
    every connection in the same game tick (debug_server.cpp pump_where). One shared connection behind a
    lock made each read wait for whatever another thread had in flight (a queue worker's job.poll, a
    wait_for poll), so S009's reads took ~155 ms with a queue running vs ~60 ms without. Each eval now
    takes an idle connection (opening one up to POOL_MAX), so concurrent RPCs run in the same tick.
    BG3AI_RPC_CONNS=1 brings back the single serialised connection (bg3se under Proton may want that).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9998, pool_max: int | None = None):
        self.host = host
        self.port = port
        self.pool_max = pool_max or POOL_MAX
        self._idle: list[LuaDebugClient] = []
        self._open_n = 0
        self._cond = threading.Condition()
        # Last event seq consumed per context, so every tool can report "what happened since".
        self.event_seq = {"server": 0, "client": 0}
        # Events already consumed by read() (events.since folded into a read's own eval), handed out by
        # the next drain_events() of that context before it asks the game for more.
        self._ev_locks = {"server": threading.RLock(), "client": threading.RLock()}  # per context
        self._stash: dict[str, list[dict]] = {"server": [], "client": []}
        # The stash has its own lock: a background drain holds _ev_locks for a whole tick, and a fresh
        # drain_events() must hand out the stash without waiting for it.
        self._stash_lock = threading.Lock()
        self._bg_drain: dict[str, threading.Thread | None] = {"server": None, "client": None}
        # .fresh: contexts whose events this tool's read() drained (or is draining in the background).
        # Shared with parallel()'s helper threads, so _events_text's per-context drains see it too.
        self._tl = threading.local()
        # Optional metrics.Recorder: gets every RPC's and job's wall-clock interval (set by the MCP server).
        self.metrics = None

    # ---- connection pool ----

    def _new_client(self) -> LuaDebugClient:
        client = LuaDebugClient(self.host, self.port, connect_timeout=3.0)
        try:
            client.connect()
        except BaseException:
            client.close()
            raise
        return client

    def _acquire(self) -> LuaDebugClient:
        while True:
            with self._cond:
                while True:
                    while self._idle:
                        c = self._idle.pop()
                        if c.connected:
                            return c
                        self._open_n -= 1
                    if self._open_n < self.pool_max:
                        self._open_n += 1
                        break
                    self._cond.wait()
            try:
                return self._new_client()
            except OSError as e:
                with self._cond:
                    self._open_n -= 1
                    self._cond.notify()
                    if self._open_n > 0:
                        # The game takes fewer connections than we wanted (bg3se allows one): use those.
                        self.pool_max = self._open_n
                        continue
                raise NotReady(f"game not reachable on {self.host}:{self.port} ({e}); is it running?") from e

    def _release(self, client: LuaDebugClient, ok: bool = True) -> None:
        with self._cond:
            if ok and client.connected:
                self._idle.append(client)
            else:
                self._open_n -= 1
                client.close()
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            idle, self._idle = self._idle, []
            self._open_n -= len(idle)
        for c in idle:
            c.close()

    def eval(self, code: str, context: str = "client", timeout: float = 12.0) -> str:
        for attempt in range(2):
            client = self._acquire()
            ok = False
            try:
                out = client.evaluate(code, context=context, timeout=timeout)
                ok = True
                return out
            except ConnectionError:
                if attempt:
                    raise NotReady("lost connection to the game")
            except TimeoutError as e:
                raise NotReady(f"game did not answer ({e}); it may be loading or frozen") from e
            except LuaDebugError as e:
                ok = True  # the connection is fine; the Lua failed
                msg = str(e)
                if "not loaded" in msg or "story thread" in msg or "no session" in msg.lower():
                    raise NotReady(msg) from e
                raise GameError(msg) from e
            finally:
                self._release(client, ok)
        raise NotReady("unreachable")

    def parallel(self, *thunks):
        """Run independent RPC thunks at once (each on its own pooled connection, so in one game tick);
        returns their results in order, re-raising the first exception."""
        if len(thunks) <= 1 or self.pool_max <= 1:
            return [t() for t in thunks]
        owner = getattr(self.metrics, "owner_token", None)
        token = owner() if owner else None
        fresh = self._fresh()  # the tool's state, not the helper thread's (live: one extra tick per read)
        out: list[Any] = [None] * len(thunks)
        errs: list[BaseException | None] = [None] * len(thunks)

        def run(i, t):
            if token is not None:
                self.metrics.adopt(token)
            mine = getattr(self._tl, "fresh", None)
            self._tl.fresh = fresh
            try:
                out[i] = t()
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
                errs[i] = e
            finally:
                self._tl.fresh = mine
                if token is not None:
                    self.metrics.adopt(None)

        threads = [threading.Thread(target=run, args=(i, t), daemon=True) for i, t in enumerate(thunks[1:], 1)]
        for th in threads:
            th.start()
        run(0, thunks[0])
        for th in threads:
            th.join()
        for e in errs:
            if e is not None:
                raise e
        return out

    # ---- rpc ----

    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float = 12.0) -> Any:
        payload = json.dumps({"op": op, "args": args or {}}, separators=(",", ":"))
        t0 = time.time()
        try:
            raw = self.eval(_long_bracket(payload), context=context, timeout=timeout)
        finally:
            if self.metrics is not None:
                self.metrics.rpc_done(op, t0, time.time())
        if raw in ("", "nil"):
            raise NotReady(f"BG3AI mod is not loaded in the {context} context")
        try:
            reply = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GameError(f"bad reply from mod: {raw[:200]}") from e
        if not reply.get("ok"):
            raise GameError(reply.get("error") or "unknown error")
        return reply.get("result")

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server", default: Any = None) -> Any:
        try:
            return self.rpc(op, args, context)
        except GameError:
            return default

    # ---- events ----

    EVENT_LIMIT = 300
    # Contexts whose events a read() without ops there drains in the background (see read()).
    LAZY_EVENTS = ("client",)

    def _take_events(self, context: str, res: Any, limit: int) -> tuple[list[dict], bool]:
        """Consume one events.since reply (caller holds the context's lock). Returns (events, complete)."""
        if not isinstance(res, dict):
            return [], False  # the op failed: not drained
        evs = (res.get("events") or []) if isinstance(res, dict) else []
        if evs:
            self.event_seq[context] = evs[-1]["seq"]
        if len(evs) < limit:
            # Ring buffer may have been reset (hot reload / new session): resync.
            if res.get("latest", 0) < self.event_seq[context]:
                self.event_seq[context] = res.get("latest", 0)
            return evs, True
        return evs, False

    def drain_events(self, context: str = "server", limit: int = EVENT_LIMIT) -> list[dict]:
        fresh = self._fresh()
        if context in fresh:
            # This tool's read() drained the context in the same eval as its data (or is still draining it
            # in the background, which stashes for the next tool): no new RPC, and no wait.
            fresh.discard(context)
            with self._stash_lock:
                out, self._stash[context] = self._stash[context], []
            return out
        with self._ev_locks[context]:  # waits for a background drain of this context, if one is running
            with self._stash_lock:
                out, self._stash[context] = self._stash[context], []
            while True:
                res = self.rpc("events.since", {"seq": self.event_seq[context], "limit": limit}, context)
                evs, complete = self._take_events(context, res, limit)
                out.extend(evs)
                if complete or not isinstance(res, dict):
                    return out

    def stashed_events(self, context: str) -> list[dict]:
        """Events read() consumed that no drain_events() has handed out yet (peek, not consumed)."""
        with self._stash_lock:
            return list(self._stash[context])

    def begin_tool(self) -> None:
        """A new tool call on this thread: forget which contexts an earlier read() drained."""
        self._tl.fresh = set()

    def _fresh(self) -> set:
        if getattr(self._tl, "fresh", None) is None:
            self._tl.fresh = set()
        return self._tl.fresh

    def _mark_fresh(self, context: str) -> None:
        self._fresh().add(context)

    def _drain_in_background(self, context: str) -> None:
        """Drain `context`'s events into the stash without making the caller wait for its tick (the client
        context only runs once a rendered frame: ~46 ms on the 2nd machine's iGPU, vs ~25 ms for the
        server). At most one such drain per context is in flight; the next tool's drain_events() gets them."""
        with self._stash_lock:
            t = self._bg_drain[context]
            if t is not None and t.is_alive():
                return

            def run():
                try:
                    self._multi([], context, True)
                except Exception:  # noqa: BLE001 - best effort; the next drain_events() retries
                    pass

            t = threading.Thread(target=run, daemon=True, name=f"bg3ai-drain-{context}")
            self._bg_drain[context] = t
            t.start()

    def multi(self, calls: list[tuple[str, dict]], context: str = "server", events: bool = False,
              timeout: float = 12.0) -> list[Any]:
        """Several mod ops in ONE eval (one game tick instead of n). Each slot is the op's result, or None
        if that op failed. events=True also drains this context's events in the same eval: they're stashed
        for this thread's next drain_events(context), so the tool's EVENTS section costs no extra tick."""
        out, _errs, complete = self._multi(calls, context, events, timeout)
        if complete:
            self._mark_fresh(context)
        return out

    def _multi(self, calls, context: str, events: bool, timeout: float = 12.0) -> tuple[list, list, bool]:
        """multi() without touching the calling thread's state: (results, per-op error or None,
        events fully drained)."""
        calls = list(calls)
        if events:
            # Under the context's lock the seq can't move between building the call and consuming its reply.
            self._ev_locks[context].acquire()
            calls.append(("events.since", {"seq": self.event_seq[context], "limit": self.EVENT_LIMIT}))
        try:
            if not calls:
                return [], [], False
            parts = [_long_bracket(json.dumps({"op": op, "args": args or {}}, separators=(",", ":")), "R")
                     for op, args in calls]
            code = "local R=Mods.BG3AI.BG3AI.rpc return '['.." + "..','..".join(parts) + "..']'"
            t0 = time.time()
            try:
                raw = self.eval(code, context=context, timeout=timeout)
            finally:
                if self.metrics is not None:
                    self.metrics.rpc_done("+".join(op for op, _ in calls), t0, time.time())
            if raw in ("", "nil"):
                raise NotReady(f"BG3AI mod is not loaded in the {context} context")
            try:
                replies = json.loads(raw)
            except json.JSONDecodeError as e:
                raise GameError(f"bad reply from mod: {raw[:200]}") from e
            out = [r.get("result") if isinstance(r, dict) and r.get("ok") else None for r in replies]
            errs = [None if isinstance(r, dict) and r.get("ok") else
                    ((r.get("error") if isinstance(r, dict) else None) or "unknown error") for r in replies]
            complete = False
            if events:
                errs.pop()
                evs, complete = self._take_events(context, out.pop(), self.EVENT_LIMIT)
                with self._stash_lock:
                    self._stash[context].extend(evs)
            return out, errs, complete
        finally:
            if events:
                self._ev_locks[context].release()

    def read(self, server: list[tuple[str, dict]] = (), client: list[tuple[str, dict]] = (),
             events: bool = True, soft: tuple = ("client",), strict: bool = False) -> tuple[list[Any], list[Any]]:
        """An observation in one game tick: the server ops in one eval and the client ops in another, run
        in parallel on two connections, each also draining its context's events (events=True) so the
        tool's EVENTS section needs no further RPC. Returns (server results, client results); a failed op
        is None, or with strict=True raises GameError with the mod's message (like rpc()).
        A context in `soft` whose eval fails (GameError/NotReady) gives None slots instead of raising, the
        way try_rpc("ui.state") did; the others raise."""
        jobs = {"server": list(server), "client": list(client)}
        ctxs = [c for c in ("server", "client") if jobs[c] or events]
        if events:
            # A context with no ops here only has events to give: don't make the read wait for its tick.
            for c in [c for c in ctxs if not jobs[c] and c in self.LAZY_EVENTS and len(ctxs) > 1]:
                self._drain_in_background(c)
                self._mark_fresh(c)
                ctxs.remove(c)

        def one(c):
            try:
                return self._multi(jobs[c], c, events)
            except GameError:
                if c not in soft:
                    raise
                return [None] * len(jobs[c]), [None] * len(jobs[c]), False

        got = dict(zip(ctxs, self.parallel(*[(lambda c=c: one(c)) for c in ctxs])))
        for c, (_, _, complete) in got.items():
            if complete:
                self._mark_fresh(c)
        if strict:
            for c in ctxs:
                err = next((e for e in got[c][1] if e), None)
                if err:
                    raise GameError(err)
        return (got["server"][0] if "server" in got else [None] * len(jobs["server"]),
                got["client"][0] if "client" in got else [None] * len(jobs["client"]))

    def read1(self, op: str, args: dict | None = None, context: str = "server") -> Any:
        """rpc(op) plus both contexts' events in the same game tick (see read()); raises like rpc()."""
        s, c = self.read([(op, args or {})] if context == "server" else [],
                         [(op, args or {})] if context == "client" else [],
                         soft=("client",) if context == "server" else ("server",), strict=True)
        return (s if context == "server" else c)[0]

    def skip_events(self, context: str = "server") -> None:
        res = self.rpc("events.since", {"seq": 10**12, "limit": 1}, context)
        with self._ev_locks[context]:
            self.event_seq[context] = res.get("latest", 0)
            with self._stash_lock:
                self._stash[context] = []

    # ---- jobs ----

    def run_job(self, op: str, args: dict, context: str = "server", wait_s: float = 25.0,
                poll_s: float = 0.1, on_start=None) -> JobResult:
        """Start an action op that returns {job}, then poll until it settles or wait_s passes.
        on_start(context, job_id) runs once the job exists (the action queue uses it to cancel)."""
        t0 = time.time()
        r = self._run_job(op, args, context, wait_s, poll_s, on_start)
        if self.metrics is not None and (r.elapsed_ms or r.status == "still_running"):  # a real job, not an instant op
            self.metrics.job_done(op, t0, time.time(), r.status)
        return r

    def _run_job(self, op: str, args: dict, context: str, wait_s: float, poll_s: float,
                 on_start=None) -> JobResult:
        started = self.rpc(op, args, context)
        if not isinstance(started, dict) or "job" not in started:
            # Instant op: no job, just a result.
            return JobResult("done", result=started)
        job_id = started["job"]
        if started.get("status") not in (None, "running"):
            # Settled during the call (refusal, nothing to do): no poll needed.
            return JobResult(started["status"], started.get("result"), started.get("error"))
        if on_start is not None:
            on_start(context, job_id)
        deadline = time.monotonic() + wait_s
        while True:
            view = self.rpc("job.poll", {"id": job_id}, context)
            if view["status"] != "running":
                return JobResult(view["status"], view.get("result"), view.get("error"),
                                 elapsed_ms=view.get("elapsed_ms", 0))
            if time.monotonic() > deadline:
                return JobResult("still_running", {"job": job_id}, elapsed_ms=view.get("elapsed_ms", 0))
            time.sleep(poll_s)

    def poll_job(self, job_id: int, context: str = "server", wait_s: float = 25.0) -> JobResult:
        deadline = time.monotonic() + wait_s
        while True:
            view = self.rpc("job.poll", {"id": job_id}, context)
            if view["status"] != "running" or time.monotonic() > deadline:
                status = view["status"] if view["status"] != "running" else "still_running"
                return JobResult(status, view.get("result"), view.get("error"), elapsed_ms=view.get("elapsed_ms", 0))
            time.sleep(0.15)

    # ---- readiness ----

    def game_state(self) -> str:
        return self.eval("return tostring(Ext.Utils.GetGameState())", "client", timeout=5.0)

    def server_ready(self) -> bool:
        try:
            self.rpc("sys.ping", context="server", timeout=5.0)
            return True
        except GameError:
            return False
