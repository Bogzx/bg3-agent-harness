"""Off-limits areas the game warned the party about, remembered per level (vis V26).

S084 04:08-04:12 (the Counting House): a guard's trespass talk ended "*It seems you've made a rather convincing case
to be left off with a warning. This time.*"; three minutes later travel_to e578 walked into the vault guards' area,
the Narrator said "*This area appears to be off-limits.*" and the second offence started a fight (reload, ~2 min).
A player who was warned keeps out of the place, or talks to its guard first.

Fair: an area is known only once the game's own Narrator line in a talk said so (off-limits, trespassing, not wanted
here, let off with a warning). The area is a disc round where the party member stood when that talk began (the mod's
dialogue_started `at`): the trespass trigger fires on entry, so the edge of the place lies there. Walks get the areas
as act.move_to `avoid` (Actions.lua: round them, else refused; trespass=true walks in anyway) and route says so.
Pure: the server feeds it events.
"""

from __future__ import annotations

import re
import time
from itertools import pairwise
from typing import Any

from . import macros

RADIUS_M = 6.0      # round the spot the talk began: covers the guard's post past the edge, not the next room
DY_M = 4.0          # Actions.lua A.AREA_DY_M: the area's own floor
PAIR_S = 180.0      # a Narrator line this long after the talk began still belongs to it
WARN_RX = re.compile(r"off[- ]limits|trespass|not wanted here|off with a warning|restricted area|"
                     r"not supposed to be here", re.IGNORECASE)


def _pos(p: Any) -> list | None:
    if isinstance(p, (list, tuple)) and len(p) >= 3 and all(isinstance(v, (int, float)) for v in p[:3]):
        return [float(v) for v in p[:3]]
    return None


def _xyz(p: list) -> str:
    return "(" + ",".join(str(round(v)) for v in p) + ")"


def _name(x: Any) -> str:
    return str(x.get("name") or "") if isinstance(x, dict) else ""


class OffLimits:
    def __init__(self) -> None:
        self.talk: dict | None = None    # the last talk with the party: {at, level, guard, t}
        self.areas: list[dict] = []      # {id, level, pos, r, guard, line, t}

    def feed(self, evs: list | None, now: float | None = None) -> list[dict]:
        """Events in drain order (server and client). Returns the areas this batch added."""
        now = time.time() if now is None else now
        added = []
        for e in evs or []:
            if not isinstance(e, dict):
                continue
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            if e.get("kind") == "dialogue_started" and not d.get("automated"):
                at = _pos(d.get("at"))
                if at:
                    guard = next((_name(s) for s in d.get("speakers") or [] if _name(s)), "")
                    self.talk = {"at": at, "level": d.get("level"), "guard": guard, "t": now}
            elif e.get("kind") == "dialogue_line":
                a = self.line(d.get("speaker"), d.get("text"), now)
                if a is not None:
                    added.append(a)
        return added

    def line(self, speaker: Any, text: Any, now: float | None = None) -> dict | None:
        """A shown dialogue line: the Narrator's trespass warning marks the place where the talk began."""
        now = time.time() if now is None else now
        t = self.talk
        if str(speaker or "") != "Narrator" or not WARN_RX.search(str(text or "")) or not t or now - t["t"] > PAIR_S:
            return None
        for a in self.areas:
            if a["level"] == t["level"] and macros.dist(a["pos"], t["at"]) <= a["r"]:
                a.update(line=str(text), t=now, guard=a["guard"] or t["guard"])  # the same place again
                return None
        a = {"id": len(self.areas) + 1, "level": t["level"], "pos": t["at"], "r": RADIUS_M, "guard": t["guard"],
             "line": str(text), "t": now}
        self.areas.append(a)
        return a

    @staticmethod
    def name(a: dict) -> str:
        """'the off-limits area by Cashguard Artillerist Dadger (-111,4,-177)'."""
        by = f" by {a['guard']}" if a.get("guard") else ""
        return f"the off-limits area{by} {_xyz(a['pos'])}"

    def avoid(self) -> list[dict]:
        """act.move_to's avoid rows (the mod keeps this level's)."""
        return [{"id": a["id"], "pos": a["pos"], "r": a["r"], "level": a["level"], "name": self.name(a)}
                for a in self.areas]

    def inside(self, level: Any, p: Any) -> dict | None:
        p = _pos(p)
        for a in self.areas:
            if p and a["level"] == level and abs(p[1] - a["pos"][1]) <= DY_M and \
                    ((p[0] - a["pos"][0]) ** 2 + (p[2] - a["pos"][2]) ** 2) ** 0.5 <= a["r"]:
                return a
        return None

    def crossed(self, level: Any, points: list) -> dict | None:
        """The first area a polyline (route legs) enters, not counting one its start is in."""
        pts = [q for q in (_pos(p) for p in points or []) if q]
        start = self.inside(level, pts[0]) if pts else None
        for a, b in pairwise(pts):
            for i in range(1, 9):
                q = [a[k] + (b[k] - a[k]) * i / 8 for k in range(3)]
                hit = self.inside(level, q)
                if hit is not None and hit is not start:
                    return hit
        return None

    def route_line(self, level: Any, here: Any, goal: Any, legs: list | None) -> str:
        """route's line about a warned area: the goal inside one, or the way crossing one; '' when neither."""
        start = self.inside(level, here)
        g = self.inside(level, goal)
        if g is not None and g is not start:
            return (f"OFF-LIMITS: the goal is inside {self.name(g)} you were warned about ({g['line']}): walks into"
                    " it are refused; talk to its guard first (a pass, a check), or travel_to/move_to trespass=true")
        pts = [here] + [l.get("to") for l in legs or [] if isinstance(l, dict)]
        c = self.crossed(level, pts)
        if c is not None:
            return (f"avoiding {self.name(c)} you were warned about: travel_to/move_to walk round it (refused when"
                    " no way round; trespass=true walks through)")
        return ""
