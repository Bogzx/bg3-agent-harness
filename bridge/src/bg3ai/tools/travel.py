"""Act tools that take the party somewhere: talk_to, jump_to, set_party, fast_travel, camp, swap_party.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any

from .. import actq, areablock, busywait, companions, longnav, macros, navhints, party, render
from ..game import GameError, JobResult, NotReady
from . import core, overview, plumbing, reporting, world_tools


@core.mcp.tool()
@reporting._guard
def talk_to(npc: str, who: str | None = None, wait: bool = True) -> str:
    """Walk up to a visible character (follows them if they move) and start a conversation. The result already shows the dialogue at its first real choice (opening filler clicked through); answer with choose_dialogue. Someone out of sight whom the party saw on this level, or past 40 m: walks there first and talks on arrival (queued)."""
    char = plumbing._who(who)
    out = plumbing._act("talk_to", "act.talk", {"char": char, "npc": plumbing._uuid(npc)}, wait_s=30, wait=wait)
    # S026: 7 refusals out of sight (Mattis, e511, e720); S028: 2 "too far away (47-50m, max 40m)" at camp.
    return _walk_first("talk_to", out, char, who, npc, actq.Step("talk_to", {"npc": npc})) or out


_TOO_FAR_RX = re.compile(r"is too far away \((\d+(?:\.\d+)?)m from this character, max \d+m\)")


# S067 #129: talk_to Gale (the CAMP roster: at camp, wants to talk) went to where the party last saw him 201 s
# before, the temple 3870 m away, and fast-travelled the whole party out of camp. A sighting that far is never
# a reason to travel by itself: the tool says the distance and the trip, and the agent decides.
STALE_TRIP_M = longnav.FT_MIN_M


def _at_camp_line(uuid: str) -> str:
    """A camp member out of sight: where the game says they are (the camp), not a last sighting."""
    here = overview._at_camp(core.game.try_rpc("ui.state", context="client"))
    return (f"{queueing._label(uuid)} is at camp now (the camp roster), not where the party last saw them: "
            + ("walk around the camp to find them (look / get_nearby), then talk_to again" if here else
               "camp() takes the party there; then find them in the camp (look / get_nearby)"))


def _camp_pos(uuid: str) -> list | None:
    """Where a camp companion stands while the party is in its camp (state.camp `pos`), or None."""
    row = next((c for c in (core.game.try_rpc("state.camp") or {}).get("camp") or []
                if isinstance(c, dict) and c.get("uuid") == uuid), None)
    p = (row or {}).get("pos")
    return list(p[:3]) if isinstance(p, (list, tuple)) and len(p) >= 3 else None


def _stale_trip_line(target: str, seen: tuple[list, int | None], here: list, d: float) -> str:
    """Refusal for a walk-first trip to a sighting past STALE_TRIP_M: the distance and the trip it would take."""
    ago = f" {render.num(seen[1])}s ago" if seen[1] is not None else ""
    wps = (core.game.try_rpc("nav.waypoints", {"pos": seen[0]}) or {}).get("waypoints")
    wp = longnav.pick_waypoint(here, seen[0], wps)
    trip = (f"a fast travel to {wp['name']} + a {render.num(round(wp['to_goal_m']))}m walk" if wp
            else f"a {render.num(round(d))}m walk")
    x, y, z = (render.num(round(v, 1)) for v in seen[0])
    return (f"NOT TRAVELLED: {target} is out of sight; the party last saw them{ago} {render.pos(seen[0])}, "
            f"{render.num(round(d))}m away ({trip}). They may have moved since. To go anyway: "
            f"travel_to x={x}, y={y}, z={z}, then talk_to {target}")


def _walk_first(tool: str, out: str, char: str, who: str | None, target: str, step) -> str | None:
    """A refusal that a walk fixes, as a player clicks it and the character walks there: the target is past the
    act range (S023 interact 75 m, S028 talk_to 47/50 m and interact 40 m at camp) or out of sight but seen on this
    level (S026 talk_to 7x). Starts travel_to there and queues `step` for the arrival; None when it doesn't apply
    (inside do_actions, in combat, another refusal, never seen)."""
    if reporting._batch["active"]:
        return None
    far = _TOO_FAR_RX.search(out)
    if not far and "target is not visible" not in out:
        return None
    if (queueing._watch_conn().try_rpc("act.watch") or {}).get("combat"):
        return None
    if far:
        dest: dict[str, Any] = {"target": target}
        line = f"{target} is {far.group(1)}m away: walking there first"
    else:
        u = plumbing._uuid(target)
        st = core.game.try_rpc("state.status") or {}
        if isinstance(st, dict) and any(isinstance(c, dict) and c.get("uuid") == u for c in st.get("camp") or []):
            # S080 #69: Karlach (camp roster) out of sight at camp: "walk around the camp to find them" and the agent
            # gave up. At camp, state.camp says where the companion stands (what swap_party walks to): go there.
            pos = _camp_pos(u) if overview._at_camp(core.game.try_rpc("ui.state", context="client")) else None
            if not pos:
                return out + "\n" + _at_camp_line(u)
            dest = {"x": pos[0], "y": pos[1], "z": pos[2]}
            line = f"{queueing._label(u)} is at camp (the camp roster) {render.pos(pos)}: walking there first"
        else:
            seen = world_tools._last_seen(u)
            if not seen:
                return None
            here = travel_macro._pos_now(char)
            d = macros.dist(here, seen[0]) if here else None
            if d is not None and d > STALE_TRIP_M:
                return out + "\n" + _stale_trip_line(target, seen, here, d)
            dest = {"x": seen[0][0], "y": seen[0][1], "z": seen[0][2]}
            line = world_tools._seen_line(target, seen)
    trip = travel_macro.travel_to(who=who, trespass=True, **dest)  # to the one they mean, as a click on them (their guard too)
    if "FAILED" in trip or "REFUSED" in trip:
        return out + "\n" + trip
    queued = batch._enqueue(char, [step], ignore=None)
    return f"{line}; then {tool} {target} on arrival ({queued})\n{trip}"


@core.mcp.tool()
@reporting._guard
def jump_to(x: float, y: float, z: float, who: str | None = None, wait: bool = True, force: bool = False) -> str:
    """Jump to a position (costs movement/bonus action in combat; distance limited by Strength). A landing in a harmful surface (fire, acid, ...) or on a wall, or a long jump that may fall short into one, is moved to the nearest clear spot near it within reliable reach (~4.5 m) and says so; with no clear spot near it the jump is refused. force=true jumps exactly there."""
    char = plumbing._who(who)
    pos, note = [x, y, z], ""
    if not force:
        try:
            lnd = core.game.rpc("nav.landing", {"char": char, "pos": pos})
        except NotReady:
            raise  # S038: the check hung the server; an unchecked jump on top of it helps nothing
        except GameError:
            lnd = None
        refusal = navhints.jump_refusal(lnd, pos)  # with a working plan (walk / walk-then-jump) when one exists
        if refusal:
            if reporting._batch["active"]:
                reporting._batch["last"] = "failed"
            return refusal
        if isinstance(lnd, dict) and lnd.get("safe") is False:
            bad = navhints.landing_bad_text(lnd)
            pos = list(lnd["suggest"])
            if lnd.get("floor_y") is not None:  # the same column's floor (a guessed y over the chasm around it)
                note = (f"landing {render.pos([x, y, z])} {bad}: the floor there is at y {render.num(lnd['floor_y'])}, "
                        f"jumping to {render.pos(pos)} ({render.num(lnd.get('suggest_dist_m', '?'))}m jump, clear)")
            else:
                note = (f"landing {render.pos([x, y, z])} {bad}: jumping to {render.pos(pos)} instead "
                        f"({render.num(lnd.get('suggest_off_m', '?'))}m from it, {render.num(lnd.get('suggest_dist_m', '?'))}m jump, clear)")
    body = plumbing._act("jump_to", "act.cast", {"char": char, "spell": "Projectile_Jump", "pos": pos}, wait=wait)
    return f"{note}\n{body}" if note else body



def _already_ok(out: str, refusal: str, done: str) -> str:
    """A set_party refusal that only says the party is already as asked → a done result (first line only)."""
    head, _, rest = out.partition("\n")
    if "FAILED" in head and refusal in head:
        return f"set_party: done — {done}" + (f"\n{rest}" if rest else "")
    return out


@core.mcp.tool()
@reporting._guard
def set_party(action: str, who: str | None = None, leader: str | None = None) -> str:
    """Party control. action: select (control 'who'), chain (link 'who' to 'leader'), unchain (detach 'who'), sneak_on, sneak_off, turn_based_on, turn_based_off."""
    a = action.lower()
    if a == "select":
        char = plumbing._who(who)
        return plumbing._act("set_party", "act.select", {"char": char}, post=lambda _r: "\n".join(turn_slots._disabled_lines(char)))
    if a == "chain":
        out = plumbing._act("set_party", "act.chain", {"char": plumbing._who(who), "leader": plumbing._who(leader) if leader else plumbing._active()})
        return _already_ok(out, "already chained together", f"{who} is already chained to the leader")
    if a == "unchain":
        out = plumbing._act("set_party", "act.unchain", {"char": plumbing._who(who)})
        # S062 10:00 ×3: unchaining a member who already walks alone is the wanted state, not a failure
        return _already_ok(out, "already unchained", f"{who} already walks alone (unchained)")
    if a in ("sneak_on", "sneak_off"):
        return plumbing._act("set_party", "act.sneak", {"char": plumbing._who(who), "on": a == "sneak_on"})
    if a in ("turn_based_on", "turn_based_off"):
        t0 = time.monotonic()
        on = a == "turn_based_on"
        if plumbing._tb_mode().get("combat"):
            return reporting._finish("set_party", {"action": a}, "set_party: FAILED — in combat (turn-based mode ends with the fight)", t0)
        ok, how = plumbing._set_turn_based(on)
        plumbing._tb_wanted["on"] = on and ok
        text = f"set_party: {'done' if ok else 'FAILED'} — turn-based mode {'on' if on else 'off'} ({how})"
        reporting._batch["last"] = "done" if ok else "failed"
        return reporting._finish("set_party", {"action": a}, text, t0)
    raise ValueError("unknown action")


def _wait_running(max_s: float = 60.0) -> bool:
    """After a teleport/load: the client back to Running and the server answering."""
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        try:
            if core.game.game_state() == "Running" and core.game.server_ready():
                return True
        except (GameError, NotReady, OSError):
            pass
        time.sleep(0.4)
    return False


def _settle_hud(max_s: float) -> bool:
    """After a rest/camp transition: wait until the HUD is up and nothing blocks travel on two polls in a row
    (live 11:4x: M pressed right after a camp leave was ignored for 3.6 s)."""
    deadline, good = time.monotonic() + max_s, 0
    while time.monotonic() < deadline:
        b = core.game.try_rpc("ui.travel_block", context="client")
        good = good + 1 if isinstance(b, dict) and b.get("hud") and not b.get("error") else 0
        if good >= 2:
            return True
        time.sleep(0.5)
    return False


def _end_night_first() -> str:
    """Live 2026-10-10: IsAtNightCamp=true + TravelError=Unavailable for hours: a long rest whose night never
    ended (29/31 fast_travel and 7 camp-leave failures 10:00-10:52). Travelling first needs the night to end, as a
    player would by finishing the rest: ui.long_rest(sleep_only) presses the camp's rest button at night, which
    (live 11:2x) opens the rest screen again; it rests from there, partially if supplies are short. '' when it
    isn't night at camp, else the line to show ('FAILED …').
    A busy UI (bg3le refusing the read) can't say whether it is night: it read as "not night", and a camp leave
    went on to act.leave_camp ("cannot travel right now", 13 times while the UI stayed busy 26 min). Say busy."""
    try:
        b = core.game.rpc("ui.travel_block", context="client")
    except GameError as e:
        if not busywait.is_busy(str(e)):
            return ""
        return "FAILED: can't read whether a long rest's night at camp still blocks travel: " + str(e)[:300]
    if not (isinstance(b, dict) and b.get("night_camp")):
        return ""
    # live-v09 t6: camp leave during Shadowheart's camp talk got ui.long_rest's refusal ("... then long_rest"); once
    # the talk was done, the same leave slept (a full long rest) and left. Say that, for whichever tool travels.
    if b.get("dialogue"):
        return ("FAILED: a conversation is open, nothing was pressed; at night in camp, leaving first ends the night "
                "(the party sleeps: a long rest)")
    out = world_tools._box_act("long_rest", "ui.long_rest", {"sleep_only": True}, wait_s=60)
    res = reporting._batch.get("result")
    if isinstance(res, dict) and res.get("night_ended"):
        _wait_running(30)
        # Live 11:2x-3x: the party sleeps (TravelError 'Condition'), then the morning moves it out of camp with the
        # HUD hidden; M pressed meanwhile never opened the map. Wait for the HUD and no travel error.
        _settle_hud(NIGHT_SLEEP_WAIT_S)
        how = res.get("rest") or "slept"
        used = res.get("supplies_used")
        return ("ended the night at camp first (a long rest's night was still running, which blocks travel): rested ("
                + str(how) + (f", {used} supplies" if used is not None else "") + ")")
    first = out.splitlines()[0] if out else "?"
    return ("FAILED: the night at camp hasn't ended (a long rest's night: travel is blocked until you sleep) — "
            + first[:300])


def _wake_first() -> str:
    """Acceptance 12:16: long_rest returned 3.6 s in with the party still Sleeping (a camp scene, TravelError
    'Condition', no night_camp flag) and the next fast_travel failed in 0.1 s 'can't fast travel now (Condition)'.
    A player waits for the morning: wait for the HUD and no travel error. '' when nothing to wait for."""
    b = core.game.try_rpc("ui.travel_block", context="client")
    if not (isinstance(b, dict) and b.get("error") == "Condition" and not b.get("dialogue")):
        return ""
    t = time.monotonic()
    ok = _settle_hud(NIGHT_SLEEP_WAIT_S)
    return f"waited {time.monotonic() - t:.0f}s for the party to wake up after the rest" + ("" if ok else " (still blocked)")


def _leave_camp_first() -> str | None:
    """Live 2026-10-10: a fast travel started at camp moved only the controlled character (companions and the
    camera stayed at the Campsite, PARTY APART 1800 m). ui.fast_travel refuses at camp, so leave it first, as the
    camp tool does (everyone comes along). Returns the line to show, '' when not at camp, or 'FAILED …'."""
    night = _end_night_first()
    if night.startswith("FAILED"):
        return night
    night = "\n".join(x for x in (night, _wake_first()) if x)
    try:
        core.game.rpc("ui.fast_travel", {"waypoint": "-", "dry": True}, context="client")
        return night
    except GameError as e:
        if "at camp" not in str(e):
            # live-v09c 1c: the mod reads TravelError before IsInCamp, so under the dream's narrator screen at camp
            # the dry run said only "(Condition)" and the trip failed "can't fast travel now (Condition)".
            b = _travel_block()
            if b.get("in_camp") and b.get("dialogue"):
                return "\n".join(x for x in (CONVO_SCREEN_REFUSED, night) if x)
            return night  # any other refusal: the real call reports it
    pressed = _travel_block(TRAVEL_BLOCK_WAIT_S)
    if pressed.get("dialogue"):
        return "\n".join(x for x in (CONVO_SCREEN_REFUSED, night) if x)
    r = core.game.run_job("act.leave_camp", {}, wait_s=60)
    if r.status != "done":
        text = plumbing._job_text("camp", r)
        if "cannot travel right now" in text:
            return "FAILED to leave camp first — " + text + _leave_unanswered_text(pressed, _travel_block())
        return "FAILED to leave camp first — " + text + _travel_block_text(_travel_block())
    if not _wait_running(60):
        return "FAILED: left camp, but the game didn't come back to Running within 60 s"
    _settle_hud(15)
    return "\n".join(x for x in (night, "left camp first (a fast travel from camp leaves your companions behind)") if x)


@core.mcp.tool()
@reporting._guard
def fast_travel(waypoint: str) -> str:
    """Travel to an unlocked waypoint (name or id from get_map). Not possible in combat. From camp it leaves camp
    first (everyone comes along), then travels. Only the selected member's group travels: members apart and unchained
    stay where they are (the result says LEFT BEHIND and the one call that travels each of them)."""
    left = _leave_camp_first()
    if left.startswith("FAILED"):
        reporting._batch["last"] = "failed"
        return reporting._finish("fast_travel", {"waypoint": waypoint}, "fast_travel: " + left, time.monotonic())
    st = core.game.try_rpc("state.status") or {}
    # S035 19:27:44, 19:39:13: "can't fast travel now (Location)" twice inside the Gauntlet of Shar, no reason or
    # way out. The greyed map button says it all along: an area block answers at once, like camp().
    if not left:
        tb = _travel_block()
        if tb.get("error") == "Location":
            reporting._batch["last"] = "failed"
            return reporting._finish("fast_travel", {"waypoint": waypoint}, "fast_travel: FAILED — can't fast travel now "
                           "(Location): this area does not allow travel (a dungeon or story area; camp() is blocked "
                           "too)." + _area_refusal("fast_travel", st), time.monotonic())
        if tb and not tb.get("error"):
            AREA.cleared(st.get("level"), _lead_pos(st))
    split = _fast_travel_split(st, waypoint)
    if split:
        if split.startswith("fast_travel: FAILED"):
            reporting._batch["last"] = "failed"
        return reporting._finish("fast_travel", {"waypoint": waypoint}, (left + "\n" if left else "") + split, time.monotonic())
    sub = overview.minimap_sub()
    watch = _ReadyCheckWatch()
    world_tools._TRANSITION_LAST.clear()
    with watch:
        out = world_tools._box_act("fast_travel", "ui.fast_travel", {"waypoint": waypoint}, wait_s=70, on_start=watch.on_start)
    if watch.failed is not None:
        if uitools._ui_screen()[0] == "map":
            core.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
        reporting._batch["last"] = "failed"
        return reporting._finish("fast_travel", {"waypoint": waypoint}, (left + "\n" if left else "") + (
            f"fast_travel: FAILED — the game's party ready check for the trip failed ({inventory._ready_label(watch.failed)}); stopped "
            f"waiting at once (S032: 66 s each). A broken ready check comes with a long-lived game process (the "
            f"runner relaunches the game between sessions); until then walk (travel_to) or try a waypoint in this "
            f"region"), time.monotonic())
    if out.rstrip().endswith("travelling"):
        # The box needed a mouse click (region change): the mod's job had stopped there, so nothing watched the
        # trip. Live 11:4x: "travelling" came back mid-load and the next reads saw the loading screen.
        if world_tools._TRANSITION_LAST.get("tool") == "fast_travel" and world_tools._TRANSITION_LAST.get("transition") in (
                "the game loaded", "the party moved"):
            out += "; arrived" if _settle_hud(20) else "; the HUD isn't back yet (wait_for idle)"
        elif saving._load_started(10.0):
            arrived = _wait_running(120) and _settle_hud(20)
            out += "; arrived" if arrived else "; still not back in the game 2 min later (wait_for idle)"
    if "fast_travel: done" in out or out.rstrip().endswith("arrived"):
        _trip_arrived()
        res = reporting._batch.get("result")
        overview.trip_arrived(sub, f"the trip to {(res.get('arrived') if isinstance(res, dict) else None) or waypoint}")
        out += _left_behind_after_trip(waypoint)
    return (left + "\n" + out) if left else out


def _trip_arrived() -> None:
    """Tell the walks a fast travel just arrived (Actions.lua act.trip_arrived): a jump in the next seconds is the
    game's (S073 22:33:39: the last 29.8 m of a walk from the waypoint, 9 s after the arrival, read `FAILED — engine
    teleported the character (not allowed)` and stopped the queue)."""
    core.game.try_rpc("act.trip_arrived")


AREA = areablock.AreaBlock()  # story-area travel blocks (Location), remembered per level (S065)


def _lead_pos(st: Any) -> list | None:
    lead = party.leader_row(st) if isinstance(st, dict) else None
    return (lead or {}).get("pos")


def _area_refusal(tool: str, st: Any = None) -> str:
    """A Location refusal: remember it for the level and say the way out (and, from the second tool on, that the
    others are refused too). S065 12:00:01-12:00:55: camp, fast_travel and travel_to(waypoint) refused one at a time,
    and camp's refusal suggested fast_travel."""
    st = st if isinstance(st, dict) and st else core.game.try_rpc("state.status") or {}
    level, pos = st.get("level"), _lead_pos(st)
    wps = None
    if AREA.current(level) is None and not AREA.ok.get(str(level)):
        wps = (core.game.try_rpc("nav.waypoints") or {}).get("waypoints")
    b = AREA.blocked(level, pos, "Location", tool, wps)
    return AREA.note(b, tool) + " Way out: " + AREA.way_out(level, pos) + ", then " + tool + " again."


def _wp_norm(s: Any) -> str:
    """The map's waypoint name as a player reads it (ui.fast_travel's wpNorm): case, a 'Waypoint:' prefix, punctuation."""
    s = re.sub(r"^\s*waypoint\s*[:\-]?\s*", "", str(s or "").lower())
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def _waypoint_here(waypoint: str, wps: Any) -> dict | None:
    """The nav.waypoints row of `waypoint` when the active member already stands at it (party.WAYPOINT_HERE_M).
    S064 11:35:08: Claude had just unlocked the Temple of Bhaal circle (4.5 m away) and fast travelled to it: the game
    moved him 2.9 m, under ui.fast_travel's 3 m 'moved' mark, and the call timed out after 66 s."""
    want = _wp_norm(waypoint)
    rows = [w for w in wps or [] if isinstance(w, dict) and want and want in (_wp_norm(w.get("name")), _wp_norm(w.get("id")))]
    if not rows:
        rows = [w for w in wps or [] if isinstance(w, dict) and want and want in _wp_norm(w.get("name"))]
    if len(rows) != 1 or not isinstance(rows[0].get("dist"), (int, float)):
        return None
    return rows[0] if rows[0]["dist"] <= party.WAYPOINT_HERE_M else None


def _travel_each(rows: list[dict], waypoint: str) -> str:
    """The do_actions line that fast travels each left-behind member: what a player does (select, click the waypoint)."""
    steps = []
    for r in rows:
        steps += [{"tool": "set_party", "action": "select", "who": overview._name_of(r["uuid"])},
                  {"tool": "travel_to", "waypoint": waypoint}]
    return "do_actions " + json.dumps(steps, ensure_ascii=False)


def _fast_travel_split(st: dict, waypoint: str) -> str:
    """Before pressing anything: '' to travel, else the answer. Unchained near members are chained first (they would
    stay behind). A trip to the waypoint the active member already stands at moves nobody (S064: a 66 s timeout):
    answered at once, and when others are apart that is the S064 case (bring them here), so it names who is where and
    the one call that travels each of them. Any other trip goes; _left_behind_after_trip says who stayed."""
    lead = party.leader_row(st)
    if not lead or lead.get("summon"):
        return ""
    rows = party.apart(st, stay=overview._stay)
    near = [r["uuid"] for r in rows if r["unchained"] and not r["stay"] and not r["other_level"] and not r["downed"]
            and not r.get("dy") and r["dist"] is not None and r["dist"] <= party.APART_M]
    took = set(overview._rechain(near, lead["uuid"])) if near else set()
    here = _waypoint_here(waypoint, (core.game.try_rpc("nav.waypoints") or {}).get("waypoints"))
    if not here:
        return ""
    me = queueing._label(lead["uuid"])
    at = f"{me} already stands at the {here['name']} waypoint ({render.num(here['dist'])}m)"
    behind = [r for r in party.travel_behind(rows) if r["uuid"] not in took]
    if not behind:
        return f"fast_travel: nothing to travel — {at}."
    return (f"fast_travel: FAILED — not travelled: {at}, and the game's fast travel takes only the selected member's "
            f"group, so it would move nobody. Apart from {me}: {party.behind_text(behind, queueing._label)}. To bring them "
            f"here, travel each one: {_travel_each(behind, waypoint)} (S064: ~3 s each), or regroup() if they can "
            "walk here.")


def _left_behind_after_trip(waypoint: str) -> str:
    """After a fast travel: members still apart from the active member (the trip took only its group), with the fix."""
    st = core.game.try_rpc("state.status") or {}
    AREA.cleared(st.get("level"), _lead_pos(st))  # travel worked: a block remembered on this level is gone
    rows = [r for r in party.apart(st, stay=overview._stay) if not r["stay"]
            and (r["other_level"] or (r["dist"] is not None and r["dist"] > party.APART_M * 3))]
    if not rows:
        return ""
    return (f"\nLEFT BEHIND by the trip: {party.behind_text(rows, queueing._label)}: travel each one "
            f"({_travel_each(rows, waypoint)}) or regroup()")


class _ReadyCheckWatch:
    """While a fast travel runs, watch the server's rest_flow events (Events.lua REST_FLOW) for the party ready
    check failing and cancel the client job then. S032 17:23:43 / 17:24:57: `ready_failed:WaypointTravel_RegionSwap`
    came at once, but ui.fast_travel (a client job: it can't see Osiris) waited out its 66 s each time."""

    POLL_S = 0.5

    def __init__(self) -> None:
        self.job: tuple[str, int] | None = None
        self.failed: str | None = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def on_start(self, context: str, job_id: int) -> None:
        self.job = (context, job_id)

    def __enter__(self) -> "_ReadyCheckWatch":
        getattr(core.game, "sync_events", lambda: None)()   # not the ring's older events
        self._seq = int((getattr(core.game, "event_seq", None) or {}).get("server", 0) or 0)
        self._t = threading.Thread(target=self._run, daemon=True, name="ready-check-watch")
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(2.0)

    def check(self, res: Any) -> bool:
        """One events.since answer; True when the ready check failed (sets self.failed)."""
        for e in (res or {}).get("events") or [] if isinstance(res, dict) else []:
            self._seq = max(self._seq, int(e.get("seq") or 0))
            step = str((e.get("data") or {}).get("step") or "") if isinstance(e.get("data"), dict) else ""
            if e.get("kind") == "rest_flow" and step.startswith("ready_failed"):
                self.failed = step.split(":", 1)[1] if ":" in step else "ready check"
                return True
        return False

    def _run(self) -> None:
        try:
            wg = queueing._watch_conn()
        except Exception:
            return
        while not self._stop.wait(self.POLL_S):
            try:
                res = wg.rpc("events.since", {"seq": self._seq, "limit": 200}, context="server", timeout=3.0)
            except Exception:
                continue
            if self.check(res):
                if self.job is not None:
                    try:
                        wg.rpc("job.cancel", {"id": self.job[1]}, context=self.job[0], timeout=3.0)
                    except Exception:
                        pass
                return


NIGHT_SLEEP_WAIT_S = 30.0  # after a night's rest: TravelError 'Condition' while the party has Sleeping
TRAVEL_BLOCK_WAIT_S = 8.0  # right after a load the game blocks travel ("Unavailable") for a few seconds


def _travel_block(wait_s: float = 0.0) -> dict:
    """ui.travel_block, polled up to wait_s while the game says travel is Unavailable (live 2026-10-10: transient
    after a load). {} when the client can't say."""
    deadline = time.monotonic() + wait_s
    while True:
        b = core.game.try_rpc("ui.travel_block", context="client") or {}
        if not b.get("error") or b.get("dialogue") or time.monotonic() >= deadline:
            return b
        time.sleep(0.4)


def _travel_block_text(b: dict) -> str:
    """Why the game won't travel, from what a player sees (greyed map/camp button, an open conversation)."""
    if not b:
        return ""
    if b.get("dialogue"):
        return " A conversation is open: finish it first (choose_dialogue)."
    if b.get("night_camp"):
        return " It is night at camp: a long rest's night hasn't ended (long_rest goes to sleep and ends it)."
    if b.get("error"):
        return (f" The game blocks travel here ({b['error']}): the camp and map travel buttons are greyed out."
                f" On screen: {b.get('screen') or '?'}. Talk to whoever waits nearby, finish the camp scene, or walk"
                f" (travel_to) and try again.")
    return ""


def _block_facts(b: dict) -> str:
    """What ui.travel_block read, as facts."""
    if not b:
        return "the game's travel state could not be read"
    facts = []
    if b.get("dialogue"):
        facts.append("a conversation screen was up")
    if b.get("night_camp"):
        facts.append("it was night at camp")
    if b.get("error"):
        facts.append(f"the camp and map travel buttons were greyed out ({b['error']})")
    if b.get("hud") is False:
        facts.append("the HUD was hidden")
    return ", ".join(facts) or "no travel button was greyed out and no conversation screen was up"


# live-v09b t2-camp2: camp leave right after the 1st long rest's dream, with the rest's narrator screen still up
# (get_status: screen=dialogue, the party Sleeping). act.leave_camp's conversation check reads the server, which had
# none: it pressed, the game didn't answer, and the job failed after its 12 s wait with no reason. The client's
# screen is checked before the press too (ui.travel_block dialogue).
CONVO_SCREEN_REFUSED = ("FAILED: a conversation is open, nothing was pressed; the game doesn't answer the camp "
                        "button while a conversation screen is up")


def _leave_unanswered_text(pressed: dict, now: dict) -> str:
    """act.leave_camp's 'cannot travel right now' is its own wait running out: no check refused, the game didn't
    answer the camp button. A reading taken after the wait can be clear (the block was over by then), so say what
    the game showed at the press too."""
    return (f" The game didn't answer the camp button: the party stayed at camp. When it was pressed: "
            f"{_block_facts(pressed)}. After the wait: {_block_facts(now)}.")


@core.mcp.tool()
@reporting._guard
def camp(action: str = "go") -> str:
    """action: go (travel to camp, like the camp button) | leave (return to where you were)."""
    if action == "leave":
        night = _end_night_first()
        if night.startswith("FAILED"):
            return reporting._finish("camp", {"action": action}, "camp: " + night, time.monotonic())
        night = "\n".join(x for x in (night, _wake_first()) if x)
        pressed = _travel_block(TRAVEL_BLOCK_WAIT_S)
        if pressed.get("dialogue"):
            reporting._batch["last"] = "failed"
            return reporting._finish("camp", {"action": action}, "\n".join(
                x for x in ("camp: " + CONVO_SCREEN_REFUSED, night) if x), time.monotonic())
        sub = overview.minimap_sub()
        out = plumbing._act("camp", "act.leave_camp", {}, wait_s=60)
        if reporting._batch.get("last") == "done":
            overview.trip_arrived(sub, "leaving camp")
            _settle_hud(15)  # the leave's transition: the next tool (fast_travel's M) works only after it
        out = (night + "\n" + out) if night else out
        if "cannot travel right now" in out:  # on that line: the EVENTS block follows it
            lines = out.split("\n")
            i = next(n for n, x in enumerate(lines) if "cannot travel right now" in x)
            lines[i] += _leave_unanswered_text(pressed, _travel_block())
            out = "\n".join(lines)
        return out
    # S035 19:27:30, 19:38:58: camp() pressed the button inside the Gauntlet of Shar and the job failed only after
    # its 12 s wait, with the reason (Location) the greyed button showed all along. A player sees the grey button:
    # an area block reads first and answers at once (transient errors still get the press).
    b = _travel_block()
    st = core.game.try_rpc("state.status") or {}
    if b.get("error") == "Location":
        reporting._batch["last"] = "failed"
        return reporting._finish("camp", {"action": action}, "camp: FAILED — cannot travel right now —" + _camp_block_text(b)
                       + _area_refusal("camp", st), time.monotonic())
    sub = overview.minimap_sub()
    out = plumbing._act("camp", "act.go_to_camp", {}, wait_s=60)
    if isinstance(out, str) and "cannot travel right now" in out:
        out += _camp_block_text(_travel_block())
    elif reporting._batch.get("last") == "done":
        overview.trip_arrived(sub, "the trip to camp")
        AREA.cleared(st.get("level"), _lead_pos(st))
        out += _camp_left_behind()
    return out


CAMP_GATHER_S = 4.0  # camp(): how long the other members get to arrive after the actor (they teleport one by one)


def _camp_left_behind() -> str:
    """After camp() arrived: members the camp travel didn't take, with the fix. S065 12:01:47: 'camp: done — at_camp'
    without Wyll (3863 m away), long_rest failed 4 s later, regroup STOPPED; set_party select Wyll + camp brought him."""
    deadline = time.monotonic() + CAMP_GATHER_S
    while True:
        rows = party.camp_behind(core.game.try_rpc("state.status") or {})
        if not rows or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if not rows:
        return ""
    names = [overview._name_of(r["uuid"]) for r in rows]
    fix = "; ".join(f"set_party select {n}, then camp()" for n in names)
    return (f"\nLEFT BEHIND: {party.behind_text(rows, queueing._label)} didn't come to camp (standing where camp travel is "
            f"blocked, or too far). long_rest fails until everyone is here: {fix} (S065 12:02: that brought Wyll); if "
            "camp() is refused for them, walk them out of the story area first.")


_CAMP_BLOCKS = {  # the HUD's TravelError (camp button greyed) → why, and the way out
    "Combat": "a fight is on: finish it first (combat_turn), then camp()",
    "Dialog": "a conversation is open: finish it first (choose_dialogue)",
    "Condition": "a party member's condition blocks it (asleep after a rest, downed, ...): wait a few seconds, "
                 "heal/raise the downed member, then camp() again",
    "DangerZone": "this is a danger zone (enemies or a hostile area around): walk away from it, then camp() again",
    "InDanger": "the party is in danger (hostiles close or aware of you): walk away, then camp() again",
}


def _camp_block_text(b: dict) -> str:
    """camp() go failed: the game's own reason (what the greyed camp button says) and the way out. S030 16:46:
    'cannot travel right now' inside the Gauntlet of Shar with no reason, and the agent gave up on resting."""
    if b.get("dialogue"):
        return " " + _CAMP_BLOCKS["Dialog"] + "."
    err = b.get("error")
    if err in _CAMP_BLOCKS:
        return f" The game blocks camp travel ({err}): {_CAMP_BLOCKS[err]}."
    if err == "Location":
        # S065 12:00:01: the old text suggested fast_travel, refused (Location) too 37 s later: the HUD greys the
        # camp and map-travel buttons together.
        return (" The game blocks camp travel here (Location): this area does not let the party go to camp (a "
                "dungeon or story area) and fast travel is blocked too. Here short_rest, potions and healing spells "
                "are what's left.")
    if b.get("night_camp"):
        return " It is night at camp: a long rest's night hasn't ended (long_rest goes to sleep and ends it)."
    head = f" The game blocks camp travel here ({err})" if err else \
        " The game gave no reason (the camp button isn't greyed)"
    return (head + ": this area does not let the party go to camp (a dungeon or story area). Ways out: walk back "
            "out the way you came (to where camp() last worked) or fast_travel to a waypoint, then camp(); here "
            "short_rest, potions and healing spells are what's left.")


SWAP_DIALOGUE_S = 25.0  # swap_party: the longest it drives the companion's dialogue
SWAP_REMARK_RETRY_S = 1.5  # swap_party: a talk that gave only a one-line remark is tried once more after this
# S070 20:59:06: swap_party 7 s after the long rest's wake-up: Wyll (a speaker of the dream scene) still stood where
# the scene put him: talk "not visible", walk "no path … travelled=0", FAILED, and the agent played Wyll's own quest
# without him. Within REST_SETTLE_WINDOW_S of a long rest such a failure waits (≤ SWAP_REST_SETTLE_S) for the
# companion to move back to its camp spot, then walks and talks again; never a final failure while it hasn't.
REST_SETTLE_WINDOW_S, SWAP_REST_SETTLE_S, SWAP_SETTLE_MOVE_M = 180.0, 30.0, 1.0


WALK_UP_SHORT_M = 2.0  # _walk_up_to stops this far short of the character (act.talk's approach covers the rest)


def _walk_up_to(char: str, uuid: str, name: str, pos: list | None = None) -> str:
    """Walks `char` next to a character too far to talk to (one walk, or travel_to's legs past MOVE_CAP_M).
    pos: where it stands when it isn't in view (state.camp rows carry it for the camp companions)."""
    far = world_tools._far_move(char, None if pos else uuid, pos)
    if far:
        dest, d = far
        here = world_tools._ent_pos(char)
        if here and d > WALK_UP_SHORT_M:
            # Acceptance 12:2x (swap to Wyll, 59 m): the walk ended on his spot and travel_to added a 3.4 m second leg
            # (1.6 s). Stop short of him on the way in: the talk job walks the last metres itself.
            k = (d - WALK_UP_SHORT_M) / d
            dest = [here[0] + (dest[0] - here[0]) * k, dest[1], here[2] + (dest[2] - here[2]) * k]
        out = travel_macro.travel_to(x=dest[0], y=dest[1], z=dest[2], who=core.H.get(char))   # who: a handle; runs async
        if "started" in out:
            batch.wait_for(target="idle", timeout_s=60, lead_s=0)
        first = out.splitlines()[0] if out else ""
        return f"walked to {name} ({render.num(round(d))}m)" + (f": {first[:160]}" if "FAILED" in first else "")
    dest = {"pos": list(pos[:3])} if pos else {"target": uuid}
    r = core.game.run_job("act.move_to", {"char": char, "run": True, **dest}, wait_s=45)
    if pos and r.status != "done" and "hasn't walked there" in str(r.error or ""):
        # Live D6 B11b: Wyll's camp spot 44 m off, past sight range: the walk to that point was refused as unseen
        # ground, while a camp companion itself counts as seen at camp (Visibility: CampPresence). Walk to it.
        r = core.game.run_job("act.move_to", {"char": char, "run": True, "target": uuid}, wait_s=45)
    return f"walked to {name}" + ("" if r.status == "done" else f" ({plumbing._job_text('move_to', r)[:120]})")


def _swap_settle(inc: dict) -> tuple[dict | None, float]:
    """After a long rest: wait until the camp companion's row (state.camp) stands ≥ SWAP_SETTLE_MOVE_M from where it
    was (back on its camp spot). -> (the new row, s waited) or (None, s waited)."""
    t0 = time.monotonic()
    p0 = inc.get("pos")
    while time.monotonic() - t0 < SWAP_REST_SETTLE_S:
        time.sleep(1.0)
        info = core.game.try_rpc("state.camp") or {}
        row = next((c for c in info.get("camp") or [] if isinstance(c, dict) and c.get("uuid") == inc.get("uuid")),
                   None)
        p = (row or {}).get("pos")
        if row and p and (not p0 or math.dist(p[:3], p0[:3]) >= SWAP_SETTLE_MOVE_M):
            return row, time.monotonic() - t0
    return None, time.monotonic() - t0


def _camp_roster_back() -> tuple[list[dict], float]:
    """After a long rest: wait (<= SWAP_REST_SETTLE_S) for state.camp to list the camp companions again (live D2
    B11: 0.3 s after long_rest(partial) it listed nobody; Wyll and Halsin were back ~20 s later: the engine gives
    them CampPresence again after the night). -> (the camp rows, possibly empty, s waited)."""
    t0 = time.monotonic()
    rows: list[dict] = []
    while not rows and time.monotonic() - t0 < SWAP_REST_SETTLE_S:
        time.sleep(1.0)
        info = core.game.try_rpc("state.camp") or {}
        plumbing._remember_names(info)
        rows = [c for c in info.get("camp") or [] if isinstance(c, dict)]
    return rows, time.monotonic() - t0


@core.mcp.tool()
@reporting._guard
def swap_party(into: str, out: str = "", leave_camp: bool = True) -> str:
    """Swap a companion waiting at camp into the active party in one call, out of combat, from anywhere: goes to camp, talks to `into`, picks its join answer (and, with a full party, the answer that sends `out` to camp), leaves camp and re-chains everyone to the leader. out: who goes to camp (required with 4 members; never your own character). get_party(camp=true) lists who waits at camp; get_status's PARTY line says when a swap pays off. A camp scene or an answer it can't recognise stops it and shows the dialogue: answer with choose_dialogue, then call it again. A companion whose menu has no join answer (the game's story state) is reported and left."""
    t0 = time.monotonic()
    args = {"into": into, "out": out}

    def done(body: str) -> str:
        return reporting._finish("swap_party", args, body, t0)

    if core._session_over():
        return done("REFUSED: the session is over. Save and write your handoff notes.")
    st = core.game.rpc("state.status")
    plumbing._remember_names(st)
    if st.get("in_combat") or st.get("in_dialogue"):
        return done("swap_party: REFUSED — not during " + ("combat" if st.get("in_combat") else "a conversation"))
    info = core.game.try_rpc("state.camp") or {}
    plumbing._remember_names(info)
    steps: list[str] = []
    camp_rows = [c for c in info.get("camp") or st.get("camp") or [] if isinstance(c, dict)]
    party_rows = [r for r in info.get("party") or st.get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    inc = companions.find_row(camp_rows, into, plumbing._uuid)
    if inc is None:
        if companions.find_row(party_rows, into, plumbing._uuid):
            return done(f"swap_party: {into} is already in the party")
        if not camp_rows and time.monotonic() - inventory.LONG_REST_AT[0] < REST_SETTLE_WINDOW_S:
            camp_rows, waited = _camp_roster_back()
            inc = companions.find_row(camp_rows, into, plumbing._uuid)
            steps.append(f"waited {waited:.0f} s for the camp roster after the long rest")
    if inc is None:
        return done("".join(x + "\n" for x in steps) + f"swap_party: FAILED — {into} is not waiting at camp. At camp: "
                    + (", ".join(f"{c.get('name')} ({companions.class_text(c)})" for c in camp_rows) or "nobody"))
    outr = companions.find_row(party_rows, out, plumbing._uuid) if out else None
    if out and outr is None:
        return done(f"swap_party: FAILED — {out} is not in the party ({', '.join(str(r.get('name')) for r in party_rows)})")
    if outr is not None and outr.get("avatar"):
        return done(f"swap_party: REFUSED — {outr.get('name')} is your own character; name a companion as out")
    full = len(party_rows) >= 4
    if full and outr is None:
        live = [r for r in party_rows if not r.get("dead")]
        sug = companions.swap_out(live, [r for r in live if not r.get("avatar")], inc)
        return done("swap_party: the party is full (4): name who goes to camp, e.g. swap_party(into=\""
                    + str(inc.get("name")) + "\", out=\"" + str((sug or {}).get("name", "?")) + "\")")
    ui = core.game.try_rpc("ui.state", context="client") or {}
    went = False
    if not overview._at_camp(ui):
        # S039 21:48:34 (Nightsong's Prison): "FAILED at go to camp — camp: FAILED — cannot travel right now" with no
        # reason. Same area pre-check as camp(): a greyed camp button (Location) answers at once, with the way out.
        b = _travel_block()
        if b.get("error") == "Location":
            return done("swap_party: FAILED at go to camp — cannot travel right now —" + _camp_block_text(b)
                        + " Swap once camp() works again.")
        r = core.game.run_job("act.go_to_camp", {}, wait_s=60)
        if r.status != "done" and "already at camp" not in str(r.error or ""):
            # (live 11:14: with the level-up page up the UI didn't say "camp" while the story's DB did: not a failure)
            text = plumbing._job_text("camp", r)
            if "cannot travel right now" in text:
                text += _camp_block_text(_travel_block())
            return done("swap_party: FAILED at go to camp — " + text)
        went = r.status == "done"
        steps.append("went to camp" if went else "at camp")
        time.sleep(1.0)
        ui = core.game.try_rpc("ui.state", context="client") or {}
        if ui.get("screen") == "dialogue":
            return done("\n".join(steps + ["STOPPED: a camp scene started on arrival: answer it (choose_dialogue), "
                                            f"then swap_party(into=\"{inc.get('name')}\", out=\"{out}\") again"]
                                   + reporting._render_ui(ui)))
        # state.camp gives a companion's camp spot only while the party is at camp (vis V22): read it on arrival
        info = core.game.try_rpc("state.camp") or {}
        inc = companions.find_row([c for c in info.get("camp") or [] if isinstance(c, dict)], into, plumbing._uuid) or inc
    leader = st.get("active") or plumbing._active()
    # The join menu is the player's own character's (S032 17:27: Shadowheart was in control, Wyll: "I'm all chatted
    # out ... I may have a word with the chief", no window, swap FAILED; the agent then walked Claude over by hand,
    # ~55 s). Talk as the avatar; the leader is the fallback when no avatar row is known.
    talker = next((r["uuid"] for r in party_rows if r.get("avatar") and r.get("uuid") and not r.get("dead")), leader)
    r = core.game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
    if r.status != "done" and any(w in plumbing._job_text("talk_to", r) for w in ("too far away", "not visible")):
        # Live 2026-10-10: Halsin stood 66 m from the camp arrival point, out of view (talk is capped at 40 m and
        # needs sight): walk up to where state.camp says it stands first.
        steps.append(_walk_up_to(talker, inc["uuid"], str(inc.get("name")), inc.get("pos")))
        r = core.game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
    unreachable = ("too far away", "not visible")
    if r.status != "done" and any(w in plumbing._job_text("talk_to", r) for w in unreachable) \
            and time.monotonic() - inventory.LONG_REST_AT[0] < REST_SETTLE_WINDOW_S:
        moved, waited = _swap_settle(inc)
        if not moved:
            return done("\n".join(steps + [
                f"swap_party: NOT YET — {inc.get('name')} hasn't come back to its camp spot since the long rest "
                f"(still where the rest's scene left it, {waited:.0f} s waited: {plumbing._job_text('talk_to', r)}). Not a "
                f"refusal: swap_party(into=\"{inc.get('name')}\", out=\"{out}\") again in a minute"]))
        steps.append(f"waited {waited:.0f} s for {inc.get('name')} to return to its camp spot after the long rest")
        inc = moved
        steps.append(_walk_up_to(talker, inc["uuid"], str(inc.get("name")), inc.get("pos")))
        r = core.game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
    if r.status != "done":
        return done("\n".join(steps + ["swap_party: FAILED to talk to " + str(inc.get("name")) + " — " + plumbing._job_text("talk_to", r)]))
    picked, stop = [], None
    asked = offered = False   # offered: the companion showed an answer list (not a one-line remark)
    for attempt in range(2):
        deadline = time.monotonic() + SWAP_DIALOGUE_S
        opened = False
        while time.monotonic() < deadline:
            ui = core.game.try_rpc("ui.state", context="client") or {}
            if ui.get("screen") != "dialogue":
                if not opened and time.monotonic() < deadline - SWAP_DIALOGUE_S + 3.0:
                    time.sleep(0.25)
                    continue
                break
            opened = True
            # S081 #95: Karlach's join answer was below the fold of a long camp menu: the whole list, read by
            # scrolling (cached per node), numbered as the agent would see it; answers are chosen by their text
            ui = reporting._complete_answers(ui)
            d = ui.get("dialogue") or {}
            opts = [o for o in d.get("options") or [] if not o.get("off")]
            offered = offered or bool(opts)
            if d.get("changing") or d.get("listening") or (not opts and not d.get("continue")):
                time.sleep(0.3)
                continue
            i, what = None, ""
            if not opts:
                i, what = 0, "continue"
            else:
                if outr is not None and asked:
                    # only after the join answer: a companion's top menu may name others ("About Gale...")
                    i = companions.pick_swap(opts, str(outr.get("name")))
                    what = f"send {outr.get('name')} to camp"
                if i is None and not asked:
                    i, what = companions.pick_join(opts), "join"
                    asked = i is not None
                if i is None:
                    ok, fi, _ = macros.dialogue_filler(d, strict=True)
                    i, what = (fi, "continue") if ok else (None, "")
            if i is None and not asked:   # no join answer in the companion's menu (pick_join found none)
                lv = next((int(o.get("i") or 0) for o in opts if re.match(r"\s*leave\b", str(o.get("text") or ""), re.I)), 0)
                if lv:
                    # Live 2026-10-10 (SCL camp): Halsin's menu had no join answer at all (Wyll's had "I'd like you to
                    # join me."): the game decides when a companion may join. Leave and stop suggesting it here.
                    name = str(inc.get("name"))
                    try:
                        dialogue._choose_text(lv, next(str(o.get("text")) for o in opts if int(o.get("i") or 0) == lv))
                    except GameError:
                        pass  # reported below either way: the menu has no join answer
                    overview._no_join[inc["uuid"]] = str(st.get("region") or "")
                    if went and leave_camp:
                        core.game.run_job("act.leave_camp", {}, wait_s=60)
                        steps.append("left camp")
                    menu = "; ".join(str(o.get("text"))[:50] for o in opts)
                    return done("\n".join(steps + [
                        f"swap_party: {name} doesn't offer to join right now: the camp menu has no join answer ({menu}). "
                        f"The game decides when (story/quest state, e.g. after a camp scene or a quest step). Left the "
                        f"conversation; the PARTY hint skips {name} in this region."]))
            if i is None:
                stop = "no join/swap answer recognised: pick it with choose_dialogue (then camp(action='leave'), regroup)"
                break
            if what != "continue":
                txt = next((str(o.get("text")) for o in opts if int(o.get("i") or 0) == i), "")
                picked.append(f"{what}: '{txt[:70]}'")
            try:
                rj = core.game.run_job("ui.dialogue_choose", {"i": i}, context="client", wait_s=20) if what == "continue" \
                    else dialogue._choose_text(i, txt)
            except GameError as e:
                rj = JobResult(status="failed", error=str(e))
            if rj.status != "done":
                stop = f"could not pick option {i}: {rj.error or rj.status}"
                break
            time.sleep(0.3)
        else:
            stop = "the dialogue is still open after " + str(int(SWAP_DIALOGUE_S)) + " s"
        if stop or asked or offered or attempt:
            break
        # S080 #60: Karlach's talk opened a one-line remark ("Hm? Did you... are you talking to me?"), no answers,
        # and swap_party said "did not join … Talk to it: talk_to". A player clicks her once more.
        time.sleep(SWAP_REMARK_RETRY_S)
        r = core.game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
        if r.status != "done" and any(w in plumbing._job_text("talk_to", r) for w in unreachable):
            # she walked off meanwhile: the camp roster says where she stands now (not a sighting)
            steps.append(_walk_up_to(talker, inc["uuid"], str(inc.get("name")), _camp_pos(inc["uuid"]) or inc.get("pos")))
            r = core.game.run_job("act.talk", {"char": talker, "npc": inc["uuid"]}, wait_s=40)
        if r.status != "done":
            steps.append("talk again: " + plumbing._job_text("talk_to", r))
            break
        steps.append(f"{inc.get('name')} only made a remark: talked again")
    if not (stop or asked or offered):
        name = str(inc.get("name"))
        overview._no_join[inc["uuid"]] = str(st.get("region") or "")
        if went and leave_camp:
            core.game.run_job("act.leave_camp", {}, wait_s=60)
            steps.append("left camp")
        return done("\n".join(steps + [(
            f"swap_party: {name} doesn't offer to join right now: talking to them gave only a one-line remark "
            f"(EVENTS) and no answers, so the game opens no camp menu for them at the moment (story or "
            f"camp state; it may change after a long rest or a story step). Nothing changed; the PARTY hint skips "
            f"{name} in this region.")]))
    steps += picked
    if stop:
        ui = core.game.try_rpc("ui.state", context="client") or {}
        return done("\n".join(steps + [f"STOPPED: {stop}"] + reporting._render_ui(ui)))
    st2 = core.game.rpc("state.status")
    plumbing._remember_names(st2)
    now = {r.get("uuid") for r in st2.get("party") or []}
    joined = inc["uuid"] in now
    left = outr is None or outr["uuid"] not in now
    if not joined:
        return done("\n".join(steps + [f"swap_party: FAILED — {inc.get('name')} did not join (party: "
                                        + ", ".join(queueing._label(u) for u in now) + "). Talk to it: talk_to(\"" + str(inc.get("name")) + "\")"]))
    steps.append(f"{queueing._label(inc['uuid'])} joined" + (f", {outr.get('name')} waits at camp" if outr is not None and left else ""))
    if outr is not None and not left:
        steps.append(f"WARNING: {outr.get('name')} is still in the party")
    if went and leave_camp:
        r = core.game.run_job("act.leave_camp", {}, wait_s=60)
        steps.append("left camp" if r.status == "done" else "leave camp: " + plumbing._job_text("camp", r))
        st2 = core.game.try_rpc("state.status") or st2
    lead = st2.get("active") or leader
    members = [r.get("uuid") for r in st2.get("party") or [] if r.get("uuid") != lead and not r.get("summon")
               and r.get("uuid") not in overview._stay]
    took = overview._rechain(members, lead) if members else []
    if took:
        steps.append("chained to " + queueing._label(lead) + ": " + ", ".join(queueing._label(u) for u in took))
    lv = overview._level_up_line(st2)
    if lv:
        steps.append(lv)
    overview.PARTY_HINT.skill_sig = None  # the roster changed: the next status re-reads the skills
    return done("swap_party: done — " + "; ".join(steps))


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402
    dialogue, inventory, uitools, batch, queueing, turn_slots, travel_macro, saving,
)
