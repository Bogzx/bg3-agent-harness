"""Observe tools, part 2: nearby, inventory, journal, map, frontier, layout, route, combat state, dialogue, events,
screenshots.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image

from .. import delta, macros, navhints, render, turn
from .. import route as route_text
from ..game import GameError
from . import core, overview, plumbing, reporting


def _nearby_kinds(kinds: str) -> tuple[list[str], list[str]]:
    """Split get_nearby's kinds into mod kinds and name words. S026: kinds="button" and kinds="props" (the
    delta's own "+37 more: props 37" word) both answered "Nothing visible in range" while Buttons stood 5 m away."""
    mod, words = [], []
    for k in (k.strip().lower() for k in kinds.split(",") if k.strip()):
        k1 = {"chars": "characters", "npcs": "characters", "doors": "door", "containers": "container",
              "ladders": "ladder", "traps": "trap", "objects": "object"}.get(k, k)
        (mod if k1 in overview._NEARBY_KINDS else words).append(k1)
    if words:
        mod = sorted(set(mod) | {"characters", "items", "object"})
    return mod, words


def _nearby_match(res: dict, words: list[str]) -> dict:
    """Keep what a word names: "props" = decoration (candles, vases...), anything else = a name substring
    (singular or plural)."""
    def hit(o: dict) -> bool:
        name = str(o.get("name") or "").lower()
        for w in words:
            if w == "props":
                if delta.is_clutter(o):
                    return True
            elif w in name or (w.endswith("s") and w[:-1] in name):
                return True
        return False
    out = dict(res)
    out["characters"] = [c for c in res.get("characters") or [] if hit(c)]
    out["items"] = [it for it in res.get("items") or [] if hit(it)]
    out["remembered"] = [r for r in res.get("remembered") or [] if hit(r)]
    out.pop("more_items", None)
    return out


def _nearby_miss_text(full: dict, words: list[str], radius: float) -> str:
    """A name search that matched nothing: what was asked, then the characters in view (names + ids)."""
    names = ", ".join(repr(w) for w in words)
    chars = [c for c in full.get("characters") or [] if isinstance(c, dict) and c.get("uuid")]
    out = f"No {names} in view within {render.num(radius)}m (a name match, part of a name is enough)."
    if chars:
        shown = [f"{core.H.get(c['uuid'], c.get('name'))} {c.get('name') or '?'}" for c in chars[:8]]
        out += " Characters in view: " + ", ".join(shown) + (f" (+{len(chars) - 8} more)" if len(chars) > 8 else "")
    else:
        out += " No characters in view."
    return out + " Someone out of sight: get_map / the journal for where they went, or route to their last place."


def _nearby_party_lines(words: list[str]) -> list[str]:
    """Party members a name search names (state.nearby lists only others): S057 07:49 / S058 07:52-07:54
    get_nearby kinds="Shadowheart,Wyll" → "No 'shadowheart', 'wyll' in view" ×4 while both lay dead nearby; the
    agent took screenshots to find the bodies for a Revivify. One line each: id, name, dead/HP, distance + way."""
    words = [w for w in words if w not in ("props", "characters", "items")]
    if not words:
        return []
    st = core.game.try_rpc("state.status") or {}
    rows = [c for c in st.get("party") or [] if isinstance(c, dict) and c.get("uuid")]
    me = next((c for c in rows if c.get("uuid") == st.get("active")), rows[0] if rows else None)
    out = []
    for c in rows:
        name = str(c.get("name") or "").lower()
        if not any(w in name for w in words):
            continue
        bits = [f"PARTY {core.H.get(c['uuid'], c.get('name'))} {c.get('name') or '?'}"]
        if c.get("dead"):
            bits.append("dead (its body)")
        elif c.get("hp") is not None:
            bits.append(f"HP {c.get('hp')}/{c.get('max_hp')}")
        if me and me is not c and me.get("pos") and c.get("pos"):
            bits.append(f"{render.num(round(macros.dist(me['pos'], c['pos']), 1))}m {delta.direction(me['pos'], c['pos'])}")
        out.append(" ".join(bits) + " (your party: target it by this id)")
    return out


