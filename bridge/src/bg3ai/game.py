"""RPC access to the BG3AI mod running inside the game.

Every call evaluates `return Mods.BG3AI.BG3AI.rpc([==[<json>]==])` in the server or client Lua
context through the LuaDebug channel (see docs/PROTOCOL.md) and decodes the JSON reply.
"""

from __future__ import annotations

import json
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


def _long_bracket(payload: str) -> str:
    eq = "="
    while f"]{eq}]" in payload:
        eq += "="
    return RPC_EXPR.format(eq=eq, payload=payload)


@dataclass
class JobResult:
    status: str  # done | failed | interrupted | timeout | still_running
    result: Any = None
    error: str | None = None
    events: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0


class Game:
    def __init__(self, host: str = "127.0.0.1", port: int = 9998):
        self.host = host
        self.port = port
        self._client: LuaDebugClient | None = None
        self._lock = threading.Lock()
        # Last event seq consumed per context, so every tool can report "what happened since".
        self.event_seq = {"server": 0, "client": 0}
        # Optional metrics.Recorder: gets every RPC's and job's wall-clock interval (set by the MCP server).
        self.metrics = None

    # ---- connection ----

    def _conn(self) -> LuaDebugClient:
        if self._client is None or not self._client.connected:
            client = LuaDebugClient(self.host, self.port, connect_timeout=3.0)
            try:
                client.connect()
            except OSError as e:
                raise NotReady(f"game not reachable on {self.host}:{self.port} ({e}); is it running?") from e
            self._client = client
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def eval(self, code: str, context: str = "client", timeout: float = 12.0) -> str:
        with self._lock:
            for attempt in range(2):
                client = self._conn()
                try:
                    return client.evaluate(code, context=context, timeout=timeout)
                except ConnectionError:
                    self.close()
                    if attempt:
                        raise NotReady("lost connection to the game")
                except TimeoutError as e:
                    raise NotReady(f"game did not answer ({e}); it may be loading or frozen") from e
                except LuaDebugError as e:
                    msg = str(e)
                    if "not loaded" in msg or "story thread" in msg or "no session" in msg.lower():
                        raise NotReady(msg) from e
                    raise GameError(msg) from e
        raise NotReady("unreachable")

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

    def drain_events(self, context: str = "server", limit: int = 300) -> list[dict]:
        out: list[dict] = []
        while True:
            res = self.rpc("events.since", {"seq": self.event_seq[context], "limit": limit}, context)
            evs = res.get("events") or []
            out.extend(evs)
            if evs:
                self.event_seq[context] = evs[-1]["seq"]
            if len(evs) < limit:
                # Ring buffer may have been reset (hot reload / new session): resync.
                if res.get("latest", 0) < self.event_seq[context]:
                    self.event_seq[context] = res.get("latest", 0)
                return out

    def skip_events(self, context: str = "server") -> None:
        res = self.rpc("events.since", {"seq": 10**12, "limit": 1}, context)
        self.event_seq[context] = res.get("latest", 0)

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
