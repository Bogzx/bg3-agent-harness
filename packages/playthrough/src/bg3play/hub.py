"""The SSE hub: feeds turn file changes into numbered events; viewers read them with Last-Event-ID resume.

Feeds (each `poll()` returns the new (type, data) pairs since its last call):
- actions: new actions.jsonl lines -> "action" (a tool call with ms and result), then "party" when its result is a
  full status (WHERE/PARTY rows) or names a new region (level_changed, get_map); a truncated file -> "actions_reset".
  The file is shared by every playthrough and run: a call from before the running session's start (runner.status.json
  started) is none of the watched session's (live3: the stream's first tail showed the previous run's tool calls
  and party under the new run's S001)
- log: the followed playthrough's agent log -> "log" entries; a new log file -> "session" first
- status: runner.status.json changed (heartbeat/updated ignored) -> "status"
- memory: a new commit in the memory repo -> "memory" (checked only when .git/logs/HEAD changes)

Event ids are "<generation>-<seq>". A reader that sends an id from another generation (the server restarted), from
the future, or older than the buffer gets reset=True and the whole buffer; otherwise exactly the events after it.
`poll()` is called by one thread (`run`) or directly by tests, so nothing here sleeps or reads a clock.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import api, parse
from .store import RunStore
from .tail import Tailer

STATUS_IGNORED = ("heartbeat", "updated")


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    data: Any


class ActionsFeed:
    def __init__(self, store: RunStore):
        self.store = store
        self.tail = Tailer(store.actions_path, initial_bytes=64 * 1024)
        self.started = False
        self.party: dict[str, Any] | None = None  # the last "party" sent: a later region change updates its place

    def poll(self) -> list[tuple[str, Any]]:
        lines, reset = self.tail.lines()
        out: list[tuple[str, Any]] = [("actions_reset", {})] if reset and self.started else []
        self.started = True
        since = self.store.status().get("started")
        for ln in lines:
            a = parse.action(ln)
            if a and isinstance(since, (int, float)) and not (isinstance(a["t"], (int, float)) and a["t"] >= since):
                continue
            if a:
                out.append(("action", dict(a, result=parse.scrub(a["result"]))))
                st = parse.status_text(a["result"])
                region = parse.region_change(a["result"])
                if st and st["party"]:
                    self.party = dict(st, t=a["t"], source=a["tool"])
                elif region and self.party and region != self.party["where"]:
                    self.party = dict(self.party, where=region, t=a["t"], source=a["tool"])
                else:
                    continue
                out.append(("party", self.party))
        return out


class LogFeed:
    """Follows `pt`'s agent log, or the runner's current playthrough when pt is None."""

    def __init__(self, store: RunStore, pt: str | None = None):
        self.store, self.pt = store, pt
        self.path: Path | None = None
        self.tail: Tailer | None = None

    def poll(self) -> list[tuple[str, Any]]:
        pt = self.pt or self.store.current()
        path = self.store.agent_log(pt) if pt else None
        out: list[tuple[str, Any]] = []
        if path != self.path:
            self.path, self.tail = path, Tailer(path, initial_bytes=32 * 1024) if path else None
            out.append(("session", {"playthrough": pt, "session": path.stem if path else None}))
        if self.tail is None:
            return out
        lines, _reset = self.tail.lines()
        sid = self.path.stem if self.path else None
        for ln in lines:
            e = parse.log_line(ln)
            if e and e["kind"] in api.LOG_KINDS_SHOWN:
                out.append(("log", dict(e, text=parse.scrub(e["text"]), playthrough=pt, session=sid)))
        return out


class StatusFeed:
    def __init__(self, store: RunStore):
        self.store = store
        self.last: dict[str, Any] | None = None

    def poll(self) -> list[tuple[str, Any]]:
        st = {k: v for k, v in self.store.status().items() if k not in STATUS_IGNORED}
        if st == self.last:
            return []
        self.last = st
        return [("status", api.runner_view(self.store))]


class MemoryFeed:
    def __init__(self, store: RunStore, pt: str | None = None):
        self.store, self.pt = store, pt
        self.key: tuple[Any, ...] | None = None
        self.seen: set[str] = set()

    def poll(self) -> list[tuple[str, Any]]:
        pt = self.pt or self.store.current()
        if not pt:
            return []
        head_log = self.store.memory_dir(pt) / ".git" / "logs" / "HEAD"
        try:
            st = head_log.stat()
            key: tuple[Any, ...] | None = (pt, st.st_size, st.st_mtime_ns)
        except OSError:
            key = None
        if key is None or key == self.key:
            return []
        first = self.key is None or self.key[0] != pt
        self.key = key
        commits = self.store.memory_log(pt, 20)
        new = [c for c in reversed(commits) if c["sha"] not in self.seen]
        self.seen.update(c["sha"] for c in commits)
        return [] if first else [("memory", dict(c, playthrough=pt)) for c in new]


class Hub:
    def __init__(self, store: RunStore, generation: str, pt: str | None = None, buffer: int = 2000):
        if "-" in generation:
            raise ValueError("generation must not contain '-'")
        self.generation = generation
        self.feeds = [StatusFeed(store), LogFeed(store, pt), ActionsFeed(store), MemoryFeed(store, pt)]
        self.events: deque[Event] = deque(maxlen=buffer)
        self.seq = 0
        self.closed = False
        self.cond = threading.Condition()

    def event_id(self, seq: int) -> str:
        return f"{self.generation}-{seq}"

    def last_id(self) -> str:
        with self.cond:
            return self.event_id(self.seq)

    def publish(self, type_: str, data: Any) -> None:
        with self.cond:
            self.seq += 1
            self.events.append(Event(self.seq, type_, data))
            self.cond.notify_all()

    def poll(self) -> int:
        """Run every feed once; returns how many events were published."""
        n = 0
        for feed in self.feeds:
            for type_, data in feed.poll():
                self.publish(type_, data)
                n += 1
        return n

    def _parse(self, last_id: str | None) -> int | None:
        """The seq a reader has seen, or None when its id doesn't belong to this hub's history."""
        if last_id is None:
            return None
        gen, _, s = last_id.rpartition("-")
        if gen != self.generation or not s.isdigit() or int(s) > self.seq:
            return None
        n = int(s)
        first = self.events[0].seq if self.events else self.seq + 1
        return n if n >= first - 1 else None

    def read(self, last_id: str | None, timeout: float = 0.0) -> tuple[list[Event], bool, str]:
        """(events after last_id, reset, the new last id). With a timeout, waits until there is something new."""
        with self.cond:
            n = self._parse(last_id)
            if timeout > 0 and n is not None and n == self.seq:
                self.cond.wait_for(lambda: self.seq != n or self.closed, timeout)
                n = self._parse(last_id)
            if n is None:
                return list(self.events), last_id is not None, self.event_id(self.seq)
            return [e for e in self.events if e.seq > n], False, self.event_id(self.seq)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def run(self, stop: threading.Event, interval: float = 0.5) -> None:
        while not stop.is_set():
            try:
                self.poll()
            except Exception as e:
                self.publish("hub_error", {"error": f"{type(e).__name__}: {e}"[:300]})
                stop.wait(10 * interval)
            stop.wait(interval)
        self.close()