@core.mcp.tool()
@reporting._guard
def get_nearby(radius: float = 25.0, kinds: str = "") -> str:
    """Visible characters and interactable objects near the party (ids, distance, relation, HP, locked/opened, [lit]/[glowing] = an object's visible puzzle state, e.g. a painting lit by its button), plus remembered last-seen positions. kinds: optional filter: characters, items, door, container, item, ladder, usable, trap, props (decoration), or words to match names (e.g. "button,lever"). Lookalikes show as one line "(+N more <name>)"; kinds=<that name> lists each with its id."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"radius": radius}
    words: list[str] = []
    if kinds:
        args["kinds"], words = _nearby_kinds(kinds)
        if words:
            args["item_limit"] = 400
    res = reporting._read1("state.nearby", args)
    party_lines: list[str] = []
    if words and isinstance(res, dict):
        full, res = res, _nearby_match(res, words)
        party_lines = _nearby_party_lines(words)
        if not any(res.get(k) for k in ("characters", "items", "remembered")):
            if party_lines:
                return reporting._finish("get_nearby", args, "\n".join(party_lines), t0)
            # S042 22:51 kinds="Jaheira,characters" → "Nothing visible in range." while 9 corpses and the party stood
            # there: say the name search found nothing, and who IS in view
            plumbing._remember_names(full)
            return reporting._finish("get_nearby", args, _nearby_miss_text(full, words, radius), t0)
    elif kinds and "characters" not in args["kinds"] and isinstance(res, dict):
        # Remembered = characters out of sight: noise in a search for doors/items (S018-S035: 65 of 123 such calls
        # carried ~4 REMEMBERED lines each, e.g. the Self-Same mirrors in every gem search)
        res = {k: v for k, v in res.items() if k != "remembered"}
    plumbing._remember_names(res)
    if radius == 25.0 and not kinds and isinstance(res, dict):
        core.DELTA.observe(delta.snapshot(core.H, nearby=res))  # the agent has now seen these; look() won't repeat them
    text = render.render_nearby(res, core.H, fold=not [w for w in words if w != "props"])
    return reporting._finish("get_nearby", args, "\n".join(party_lines + [text]) if party_lines else text, t0)


@core.mcp.tool()
@reporting._guard
def look_at(target: str) -> str:
    """What the Examine panel shows for a visible character or object (stats, resistances, conditions, description)."""
    t0 = time.monotonic()
    res = reporting._read1("state.entity", {"uuid": plumbing._uuid(target)})
    plumbing._remember_names(res)
    return reporting._finish("look_at", {"target": target}, render.compact(render.public_levels(_shown_statuses(res)), core.H), t0)


def _shown_statuses(res: Any) -> Any:
    """Examine shows a condition's name, never the engine's status id (SCL_MOONSHIELD, MAG_..._BUFF: ids carry
    hints the name doesn't; fairness audit S023-S027). The id stays only where a status has no name."""
    if not isinstance(res, dict) or not isinstance(res.get("statuses"), list):
        return res
    sts = [{k: v for k, v in s.items() if k != "id"} if isinstance(s, dict) and s.get("name") else s
           for s in res["statuses"]]
    return {**res, "statuses": sts}


@core.mcp.tool()
@reporting._guard
def get_inventory(who: str | None = None, filter: str = "") -> str:
    """Party inventory (or one member's): equipment, notable items (story, magic, consumables, tools, bags) and a junk summary; plus the open container, camp supplies carried and gear upgrades. filter: "equipped" | "consumables" | "food" (camp supplies: supply=N per item) | "junk" (what trade sell=junk sells) | "gear" (better gear in bags) | "all" | words to match names (e.g. "potion")."""
    t0 = time.monotonic()
    args: dict[str, Any] = {}
    if who:
        args["uuid"] = plumbing._who_uuid(who)
    if filter and filter.strip().lower() not in ("equipped", "notable"):
        args["expand"] = True  # food sits in the Camp Supply Sacks, keys on the keychain (S026: filter=food found nothing)
    (st, res), _ = reporting._read([("state.status", {}), ("state.inventory", args)])
    plumbing._remember_names(st or {})  # party first: short handles + owner names
    if res is None:
        res = core.game.rpc("state.inventory", args)  # surfaces the mod's error
    plumbing._remember_names(res)
    if not who and isinstance(res, dict):
        core.DELTA.observe({"inv": delta.snapshot(core.H, inventory=res)["inv"]})  # seen now; look() reports later changes
    return reporting._finish("get_inventory", {"who": who, "filter": filter}, render.render_inventory(res, core.H, filter), t0)


@core.mcp.tool()
@reporting._guard
def get_journal(quest: str = "") -> str:
    """Active quests with their latest journal entry, plus finished quest titles. quest: words from a title to see all of that quest's entries."""
    t0 = time.monotonic()
    res = reporting._read1("state.journal")
    if isinstance(res, list):
        core.DELTA.observe(delta.snapshot(core.H, journal=res))
    return reporting._finish("get_journal", {"quest": quest}, render.render_journal(res, core.H, quest), t0)


@core.mcp.tool()
@reporting._guard
def get_map(full: bool = False) -> str:
    """Current region/subregion, unlocked waypoints, active quest objectives (distance, direction), and your own map markers.
    After the first call it shows only what changed (new waypoints, objectives added/done, region); full=true for everything."""
    t0 = time.monotonic()
    (res, mk), _ = reporting._read([("state.map", {}), ("nav.markers", {})])
    if res is None:
        res = core.game.rpc("state.map")
    ms = mk.get("markers") if isinstance(mk, dict) and isinstance(mk.get("markers"), list) else None
    if ms is not None:
        overview._set_objectives(ms)
    first = not core.DELTA.has("waypoints")
    snap = delta.snapshot(core.H, map_=res, markers=ms)
    region = " · ".join(str(x) for x in (res.get("subregion"), res.get("region")) if x) if isinstance(res, dict) else ""
    snap["map_region"] = region
    moved_area = core.DELTA.last.get("map_region") not in (None, region)
    lines = core.DELTA.observe(snap)
    markers = session._load_markers()
    if full or first or moved_area:
        body = render.render_map(res, core.H)
        obj = render.render_objectives(ms or [])
        if obj:
            body += "\nOBJECTIVES\n" + "\n".join(obj)
        if markers:
            body += "\nMARKERS\n" + "\n".join(f"  {m['id']} {m['label']} @{render.pos(m.get('pos'))} ({m.get('region', '')})" for m in markers)
    else:
        body = "\n".join(lines) if lines else _map_same_text(region, ms, markers)
    return reporting._finish("get_map", {"full": full}, body, t0)


# naut2 run 1: on arrival at o1 the agent asked get_map ("No map change (Nautiloid; 1 objectives, 0 markers)"),
# get_status ("No change"), then get_map full=true to learn o1 now stood 66.8m W: two model turns (~3.5 s) while the
# character stood still. The no-change answer names this area's objectives with their distance now.
MAP_SAME_OBJECTIVES = True
MAP_SAME_OBJECTIVES_MAX = 3


def _map_same_text(region: str, ms: list | None, markers: list) -> str:
    head = f"No map change ({region or '?'}; {len(ms or [])} objectives, {len(markers)} markers)."
    obj = render.render_objectives(ms or [], here_only=True, limit=MAP_SAME_OBJECTIVES_MAX) if MAP_SAME_OBJECTIVES else []
    obj = [ln.strip() for ln in obj if ln.strip() and not ln.strip().startswith("(+")]
    return head + (" Here: " + "; ".join(o.removeprefix("OBJECTIVE ") for o in obj) + "." if obj else "") \
        + " full=true lists everything."


@core.mcp.tool()
@reporting._guard
def get_frontier() -> str:
    """Leads worth exploring: visible doors, ladders, stairs, exits and unvisited things near you, with distance and direction. Use it to decide where to go next."""
    t0 = time.monotonic()
    res = reporting._read1("nav.frontier", {})
    plumbing._remember_names(res)
    return reporting._finish("get_frontier", {}, render.render_frontier(res, core.H), t0)


@core.mcp.tool()
@reporting._guard
def get_layout(radius: float = 12.0, who: str | None = None) -> str:
    """The walls, openings and floor around you as a text map (what the overhead camera shows), N up, you = @: mazes, rooms, ledges, chasms. Marks the party (1-3), characters (E hostile, n other), doors (D), ladders (H), levers/buttons (L), containers (C). Ground the party hasn't seen or walked is '?'. radius 4-20 m."""
    t0 = time.monotonic()
    char = plumbing._who_uuid(who) if who else plumbing._active()
    lay = reporting._read1("nav.layout", {"char": char, "radius": radius})
    st = core.game.try_rpc("state.status") or {}
    near = core.game.try_rpc("state.nearby", {"radius": min(max(radius, 4.0), 20.0) * 1.5}) or {}
    plumbing._remember_names(near)
    text = render.render_layout(lay if isinstance(lay, dict) else {}, core.H, near, st.get("party") or [], char)
    return reporting._finish("get_layout", {"radius": radius}, text, t0)


@core.mcp.tool()
@reporting._guard
def route(objective: str = "", target: str = "", x: float | None = None, y: float | None = None,
          z: float | None = None, who: str | None = None, connectors: bool = False, go: bool = False) -> str:
    """The way to a goal, read from the engine instead of a screenshot: objective (o1, or a map marker m1), a visible target id, or x,y,z.
    Says how far and how high it is, the walk in legs (8m NW, CLIMB 9m up, 6m E) with
    fire in view on the way, and when no path exists: the closest walkable spot and the ladders/doors in view ranked by how much they
    help (which way a ladder climbs), ending with one NEXT step. connectors=true lists ladders/doors even when a path exists.
    A goal on ground the party hasn't seen or walked gets only its distance and direction.
    go=true: check and go in one call: starts travel_to the goal when a path exists, else to the closest walkable spot."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": plumbing._who_uuid(who)} if who else {}  # no char: nav.route uses the active one
    t = (target or objective).strip()
    if re.fullmatch(r"[om]\d+", t) or (objective and not target):  # objective o1 / words, map marker m1
        marker = t if re.fullmatch(r"m\d+", t) else ""
        args["pos"], what = macro_tools._travel_dest(marker, "" if marker else t, None, None, None)
    elif target:
        args["target"] = plumbing._uuid(target)
        what = target
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
        what = render.pos([x, y, z])
    else:
        raise ValueError("give objective=o1 (or m1), target=<id>, or x,y,z")
    if connectors:
        args["connectors"] = True
    seen_txt = ""
    try:
        res = reporting._read1("nav.route", args)
    except GameError as e:
        seen = world_tools._last_seen(args["target"]) if "target" in args and "not visible" in str(e) else None
        if not seen:
            raise
        # out of sight now: the way to where the party last saw them (S025 route e992/e6 "not visible")
        seen_txt = world_tools._seen_line(target, seen) + "\n"
        args["pos"] = seen[0]
        del args["target"]
        res = reporting._read1("nav.route", args)
    plumbing._remember_names(res)
    ends = batch.DEAD.for_goal(args.get("pos") or (res.get("goal") if isinstance(res, dict) else None))
    tele = plumbing._teleport_hint(res)
    warned = _offlimits_route_line(res)
    text = reporting._finish("route", {"objective": objective, "target": target},
                   seen_txt + route_text.render_route(res, core.H, what, ends) + tele + warned, t0)
    if go and isinstance(res, dict):
        # S023-S026: route then travel_to the same goal (17x) or to its closest walkable spot (12x): 2 model turns.
        c = res.get("closest") if isinstance(res.get("closest"), dict) else {}
        dr = res.get("drop") if isinstance(res.get("drop"), dict) else {}
        if res.get("reachable") and isinstance(dr.get("pos"), (list, tuple)) and len(dr["pos"]) >= 3:
            dest = {"x": dr["pos"][0], "y": dr["pos"][1], "z": dr["pos"][2]}  # walks refuse the drop: to its edge
        elif res.get("reachable") or res.get("unknown_goal"):  # unknown: travel_to's legs end on ground in view
            dest = {"target": target} if target and "pos" not in args else {"x": args["pos"][0], "y": args["pos"][1],
                                                                             "z": args["pos"][2]}
        elif isinstance(c.get("pos"), (list, tuple)) and len(c["pos"]) >= 3:
            dest = {"x": c["pos"][0], "y": c["pos"][1], "z": c["pos"][2]}
        else:
            return text + "\ngo: not started (no path and no closest walkable spot)"
        return text + "\nGO " + travel_macro.travel_to(who=who, **dest)
    return text


def _offlimits_route_line(res: Any) -> str:
    """route: a line 'avoiding the off-limits area by ... you were warned about' (or the goal inside one), '' when no
    warned area of this level is on the way (vis V26)."""
    if not (reporting.OFFLIMITS.areas and isinstance(res, dict)):
        return ""
    st = core.game.try_rpc("state.status") or {}
    line = reporting.OFFLIMITS.route_line(st.get("level") if isinstance(st, dict) else None, res.get("from"), res.get("goal"),
                                res.get("legs") if res.get("reachable") else None)
    return "\n" + line if line else ""


@core.mcp.tool()
@reporting._guard
def get_explored() -> str:
    """Where the party has been in this area (your own breadcrumbs): size of explored region and recent named places."""
    t0 = time.monotonic()
    res = reporting._read1("nav.explored", {})
    return reporting._finish("get_explored", {}, render.compact(render.public_levels(res), core.H), t0)


@core.mcp.tool()
@reporting._guard
def path_preview(target: str = "", x: float | None = None, y: float | None = None, z: float | None = None,
                 who: str | None = None) -> str:
    """Walking distance to a target id or x,y,z: reachable?, path length, needs a jump?, fits remaining movement? Use before committing combat movement. A spot the party hasn't seen or walked gets the straight line only."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": plumbing._who(who)}
    if target and re.fullmatch(r"[om]\d+", target.strip()):  # objective o1 / map marker m1, as travel_to takes
        t = target.strip()
        args["pos"] = macro_tools._travel_dest(t if t[0] == "m" else "", t if t[0] == "o" else "", None, None, None)[0]
    elif target:
        args["target"] = plumbing._uuid(target)
    elif None not in (x, y, z):
        args["pos"] = [x, y, z]
    else:
        raise ValueError("give target=<id or o1/m1> or x,y,z")
    res = core.game.rpc("nav.path", args)
    return reporting._finish("path_preview", {"target": target}, render.render_path(res, core.H), t0)


def _route_hint(args: dict) -> str:
    """nav.route for a failed walk toward a pos/target: the height gap, the closest spot, the best ladder/door,
    and one NEXT step (route.hint_text). '' if it can't be had."""
    q = {k: args[k] for k in ("char", "pos", "target") if k in args}
    res = core.game.try_rpc("nav.route", q) if ("pos" in q or "target" in q) else None
    if not isinstance(res, dict):
        return ""
    plumbing._remember_names(res)
    return route_text.hint_text(res, core.H)


def _wedged_hint(char: str | None, goal: list | None = None) -> str:
    """'; you are wedged: ... jump_to (x,y,z) next to Wyll (2.8m)' when no floor around the walker is walkable
    (nav.wedged, S058 08:00: stuck in a nook after a pick_up, 6 calls to guess the jump out), else ''. goal: no
    landing on a floor farther from it (naut2 run 3: the jump went back down the ladder just climbed)."""
    args: dict[str, Any] = {"char": char} if char else {}
    if goal and len(goal) == 3:
        args["goal"] = list(goal)
    try:  # a hint only: never let it break the failure it decorates
        w = core.game.try_rpc("nav.wedged", args)
    except Exception:
        return ""
    return route_text.wedged_text(w)


# ACT-P3: in a fight, a walk_toward longer than the movement left walks as far as it goes (says so) instead of failing.
# BG3AI_WALK_CLIP_MOVEMENT=0 reverts.
WALK_CLIP_MOVEMENT = os.environ.get("BG3AI_WALK_CLIP_MOVEMENT", "1") != "0"


@core.mcp.tool()
@reporting._guard
def walk_toward(direction: str = "", objective: str = "", target: str = "", max_m: float = 30.0,
                who: str | None = None, run: bool = True, wait: bool = False) -> str:
    """Walk as far as the ground allows (up to max_m) toward a compass direction (N, NE, E, SE, S, SW, W, NW), a quest objective (id like o1 from get_status/get_map, or words from its name), or a visible target id. Picks the farthest point with a direct walkable path, then walks there like move_to (returns within ~1 s with a job id + ETA, or the result if it already ended; wait=true blocks only in combat). Good for "head west" or "go toward the objective marker" when no door or item is in view. In a fight a walk longer than the movement left is cut to it (the result says so)."""
    t0 = time.monotonic()
    args: dict[str, Any] = {"char": plumbing._who(who), "max_m": max_m}
    if objective:
        try:  # o2 means the o2 the agent last saw; the order changes as the party moves
            want = render.find_objective(overview._objectives["last"], objective)
        except ValueError:
            want = None
        fresh = overview._objective_markers()
        m = None
        if want is not None:
            m = next((x for x in fresh if isinstance(x, dict) and x.get("label") == want.get("label")
                      and x.get("quest") == want.get("quest")), None)
        if m is None:
            m = render.find_objective(fresh, objective)
        if not (m.get("here") and m.get("pos")):
            raise ValueError(f"objective '{render.objective_label(m)}' is not in this area")
        args["pos"] = m["pos"]
        what = f"objective {render.objective_label(m)}"
    elif target:
        args["target"] = plumbing._uuid(target)
        what = target
    elif direction:
        args["dir"] = direction.strip().upper()
        what = args["dir"]
    else:
        raise ValueError("give direction=N|NE|E|SE|S|SW|W|NW, objective=o1, or target=<id>")
    # loose: a winding walk counts when nothing direct does; alts: a dead direction names the ones that walk
    res = core.game.rpc("nav.reach_toward", {**args, "loose": True, "alts": "dir" in args})
    if not isinstance(res, dict) or not res.get("reachable") or not res.get("pos"):
        n = res.get("samples") if isinstance(res, dict) else None
        hz = macros.hazard_text(res.get("hazard")) if isinstance(res, dict) else ""
        if hz:
            return reporting._finish("walk_toward", args, f"FAILED: the way toward {what} crosses {hz}. Go around it"
                           " (another direction, get_frontier) or "
                           f"{macros.hazard_way_out(res.get('hazard'))}.", t0)
        body = (f"FAILED: no walkable ground toward {what} within {render.num(max_m)}m"
                + (f" ({n} spots checked)" if n is not None else ""))
        note = render.opened_note(res, core.H)
        if note:
            body += f"\n{note}"
        hint = _route_hint(args)  # toward an objective/target only (a direction has no goal)
        others = navhints.other_dirs_text(res.get("others") if isinstance(res, dict) else None)
        if not hint and not others:
            hint = _wedged_hint(args.get("char"), args.get("pos")).lstrip("; ")
        return reporting._finish("walk_toward", args, body + ("\n" + hint if hint else others + "." if others else
                                                    ". Try another direction, get_frontier, or a door."), t0)
    head = f"heading {res.get('dir') or ''} toward {what}: {render.num(res.get('straight_m', 0))}m"
    if res.get("path_m") is not None:
        head += f" (path {render.num(res['path_m'])}m{', winding' if res.get('winding') else ''})"
    if res.get("needs_jump"):
        head += ", needs a jump"
    if res.get("hazard_beyond"):
        head += f", stopping short of {macros.hazard_text(res['hazard_beyond'])}"
    if res.get("standing_in"):
        head += f" (you are standing in {res['standing_in']})"
    wait, note = plumbing._walk_wait(wait)
    body = plumbing._act("walk_toward", "act.move_to", {"char": args["char"], "pos": res["pos"], "run": run}, wait=wait,
                note=note)
    left = turn.movement_left(body) if WALK_CLIP_MOVEMENT and turn.move_too_long(body) else None
    over = turn_plan._path_over(body)
    if left is not None and left >= 1.0 and over is not None and res.get("straight_m") is not None:
        # ACT-P3 (naut2 run 1 19:59:37): walk_toward(max_m=9) in a fight → "FAILED — beyond movement (path 9.6 m,
        # 9.0 m left)", the same call again with max_m 8.5. Cut the walk to the movement left, as combat_turn does.
        # Along the real route to the spot, cut where its own walk fits the movement left (nav.approach checks the
        # cut spot's path); the straight-line samples below are the fallback for a mod without nav.approach (vis,
        # A6: a straight pick can still need more walking than is left).
        budget = float(res["straight_m"]) - over - 0.3
        r2 = core.game.try_rpc("nav.approach", {"char": args["char"], "pos": res["pos"], "within": 0.5,
                                            "budget_m": float(left) - 0.3})
        if isinstance(r2, dict) and ("pos" in r2 or "reachable" in r2):
            ok = r2.get("reachable") and r2.get("pos") and float(r2.get("walk_m") or 0) >= 1.0
            r2 = dict(r2, straight_m=r2.get("walk_m")) if ok else None
        else:
            r2 = core.game.try_rpc("nav.reach_toward", {**args, "max_m": budget, "loose": True}) if budget >= 1.0 else None
        if isinstance(r2, dict) and r2.get("reachable") and r2.get("pos"):
            body = plumbing._act("walk_toward", "act.move_to", {"char": args["char"], "pos": r2["pos"], "run": run},
                        wait=wait, note=note)
            head += (f"; cut to the movement left ({render.num(left)}m): "
                     f"{render.num(r2.get('straight_m', budget))}m")
    return head + "\n" + body


@core.mcp.tool()
@reporting._guard
def get_combat_state() -> str:
    """Turn order (visible combatants), whose turn it is, round, and the active character's remaining action economy."""
    t0 = time.monotonic()
    return reporting._finish("get_combat_state", {}, _combat_state_text(), t0)


def _story_timer() -> Any:
    """The story turn counter under the minimap (Client UIState.storyTimer), None when the client can't say."""
    return core.game.try_rpc("ui.story_timer", context="client")


def _combat_state_text() -> str:
    par = getattr(core.game, "parallel", None)  # the counter's client read in the same tick as state.combat
    res, timer = par(lambda: core.game.rpc("state.combat"), _story_timer) if par else (core.game.rpc("state.combat"),
                                                                                   _story_timer())
    plumbing._remember_names(res)
    if isinstance(res, dict):
        turn_plan._seen_sides(res)  # the side of a target planned from this read that is out of sight by its turn
    if isinstance(res, dict) and not res.get("active", True) and len(res) <= 1:
        _acts_shown.clear()
        _light_noted.clear()
        return "Not in combat."
    body = render.render_combat(res, core.H) if isinstance(res, dict) else render.compact(res, core.H)
    limit = render.turn_limit_line(timer)
    if limit:
        body += "\n" + limit
    # One read should be enough to plan the whole turn: add the acting party member's actions.
    st = core.game.try_rpc("state.status") or {}
    turn_plan._remember_party(st)  # the result's events text reads the party from it, not from a second read
    party = {c.get("uuid") for c in st.get("party") or []}
    hint = reporting._fight_hint(res, party)
    if hint:
        body += "\n" + hint
    if isinstance(res, dict):
        body += "".join("\n" + x for x in turn.disabled_lines(res, queueing._label))
    mine = turn.party_acting(res, party) if isinstance(res, dict) else []
    cur = st.get("active") if st.get("active") in mine else (mine[0] if mine else None)
    if len(mine) > 1:
        body += ("\nSHARED TURN: " + ", ".join(queueing._label(u) for u in mine)
                 + " can all act now, in any order (combat_turn who=..., end each one's turn)")
    if cur in party:
        acts = core.game.try_rpc("state.actions", {"uuid": cur})
        overview._learn_actions(cur, acts)
        if acts:
            mv = (acts.get("resources") or {}).get("Movement")  # [amount, max] (State.lua resourcesView)
            rl = turn.reach_line(res, cur, mv[0] if isinstance(mv, (list, tuple)) and mv else None, queueing._label)
            if rl:
                body += "\n" + rl
            body += "\nYOUR ACTIONS (" + core.H.get(cur) + ")\n" + render.render_actions(acts, core.H)
            _acts_shown[cur] = set(render.render_actions(acts, core.H).splitlines()[1:])
    body += _light_note(res)
    return body


_light_noted: set[str] = set()  # members told this combat that they fight with the lantern


def _light_note(cs: Any) -> str:
    """'\nLIGHT: Claude fights with the Moonlantern …' once per member per fight (S025-S027: Claude, a paladin,
    swung the Moonlantern with the Sword of Justice in a bag; dropping it mid-fight in the curse loses Moonshield
    for everyone near, so the fix is after the fight: equip_best hands the light to the caster)."""
    if not isinstance(cs, dict) or not cs.get("active"):
        _light_noted.clear()
        return ""
    out = []
    for o in cs.get("order") or []:
        u, light = o.get("uuid"), o.get("light_in_hand")
        if light and u and u not in _light_noted:
            _light_noted.add(u)
            out.append(f"LIGHT: {queueing._label(u)} fights with the {light} in the main hand (torch damage). Keep it held "
                       "in the Shadow Curse (its aura is the party's Moonshield); after the fight equip_best gives "
                       "it to a caster and puts a real weapon back in this hand.")
    return "".join("\n" + x for x in out)


# A names-only action line (render.ACTION_IDS False): the name ends where the first detail starts.
_ACT_NAME = re.compile(r"(.*?)(?= \[| [\d.]+m\b| self\b| point\b| L\d\b| uses | aoe | —|$)")

# Action lines last listed per character this combat: combat_turn's next-up listing repeated the same ~40 lines
# (~2 KB) every turn in S011 (Jump, Dip, Shove, all spells). get_combat_state always lists everything.
_acts_shown: dict[str, set[str]] = {}


def _actions_delta(uuid: str, acts: Any) -> str:
    """'\n<full list>' the first time this combat, then only the NOW line and what changed since."""
    text = render.render_actions(acts, core.H)
    lines = text.splitlines()
    head = lines[:1] if lines and lines[0].startswith("NOW ") else []
    body = lines[len(head):]
    prev = _acts_shown.get(uuid)
    _acts_shown[uuid] = set(body)
    if prev is None:
        return "\n" + text
    new = [x for x in body if x not in prev]

    def key(x: str) -> str:  # "Name (spell_id)" or "Name [cost]": a changed cost/uses line is a '+', not a '-'
        m = re.match(r"(.*?\(\S+\))", x) or re.match(_ACT_NAME, x)
        return m.group(1) if m else x
    renewed = {key(x) for x in new}
    unav = next((x for x in body if x.startswith("UNAVAILABLE")), "")

    def now_unavailable(x: str) -> bool:  # S026: 13 L3 spells were in the '+ UNAVAILABLE' line and listed again as '-'
        m = None if " (" in x else re.match(_ACT_NAME, x)
        name = m.group(1) if m else x.split(" (", 1)[0]
        return bool(unav) and re.search(r"(?<![\w'])" + re.escape(name) + r"(?=[,;( ]|$)", unav) is not None
    gone = sorted(x for x in prev if x not in body and key(x) not in renewed and not x.startswith("UNAVAILABLE")
                  and not now_unavailable(x))
    if len(gone) + len(new) > len(body) // 2:
        return "\n" + text
    out = head + [f"(other actions as listed before for {core.H.get(uuid)}; get_combat_state lists them all)"]
    out += ["+ " + x for x in new] + ["- " + key(x) for x in gone]
    return "\n" + "\n".join(out)


@core.mcp.tool()
@reporting._guard
def get_dialogue() -> str:
    """The current dialogue line and numbered answer options. A tagged option also shows its check and your speaker's bonus (plus the party's best), never the DC. Or the roll/reaction/popup on screen."""
    t0 = time.monotonic()
    ui = reporting._read1("ui.state", context="client")
    lines = reporting._render_ui(ui)
    return reporting._finish("get_dialogue", {}, "\n".join(lines) or f"No dialogue on screen (screen={ui.get('screen')}).", t0)


@core.mcp.tool()
@reporting._guard
def get_events() -> str:
    """Never blocks: what happened since your last tool call (combat log, quest updates, items, rolls, job_finished, queue_*), plus jobs and queues still running."""
    t0 = time.monotonic()
    body = queueing._running_text()
    return reporting._finish("get_events", {}, body, t0) or "No new events."


def _shots_dir() -> Path:
    """Where the game's capture layer writes (its output_dir, set at launch from the launcher's runtime). A bridge
    with its own BG3AI_RUNTIME (acceptance.py) waited in the wrong folder: "did not present a frame in time"."""
    return Path(os.environ.get("BG3AI_SHOTS_DIR") or core.RUNTIME / "shots")


@core.mcp.tool()
@reporting._guard
def screenshot(scale: float = 0.5, map: bool = False) -> Image | str:
    """Picture of the game screen exactly as the player sees it. Costly: use only when text tools can't answer (layout, puzzles, unknown popups). scale: 0.25-1.0 of 2560px width. map=true: opens the map, takes the picture and closes the map again, in one call (no M / ESCAPE by hand)."""
    from ..screen import ScreenCapture

    if core._screen is None:
        core._screen = ScreenCapture(_shots_dir())
    width = int(2560 * max(0.25, min(scale, 1.0)))
    opened = False
    if map:
        # S055 x2, S056 x3: M + screenshot + ESCAPE by hand (S056 07:17: a 2nd M closed it, the ESCAPE was refused)
        r = core.game.run_job("ui.map_show", {}, context="client", wait_s=12)
        if r.status != "done":
            return f"screenshot: FAILED — the map didn't open: {r.error or r.status}"
        opened = bool((r.result or {}).get("opened")) if isinstance(r.result, dict) else False
        time.sleep(MAP_SHOT_SETTLE_S)  # the map's first frames draw its tiles in
    try:
        jpg, full = core._screen.capture(max_width=width)
    finally:
        if opened:
            core.game.try_rpc("ui.map_close", context="client")
    core._last_full_frame = full
    core._log("screenshot", {"scale": scale, "map": map}, f"{full.size}", 0)
    return Image(data=jpg, format="jpeg")


MAP_SHOT_SETTLE_S = 0.8


@core.mcp.tool()
@reporting._guard
def zoom(x: float, y: float, w: float, h: float) -> Image | str:
    """Crop of the LAST screenshot at full resolution. x,y,w,h are fractions 0-1 of the screen (e.g. 0.3,0.7,0.4,0.3 = bottom-middle dialogue box)."""
    import io

    if core._last_full_frame is None:
        return "Take a screenshot first."
    W, Hh = core._last_full_frame.size
    box = (int(x * W), int(y * Hh), int((x + w) * W), int((y + h) * Hh))
    crop = core._last_full_frame.crop(box)
    if crop.width > 1600:
        crop = crop.resize((1600, round(crop.height * 1600 / crop.width)))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    world_tools, batch, queueing, turn_plan, macro_tools, travel_macro, session,
)
