"""Macros: travel_to, regroup, read_all, recall, rest_if_needed.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import actq, longnav, lore, macros, party, render
from .. import route as route_text
from ..game import GameError
from . import (
    batch,
    core,
    macro_tools,
    memory,
    observe,
    overview,
    plumbing,
    queueing,
    reporting,
    travel,
    turn_plan,
    world_tools,
)


@core.mcp.tool()
@reporting._guard
def travel_to(marker: str = "", objective: str = "", waypoint: str = "", target: str = "",
              x: float | None = None, y: float | None = None, z: float | None = None, who: str | None = None,
              wait: bool = False, avoid_hazards: str = "auto", fast: str = "auto", note: str = "",
              trespass: bool = False) -> str:
    """Get somewhere far in one call: your map marker (m1 or words of its label), a quest objective on this level (o1), a visible target, x,y,z, or an unlocked waypoint (fast travel). Walks the game's own route in one go when the whole way is plain walking (long trips in ≤55 m legs), else chains walks of up to 30 m along walkable ground toward it, around harmful surfaces you can see (fire, acid, ...); a leg that fails on the ground (no path, blocked) is retried another way (along the route, or shorter; twice per trip); stops on arrival (within 3 m), when no walkable ground leads further, after 2 legs without progress, or at the first interrupt (combat, conversation, prompt, new hostile, any damage). Stepping into a surface: it steps out (forward if it can) and goes on.
    avoid_hazards: auto (default) = when no clear way leads on, it takes the shortest crossing (says e.g. 'crossing 1.5m of Fire (~1-4 dmg + Burning)') unless HP is too low for the worst case; strict = never crosses; cross = crosses even at low HP (never lava/chasms, never >6 m).
    Returns within ~1 s with live progress (m left, ETA), or the outcome if the trip already ended: the trip runs as the first entry of the character's action queue, so queue_actions appends what to do on arrival while you observe and plan. Every later result shows it RUNNING, then queue_done or queue_interrupted (why). wait is ignored (the trip never blocks).
    fast: auto (default) = a trip of 120 m+ fast travels first to an unlocked waypoint of this level that is much nearer the goal (says so); never = always walk.
    A trip that stops twice at the same blocked spot says "tried 2x: blocked" and names another way (a waypoint, another side); route() shows the dead ends, unlocked waypoints and the closest spot you have been.
    note: memory log line(s) appended to your session log at once, as note() does: write notes with the walk, no extra turn.
    An off-limits area the game warned you about (a guard's trespass talk) is walked round; a goal inside it, or a way with no way round, is refused (trespass=true walks in anyway)."""
    t0 = time.monotonic()
    if refused := session._note_refused(note):
        return reporting._finish("travel_to", {}, refused, t0)
    memory._note_quiet(note)
    if waypoint:
        return travel.fast_travel(waypoint)
    policy = (avoid_hazards or "auto").strip().lower()
    if policy not in macros.AVOID_POLICIES:
        raise ValueError("avoid_hazards must be auto, strict or cross")
    w = macro_tools._no_combat("travel_to")
    char = plumbing._who(who)
    seen_txt = ""
    if target:  # a far target is walked in legs to where it stands (one move_to to it is capped at MOVE_CAP_M)
        here = next((r.get("pos") for r in w.get("party") or [] if r.get("uuid") == char), None)
        tp = world_tools._ent_pos(plumbing._uuid(target))
        seen = None if tp else world_tools._last_seen(plumbing._uuid(target))
        if seen:  # out of sight now: walk to where the party last saw them (S025/S026 "target is not visible")
            seen_txt = world_tools._seen_line(target, seen) + "\n"
            (x, y, z), target = seen[0], ""
        elif here and tp and macros.dist(here, tp) > macros.TRAVEL_LEG_M:
            x, y, z, target = tp[0], tp[1], tp[2], ""
    if not reporting._batch["active"]:  # wait=true too: the agent plans while the character walks
        return seen_txt + batch._travel_async(char, w, marker, objective, target, x, y, z, t0, policy, waited=wait,
                                        fast=(fast or "auto").strip().lower() != "never", trespass=trespass)
    outer = reporting._batch["exec"] if reporting._batch["active"] else None  # the queue running this trip: its cancel stops it
    walk = {"trespass": True} if trespass else {}  # its walks' move_to args
    if target:
        done: list = []
        step = actq.Step("move_to", {"target": target, **walk})
        lines, stop, left = macro_tools._macro_groups(char, [(f"walk to {target}", [step])], done=done)
        if not (stop or done):  # S083: a failed walk (90 m, past the move cap) ended as queue_done done=1
            reporting._batch["last"] = "failed"
        return macro_tools._macro_finish("travel_to", {"target": target}, lines, stop, left, t0)
    dest, what = macro_tools._travel_dest(marker, objective, x, y, z)
    me = next((r for r in w.get("party") or [] if r.get("uuid") == char), {})
    here = me.get("pos")
    hp = [me.get("hp"), me.get("max_hp")]
    start_d = macros.dist(here, dest)
    lines = [f"travel_to {what}: {render.num(round(start_d, 1))}m"]
    crew = _trip_crew(char, lines)  # the leader's trip takes the party along (None: a companion's own trip)
    # travel_to deals with surfaces and damage itself (steps out, crosses on purpose, its own watcher stops on
    # other damage): the queue running it must not stop the trip on them.
    outer_w = outer.watcher if outer is not None else None
    added: set = set()

    def quiet(kinds: set) -> None:
        if outer_w is not None:
            new = kinds - outer_w.ignore
            outer_w.ignore |= new
            added.update(new)
    if policy != "strict":
        quiet({"surface", "damage"})
    in_travel, reporting._batch["in_travel"] = reporting._batch.get("in_travel"), True  # its legs are plain moves (never re-routed)
    try:
        esc = macro_tools._escape_surface(char, dest) if me.get("surface") else None
        if esc:
            lines.append(esc)
            here = _pos_now(char) or here
        watcher = actq.Watcher(queueing._watch_conn())
        watcher.baseline()
        prog = macros.TravelProgress(here, dest)
        stop = None
        retries, failed_end = 0, None  # a failed leg gets an alternative leg (S002: one failed leg ended the trip)
        blocks = 0  # legs re-run after someone in the way moved (or was stepped aside)
        for leg in range(1, macros.TRAVEL_MAX_LEGS + 1):
            d = macros.dist(here, dest)
            if outer is not None and (outer.interrupt or outer.stop.is_set()):
                stop = (outer.interrupt or ("cancel", "cancelled"))[1]
                break
            if d <= macros.TRAVEL_ARRIVE_M:
                lines.append(f"arrived ({render.num(round(d, 1))}m from it)")
                break
            # direct: the engine's own route in one long move_to when the whole way is plain walking (S005: 30 m
            # legs of sampled points ended on beach rocks and slopes, "path blocked stopped_short")
            args = {"char": char, "pos": dest, "max_m": min(macros.TRAVEL_LEG_M, d), "direct": True}
            if policy != "strict":
                args["avoid"] = "least"
            if failed_end is not None:
                args.update(alt=True, exclude=failed_end)
            r = core.game.try_rpc("nav.reach_toward", args)
            if isinstance(r, dict) and not r.get("reachable") and r.get("hazard"):
                stop = (f"the way on crosses {macros.hazard_text(r['hazard'])} ({render.num(round(d, 1))}m left)"
                        + (": no way around or across it found" if policy != "strict" else "")
                        + ": go around it (walk_toward another direction, get_frontier) or "
                        + macros.hazard_way_out(r["hazard"]))
                break
            # A leg along the engine's route may head away from the goal first (S029: a 325 m route, 2.4x the
            # straight line, began SE of a NW goal): it gains route metres, not straight-line ones.
            along = bool(isinstance(r, dict) and r.get("direct") and r.get("route_left_m") is not None
                         and prog.route_gain(r["route_left_m"]))
            if not (isinstance(r, dict) and r.get("reachable") and r.get("pos")) or (
                    macros.dist(r["pos"], dest) > d - 1.0 and not along):
                stop = (f"no walkable ground leads further toward it ({render.num(round(d, 1))}m left"
                        + (", no other leg either" if failed_end is not None else "")
                        + "): try get_frontier, a door, or a jump")
                stop += observe._wedged_hint(char, dest)
                if isinstance(r, dict):
                    stop += macros.direct_why_text(r.get("direct_why"), d)
                if isinstance(r, dict) and r.get("hazard_beyond"):
                    stop += f"; ahead: {macros.hazard_text(r['hazard_beyond'])}"
                break
            # A plain sampled leg that gains only a few metres on a long trip (acceptance: 30 m, 2 m, 29 m legs; the
            # sampler stopped at an obstacle edge): one verified alternative leg first, kept when it gets clearly further.
            if (failed_end is None and not r.get("direct") and not r.get("exposure") and not r.get("detour")
                    and d > macros.TRAVEL_MIN_GAIN_M + macros.TRAVEL_ARRIVE_M
                    and d - macros.dist(r["pos"], dest) < macros.TRAVEL_MIN_GAIN_M):
                a2 = core.game.try_rpc("nav.reach_toward", {**args, "alt": True, "exclude": r["pos"]})
                if (isinstance(a2, dict) and a2.get("reachable") and a2.get("pos") and not a2.get("exposure")
                        and macros.dist(a2["pos"], dest) < macros.dist(r["pos"], dest) - 2.0):
                    r = a2
            ex = r.get("exposure")
            leg_w, note = watcher, ""
            if isinstance(ex, dict) and ex.get("surface"):
                ok, why = macros.crossing_ok(ex, hp[0], hp[1], policy)
                if not ok:
                    stop = (f"the way on crosses {macros.exposure_text(ex)} @{render.pos(ex.get('pos'))} "
                            f"({render.num(round(d, 1))}m left): {why}")
                    break
                note = f"crossing {macros.exposure_text(ex)} @{render.pos(ex.get('pos'))}, the shortest way on: "
                leg_w = actq.Watcher(queueing._watch_conn(), ignore={"surface", "damage"})
                leg_w.baseline()
                watcher.ignore.add("damage")  # Burning keeps ticking on the next legs
            elif r.get("detour"):
                hz = (r.get("straight") or {}).get("hazard")
                note = f"around {hz['surface']} (heading {r.get('dir') or ''}): " if isinstance(hz, dict) and hz.get("surface") else ""
            if r.get("direct"):
                note = f"along the route ({render.num(r.get('route_m') or r.get('path_m') or 0)}m): " + note
            if r.get("alt"):
                note = f"another way ({'along the route' if r['alt'] == 'route' else 'a shorter leg'}): " + note
            failed_end = None
            px, py, pz = r["pos"]
            rep, text = turn_plan._run_one(leg_w, char, actq.Step("move_to", {"x": px, "y": py, "z": pz, **walk}), outer)
            if r.get("hazard_beyond") and not ex:
                text += f" (kept clear of {macros.hazard_text(r['hazard_beyond'])})"
            lines.append(f"leg {leg}: {note}{text}")
            res = rep.last_result if isinstance(rep.last_result, dict) else {}
            aft = res.get("after") or {}
            if aft.get("hp") is not None:
                hp = [aft.get("hp"), aft.get("max_hp", hp[1])]
            if rep.stop_kind == "surface":
                esc = macro_tools._escape_surface(char, dest)
                if policy == "strict":
                    stop = queueing._label(rep.stopped or "") + (f"; {esc}" if esc else "")
                    break
                if esc:
                    lines.append(esc)
                here = _pos_now(char) or here
                why = prog.leg(here, True)
                if why:
                    stop = why + ": try get_frontier, a door, or a jump"
                    break
                continue
            if not rep.done:
                if (retries < macros.TRAVEL_LEG_RETRIES and macros.leg_retryable(rep.stop_kind, rep.stopped, text)
                        and not (outer is not None and (outer.interrupt or outer.stop.is_set()))):
                    retries += 1
                    failed_end = [px, py, pz]
                    here = _pos_now(char) or here
                    continue
                stop = queueing._label(rep.stopped or "") if rep.stop_kind not in (None, "failed") else f"leg {leg} failed"
                break
            here = (aft.get("pos") or res.get("pos") or r["pos"])
            if (macros.leg_stopped_short(res) and blocks < BLOCKER_TRIES and "blocked" in text
                    and macros.dist(here, r["pos"]) > macros.TRAVEL_ARRIVE_M):
                note, cleared = _clear_blocker(char, here, r["pos"], outer)
                if note:
                    lines.append(note)
                    blocks = blocks + 1 if cleared else BLOCKER_TRIES  # a blocker that stayed put stays put: once
                if cleared:
                    continue  # the same leg again, not a stall
            short = macros.leg_stopped_short(res)
            why = prog.leg(here, short, route_left=r.get("route_left_m") if r.get("direct") and not short else None)
            if why:
                stop = why + ": try get_frontier, a door, or a jump"
                break
            if crew:
                note, behind = _wait_for_stragglers(char, here, crew, outer)
                if note:
                    lines.append(note)
                if behind:
                    stop = behind
                    break
        else:
            stop = f"still {render.num(round(macros.dist(here, dest), 1))}m away after {macros.TRAVEL_MAX_LEGS} legs"
    finally:
        reporting._batch["in_travel"] = in_travel
        if outer_w is not None:
            outer_w.ignore -= added
    if objective and not stop and TRAVEL_OBJECTIVE_RECHECK and lines and lines[-1].startswith("arrived"):
        moved = _objective_moved_note(objective, dest)
        if moved:
            lines.append(moved)
    if longnav.ground_stop(stop, lines):
        # Every ground stop (no ground ahead, stalled legs, a blocked leg) names the way up/down: the stall stops
        # never did, so the ladder was named only by some stops (se 12:3x).
        stop += _climb_hint(_pos_now(char) or here, dest)
        stop += _dead_end_note(char, here, dest, what)
    return macro_tools._macro_finish("travel_to", {"to": what}, lines, stop, [], t0)


# naut2 run 1: travel_to o1 "arrived (0.7m from it)", then o1 Escape Route stood 66.8m W (the objective's marker
# moves on as you reach it); the agent found out 3 calls later. Re-read the markers (one nav.markers) on arrival.
TRAVEL_OBJECTIVE_RECHECK = True
OBJECTIVE_MOVED_M = 5.0


def _objective_moved_note(objective: str, dest: list) -> str:
    """'o1 Escape Route moved on: now 66.8m W: travel_to(objective="o1") again' when the objective walked to now
    stands elsewhere, a 'done or gone' line when it vanished, else ''."""
    try:
        m = render.find_objective(overview._objective_markers(), objective)
    except ValueError:
        return f"objective {objective} is gone now (done, or the quest moved on): get_map"
    except Exception:
        return ""
    if not (m.get("here") and m.get("pos") and m.get("dist") is not None):
        return ""
    if macros.dist(m["pos"], dest) < OBJECTIVE_MOVED_M or float(m["dist"]) < OBJECTIVE_MOVED_M:
        return ""
    ref = f"o{m['oid']}" if m.get("oid") else objective
    return (f"{ref} {render.objective_label(m)} moved on: now {render.num(m['dist'])}m {m.get('dir') or ''}".rstrip()
            + f': travel_to(objective="{ref}") again')


def _dead_end_note(char: str, here: Any, dest: list, what: str) -> str:
    """travel_to stopped on blocked ground: remember the spot; from the second stop there, say so and name
    another way (longnav.stop_note)."""
    at = _pos_now(char) or here
    e = batch.DEAD.record(dest, at, macros.dist(at, dest))
    if e is None or e.count < longnav.DEAD_REPEAT:
        return ""
    wps = (core.game.try_rpc("nav.waypoints", {"pos": dest}) or {}).get("waypoints")
    wp = longnav.pick_waypoint(at, dest, wps, min_m=0, save_m=longnav.ROUTE_WP_SAVE_M, ratio=1.0)
    return longnav.stop_note(e, wp, what.replace("objective ", ""))


def _trip_crew(char: str, lines: list) -> set | None:
    """Start of the leader's travel_to: chain drifted companions so they follow, and return the members to
    wait for when they fall behind: chained to the leader (or just re-chained), on this level, not staying,
    dead, downed or off stage. None when char is not the leader (a companion's own trip)."""
    st = core.game.try_rpc("state.status") or {}
    if st.get("active") != char:
        return None
    if overview._at_camp(core.game.try_rpc("ui.state", context="client")):
        # Logs pass 4 #5: at the Campsite companions idle at their tents and never follow: every camp walk (59 m)
        # stopped "e4 Karlach 37m … can't keep up" after a 15 s walk back. A camp walk is the leader's own.
        return set()
    rows = party.apart(st, stay=overview._stay)
    took = overview._rechain(party.rechain_candidates(rows, overview._member_busy), char)
    if took:
        lines.append("chained to follow: " + ", ".join(queueing._label(u) for u in took))
    other = {r["uuid"] for r in rows if r["other_level"]}
    return {r.get("uuid") for r in st.get("party") or []
            if r.get("uuid") != char and (r.get("chained") is True or r.get("uuid") in took)
            and r.get("uuid") not in overview._stay and r.get("uuid") not in other
            and not (r.get("dead") or r.get("off_stage") or r.get("downed") or r.get("summon"))}


BLOCKER_TRIES = 2      # travel_to: legs re-run per trip after someone in the way moved
BLOCKER_AHEAD_M = 4.0  # who counts as in the way: near the first metres of the leg ...
BLOCKER_NEAR_M = 1.6   # ... within this of that line (a doorway is ~1.5-2 m wide)
BLOCKER_WAIT_S = 3.0   # an NPC in the way gets this long to move on
BLOCKER_STEP_M = 3.0   # a companion in the way is stepped back this far behind the walker


def _in_the_way(here: list, toward: list, rows: list[dict], skip: str) -> list[dict]:
    """Characters standing on the first BLOCKER_AHEAD_M of the walk from here toward `toward` (flat distance to
    that line under BLOCKER_NEAR_M, same floor), nearest first."""
    dx, dz = toward[0] - here[0], toward[2] - here[2]
    n = (dx * dx + dz * dz) ** 0.5
    if n < 0.1:
        return []
    ux, uz = dx / n, dz / n
    out = []
    for r in rows:
        p = r.get("pos")
        if r.get("uuid") == skip or not (isinstance(p, (list, tuple)) and len(p) >= 3) or r.get("dead"):
            continue
        if abs(p[1] - here[1]) > 2.0:
            continue
        t = (p[0] - here[0]) * ux + (p[2] - here[2]) * uz
        if not 0.0 <= t <= BLOCKER_AHEAD_M:
            continue
        off = abs((p[0] - here[0]) * uz - (p[2] - here[2]) * ux)
        if off <= BLOCKER_NEAR_M:
            out.append(dict(r, _t=t))
    return sorted(out, key=lambda r: r["_t"])


def _clear_blocker(char: str, here: list, toward: list, outer: actq.Executor | None) -> tuple[str, bool]:
    """A leg stopped short "path blocked" right where someone stands (live 11:12, Last Light Inn: four legs
    stopped at an opened Oak Door, the party/NPCs in the doorway). A companion in the way is stepped back behind
    the walker; an NPC gets BLOCKER_WAIT_S to move on. Returns (line naming who blocked, whether to re-run the
    leg). ('', False): nobody in the way, the usual stall handling goes on."""
    watch = (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []
    near = (core.game.try_rpc("state.nearby", {"radius": BLOCKER_AHEAD_M + 2, "kinds": ["characters"]}) or {}).get("characters") or []
    party_ids = {r.get("uuid") for r in watch}
    rows = [dict(r, party=True) for r in watch] + [r for r in near if r.get("uuid") not in party_ids]
    who = _in_the_way(here, toward, rows, char)
    if not who:
        return "", False
    names = ", ".join(f"{queueing._label(r['uuid'])}" if r.get("party") else f"{core.H.get(r['uuid'], r.get('name'))} {r.get('name', '?')}"
                      for r in who)
    mates = [r for r in who if r.get("party") and not overview._member_busy(r["uuid"])]
    if mates:
        dx, dz = toward[0] - here[0], toward[2] - here[2]
        n = max((dx * dx + dz * dz) ** 0.5, 0.1)
        back = [here[0] - dx / n * BLOCKER_STEP_M, here[1], here[2] - dz / n * BLOCKER_STEP_M]
        for r in mates:
            core.game.run_job("act.move_to", {"char": r["uuid"], "pos": back}, wait_s=6)
        return f"in the way: {names}: stepped back behind {queueing._label(char)}, walking on", True
    if outer is not None and (outer.interrupt or outer.stop.is_set()):
        return "", False
    first = {r["uuid"]: r.get("pos") for r in who}
    end = time.monotonic() + BLOCKER_WAIT_S
    while time.monotonic() < end:
        time.sleep(0.3)
        near = (core.game.try_rpc("state.nearby", {"radius": BLOCKER_AHEAD_M + 2, "kinds": ["characters"]}) or {}).get("characters") or []
        if not _in_the_way(here, toward, [r for r in near if r.get("uuid") in first], char):
            return f"in the way: {names}: waited {render.num(round(BLOCKER_WAIT_S - (end - time.monotonic()), 1))}s for them to move, walking on", True
    return (f"in the way: {names} (stands in the path, didn't move in {render.num(BLOCKER_WAIT_S)}s): "
            "talk_to them, go around (walk_toward another direction) or wait"), False


def _wait_for_stragglers(char: str, here: Any, crew: set, outer: actq.Executor | None) -> tuple[str, str | None]:
    """After a leader's leg: a companion more than party.STRAGGLER_M behind (stuck on a ladder, a gap, a
    door) is waited for a moment, then walked back in legs. Returns (note for the leg lines, the stop
    reason if one still can't keep up)."""
    def lag() -> list[tuple[str, float]]:
        rows = (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []
        rows = [r for r in rows if r.get("uuid") in crew and not overview._member_busy(r.get("uuid"))]
        return party.stragglers(rows, here)

    def stopped() -> bool:
        return outer is not None and (outer.interrupt is not None or outer.stop.is_set())
    behind = lag()
    if not behind:
        return "", None
    first = dict(behind)
    # Followers trail a running leader by more after a long leg: wait while they still close in (the gap shrinks),
    # up to CATCH_UP_MAX_S; only a member who stopped gaining is walked back.
    t_start = time.monotonic()
    end = t_start + party.CATCH_UP_S
    gap = max(d for _, d in behind)
    while behind and time.monotonic() < end and not stopped():
        time.sleep(0.25)
        behind = lag()
        now_gap = max((d for _, d in behind), default=0.0)
        if now_gap < gap - party.CATCH_UP_GAIN_M:
            gap = now_gap
            end = min(t_start + party.CATCH_UP_MAX_S, max(end, time.monotonic() + party.CATCH_UP_S / 2))
    names = ", ".join(f"{queueing._label(u)} {render.num(round(d))}m" for u, d in first.items())
    if not behind:
        return f"waited for {names} behind: caught up", None
    if stopped():
        return "", None
    x, y, z = here
    for u, _d in behind:
        queueing._queue_ignore[u] = set()
        queueing._queues().add(u, [actq.Step("travel_to", {"x": x, "y": y, "z": z})], replace=True)
    end = time.monotonic() + 30.0
    while any(queueing._queues().busy(u) for u, _ in behind) and time.monotonic() < end and not stopped():
        time.sleep(0.2)
    rows = {r.get("uuid"): r.get("pos") for r in (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []}
    still = [(u, macros.dist(rows.get(u), here)) for u, _d in behind if rows.get(u)]
    still = [(u, d) for u, d in still if d > party.STRAGGLER_M]
    if not still:
        return f"{names} fell behind: walked back to {queueing._label(char)}", None
    left = ", ".join(f"{queueing._label(u)} {render.num(round(d))}m behind" for u, d in still)
    return (f"{names} fell behind; the walk back failed",
            f"{left} can't keep up (stuck on a ladder, gap or door?): route(who=...) for their way, regroup(), "
            "or stay(who) to leave them on purpose")


_ladder_ends, _rank_ways = route_text.ladder_ends, route_text.rank_ways
FLOOR_SAME_M = route_text.FLOOR_SAME_M


def _floor_ways(within: float = 1e9, here: list | None = None, dest: list | None = None,
                kinds: tuple = ("ladder", "stairs", "exit")) -> tuple[dict | None, list[dict], list[dict]]:
    """(frontier, the 2 best ladders/stairs/exits in view within `within` m, the ladders to climb in order): the ways
    to another floor or an interior, ranked toward dest (_rank_ways; nearest first without one). Leads only: the
    open-direction searches cost ~100 ms."""
    # the frontier's own radius is 30 m: ask for `within` (acceptance 01:07: the Mausoleum Entrance 34 m from the
    # ladder top was cut, so travel_to (-286,-31,-847.5) started a 934 m walk instead of naming the entrance)
    args: dict[str, Any] = {"ways": False}
    if within < 1e8:
        args["radius"] = within
        # ...and a way in the party saw minutes ago counts (live 01:4x: the entrance is out of sight from the
        # Knotted Roots top, behind the cliff edge, 34 m off)
        args["known"] = True
    res = core.game.try_rpc("nav.frontier", args)
    leads = res.get("leads") if isinstance(res, dict) else None
    # a ladder pulled up out of reach is no way up (S059 08:57 e683, Navigation.lua N.ladderRaised)
    ways = [l for l in leads or [] if l.get("uuid") and l.get("kind") in kinds
            and not (l.get("raised_m") and l.get("leads") == "up") and (l.get("dist") or 0) <= within]
    ranked, chain = _rank_ways(ways, here, dest)
    return res, ranked[:2], chain


def _climb_text(l: dict, here: list | None) -> str:
    """' (climbs 10.1m up, from a floor 2.6m above you)' for a ladder whose climb the frontier measured."""
    ends = _ladder_ends(l)
    if ends is None:
        return ""
    off = ""
    if here and abs(ends[0] - here[1]) > FLOOR_SAME_M:
        off = f", from a floor {render.num(round(abs(ends[0] - here[1]), 1))}m {'above' if ends[0] > here[1] else 'below'} you"
    return f" (climbs {render.num(l['height_m'])}m {l['leads']}{off})"


def _ways_text(res: dict | None, ups: list[dict], here: list | None = None) -> str:
    plumbing._remember_names(res)
    return ", ".join(f"{core.H.get(l['uuid'], l.get('name'))} {l.get('name', '?')} <{l.get('kind')}> "
                     f"{render.num(l.get('dist', '?'))}m{(' ' + l['dir']) if l.get('dir') else ''}"
                     + _climb_text(l, here)
                     + (" (seen earlier, out of sight now)" if l.get("remembered") else "") for l in ups)


def _climb_hint(here: list | None, dest: list | None) -> str:
    """travel_to stopped short of a destination on another deck: name the ladders/stairs/exits that lead there (the
    walker never climbs on its own; interact with the ladder does)."""
    if not (here and dest) or abs(dest[1] - here[1]) < 2.5:
        return ""
    res, ups, _chain = _floor_ways(here=here, dest=dest)
    side = "above" if dest[1] > here[1] else "below"
    hint = f"; it is {render.num(round(abs(dest[1] - here[1]), 1))}m {side} you"
    if not ups:
        return hint + ": find a ladder or stairs (get_frontier) or jump_to"
    return hint + f": climb with interact on a ladder/stairs/hatch ({_ways_text(res, ups, here)}), then travel_to again"


FLOOR_DY_M = 6.0      # travel_to: a goal this far above/below gets one route probe before the trip starts
FLOOR_WAY_M = 40.0    # ... and an entrance/ladder/stairs this near is named as the way there
FLOOR_FAR_DY_M = 20.0  # a goal too far to preview this far up/down + an entrance here = an interior (refused)
FLOOR_DETOUR_X = 3.0   # a route this many times the straight distance (and FLOOR_DETOUR_ADD_M longer) to a goal on
FLOOR_DETOUR_ADD_M = 40.0  # another floor, with a ladder/stairs in view: the trip walks, the start names the climb


def _other_floor(char: str, here: list | None, dest: list | None) -> str | None:
    """A goal on another floor or inside an interior that no walk reaches (the engine's route says so) while a way
    in/up/down is in view: the refusal naming it (None: walk as usual). Live 10:45: travel_to toward the
    Mausoleum puzzle room, 62 m below, walked 120 m the wrong way to stop at "no walkable ground", with the
    Mausoleum Entrance 17 m away. Costs one nav.route (+ a leads-only frontier) only for goals >= 6 m up/down."""
    if not (here and dest) or abs(dest[1] - here[1]) < FLOOR_DY_M:
        return None
    r = core.game.try_rpc("nav.route", {"char": char, "pos": list(dest), "ways": False})
    dy = abs(dest[1] - here[1])
    if not isinstance(r, dict):
        return None
    if r.get("reachable") is True:
        return _floor_detour_note(r, here, dest, dy)
    if r.get("reachable") is not False:
        # Too far for the engine to preview: an interior (the Grand Mausoleum's rooms sit ~950 m off and 69 m
        # below its gate in level coordinates; live 11:2x travel_to walked off toward them) when far below/above
        # and an entrance is right here. Else walk as before.
        if r.get("reachable") is True or not (r.get("estimate") or r.get("too_far")) or dy < FLOOR_FAR_DY_M:
            return None
    # an interior: only a named entrance/exit leads in
    res, ups, chain = _floor_ways(FLOOR_WAY_M, here, dest, ("ladder", "stairs", "exit")
                                  if r.get("reachable") is False else ("exit",))
    if not ups:
        return None
    side = "above" if dest[1] > here[1] else "below"
    first = core.H.get(ups[0]["uuid"], ups[0].get("name"))
    if len(chain) == 2:
        # naut2 run 2: the queued [interact e56, interact e57] is the helm's way up from the lower deck
        a, b = (core.H.get(l["uuid"], l.get("name")) for l in chain)
        how = f"interact {a}, then interact {b} (or queue_actions [interact {a}, interact {b}, travel_to ...])"
    else:
        how = f"interact {first} (or queue_actions [interact {first}, travel_to ...])"
    why = ("no walk on this floor reaches it (inside/under/over something)" if r.get("reachable") is False
           else f"{render.num(round(macros.dist(here, dest)))}m off and too far to preview: likely inside an interior")
    return (f"REFUSED: the goal is {render.num(round(dy))}m {side} you and {why}. The way there is likely: {_ways_text(res, ups, here)}. "
            f"{how}, then travel_to again")


def _floor_detour_note(r: dict, here: list, dest: list, dy: float) -> str | None:
    """A walkable route to a goal on another floor that winds far around (se 12:3x, Roadside Cliffs: locks 9-16 m
    below a ledge, the ladder 13 m N named only by some stops) while a ladder/stairs is in view: a note naming the
    climb (the walk still starts; never a refusal: the long way may be the only one the agent knows)."""
    length, straight = r.get("length_m"), macros.dist(here, dest)
    # The engine's route climbs/drops on its own (live 13:08: the 60 m route to a goal 10 m up went up the 11 m ladder
    # 2.6 m away, "used Ladder"): nothing to add.
    if any(isinstance(lk, dict) and lk.get("kind") in ("climb", "drop") for lk in r.get("links") or []):
        return None
    if not isinstance(length, (int, float)) or length < max(FLOOR_DETOUR_X * straight, straight + FLOOR_DETOUR_ADD_M):
        return None
    res, ups, _chain = _floor_ways(FLOOR_WAY_M, here, dest, ("ladder", "stairs"))
    if not ups:
        return None
    side = "above" if dest[1] > here[1] else "below"
    return (f"note: the goal is {render.num(round(dy))}m {side} you and the walk there winds {render.num(round(length))}m "
            f"for {render.num(round(straight))}m straight; shorter: {_ways_text(res, ups, here)} "
            f"(cancel, interact {core.H.get(ups[0]['uuid'], ups[0].get('name'))}, then travel_to again)")


def _pos_now(char: str) -> list | None:
    """A party member's position from act.watch (read once per tool result tail, see _running_memo)."""
    rows = queueing._running_memo.get("watch") if queueing._running_memo["on"] else None
    if rows is None:
        rows = (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []
        if queueing._running_memo["on"]:
            queueing._running_memo["watch"] = rows
    return next((r.get("pos") for r in rows if r.get("uuid") == char), None)


@core.mcp.tool()
@reporting._guard
def regroup(leader: str | None = None, near_m: float = macros.REGROUP_NEAR_M, chain: bool = True) -> str:
    """Bring party members who are farther than near_m from the leader (default: the active character) back to them, all walking at once, each in legs of verified walkable ground like travel_to (any distance; jump/climb links on the route are fine out of combat), then chain everyone to the leader (chain=false skips that). Downed members are left (help them up first); a member on another level is named (walk the leader there or fast travel). Members told to stay come back too. Each walk stops on the usual interrupts."""
    t0 = time.monotonic()
    w = macro_tools._no_combat("regroup")
    lead = plumbing._who(leader)
    st = core.game.try_rpc("state.status") or {}
    info = {r.get("uuid"): r for r in st.get("party") or []}
    rows = {r.get("uuid"): r for r in w.get("party") or []}
    lp = (rows.get(lead) or {}).get("pos")
    if not lp:
        raise GameError(f"no position for {queueing._label(lead)}")
    lreg = (info.get(lead) or {}).get("region")
    far, lines, skip = [], [], set()
    for u, r in rows.items():
        if u == lead or (info.get(u) or {}).get("summon"):
            skip.add(u)  # summons: not companions, they come and go with their spell
            continue
        overview._stay.discard(u)
        reg = (info.get(u) or {}).get("region")
        if reg and lreg and reg != lreg:
            lines.append(f"{queueing._label(u)}: on another level ({reg}): walk {queueing._label(lead)} there or fast_travel")
            skip.add(u)
            continue
        d = macros.dist(r.get("pos"), lp)
        if r.get("downed"):
            lines.append(f"{queueing._label(u)}: downed, {render.num(round(d, 1))}m away (help them up)")
            skip.add(u)
        elif d > near_m:
            far.append((u, d))
    unchained = [u for u in rows if u != lead and u not in skip and (info.get(u) or {}).get("chained") is False]
    if not far and not (chain and unchained):
        return reporting._finish("regroup", {}, "\n".join(lines + [f"everyone is within {render.num(near_m)}m of {queueing._label(lead)}"]), t0)
    x, y, z = lp
    # S035 19:28: Shadowheart's own move_to + jump_to queue (her way up) was replaced by regroup's travel_to, which
    # found no walkable way ("no walkable ground leads further"). A member already on its own queue keeps it.
    own = [(u, d) for u, d in far if queueing._queues().busy(u)]
    for u, d in own:
        lines.append(f"{queueing._label(u)} ({render.num(round(d, 1))}m): left on its own queue (it is still running); "
                     "regroup again once it ends")
    far = [(u, d) for u, d in far if (u, d) not in own]
    skip.update(u for u, _ in own)   # no chain mid-queue either (a chained follower walks after the leader)
    if not far and not (chain and unchained):
        return reporting._finish("regroup", {}, "\n".join(lines), t0)
    for u, d in far:
        queueing._queue_ignore[u] = set()
        queueing._queues().add(u, [actq.Step("travel_to", {"x": x, "y": y, "z": z})], replace=True)
    end = time.monotonic() + party.REGROUP_WAIT_S
    while any(queueing._queues().busy(u) for u, _ in far) and time.monotonic() < end:
        time.sleep(0.1)
    now = {r.get("uuid"): r.get("pos") for r in (queueing._watch_conn().try_rpc("act.watch") or {}).get("party") or []}
    lp2 = now.get(lead) or lp
    stop = None
    for u, d in far:
        q = queueing._queues().queues.get(u)
        rep = q.last_report if q else None
        d2 = macros.dist(now.get(u), lp2) if now.get(u) else None
        at = f" → {render.num(round(d2, 1))}m" if d2 is not None else ""
        last = (rep.lines[-1] if rep and rep.lines else "").splitlines()
        why = next((ln for ln in reversed(last) if ln.startswith("STOPPED")), last[-1] if last else "")
        if rep is None or queueing._queues().busy(u):
            lines.append(f"{queueing._label(u)} ({render.num(round(d, 1))}m{at}): still walking")
        elif d2 is not None and d2 <= max(near_m, macros.TRAVEL_ARRIVE_M + 1.0):
            lines.append(f"{queueing._label(u)} ({render.num(round(d, 1))}m{at}): back")
        else:
            lines.append(f"{queueing._label(u)} ({render.num(round(d, 1))}m{at}): {why or 'did not get back'}".rstrip())
            stop = stop or (f"{queueing._label(u)} could not get back to {queueing._label(lead)}"
                            + (f": {queueing._label(rep.stopped)}" if rep and rep.stopped else ""))
    if chain:
        for u in rows:
            if u != lead and u not in skip and ((info.get(u) or {}).get("chained") is not True or any(u == f for f, _ in far)):
                r = core.game.run_job("act.chain", {"char": u, "leader": lead}, wait_s=10)
                ok = r.status == "done" or "already chained" in str(r.error or "")
                lines.append(f"chain {queueing._label(u)} → {queueing._label(lead)}: " + ("done" if ok else f"{r.status} — {r.error}"))
    return macro_tools._macro_finish("regroup", {}, [f"regroup at {queueing._label(lead)}: {len(far)} walking"] + lines, stop, [], t0)


_READ_FILE = "read_items.json"


def _read_set() -> set:
    try:
        return set(json.loads((core.MEMORY / _READ_FILE).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


@core.mcp.tool()
@reporting._guard
def read_all(who: str | None = None, max_items: int = 6) -> str:
    """Read every book, note and letter in the party's bags you haven't read yet (by name), one after another, and return their text. Remembers what was read across sessions. Out of combat only. Stops at a conversation, roll, reaction or decision popup."""
    t0 = time.monotonic()
    macro_tools._no_combat("read_all")
    inv = core.game.rpc("state.inventory", {"uuid": plumbing._who_uuid(who)} if who else {})
    plumbing._remember_names(inv)
    seen = _read_set()
    items = [it for it in (inv.get("items") or []) if macros.readable(it) and it.get("uuid") not in seen]
    if not items:
        return reporting._finish("read_all", {}, "nothing new to read", t0)
    watcher = actq.Watcher(queueing._watch_conn(), ignore={"prompt"})
    watcher.baseline()
    lines, stop, budget = [], None, 6000
    for it in items[:max(1, min(int(max_items), 12))]:
        h = core.H.get(it["uuid"], it.get("name"))
        rep, text = turn_plan._run_one(watcher, it.get("owner"), actq.Step("manage_inventory", {"op": "use", "item": h}))
        if not rep.done:
            lines.append(f"{h} {it.get('name')}: {text}")
            if rep.stop_kind not in (None, "failed"):
                stop = queueing._label(rep.stopped or "")
                break
            continue
        page = None
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            ui = core.game.try_rpc("ui.state", context="client") or {}
            scr = ui.get("screen")
            if scr in ("dialogue", "roll", "reaction", "game_over") or (scr == "popup" and (ui.get("popup") or {}).get("decision")):
                lines.extend(reporting._render_ui(ui, brief=True))
                stop = f"{scr} on screen"
                break
            if scr in ("book", "popup", "reward") and ui.get("popup"):
                page = ui["popup"]
                break
            time.sleep(0.2)
        if stop:
            break
        seen.add(it["uuid"])
        txt = ""
        if page:
            txt = " — ".join(str(page[k]) for k in ("title", "text") if page.get(k))
            if page.get("text"):
                lore.remember(core.MEMORY, core.H.scrub(str(page.get("title") or it.get("name") or "")), core.H.scrub(str(page["text"])))
            core.game.try_rpc("ui.dismiss", {}, context="client")
            end = time.monotonic() + 1.5  # let the panel close, so the next book's text isn't this one's
            while time.monotonic() < end and (core.game.try_rpc("ui.state", context="client") or {}).get("screen") == "book":
                time.sleep(0.1)
        txt = core.H.scrub(txt)[:max(200, budget)] if txt else "(read; no text panel seen)"
        budget -= len(txt)
        lines.append(f"{h} {it.get('name')}: {txt}")
    try:
        core.MEMORY.mkdir(parents=True, exist_ok=True)
        (core.MEMORY / _READ_FILE).write_text(json.dumps(sorted(seen)))
    except OSError:
        pass
    left = [f"{core.H.get(it['uuid'])} {it.get('name')}" for it in items if it["uuid"] not in seen]
    head = f"read_all: {len(items)} unread"
    return macro_tools._macro_finish("read_all", {}, [head] + lines, stop, left if stop else [], t0)


@core.mcp.tool()
@reporting._guard
def recall(query: str = "", max_chars: int = 3000) -> str:
    """Search what the party has READ (books, notes, letters, inscriptions shown on screen, read_all) and its journal steps, by words: the puzzle hints a player remembers (e.g. "statue order", "door password"). Empty query: the titles read so far. Only texts the game showed you; read nearby books/notes first (interact) when nothing matches."""
    t0 = time.monotonic()
    journal = core.game.try_rpc("state.journal") if query.strip() else None
    return reporting._finish("recall", {"query": query}, lore.search(core.MEMORY, query, journal, max(500, min(int(max_chars), 8000))), t0)


@core.mcp.tool()
@reporting._guard
def rest_if_needed(threshold: float = 50.0, allow_long: bool = False) -> str:
    """Rest only when it's worth it: if any party member is downed or at/below threshold % HP, take a short rest; if no short rest is left and allow_long=true, go to camp and long rest (camp scenes then come up as dialogues). Out of combat only. HP only: check spell slots yourself."""
    t0 = time.monotonic()
    w = macro_tools._no_combat("rest_if_needed")
    low = macros.hp_low(w.get("party") or [], threshold)
    if not low:
        pcts = [round(100 * r["hp"] / r["max_hp"]) for r in w.get("party") or []
                if isinstance(r.get("hp"), (int, float)) and r.get("max_hp")]
        return reporting._finish("rest_if_needed", {}, f"no rest needed (lowest HP {min(pcts) if pcts else '?'}%)", t0)
    who_low = ", ".join(f"{queueing._label(u)} {hp}/{mx}" for u, hp, mx in low)
    lines, stop, left = macro_tools._macro_groups(None, [("rest", [actq.Step("short_rest", {})])])
    rested = bool(lines) and "short_rest: done" in lines[-1]
    if not rested and not stop and allow_long:
        more, stop, left = macro_tools._macro_groups(None, [("long rest", [actq.Step("camp", {"action": "go"}),
                                                               actq.Step("long_rest", {})])])
        lines += more
    elif not rested and not stop:
        lines.append("no short rest worked: long rest at camp (camp, long_rest) or heal with potions/spells")
    return macro_tools._macro_finish("rest_if_needed", {}, [f"low: {who_low}"] + lines, stop, left, t0)


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    saving, session,
)
