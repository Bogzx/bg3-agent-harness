"""What the game did when a Lockpick row was pressed and no roll came: from its events and the actor's position.

Lockpick read, 2026-11-09 (ops/logs live.md): 16 of 16 no-rolls were a UseFinished with success false about 0.33 s after
the click, with the actor not moving and no StartedLockpicking. "nothing happened" was false there.
"""

from __future__ import annotations

import math
from typing import Any

WALK_EPS_M = 0.15
PLAIN_USE_FAILED = "the game ran a plain Use of it, which failed; no lockpicking started; the actor did not move"


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


def plain_use_failed(game: Any, before: dict, char: str | None, target: str | None) -> bool:
    """Did the game answer the click with a failed Use of the target by the actor, who stayed put?"""
    try:
        evs = game.rpc("events.since", {"seq": before["seq"], "limit": 200}).get("events") or []
        moved = math.dist(game.rpc("nav.activity", {})["pos"][char], before["pos"])
    except Exception:
        return False
    if moved > WALK_EPS_M:
        return False
    return any(
        e.get("kind") == "used"
        and _same(e["data"].get("char"), char)
        and _same(e["data"].get("item"), target)
        and not e["data"].get("success")
        for e in evs
        if isinstance(e.get("data"), dict)
    )


def plain_use_text(error: str | None, reason: Any, game: Any, before: dict, args: dict) -> str | None:
    """The failure's words: PLAIN_USE_FAILED when that is what was read after a click with no effect, else `error`."""
    if reason == "no_effect" and plain_use_failed(game, before, args.get("char"), args.get("target")):
        return PLAIN_USE_FAILED
    return error
