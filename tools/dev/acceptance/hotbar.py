"""Hotbar probes for se and act: the widget tree, Bless on 3 allies (ACT-8), Dimension Door (ACT-9)."""

from __future__ import annotations

import json
import math
import time

from .core import RESULTS, S, item, log, lua_server, need, party_pos, server_map_line, status, took, ui_screen


def ui3_refused(msg: str) -> bool:
    """A mod UI-3 refusal (Client/UIActions.lua SAVELOAD_HINT): the whole message names save_game / load_game."""
    return "refused" in msg and "save_game / load_game" in msg


def hb_slot(spell: str, char: str | None) -> dict:
    """ui.hotbar_slot as the cast route reads it (bridge hotbar._slot: the server's hotbar map when the client has no
    spell ids, live window #1); {} on an error."""
    from bg3ai import hotbar as HB
    try:
        return HB._slot(S.game, spell, char) or {}
    except Exception:  # noqa: BLE001
        return {}


def close_targeting() -> str:
    """Leave a targeting mode (or an accidental game menu) with ESCAPE, by ui.key: the guarded press_key refuses it."""
    for _ in range(3):
        S.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
        time.sleep(0.5)
        if ui_screen() != "game_menu":
            break
    return ui_screen()


def hotbar_probe() -> None:
    """Audit ACT-8 rest (se, 2026-10-16): the hotbar cast route (bridge hotbar.py + Client/Hotbar.lua), built offline
    from HotBar.xaml with guesses G1-G10 (Hotbar.lua / hotbar.py headers). 1) read-only: the widget tree as the mod
    sees it (decks, a slot VM's spell id, a slot button's DataContext), ui.hotbar_slot for Main Hand Attack, and the
    cursor search for its cell (moves only, no click). 2) one Bless on 3 party members out of combat through the
    hotbar (spends a level-1 slot; sec_se reloads the base save after). PASS = the cast went off on 3 (BLESS on each)
    and nothing is left in hand. Full reads in se.log (PROBE hotbar lines)."""
    from bg3ai import hotbar as HB
    st = status()
    sel = st.get("active")
    try:
        tree = S.game.eval(
            "local UI = Mods.BG3AI.BG3AI.UI local dc = UI.dc('HotBar') if not dc then return 'no HotBar dc' end "
            "local bars = UI.path(dc, 'CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars') "
            "local out = {'decks=' .. tostring(UI.count(bars))} "
            "for i, b in UI.items(bars, 8) do local sl = UI.get(b, 'SlotList') local s1 = sl and UI.count(sl) > 0 and sl:At(1) "
            "local c = s1 and UI.get(s1, 'Content') "
            "out[#out + 1] = tostring(UI.get(b, 'HotBarType')) .. ' n=' .. tostring(sl and UI.count(sl)) .. ' cols=' "
            ".. tostring(UI.get(b, 'CellColumns')) .. ' rows=' .. tostring(UI.get(b, 'NumberOfBars')) .. ' s1=' "
            ".. tostring(UI.typeName(s1)) .. '/' .. tostring(UI.get(s1, 'SlotIndex')) .. ' content=' .. tostring(UI.typeName(c)) "
            ".. ' PrototypeID=' .. tostring(UI.get(c, 'PrototypeID')) end "
            "local w = UI.widget('HotBar') local btn = w and UI.find(w, function(e) return UI.name(e) == 'contentContainer' end) "
            "out[#out + 1] = 'button dc=' .. tostring(btn and UI.typeName(UI.get(btn, 'DataContext'))) "
            ".. ' w=' .. tostring(btn and UI.get(btn, 'ActualWidth')) "
            "out[#out + 1] = 'shown=' .. tostring(UI.path(dc, 'CurrentShownDeck.HotBarType')) "
            ".. ' ActiveTask=' .. tostring(UI.path(dc, 'CurrentPlayer.UIData.ActiveTask')) "
            # window #2: a hidden duplicate "HotBar" page read in place of the shown one? (visible flag per page)
            "local hb = {} for _, x in ipairs(UI.widgets()) do if x.name == 'HotBar' then "
            "hb[#hb + 1] = tostring(UI.isVisible(x.el)) .. (x.el == w and '*' or '') end end "
            "out[#out + 1] = 'HotBar pages=' .. table.concat(hb, ',') .. ' (* = UI.widget)' "
            "return table.concat(out, ' | ')", context="client")
    except Exception as e:  # noqa: BLE001
        tree = f"eval error {e}"
    log(f"PROBE hotbar tree: {tree}")
    # window #1 (10-16 18:32): PrototypeID nil on every slot VM, button DataContext nil, ActiveTask set with nothing
    # in hand. Now: the ids come from the HotbarContainer component (Shared/HotbarMap.lua); log both contexts' maps,
    # the first slot VMs' reflected props (UI.props: view models only, never elements) and the armed signals.
    try:
        ids = S.game.eval(
            "local UI, HM = Mods.BG3AI.BG3AI.UI, Mods.BG3AI.BG3AI.HotbarMap local dc = UI.dc('HotBar') "
            "local out = {} local bars = dc and UI.path(dc, 'CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars') "
            "local b1 = bars and bars:At(1) local sl = b1 and UI.get(b1, 'SlotList') "
            "for i = 1, 2 do local s = sl and UI.count(sl) >= i and sl:At(i) local c = s and UI.get(s, 'Content') "
            "local ks = {} for k, v in pairs(UI.props(c)) do ks[#ks + 1] = tostring(k) .. '=' .. tostring(v) end "
            "table.sort(ks) out[#out + 1] = 'vm' .. i .. ' {' .. table.concat(ks, ','):sub(1, 400) .. '}' end "
            "local m, info = Mods.BG3AI.BG3AI.Hotbar.clientMap(nil) "
            "if m then local n = 0 for d, t in pairs(m) do for s, id in pairs(t) do n = n + 1 "
            "if n <= 6 then out[#out + 1] = d .. '#' .. s .. '=' .. id end end end "
            "out[#out + 1] = 'client map: ' .. n .. ' ids, container ' .. tostring(info.container) "
            "else out[#out + 1] = 'client map: none (' .. tostring(info) .. ')' end "
            "return table.concat(out, ' | ')", context="client")
    except Exception as e:  # noqa: BLE001
        ids = f"eval error {e}"
    log(f"PROBE hotbar ids: {ids}")
    srv = S.game.try_rpc("hotbar.map", {"char": sel}, context="server") if sel else None
    log(f"PROBE hotbar server map: {server_map_line(srv)}")
    inp = S._inp()
    try:
        want = HB._slot(S.game, "Target_MainHandAttack", sel)   # = the route's read (server map when ids=none)
    except Exception as e:  # noqa: BLE001
        want = {"error": str(e)}
    cs = HB.cast_state(S.game, sel)
    pos, last = (None, {}) if not want.get("found") or inp is None else HB.locate_cell(S.game, inp, "Target_MainHandAttack", sel, want)
    log(f"PROBE hotbar slot {json.dumps(want)} | cast_state {json.dumps(cs)} | located {pos} {json.dumps(last)[:600]}")
    # window #2: 39 misses and only the last one logged. `seen` = how many probes were on the HUD (IsOverUI), on the
    # decks (Concertina), on a cell, the first spot of each, and the last IsMouseOver chain under the HotBar widget.
    seen = last.get("seen")
    if seen:
        log(f"PROBE hotbar locate seen {json.dumps(seen)}")
    ok = bool(want.get("found")) and want.get("rendered") is not False and pos is not None and cs.get("armed") is False
    item("hotbar widget tree (ACT-8, read-only)", "PASS" if ok else "FAIL",
         f"{tree[:120]}; ids={want.get('ids')} from={want.get('ids_from')} map_why={want.get('map_why')}; "
         f"MainHandAttack: found={want.get('found')} deck={want.get('deck')} index={want.get('index')} "
         f"cols={want.get('cols')} rows={want.get('rows')} rendered={want.get('rendered')} cell={want.get('cell')}; "
         f"located at {pos} in {last.get('probes')} probes"
         + (f" (seen ui={seen.get('over_ui')} decks={seen.get('over_hud')} cells={seen.get('cells')} "
            f"chain={(seen.get('hover') or {}).get('chain')})" if seen else "")
         + f"; armed={cs.get('armed')} by={cs.get('armed_by')} "
         f"preview={cs.get('preview')} spell_task={cs.get('spell_task')}")
    if not ok:  # the guesses the clicks rely on broke: don't click blind into the world with a spell in hand
        item("hotbar cast: Bless on 3 allies (ACT-8)", "SKIP", "the read-only hotbar item failed (see its PROBE line)")
        return
    # 2) Bless x3 out of combat
    party = [r.get("uuid") for r in st.get("party") or []]
    pick, why = pick_hotbar_caster(party, sel)
    caster, spell, status_id, n = pick or (None, None, None, 0)
    if need(caster is not None and not st.get("in_combat") and inp is not None,
            "hotbar cast: Bless on 3 allies (ACT-8)",
            f"caster with a usable ally multi-target spell={caster}, party={len(party)}, "
            f"in_combat={st.get('in_combat')}; {why}"):
        targets = ([caster] + [u for u in party if u != caster])[:n]
        r = HB.hotbar_cast(S.game, inp, caster, spell, targets)
        time.sleep(2.5)
        on = []
        for u in targets:
            try:
                on.append(lua_server(f'return tostring(Osi.HasActiveStatus("{u}", "{status_id}"))') == "1")
            except Exception:  # noqa: BLE001
                on.append(False)
        cs = HB.cast_state(S.game, caster)
        ok = r.ok and r.picked == len(targets) and all(on) and not cs.get("armed") and ui_screen() == "game"
        if ok:
            HOTBAR_PICK.update(caster=caster, spell=spell, status=status_id, n=n)
        item("hotbar cast: Bless on 3 allies (ACT-8)", "PASS" if ok else "FAIL",
             f"{spell} by {S.H.get(caster)} on {len(targets)}: {json.dumps(r.as_dict())[:200]}; {status_id} on {on}; "
             f"in hand after={cs.get('armed')}; screen={ui_screen()}")
        if not ok:
            close_targeting()
    if sel:
        S.game.run_job("act.select", {"char": sel}, wait_s=5)


