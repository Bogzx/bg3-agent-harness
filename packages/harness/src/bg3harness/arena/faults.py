"""What counts as a side's infrastructure, not its play: the phase a client fault cost it, and its tool faults.

A side's tool timeouts and tool exceptions are counted once its agent is stopped (runs.stream_failures over its events
file, which holds every process of the side; a resume's own stream would count them twice): `arena_tool_faults {side,
n, faults}` for a side with any. TOLERATED of them is recorded and the match stands; a side past it makes the match
invalid (rerun, never graded), its end's reason and winner kept and arena_end carrying `invalid: "tool_faults"`. A
real timeout can decide a match, and our tools failing isn't the side's play: the rule of match.FAULT_RESUMES (owner
D459). The bench voids at the first (bg3bench.results.validity)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bg3harness import runs
from bg3harness.arena.spec import SIDES

if TYPE_CHECKING:
    from bg3harness.arena.match import Turn

KINDS = ("tool_timeout", "tool_exception")  # the runs.stream_failures kinds a side's count holds
TOLERATED = 1  # of them, a side's in one match: recorded; one more makes the match invalid
INVALID = "tool_faults"  # arena_end's `invalid` then


def excuse_fault(turn: Turn | None, side: str) -> None:
    """`side`'s client failed: the phase of its in progress counts toward no forfeit. One already forced stays counted:
    its arena_turn_forced is written, and the grader has to read the same streak."""
    if turn is not None and turn.side == side and not turn.forced:
        turn.excused = "client_fault"


def record_tool_faults(out: Path, event: Callable[..., None], end: dict[str, Any] | None) -> str | None:
    """`out`: the match's directory; `event`: writes a fact; `end`: the arena_end not yet written, which a side past
    TOLERATED marks invalid (an end already invalid, as scripted, stays so). The outcome's why then, else None."""
    over: list[str] = []
    for s in SIDES:
        fails = [f for f in runs.stream_failures(out / f"{s}.jsonl") if f["kind"] in KINDS]
        n = sum(f.get("n", 1) for f in fails)  # a failure's n is left out at 1
        if n:
            event("arena_tool_faults", side=s, n=n, faults=[f["detail"] for f in fails])
        if n > TOLERATED:
            over.append(f"side {s} had {n}")
    if not over:
        return None
    if end is not None:
        end.setdefault("invalid", INVALID)
    return f"{INVALID}: {' and '.join(over)} tool timeouts or exceptions (one tolerated)"
