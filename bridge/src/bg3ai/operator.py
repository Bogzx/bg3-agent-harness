"""Operator messages: what the person running a playthrough sends the agent from the app (operator chat).

The app's runner sets $BG3AI_OPERATOR_DIR (<runtime>/sessions/<pt>) only for a playthrough with the option on; the
bench never has it. Without it nothing here opens a file. Two files there, one writer each:

    operator.jsonl            the app's server appends {id, t, text} (bg3play.operator)
    operator.delivered.jsonl  the bg3 server appends {id, t, session, tool} when a result carries the message; the
                              app's runner appends {id, t, redeliver: true} at a restart for a delivered message the
                              dead client's stream never showed (its model never saw it): it is pending again

A message is pending until a delivered row names it, and again after a later redeliver row. `Outbox.take` (the MCP
layer, tools/session.py, via reporting.operator_told: the result the client gets, never a nested call's) adds every
pending message to that result and marks them delivered, so each goes out once; one stat per call when nothing
changed. The block states the message and its time only.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

INBOX = "operator.jsonl"
DELIVERED = "operator.delivered.jsonl"
PREFIX = "OPERATOR MESSAGE (from the person running this playthrough, sent "


def clock(t: float) -> str:
    """The message's sent time as the block shows it (local HH:MM:SS)."""
    return time.strftime("%H:%M:%S", time.localtime(t))


def head(t: float) -> str:
    """The block line's fixed start up to the colon: how a stream log is searched for a delivered message."""
    return f"{PREFIX}{clock(t)}):"


def line(t: float, text: str) -> str:
    return f"{head(t)} {text}"


def read_rows(path: Path) -> list[dict[str, Any]]:
    """The JSON object lines of a jsonl file, in order; a missing file or a broken line is skipped."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for ln in text.splitlines():
        try:
            v: object = json.loads(ln)
        except ValueError:
            continue
        if isinstance(v, dict):
            rows.append({str(k): x for k, x in v.items()})  # pyright: ignore[reportUnknownVariableType]
    return rows


def delivery(delivered: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Per message id, its current delivery: the last delivered row, or None when a redeliver row came after it."""
    out: dict[str, dict[str, Any] | None] = {}
    for r in delivered:
        mid = r.get("id")
        if isinstance(mid, str):
            out[mid] = None if r.get("redeliver") else r
    return out


def pending(inbox: list[dict[str, Any]], delivered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The inbox rows not delivered now, in the order they were sent."""
    d = delivery(delivered)
    return [r for r in valid(inbox) if d.get(r["id"]) is None]


def valid(inbox: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The inbox rows with an id, a sent time and a text (what the app's server writes)."""
    return [r for r in inbox if isinstance(r.get("id"), str) and isinstance(r.get("t"), (int, float))
            and isinstance(r.get("text"), str)]  # fmt: skip


def _sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class Outbox:
    """The bg3 server's side: pending messages into a result, once."""

    def __init__(self, directory: Path | None):
        self.dir = directory
        self._seen: tuple[Any, ...] | None = None  # both files' (mtime, size) when they last held nothing pending
        self._lock = threading.Lock()

    def take(self, session: str, tool: str, now: float | None = None) -> tuple[str, list[str]]:
        """(the block for this result, "" when nothing is pending; the ids it carries). Marks them delivered."""
        if self.dir is None:
            return "", []
        inbox_p, done_p = self.dir / INBOX, self.dir / DELIVERED
        with self._lock:
            sig = (_sig(inbox_p), _sig(done_p))
            if sig[0] is None or sig == self._seen:
                return "", []
            todo = pending(read_rows(inbox_p), read_rows(done_p))
            if not todo:
                self._seen = sig
                return "", []
            t = time.time() if now is None else now
            try:
                with done_p.open("a", encoding="utf-8") as f:
                    for r in todo:
                        f.write(json.dumps({"id": r["id"], "t": round(t, 3), "session": session, "tool": tool}) + "\n")
            except OSError:
                return "", []  # not marked: not shown either, so it goes out with a later result
            self._seen = None
            return "\n".join(line(r["t"], r["text"]) for r in todo), [r["id"] for r in todo]
