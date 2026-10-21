"""Section act: prepared spells, the spellbook, point scrolls and the helm ladders."""

from __future__ import annotations

import json
import math
import os
import re
import time

from .core import (
    BASE,
    HOTBAR_SAVE,
    SCROLL_SAVE,
    S,
    call,
    first_line,
    item,
    load_verified,
    log,
    lua_server,
    need,
    party_pos,
    status,
    took,
)
from .hotbar import floor_point, spell_state

PREPARED_ITEM = "unprepared spell refused + hidden (A4 fairness)"


def prepared_pick(states: dict) -> tuple | None:
    """(member, an unprepared spell, a prepared one or None) from {member: spell_state(member, '')}: the first
    member whose book has a spell that needs preparing and isn't prepared (a cleric / paladin / wizard)."""
    for u, stt in states.items():
        book = stt.get("book") or []
        off = [b["id"] for b in book if b.get("unprepared")]
        if off:
            on = [b["id"] for b in book if b.get("prep") == "RequiresPreparation" and b.get("prepared")]
            return u, off[0], (on[0] if on else None)
    return None


def prepared_guard_probe() -> None:
    """A4 (2026-10-17): Guard.known_spell's prepared check never ran (p.Prototype on a SpellMetaId raises in
    bg3se), so the Osiris route cast any class spell and state.actions listed the whole class list. Read-only:
    PASS = an unprepared spell is refused 'not prepared' and missing from state.actions, a prepared one passes."""
    g = "local G=Mods.BG3AI.BG3AI.Guard "
    states = {r.get("uuid"): spell_state(r.get("uuid"), "") for r in status().get("party") or [] if r.get("uuid")}
    bad = {S.H.get(u): s["why"] for u, s in states.items() if s.get("why")}
    pick = prepared_pick(states)
    books = ", ".join(f"{S.H.get(u)} {len(s.get('book') or [])}" for u, s in states.items())
    if not need(
        pick is not None,
        PREPARED_ITEM,
        f"no member has an unprepared spell (unread: {bad or 'none'}; book entries: {books})",
    ):
        return
    u, off, on = pick
    res = {}
    for spell in [s for s in (off, on) if s]:
        res[spell] = lua_server(
            g + f'local ok,e=pcall(G.known_spell,"{u}","{spell}") '
            'return ok and "ok" or (G.is_refusal(e) and e.reason or tostring(e))'
        )
    v_off = res[off]
    v_on = res[on] if on else "ok (no prepared one to try)"
    listed = lua_server(
        f'local n=0 for _, a in ipairs(Mods.BG3AI.BG3AI.State.actions("{u}")) do '
        f'if a.spell == "{off}" then n = n + 1 end end return tostring(n)'
    )
    ok = "not prepared" in v_off and v_on.startswith("ok") and listed == "0"
    item(
        PREPARED_ITEM,
        "PASS" if ok else "FAIL",
        f"{S.H.get(u)}: {off} -> {v_off[:80]}; listed in state.actions x{listed}; prepared {on} -> {v_on[:60]}",
    )


SPELLBOOK_PROBE_ITEM = "spellbook probe (A5, read-only)"
PREPARE_ITEM = "prepare_spells Bless on Claude: prepared, then unprepared; hotbar slot reported (A5, A15)"
PREPARE_REFUSE_ITEM = "prepare_spells honest refusals (A5)"
PREPARE_LIMIT_ITEM = "prepare_spells limit = the page's (A7, read-only)"
SPELLBOOK_ICON_ITEM = "spellbook cells by icon sheet place (A8, read-only)"
PREPARE_WHO = "Claude"  # the Paladin in HOTBAR_SAVE; Bless is on his class list, unprepared at S065-end (A4)


def _member(name: str) -> str | None:
    return next((r.get("uuid") for r in status().get("party") or [] if r.get("name") == name), None)


