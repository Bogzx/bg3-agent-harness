"""The arena facts' events, and the turns the game ran itself (bg3bench.arena's grade; split for the 1000-line cap).

`lost_control(records, rolls, end_t)`: one entry per `arena_control_lost` before the end, {side, character, round,
status, rolls}: the runner writes it once at a phase's first read whose character the game runs itself (a player has no
control of it either), status its display name or None. rolls: that character's rolls.jsonl rows from the event to the
next arena_turn or the end (its own rolls in the game's turn), None without rolls. Facts only: nothing here decides
whether such a match rates.

`reactions_unanswered(records, end_t)`: per side, {declined, over_budget, waited_s, charged_s, budget_spent}: the prompts
the runner declined for it (arena_reaction_declined), those declined at their first read past its budget, the seconds
waited, the seconds taken off its turns (arena_turn_charged), and whether its budget ran out
(arena_reaction_budget_spent).
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


def reactions_unanswered(records: list[dict[str, Any]], end_t: float) -> dict[str, dict[str, Any]]:
    """Each side's unanswered prompts before end_t, as the module docstring says."""
    out: dict[str, dict[str, Any]] = {
        s: {"declined": 0, "over_budget": 0, "waited_s": 0.0, "charged_s": 0.0, "budget_spent": False} for s in "AB"
    }
    for r in events(records, "arena_reaction_declined"):
        if r["t"] <= end_t and r.get("side") in out:
            row = out[r["side"]]
            row["declined"] += 1
            row["over_budget"] += 1 if r.get("over_budget") else 0
            row["waited_s"] = round(row["waited_s"] + float(r.get("waited_s") or 0.0), 1)
    for r in events(records, "arena_turn_charged"):
        if r["t"] <= end_t and r.get("side") in out:
            out[r["side"]]["charged_s"] = round(out[r["side"]]["charged_s"] + float(r["charged_s"]), 1)
    for r in events(records, "arena_reaction_budget_spent"):
        if r["t"] <= end_t and r.get("side") in out:
            out[r["side"]]["budget_spent"] = True
    return out


def control_fields(
    records: list[dict[str, Any]], rolls: Iterable[Mapping[str, Any]] | None, end_t: float
) -> dict[str, Any]:
    """The grade's fields from this module."""
    return {
        "lost_control": lost_control(records, rolls, end_t),
        "reactions_unanswered": reactions_unanswered(records, end_t),
    }
