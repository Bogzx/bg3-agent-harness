"""Section nav: frontier, routes, fast travel and the ladder climb."""

from __future__ import annotations

import re
import time

from .core import BASE, S, call, far_walk_target, first_line, grab, item, load_verified, need, route_brief, status, took


def sec_nav() -> None:
    if not load_verified(BASE):
        return
    st = status()
    region = str((S.game.try_rpc("ui.state", context="client") or {}).get("region", ""))
    if region.lower() == "campsite":
        call("camp", action="leave")
    me = (status().get("party") or [{}])[0]
    p = me.get("pos") or [0, 0, 0]
    # Multi-leg walk: a route-proven spot of the region from its waypoint (KNOWN_WALK), else a lead 40-160 m away
    # (raw nav.frontier positions: get_frontier's text has none; window 20261016-183253: only doors 10-17 m here).
    fr = call("get_frontier")
    raw_fr = S.game.try_rpc("nav.frontier", {"radius": 160, "limit": 60}) or {}
    tgt = far_walk_target(p, raw_fr.get("leads"), status().get("region") or region)
    if need(tgt is not None, "travel_to multi-leg", "no known spot or frontier point 40-160 m away to walk to: "
            + first_line(fr)):
        q, via = tgt
        ok_start = True
        if via:
            # What the game's own path says from here (window #2: travel_to fell back to dead-end straight legs):
            # one probe, recorded for the next window's diagnosis, no walk.
            item("nav.route from the start to the multi-leg spot (no walk)", "INFO",
                 f"from {[round(float(x), 1) for x in p[:3]]}: "
                 + route_brief(S.game.try_rpc("nav.route", {"pos": list(q), "connectors": False})))
            ft = call("fast_travel", waypoint=via)
            ok_start = need("FAILED" not in ft and "REFUSED" not in ft, "travel_to multi-leg",
                            f"fast_travel {via} failed: " + first_line(ft))
            p = (status().get("party") or [{}])[0].get("pos") or p
        if ok_start:
            out, dt = took("travel_to", x=q[0], y=q[1], z=q[2])
            end = time.monotonic() + 180
            w = ""
            while time.monotonic() < end:
                w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
                if "still waiting" not in w:
                    break
            me2 = (status().get("party") or [{}])[0].get("pos") or p
            left = ((me2[0] - q[0]) ** 2 + (me2[2] - q[2]) ** 2) ** 0.5
            legs = len(re.findall(r"(?m)^leg \d+:", w))
            ok = left < 6 and legs >= 2 and "INTERRUPTED" not in w
            item("travel_to multi-leg", "PASS" if ok else "FAIL",
                 f"{((q[0]-p[0])**2+(q[2]-p[2])**2)**0.5:.0f} m trip{' from ' + via if via else ''}, {legs} legs, "
                 f"{left:.1f} m left at the end; {first_line(out)} | {first_line(w)}")

    # Interior via entrance: the Mausoleum room from its gate is REFUSED naming the entrance, then interact.
    ft, dt = took("fast_travel", waypoint="Moonrise Towers")
    if not need("FAILED" not in ft, "travel_to into an interior via entrance", "fast_travel Moonrise Towers failed: " + first_line(ft)):
        load_verified(BASE)
        return
    t = time.monotonic()
    out = call("travel_to", x=-183.0, y=38.0, z=99.0)
    end = time.monotonic() + 240
    while time.monotonic() < end:
        w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
        if "still waiting" not in w:
            break
    legs = len(re.findall(r"(?m)^leg \d+:", w))
    arr = grab(r"arrived \([\d.]+m from it\)", w)
    item("travel_to multi-leg (waypoint → Mausoleum gate)", "PASS" if arr and legs >= 2 else "FAIL",
         f"{first_line(out)}; {legs} legs, {arr or first_line(w)} in {time.monotonic() - t:.0f}s")
    ladder_from_top()
    if S.game.try_rpc("state.combat", {}) and (S.game.try_rpc("state.combat") or {}).get("active"):
        item("travel_to into an interior via entrance", "SKIP", "a fight started on the way (reloaded)")
        load_verified(BASE)
        return
    t = time.monotonic()
    ref = call("travel_to", x=-286.0, y=-31.0, z=-847.5)
    rdt = time.monotonic() - t
    m = re.search(r"(e\d+)[^\n]{0,30}Mausoleum Entrance", ref + "\n" + call("get_frontier"))
    ok = "REFUSED" in ref and "interact" in ref
    item("travel_to other floor → entrance named", "PASS" if ok else "FAIL", f"{rdt:.2f}s; {first_line(ref)}")
    if need(bool(m), "travel_to into an interior via entrance", "no Mausoleum Entrance handle near the gate"):
        out, dt = took("interact", target=m.group(1))
        time.sleep(3)
        lv = status()
        tr = call("travel_to", x=-283.0, y=-31.0, z=-847.0)
        for _ in range(4):
            w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
            if "still waiting" not in w:
                break
        pos = (status().get("party") or [{}])[0].get("pos") or [0, 0, 0]
        d = ((pos[0] + 283) ** 2 + (pos[2] + 847) ** 2) ** 0.5
        item("travel_to into an interior via entrance", "PASS" if d < 8 else "FAIL",
             f"interact entrance {dt:.1f}s ({first_line(out)}); then {d:.1f} m from the room spot")
        if d < 15:
            nb = call("get_nearby", radius=25, kinds="button")
            tags = re.findall(r"\(under ([^)]+)\)", nb)
            trap = "TRAP spotted" in w  # a perception roll: the walk stops short and fewer buttons are in sight
            item("buttons listed (Mausoleum)", "PASS" if len(tags) >= (1 if trap else 2) else "FAIL",
                 f"{len(re.findall(r'Button', nb))} buttons, tags={tags[:4]}" + ("; the walk stopped at a spotted trap" if trap else ""))
        else:
            item("buttons listed (Mausoleum)", "SKIP", "did not reach the puzzle room")
    load_verified(BASE)


