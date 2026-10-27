"""A play session's cost from its normalized events log (Sxxx.events.jsonl, events.v1) through the harness's reader:
bg3harness.events.load_events and bg3harness.runs.UsageMeter, the fold the runner's
cost cap uses: a process's result event gives its exact cost (Claude's total_cost_usd), and a process without one
yet counts its last usage event's running cost (pi's billed cost). Resumes (the harness's Sxxx-rN / Sxxx-cN files; the
app appends them to Sxxx's) add to their session. A clients.RUNNING_COST client's result reports its conversation's
cost so far over --resume, so a conversation costs the highest total it reported (runs.own_cost over one `running`
for the session), never the sum over its resumes (bench diverse-2026-10-25 Opus act1_gold_max: $42.09 for $9.70).
Cached per the session's files' size and mtime: /sessions and /playthroughs read every session on each request."""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

COST_KINDS = frozenset({"usage", "result"})
_cache: dict[tuple[tuple[str, int, int], ...], float | None] = {}
events: Any = importlib.import_module("bg3harness.events")  # Any to pyright: untyped legacy module
runs: Any = importlib.import_module("bg3harness.runs")
clients: Any = importlib.import_module("bg3harness.clients")


def events_cost(events: list[Any], running: dict[str, float] | None = None) -> float | None:
    """The UsageMeter cost of clients.Events in write order; None when none of them carries a cost. running: the
    session's conversation id -> highest reported total, for a RUNNING_COST client (runs.own_cost); None: each result's
    cost is its process's."""
    if not any(getattr(e, "cost_usd", None) is not None for e in events):
        return None
    meter = runs.UsageMeter(running=running)
    for e in events:
        meter.on_event(e)
    return round(float(meter.cost), 4)


def session_cost(base: Path) -> float | None:
    """The cost of session `base` (<dir>/Sxxx) and its resumes, or None without an events log carrying a cost."""
    rx = re.compile(rf"^{re.escape(base.name)}(-[rc]\d+)?\.events\.jsonl$")
    try:
        logs = sorted(f for f in base.parent.iterdir() if rx.match(f.name))
        key = tuple((str(f), (st := f.stat()).st_size, st.st_mtime_ns) for f in logs)
    except OSError:
        return None
    if key not in _cache:
        running: dict[str, float] = {}
        costs: list[float] = []
        for f in logs:
            b = f.with_name(f.name[: -len(".events.jsonl")])
            c = events_cost(
                events.load_events(b, kinds=COST_KINDS),
                running if runs.process_client(b) in clients.RUNNING_COST else None,
            )
            if c is not None:
                costs.append(c)
        _cache[key] = round(sum(costs), 4) if costs else None
    return _cache[key]