def spellbook_probe() -> None:
    """A5: the spellbook page as bg3le reads it, read-only (Client/Spellbook.lua GUESSES S1-S4): K opens the page,
    the selected character's preparing book has cells (A7: one look line each; A8: `icon=atlas@u,v`),
    PreparedSpellsStat reads, and the widget's DataContext has ToggleCharacterAction (CanExecute per cell). The page
    is closed again; nothing is toggled. PASS = a preparing book with cells and a max, and the command present.
    A7 also: PREPARE_LIMIT_ITEM = the server's limit (act.prepared max: SpellBookPrepares.ClassPreparedSpellCount,
    GUESS P1) equals the page's PreparedSpellsStat.Max (live L4: 12; Stats.MaxPreparedSpells read 9).
    A8: SPELLBOOK_ICON_ITEM = every spell that needs preparing finds its cell(s) by its stats Icon's place on the icon
    sheet (Ext.StaticData.GetIconUVs/GetIconAtlas, GUESS S5) in its own state: prepared -> on, unprepared -> off."""
    u = _member(PREPARE_WHO)
    if not need(u is not None, SPELLBOOK_PROBE_ITEM, f"no {PREPARE_WHO} in the party"):
        return
    # A8: the stats Icon of each spell that needs preparing, by state (Server/Guard.lua prep_info, the plan's look)
    raw_icons = lua_server(
        f"local G=Mods.BG3AI.BG3AI.Guard local i=G.prep_info(G.guid('{u}')) local o={{}} "
        "for _,k in ipairs({'prepared','unprepared'}) do for _,r in ipairs(i[k]) do "
        "o[#o+1]=k:sub(1,1)..'|'..r.name..'|'..tostring(r.look and r.look.icon) end end "
        "return table.concat(o,';')"
    )
    spells = [x.split("|") for x in raw_icons.split(";") if x.count("|") == 2]
    S.game.run_job("act.select", {"char": u}, wait_s=5)
    time.sleep(0.5)
    r = S.game.run_job(
        "ui.prepare_spells",
        {"name": PREPARE_WHO, "probe": True, "icons": sorted({ic for _, _, ic in spells if ic != "nil"})},
        context="client",
        wait_s=15,
    )
    res = r.result if isinstance(r.result, dict) else {}
    p = res.get("probe") or {}
    icons = p.get("icons") or {}
    log("PROBE spellbook icons: " + json.dumps(icons)[:4000])
    # each icon's cells, by state, = the spells with that icon in that state (shared icons count together)
    want: dict = {}
    for k, _, ic in spells:
        on, off = want.get(ic, (0, 0))
        want[ic] = (on + (k == "p"), off + (k == "u"))
    bad = [
        f"{name}({'on' if k == 'p' else 'off'}): cells on/off {icons.get(ic, {}).get('on')}/"
        f"{icons.get(ic, {}).get('off')} want {want[ic][0]}/{want[ic][1]}"
        for k, name, ic in spells
        if (icons.get(ic, {}).get("on"), icons.get(ic, {}).get("off")) != want[ic]
    ]
    bless = icons.get("Spell_Enchantment_Bless") or {}
    item(
        SPELLBOOK_ICON_ITEM,
        "PASS" if spells and not bad else "FAIL",
        (
            f"{len(spells)} spells, {len(spells) - len(bad)} matched by icon; "
            + ("; ".join(bad[:6]) if bad else "all")
            + f"; Bless uv={bless.get('uv')} raw={bless.get('raw')}"
        )[:400],
    )
    log(
        "PROBE spellbook: "
        + json.dumps(
            {"status": r.status, "error": r.error, "opened": res.get("opened"), "closed": res.get("closed"), "probe": p}
        )[:6000]
    )
    books = p.get("preparing") or []
    ok = r.status == "done" and bool(p.get("toggle_cmd")) and any(b.get("cells") and b.get("max") for b in books)
    item(
        SPELLBOOK_PROBE_ITEM,
        "PASS" if ok else "FAIL",
        f"{r.status} {r.error or ''}; shows={p.get('selected')} metadata={p.get('metadata')} dc={p.get('dc')} "
        f"toggle_cmd={p.get('toggle_cmd')}; preparing="
        + "; ".join(
            f"{b.get('class')} {b.get('value')}/{b.get('max')} {len(b.get('cells') or [])} cells" for b in books
        )
        + " (full: acceptance.log PROBE spellbook)",
    )
    for b in books:
        log(f"PROBE spellbook cells {b.get('class')}: " + " | ".join(b.get("cells") or []))
    rows = (S.game.try_rpc("act.prepared", {"uuids": [u]}) or {}).get("casters") or []
    row = rows[0] if rows else {}
    raw = lua_server(
        f"local p=Ext.Entity.Get('{u}').SpellBookPrepares local o={{}} "
        "for k,v in pairs(p.ClassPreparedSpellCount) do o[#o+1]=tostring(k):sub(1,8)..'='..tostring(v) end "
        "local f={} for k,v in pairs(p.ClassFallbackPreparedSpellCount) do "
        "f[#f+1]=tostring(k):sub(1,8)..'='..tostring(v) end "
        "return 'count{'..table.concat(o,',')..'} fallback{'..table.concat(f,',')..'}'"
    )
    page = next((b.get("max") for b in books if b.get("max")), None)
    ok_l = page is not None and row.get("max") is not None and float(row["max"]) == float(page)
    item(
        PREPARE_LIMIT_ITEM,
        "PASS" if ok_l else "FAIL",
        f"page {page}; server max={row.get('max')} (class_max={row.get('class_max')}, "
        f"stats_max={row.get('stats_max')}, count={row.get('count')}); SpellBookPrepares {raw[:160]}",
    )


