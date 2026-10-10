"""Job-finished push: wake a waiting call the moment the mod finishes a job, instead of polling.

The mod prints `BG3AI.job <context> <id> <status>` from RPC.finish_job (Shared/RPC.lua). bg3le sends
every Lua print to each attached debugger client (lua_host.cpp l_log → debug_server_output), so one
extra LuaDebug connection that only listens hears every finish within a few ms (live 2026-10-10: 8 ms).
Game.run_job / poll_job then wait on the mark and poll once for the result: a 1 s cast costs 2 RPCs
instead of ~10, and combat_turn drops ~45 job.poll round trips per call (S022-S026 p50 67 RPCs).

Safe by construction: a missed or absent mark (an old mod, bg3se, a reconnect) only means the waiter
falls back to a poll every PUSH_FALLBACK_S; push is used only after this listener has heard one mark.
Marks are matched by (context, id) and must be newer than the waiter's last poll, so a stale mark
(job ids restart after a load) wakes at most one extra poll.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from typing import Callable

from .luadebug import LuaDebugClient

PUSH_FALLBACK_S = 1.0   # with push working: poll anyway this often (a lost mark costs at most this)
RECONNECT_S = 2.0       # don't retry a failed listener connection more often than this
MARK_TTL_S = 300.0
_MARK = re.compile(r"^BG3AI\.job (server|client) (\d+) (\w+)")


class JobPush:
    def __init__(self, connect: Callable[[], LuaDebugClient]):
        self._connect = connect
        self._cond = threading.Condition()
        self._marks: dict[tuple[str, int], float] = {}
        self._client: LuaDebugClient | None = None
        self._tried_at = 0.0
        self.seen = False  # heard at least one mark on the current connection

    # ---- listener ----

    def ensure(self) -> None:
        """Connect the listener if it isn't (at most every RECONNECT_S); never raises."""
        with self._cond:
            if self._client is not None and self._client.connected:
                return
            if time.monotonic() - self._tried_at < RECONNECT_S:
                return
            self._tried_at = time.monotonic()
            self._client, self.seen = None, False
        try:
            client = self._connect()
        except Exception:  # noqa: BLE001 - the game is down or loading: plain polling meanwhile
            return
        with self._cond:
            self._client = client
        threading.Thread(target=self._read, args=(client,), name="jobpush", daemon=True).start()

    def _read(self, client: LuaDebugClient) -> None:
        while client.connected:
            try:
                line = client.output.get(timeout=0.5)
            except queue.Empty:
                continue
            self.feed(getattr(line, "message", str(line)))
        with self._cond:
            if self._client is client:
                self._client, self.seen = None, False
            self._cond.notify_all()

    def feed(self, text: str) -> None:
        m = _MARK.match(text or "")
        if not m:
            return
        now = time.monotonic()
        with self._cond:
            self._marks[(m.group(1), int(m.group(2)))] = now
            self.seen = True
            if len(self._marks) > 500:
                self._marks = {k: t for k, t in self._marks.items() if now - t < MARK_TTL_S}
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            c, self._client, self.seen = self._client, None, False
        if c is not None:
            c.close()

    # ---- waiting ----

    @property
    def usable(self) -> bool:
        with self._cond:
            return self.seen and self._client is not None and self._client.connected

    def wait(self, context: str, job_id: int, since: float, timeout: float) -> bool:
        """Block until a finish mark for (context, job_id) newer than `since` arrives, or timeout.
        True = the mark came (poll now for the result)."""
        key = (context, int(job_id))
        with self._cond:
            return self._cond.wait_for(lambda: self._marks.get(key, 0.0) > since, max(0.0, timeout))


_shared: dict[tuple[str, int], JobPush] = {}
_shared_lock = threading.Lock()


def shared(host: str, port: int) -> JobPush:
    """One listener per game address, shared by every Game object of this process."""
    with _shared_lock:
        jp = _shared.get((host, port))
        if jp is None:
            def connect() -> LuaDebugClient:
                c = LuaDebugClient(host, port, connect_timeout=2.0)
                try:
                    c.connect()
                except BaseException:
                    c.close()
                    raise
                return c
            jp = _shared[(host, port)] = JobPush(connect)
        return jp
