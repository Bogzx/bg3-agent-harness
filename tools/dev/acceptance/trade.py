"""Section trade: trading with Quartermaster Talli at the Last Light Inn."""

from __future__ import annotations

import re

from .core import TRADE_SAVE, call, first_line, grab, item, load_verified, need, status, took, ui_screen


def wares_block(listing: str) -> str:
    """The WARES lines of a trade listing (not YOUR ITEMS: those are ours to offer, not to request)."""
    m = re.search(r"(?ms)^WARES .*?(?=^YOUR ITEMS|^\(|\Z)", listing)
    return m.group(0) if m else ""


def sec_trade() -> None:
    if not load_verified(TRADE_SAVE):
        return
    lk = call("look", full=True)
    m = re.search(r"(e\d+) Quartermaster Talli", lk)
    if not m:  # Talli stands ~33 m S of the save spot, inside the inn (S02x logs: (-31.3,27.5,130.2))
        tr, dt = took("travel_to", x=-31.0, y=27.5, z=132.5)
        w = ""
        for _ in range(3):
            w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
            if "still waiting" not in w:
                break
        lk = call("look", full=True)
        m = re.search(r"(e\d+) Quartermaster Talli", lk)
        item(
            "travel_to indoors (Last Light Inn, to Talli)",
            "PASS" if m else "FAIL",
            f"{dt:.1f}s to start; {first_line(tr)} | {first_line(w)}; Talli in view={bool(m)}",
        )
    if not need(bool(m), "trade names + sell junk", "Talli not in view in " + TRADE_SAVE):
        return
    out = call("talk_to", npc=m.group(1))
    opt = re.search(r"^\s*(\d+)\. Let's see your wares", out, re.M)
    if not opt:
        g = call("get_dialogue")
        opt = re.search(r"^\s*(\d+)\. Let's see your wares", g, re.M)
    if not need(bool(opt), "trade names + sell junk", "no 'Let's see your wares' answer: " + first_line(out)):
        load_verified(TRADE_SAVE)
        return
    wares = call("choose_dialogue", option=int(opt.group(1)))
    if len(wares) < 400:  # choose_dialogue says only "screen=trade": list the wares
        wares = call("trade")
    nameless = re.findall(r"(?m)^\s*-?\s*(?:e\d+ )?price=\d+", wares)
    sup = grab(r"supply=\d+", wares)
    ok = not nameless and bool(sup) and len(wares) < 3500
    item(
        "trade wares named + compact list",
        "PASS" if ok else "FAIL",
        f"{len(wares)} chars, nameless rows={len(nameless)}, {sup or 'no supply=N'}; {first_line(wares)}",
    )
    junk = call("get_inventory", filter="junk")
    g0 = status().get("gold")
    out, dt = took("trade", sell="junk")
    g1 = status().get("gold")
    ok = out.startswith("trade: done") and (
        (isinstance(g0, int) and isinstance(g1, int) and g1 > g0) or "nothing" in out.lower()
    )
    tail = " ".join(
        re.findall(r"(?m)^\((?:sold|\d+ barters)[^\n]*", out)
    )  # "(3 barters of up to 25 items)" "(sold N …)"
    item("trade sell=junk (one call)", "PASS" if ok else "FAIL", f"{dt:.1f}s; gold {g0}→{g1}; {first_line(out)} {tail}")
    # [act] S067 #125 / S068 #168: the window stayed up after trade(request, confirm=true) and the agent pressed
    # ESCAPE by hand: a confirmed deal closes it now and says so. Buys the cheapest food ware (reloaded after).
    # A15 (L7 22:08): the picks come from the WARES block only (the first listing had no ware ids, and the old regex
    # took e100/e171 from YOUR ITEMS: "isn't on this side"); sell=junk's result is the newer listing
    shop = wares_block(out if "WARES" in out else wares)
    food = sorted(((int(p), e) for e, p in re.findall(r"(e\d+) [^|\n]*? (\d+)g supply=\d+", shop)), key=lambda x: x[0])
    if food:
        out, dt = took("trade", request=food[0][1], confirm=True)
        scr = ui_screen()
        ok = "closed the trade window: back in the game" in out and scr == "game"
        item(
            "trade confirm closes the window",
            "PASS" if ok else "FAIL",
            f"{dt:.1f}s; screen={scr}; {first_line(out)} | "
            + " ".join(re.findall(r"(?m)^\((?:closed|closing) the trade window[^\n]*", out)),
        )
    else:
        item("trade confirm closes the window", "SKIP", "no food ware in the listing: " + first_line(wares))
    # [act] A13 S069 20:37:23: request 2 wares (one a stack) + confirm went to the barter before the Balance's gold
    # was on our side: refused, ESCAPE + reopen. confirm now waits for an even table; the TABLE counts gold once.
    priced = [(int(p), e, " x" in t) for e, t, p in re.findall(r"(e\d+) ([^|\n]*?) (\d+)g", shop)]
    stack = min((x for x in priced if x[2]), default=None)
    single = min((x for x in priced if not x[2] and (not stack or x[1] != stack[1])), default=None)
    if stack and single and m:
        if ui_screen() != "trade":
            call("trade", npc=m.group(1))
        out, dt = took("trade", request=f"{stack[1]},{single[1]}", confirm=True)
        ok = out.startswith("trade: done") and "refused" not in out and "(no id)" not in out
        item(
            "trade confirm: 2 wares incl. a stack, even table first (A13)",
            "PASS" if ok else "FAIL",
            f"{dt:.1f}s; {stack[1]} {stack[0]}g + {single[1]} {single[0]}g; {first_line(out)} | "
            + grab(r"^TABLE[^\n]*", out),
        )
    else:
        item("trade confirm: 2 wares incl. a stack, even table first (A13)", "SKIP", "no stack + single ware listed")
    esc = call("press_key", key="ESCAPE") if ui_screen() == "trade" else "(closed already)"
    # [se] S057 07:40:25: camp() right after closing a trade failed "cannot do that during a conversation" ×2 (the
    # trader's goodbye line); camp now waits ≤ 6 s for a line with no dialogue window, then goes. Reloaded after.
    out, dt = took("camp")
    if "during a conversation" in out or "camp: done" in out or "at_camp" in out:
        ok = "during a conversation" not in out and "FAILED" not in out
        item(
            "camp right after closing a trade (goodbye line)",
            "PASS" if ok else "FAIL",
            f"{first_line(out)[:200]} ({dt:.1f}s; ESCAPE before: {first_line(esc)[:60]})",
        )
    else:
        item(
            "camp right after closing a trade (goodbye line)",
            "SKIP",
            f"camp blocked otherwise: {first_line(out)[:200]}",
        )
    load_verified(TRADE_SAVE)