def prepare_spells_refusals() -> None:
    """A5: a spell not in the book is refused by name, and a member that doesn't prepare (no act.prepared row: a
    barbarian, warlock, ...) is refused with the level-up hint; neither opens the spellbook."""
    if not need(_member(PREPARE_WHO) is not None, PREPARE_REFUSE_ITEM, f"no {PREPARE_WHO} in the party"):
        return
    out1 = call("prepare_spells", who=PREPARE_WHO, add=["Fireball"])
    preparing = {c.get("uuid") for c in (S.game.try_rpc("act.prepared", {}) or {}).get("casters") or []}
    other = next((r.get("name") for r in status().get("party") or [] if r.get("uuid") not in preparing), None)
    out2 = call("prepare_spells", who=other, add=["Bless"]) if other else ""
    ok1 = f"{PREPARE_WHO} doesn't know Fireball" in out1
    ok2 = (not other) or "doesn't prepare spells" in out2
    item(
        PREPARE_REFUSE_ITEM,
        "PASS" if ok1 and ok2 else "FAIL",
        f"Fireball: {first_line(out1)[:120]} | {other or '(every member prepares)'}: {first_line(out2)[:120]}",
    )


def _bless_state(u: str) -> tuple[bool, bool, dict]:
    """(Bless prepared on the server, Bless on the server hotbar map, the act.prepared row). The map is read fresh
    (A15: L7 read one cached before the prepare)."""
    from bg3ai import hotbar as HB

    rows = (S.game.try_rpc("act.prepared", {"uuids": [u]}) or {}).get("casters") or []
    row = rows[0] if rows else {}
    prepared = any(r.get("id") == "Target_Bless" for r in row.get("prepared") or [])
    HB.forget_map(u)
    return prepared, HB.map_has(HB.server_map(S.game, u), "Target_Bless"), row


def _bless_slot(u: str) -> str:
    """Where the player sees Bless: the HUD deck cell (ui.hotbar_slot), or that no deck has it."""
    from bg3ai import hotbar as HB
    from bg3ai.game import GameError

    try:
        r = HB._slot(S.game, "Target_Bless", u)
    except GameError as e:
        return f"slot read failed: {e}"
    return f"{r.get('deck')} #{r.get('index')}" if r.get("found") else "on no deck"


def _wait_bless(u: str, want: bool, s: float = 3.0) -> tuple[bool, bool, dict]:
    """Until Bless is prepared == want and its hotbar presence matches too (an unslotted spell waits the whole s)."""
    end = time.monotonic() + s
    while True:
        st = _bless_state(u)
        if (st[0] == want and st[1] == want) or time.monotonic() > end:
            return st
        time.sleep(0.3)


