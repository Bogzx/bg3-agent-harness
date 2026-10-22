"""A play session's cost from its normalized events log (Sxxx.events.jsonl, events.v1) through the harness's reader:
bg3harness.events.load_events and runner/runs.UsageMeter, the fold the runner's
cost cap uses: a process's result event gives its exact cost (Claude's total_cost_usd), and a process without one
yet counts its last usage event's running cost (pi's billed cost). Resumes (Sxxx-r1, ...) add to their session.
Cached per file size and mtime: /sessions and /playthroughs read every session on each request."""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from . import legacy_paths

COST_KINDS = frozenset({"usage", "result"})
_cache: dict[tuple[str, int, int], float | None] = {}
events: Any = importlib.import_module("bg3harness.events")  # Any to pyright: untyped legacy module


def events_cost(events: list[Any]) -> float | None:
    """The UsageMeter cost of clients.Events in write order; None when none of them carries a cost."""
    if not any(getattr(e, "cost_usd", None) is not None for e in events):
        return None
    meter = legacy_paths.load("runs").UsageMeter()
    for e in events:
        meter.on_event(e)
    return round(float(meter.cost), 4)


def _file_cost(log: Path) -> float | None:
    st = log.stat()
    key = (str(log), st.st_size, st.st_mtime_ns)
    if key not in _cache:
        base = log.with_name(log.name[: -len(".events.jsonl")])
        _cache[key] = events_cost(events.load_events(base, kinds=COST_KINDS))
    return _cache[key]


def session_cost(base: Path) -> float | None:
    """The cost of session `base` (<dir>/Sxxx) and its resumes, or None without an events log carrying a cost."""
    rx = re.compile(rf"^{re.escape(base.name)}(-r\d+)?\.events\.jsonl$")
    try:
        logs = sorted(f for f in base.parent.iterdir() if rx.match(f.name))
    except OSError:
        return None
    costs = [c for c in (_file_cost(f) for f in logs) if c is not None]
    return round(sum(costs), 4) if costs else None
