"""PASSED: what came into view during a trip and is out of view again at its end (human-gap research §1e).

    PASSED (seen on the way, out of view now): e12 Chest <container> 30m NW | e7 Goblin hostile 41m S (5m down)

CHANGES diffs the trip's start against its end only, so a chest in view on leg 2 of a 100 m trip and out of range
at the end was never reported. travel_to's legs read state.nearby, centred on the walker, in the check each
finished leg already makes (actq.Watcher.post_ops: the same round trip, one op more in its server eval); regroup
unions its walkers' trips. Listed: what was not in the agent's last view when the trip started (delta.TRACKER) and
is not in view at the end (the end's CHANGES read), nearest first from the walker's end spot, up to MAX_PASSED.
Scenery objects and clutter are left out, as CHANGES leaves them out. Facts only: name, kind, distance, direction,
height.
"""

from __future__ import annotations

from typing import Any

from . import delta, minimap, render
from .render import Handles

MAX_PASSED = 6  # entries shown, nearest first, then "+N more"
RADIUS_M = 25  # the per-leg read's radius: the same as CHANGES' state.nearby (delta._OPS)

LAST: dict[str, Sightings] = {}  # each walker's last trip (regroup reads its walkers' after their queues end)


def _pos(p: Any) -> list | None:
    return list(p[:3]) if isinstance(p, (list, tuple)) and len(p) >= 3 else None


class Sightings:
    """What one trip's leg reads saw: uuid -> {name, pos, bits, kind}; known = what the agent's last view had."""

    def __init__(self, known: Any = ()) -> None:
        self.known = set(known)
        self.seen: dict[str, dict] = {}

    @classmethod
    def from_tracker(cls) -> Sightings:
        """A new trip's sightings: the entities of the agent's last view count as known."""
        return cls((delta.TRACKER.last.get("ents") or {}).keys())

    def attach(self, watcher: Any, center: str) -> None:
        """Read state.nearby around `center` with the check after each finished step of this watcher."""
        watcher.post_ops = [("state.nearby", {"radius": RADIUS_M, "center": center})]
        watcher.on_post = lambda got: self.add(got.get("state.nearby"))

    def add(self, nearby: Any) -> None:
        """One state.nearby payload: its characters and its leads (not scenery, not clutter); the latest wins."""
        if not isinstance(nearby, dict):
            return
        for c in nearby.get("characters") or []:
            if isinstance(c, dict) and c.get("uuid") and _pos(c.get("pos")):
                bits = [str(c["relation"])] if c.get("relation") else []
                bits += ["dead"] if c.get("dead") else []
                self.seen[c["uuid"]] = {"name": c.get("name") or "?", "pos": _pos(c["pos"]), "bits": bits}
        for it in nearby.get("items") or []:
            if not (isinstance(it, dict) and it.get("uuid") and _pos(it.get("pos"))):
                continue
            if it.get("kind") == "object" and not (it.get("locked") or it.get("trap") or it.get("near")):
                continue  # scenery, as delta.snapshot leaves it out of CHANGES
            rec = delta._item_rec(it)
            if delta.is_clutter(rec):
                continue
            bits = [f"<{rec['kind'] or 'item'}>"] + [f for f in rec["flags"] if f in ("locked", "trap")]
            bits += ["story"] if rec["story"] else []
            self.seen[it["uuid"]] = {"name": rec["name"], "pos": _pos(it["pos"]), "bits": bits}

    def merge(self, other: Sightings) -> None:
        self.known |= other.known
        self.seen.update(other.seen)

    def line(self, handles: Handles, got: dict, walker: str | None, views: tuple = ()) -> str:
        """The PASSED line after the trip's end read (got: delta.fetch's parts); '' when nothing passed or the end's
        view wasn't read (then nothing can be said to be out of view). views: more state.nearby reads of the end
        (around the walker when it isn't the selected member the end read is centred on): in view too."""
        near = got.get("nearby")
        if not isinstance(near, dict):
            return ""
        now = {
            r.get("uuid")
            for v in (near, *views)
            if isinstance(v, dict)
            for k in ("characters", "items")
            for r in v.get(k) or []
            if isinstance(r, dict)
        }
        rows = [(u, r) for u, r in self.seen.items() if u not in self.known and u not in now]
        if not rows:
            return ""
        st = got.get("status")
        at = (
            next(
                (
                    _pos(r.get("pos"))
                    for r in (st or {}).get("party") or []
                    if isinstance(r, dict) and r.get("uuid") == walker
                ),
                None,
            )
            if isinstance(st, dict)
            else None
        )
        at = at or minimap.here(st)

        def far(r: dict) -> float:
            return delta._dist(at, r["pos"]) if at else 0.0

        rows.sort(key=lambda ur: (far(ur[1]), ur[1]["name"]))
        out = []
        for u, r in rows[:MAX_PASSED]:
            s = " ".join([handles.get(u, r["name"]), r["name"], *r["bits"]])
            if at:
                s += f" {render.num(round(far(r)))}m {delta.direction(at, r['pos'])}".rstrip()
                s += minimap._height(r, at)
            out.append(s)
        more = len(rows) - MAX_PASSED
        tail = f" | +{more} more" if more > 0 else ""
        return "PASSED (seen on the way, out of view now): " + " | ".join(out) + tail
