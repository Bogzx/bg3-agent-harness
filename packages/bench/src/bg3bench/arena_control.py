"""The arena facts' events, and the turns the game ran itself (bg3bench.arena's grade; split for the 1000-line cap).

`lost_control(records, rolls, end_t)`: one entry per `arena_control_lost` before the end, {side, character, round,
status, rolls}: the runner writes it once at a phase's first read whose character the game runs itself (a player has no
control of it either), status its display name or None. rolls: that character's rolls.jsonl rows from the event to the
next arena_turn or the end (its own rolls in the game's turn), None without rolls. Facts only: nothing here decides
whether such a match rates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def events(records: Iterable[dict[str, Any]], ev: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("kind") == "event" and r.get("ev") == ev]


def lost_control(
    records: list[dict[str, Any]], rolls: Iterable[Mapping[str, Any]] | None, end_t: float
) -> list[dict[str, Any]]:
    """The game's turns before end_t, as the module docstring says."""
    rows = list(rolls) if rolls is not None else None
    turns = [r["t"] for r in events(records, "arena_turn")]
    out: list[dict[str, Any]] = []
    for r in events(records, "arena_control_lost"):
        if r["t"] > end_t:
            continue
        until = min([t for t in turns if t > r["t"]] + [end_t])
        out.append(
            {
                "side": r.get("side"),
                "character": r.get("character"),
                "round": r.get("round"),
                "status": r.get("status"),
                "rolls": None
                if rows is None
                else sum(1 for x in rows if x.get("roller") == r.get("uuid") and r["t"] <= x.get("t", -1) < until),
            }
        )
    return out