HOTBAR_ITEM = "use_ability Bless on 3 via the hotbar route (ACT-8 wiring)"

# Spells for the N-target hotbar items: they target allies (castable out of combat on the party without a fight),
# take more than 2 targets at their base level (the Osiris route's limit), and leave a status to check. Bless: on
# HOTBAR_SAVE's hotbars (Claude the Paladin casts it in claude-run-2 S029-S065; S027-start has it on nobody's).
# Aid / Mass Healing Word are Shouts (an area, no target clicks) and prove nothing about the click route.
HOTBAR_ALLY_SPELLS = (("Target_Bless", "BLESS", 3),)
# Seen on the maps only to say what else the party could cast (all hostile: not for an out-of-combat item).
HOTBAR_MULTI_OTHER = ("Target_Bane", "Projectile_MagicMissile", "Projectile_ScorchingRay", "Projectile_EldritchBlast",
                      "Target_HoldPerson")
HOTBAR_PICK: dict = {}   # se's passed pick (caster, spell, status, n): act's wiring item casts the same


# A member's SpellBook / SpellContainer entries whose id contains NEEDLE, with the prepared state the guard reads
# (Guard.prepared_ids / Guard.unprepared, A4 2026-10-17): {book: [{id, orig, src, prep, prepared, unprepared}],
# container: [{id, src, prep, in, linked}], prepared_read} | {why}. Read-only.
SPELL_STATE_LUA = (
    "local G = Mods.BG3AI.BG3AI.Guard local e = Ext.Entity.Get(U) local needle = NEEDLE "
    "if not e then return Ext.Json.Stringify({why = 'no entity'}) end "
    "if not (G and G.prepared_ids and G.unprepared) then return Ext.Json.Stringify({why = 'the mod has no "
    "Guard.prepared_ids (pre-A4)'}) end "
    "local s = tostring local prep = G.prepared_ids(e) local out = {book = {}, container = {}, "
    "prepared_read = prep ~= false} "
    "pcall(function() for _, x in ipairs(e.SpellBook.Spells) do local id = s(x.Id.Prototype) "
    "if id:find(needle, 1, true) then local o = s(x.Id.OriginatorPrototype) out.book[#out.book + 1] = {id = id, "
    "orig = o, src = s(x.Id.SourceType), prep = s(x.PrepareType), prepared = (prep and prep[o]) == true, "
    "unprepared = G.unprepared(x, prep)} end end end) "
    "pcall(function() for _, m in ipairs(e.SpellContainer.Spells) do local id = s(m.SpellId.OriginatorPrototype) "
    "if id:find(needle, 1, true) then out.container[#out.container + 1] = {id = id, src = s(m.SpellId.SourceType), "
    "prep = s(m.PrepareType), ['in'] = s(m.ContainerSpell), linked = m.LinkedSpellContainer == true} end end end) "
    "return Ext.Json.Stringify(out)")