KNOTTED_ROOTS = (-194.8, 31.2, 80.6)  # the ladder (its foot) below the Mausoleum gate spot (-183, 38.2, 99)
LADDER_TOP = (-194.9, 35.6, 79.1)     # where its Use stands at the top (live 14:2x)


def ladder_from_top() -> None:
    """At the Mausoleum gate: the Knotted Roots ladder's foot is ~7 m below; interact climbs down from the top.
    The ACTIVE character must be the one who climbs (vis 13:3x: Gale climbed instead of Claude)."""
    def heights() -> tuple[str, dict]:
        st = status()
        return st.get("active"), {r.get("name"): float((r.get("pos") or [0, 0, 0])[1]) for r in st.get("party") or []}

    def me_pos() -> list:
        st = status()
        return next((r.get("pos") for r in st.get("party") or [] if r.get("uuid") == st.get("active")), None) or [0, 0, 0]

    p0 = me_pos()
    raw = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["ladder"]}) or {}
    lads = [it for it in (raw.get("items") or []) if it.get("pos") and p0[1] - it["pos"][1] >= 2.5]
    if not lads:  # in view from the gate only at some angles: step toward the ladder's top first
        tr = call("travel_to", x=LADDER_TOP[0] + 2.0, y=LADDER_TOP[1], z=LADDER_TOP[2] - 2.0)
        if "started" in tr:
            call("wait_for", target="queue", timeout_s=40, lead_s=0)
        p0 = me_pos()
        raw = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["ladder"]}) or {}
        lads = [it for it in (raw.get("items") or []) if it.get("pos") and p0[1] - it["pos"][1] >= 2.5]
    if not need(bool(lads), "ladder from the top", f"no ladder foot >= 2.5 m below the party at {p0} "
                f"({len(raw.get('items') or [])} ladders seen)"):
        return
    lad = min(lads, key=lambda it: (it["pos"][0] - p0[0]) ** 2 + (it["pos"][2] - p0[2]) ** 2)
    h = S.H.get(lad["uuid"], lad.get("name"))
    act, y0 = heights()
    name = next((r.get("name") for r in status().get("party") or [] if r.get("uuid") == act), "?")
    out, dt = took("interact", target=h)
    foot = float(lad["pos"][1])
    ys, t_c = y0, time.monotonic()
    for _ in range(24):  # the climb runs on after the interact job ends (followers climb too)
        _, ys = heights()
        if ys.get(name, 99) < foot + 1.5:
            break
        time.sleep(0.5)
    t_c = time.monotonic() - t_c
    climbed = [n for n in ys if ys[n] < foot + 1.5]
    ok = "FAILED" not in out and "REFUSED" not in out and name in climbed
    item("ladder from the top (active character climbs)", "PASS" if ok else "FAIL",
         f"{lad.get('name')} (foot y {foot:.1f}) {dt:.1f}s + {t_c:.1f}s; active={name}; at the foot={climbed}; heights "
         + ", ".join(f"{n} {y0.get(n, 0):.1f}->{ys[n]:.1f}" for n in ys) + f"; {first_line(out)}")
    # act ladder 4th look (A.LADDER_WATCH_AT_USE): from the gate the job must END done (10-11 08:37/12:22 "still
    # running" at 25 s) and the climber must stay at the foot (10:43: "done climbed=down", back at the top 12 s later)
    held = []
    for _ in range(5):
        time.sleep(1.0)
        held.append(heights()[1].get(name, 99))
    stays = bool(held) and max(held) < foot + 1.5
    item("ladder climb ends done and holds (act LADDER_WATCH_AT_USE)",
         "PASS" if ok and "interact: done" in out and "climbed=" in out and stays else "FAIL",
         f"{first_line(out)}; {name} over 5 s after: " + ", ".join(f"{y:.1f}" for y in held)
         + f" (foot {foot:.1f}); FAIL with 'still running' or a climb back up: turn the flag off "
         "(Mods.BG3AI.BG3AI.Actions.LADDER_WATCH_AT_USE = false) and rerun to compare")
    # back to the gate for the entrance check (up the ladder or round by the path)
    call("travel_to", x=-183.0, y=38.0, z=99.0)
    for _ in range(3):
        if "still waiting" not in call("wait_for", target="queue", timeout_s=40, lead_s=0):
            break
