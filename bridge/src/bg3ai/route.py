"""The route ahead (nav.route) as text: what a player reads off the path preview and the scenery between here
and a goal, so the agent doesn't take a screenshot and click its way through (live S001: 23 screenshots and
13 clicks in 14 min, mostly at the fire maze and the ladder).

render_route(res, H, what) is the `route` tool's body; hint_text(res, H) is a 1-3 line version for failure
messages (walk_toward, travel_to stops)."""

from __future__ import annotations

from typing import Any

from . import longnav, macros, render
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


def _connector(c: dict, H: Handles, ends: list | None = None) -> str:
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
    elif c.get("unprobed"):
        bits.append("path not checked")
    if c.get("to_goal_m") is not None:
        bits.append(f"then {_m(c['to_goal_m'])} from the goal" + (" (closer)" if c.get("helps") else ""))
    dead = longnav.connector_dead(c, ends or [])
    if dead:
        bits.append(f"LEADS TO A DEAD END ({longnav.dead_end_text(dead)})")
    return "  " + " ".join(bits)


def route_waypoint(res: dict) -> dict | None:
    """An unlocked waypoint ROUTE_WP_SAVE_M nearer the goal than we are (nav.route's waypoints), or None."""
    if not isinstance(res, dict) or not res.get("from") or not res.get("goal"):
        return None
    return longnav.pick_waypoint(res["from"], res["goal"], res.get("waypoints"), min_m=0,
                                 save_m=longnav.ROUTE_WP_SAVE_M, ratio=1.0)


def _next(res: dict, H: Handles, ends: list | None = None) -> str:
    """One concrete suggestion."""
    wd = res.get("wedged")
    if res.get("reachable") is False and isinstance(wd, dict) and wd.get("to"):
        return f"NEXT: jump_to x,y,z = {pos(wd['to'])} (out of the nook), then route again"
    jx = res.get("jump")
    if res.get("reachable") is False and isinstance(jx, dict) and jx.get("to"):
        return (f"NEXT: travel_to x,y,z = {pos(jx['from'])} (the edge), then jump_to x,y,z = {pos(jx['to'])}, "
                f"then route again")
    lo = next((x for x in res.get("lower") or [] if isinstance(x, dict) and x.get("toward") and x.get("to")
               and isinstance(x.get("reach_m"), (int, float)) and (x.get("gap_m") or 0) <= x["reach_m"]), None)
    if res.get("reachable") is False and lo:
        return (f"NEXT: move_to x,y,z = {pos(lo['from'])} (the edge), then jump_to x,y,z = {pos(lo['to'])} "
                f"({_m(lo.get('drop_m'))} down: a fall that deep hurts without Feather Fall), then route again")
    wp = route_waypoint(res)
    if wp and (res.get("reachable") is False or (res.get("length_m") or 0) > (wp.get("to_goal_m") or 0) + 60):
        return (f"NEXT: travel_to waypoint=\"{wp['name']}\" ({_m(wp.get('to_goal_m'))} from the goal, you are "
                f"{_m(res.get('straight_m'))}), then route again")
    dr = res.get("drop")
    if res.get("reachable") and isinstance(dr, dict) and dr.get("pos"):
        return (f"NEXT: travel_to x,y,z = {pos(dr['pos'])} (the edge of the {_m(dr.get('m'))} drop), then jump_to the "
                f"floor below (jump_to checks the landing), or find a ladder/stairs")
    if res.get("reachable"):
        links = res.get("links") or []
        if links:
            l = links[0]
            return (f"NEXT: travel_to/move_to walks it; if the walk stops at the {l.get('kind')} ({_m(l.get('at_m'))} in), "
                    f"interact with the ladder there or jump_to {pos(l.get('to'))}")
        return "NEXT: travel_to/move_to walks it"
    c = res.get("closest")
    dead = longnav.near_in(ends or [], c.get("pos")) if isinstance(c, dict) and c.get("pos") else None
    if res.get("reachable") is False and (dead or res.get("far")) and any(e.count >= longnav.DEAD_REPEAT for e in ends or []):
        return ("NEXT: this side is closed (" + longnav.dead_end_text(max(ends, key=lambda e: e.count))
                + "): don't walk it again. Come from another side: explore along the roads (get_map), "
                  "wiki_search how to reach it, or another objective first")
    cons = [c for c in res.get("connectors") or [] if not c.get("unreachable") and not c.get("locked")
            and not longnav.connector_dead(c, ends or [])]
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


def wedged_text(w: Any) -> str:
    """nav.wedged / route's res['wedged'] -> '; you are wedged: ...' (S058 08:00), '' when not wedged."""
    if not isinstance(w, dict) or not w.get("wedged") or not w.get("to"):
        return ""  # no member to land next to: maybe a small balcony whose way out is the ladder (S059 08:52)
    return (f"; you are wedged: no walkable ground leads out from where you stand: jump_to x,y,z = {pos(w['to'])} "
            f"next to {w.get('near') or 'a party member'} ({_m(w.get('dist_m'))})"
            + (" if it is in reach" if w.get("in_reach") is False else "") + ", then try again")