def spell_state(u: str, needle: str) -> dict:
    """SPELL_STATE_LUA for one member; {why} on an error."""
    try:
        v = json.loads(lua_server(f"local U, NEEDLE = '{u}', '{needle}' " + SPELL_STATE_LUA))
        return v if isinstance(v, dict) else {"why": f"not a table: {v!r}"[:80]}
    except Exception as e:  # noqa: BLE001
        return {"why": f"read error {e}"[:120]}


def prep_reason(state: dict, spell: str, on_map: bool) -> str:
    """Why `spell` is or isn't castable from a member's hotbar, from spell_state (A4: S065-end had no Bless on any
    hotbar map, yet Claude cast it in S065 through the Osiris route, whose prepared check never ran)."""
    if state.get("why"):
        return f"spell state unread ({state['why']})"
    rows = [b for b in state.get("book") or [] if str(b.get("id", "")).lower() == spell.lower()]
    if not rows:
        return "not in the spellbook" + (" but on the hotbar map" if on_map else "")
    if not state.get("prepared_read"):
        return "in the spellbook; the prepared list did not read"
    b = rows[0]
    if b.get("unprepared"):
        return (f"in the spellbook ({b.get('prep')}), not prepared: not castable, and the game keeps it off the "
                "hotbar" + ("; yet the map has it" if on_map else ""))
    if on_map:
        return f"prepared ({b.get('prep')}) and on the hotbar map"
    return (f"castable ({b.get('prep')}, prepared={b.get('prepared')}) but not on the server hotbar map: the bar lost "
            "it or Shared/HotbarMap.lua misses a bar (see PROBE hotbar server map)")