def prepare_bless_round_trip() -> None:
    """A5: prepare_spells(Claude, add Bless) through the spellbook page -> Bless prepared on the server; then remove
    it again -> not prepared. A full book swaps out his last prepared spell for the test and puts it back.
    HOTBAR_SAVE is never saved over. A15: PASS does not assume the game slots a newly prepared spell (L7: no hotbar
    cell after the prepare, from a stale map read); where it went is reported: the server map (fresh) and the HUD
    deck after the prepare, and whether the spell swapped out left the map."""
    u = _member(PREPARE_WHO)
    if not need(u is not None, PREPARE_ITEM, f"no {PREPARE_WHO} in the party"):
        return
    was, on_map, row = _bless_state(u)
    if not need(bool(row), PREPARE_ITEM, f"{PREPARE_WHO} has no act.prepared row (no preparing class read)"):
        return
    log(
        f"PROBE prepare before: Bless prepared={was} on_map={on_map}; {row.get('count')}/{row.get('max')} "
        + ", ".join(r.get("name", "?") for r in row.get("prepared") or [])
    )
    swap = []
    if not was and row.get("max") is not None and row.get("count", 0) >= row["max"] and row.get("prepared"):
        swap = [row["prepared"][-1]["name"]]
    first = {"remove": ["Bless"], "add": []} if was else {"add": ["Bless"], "remove": swap}
    out1, dt1 = took("prepare_spells", who=PREPARE_WHO, **first)
    st1 = _wait_bless(u, not was)
    slot1 = _bless_slot(u)
    from bg3ai import hotbar as HB

    swap_id = next((r.get("id") for r in row.get("prepared") or [] if swap and r.get("name") == swap[0]), None)
    swap_left = (not HB.map_has(HB.server_map(S.game, u), swap_id)) if swap_id else None
    second = {"add": ["Bless"], "remove": []} if was else {"remove": ["Bless"], "add": swap}
    out2, dt2 = took("prepare_spells", who=PREPARE_WHO, **second)
    st2 = _wait_bless(u, was)
    ok = (
        first_line(out1).startswith("prepare_spells: done")
        and st1[0] == (not was)
        and first_line(out2).startswith("prepare_spells: done")
        and st2[0] == was
    )
    item(
        PREPARE_ITEM,
        "PASS" if ok else "FAIL",
        f"1) {first_line(out1)[:110]} ({dt1:.1f}s) -> prepared={st1[0]} on_map={st1[1]} hud={slot1}; "
        f"2) {first_line(out2)[:110]} ({dt2:.1f}s) -> prepared={st2[0]} on_map={st2[1]}"
        + (f"; swapped out {swap[0]} and back ({swap_id} left the map: {swap_left})" if swap else ""),
    )


BLOCKING_STATUS_ITEM = "cast guard reads Stunned/Fearful status types (A9, read-only)"
POINT_SCROLL_ITEM = "point scroll aimed at the ground: Misty Step 6 m (A9)"


def point_scroll_probe() -> None:
    """A9 (S066 #66 Cone of Cold, #143 Misty Step: "cast on <the reader> by a click", the x,y,z dropped): out of combat
    in SCROLL_SAVE, Karlach reads her Scroll of Misty Step at a floor point 6 m away on screen (bridge
    twostep.ground_click: the cursor checked by ui.ground_check, then the click). PASS = the result says "aimed at
    the ground" and "moved", and she stands within 1.5 m of the point. Spends the scroll in memory only (not saved)."""
    if not load_verified(SCROLL_SAVE, record=True):
        item(POINT_SCROLL_ITEM, "SKIP", f"{SCROLL_SAVE} did not load")
        return
    inv = S.game.try_rpc("state.inventory", {}) or {}
    it = next(
        (
            x
            for x in inv.get("items") or []
            if isinstance(x, dict) and str(x.get("name") or "").lower() == "scroll of misty step"
        ),
        None,
    )
    if not need(it is not None, POINT_SCROLL_ITEM, f"no Scroll of Misty Step carried in {SCROLL_SAVE}"):
        return
    if not need(not (S.game.try_rpc("state.combat") or {}).get("active"), POINT_SCROLL_ITEM, "in combat"):
        return
    u = it["owner"]
    p0 = party_pos().get(u)
    land = floor_point(p0, 6) if p0 else None
    if not need(land is not None, POINT_SCROLL_ITEM, "no floor point 6 m away on screen around the reader"):
        return
    out, dt = took("use_ability", ability="Scroll of Misty Step", x=land[0], y=land[1], z=land[2], who=S.H.get(u))
    time.sleep(1.0)
    p1 = party_pos().get(u) or p0
    off = math.dist([p1[0], p1[2]], [land[0], land[2]])
    head = first_line(out)
    # A17: L8 said "nothing cast" while Karlach moved 5.1 m: is the scroll still carried?
    inv2 = S.game.try_rpc("state.inventory", {}) or {}
    left = any(isinstance(x, dict) and x.get("uuid") == it.get("uuid") for x in inv2.get("items") or [])
    ok = "aimed at the ground" in head and " moved " in head and off <= 1.5 and not left
    item(
        POINT_SCROLL_ITEM,
        "PASS" if ok else "FAIL",
        f"{head[:420]} ({dt:.1f}s); {off:.1f} m from the point {land}, moved {math.dist(p0, p1):.1f} m; "
        f"scroll still carried={left}",
    )


