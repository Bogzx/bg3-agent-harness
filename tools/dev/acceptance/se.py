"""Section se: the hotbar probes, door line of sight, area blasts, click search and the strict lock probe."""

from __future__ import annotations

import json
import re
import time

from .core import (
    BASE,
    DOOR_SAVE,
    HOTBAR_SAVE,
    OUT,
    S,
    alive,
    bg3le_log,
    call,
    first_line,
    grab,
    grep_crash,
    item,
    load_verified,
    log,
    lua_server,
    marker_gone,
    moved_m,
    need,
    party_pos,
    set_marker,
    status,
    took,
    ui_screen,
)
from .hotbar import close_targeting, hotbar_probe, hotbar_use_ability, twostep_dd_probe, ui3_refused
from .strict import strict_lock_probe


def sec_se() -> None:
    """se overnight batch (2026-10-10 23:xx): click hover/unknown button, reactions on an unselected member, the
    game-process check of load_game, Noesis counters, and a read-only CursorText probe in the Jump targeting."""
    if not load_verified(BASE):
        return
    # patch 0009 (S060 09:11 crash): the first Ext.StaticData read finds the resource manager; a false positive at
    # region offset 0xeb5xx killed the game twice (`index < size_`). PASS = the read answers, the game lives, and the
    # manager the log names is the real one (0x15axxx / "recorded offset") with no crash after it.
    lg = bg3le_log()
    try:
        out = lua_server('local ok, all = pcall(Ext.StaticData.GetAll, "ActionResource") '
                         'return tostring(ok) .. " " .. tostring(ok and all and #all or all)')
    except Exception as e:  # noqa: BLE001
        out = f"eval error {e}"
    txt = lg.read_text(errors="replace") if lg else ""
    found = re.findall(r"static data: resource manager (?:at (0x[0-9a-f]+)(.*)|not found.*)", txt)
    where = found[-1] if found else None
    real = bool(where) and (where[0][-5:-3] == "5a" or "recorded offset" in where[1])
    crash = grep_crash(lg)
    item("static data manager is the real one (patch 0009)",
         "PASS" if alive() and out.startswith("true") and real and not crash else "FAIL",
         f"GetAll(ActionResource) → {out[:80]}; log: {('at ' + where[0] + where[1][:40]) if where else 'no find line'}"
         f" ({lg.name if lg else 'no log'}){'; ' + crash if crash else ''}")
    # click(button="none"): S039 21:47:42 it committed a left click (a 5 m jump under Z). 06:27 FAIL was the load's
    # settle (hover 2.3 s after load_game, `level_changed` in the same result; 0.0 m 1 s later): wait until the party
    # stands still, then judge the controlled character only (the one a click would move); followers are evidence.
    def still_party(max_s: float = 8.0) -> dict:
        prev, calm, end = party_pos(), 0, time.monotonic() + max_s
        while time.monotonic() < end and calm < 2:
            time.sleep(0.5)
            now = party_pos()
            calm = calm + 1 if moved_m(prev, now) <= 0.05 else 0
            prev = now
        return prev

    def per_member(a: dict, b: dict) -> str:
        names = {r.get("uuid"): r.get("name") for r in status().get("party") or []}
        return ", ".join(f"{names.get(u, u[:6])} {moved_m({u: a[u]}, {u: b[u]}):.1f}" for u in a if u in b)

    def hover_check(name: str, button: str, want: str) -> None:
        sel = status().get("active")
        p0 = still_party()
        out, dt = took("click", x=0.5, y=0.62, button=button)
        time.sleep(1.5)
        p1 = party_pos()
        mv = moved_m({sel: p0[sel]}, {sel: p1[sel]}) if sel in p0 and sel in p1 else moved_m(p0, p1)
        scr = ui_screen()
        ok = want in out and mv < 0.3 and scr == "game"
        item(name, "PASS" if ok else "FAIL", f"{first_line(out)} ({dt:.1f}s); controlled character moved {mv:.1f} m "
             f"(all: {per_member(p0, p1)}); screen={scr}")

    hover_check("click button=none hovers only", "none", "nothing clicked")
    hover_check("click unknown button refused", "middle", "unknown button")

    # audit UI-3 (se, 2026-10-16): the escape hatches don't save or load around save_game/load_game. Bridge refusals
    # (ui_action QuickLoadCommand, press_key F8), the mod's own (ui.command / ui.key straight to the client), and no
    # load happened (the server marker survives). Nothing is saved or loaded either way except on a regression.
    set_marker()
    outs = [call("ui_action", command="QuickLoadCommand", widget="*"), call("press_key", key="F8")]
    mod, mod_ok = [], []
    # the raw mod ops would really quicksave/quickload on a mod without the UI-3 refusal (not hot-reloaded): send them
    # only when the live client Lua has it
    live = S.game.eval("local A = Mods.BG3AI.BG3AI.UIActions return tostring(A and A.saveLoadName ~= nil)",
                       context="client")
    for op, args in [] if live != "true" else (("ui.command", {"widget": "*", "command": "QuickLoadCommand"}), ("ui.key", {"key": "F8"}),
                     ("ui.command", {"widget": "*", "command": "QuickSaveCommand"})):
        what = f"{op} {args.get('command') or args.get('key')}"
        try:
            mod.append(f"{what}: NOT refused {S.game.rpc(op, args, context='client')}")
            mod_ok.append(False)
        except Exception as e:  # noqa: BLE001
            # live window #1 (10-16): the verdict read the hint after a 90-char cut ("go through save_ga") and FAILed
            # three right refusals: judge the whole message, shorten only the evidence
            mod_ok.append(ui3_refused(str(e)))
            mod.append(f"{what}: {'refused' if mod_ok[-1] else 'error'} {str(e)[:70]}")
    time.sleep(3.0)
    loaded = marker_gone()
    if live != "true":
        mod.append(f"mod ops not sent: the live client Lua has no UI-3 refusal (UIActions.saveLoadName={live}): hot reload")
    ok = (live == "true" and all(o.startswith("REFUSED") for o in outs) and len(mod_ok) == 3 and all(mod_ok)
          and not loaded and alive())
    item("ui_action / press_key / ui.command don't save or load (UI-3)", "PASS" if ok else "FAIL",
         f"a load happened={loaded}; mod refusals {sum(mod_ok)}/3 | "
         + " | ".join(first_line(o)[:90] for o in outs) + " | " + " | ".join(mod))

    # Save name box read-back (S056 ×4, S057: every save_game FAILED "the save name box keeps reading 'ls.LSTextBox'":
    # the check read ToString(), a TextBox's class name). ui.menu save probe: open, set the name, read the Text
    # property back after the settle, ESCAPE out; nothing is written (checked on disk too).
    t = time.time()
    r = S.game.run_job("ui.menu", {"action": "save", "name": "acc-name-probe", "probe": True}, context="client", wait_s=25)
    time.sleep(1.0)
    res = r.result if isinstance(r.result, dict) else {}
    scr = ui_screen()
    stray = [d.name for d in S.SAVE_DIR.iterdir() if d.stat().st_mtime >= t] if S.SAVE_DIR.is_dir() else []
    ok = r.status == "done" and res.get("matches") is True and scr == "game" and not stray
    # BACKLOG "save-name probe reads None" (2026-10-16): the Text read is unboxed and falls back to an inner TextBox and
    # the dialog DC's SavegameNameInput; read_via names the source that answered (text/field/inner/dc). PASS with
    # via=dc means the box itself is still unreadable: real saves then re-set the name before each RETURN (blind_sets).
    item("save name box reads our name (probe, no save)", "PASS" if ok else "FAIL",
         f"{r.status} {r.error or ''}; read={res.get('read')!r} via={res.get('read_via')} matches={res.get('matches')} "
         f"renamed={res.get('renamed')}; screen after={scr}; save folders written={stray or 'none'}")

    # set_reactions on a member that is not selected (S041 22:41:22 "char is not the selected character")
    st = status()
    sel = st.get("active")
    other = next((r for r in st.get("party") or [] if r.get("uuid") != sel), None)
    if need(other is not None, "reactions on an unselected member", "party of one"):
        out, dt = took("set_reactions", who=other.get("name"))
        ok = "not the selected character" not in out and "FAILED" not in out and "EXCEPTION" not in out
        item("reactions on an unselected member", "PASS" if ok else "FAIL",
             f"{other.get('name')} (selected was {sel}): {first_line(out)[:160]} ({dt:.1f}s)")
        if sel:
            S.game.run_job("act.select", {"char": sel}, wait_s=5)
        # [act] A13 S069 20:45:31: act.select was done 1 ms after the order while another member held ClientControl;
        # done now means the member holds it (one resync via another member). Read-only on the selection.
        r = S.game.run_job("act.select", {"char": other["uuid"]}, wait_s=5)
        now = status().get("active")
        item("act.select done = the member holds ClientControl (A13)",
             "PASS" if r.status == "done" and now == other["uuid"] else "FAIL",
             f"{r.status} {r.error or ''} result={r.result}; active after={now} (wanted {other['uuid']})")
        # [act] A16 S074 23:06:52: act.select was done on the server while the HUD kept another member (ui.reactions
        # refused). Done now means the client's ClientControl (ui.selected) shows the member; a second select of the
        # same member is the server's no-op order that S074 hit.
        hud = (S.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
        r2 = S.game.run_job("act.select", {"char": other["uuid"]}, wait_s=5)
        hud2 = (S.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
        item("act.select done = the HUD shows the member (A16)",
             "PASS" if r.status == "done" and r2.status == "done" and hud == hud2 == other["uuid"] else "FAIL",
             f"first {r.status}, HUD after={hud}; again {r2.status} {r2.error or ''} result={r2.result}, HUD after="
             f"{hud2} (wanted {other['uuid']}; None = client Lua not hot-reloaded)")
        if sel:
            S.game.run_job("act.select", {"char": sel}, wait_s=5)

    # load_game's crash fast path: the 60 MB Steam-runtime parent must not count, the game must
    from bg3ai import metrics as MX
    pid = MX.game_pid()
    rss = MX.rss_mb(pid)
    alive_now = S._game_process_alive()
    item("load_game game-process check", "PASS" if alive_now and (rss or 0) >= S.GAME_ALIVE_MIN_MB else "FAIL",
         f"_game_process_alive={alive_now}; biggest bg3 pid={pid} rss={rss} MB (min {S.GAME_ALIVE_MIN_MB})")

    # naut helm cinematic (se, 2026-10-16): AutoSkip now skips a line playing in our combat turn (skip command +
    # engine SPACE, never shim SPACE) and autoskip.status says why a line is not being skipped; the bridge hands a
    # stuck line back after DIALOGUE_STUCK_EXIT_S and a read tool waits AUTO_DIALOGUE_READ_LINE_S. BASE has no combat
    # cinematic: this checks the live mod/bridge carry it; the skip itself is the SESSION-WATCH "helm cinematic" row.
    sw = S.game.eval("local K = Mods.BG3AI.BG3AI.AutoSkip return tostring(K and K.TURN_LINE_SKIP) .. ' ' .. "
                     "tostring(K and K.TURN_LINE_SPACE)", context="client")
    ast = S.game.try_rpc("autoskip.status", context="client") or {}
    ok = sw == "true true" and isinstance(ast, dict) and ast.get("on") is True \
        and S.DIALOGUE_STUCK_EXIT_S > 0 and S.AUTO_DIALOGUE_READ_LINE_S < S.DIALOGUE_LINE_WAIT_S
    item("AutoSkip skips lines in our combat turn (helm; switches live)", "PASS" if ok else "FAIL",
         f"TURN_LINE_SKIP/SPACE={sw} (nil nil = client Lua not hot-reloaded); autoskip on={ast.get('on')} "
         f"line={ast.get('line')}; bridge stuck_exit={S.DIALOGUE_STUCK_EXIT_S}s read_line={S.AUTO_DIALOGUE_READ_LINE_S}s")

    # Noesis parked/leaked: sys.mem (client) + the bg3le minute line
    m = S.game.try_rpc("sys.mem", context="client") or {}
    nz = m.get("noesis") if isinstance(m, dict) else None
    lg = bg3le_log()
    line = ""
    for _ in range(14):  # the minute line: up to ~70 s
        txt = lg.read_text(errors="replace") if lg else ""
        hits = re.findall(r".*mem: client lua .*\| noesis gen.*", txt)
        if hits:
            line = hits[-1]
            break
        time.sleep(5)
    ok = isinstance(nz, dict) and isinstance(nz.get("parked"), int) and bool(line)
    item("Noesis parked observable", "PASS" if ok else "FAIL",
         f"sys.mem noesis={nz}; log: {line.strip()[:140] or 'no minute line with noesis (' + (lg.name if lg else 'no log') + ')'}")

    # CursorText probe for the hand-aimed jumps item (BACKLOG [se] M): what does the game show next to the cursor
    # while the Jump targeting (Z) hovers a near and a far point? Read-only: hover, read, ESCAPE; nothing is cast.
    # 01:06 run: CursorText had no text items and the props log was cut at 300 chars → now: every visible widget's
    # texts before Z vs while hovering (whatever widget holds "Too far"/"Invalid" shows up as NEW), and the full
    # CursorText props one level down, unshortened, in se.log.
    def texts() -> dict:
        d = S.game.try_rpc("ui.dump", {"text_only": True, "max": 400}, context="client") or {}
        return {w.get("w"): [x.get("t") for x in w.get("items") or [] if isinstance(x, dict) and x.get("t")]
                for w in (d.get("widgets") or []) if isinstance(w, dict)}

    def cursor_props() -> dict:
        pr = S.game.try_rpc("ui.props", {"widget": "CursorText", "commands": True}, context="client") or {}
        sub = {}
        for k in list((pr.get("objects") or {}) if isinstance(pr, dict) else {})[:15]:
            sub[k] = S.game.try_rpc("ui.props", {"widget": "CursorText", "path": k}, context="client")
        return {"props": pr, "objects": sub}

    # 04:30 run: IsScreenFadeActive was still true right after the load: wait for the fade, else Z may do nothing.
    def player_vals() -> dict:
        out = {}
        # 09:0x read-only probe in S059: CurrentPlayer.UIData.Cursor (Distance, CursorObscuredState, …) and
        # UIData.CastInfo (a collection, empty outside targeting) are where a targeting verdict could live.
        for path in ("CurrentPlayer", "CurrentPlayer.SelectedCharacter", "CurrentPlayer.UIData",
                     "CurrentPlayer.UIData.Cursor", "CurrentPlayer.UIData.CastInfo.1", "CurrentPlayer.UIData.HitChanceDesc"):
            pr = S.game.try_rpc("ui.props", {"widget": "CursorText", "path": path}, context="client")
            for k, v in ((pr or {}).get("values") or {}).items() if isinstance(pr, dict) else []:
                out[f"{path}.{k}"] = v
        return out

    for _ in range(20):
        if not player_vals().get("CurrentPlayer.IsScreenFadeActive"):
            break
        time.sleep(0.5)
    before = texts()
    vals0 = player_vals()
    call("press_key", key="Z")
    time.sleep(0.8)
    vals1 = player_vals()
    changed = {k: [vals0.get(k), v] for k, v in vals1.items() if vals0.get(k) != v}
    if not changed:
        # 06:27: press_key Z (the mod's InjectKeyPress) changed nothing: try the real SDL key through the input shim
        try:
            S._inp().key("Z")
            time.sleep(0.8)
            vals1 = player_vals()
            changed = {k: [vals0.get(k), v] for k, v in vals1.items() if vals0.get(k) != v}
            log(f"PROBE shim key Z: changed {json.dumps(changed)}")
        except Exception as e:  # noqa: BLE001
            log(f"PROBE shim key Z failed: {e}")
    log("PROBE ui.state after Z (is the Jump targeting on?): "
        + json.dumps(S.game.try_rpc("ui.state", context="client"))[:1500])
    log(f"PROBE player/selected values changed by Z: {json.dumps(changed)}")
    # LIVE G4 check (Hotbar.lua armedOf): with Jump in hand armed should be true, after the ESCAPE below false
    log("PROBE cast_state with Jump (Z) in hand: "
        + json.dumps(S.game.try_rpc("ui.cast_state", {"char": status().get("active")}, context="client"))[:600])
    new_texts = []
    for x, y in ((0.55, 0.6), (0.9, 0.2)):
        call("click", x=x, y=y, button="none")
        time.sleep(0.6)
        now = texts()
        fresh = {w: [t for t in ts if t not in (before.get(w) or [])] for w, ts in now.items()}
        fresh = {w: ts for w, ts in fresh.items() if ts}
        new_texts.append(f"@{x},{y}: {json.dumps(fresh)[:400]}")
        log(f"PROBE new widget texts while targeting @{x},{y}: {json.dumps(fresh)}")
        try:  # what a person sees there (is the jump arc / cursor text on?)
            img = getattr(S.screenshot, "fn", S.screenshot)(scale=0.5)
            shot = OUT / f"se-jump-probe-{x}-{y}.jpg"
            shot.write_bytes(img.data)
            log(f"PROBE screenshot {shot}")
        except Exception as e:  # noqa: BLE001
            log(f"PROBE screenshot failed: {e}")
        hv = player_vals()
        log(f"PROBE UIData/Cursor/CastInfo values changed while hovering @{x},{y}: "
            + json.dumps({k: [vals1.get(k), v] for k, v in hv.items() if vals1.get(k) != v}))
        log(f"PROBE CursorText props @{x},{y}: {json.dumps(cursor_props())}")
        log(f"PROBE CursorText dump @{x},{y}: " + json.dumps(S.game.try_rpc(
            "ui.dump", {"root": "CursorText", "hidden": True, "text_only": False, "depth": 30, "max": 300},
            context="client")))
    p0 = party_pos()
    after = close_targeting()
    time.sleep(1.0)
    log("PROBE cast_state after the ESCAPE (nothing in hand): "
        + json.dumps(S.game.try_rpc("ui.cast_state", {"char": status().get("active")}, context="client"))[:600])
    mv = moved_m(p0, party_pos())
    got = any(t.split(": ", 1)[1] != "{}" for t in new_texts)
    item("CursorText probe (Jump targeting)", "PASS" if got and mv < 0.5 else ("FAIL" if mv >= 0.5 else "INFO"),
         f"new texts while targeting={got}; party moved {mv:.1f} m; screen after={after}; " + " | ".join(new_texts)[:220]
         + " (full: se.log PROBE lines)")
    # audit ACT-8 rest: hotbar cell + Bless x3 on HOTBAR_SAVE (spends a slot: after the probes, reloaded right below);
    # the read-only hotbar item still runs on BASE when that save won't load
    hb_save = HOTBAR_SAVE if load_verified(HOTBAR_SAVE, record=True) else BASE
    hotbar_probe()
    load_verified(hb_save)
    if hotbar_use_ability():   # the same Bless x3 through use_ability (act's wiring): reloaded again
        load_verified(hb_save)
    if twostep_dd_probe():   # ACT-9: Dimension Door by the clicks (spends a slot, moves the caster): reloaded
        load_verified(hb_save)
    # Lockpick on the S050 door (S050 04:11-04:12 + S051: 8x "hover_miss hovered_instead=[(an object with no id)]";
    # S047 02:57: "nothing happened" while the game walked the actor to its lockpick spot). Opens the roll only:
    # ESCAPE, then the base save is reloaded; nothing is rolled or saved.
    if load_verified(DOOR_SAVE):
        # S055 06:46:20 Lower City: "the map didn't open (pressed M,M,M,M,M over 6.2s)" (the Act 3 map shows slowly;
        # the 1.2 s re-press closed it) + check_all "screen map after closing the map (left open?)".
        t = time.monotonic()
        r = S.game.run_job("ui.waypoints", {}, context="client", wait_s=20)
        dt = time.monotonic() - t
        time.sleep(1.5)
        res = r.result if isinstance(r.result, dict) else {}
        scr = ui_screen()
        n = len(res.get("waypoints") or [])
        ok = r.status == "done" and n > 0 and res.get("map_presses") == 1 and scr == "game"
        item("map opens once in the Lower City (slow map)", "PASS" if ok else "FAIL",
             f"{r.status} {r.error or ''} in {dt:.1f}s; waypoints={n} map_presses={res.get('map_presses')}; "
             f"screen 1.5 s after closing={scr}")
        # [act] check_all S11 (19:11:17 / 19:56:53, S068 started on the map): two ui.waypoints back to back (check_all's
        # pattern), the 2nd said done right after its ESCAPE with the map still up. done = closed now: no sleep here.
        rows = []
        for _ in range(2):
            t = time.monotonic()
            r = S.game.run_job("ui.waypoints", {}, context="client", wait_s=20)
            res = r.result if isinstance(r.result, dict) else {}
            rows.append((r.status, ui_screen(), res.get("map_escapes"), res.get("map_open"), time.monotonic() - t))
        ok = all(st == "done" and sc == "game" and not mo for st, sc, _, mo, _ in rows)
        item("ui.waypoints done = map closed (twice in a row)", "PASS" if ok else "FAIL",
             "; ".join(f"{st} screen={sc} escapes={es} map_open={mo} {d:.1f}s" for st, sc, es, mo, d in rows))
        # screenshot(map=true) (S055 x2, S056 x3: M + screenshot + ESCAPE by hand): one call opens, shoots, closes.
        # Judged on the map (opened, closed after); a frame that doesn't present (08:0x: the acceptance window's
        # screenshots failed) is reported, not failed.
        t = time.monotonic()
        try:
            img = getattr(S.screenshot, "fn", S.screenshot)(scale=0.25, map=True)
            shot = "map image" if not isinstance(img, str) else img
        except Exception as e:  # noqa: BLE001
            shot = f"capture error: {e}"
        dt = time.monotonic() - t
        time.sleep(1.0)
        scr = ui_screen()
        if isinstance(shot, str) and "didn't open" in shot:
            item("screenshot(map=true) opens + closes the map", "FAIL", f"{shot[:200]} ({dt:.1f}s)")
        else:
            item("screenshot(map=true) opens + closes the map", "PASS" if scr == "game" else "FAIL",
                 f"{shot[:160]} in {dt:.1f}s; screen 1 s after={scr} (game = closed again)")
        # 20261017-145354: the capture waited in rt/shots, the game wrote runtime/shots (BG3AI_SHOTS_DIR now)
        item("screenshot works in the acceptance window (shots dir = the game's)",
             "PASS" if shot == "map image" else "FAIL", f"{shot[:160]} dir={S._shots_dir()}")
        near = call("get_nearby", kinds="door", radius=20)
        # S054 05:54:51: no open/closed on doors → interact closed an open door (a scene started). State.lua: doors
        # carry open/closed (Osi.IsOpened); the locked Foundry door must read "closed".
        drows = re.findall(r"^.*<door>.*$", near, re.M)
        ok = bool(drows) and all(re.search(r"\b(open|closed)\b", r) for r in drows)
        item("doors show open/closed in get_nearby", "PASS" if ok else ("SKIP" if not drows else "FAIL"),
             " | ".join(drows)[:240] or "no door within 20 m")
        doors = re.findall(r"^(?:ITEM\s+|\+\s*)?(e\d+) ([^<\n]+?) <door>[^\n]*\blocked\b", near, re.M)  # "ITEM e8 Steel … <door> 1.6m locked"
        if doors:
            click_search_probe(doors[0][0], doors[0][1].strip())
        if need(bool(doors), "lockpick a door (no-id hover, walk wait)", f"no locked door within 20 m of {DOOR_SAVE}"):
            h, name = doors[0]
            out, dt = took("lockpick", target=h)
            ok = "opened=roll" in out or "unlocked" in out
            # live window #2 (19:51): a Steel Watcher saw the lockpick and the crime scene came in place of the roll.
            # The route (hover, menu row, the game's answer) worked; the tool now says so (reason=interrupted).
            caught = "reason=interrupted" in out and "menu=Lockpick" in out
            ev = (f"{h} {name.strip()}: {first_line(out)[:200]} ({dt:.1f}s) "
                  + grab(r"hover_via=\w+|no_id_objects=\[[^\]]*\]|walked_m=[\d.]+", out)
                  + (" [caught: the game's crime scene, not a roll; the walk wait is not exercised]" if caught else ""))
            ok = ok or caught
            item("lockpick a door (no-id hover, walk wait)", "PASS" if ok else "FAIL", ev)
            if ui_screen() == "roll":
                call("press_key", key="ESCAPE")
        door_los_probe([h for h, _ in re.findall(r"^(?:ITEM\s+|\+\s*)?(e\d+) ([^<\n]+?) <door>", near, re.M)])
        blast_probe()
        load_verified(BASE)
    strict_lock_probe()  # last: process-wide mode, restored in a finally; nothing runs after it in this section


# A20 (S083 03:37/03:52/03:55): use_ability at a door or wall past G.ITEM_LOS_NEAR_M (4 m) was refused "no line of
# sight" (the engine's HasLineOfSight) while the game's cursor showed 100 %; the guard now also takes an eye ray that
# reaches the item's body (G.item_ray_los). Read-only, per party member x door within 20 m: the distance, the engine's
# LOS, the rays' verdict. FAIL when the rays are unusable live (nil: uncalibrated / API error, every far door stays
# refused) or say no where the engine says yes (the ray rule must not lose what the engine sees). The cast itself is
# SESSION-WATCH "door/wall cast past 4 m (A20)".
DOOR_LOS_ITEM = "act: door line of sight by eye rays (A20, S083)"
DOOR_LOS_LUA = (
    "local B=Mods.BG3AI.BG3AI local G,U=B.Guard,B.Util local out={} "
    "for _,u in ipairs(DOORS) do for _,r in pairs(Osi.DB_PartyMembers:Get(nil)) do local c=r[1] "
    "local p,t=G.pos(c),G.pos(u) local ok,ray=pcall(G.item_ray_los,c,u) "
    "out[#out+1]={door=u,char=tostring(c),d=(p and t) and U.dist(p,t) or -1,los=G.b(Osi.HasLineOfSight(c,u)),"
    "ray=ok and tostring(ray) or ('err '..tostring(ray)),guard=G.los_ok(c,u)} end end "
    "return Ext.Json.Stringify(out,{Beautify=false})")


def door_los_probe(handles: list[str]) -> None:
    uuids = [u for u in (S._world_uuid(h) for h in handles[:4]) if u]
    if not need(bool(uuids), DOOR_LOS_ITEM, f"no door within 20 m of {DOOR_SAVE}"):
        return
    doors = ",".join(f"'{u}'" for u in uuids)
    raw = lua_server(f"local DOORS={{{doors}}} " + DOOR_LOS_LUA)
    try:
        rows = json.loads(raw)
    except ValueError:
        item(DOOR_LOS_ITEM, "FAIL", f"probe: {raw[:200]}")
        return
    bad = [r for r in rows if r.get("ray") not in ("true", "false") or (r.get("los") and r.get("ray") != "true")]
    ev = "; ".join(f"{r['door'][:8]}<-{r['char'][-12:]} {r['d']:.1f}m los={int(bool(r['los']))} ray={r['ray']}"
                   f" guard={int(bool(r['guard']))}" for r in rows)
    item(DOOR_LOS_ITEM, "FAIL" if bad or not rows else "PASS", ev)


# A22 (S085 05:03:37): Guard.blast_risk refuses a harmful area cast whose blast catches a party member / ally /
# neutral in sight, or a projectile that bursts short of its point (eye ray). Read-only: Fireball's radius from the
# live stats (ExplodeRadius / AreaRadius: which one the game fills), and per member G.blast_path to a point 8 m east
# (clear/blocked, no error) and G.in_blast around itself (the member must be listed). FAIL = radius 0 (the check
# never fires), a ray/scan error, or a member missing from its own ring. The real cast: SESSION-WATCH "AoE blast".
BLAST_ITEM = "act: area blast check reads the radius and who stands in it (A22, S085)"
BLAST_LUA = (
    "local B=Mods.BG3AI.BG3AI local G=B.Guard local st=Ext.Stats.Get('Projectile_Fireball') "
    "local out={explode=st and tostring(st.ExplodeRadius),area=st and tostring(st.AreaRadius),"
    "r=st and G.blast_radius(st) or -1,rows={}} "
    "for _,row in pairs(Osi.DB_PartyMembers:Get(nil)) do local c=row[1] local p=G.pos(c) if p then "
    "local ok,at,blk=pcall(G.blast_path,c,{p[1]+8,p[2],p[3]}) local ok2,hit=pcall(G.in_blast,c,p,4) local me=false "
    "if ok2 and hit then for _,h in ipairs(hit) do if G.same(h.uuid,c) then me=true end end end "
    "out.rows[#out.rows+1]={char=tostring(c),path=ok and (blk and 'blocked' or 'clear') or ('err '..tostring(at)),"
    "ring=ok2 and (hit and #hit or -1) or -2,me=me} end end "
    "return Ext.Json.Stringify(out,{Beautify=false})")


def blast_probe() -> None:
    raw = lua_server(BLAST_LUA)
    try:
        v = json.loads(raw)
    except ValueError:
        item(BLAST_ITEM, "FAIL", f"probe: {raw[:200]}")
        return
    rows = v.get("rows") or []
    bad = [r for r in rows if r.get("path") not in ("clear", "blocked") or r.get("ring", -1) < 1 or not r.get("me")]
    ev = (f"Fireball ExplodeRadius={v.get('explode')} AreaRadius={v.get('area')} radius={v.get('r')}; "
          + "; ".join(f"{r['char'][-12:]} path={r['path']} ring={r['ring']} self={int(bool(r['me']))}" for r in rows))
    item(BLAST_ITEM, "FAIL" if bad or not rows or not (v.get("r") or 0) > 0 else "PASS", ev)


def click_search_probe(h: str, name: str) -> None:
    """se click_on latency (naut-r2 S001: 83 tries 12.6 s; off_screen x2 7 s): the hover search on the S050 door, new
    (HOVER_V2) then old, hover only (nothing is clicked). PASS = the new search finds the door, in no more time than
    the old one (+0.3 s) or within 2 s. The evidence names the settle mode: "cursor" = the per-try settle on the
    picking helper's cursor works live (~2 frames a try); "frames" = WindowCursorPos never matched (units?), so
    only the order/early-turn gains apply -> BACKLOG for se with the cursor values from a ui.hover_check."""
    from bg3ai import clicker
    inp = S._inp()
    name_ = "click_on hover search: new vs old (tries, s, settle)"
    if not need(inp is not None, name_, "input shim not available"):
        return
    u = S._world_uuid(h)
    saved, got = clicker.HOVER_V2, {}
    try:
        # window #1 (10-16) ran new first: its two occluded turns left old a view where the first point hit (1 try)
        # while new had started from the hinge view. Now old first, its occluded turns / zoom undone, then new:
        # both start from the same view.
        for v2 in (False, True):
            clicker.HOVER_V2 = v2
            t = time.monotonic()
            centered = clicker.center_on_actor(S.game, inp, u)
            moves = clicker.bring_into_view(S.game, inp, u)
            fn = clicker._search_v2 if v2 else clicker._search_v1
            pos, tries, last = fn(S.game, inp, u, moves, {}, lambda d: None)
            got[v2] = (pos, tries, time.monotonic() - t, last.get("settle"), (["center"] if centered else []) + moves)
            w, hh = inp.window_size()
            inp.move(w // 2, hh - 8)   # off the door before the next search
            if not v2:
                undo_search_camera(inp, u, moves)
            time.sleep(0.3)
    except Exception as e:  # noqa: BLE001
        item(name_, "FAIL", f"{h} {name}: {type(e).__name__}: {e}")
        return
    finally:
        clicker.HOVER_V2 = saved
    (p2, t2, s2, st2, m2), (p1, t1, s1, _, m1) = got[True], got[False]
    ok, why = hover_compare(got[False], got[True])
    item(name_, "PASS" if ok else "FAIL",
         f"{h} {name}: new {'hit' if p2 else 'MISS'} {t2} tries {s2:.2f}s settle={st2} camera={m2}; "
         f"old {'hit' if p1 else 'MISS'} {t1} tries {s1:.2f}s camera={m1}" + (f"; {why}" if why else ""))


OCCLUDED_TURNS = {"rotate_occluded_right": 1, "rotate_occluded_left": -2}   # clicker's turn multipliers


def undo_search_camera(inp, uuid: str, moves: list[str]) -> None:
    """Turn back a search's occluded turns and zoom-out (bring_into_view's own moves stay: the next search
    re-runs it), so the next search starts from the same view."""
    from bg3ai import clicker
    net = sum(OCCLUDED_TURNS.get(m, 0) for m in moves)
    if net:
        clicker._turn(S.game, inp, uuid, -net)
    if "zoom_out" in moves:
        w, hh = inp.window_size()
        inp.wheel(2, w // 2, hh // 2)
        clicker.settle_camera(S.game, inp, uuid)


def hover_compare(old: tuple, new: tuple) -> tuple[bool, str]:
    """(pos, tries, s, settle, camera) of the old and the new search from the same view -> (ok, why not).
    New must hit, in <= old + 0.3 s (or <= 2 s); and where old hit without an occluded turn on its first aim's
    ring or an aim's centre (<= 9 + aims-1 tries), new must not turn the camera either nor take more tries."""
    from bg3ai import clicker
    (p1, t1, s1, _, m1), (p2, t2, s2, _, m2) = old, new
    if p2 is None:
        return False, "new missed"
    if not (s2 <= s1 + 0.3 or s2 <= 2.0):
        return False, f"new slower: {s2:.2f}s vs {s1:.2f}s"
    turned = lambda m: any(x in OCCLUDED_TURNS for x in m)  # noqa: E731
    if p1 is not None and not turned(m1) and t1 <= 9 + len(clicker.AIMS) - 1:
        if turned(m2):
            return False, "new turned the camera where old hit without a turn"
        if t2 > t1:
            return False, f"new took more tries ({t2} > {t1})"
    return True, ""
