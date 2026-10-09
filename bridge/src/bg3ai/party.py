"""Party cohesion: who is apart from the leader (the active character), and how to say it in one line.

S005: Claude travelled 90 m alone, Shadowheart (unchained after the crash landing) stayed 77 m behind, and
regroup failed ("destination is too far away": one 77 m move). Pure helpers here, wired in server.py:
get_status/look print `PARTY APART: ...`, members are re-chained to the leader unless told to stay,
the leader's travel_to waits for a member who falls behind, and regroup walks members back in legs.
"""

from __future__ import annotations

from typing import Any, Callable

APART_M = 15.0        # farther than this from the leader is "apart"
STRAGGLER_M = 12.0    # travel_to: a member this far behind after a leg gets waited for
CATCH_UP_S = 4.0      # ...this long (following on its own), before it is walked back in legs
REGROUP_WAIT_S = 60.0  # regroup: the longest it waits for the walks back


def _d(a: Any, b: Any) -> float | None:
    if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) >= 3 and len(b) >= 3):
        return None
    return sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)) ** 0.5


def leader_row(status: dict, leader: str | None = None) -> dict | None:
    lead = leader or (status or {}).get("active")
    return next((r for r in (status or {}).get("party") or [] if r.get("uuid") == lead), None)


def apart(status: dict, leader: str | None = None, far_m: float = APART_M, stay: set | None = None) -> list[dict]:
    """Members away from the leader: [{uuid, name, dist|None, other_level, unchained, downed, stay}].
    status: state.status (party rows with pos, region, chained). Dead and off-stage members are left out;
    an unchained member counts even when near (it won't follow the next walk)."""
    lead = leader_row(status, leader)
    if not lead:
        return []
    out = []
    for r in status.get("party") or []:
        u = r.get("uuid")
        if u == lead.get("uuid") or r.get("dead") or r.get("off_stage"):
            continue
        other = bool(r.get("region") and lead.get("region") and r["region"] != lead["region"])
        d = None if other else _d(r.get("pos"), lead.get("pos"))
        unchained = r.get("chained") is False
        if other or (d is not None and d > far_m) or unchained:
            out.append({"uuid": u, "name": r.get("name"), "dist": d, "other_level": other,
                        "unchained": unchained, "downed": bool(r.get("downed")), "stay": u in (stay or set())})
    return out


def apart_line(rows: list[dict], label: Callable[[str], str], far_m: float = APART_M) -> str:
    """'PARTY APART: e1 Lae'zel 34m, e3 Gale other level → regroup()' ('' when everyone is together).
    Members told to stay are listed as such; an unchained member near the leader as 'unchained'."""
    bits, fix = [], False
    for r in rows:
        if r["other_level"]:
            what = "other level"
        elif r["dist"] is not None and r["dist"] > far_m:
            what = f"{round(r['dist'])}m"
        else:
            what = "near"
        if r["unchained"]:
            what += ", unchained"
        if r["downed"]:
            what += ", downed"
        if r["stay"]:
            what += " (staying)"
        else:
            fix = fix or r["other_level"] or (r["dist"] or 0) > far_m or r["unchained"]
        bits.append(f"{label(r['uuid'])} {what}")
    if not bits:
        return ""
    return "PARTY APART: " + ", ".join(bits) + (" → regroup()" if fix else "")


def rechain_candidates(rows: list[dict], busy: Callable[[str], bool]) -> list[str]:
    """Unchained members to attach back to the leader now: not told to stay, not downed, on this level,
    and not in the middle of their own walk or queue."""
    return [r["uuid"] for r in rows
            if r["unchained"] and not r["stay"] and not r["downed"] and not r["other_level"] and not busy(r["uuid"])]


def stragglers(rows: list[dict], leader_pos: Any, far_m: float = STRAGGLER_M, stay: set | None = None) -> list[tuple[str, float]]:
    """From act.watch party rows: (uuid, metres behind) of members (not staying, not downed) farther than
    far_m from the leader's position."""
    out = []
    for r in rows or []:
        u = r.get("uuid")
        if u in (stay or set()) or r.get("downed"):
            continue
        d = _d(r.get("pos"), leader_pos)
        if d is not None and d > far_m:
            out.append((u, d))
    return out
