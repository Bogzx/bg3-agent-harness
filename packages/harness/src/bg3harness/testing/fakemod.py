"""A fake BG3AI mod: a TCP server speaking the game's LuaDebug protocol that answers the bridge's RPC evals.

The bridge (bg3ai.game.Game) sends Lua; the real game runs it through the mod's RPC dispatcher (Shared/RPC.lua). Here
the two Lua shapes the bridge sends are decoded instead (bg3ai.rpc_record.decode, the recorder's own decoder) and each
mod op is answered by the first responder that knows it: typically a scripted GameModel (scripted.py), then recorded
fixtures (fixtures.py). An op nobody answers gets the mod's own refusal ("unknown op '<op>' in <ctx> context").

Deterministic by construction: no ticks, no sleeps. Every eval is answered under one lock in arrival order, and every
op the bridge called is kept in `calls` for assertions. Point the bridge at it with BG3LE_DEBUG_PORT=<fake.port>.
"""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any, Protocol

from bg3ai.rpc_record import decode  # pyright: ignore[reportMissingTypeStubs] - the bridge ships no py.typed

from . import wire

Reply = dict[str, Any]  # {"ok": true, "result": ...} | {"ok": false, "error": "..."} | {"lua_error": "..."}


class Responder(Protocol):
    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Reply | None:
        """The mod's reply to one op, or None when this responder doesn't know it."""
        ...

    def eval_raw(self, ctx: str, code: str) -> str | None:
        """The result string of a non-RPC eval (e.g. the GetGameState probe), or None."""
        ...


class LuaError(Exception):
    """The eval fails as a Lua error (bg3le's error_message), e.g. the mod isn't loaded in that context."""


def unknown(ctx: str, op: str) -> Reply:
    return {"ok": False, "error": f"unknown op '{op}' in {ctx} context"}


class FakeMod:
    def __init__(self, *responders: Responder, host: str = "127.0.0.1"):
        self.responders = list(responders)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.evals = 0
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._stop = threading.Event()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(16)
        self.host = host
        self.port: int = self._sock.getsockname()[1]
        self._thread: threading.Thread | None = None

    # ---- answering (pure apart from the call log) ----

    def answer(self, ctx: str, code: str) -> str:
        """The eval's result string, as the mod's dispatcher would build it; raises LuaError for a Lua error."""
        with self._lock:
            self.evals += 1
            decoded = decode(code)
            if decoded is None:
                for r in self.responders:
                    out = r.eval_raw(ctx, code)
                    if out is not None:
                        return out
                raise LuaError(f"fake mod can't run: {code[:80]}")
            calls, multi = decoded
            replies = [self._one(ctx, c["op"], c["args"]) for c in calls]
            for rep in replies:
                if "lua_error" in rep:
                    raise LuaError(str(rep["lua_error"]))
            parts = [json.dumps(rep, separators=(",", ":")) for rep in replies]
            return "[" + ",".join(parts) + "]" if multi else parts[0]

    def _one(self, ctx: str, op: str, args: dict[str, Any]) -> Reply:
        self.calls.append((ctx, op, args))
        for r in self.responders:
            rep = r.reply(ctx, op, args)
            if rep is not None:
                return rep
        return unknown(ctx, op)

    def ops(self, ctx: str | None = None) -> list[str]:
        """The ops called so far, in order (one context or both)."""
        with self._lock:
            return [op for c, op, _ in self.calls if ctx is None or c == ctx]

    def broadcast(self, message: str) -> None:
        """A Lua print sent to every attached debugger (bg3le forwards prints; the bridge's jobpush listens)."""
        data = wire.frame(wire.debug_output(message))
        with self._lock:
            conns = list(self._conns)
        for fd in conns:
            with contextlib.suppress(OSError):
                fd.sendall(data)

    # ---- the debug server ----

    def start(self) -> FakeMod:
        self._thread = threading.Thread(target=self._accept, name="fakemod-accept", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        with self._lock:
            conns, self._conns = self._conns, []
        for fd in conns:
            with contextlib.suppress(OSError):
                fd.shutdown(socket.SHUT_RDWR)
            fd.close()

    def __enter__(self) -> FakeMod:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                fd, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(fd)
            threading.Thread(target=self._serve, args=(fd,), name="fakemod-conn", daemon=True).start()

    def _serve(self, fd: socket.socket) -> None:
        try:
            while True:
                head = _recv(fd, 4)
                if head is None:
                    return
                (total,) = struct.unpack("<I", head)
                if total < 4 or total > wire.MAX_PACKET:
                    return
                body = _recv(fd, total - 4) if total > 4 else b""
                if body is None:
                    return
                req = wire.parse_request(body)
                if req.kind == "connect":
                    fd.sendall(wire.frame(wire.connect_response(req.seq)))
                elif req.kind == "evaluate":
                    try:
                        msg = wire.evaluate_response(req.seq, result=self.answer(req.context, req.code))
                    except LuaError as e:
                        msg = wire.evaluate_response(req.seq, error=str(e) or "lua error")
                    fd.sendall(wire.frame(msg))
        except OSError:
            return
        finally:
            with self._lock:
                if fd in self._conns:
                    self._conns.remove(fd)
            fd.close()


def _recv(fd: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = fd.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class Ops:
    """A responder from plain functions: ops[name] = fn(ctx, args) -> result (raise to fail, as a Lua op would)."""

    def __init__(
        self, ops: dict[str, Callable[[str, dict[str, Any]], Any]] | None = None, game_state: str | None = "Running"
    ):
        self.ops = dict(ops or {})
        self.game_state = game_state

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Reply | None:
        fn = self.ops.get(op)
        if fn is None:
            return None
        try:
            return {"ok": True, "result": fn(ctx, args)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def eval_raw(self, ctx: str, code: str) -> str | None:
        if self.game_state is not None and "GetGameState" in code:
            return self.game_state
        return None