def render_route(res: Any, H: Handles, what: str = "it", ends: list | None = None) -> str:
    """ends: this session's dead ends toward this goal (longnav.DeadEnds.for_goal)."""
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
        sm, lm = res.get("straight_m"), res.get("length_m")
        if isinstance(sm, (int, float)) and isinstance(lm, (int, float)) and sm > 20 and lm > 1.6 * sm:
            line += f" (a long way round: {num(round(lm / sm, 1))}x the straight line)"
        if res.get("links"):
            line += ", with " + ", ".join(f"a {l.get('kind')} ({_link_size(l)}) at {_m(l.get('at_m'))}" for l in res["links"][:3])
        out.append(line)
        out.extend(_legs(res.get("legs")))
        dr = res.get("drop")
        if isinstance(dr, dict) and dr.get("pos"):  # S053 05:49: PATH shown, then travel_to's leg refused the drop
            out.append(f"  DROPS {_m(dr.get('m'))} at {pos(dr['pos'])} (fall damage): walks stop there; take a ladder/"
                       f"stairs (get_frontier), or move_to that edge and jump_to the floor below on purpose")
        if res.get("exposure"):
            ex = res["exposure"]
            out.append(f"  crosses {macros.exposure_text(ex)} at {_m(ex.get('at_m'))} @{pos(ex.get('pos'))}")
    elif res.get("reachable") is False:
        line = "NO WALKABLE PATH" + (" from here: the engine finds no way there from this side" if res.get("far") else "")
        if res.get("no_path_reason"):  # the goal itself: no ground there (the void) / a Chasm (S039 21:44)
            line += f": {res['no_path_reason']}"
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
        jx = res.get("jump")
        if isinstance(jx, dict) and jx.get("to"):  # S032 Yurgir: the broken staircase was a jump, never a walk
            dy = jx.get("dy") or 0
            hd = f", {_m(abs(dy))} {'down' if dy < 0 else 'up'}" if abs(dy) >= 1 else ""
            out.append(f"JUMP ACROSS: from {pos(jx['from'])} (walk {_m(jx.get('walk_m'))}) over a {_m(jx.get('gap_m'))} "
                       f"gap{hd} to {pos(jx['to'])}, {_m(jx.get('to_goal_m'))} from the goal")
        for lo in (res.get("lower") or [])[:3]:  # S038/S039 Shadowfell: the way down is islands far below the ledge
            if not isinstance(lo, dict) or not lo.get("to"):
                continue
            line = (f"LOWER GROUND {lo.get('dir') or ''}: a floor {_m(lo.get('drop_m'))} below at {pos(lo['to'])}, "
                    f"{_m(lo.get('gap_m'))} out from the edge at {pos(lo.get('from'))}")
            if lo.get("to_goal_m") is not None:
                line += f", {_m(lo['to_goal_m'])} from the goal"
            reach = lo.get("reach_m")
            if isinstance(reach, (int, float)) and isinstance(lo.get("gap_m"), (int, float)):
                line += (f"; jumping down from there reaches ~{_m(reach)}: move_to the edge, then jump_to {pos(lo['to'])}"
                         if lo["gap_m"] <= reach else f"; beyond a jump from there (~{_m(reach)})")
                # the drop extends the reach (S040), but a long fall hurts unless the jumper has Feather Fall
                if lo["gap_m"] <= reach and isinstance(lo.get("drop_m"), (int, float)) and lo["drop_m"] >= 6 \
                        and not res.get("feather_fall"):
                    line += f" (a {_m(lo['drop_m'])} fall: fall damage unless the jumper has Feather Fall)"
            out.append(line)
        if res.get("ways"):  # the open directions from here, as get_frontier says them
            out.append(render.render_ways(res["ways"]))
        wt = wedged_text(res.get("wedged"))
        if wt:
            out.append("WEDGED" + wt[len("; you are wedged"):])
    else:
        why = res.get("unknown_reason")  # S039 21:44: after a jump onto a rock off the walkable grid ("midair")
        out.append(f"path unknown: {why}" + ("; step onto plain ground (move_to a floor near you) and route again"
                                             if "no floor under" in str(why) else "")
                   if why else "path unknown (pathfinding unavailable)")
    vn = res.get("visited_near")
    if isinstance(vn, dict) and vn.get("pos") and (vn.get("to_goal_m") or 0) < (res.get("straight_m") or 0) - 5:
        out.append(f"CLOSEST YOU HAVE BEEN: {pos(vn['pos'])}, {_m(vn.get('to_goal_m'))} from it")
    for e in (ends or [])[:3]:
        out.append(f"BLOCKED BEFORE (this session): {longnav.dead_end_text(e)}")
    wps = res.get("waypoints") or []
    if wps:
        out.append("WAYPOINTS (unlocked, nearest the goal first): " + "; ".join(
            f"{w.get('name')} {_m(w.get('to_goal_m'))} from it (you: {_m(w.get('dist'))})" for w in wps[:3]))
    cons = res.get("connectors") or []
    if cons:
        out.append("LADDERS/DOORS in view (best first):")
        out.extend(_connector(c, H, ends) for c in cons)
    out.append(_next(res, H, ends))
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
    wt = wedged_text(res.get("wedged"))
    if wt:
        out.append(wt.lstrip("; "))
    best = next((x for x in res.get("connectors") or [] if not x.get("unreachable")), None)
    if best:
        out.append(_connector(best, H).strip())
    out.append(_next(res, H))
    return "\n".join(out)
