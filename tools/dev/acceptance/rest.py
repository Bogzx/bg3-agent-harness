"""Section rest: short and long rests."""

from __future__ import annotations

import re
import time

from .core import BASE, S, call, first_line, grab, item, load_verified, log, status, took, ui_screen


def sec_rest() -> None:
    if not load_verified(BASE):
        return
    st0 = status()
    out, dt = took("short_rest")
    if "done" in out:
        hp = grab(r"^HP (unchanged|\w+ \d+→).*$", out)
        item("short_rest (HP before→after)", "PASS" if hp else "FAIL", f"{dt:.1f}s; {hp or first_line(out)}")
    else:
        item(
            "short_rest (HP before→after)",
            "FAIL" if "short rest" not in out.lower() else "SKIP",
            f"{dt:.1f}s; {first_line(out)}",
        )

    out, dt = took("camp", action="go")
    if "FAILED" in out and "already at camp" not in out:
        item("camp go", "FAIL", f"{dt:.1f}s; {first_line(out)}")
        load_verified(BASE)
        return
    time.sleep(2)
    out, dt = took("long_rest")
    refused = "partial" in out.lower() and "FAILED" in out
    if refused:
        scr = ui_screen()
        w, wdt = took("wait_for", target="combat_started", timeout_s=10)
        ok = scr == "rest" and "not waiting" in w and wdt < 2
        item("wait_for on the rest screen", "PASS" if ok else "FAIL", f"screen={scr}; {first_line(w)} ({wdt:.2f}s)")
        out, dt = took("long_rest", partial=True)
    else:
        item("wait_for on the rest screen", "SKIP", "long_rest didn't stop at the supplies check (enough supplies)")
    clicks = len(re.findall(r"(?i)hand.?click|click\(", out))
    ok = ("done" in out or "rested" in out) and "FAILED" not in out
    item(
        "long_rest no-click",
        "PASS" if ok else "FAIL",
        f"{dt:.1f}s; {first_line(out)} {grab(r'(?i)^.*(supplies|partial|full).*$', out)}",
    )
    al = grab(r"^ALERTS.*$", call("get_status", full=True))
    item("ALERTS after rest", "PASS" if al else "INFO", al or "no ALERTS (all healthy)")
    rest_texts = out + "\n" + call("look")

    st = status()
    night = S.game.try_rpc("ui.travel_block", context="client")
    log(f"travel_block after rest: {night}")
    out, dt = took("fast_travel", waypoint="Last Light Inn")
    time.sleep(2)
    st = status()
    ui = S.game.try_rpc("ui.state", context="client") or {}
    party = st.get("party") or []
    lead = (party[0].get("pos") if party else None) or [0, 0, 0]
    spread = max((sum((a - b) ** 2 for a, b in zip(r.get("pos") or lead, lead)) ** 0.5 for r in party), default=0)
    ok = "FAILED" not in out and str(ui.get("region", "")).lower() != "campsite" and spread < 25
    item(
        "fast_travel + camp leave (night camp)",
        "PASS" if ok else "FAIL",
        f"{dt:.1f}s; {first_line(out)}; region={ui.get('region')} party spread={spread:.0f} m",
    )
    # S044 23:50:48 fairness LEAK: `rest_flow step=ready_ok:MOO_Assault_ReadyCheck` in look EVENTS (se/vis 02:5x)
    leak = re.findall(r"rest_flow|\w*ReadyCheck\w*|WaypointTravel_\w+", rest_texts + "\n" + out)
    item(
        "no ready-check ids in rest/travel results",
        "PASS" if not leak else "FAIL",
        f"leaks={sorted(set(leak))[:5]}"
        if leak
        else "long_rest, look and fast_travel texts carry no rest_flow line/engine id",
    )
    load_verified(BASE)
