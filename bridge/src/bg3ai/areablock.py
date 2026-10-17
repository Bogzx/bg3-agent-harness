"""Story-area travel blocks, remembered per level.

S065 12:00:01-12:00:55: camp `FAILED (Location)`, then fast_travel `can't fast travel now (Location)`, then
travel_to(waypoint) the same: each refusal was right but came one tool at a time (4 calls, a 32.6 s gap), and
camp's refusal even suggested fast_travel. The HUD greys the camp and map-travel buttons together, so one Location
refusal answers for all three tools. The block is remembered for the level until the party walks away from where it
was last confirmed (the server reads the HUD again then) or travel works; the observation's ALERTS line shows it, so
the agent doesn't try, and every refusal names the way out (where travel last worked, else the nearest waypoint).
Pure: the server feeds it positions and the HUD's answers.
"""

from __future__ import annotations

import time
from typing import Any

from . import delta, macros

RECHECK_M = 15.0   # the party moved this far from where the block was last confirmed: read the HUD again
TOOLS = "camp, fast_travel and travel_to(waypoint)"


def _xyz(p: Any) -> str:
    return "(" + ",".join(str(round(float(v))) for v in p[:3]) + ")"


def _off(a: Any, b: Any) -> str:
    """'40m W' from a to b."""
    return " ".join(x for x in (f"{round(macros.dist(a, b))}m", delta.direction(a, b)) if x)


class AreaBlock:
    def __init__(self) -> None:
        self.block: dict | None = None   # {level, pos, err, tools: [..], t, wps}
        self.ok: dict[str, list] = {}    # level -> the last position where travel worked

    def blocked(self, level: Any, pos: Any, err: str, tool: str, wps: list | None = None,
                t: float | None = None) -> dict:
        """A refusal the HUD explained (err: TravelError, 'Location'; wps: nav.waypoints rows for the way out).
        Returns the block (kept or new)."""
        b = self.current(level)
        if b is None:
            b = self.block = {"level": level, "pos": pos, "err": err, "tools": [], "n": 0, "wps": [],
                              "t": t or time.time()}
        if pos:
            b["pos"] = pos
        if wps:
            b["wps"] = wps
        if tool not in b["tools"]:
            b["tools"].append(tool)
        b["n"] += 1
        return b

    def cleared(self, level: Any, pos: Any) -> None:
        """Travel works here (a fast travel or camp went, or the HUD shows no block): forget the block."""
        if pos and level:
            self.ok[str(level)] = list(pos)
        self.block = None

    def current(self, level: Any) -> dict | None:
        """The block for this level, or None (a block from another level is dropped)."""
        if self.block is not None and self.block["level"] != level:
            self.block = None
        return self.block

    def stale(self, level: Any, pos: Any) -> bool:
        """True when a block is remembered and the party walked RECHECK_M+ from where it was last confirmed."""
        b = self.current(level)
        return bool(b and pos and b.get("pos") and macros.dist(pos, b["pos"]) >= RECHECK_M)

    def way_out(self, level: Any, pos: Any, offset: bool = True) -> str:
        """'walk to (-45,-46,1006) 40m W (travel_to x,y,z), where travel last worked', else toward the nearest
        unlocked waypoint of the level (the block's wps). offset=False: without the distance (the ALERTS phrase
        must not change with every step, or it is reprinted on every look)."""
        off = (lambda a, b: " " + _off(a, b)) if offset else (lambda a, b: "")
        ok = self.ok.get(str(level))
        if ok and pos:
            return f"walk to {_xyz(ok)}{off(pos, ok)} (travel_to x,y,z), where travel last worked"
        b = self.current(level) or {}
        near = [w for w in b.get("wps") or [] if isinstance(w, dict) and w.get("pos")]
        if near and pos:
            w = min(near, key=lambda w: macros.dist(b.get("pos") or pos, w["pos"]))
            return (f"walk out the way you came, e.g. toward the {w.get('name')} waypoint {_xyz(w['pos'])}"
                    f"{off(pos, w['pos'])} (travel_to x,y,z: travel_to waypoint= is a fast travel, refused too)")
        return "walk out the way you came (travel_to x,y,z back to where camp or fast_travel last worked)"

    def note(self, b: dict, tool: str) -> str:
        """The repeat note for a refusal: ' Same story-area block as camp at 12:00:01: ...' ('' on the first)."""
        if b.get("n", 0) <= 1:
            return ""
        first = b["tools"][0] if b.get("tools") else tool
        return (f" Same story-area block as {first} at {time.strftime('%H:%M:%S', time.localtime(b['t']))}: "
                f"{TOOLS} are all refused here, so don't try the others.")

    def alert(self, level: Any, pos: Any) -> str:
        """The ALERTS phrase while a block is remembered ('' when none)."""
        b = self.current(level)
        if b is None:
            return ""
        return (f"travel blocked here (story area, {b['err']}): no {TOOLS}; way out: "
                + self.way_out(level, pos, offset=False))