def pick_hotbar_caster(party: list, sel: str | None) -> tuple[tuple | None, str]:
    """(caster, spell, status, targets) for the N-target hotbar items, from each member's SERVER hotbar map (no HUD
    switching: live window #3 found no Bless 0.4 s after each act.select, the HUD still showing the previous member).
    The selected member first. Then the caster is selected and the HUD read polled until it shows the cell
    (hotbar._slot_after_select). Returns (None, why) with what every member has when nobody fits."""
    from bg3ai import hotbar as HB
    order = ([sel] if sel in party else []) + [u for u in party if u != sel]
    seen = []
    for spell, status_id, n in HOTBAR_ALLY_SPELLS:
        if len(party) < n:
            continue
        for u in order:
            m = HB.server_map(S.game, u)
            if not HB.map_has(m, spell):
                continue
            if u != sel:
                S.game.run_job("act.select", {"char": u}, wait_s=5)
            r = HB._slot_after_select(S.game, spell, u)
            log(f"PROBE hotbar caster {S.H.get(u)}: {spell} on the map; HUD read found={r.get('found')} "
                f"can_use={r.get('can_use')} selected={r.get('selected')} deck={r.get('deck')} index={r.get('index')}")
            if r.get("found") and r.get("can_use") is not False:
                return (u, spell, status_id, n), ""
            seen.append(f"{S.H.get(u)} has {spell} but the HUD read found={r.get('found')} can_use={r.get('can_use')}")
    for u in order:
        m = HB.server_map(S.game, u)
        have = [sp for sp in tuple(s for s, _, _ in HOTBAR_ALLY_SPELLS) + HOTBAR_MULTI_OTHER if HB.map_has(m, sp)]
        n_ids = sum(len(v or {}) for v in (m or {}).values())
        line = f"{S.H.get(u)}: {n_ids} ids, multi-target {have or 'none'}" if m else f"{S.H.get(u)}: no map"
        for spell, _, _ in HOTBAR_ALLY_SPELLS:   # why each ally spell is missing: unprepared, or the map
            line += f"; {spell} {prep_reason(spell_state(u, spell), spell, HB.map_has(m, spell))}"
        seen.append(line)
    why = "; ".join(seen)
    log(f"PROBE hotbar caster: none fits ({why})")
    return None, why


