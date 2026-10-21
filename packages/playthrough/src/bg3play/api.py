"""Payload builders for the v1 JSON API, over a RunStore. Every payload carries "v": API_VERSION."""

from __future__ import annotations

from typing import Any

from . import parse as P
from . import positions as POS
from .store import RunStore

API_VERSION = 1
RUNNER_KEYS = ("session", "started", "deadline", "hard_stop", "run_end", "session_ended", "updated", "model",
               "effort", "session_minutes", "status_line", "alert")
LOG_KINDS_SHOWN = ("say", "tool", "result", "progress", "end", "error", "runner", "init", "header")


def _v(payload: dict) -> dict:
    return {"v": API_VERSION, **payload}


def runner_view(store: RunStore) -> dict:
    st = store.status()
    return {"playthrough": store.current(), **{k: st.get(k) for k in RUNNER_KEYS if k in st}}


def status(store: RunStore) -> dict:
    return _v({"runner": runner_view(store)})


def playthroughs(store: RunStore) -> dict:
    cur = store.current()
    rows = []
    for pt in store.playthroughs():
        ss = store.sessions(pt)
        costs = [s["cost_usd"] for s in ss if s["cost_usd"] is not None]
        rows.append({"name": pt, "current": pt == cur, "sessions": len(ss),
                     "last_session": ss[-1]["session"] if ss else None,
                     "cost_usd": round(sum(costs), 2) if costs else None,
                     "minutes": round(sum(s["minutes"] or 0 for s in ss), 1)})
    return _v({"playthroughs": rows})


def sessions(store: RunStore, pt: str) -> dict:
    return _v({"playthrough": pt, "sessions": store.sessions(pt)})


def _party(actions: list[dict], snapshot: str) -> dict | None:
    """The newest full status (WHERE/PARTY rows) among the tool results, else memory snapshots/status.md."""
    for a in reversed(actions):
        st = P.status_text(a["result"])
        if st and st["party"]:
            return {**st, "t": a["t"], "source": a["tool"]}
    st = P.status_text(snapshot)
    return {**st, "t": None, "source": "snapshots/status.md"} if st else None


def watch(store: RunStore, pt: str, log_entries: int = 300, action_rows: int = 150) -> dict:
    """Everything the Watch screen shows at load; the SSE stream keeps it current afterwards."""
    ss = store.sessions(pt)
    log = store.agent_log(pt)
    entries = [e for e in store.log_entries(log) if e["kind"] in LOG_KINDS_SHOWN][-log_entries:] if log else []
    actions = store.recent_actions()
    if pt != store.current() and ss:
        # actions.jsonl is shared by every playthrough: keep this one's session windows
        t0 = min((s["started"] for s in ss if s["started"]), default=None)
        t1 = max((s["ended"] for s in ss if s["ended"]), default=None)
        actions = [a for a in actions if isinstance(a["t"], (int, float))
                   and (t0 is None or a["t"] >= t0) and (t1 is None or a["t"] <= t1)]
    costs = [s["cost_usd"] for s in ss if s["cost_usd"] is not None]
    last = ss[-1] if ss else None
    shot = store.latest_screenshot()
    return _v({
        "playthrough": pt,
        "runner": runner_view(store) if pt == store.current() else None,
        "session": log.stem if log else None,
        "log": entries,
        "actions": [dict(a, result=P.scrub(a["result"])) for a in actions[-action_rows:]],
        "party": _party(actions, store.memory_text(pt, "snapshots/status.md")),
        "goal": P.current_goal(store.memory_text(pt, "objectives.md"), store.memory_text(pt, "HANDOFF.md")),
        "cost": {"total_usd": round(sum(costs), 2) if costs else None,
                 "last_session_usd": last["cost_usd"] if last else None,
                 "sessions": len(ss)},
        "latency": (last or {}).get("latency"),
        "screenshot": {"t": shot.stat().st_mtime, "name": shot.name} if shot else None,
    })


def memory_tree(store: RunStore, pt: str) -> dict:
    return _v({"playthrough": pt, "files": store.memory_tree(pt)})


def memory_file(store: RunStore, pt: str, rel: str) -> dict | None:
    text = store.memory_file(pt, rel)
    if text is None:
        return None
    return _v({"playthrough": pt, "path": rel, "text": text, "history": store.memory_log(pt, 20, rel)})


def memory_log(store: RunStore, pt: str, limit: int = 50) -> dict:
    return _v({"playthrough": pt, "commits": store.memory_log(pt, limit)})


def timeline(store: RunStore, pt: str) -> dict:
    """Choices (decisions.md) and fights per session, with each session's start time for the axis."""
    ss = store.sessions(pt)
    starts: dict[str, Any] = {s["session"]: s["started"] for s in ss}
    cards = P.decisions(store.memory_text(pt, "decisions.md"))
    for c in cards:
        n = P.session_number(c["session"])
        c["session_id"] = f"S{n:03d}" if n is not None else None
        c["t"] = starts.get(c["session_id"])
    return _v({"playthrough": pt,
               "sessions": [{"session": s["session"], "started": s["started"], "minutes": s["minutes"],
                             "outcome": s["outcome"]} for s in ss],
               "decisions": cards, "fights": store.fights(pt)})


def positions(store: RunStore, pt: str, level: str | None = None) -> dict:
    """The breadcrumb plot's data (positions.tracks) for one level, default the latest."""
    return _v({"playthrough": pt, **POS.tracks(POS.read(store.positions_path(pt)), level or None)})
