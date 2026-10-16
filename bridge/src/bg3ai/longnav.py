"""Long trips across an open level (claude-run-2 S010/S011: Karlach 90-150 m NW of the Emerald Grove).

What went wrong there: `route` said only "too far to preview the walk", travel_to stopped at the same boulder
wall and vine ledge five times in one session (the agent climbed the Cragged Rock up to the same dead end
twice), and the agent walked 460 m toward Zevlor while an unlocked waypoint stood next to him.

Pure helpers (server.py and route.py call them):
* pick_waypoint: fast travel first when an unlocked waypoint on this level is much nearer the goal;
* DeadEnds: where trips toward a goal were blocked this session (the bridge restarts per session), so the
  second stop at the same spot says "tried 2x: blocked" and offers another way instead of a third try. Each
  is tagged with its level (coordinates of another map mean nothing here), expires DEAD_TTL_S after its last
  hit (a door since opened, a cleared obstacle) and the store is cleared on a save load (audit NAV-10).

Fair: waypoints are the party's unlocked ones (the map shows them, with their places); dead ends are the
agent's own failed walks.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .macros import dist

FT_MIN_M = 120.0      # a trip shorter than this (straight) always walks
FT_SAVE_M = 60.0      # the waypoint must be this much nearer the goal than we are...
FT_RATIO = 0.6        # ...and at most this fraction of our distance from it
ROUTE_WP_SAVE_M = 30.0  # route's NEXT suggests a waypoint this much nearer the goal (no minimum trip)
DEAD_GOAL_M = 15.0    # trips toward goals this close together share their dead ends
DEAD_SPOT_M = 8.0     # stops this close together are the same dead end
DEAD_REPEAT = 2       # this many stops at one spot: "tried twice: blocked"
DEAD_TTL_S = 1200.0   # a dead end not hit again for this long is forgotten (the way may have opened since)

_GROUND = re.compile(r"no walkable ground|the way is blocked|no walkable path|no path to the destination|could not get closer|"
                     r"path blocked", re.I)


def pick_waypoint(here: Any, dest: Any, wps: Any, min_m: float = FT_MIN_M, save_m: float = FT_SAVE_M,
                  ratio: float = FT_RATIO) -> dict | None:
    """The unlocked waypoint to fast travel to before a long walk, or None. wps: nav.waypoints rows
    ({name, id, pos}). Straight distances only: a waypoint nearer the goal is where a player would start."""
    d = dist(here, dest)
    if d == float("inf") or d < min_m:
        return None
    rows = [w for w in wps or [] if isinstance(w, dict) and w.get("pos") and w.get("name")]
    if not rows:
        return None
    best = min(rows, key=lambda w: dist(w["pos"], dest))
    to = dist(best["pos"], dest)
    if to + save_m <= d and to <= ratio * d and dist(here, best["pos"]) > 20.0:
        return {**best, "to_goal_m": round(to, 1), "from_here_m": round(d, 1)}
    return None


def ground_stop(stop: str | None, lines: list[str] | None = None) -> bool:
    """Did a trip end because the ground led no further (not an interrupt, a cancel or a hazard)?"""
    if not stop or re.search(r"cancel|conversation|combat|hostile|prompt|HP|crosses", stop, re.I):
        return False
    if _GROUND.search(stop):
        return True
    if re.fullmatch(r"leg \d+ failed", stop.strip()):
        return bool(lines) and bool(_GROUND.search(lines[-1]))
    return False


@dataclass
class DeadEnd:
    goal: list
    pos: list
    left_m: float
    times: list[str] = field(default_factory=list)
    level: str | None = None  # the level it was hit on (None: unknown, applies anywhere until it expires)
    last: float = 0.0         # monotonic time of the last hit (expiry)

    @property
    def count(self) -> int:
        return len(self.times)


def _p(p: Any) -> str:
    def n(x: float) -> str:
        r = round(float(x), 1)
        return str(int(r)) if r == int(r) else str(r)
    return f"({n(p[0])},{n(p[1])},{n(p[2])})"


class DeadEnds:
    """Where trips toward a goal stopped on blocked ground, this session. level: the current level id (None when
    unknown; read once per record/for_goal), now: a monotonic clock (tests inject both)."""

    def __init__(self, clock: Callable[[], str] | None = None, level: Callable[[], str | None] | None = None,
                 now: Callable[[], float] | None = None, ttl_s: float = DEAD_TTL_S) -> None:
        self.items: list[DeadEnd] = []
        self.clock = clock or (lambda: time.strftime("%H:%M"))
        self.level = level or (lambda: None)
        self.now = now or time.monotonic
        self.ttl_s = ttl_s

    def _here(self) -> str | None:
        try:
            lv = self.level()
        except Exception:  # noqa: BLE001 - a failed level read (loading) must not fail travel_to
            return None
        return lv if isinstance(lv, str) and lv else None

    def _live(self, lv: str | None) -> list[DeadEnd]:
        """Drop the expired entries; return those of this level (an unknown level on either side matches)."""
        t = self.now()
        self.items = [e for e in self.items if t - e.last <= self.ttl_s]
        return [e for e in self.items if lv is None or e.level is None or e.level == lv]

    def record(self, goal: Any, pos: Any, left_m: float | None = None) -> DeadEnd | None:
        if not (isinstance(goal, (list, tuple)) and isinstance(pos, (list, tuple)) and len(goal) >= 3 and len(pos) >= 3):
            return None
        lv = self._here()
        left = float(left_m) if left_m is not None else dist(pos, goal)
        for e in self._live(lv):
            if dist(e.goal, goal) <= DEAD_GOAL_M and dist(e.pos, pos) <= DEAD_SPOT_M:
                e.times.append(self.clock())
                e.left_m = min(e.left_m, round(left, 1))
                e.last = self.now()
                e.level = e.level or lv
                return e
        e = DeadEnd([round(float(x), 1) for x in goal[:3]], [round(float(x), 1) for x in pos[:3]], round(left, 1),
                    [self.clock()], lv, self.now())
        self.items.append(e)
        return e

    def for_goal(self, goal: Any) -> list[DeadEnd]:
        """Dead ends toward this goal on this level, most hit first."""
        if not isinstance(goal, (list, tuple)):
            return []
        return sorted((e for e in self._live(self._here()) if dist(e.goal, goal) <= DEAD_GOAL_M), key=lambda e: -e.count)

    def near(self, goal: Any, pos: Any, within: float = DEAD_SPOT_M) -> DeadEnd | None:
        return next((e for e in self.for_goal(goal) if dist(e.pos, pos) <= within), None)

    def clear(self) -> None:
        self.items.clear()


def dead_end_text(e: DeadEnd) -> str:
    """'blocked 2x at (101.8,22.2,481) 35.6m short (01:11, 01:15)'."""
    return f"blocked {e.count}x at {_p(e.pos)} {e.left_m:g}m short ({', '.join(e.times[-3:])})"


def stop_note(e: DeadEnd | None, wp: dict | None, what: str = "it") -> str:
    """Appended to travel_to's stop: '' the first time; from the second stop at the same spot, say so and
    name another way (a waypoint nearer the goal, else the closest explored spot / the wiki)."""
    if e is None or e.count < DEAD_REPEAT:
        return ""
    out = f"; tried {e.count}x: {dead_end_text(e)}. Don't try this approach again"
    if wp:
        out += (f": fast travel to {wp['name']} ({wp.get('to_goal_m', '?')}m from {what}) and come from that side "
                f"(travel_to waypoint=\"{wp['name']}\")")
    else:
        out += (": the way in is from another side. route(...) shows the closest spot you have been; "
                "wiki_search how to reach it, or explore along the roads (get_map)")
    return out


def start_warning(ends: list[DeadEnd]) -> str:
    """travel_to's start line when this goal already has a repeated dead end."""
    bad = [e for e in ends if e.count >= DEAD_REPEAT]
    if not bad:
        return ""
    return ("KNOWN DEAD END toward it: " + "; ".join(dead_end_text(e) for e in bad[:2])
            + ". If this trip stops there again, take another approach (route() lists waypoints and the closest "
              "spot you have been)")


def connector_dead(c: dict, ends: list[DeadEnd]) -> DeadEnd | None:
    """A ladder/climb whose far end (its top when it leads up) is a known dead end (S010: the Cragged Rock)."""
    p = c.get("pos")
    if not (isinstance(p, (list, tuple)) and len(p) >= 3):
        return None
    far = [p[0], p[1] + (c.get("height_m") or 0) if c.get("leads") == "up" else p[1], p[2]]
    return next((e for e in ends if dist(e.pos, far) <= DEAD_SPOT_M), None)


def near_in(ends: list[DeadEnd], pos: Any, within: float = DEAD_SPOT_M) -> DeadEnd | None:
    """The dead end among ends at pos (within metres), or None."""
    return next((e for e in ends if dist(e.pos, pos) <= within), None)