def hotbar_use_ability() -> bool:
    """act 2026-10-16, audit ACT-8 wiring: use_ability with 3 targets goes the hotbar route (act.cast {ui=true} checks
    and watches the cast, hotbar.hotbar_cast clicks it). HOTBAR_CAST_ENABLED is off by default: turned on for this one
    call, only after se's direct item passed. PASS = 'use_ability: done' with via=hotbar, 'hotbar: 3 of 3', BLESS on
    all 3, nothing charged by the guard, nothing in hand. Decision for the live window: PASS -> set
    hotbar.HOTBAR_CAST_ENABLED = True. True = it cast (the caller reloads)."""
    from bg3ai import hotbar as HB
    se_ok = any(r["item"] == "hotbar cast: Bless on 3 allies (ACT-8)" and r["verdict"] == "PASS" for r in RESULTS)
    if not need(se_ok, HOTBAR_ITEM, "se's 'hotbar cast: Bless on 3 allies (ACT-8)' did not PASS"):
        return False
    st = status()
    party = [r.get("uuid") for r in st.get("party") or []]
    # the spell and caster se's direct item proved (the same reload: the same party and hotbars)
    caster, spell, status_id, n = (HOTBAR_PICK.get(k) for k in ("caster", "spell", "status", "n"))
    if not need(caster in party and len(party) >= (n or 3), HOTBAR_ITEM,
                f"caster={caster} (se's pick), party={len(party)}"):
        return False
    targets = ([caster] + [u for u in party if u != caster])[:n]
    saved, HB.HOTBAR_CAST_ENABLED = HB.HOTBAR_CAST_ENABLED, True
    try:
        out, dt = took("use_ability", ability=spell, targets=[S.H.get(u) for u in targets], who=S.H.get(caster))
    finally:
        HB.HOTBAR_CAST_ENABLED = saved
    time.sleep(1.0)
    blessed = [lua_server(f'return tostring(Osi.HasActiveStatus("{u}", "{status_id}"))') == "1" for u in targets]
    cs = HB.cast_state(S.game, caster)
    ok = ("use_ability: done" in out and "via=hotbar" in out and f"hotbar: {n} of {n}" in out and all(blessed)
          and "charged_by_guard" not in out and not cs.get("armed") and ui_screen() == "game")
    item(HOTBAR_ITEM, "PASS" if ok else "FAIL",
         f"{spell} by {S.H.get(caster)}: {dt:.1f}s; {status_id} on {blessed}; in hand={cs.get('armed')}; "
         + " | ".join(l for l in out.splitlines() if l.startswith(("use_ability", "hotbar", "targets")))[:200])
    if not ok:
        close_targeting()
    return True


TWOSTEP_ITEM = "Dimension Door 10 m via the hotbar (ACT-9 two-step route)"
DD_SPELL = "Teleportation_DimensionDoor"


# Read-only: the container cell's slot VM and Content VM props, each collection prop's first items (the sub-bar's
# variant cells, if the VM carries them), on the selected member's HUD (live window 20261017-152016: e3
# Shadowheart's Teleportation_DimensionDoor read IsContainer at ClassHotBar index 26; its stats have no
# ContainerSpells, so which variants the game boxed there is unknown).
CONTAINER_VM_LUA = (
    "local UI = Mods.BG3AI.BG3AI.UI local dc = UI.dc('HotBar') local out = {} "
    "local function dump(o) local ks = {} for k, v in pairs(UI.props(o)) do "
    "ks[#ks + 1] = tostring(k) .. '=' .. tostring(v):sub(1, 60) end table.sort(ks) return table.concat(ks, ',') end "
    "local bars = dc and UI.path(dc, 'CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars') "
    "for _, bar in UI.items(bars, 16) do if tostring(UI.get(bar, 'HotBarType')) == DECK then "
    "for i, slot in UI.items(UI.get(bar, 'SlotList'), 400) do if i == INDEX then local c = UI.get(slot, 'Content') "
    "out[#out + 1] = 'slot{' .. dump(slot):sub(1, 600) .. '}' out[#out + 1] = 'content{' .. dump(c):sub(1, 1200) .. '}' "
    "for k, v in pairs(UI.props(c)) do if tostring(v):find('Collection', 1, true) then "
    "out[#out + 1] = tostring(k) .. '[' .. tostring(UI.count(v)) .. ']' "
    "for j, x in UI.items(v, 4) do out[#out + 1] = '  ' .. tostring(k) .. '#' .. j .. '{' .. dump(x):sub(1, 400) .. '}' "
    "end end end end end end end "
    "return #out > 0 and table.concat(out, ' | ') or ('no cell ' .. DECK .. ' #' .. INDEX .. ' on the selected HUD')")


