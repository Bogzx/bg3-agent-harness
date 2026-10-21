"""Section reads: read-only tools on the base save, and the inventory read cost (V12)."""

from __future__ import annotations

import json
import re

from .core import BASE, OUT, S, call, first_line, grab, item, load_verified, lua_server, need, status, took, ui_screen


def sec_reads() -> None:
    if not load_verified(BASE, record=True):
        return
    st_full = call("get_status", full=True)
    al = grab(r"^ALERTS.*$", st_full)
    item("ALERTS line", "PASS" if al else "FAIL", al or "no ALERTS line in get_status(full=true)")

    st = status()
    camp = [c.get("name") for c in st.get("camp") or []]
    party_line = grab(r"^PARTY:.*$", st_full)
    if party_line:
        item("PARTY hint", "PASS", party_line)
    else:
        mk = S.game.try_rpc("nav.markers") or {}
        forced = S._party_hint_line(st, (mk or {}).get("markers") if isinstance(mk, dict) else None, force=True)
        item("PARTY hint", "PASS" if forced or not camp else "WARN",
             f"forced line: {forced!r}; camp={camp}" if forced else f"no PARTY line (camp={camp}; nothing to suggest?)")

    lvl = grab(r"^LEVEL UP.*$", st_full)
    pend = [(r.get("name"), r.get("level")) for r in (st.get("party") or []) + (st.get("camp") or []) if r.get("level_up")]
    if lvl:
        item("camp level-up line", "PASS", lvl)
    elif pend:
        item("camp level-up line", "FAIL", f"level-ups pending {pend} but no LEVEL UP line")
    else:
        item("camp level-up line", "SKIP", "nobody has a level-up pending in this save")

    mp = call("get_map", full=True)
    ids = sorted(set(re.findall(r"\b(?:WLD_\w+|WAYP_\w+|[A-Z]{3}_[A-Za-z]+_[A-Za-z_]+)\b", mp)))
    wps = len(re.findall(r"(?im)waypoint", mp))
    item("get_map names only", "PASS" if not ids and len(mp) > 40 else "FAIL",
         f"ids={ids[:5]}; {len(mp)} chars; first: {first_line(mp)}")

    mem = OUT / "memory"
    (mem / "sessions").mkdir(parents=True, exist_ok=True)
    log_path = mem / "sessions" / "S999.md"
    log_path.write_text("# S999 acceptance scratch log\n")
    out, dt = took("note", text="acceptance check line one\n[quest] second line", tag="lesson")
    body = log_path.read_text()
    ok = "[lesson] acceptance check line one" in body and "[quest] second line" in body and dt < 0.5
    item("note()", "PASS" if ok else "FAIL", f"{first_line(out)} in {dt * 1000:.0f} ms; log tail: {body.strip().splitlines()[-1]}")

    scr = ui_screen()
    if need(scr == "game", "ESCAPE guard", f"screen is {scr}, not the plain game view"):
        out, dt = took("press_key", key="ESCAPE")
        after = ui_screen()
        ok = "refused" in out.lower() and after == "game"
        item("ESCAPE guard", "PASS" if ok else "FAIL", f"{first_line(out)} ({dt:.1f}s); screen after={after}")

    out, dt = took("feed_gale", item="Moonlantern")
    after = ui_screen()
    ok = ("FAILED" in out or "REFUSED" in out or "not open" in out.lower()) and after == "game" and dt < 20
    item("feed_gale dry run (no donation window)", "PASS" if ok else "FAIL",
         f"{first_line(out)} ({dt:.1f}s); screen after={after} (the window itself only opens from Gale's dialogue)")

    talk = grab(r"^TALK:.*$", st_full)
    item("TALK line ('!' companion)", "PASS" if talk else "SKIP", talk or "no companion has a '!' in this save")

    read_cost_probe()
    load_verified(BASE)


# V12: the full state.inventory rebuild (look/get_status/get_inventory; the read cache's 20 s TTL had expired at most
# reads in S066-S070) and act.prepared's prep_info (every get_status), with the stats memos emptied (the cost before
# V12) and then filled. Read-only: the memos are put back, the inventory cache rebuilds on the next read.
READ_COST_LUA = (
    "local B=Mods.BG3AI.BG3AI local S,G,V=B.State,B.Guard,B.Visibility local now=Ext.Utils.MonotonicTime "
    "local function inv() S.invCache=nil local t=now() local r=S.inventory(V.context(),{}) return now()-t,#r.items end "
    "local function prep() local t,n=now(),0 for _,u in ipairs(G.party()) do local g=G.guid(u) "
    "if g then n=n+#G.prep_info(g).prepared end end return now()-t,n end "
    "local keepS,keepG=S.statsFactsMemo,G.spellFacts S.statsFactsMemo,G.spellFacts={},{} "
    "local ic,items=inv() local pc,rows=prep() local iw=inv() local pw=prep() "
    "for k,v in pairs(S.statsFactsMemo) do keepS[k]=v end for k,v in pairs(G.spellFacts) do keepG[k]=v end "
    "S.statsFactsMemo,G.spellFacts=keepS,keepG "
    "return Ext.Json.Stringify({inv_cold=ic,inv_warm=iw,prep_cold=pc,prep_warm=pw,items=items,prepared=rows})"
)
READ_COST_ITEM = "read cost: stats memos cut the inventory + prepared reads (V12)"


def read_cost_probe() -> None:
    """PASS when the filled memos take each read to <= 60 % of its emptied-memo time (or under 15 ms)."""
    try:
        r = json.loads(lua_server(READ_COST_LUA))
    except Exception as e:  # noqa: BLE001 - a probe error is a result
        item(READ_COST_ITEM, "FAIL", f"probe error: {e}")
        return
    ok = all(r[w] <= max(0.6 * r[c], 15) for c, w in (("inv_cold", "inv_warm"), ("prep_cold", "prep_warm")))
    item(READ_COST_ITEM, "PASS" if ok else "FAIL",
         f"state.inventory rebuild {r['inv_cold']:.0f} -> {r['inv_warm']:.0f} ms ({r['items']} items); "
         f"prep_info (party) {r['prep_cold']:.0f} -> {r['prep_warm']:.0f} ms ({r['prepared']} prepared rows); "
         "emptied memo -> filled")
