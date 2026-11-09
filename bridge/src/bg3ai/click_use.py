"""What the game did when a Lockpick row was pressed and no roll came: from its events and the actor's position.

Lockpick read, 2026-11-09 (ops/logs live.md): 16 of 16 no-rolls were a UseFinished with success false about 0.33 s after
the click, with the actor not moving and no StartedLockpicking. "nothing happened" was false there. Round live read,
2026-11-09 (row 4b): the same failed Use 0.75 s after the click with the actor walked 1.4 m first; false there too.
"""

from __future__ import annotations

import math
from typing import Any

WALK_EPS_M = 0.15
PLAIN_USE_FAILED = "the game ran a plain Use of it, which failed; no lockpicking started; the actor did not move"
USE_FAILED_MOVED = "the game ran a plain Use of it, which failed; no lockpicking started; the actor moved {m:.1f} m"
NO_ROLL_MOVED = "no roll came; the actor moved {m:.1f} m"


def before_click(game: Any, char: str | None) -> dict:
    """The server event seq and the actor's position just before the row is clicked ({}: not readable)."""
    try:
        return {
            "seq": game.rpc("events.since", {"seq": 10**12, "limit": 1})["latest"],
            "pos": game.rpc("nav.activity", {})["pos"][char],
        }
    except Exception:  # an older mod, or a fake: the failure keeps its plain words
        return {}


def _same(ref: Any, uuid: str | None) -> bool:
    return isinstance(ref, dict) and str(ref.get("uuid"))[-36:] == str(uuid)[-36:]


def after_click(game: Any, before: dict, char: str | None, target: str | None) -> tuple[bool, float] | None:
    """Read when the failure is worded: was there a failed Use of the target by the actor, and how far the actor now
    stands from where it stood before the click (None: not readable)."""
    try:
        evs = game.rpc("events.since", {"seq": before["seq"], "limit": 200}).get("events") or []
        moved = math.dist(game.rpc("nav.activity", {})["pos"][char], before["pos"])
    except Exception:
        return None
    failed = any(
        e.get("kind") == "used"
        and _same(e["data"].get("char"), char)
        and _same(e["data"].get("item"), target)
        and not e["data"].get("success")
        for e in evs
        if isinstance(e.get("data"), dict)
    )
    return failed, moved


def plain_use_text(error: str | None, reason: Any, game: Any, before: dict, args: dict) -> str | None:
    """The words after a click with no effect: what was read of the Use and the actor's position, else `error`."""
    read = after_click(game, before, args.get("char"), args.get("target")) if reason == "no_effect" else None
    if read is None:
        return error
    failed, moved = read
    if moved > WALK_EPS_M:
        return (USE_FAILED_MOVED if failed else NO_ROLL_MOVED).format(m=moved)
    return PLAIN_USE_FAILED if failed else error