def container_probe(u: str, cell: dict) -> str:
    """What a container cell holds: the server's spell entries for the spell (SpellBook / SpellContainer:
    ContainerSpell, LinkedSpellContainer) and the cell's VMs (CONTAINER_VM_LUA, after selecting `u`). Read-only."""
    from bg3ai import hotbar as HB
    srv = spell_state(u, "DimensionDoor")
    try:
        if cell.get("selected") is False:
            S.game.run_job("act.select", {"char": u}, wait_s=5)
            HB._slot_after_select(S.game, DD_SPELL, u)
        vm = str(S.game.eval(f"local DECK, INDEX = '{cell.get('deck')}', {int(cell.get('index') or 0)} "
                             + CONTAINER_VM_LUA, context="client"))
    except Exception as e:  # noqa: BLE001
        vm = f"eval error {e}"
    return f"server {json.dumps(srv)[:1500]} | hud {vm[:3000]}"


def floor_point(p0: list, dist: float) -> list | None:
    """A floor point at p0's height (within 1 m), `dist` m away, well on screen; None when no direction has one."""
    for k in range(8):
        a = k * math.pi / 4
        q = [p0[0] + dist * math.cos(a), p0[1], p0[2] + dist * math.sin(a)]
        hs = lua_server(f"local ok,h=pcall(Ext.Level.GetHeightsAt,{q[0]},{q[2]}) "
                        "if not ok or type(h)~='table' then return '' end local o={} "
                        "for _,v in ipairs(h) do o[#o+1]=string.format('%.2f',v) end return table.concat(o,',')")
        ys = [float(v) for v in hs.split(",") if v.strip()] if hs and "error" not in hs else []
        y = min(ys, key=lambda v: abs(v - p0[1]), default=None)
        if y is None or abs(y - p0[1]) > 1.0:
            continue
        sp = S.game.try_rpc("ui.screen_pos", {"pos": [q[0], y, q[2]]}, context="client") or {}
        nd = sp.get("ndc") or [9, 9]
        if sp.get("on_screen") and abs(nd[0]) < 0.8 and abs(nd[1]) < 0.8:
            return [round(q[0], 2), round(y, 2), round(q[2], 2)]
    return None