def sec_act() -> None:
    """act overnight batch (2026-10-10/11): the hot-reloaded Lua is live (jump FELL report, CastSpellFailed reason,
    missing ranged weapon named, one-decimal range refusal), react by name, swap_party's camp block answer,
    Revivify's two-step spot."""
    if not load_verified(BASE):
        return
    g = "local G=Mods.BG3AI.BG3AI.Guard local A=Mods.BG3AI.BG3AI.Actions "
    # jump fall/death report + CastSpellFailed reason (S038 20:44:25 fall "done"; S024-S041 "the action failed")
    v = lua_server(
        g + 'return tostring(A.JUMP_FALL_M).."|"..tostring(A.cast_failed_why and '
        "A.cast_failed_why(Osi.GetHostCharacter(), nil))"
    )
    item(
        "act Lua live: jump FELL + cast refused reason",
        "PASS" if v.startswith("4") and "the game refused the cast" in v else "FAIL",
        v[:200],
    )
    # Guard.within: one decimal when the distance rounds to the max (S028 "40m …, max 40m"). G.pos is wrapped for
    # the call only, so a synthetic 40.3 m target needs no walk.
    v = lua_server(
        g + "local h=Osi.GetHostCharacter() local real=G.pos "
        'G.pos=function(u) local p=real(h) if u=="__far" then return {p[1]+40.3,p[2],p[3]} end return real(u) end '
        'local ok,e=pcall(G.within,h,"__far",40,"target") G.pos=real '
        'return ok and "no refusal" or (G.is_refusal(e) and e.reason or tostring(e))'
    )
    item(
        "range refusal with one decimal at the max",
        "PASS" if "(40.3m from this character, max 40m)" in v else "FAIL",
        v[:200],
    )
    # a missing Ranged Attack names the missing ranged weapon (S031 17:04 Gale)
    rows = []
    for r in status().get("party") or []:
        u = r.get("uuid")
        v = lua_server(
            g + f'local ok,e=pcall(G.known_spell,"{u}","Projectile_MainHandAttack") '
            'return ok and "has it" or (G.is_refusal(e) and e.reason or tostring(e))'
        )
        rows.append((r.get("name"), v))
    missing = [(n, v) for n, v in rows if v != "has it"]
    if not missing:
        item(
            "missing ranged weapon named", "SKIP", "every member has a ranged weapon: " + ", ".join(n for n, _ in rows)
        )
    else:
        item(
            "missing ranged weapon named",
            "PASS" if all("no ranged weapon equipped" in v for _, v in missing) else "FAIL",
            "; ".join(f"{n}: {v[:90]}" for n, v in missing),
        )
    prepared_guard_probe()
    # react by name with no prompt: answers at once, no Lua error (S034 19:03:14 react(which="Shield"))
    out, dt = took("react", use=True, which="Shield")
    item(
        "react by name",
        "PASS" if "no reaction prompt on screen" in out and dt < 3 else "FAIL",
        f"{first_line(out)} ({dt:.1f}s)",
    )
    # a name by one whole word ("Aylin" for "Dame Aylin", S045 00:15) resolves
    nb = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters"]}) or {}
    two = [c for c in nb.get("characters") or [] if len(str(c.get("name") or "").split()) >= 2]
    if not two:
        item("name by a whole word", "SKIP", "no character with a two-word name in view")
    else:
        c = two[0]
        word = str(c["name"]).split()[-1].strip("'")
        out = call("look_at", target=word)
        item(
            "name by a whole word",
            "PASS" if "unknown target" not in out else "FAIL",
            f"look_at({word!r}) for {c['name']!r}: {first_line(out)[:160]}",
        )
    # ACT-9: the TargetPos2 engine write (unproven struct mapping, HANDOFF §7) is OFF by default, and a two-step
    # spell is refused before any order. The probe below writes it, so it runs only when asked for
    # (BG3AI_PROBE_TASK_POS2=1, an attended window: watch for a crash in the next minutes).
    v = lua_server(g + 'if A.TASK_POS2==nil then return "no A.TASK_POS2" end return tostring(A.TASK_POS2)')
    item("act-9 second-pick write off by default", "PASS" if v == "false" else "FAIL", v[:200])
    if os.environ.get("BG3AI_PROBE_TASK_POS2", "0") in ("", "0"):
        item(
            "revivify two-step spot set",
            "SKIP",
            "the TargetPos2 write is off (ACT-9); BG3AI_PROBE_TASK_POS2=1 probes it",
        )
    else:
        # Revivify is two-step (S057/S058 0 of 14): the live stats say so, and the queued UseSpell task takes the
        # ground spot (TargetPos2). Probe on a living companion: the task is queued, given its spot, then purged.
        v = lua_server(
            g + "local h=Osi.GetHostCharacter() local o "
            "for _,r in pairs(Osi.DB_PartyMembers:Get(nil)) do if not G.same(r[1],h) then o=G.guid(r[1]) break end end "
            'if not A.two_step_spell then return "no A.two_step_spell" end '
            'if not A.two_step_spell("Teleportation_Revivify") then return "stats: not two-step" end '
            'if not o then return "no companion" end '
            'Osi.UseSpell(h,"Teleportation_Revivify",o,"NULL_00000000-0000-0000-0000-000000000000",1) '
            'A.TASK_POS2=true local ok,why=A.set_task_pos2(h,"Teleportation_Revivify",A.place_spot(h,o)) '
            'A.TASK_POS2=false Osi.PurgeOsirisQueue(h) return ok and "set" or ("not set: "..tostring(why))'
        )
        item("revivify two-step spot set", "PASS" if v == "set" else "FAIL", v[:200])
    # Dimension Door is a two-step teleport (S059 x3 refused): who goes (the caster / an ally within 2 m), then the
    # landing (AreaRadius). Live stats agree, and a landing in the air above the host is refused with a reason.
    v = lua_server(
        g + 'local h=Osi.GetHostCharacter() local sp="Teleportation_DimensionDoor" '
        'if not A.landing_ok then return "no A.landing_ok" end '
        'if not A.two_step_spell(sp) then return "stats: not two-step" end '
        'if A.revive_spell(sp) then return "stats: read as a revive" end '
        "local r=A.landing_range(sp) local p=G.pos(h) "
        "local ok,e=pcall(A.landing_ok,h,sp,{p[1],p[2]+30,p[3]}) "
        'return "range="..tostring(r).." air="..(ok and "accepted" or (G.is_refusal(e) and e.reason or tostring(e)))'
    )
    item(
        "dimension door two-step landing",
        "PASS" if v.startswith("range=") and "air=accepted" not in v and "range=nil" not in v else "FAIL",
        v[:200],
    )
    # audit ACT-11/13/15 (offline-fixed 2026-10-16): the combat no-start timeout, G.q queries only (an action name
    # through it is refused, never run), CharacterMoveTo blocked, approval seeded per companion after the load.
    v = lua_server(
        g + "local E=Mods.BG3AI.BG3AI.Events local h=Osi.GetHostCharacter() "
        'local seeded=0 for k,_ in pairs(E.approval or {}) do if tostring(k):find("|",1,true) then seeded=seeded+1 end end '
        'return tostring(A.CAST_NO_START_COMBAT_MS).."|"..tostring(G.query_ok and G.query_ok("IsLocked")).."|"'
        '..tostring(G.query_ok and G.query_ok("Unlock")).."|"..(G.query_ok and tostring(G.q("Unlock",h,h)) or "old mod: not called").."|"'
        '..tostring(G.ALLOW.CharacterMoveTo).."|"..tostring(G.BLOCK.CharacterMoveTo).."|"..tostring(seeded)'
    )
    parts = v.split("|")
    ok = len(parts) == 7 and parts[:6] == ["3000", "true", "false", "nil", "nil", "true"]
    item(
        "act audit ACT-11/13/15 live state",
        "PASS" if ok and parts[6] != "0" else ("FAIL" if not ok else "SKIP"),
        f"no_start_combat|query_ok(IsLocked)|query_ok(Unlock)|q(Unlock)|ALLOW.MoveTo|BLOCK.MoveTo|approval seeded: {v[:160]}"
        + ("" if not ok or parts[6] != "0" else " (no companion rating seeded: solo party?)"),
    )
    # act combat target gone (BACKLOG ~937, offline 2026-10-16; audit C9 2026-10-18): a character out of sight reads
    # only visible=false (no dead/off_stage why); the reloaded mod has A.left_fight; the ladder flag is on.
    ts = S.game.try_rpc("act.target_state", {"uuid": "c0ffee00-0000-4000-8000-000000000bad"}) or {}
    v = lua_server(g + 'return tostring(A.LADDER_WATCH_AT_USE).."|"..tostring(A.left_fight ~= nil)')
    item(
        "act target gone + ladder watch flag live",
        "PASS" if ts == {"visible": False} and v == "true|true" else "FAIL",
        f"target_state(unknown uuid)={ts}; LADDER_WATCH_AT_USE|left_fight: {v[:80]}",
    )
    # A9 (S066 #100/#130, S067 #32/#43/#54): the cast guard reads the caster's status types (Stunned refused, a dropped
    # order while Fearful names it). Read-only: the stats types the check keys on, and the check on the host now.
    v = lua_server(
        g + 'if not G.blocking_status then return "no G.blocking_status" end '
        "local b=G.blocking_status(Osi.GetHostCharacter()) "
        'return tostring(Ext.Stats.Get("FEARED").StatusType).."|"..tostring(Ext.Stats.Get("STUNNED").StatusType)'
        '.."|"..tostring(b and b.name)'
    )
    item(
        BLOCKING_STATUS_ITEM,
        "PASS" if v.startswith("FEAR|INCAPACITATED|") else "FAIL",
        f"FEARED|STUNNED StatusType|host's blocking status: {v[:160]}",
    )
    # latency round (2026-10-16): a shared-group end is reported after 0.4 s (Lua A.END_REQUEST_AFTER_MS, was 1.5 s);
    # dialogue_advance with no conversation anywhere returns in ~0.5 s (was 2.5 s: naut "0 filler line(s)" 2.7 s).
    v = lua_server(g + "return tostring(A.END_REQUEST_AFTER_MS)")
    out, dt = took("dialogue_advance")
    ok = v == "400" and "conversation ended" in out and dt < 1.5
    item(
        "act latency: end hold 0.4 s + dialogue_advance idle exit",
        "PASS" if ok else "FAIL",
        f"END_REQUEST_AFTER_MS={v}; dialogue_advance {dt:.1f}s: {first_line(out)[:80]}; bridge END_ASYNC={S.END_ASYNC} "
        f"TURN_POLL_S={S.TURN_POLL_S} DIALOGUE_IDLE_OPEN_S={S.DIALOGUE_IDLE_OPEN_S}",
    )
    # swap_party where the camp button is greyed by the area: the reason at once (S039 21:48:34)
    b = S.game.try_rpc("ui.travel_block", context="client") or {}
    if b.get("error") != "Location":
        item("swap_party camp block answers at once", "SKIP", f"camp travel allowed here (travel_block={b})")
    else:
        camp = [c.get("name") for c in status().get("camp") or []]
        party = [r.get("name") for r in status().get("party") or [] if not r.get("avatar")]
        out, dt = took("swap_party", into=camp[0] if camp else "Gale", out=party[-1] if party else "")
        item(
            "swap_party camp block answers at once",
            "PASS" if "(Location)" in out and dt < 3 else "FAIL",
            f"{first_line(out)[:200]} ({dt:.1f}s)",
        )
    # A5 prepare_spells on HOTBAR_SAVE (Claude the Paladin, L10): the read-only spellbook probe, then Bless prepared
    # and unprepared again (nothing is saved; the next section loads its own save)
    if not load_verified(HOTBAR_SAVE, record=True):
        for name in (SPELLBOOK_PROBE_ITEM, SPELLBOOK_ICON_ITEM, PREPARE_LIMIT_ITEM, PREPARE_ITEM, PREPARE_REFUSE_ITEM):
            item(name, "SKIP", f"{HOTBAR_SAVE} did not load")
    else:
        spellbook_probe()
        prepare_spells_refusals()
        prepare_bless_round_trip()
    # A6 helm ladders (live L5: it sat at the end of sec_vis, so --only act never ran it); loads its own save
    helm_ladder_probe()
    # A9 (S066 #66/#143): a point scroll is aimed at the ground at its x,y,z; loads its own save
    point_scroll_probe()


