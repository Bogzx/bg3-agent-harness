"""The route ahead (nav.route) as text: what a player reads off the path preview and the scenery between here
and a goal, so the agent doesn't take a screenshot and click its way through (live S001: 23 screenshots and
13 clicks in 14 min, mostly at the fire maze and the ladder).

render_route(res, H, what) is the `route` tool's body; hint_text(res, H) is a 1-3 line version for failure
messages (walk_toward, travel_to stops)."""

from __future__ import annotations

from typing import Any

from . import macros
from .render import Handles, num, pos


def _m(x: Any) -> str:
    return f"{num(round(float(x), 1))}m" if isinstance(x, (int, float)) else "?m"


def _height(dy: Any, min_m: float = 1.5) -> str:
    """9.2 -> '9.2m above you'; small differences -> ''."""
    if not isinstance(dy, (int, float)) or abs(dy) < min_m:
        return ""
    return f"{_m(abs(dy))} {'above' if dy > 0 else 'below'} you"


def _levels(ls: Any, here: bool = True) -> str:
    if not isinstance(ls, list) or not ls:
        return "?"
    return ", ".join("0 (you)" if here and abs(x) < 0.5 else (f"+{num(x)}" if x > 0 else num(x)) for x in ls)


def _legs(legs: Any, limit: int = 8) -> list[str]:
    out = []
    for i, l in enumerate(legs or [], 1):
        if i > limit:
            out.append(f"  (+{len(legs) - limit} more legs)")
            break
        if l.get("link"):
            out.append(f"  {i}. {l['link'].upper()} {_link_size(l)} to {pos(l.get('to'))}")
            continue
        h = _height(l.get("dy"), 1.0).replace(" you", "")
        out.append(f"  {i}. {_m(l.get('m'))} {l.get('dir') or ''}" + (f" ({h.replace('above', 'up').replace('below', 'down')})" if h else ""))
    return out


def _link_size(l: dict) -> str:
    dy = l.get("dy")
    if l.get("link") == "jump" or l.get("kind") == "jump":
        return f"{_m(l.get('m'))} gap"
    return f"{_m(abs(dy))} {'up' if (dy or 0) > 0 else 'down'}" if isinstance(dy, (int, float)) else ""


def _connector(c: dict, H: Handles) -> str:
    h = H.get(c["uuid"], c.get("name")) if c.get("uuid") else "?"
    bits = [f"{h} {c.get('name', '?')} <{c.get('kind', '?')}> {_m(c.get('dist'))} {c.get('dir') or ''}".rstrip()]
    hd = _height(c.get("dy"))
    if hd:
        bits.append(f"(on a floor {hd})")
    if c.get("kind") == "ladder":
        if c.get("leads") and c.get("height_m"):
            bits.append(f"climbs {_m(c['height_m'])} {c['leads']}")
        bits.append("[interact to climb]")
    if c.get("locked"):
        bits.append("LOCKED")
    if c.get("unreachable"):
        bits.append("no walkable path to it")
    elif c.get("path_m") is not None:
        bits.append(f"walk {_m(c['path_m'])}")
    if c.get("to_goal_m") is not None:
        bits.append(f"then {_m(c['to_goal_m'])} from the goal" + (" (closer)" if c.get("helps") else ""))
    return "  " + " ".join(bits)


def _next(res: dict, H: Handles) -> str:
    """One concrete suggestion."""
    if res.get("reachable"):
        links = res.get("links") or []
        if links:
            l = links[0]
            return (f"NEXT: travel_to/move_to walks it; if the walk stops at the {l.get('kind')} ({_m(l.get('at_m'))} in), "
                    f"interact with the ladder there or jump_to {pos(l.get('to'))}")
        return "NEXT: travel_to/move_to walks it"
    cons = [c for c in res.get("connectors") or [] if not c.get("unreachable") and not c.get("locked")]
    up = res.get("dy") or 0
    if abs(up) >= 2.5:
        want = "up" if up > 0 else "down"
        lad = next((c for c in cons if c.get("kind") == "ladder" and c.get("leads") in (want, None)), None)
        if lad:
            return f"NEXT: interact {H.get(lad['uuid'], lad.get('name'))} (ladder {want}), then route again"
    helper = next((c for c in cons if c.get("helps")), None)
    if helper:
        verb = "interact" if helper.get("kind") == "ladder" else "open"
        return f"NEXT: {verb} {H.get(helper['uuid'], helper.get('name'))} ({helper.get('kind')}), then route again"
    c = res.get("closest")
    if isinstance(c, dict) and c.get("pos"):
        tail = ""
        if abs(c.get("dy_to_goal") or 0) >= 2.5:
            tail = "; from there it is " + _height(c["dy_to_goal"]).replace(" you", "") + ": find a ladder/ledge (get_frontier) or jump_to"
        elif res.get("straight_hazard"):
            tail = "; the rest crosses " + macros.exposure_text(res["straight_hazard"]) + ": travel_to crosses short patches"
        return f"NEXT: travel_to x,y,z = {pos(c['pos'])} (the closest walkable spot){tail}"
    if res.get("straight_hazard"):
        return ("NEXT: travel_to (it crosses short patches on purpose) or jump_to over "
                + macros.exposure_text(res["straight_hazard"]))
    return "NEXT: get_frontier for doors/exits, or walk_toward another direction"