def twostep_dd_probe() -> bool:
    """act 2026-10-16, audit ACT-9: a two-step spell by the player's clicks (bridge twostep.py: hotbar cell, who goes =
    the caster, then the landing on the ground, the cursor checked by ui.ground_check before the click). Out of
    combat in BASE, to a floor point ~10 m from the caster that is on screen. twostep.TWOSTEP_ENABLED (A30: its own
    switch) is on for this one call, only after se's read-only hotbar item passed. A30: the cell is armed by its own
    click command (ui.hotbar_use) first; `arm=` in the line says which route armed it (command / the cursor's cell). PASS = 'use_ability: done', the caster now <= 1.5 m from the
    landing, nothing in hand, screen game. The PROBE line logs ui.ground_check at the landing (GUESS T1: cursor
    "Cast"/"CastWarning"). A7: a container cell (live L4: the upcast level picker) is cast through the picker's lowest
    real level. SKIP when nobody has Dimension Door on the hotbar, it is greyed out, or the picker has no usable level.
    True = it cast (the caller reloads)."""
    from bg3ai import hotbar as HB
    ro_ok = any(r["item"] == "hotbar widget tree (ACT-8, read-only)" and r["verdict"] == "PASS" for r in RESULTS)
    if not need(ro_ok, TWOSTEP_ITEM, "se's 'hotbar widget tree (ACT-8, read-only)' did not PASS"):
        return False
    st = status()
    if not need(not (S.game.try_rpc("state.combat") or {}).get("active"), TWOSTEP_ITEM, "in combat"):
        return False
    party = {r.get("uuid"): r for r in st.get("party") or [] if r.get("uuid")}
    caster, cell, greyed = None, {}, []
    for u in party:
        if not HB.map_has(HB.server_map(S.game, u), DD_SPELL):
            continue
        # A7 (live L4): read the member's OWN hud. Unselected, ui.hotbar_slot reads the selected character's decks with
        # this member's id map: e5 Shadowheart's DD index 26 was Claude's Blinding Smite there (no container flag).
        r = hb_slot(DD_SPELL, u)
        if r.get("selected") is False:
            S.game.run_job("act.select", {"char": u}, wait_s=5)
            r = HB._slot_after_select(S.game, DD_SPELL, u)
        if not r.get("found"):
            continue
        if r.get("can_use") is False:
            greyed.append(u)
            continue
        caster, cell = u, r
        break
    if caster is None:
        why = (f"{', '.join(str(S.H.get(u)) for u in greyed)} has {DD_SPELL} greyed out (no level-4+ slot left)"
               if greyed else f"nobody in the party has {DD_SPELL} on the hotbar (level 7+ Gale, or a save with it)")
        item(TWOSTEP_ITEM, "SKIP", why + ": run SESSION-WATCH 'two-step by the hotbar' instead")
        return False
    if cell.get("container") or cell.get("upcast"):
        # the cell opens the upcast level picker (live L4); the route picks the lowest real level
        log(f"PROBE twostep container {S.H.get(caster)}: {container_probe(caster, cell)}")
    p0 = party[caster].get("pos")
    land = floor_point(p0, 10)
    if not need(land is not None, TWOSTEP_ITEM, "no floor point 10 m away on screen around the caster"):
        return False
    sp = S.game.try_rpc("ui.screen_pos", {"pos": land}, context="client") or {}
    log(f"PROBE twostep landing {land} screen {json.dumps(sp)}")
    from bg3ai import twostep as TS
    saved, TS.TWOSTEP_ENABLED = TS.TWOSTEP_ENABLED, True
    try:
        out, dt = took("use_ability", ability=DD_SPELL, x=land[0], y=land[1], z=land[2], who=S.H.get(caster))
    finally:
        TS.TWOSTEP_ENABLED = saved
    time.sleep(1.0)
    p1 = party_pos().get(caster) or p0
    off = math.dist([p1[0], p1[2]], [land[0], land[2]])
    cs = HB.cast_state(S.game, caster)
    land_look = ""
    try:
        rows = (S.RUNTIME / "hotbar_cast.jsonl").read_text().splitlines()
        log(f"PROBE twostep result {rows[-1] if rows else '-'}")
        # A17: what the cursor showed at the landing (L8: no landing click was made at all, `one_step`)
        last = json.loads(rows[-1]) if rows else {}
        land_look = (f"; arm={last.get('command_arm') or 'cursor'} cell={last.get('cell_pos')} "
                     f"landing looks={last.get('looks')} clicks={last.get('ground_clicks')} "
                     f"one_step={last.get('one_step')} first_s={last.get('first_s')}")
    except (OSError, ValueError):
        pass
    ok = "use_ability: done" in out and off <= 1.5 and not cs.get("armed") and ui_screen() == "game"
    if not ok and "no spell slot left" in out and not cs.get("armed"):
        # the picker greyed every level (a save without the slot): not a route failure
        item(TWOSTEP_ITEM, "SKIP", f"{S.H.get(caster)}: the level picker had no usable level (no slot left); "
             + " | ".join(l for l in out.splitlines() if l.startswith(("use_ability", "hotbar")))[:200])
        return False
    item(TWOSTEP_ITEM, "PASS" if ok else "FAIL",
         f"{dt:.1f}s; now {off:.1f} m from the landing (moved {math.dist(p0, p1):.1f} m); in hand={cs.get('armed')}; "
         + " | ".join(l for l in out.splitlines() if l.startswith(("use_ability", "hotbar", "the spell")))[:220]
         + land_look[:200])
    if not ok:
        close_targeting()
    return True
