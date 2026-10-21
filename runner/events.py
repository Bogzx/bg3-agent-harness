"""The normalized event log of one play process: Sxxx.events.jsonl next to the raw client stream Sxxx.jsonl
(research/arch/harness.md §3.2). One line per clients.Event with an envelope (v, seq, t, src): the client's events as
its parser read them (src "client") and the runner's own (src "harness": session_start, fault, session_end, ...).
The schema is packages/harness/schemas/events.v1.schema.json.

Analyzers read sessions through load_events(base): the events log when the session has one, else the raw stream
parsed by its client (sessions recorded before the log), with the tool calls and results timed by Sxxx.turns.jsonl.

    python3 -m unittest runner/test_events.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clients import Event


class EventLog:
    """Appends Events to one events.jsonl; the only writer of its file (the pump thread and the runner share it, so
    seq is total). A write that fails is dropped: the log never breaks a session."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
        self.lock = threading.Lock()
        self.seq = 0  # a log reopened (the same file stem again) goes on counting
        try:
            with open(self.path, "rb") as f:
                self.seq = sum(1 for _ in f)
        except OSError:
            pass
        self.f = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - open for the whole process

    def write(self, ev: Event) -> Event:
        with self.lock:
            ev.t = round(self.clock(), 3) if ev.t is None else ev.t
            ev.seq = self.seq
            self.seq += 1
            try:
                if not self.f.closed:
                    self.f.write(ev.to_json() + "\n")
                    self.f.flush()
            except (OSError, TypeError, ValueError):
                pass
        return ev

    def harness(self, kind: str, **detail) -> Event:
        return self.write(Event(kind, src="harness", detail=detail or None))

    def end(self, reason: str, code: int | None, start: float, **detail) -> None:
        """The process ended: a session_end event (empty details left out), then the file is closed. Returns None
        (the runner's `events_log = evlog.end(...)`: no log open)."""
        self.harness(
            "session_end",
            reason=reason,
            exit=code,
            minutes=round((self.clock() - start) / 60, 2),
            **{k: v for k, v in detail.items() if v},
        )
        self.close()

    def close(self) -> None:
        with self.lock:
            self.f.close()


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _turn_times(path: Path) -> dict:
    """(use|result, call id) -> the first arrival time in Sxxx.turns.jsonl (bg3ai.metrics.StreamTiming)."""
    times: dict = {}
    for ln in _lines(path):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("id") and r.get("type") in ("use", "result"):
            times.setdefault((r["type"], r["id"]), r.get("t"))
    return times


def load_events(
    base: Path, kinds: Collection[str] | None = None, parsers: dict | None = None, turns: Path | None = None
) -> list[Event]:
    """The Events of one play process; base is its path without the extension (runtime/sessions/<pt>/S012-r1).
    kinds: only these kinds (the raw fallback skips lines early for kinds={"result"}: streams run to tens of MB).
    parsers: the raw fallback's parser per client, shared across a run's streams in write order (runs.stream_events:
    codex's resumed threads report running totals). turns: the raw fallback's timing file (default base.turns.jsonl)."""
    base = Path(base)
    log = base.with_name(base.name + ".events.jsonl")
    if log.exists():
        marks = tuple(f'"kind":"{k}"' for k in kinds) if kinds else ()
        out = []
        for ln in _lines(log):
            if marks and not any(m in ln for m in marks):
                continue
            try:
                ev = Event.from_json(ln)
            except (ValueError, TypeError):
                continue  # a line cut short by a crash
            if not kinds or ev.kind in kinds:
                out.append(ev)
        return out
    import runs  # the raw parsers' dispatch lives in runs (runs imports the analyzers that import this module)

    lines = _lines(base.with_name(base.name + ".jsonl"))
    name = runs.stream_client(lines)
    if kinds and set(kinds) <= {"result"} and name in runs.RESULT_MARKS:   # pi: its result needs the whole stream
        lines = [ln for ln in lines if any(m in ln for m in runs.RESULT_MARKS[name])]
    times = _turn_times(turns or base.with_name(base.name + ".turns.jsonl"))
    out = []
    for i, ev in enumerate(runs.stream_events(lines, parsers, name)):
        if kinds and ev.kind not in kinds:
            continue
        ev.seq = i
        if ev.kind in ("tool_call", "tool_result") and ev.call_id:
            ev.t = times.get(("use" if ev.kind == "tool_call" else "result", ev.call_id))
        out.append(ev)
    return out


def result_text(ev: Event) -> str:
    """A tool result's text with the bridge's {"result": "..."} envelope unwrapped (triage.result_text's rule)."""
    s = ev.text.strip()
    if s.startswith("{"):
        try:
            v = json.loads(s)
            if isinstance(v, dict) and isinstance(v.get("result"), str):
                return v["result"]
        except ValueError:
            pass
    return s