# A6 #5 (live L3 helm probe 2026-10-17, research/impl-se.md): naut3-r3's character stopped at the helm fire, where every
# naut3 run's [interact e39, interact e40] climbed e39 on the approach walk and then back down with the Use. The save
# is another character's ("Marek Dunhallow-"), so the prefix is switched for its load only.
HELM_SAVE, HELM_PREFIX = "ACC-naut-helm", "Marek Dunhallow-"
HELM_E39 = "a8e40469-0efa-4077-b125-47058511202e"  # Arterial Mesh ladder, foot y 4.53, climbs 2.9 m up
HELM_E40 = "73876474-0791-42e6-87ea-bf16db317afe"  # Arterial Mesh ladder from e39's top to the helm deck (y 18)
HELM_ITEM = "helm ladders: approach on the foot's floor, both climb up (A6, naut3 helm-hint/helm-once)"


def helm_ladder_probe() -> None:
    before = S.SAVE_PREFIX
    S.SAVE_PREFIX = HELM_PREFIX
    try:
        if not any(d.name.endswith("__" + HELM_SAVE) for d in S._my_saves()):
            item(HELM_ITEM, "SKIP", f"no {HELM_PREFIX}*__{HELM_SAVE} save folder")
            return
        if not load_verified(HELM_SAVE):
            return
        g = "local G=Mods.BG3AI.BG3AI.Guard local A=Mods.BG3AI.BG3AI.Actions local h=Osi.GetHostCharacter() "
        ap = lua_server(
            g + f'local ok,p=pcall(G.approach_point,h,"{HELM_E39}",A.USE_REACH) '
            'if not ok then return "refused: "..tostring(G.is_refusal(p) and p.reason or p) end '
            'if p.here then return "here" end return string.format("%.2f", p.pos[2])'
        )
        try:
            ap_ok = abs(float(ap) - 4.53) < 1.5
        except ValueError:
            ap_ok = False
        # handles, not uuids (live L5: do_actions said "unknown target e252" for the raw uuid; render.py takes handles)
        steps = [{"tool": "interact", "target": S.H.get(HELM_E39)}, {"tool": "interact", "target": S.H.get(HELM_E40)}]
        out, dt = took("do_actions", actions=json.dumps(steps))
        y = lua_server('local _,y,_=Osi.GetPosition(Osi.GetHostCharacter()) return string.format("%.2f", y)')
        climbs = re.findall(r"climbed=(\w+)", out)
        try:
            top = float(y) >= 17.0
        except ValueError:
            top = False
        item(
            HELM_ITEM,
            "PASS" if ap_ok and climbs[:2] == ["up", "up"] and top else "FAIL",
            f"approach y={ap} (foot 4.53); climbs={climbs} final y={y} in {dt:.1f}s; "
            + " | ".join(ln for ln in out.splitlines() if re.match(r"\d+\. ", ln))[:300],
        )
    finally:
        S.SAVE_PREFIX = before
