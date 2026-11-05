"""The minimap block: what a player reads off the HUD's compass and minimap at any moment, as three lines.

    OBJECTIVES o1 Escape the ship 16m W [Escape the Nautiloid] | o2 Find the helm 40m N | +2 more
    WAYS walkable: N 20m+, E 8m | blocked: S W
    LEADS (unvisited; get_frontier lists all): e529 Damaged Vase <usable> 8.3m E | e12 Door <door> 14m N | +4 more

It ends every walk out of combat (walk_toward, travel_to, move_to, jump_to, regroup, a fast travel's arrival) and
every `look`, always, not only when something changed (human-gap research 2026-11-02: after 44-82% of launch-v1's
walk ends the next call asked where things were: get_frontier, get_layout, a map screenshot). One renderer for
every caller. Its inputs come from reads the result makes anyway (delta.fetch: state.status, nav.markers, and
nav.frontier in the same eval), so it adds no RPC of its own. Facts only: the active objective markers (distance,
compass direction; no height: the HUD's quest diamonds show none, live window 2026-11-03), the open
directions, and the nearest things not walked to yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import render
from .render import Handles

MAX_OBJECTIVES = 3  # objective markers shown (here first, nearest first: the mod's order), then "+N more"
MAX_LEADS = 3  # unvisited leads shown, nearest first, then "+N more"
DY_MIN_M = 1.5  # a height difference this big is shown ("3m down") on a PASSED row, as get_frontier shows a lead's


def here(status: Any) -> Sequence[Any] | None:
    """The active member's position in a state.status payload (where the markers' distances are measured from)."""
    if not isinstance(status, dict):
        return None
    rows = [r for r in status.get("party") or [] if isinstance(r, dict)]
    row = next((r for r in rows if r.get("uuid") == status.get("active")), rows[0] if rows else None)
    p = (row or {}).get("pos")
    return p if isinstance(p, (list, tuple)) and len(p) >= 3 else None


def _height(m: dict, at: Sequence[Any] | None) -> str:
    p = m.get("pos")
    if at is None or not (isinstance(p, (list, tuple)) and len(p) >= 3):
        return ""
    dy = float(p[1]) - float(at[1])
    return f" ({render.num(round(abs(dy), 1))}m {'up' if dy > 0 else 'down'})" if abs(dy) >= DY_MIN_M else ""


def objectives_line(markers: Any) -> str:
    """'OBJECTIVES o1 <label> 16m W [quest] | ... | +2 more'; '' when the markers weren't read. No height: the
    compass and minimap show none for a quest marker (live window 2026-11-03), so it would be a fact off the screen."""
    if not isinstance(markers, list):
        return ""
    shown: list[str] = []
    ids: list[list[str]] = []
    more = 0
    for i, m in enumerate(markers, 1):
        if not isinstance(m, dict):
            continue
        label = render.objective_label(m)
        if m.get("here") and m.get("dist") is not None:
            s = f"{label} {render.num(m['dist'])}m {m.get('dir') or ''}".rstrip()
        else:
            s = f"{label} (another area)"
        quest = m.get("quest")
        if quest and quest != label:
            s += f" [{quest}]"
        if s in shown:  # two markers of one target (render_objectives): one entry, both ids
            ids[shown.index(s)].append(f"o{m.get('oid') or i}")
        elif len(shown) < MAX_OBJECTIVES:
            shown.append(s)
            ids.append([f"o{m.get('oid') or i}"])
        else:
            more += 1
    if not shown:
        return "OBJECTIVES none on the map"
    parts = [f"{'/'.join(d)} {s}" for d, s in zip(ids, shown, strict=True)]
    return "OBJECTIVES " + " | ".join(parts) + (f" | +{more} more (get_map lists all)" if more else "")


def leads_line(frontier: Any, handles: Handles) -> str:
    """'LEADS (unvisited; get_frontier lists all): <the MAX_LEADS nearest unvisited> | +N more'; '' without a read."""
    if not isinstance(frontier, dict) or not isinstance(frontier.get("leads"), list):
        return ""
    new = sorted(
        (x for x in frontier["leads"] if isinstance(x, dict) and x.get("unvisited")),
        key=lambda x: float(x["dist"]) if isinstance(x.get("dist"), (int, float)) else float("inf"),
    )
    head = "LEADS (unvisited; get_frontier lists all): "
    if not new:
        return head + "none"
    shown = render.render_frontier({"leads": new[:MAX_LEADS]}, handles).splitlines()
    more = len(new) - MAX_LEADS + (frontier.get("more") or 0)
    return head + " | ".join(x.replace(" unvisited", "") for x in shown) + (f" | +{more} more" if more > 0 else "")


def block(got: dict, handles: Handles) -> str:
    """The block from one delta.fetch() result (parts status, markers, frontier); '' when none of them was read."""
    frontier = got.get("frontier")
    lines = [
        objectives_line(got.get("markers")),
        render.render_ways(frontier.get("ways")) if isinstance(frontier, dict) else "",
        leads_line(frontier, handles),
    ]
    return "\n".join(x for x in lines if x)
