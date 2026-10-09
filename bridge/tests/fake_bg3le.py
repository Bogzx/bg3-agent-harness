"""A fake bg3le Lua debug server for offline latency and concurrency tests.

It copies the parts of third_party/bg3le/src/debug_server.cpp that set read latency:
* each connection is served by its own thread, one eval at a time (handle_client waits for its job);
* evals from every connection go into one queue that the game drains once per tick: the server context
  on the "story thread" (server_tick_s), the client context on the client thread (client_tick_s);
  every queued eval runs in that tick, one after another (pump_where), each costing its op's `cost_s`.
Lua isn't run: the two expression shapes the bridge sends are parsed instead, a single
`return Mods.BG3AI.BG3AI.rpc([=[json]=])` and Game.multi's `local R=... return '['..R([=[json]=])..','..']'`.
Ops are Python callables; events.since is built in (emit() adds events).
"""

from __future__ import annotations

import json
import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bg3ai.luadebug import _bytes_field, _fields, _first, _uint_field  # noqa: E402

_SINGLE = re.compile(r"^return Mods\.BG3AI\.BG3AI\.rpc\(\[(=*)\[(.*)\]\1\]\)$", re.S)
_MULTI_PART = re.compile(r"R\(\[(=*)\[(.*?)\]\1\]\)", re.S)


class _Job:
    __slots__ = ("code", "client", "done", "result", "error")

    def __init__(self, code: str, client: bool):
        self.code, self.client, self.done, self.result, self.error = code, client, False, "", ""


class FakeBg3le:
    def __init__(self, server_tick_s: float = 0.030, client_tick_s: float = 0.016, max_conns: int = 0):
        self.server_tick_s, self.client_tick_s, self.max_conns = server_tick_s, client_tick_s, max_conns
        self.ops: dict = {}          # op -> fn(args, ctx) -> result (raise to fail)
        self.cost_s: dict = {}       # op -> simulated Lua time inside the tick
        self.events = {"server": [], "client": []}
        self.seq = {"server": 0, "client": 0}
        self.evals: list[tuple[str, float]] = []   # (ctx, tick time) per eval run
        self.conns = 0
        self.peak_conns = 0
        self._q: list[_Job] = []
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.op("sys.ping", lambda a, c: "pong")

    # ---- setup ----

    def op(self, name: str, fn, cost_s: float = 0.0) -> None:
        self.ops[name], self.cost_s[name] = fn, cost_s

    def emit(self, ctx: str, kind: str, **kw) -> int:
        with self._cv:
            self.seq[ctx] += 1
            self.events[ctx].append({"seq": self.seq[ctx], "kind": kind, **kw})
            return self.seq[ctx]

    def start(self) -> "FakeBg3le":
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._tick, args=(False, self.server_tick_s), daemon=True).start()
        threading.Thread(target=self._tick, args=(True, self.client_tick_s), daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # ---- the game side: one pump per tick ----

    def _tick(self, client: bool, period: float) -> None:
        ctx = "client" if client else "server"
        while not self._stop.is_set():
            time.sleep(period)
            with self._cv:
                mine = [j for j in self._q if j.client == client]
                self._q = [j for j in self._q if j.client != client]
            for j in mine:
                try:
                    j.result = self._run(j.code, ctx)
                except Exception as e:  # noqa: BLE001
                    j.error = str(e)
                with self._cv:
                    j.done = True
                    self.evals.append((ctx, time.monotonic()))
                    self._cv.notify_all()

    def _rpc(self, payload: str, ctx: str) -> str:
        req = json.loads(payload)
        op, args = req["op"], req.get("args") or {}
        if op == "events.since":
            with self._cv:
                since, limit = args.get("seq", 0), args.get("limit", 200)
                out = [e for e in self.events[ctx] if e["seq"] > since][:limit]
                res = {"latest": self.seq[ctx], "events": out}
            return json.dumps({"ok": True, "result": res})
        fn = self.ops.get(op)
        if fn is None:
            return json.dumps({"ok": False, "error": f"unknown op {op}"})
        time.sleep(self.cost_s.get(op, 0.0))
        try:
            return json.dumps({"ok": True, "result": fn(args, ctx)})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)})

    def _run(self, code: str, ctx: str) -> str:
        m = _SINGLE.match(code.strip())
        if m:
            return self._rpc(m.group(2), ctx)
        if code.startswith("local R=Mods.BG3AI.BG3AI.rpc"):
            return "[" + ",".join(self._rpc(p.group(2), ctx) for p in _MULTI_PART.finditer(code)) + "]"
        if "GetGameState" in code:
            return "Running"
        raise RuntimeError(f"fake bg3le can't run: {code[:80]}")

    # ---- the debug server side: a thread per connection, one eval at a time ----

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                fd, _ = self._sock.accept()
            except OSError:
                return
            if self.max_conns and self.conns >= self.max_conns:
                fd.close()  # like a debugger that takes one client
                continue
            fd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._client, args=(fd,), daemon=True).start()

    @staticmethod
    def _recv(fd, n):
        buf = b""
        while len(buf) < n:
            c = fd.recv(n - len(buf))
            if not c:
                return None
            buf += c
        return buf

    @staticmethod
    def _send(fd, msg: bytes) -> None:
        fd.sendall(struct.pack("<I", len(msg) + 4) + msg)

    def _client(self, fd) -> None:
        with self._cv:
            self.conns += 1
            self.peak_conns = max(self.peak_conns, self.conns)
        try:
            while True:
                h = self._recv(fd, 4)
                if h is None:
                    return
                (total,) = struct.unpack("<I", h)
                body = self._recv(fd, total - 4) if total > 4 else b""
                if body is None:
                    return
                msg = _fields(body)
                seq = _first(msg, 1, 0)
                if 3 in msg:  # connect
                    self._send(fd, _uint_field(2, seq) + _bytes_field(3, _uint_field(1, 4)))
                    continue
                if 7 not in msg:
                    continue  # updateSettings & co: ignored, like bg3le
                ev = _fields(_first(msg, 7))
                job = _Job(_first(ev, 2, b"").decode(), bool(_first(ev, 1, 0)))
                with self._cv:
                    self._q.append(job)
                    self._cv.wait_for(lambda: job.done, timeout=10)
                value = _uint_field(1, 4) + _bytes_field(5, job.result.encode())
                resp = _bytes_field(1, value) + (_bytes_field(2, job.error.encode()) if job.error else b"")
                self._send(fd, _uint_field(2, seq) + _bytes_field(5, resp))
        except OSError:
            return
        finally:
            with self._cv:
                self.conns -= 1
            fd.close()
