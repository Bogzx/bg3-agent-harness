"""The Replay screen's data: one session's events on one wall-clock axis, and the recorded video that covers it.

    GET /api/v1/playthroughs/<pt>/replay/<session> ->
        {session, started, ended, events: [{t, kind: "action"|"say"|"memory", ...}], segments: [index rows]}

Times are epoch seconds. Tool calls carry their own `t` (actions.jsonl, shared by every playthrough, so cut to the
session's window); the agent's lines carry only a local HH:MM:SS clock, put on the axis from the session's start
(the log is written in the machine's local time, so read it on the machine or in the same time zone); memory commits
have their commit time. Video segments come from recorder.py's index; `recorder.locate` maps a time to (segment,
offset) and the web app does the same.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import parse as P
from . import recorder as rec
from .store import RunStore

MAX_EVENTS = 5000
SAY_KINDS = ("say", "end", "error")


def clock_to_epoch(clock: str, started: float) -> float | None:
    """A log line's local "HH:MM:SS" -> epoch s, the first such time at or after the session start (minus a second:
    the start is not on a whole second); a session past midnight wraps."""
    try:
        h, m, s = (int(x) for x in clock.split(":"))
    except ValueError:
        return None
    st = datetime.fromtimestamp(started).astimezone()  # local time, as the log was written
    day = st.hour * 3600 + st.minute * 60 + st.second
    delta = (h * 3600 + m * 60 + s - day) % 86400
    if delta > 86400 - 2:  # the start's own second, written a moment before `started`
        delta -= 86400
    return float(int(started) + delta)


def window_actions(path: Path, t0: float, t1: float, limit: int = MAX_EVENTS) -> list[dict]:
    """actions.jsonl lines with t0 <= t <= t1, in file order (cheap pre-filter on the "t" value before parsing)."""
    out = []
    try:
        f = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    with f:
        for line in f:
            try:
                t = json.loads(line).get("t")
            except (ValueError, AttributeError):
                continue
            if not isinstance(t, (int, float)) or not t0 <= t <= t1:
                continue
            a = P.action(line, result_limit=600)
            if a:
                out.append({"t": t, "kind": "action", "tool": a["tool"], "args": a["args"], "ms": a["ms"],
                            "ok": a["ok"], "result": P.scrub(a["result"])})
                if len(out) >= limit:
                    break
    return out


def session_window(store: RunStore, pt: str, session: str) -> tuple[float, float] | None:
    for s in store.sessions(pt):
        if s["session"] == session and s["started"]:
            end = s["ended"] or (s["started"] + (s["minutes"] or 60) * 60)
            return float(s["started"]), float(end)
    return None


def replay(store: RunStore, pt: str, session: str, video_dir: Path) -> dict | None:
    """The replay payload, or None when the session (or its start time) is unknown."""
    win = session_window(store, pt, session)
    if win is None:
        return None
    t0, t1 = win
    events = window_actions(store.actions_path, t0, t1)
    log = store.session_dir(pt) / f"{session}.log"
    for e in store.log_entries(log, max_bytes=4 * 1024 * 1024) if log.exists() else []:
        if e["kind"] in SAY_KINDS and e["clock"]:
            t = clock_to_epoch(e["clock"], t0)
            if t is not None and t <= t1 + 60:
                events.append({"t": t, "kind": "say", "say": e["kind"], "text": P.scrub(e["text"])})
    for c in store.memory_log(pt, 200):
        if t0 <= c["t"] <= t1 + 900:  # the keeper commits after the session
            events.append({"t": c["t"], "kind": "memory", "subject": c["subject"], "sha": c["sha"]})
    events.sort(key=lambda e: e["t"])
    segs = [s for s in rec.read_index(video_dir)
            if s["start"] <= t1 and (s["end"] is None or s["end"] >= t0)]
    return {"session": session, "started": t0, "ended": t1, "events": events[:MAX_EVENTS], "segments": segs}
