"""Party cohesion: who is apart from the leader (the active character), and how to say it in one line.

S005: Claude travelled 90 m alone, Shadowheart (unchained after the crash landing) stayed 77 m behind, and
regroup failed ("destination is too far away": one 77 m move). Pure helpers here, wired in server.py:
get_status/look print `PARTY APART: ...`, members are re-chained to the leader unless told to stay,
the leader's travel_to waits for a member who falls behind, and regroup walks members back in legs.
"""

from __future__ import annotations

from typing import Any, Callable

APART_M = 15.0        # farther than this from the leader is "apart"
FLOOR_M = 4.0         # ...or this far above/below it: another floor (S015: Gale left on a 5 m ledge 7 m away, no path)
STRAGGLER_M = 12.0    # travel_to: a member this far behind after a leg gets waited for
CATCH_UP_S = 4.0      # ...this long (following on its own), before it is walked back in legs
CATCH_UP_GAIN_M = 0.5  # ...and longer while the gap still shrinks by this much per poll (trailing a run, not stuck)
CATCH_UP_MAX_S = 15.0  # ...up to this long in all
REGROUP_WAIT_S = 60.0  # regroup: the longest it waits for the walks back


def _d(a: Any, b: Any) -> float | None:
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) >= 3 and len(b) >= 3):
        return None
    return sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)) ** 0.5


def leader_row(status: dict, leader: str | None = None) -> dict | None:
    lead = leader or (status or {}).get("active")
    return next((r for r in (status or {}).get("party") or [] if r.get("uuid") == lead), None)


def summons(status: dict) -> set:
    """Uuids of summoned party rows (Spiritual Weapon, Find Familiar, Mage Hand...: the mod tags them
    summon=true). They are allies in combat but not companions: never apart, re-chained, waited for or
    walked back (S017 probe: a chained `Spiritual Weapon` row)."""
    return {r.get("uuid") for r in (status or {}).get("party") or [] if r.get("summon")}


def _dy(a: Any, b: Any) -> float | None:
    """Height of a over b (y is up), None without both positions."""
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) >= 3 and len(b) >= 3):
        return None
    return float(a[1]) - float(b[1])


def apart(status: dict, leader: str | None = None, far_m: float = APART_M, stay: set | None = None,
          floor_m: float = FLOOR_M) -> list[dict]:
    """Members away from the leader: [{uuid, name, dist|None, dy|None, other_level, unchained, downed, stay}].
    status: state.status (party rows with pos, region, chained). Dead and off-stage members are left out;
    an unchained member counts even when near (it won't follow the next walk), so does one on another floor
    (more than floor_m above/below: a ledge or a drop it may have no path from). Summons are left out,
    and nothing is apart from a summon leader (the last combat_turn may have selected one)."""
    lead = leader_row(status, leader)
    if not lead or lead.get("summon"):
        return []
    out = []
    for r in status.get("party") or []:
        u = r.get("uuid")
        if u == lead.get("uuid") or r.get("dead") or r.get("off_stage") or r.get("summon"):
            continue
        other = bool(r.get("region") and lead.get("region") and r["region"] != lead["region"])
        d = None if other else _d(r.get("pos"), lead.get("pos"))
        dy = None if other else _dy(r.get("pos"), lead.get("pos"))
        floor = dy is not None and abs(dy) > floor_m
        unchained = r.get("chained") is False
        if other or (d is not None and d > far_m) or unchained or floor:
            out.append({"uuid": u, "name": r.get("name"), "dist": d, "dy": dy if floor else None, "other_level": other,
                        "unchained": unchained, "downed": bool(r.get("downed")), "stay": u in (stay or set())})
    return out


def _where(r: dict, far_m: float = APART_M) -> str:
    """'136m, 64m above, unchained' for an apart() row."""
    if r["other_level"]:
        what = "other level"
    elif r["dist"] is not None and r["dist"] > far_m:
        what = f"{round(r['dist'])}m"
    else:
        what = "near" if not r.get("dy") else f"{round(r['dist'])}m"
    if r.get("dy"):
        what += f", {round(abs(r['dy']))}m {'above' if r['dy'] > 0 else 'below'}"
    if r["unchained"]:
        what += ", unchained"
    if r["downed"]:
        what += ", downed"
    return what


def apart_line(rows: list[dict], label: Callable[[str], str], far_m: float = APART_M) -> str:
    """'PARTY APART: e1 Lae'zel 34m, e3 Gale other level → regroup()' ('' when everyone is together).
    Members told to stay are listed as such; an unchained member near the leader as 'unchained'."""
    bits, fix = [], False
    for r in rows:
        what = _where(r, far_m)
        if r["stay"]:
            what += " (staying)"
        else:
            fix = fix or r["other_level"] or (r["dist"] or 0) > far_m or r["unchained"] or bool(r.get("dy"))
        bits.append(f"{label(r['uuid'])} {what}")
    if not bits:
        return ""
    return "PARTY APART: " + ", ".join(bits) + (" → regroup()" if fix else "")


def rechain_candidates(rows: list[dict], busy: Callable[[str], bool]) -> list[str]:
    """Unchained members to attach back to the leader now: not told to stay, not downed, on this level,
    and not in the middle of their own walk or queue."""
    return [r["uuid"] for r in rows
            if r["unchained"] and not r["stay"] and not r["downed"] and not r["other_level"] and not busy(r["uuid"])]