def render_route(res: Any, H: Handles, what: str = "it") -> str:
    if not isinstance(res, dict):
        return str(res)
    head = f"ROUTE to {what}: {_m(res.get('straight_m'))} {res.get('dir') or ''}".rstrip()
    hd = _height(res.get("dy"))
    if hd:
        head += f", {hd}"
    out = [head]
    if res.get("standing_in"):
        out.append(f"you stand in {res['standing_in']}: step out first")
    lh, lg = res.get("levels_here"), res.get("levels_goal")
    if (isinstance(lh, list) and len(lh) > 1) or (isinstance(lg, list) and any(abs(x) >= 1.5 for x in lg)):
        out.append(f"FLOORS here: {_levels(lh)} · at the goal: {_levels(lg, False)} (m, relative to you)")
    if res.get("too_far"):
        out.append("too far to preview the walk: travel_to goes in 30 m legs")
    elif res.get("reachable"):
        line = f"PATH {_m(res.get('length_m'))}"
        if res.get("links"):
            line += ", with " + ", ".join(f"a {l.get('kind')} ({_link_size(l)}) at {_m(l.get('at_m'))}" for l in res["links"][:3])
        out.append(line)
        out.extend(_legs(res.get("legs")))
        if res.get("exposure"):
            ex = res["exposure"]
            out.append(f"  crosses {macros.exposure_text(ex)} at {_m(ex.get('at_m'))} @{pos(ex.get('pos'))}")
    elif res.get("reachable") is False:
        line = "NO WALKABLE PATH"
        if res.get("straight_hazard"):
            sh = res["straight_hazard"]
            line += f" (the straight line crosses {macros.exposure_text(sh)} at {_m(sh.get('at_m'))})"
        out.append(line)
        c = res.get("closest")
        if isinstance(c, dict) and c.get("pos"):
            line = f"CLOSEST walkable spot {pos(c['pos'])}: {_m(c.get('to_goal_m'))} from the goal"
            hg = _height(-(c.get("dy_to_goal") or 0))  # the spot relative to the goal
            if hg:
                line += ", " + hg.replace(" you", " it")
            line += f"; walk {_m(c.get('path_m'))}"
            out.append(line)
            out.extend(_legs(c.get("legs"), 4))
            if c.get("exposure"):
                out.append(f"  crosses {macros.exposure_text(c['exposure'])}")
    else:
        out.append("path unknown (pathfinding unavailable)")
    cons = res.get("connectors") or []
    if cons:
        out.append("LADDERS/DOORS in view (best first):")
        out.extend(_connector(c, H) for c in cons)
    out.append(_next(res, H))
    return "\n".join(out)


def hint_text(res: Any, H: Handles) -> str:
    """Failure-message version: the height gap, the closest spot and the best ladder/door, in 1-3 lines."""
    if not isinstance(res, dict) or res.get("reachable"):
        return ""
    bits = []
    hd = _height(res.get("dy"))
    if hd:
        bits.append(f"the goal is {hd}")
    c = res.get("closest")
    if isinstance(c, dict) and c.get("pos") and (c.get("to_goal_m") or 0) >= 2:
        bits.append(f"closest walkable spot {pos(c['pos'])} is {_m(c.get('to_goal_m'))} from it")
    out = ["ROUTE: " + "; ".join(bits)] if bits else []
    best = next((x for x in res.get("connectors") or [] if not x.get("unreachable")), None)
    if best:
        out.append(_connector(best, H).strip())
    out.append(_next(res, H))
    return "\n".join(out)
