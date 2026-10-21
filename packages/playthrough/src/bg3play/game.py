"""Live game view while someone watches: the mod's state.status + visible events, and screen frames.

Ported from runner/dashboard/serve.py (Hub.game_loop / screen_loop). Read-only toward the game: only sys.info,
state.status and events.since RPCs, and screen captures that share the Mesa layer's single control slot (the
bridge's file lock). Both pollers stop touching the game when nobody has looked for VIEWER_IDLE_S.

The game connection and the capture are passed in (adapters/bridge_status.py builds the real ones), and `poll()`
takes the time, so the loops are tested with fakes and a fake clock.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any, Protocol

from . import live
from .jsontypes import is_obj

VIEWER_IDLE_S = 30.0
EVENTS_KEEP = 150


class GameRpc(Protocol):
    def rpc(
        self, op: str, params: dict[str, Any] | None = None, context: str = "server", timeout: float = 5.0
    ) -> Any: ...

    def close(self) -> None: ...


class Viewers:
    """When someone last asked for the live view."""

    def __init__(self, clock: Callable[[], float]):
        self.clock, self.last = clock, float("-inf")

    def seen(self) -> None:
        self.last = self.clock()

    def watching(self, now: float) -> bool:
        return now - self.last < VIEWER_IDLE_S


class GameWatch:
    """The latest shaped status and the visible events since the first poll; `poll(now)` -> seconds to wait."""

    def __init__(self, connect: Callable[[], GameRpc], viewers: Viewers, interval: float = 3.0):
        self.connect, self.viewers, self.interval = connect, viewers, interval
        self.lock = threading.Lock()
        self.game: GameRpc | None = None
        self.ops: set[str] = set()
        self.ops_t = float("-inf")
        self.event_seq: int | None = None
        self.view: dict[str, Any] = {"reachable": False, "error": "not polled yet"}
        self.events: list[dict[str, Any]] = []

    def snapshot(self, last_events: int = 40) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self.lock:
            return dict(self.view), list(self.events[-last_events:])

    def poll(self, now: float) -> float:
        if not self.viewers.watching(now):
            return 1.0
        try:
            if self.game is None:
                self.game = self.connect()
            if now - self.ops_t > 30:
                info = self.game.rpc("sys.info", context="server", timeout=5.0)
                self.ops, self.ops_t = set(info.get("ops") or []), now
            st = self.game.rpc("state.status", context="server", timeout=6.0) if "state.status" in self.ops else None
            evs = self._events() if "events.since" in self.ops else []
            with self.lock:
                self.view = {
                    "reachable": True,
                    "error": None,
                    "t": now,
                    "status": live.shape_status(st) if is_obj(st) else None,
                    "note": None if st is not None else "state.status not available yet",
                }
                if evs:
                    self.events = (self.events + evs)[-EVENTS_KEEP:]
        except Exception as e:
            with self.lock:
                self.view = {"reachable": False, "error": f"{type(e).__name__}: {e}"[:300], "t": now}
            self.close()
            return max(self.interval, 10.0)
        return self.interval

    def _events(self) -> list[dict[str, Any]]:
        assert self.game is not None
        if self.event_seq is None:  # start from "now"; history before the first look is skipped
            res = self.game.rpc("events.since", {"seq": 10**12, "limit": 1}, "server", timeout=5.0)
            self.event_seq = res.get("latest", 0)
            return []
        res = self.game.rpc("events.since", {"seq": self.event_seq, "limit": 100}, "server", timeout=5.0)
        evs: list[dict[str, Any]] = res.get("events") or []

        if evs:
            self.event_seq = evs[-1].get("seq", self.event_seq)
        elif res.get("latest", 0) < self.event_seq:
            self.event_seq = res.get("latest", 0)  # ring buffer reset (reload)
        return [{"t": e.get("t"), "kind": e.get("kind"), "text": live.event_text(e)} for e in evs]

    def close(self) -> None:
        if self.game is not None:
            with contextlib.suppress(Exception):
                self.game.close()
        self.game, self.ops_t = None, float("-inf")


Capture = Callable[[], tuple[bytes, str]]  # -> (image bytes, mime)


class ScreenWatch:
    """The latest frame from `capture` while someone watches; `poll(now)` -> seconds to wait."""

    def __init__(
        self,
        capture: Capture | None,
        viewers: Viewers,
        interval: float = 5.0,
        note: str | None = None,
        error: str | None = None,
    ):
        self.capture, self.viewers, self.interval = capture, viewers, interval
        self.lock = threading.Lock()
        self.frame: bytes | None = None
        self.view: dict[str, Any] = {"enabled": capture is not None, "error": error, "t": None, "mime": None}
        self.note = note

    def snapshot(self) -> tuple[dict[str, Any], bytes | None]:
        with self.lock:
            return dict(self.view), self.frame

    def poll(self, now: float) -> float:
        if self.capture is None or not self.viewers.watching(now):
            return 1.0
        try:
            data, mime = self.capture()
        except Exception as e:
            with self.lock:
                self.view.update(error=f"{type(e).__name__}: {e}"[:200])
            return max(self.interval, 15.0)
        with self.lock:
            self.frame = data
            self.view.update(error=None, t=now, mime=mime, bytes=len(data), note=self.note)
        return self.interval


def loop(poller: GameWatch | ScreenWatch, stop: threading.Event, clock: Callable[[], float]) -> None:
    while not stop.is_set():
        stop.wait(poller.poll(clock()))