def stragglers(rows: list[dict], leader_pos: Any, far_m: float = STRAGGLER_M, stay: set | None = None,
               skip: set | None = None) -> list[tuple[str, float]]:
    """From act.watch party rows: (uuid, metres behind) of members (not staying, not downed, not in skip:
    the summons, which act.watch doesn't tag) farther than far_m from the leader's position."""
    out = []
    for r in rows or []:
        u = r.get("uuid")
        if u in (stay or set()) or u in (skip or set()) or r.get("downed") or r.get("summon"):
            continue
        d = _d(r.get("pos"), leader_pos)
        if d is not None and d > far_m:
            out.append((u, d))
    return out


WAYPOINT_HERE_M = 8.0   # fast_travel: the selected member already stands at the waypoint (S064: 4.5 m from its circle)
CAMP_BEHIND_M = 150.0   # at camp: a member this far from the actor stayed behind (the camp is ~40 m across; S065 3863 m)


def travel_behind(rows: list[dict]) -> list[dict]:
    """apart() rows a fast travel leaves where they are: unchained (out of the selected member's group) or on
    another level, and not told to stay. S064 11:36: with Karlach selected and Wyll + Shadowheart unchained 138 m
    away, the game moved Karlach alone ('arrived'); the others took one fast travel each."""
    return [r for r in rows if not r["stay"] and (r["unchained"] or r["other_level"])]


def camp_behind(status: dict, stay: set | None = None, far_m: float = CAMP_BEHIND_M) -> list[dict]:
    """Members the camp travel left behind: living, not summons, on another level or farther than far_m from the
    biggest group (the camp; the active member may be the one left out). S065 12:01:47: camp() 'done — at_camp'
    moved Claude, Karlach and Shadowheart; Wyll stayed 3863 m away and long_rest failed 4 s later (the rest
    check: 'Wyll: not at a place where you can rest')."""
    members = [r for r in (status or {}).get("party") or []
               if r.get("uuid") and not (r.get("dead") or r.get("off_stage") or r.get("summon"))]

    def near(a: dict, b: dict) -> bool:
        if a.get("region") and b.get("region") and a["region"] != b["region"]:
            return False
        d = _d(a.get("pos"), b.get("pos"))
        return d is not None and d <= far_m
    if len(members) < 2:
        return []
    active = (status or {}).get("active")
    ref = max(members, key=lambda m: (sum(near(m, o) for o in members), m.get("uuid") == active))
    rows = apart(status, leader=ref["uuid"], far_m=far_m, stay=stay, floor_m=float("inf"))
    # the chain doesn't matter for camp travel (and `chained` is relative to the active member, not the camp)
    return [{**r, "unchained": False} for r in rows if r["other_level"] or (r["dist"] is not None and r["dist"] > far_m)]


def behind_text(rows: list[dict], label: Callable[[str], str]) -> str:
    """'e1 Karlach 136m, 64m above, unchained; e2 Wyll other level'."""
    return "; ".join(f"{label(r['uuid'])} {_where(r)}" for r in rows)


def moved_left_behind(status: dict, mover: str, far_m: float = APART_M) -> list[dict]:
    """apart() rows, measured from a member the game just moved, who are far from it or on another floor/level
    (an unchained member standing next to it is not left behind). S066 19:11:50: Orin's arena pulled Claude 22 m
    over the chasm; Karlach, Wyll and Shadowheart stayed 23-25 m back and Claude fought 3 rounds alone."""
    return [r for r in apart(status, leader=mover, far_m=far_m)
            if not r["stay"] and (r["other_level"] or r.get("dy") or (r["dist"] or 0) > far_m)]


def game_moved_text(tool: str, res: dict, who: str, label: Callable[[str], str], area: str | None,
                    behind: list[dict], sealed_from: str | None, in_combat: bool = False) -> str:
    """The result of a walk the game's story cut short by moving the walker itself (Actions.lua tele_finish,
    by_game): where it is now and how the rest of the party follows. sealed_from: the member nav found no
    walkable way from (None: the way was found or not checked)."""
    pos = res.get("pos") or []
    at = f" to @({','.join(f'{float(v):g}' for v in pos[:3])})" if len(pos) >= 3 else ""
    out = [f"{tool}: INTERRUPTED — the game moved {label(who)} {round(float(res.get('m') or 0))}m{at}"
           + (f" into {area}" if area else "") + f" ({res.get('why') or 'a story move'}): not our walk, nothing "
           "to undo; the scene there is theirs now"]
    if behind:
        line = "PARTY LEFT BEHIND: " + behind_text(behind, label)
        if sealed_from:
            line += (f"; no walkable way from {label(sealed_from)} to {label(who)} (a sealed story area?): plan the "
                     "scene without them, or reload before the approach and walk in together")
        elif in_combat:
            line += f" → in combat: move them toward {label(who)} on their turns (combat_turn move)"
        else:
            line += (f" → regroup(leader={label(who).split(' ')[0]}) walks them to {label(who)} (their walks keep "
                     "going through a conversation they are not in)")
        out.append(line)
    return "\n".join(out)
