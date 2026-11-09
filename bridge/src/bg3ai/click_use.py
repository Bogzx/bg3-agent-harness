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
NO_ROLL = "no roll came"
NO_ROLL_MOVED = NO_ROLL + "; the actor moved {m:.1f} m"


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


class SeenNotices:
    """The game's notices read on any poll of a click's wait, in order, each once. ui.state shows what is on screen at
    that poll: row 4b's "Can't reach this" was gone 2.8 s after the click, before the 4 s wait ended."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def note(self, st: dict) -> dict:
        self.seen = list(dict.fromkeys(self.seen + list(st.get("notifications") or [])))
        return st


def after_click(game: Any, before: dict, char: str | None, target: str | None) -> tuple[bool, float, list] | None:
    """Read when the failure is worded: was there a failed Use of the target by the actor, how far the actor now
    stands from where it stood before the click, and where that is (None: not readable)."""
    try:
        evs = game.rpc("events.since", {"seq": before["seq"], "limit": 200}).get("events") or []
        pos = game.rpc("nav.activity", {})["pos"][char]
        moved = math.dist(pos, before["pos"])
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
    return failed, moved, pos


def plain_use_text(error: str | None, reason: Any, game: Any, before: dict, args: dict, result: dict) -> str | None:
    """The words after a click with no effect: what was read of the Use, the actor's position and the game's notices,
    else `error`. The notices move out of result's click detail into the words; `after` takes a walked actor's position
    (the mod's is from the job's end, before the click)."""
    read = after_click(game, before, args.get("char"), args.get("target")) if reason == "no_effect" else None
    if read is None:
        return error
    failed, moved, pos = read
    text = PLAIN_USE_FAILED if failed else None
    if moved > WALK_EPS_M:
        text = (USE_FAILED_MOVED if failed else NO_ROLL_MOVED).format(m=moved)
        if isinstance(result.get("after"), dict):
            result["after"] = {**result["after"], "pos": [round(v, 1) for v in pos]}
    notices = result["click"].pop("notifications", None)
    if notices:
        return (text or NO_ROLL) + "; the game said " + ", ".join(f'"{n}"' for n in notices)
    return text or error
