"""Section vis: visibility reads, Mystra's statue (V10)."""

from __future__ import annotations

import json
import re
import time

from .core import (
    BASE,
    MYSTRA_SAVE,
    S,
    call,
    first_line,
    grab,
    item,
    load_verified,
    log,
    lua_server,
    need,
    static_lead_ids,
    status,
    took,
)


def sec_vis() -> None:
    """vis overnight batch (2026-10-10/11), read-only on the base save (no jump is cast): the drop extends the jump
    reach, a climb of 10 m is refused, Feather Fall known, route to a point with no floor says why, o1 as a target,
    an empty name search lists who is in view, the journal lists open quests the game never 'accepted'."""
    if not load_verified(BASE):
        return
    g = "local G=Mods.BG3AI.BG3AI.Guard local N=Mods.BG3AI.BG3AI.Nav local h=Osi.GetHostCharacter() "
    # 1. high-ground jump reach (S040: the game's jumps 18-28 m over 18-23 m drops; ours capped at 8.5 m)
    v = lua_server(
        g + "local p=G.pos(h) local r,flat=G.jump_reach(h,p,{p[1],p[2]-10,p[3]}) "
        'return string.format("%.1f|%.1f|%s", r, flat, tostring(G.HIGH_GROUND_JUMP_MULT))'
    )
    try:
        r, flat, mult = v.split("|")
        ok = abs(float(r) - float(flat) - 10) < 0.2
    except ValueError:
        ok = False
    item("vis: jump reach grows with the drop", "PASS" if ok else "FAIL", f"reach|flat|mult for a 10 m drop = {v}")
    # 2. a landing 10 m above the jumper is refused by the Guard (checked through check_cast, nothing is cast)
    v = lua_server(
        g + 'local p=G.pos(h) local ok,e=pcall(G.check_cast,h,"Projectile_Jump",nil,{p[1]+2,p[2]+10,p[3]},nil,true) '
        'return ok and "no refusal" or (G.is_refusal(e) and e.reason or tostring(e))'
    )
    item("vis: jump 10 m up refused", "PASS" if v != "no refusal" and "attempt to" not in v else "FAIL", v[:200])
    # 3. Feather Fall read (FEATHER_FALL; true only in the Shadowfell or under the spell)
    v = lua_server(g + 'return tostring(N.featherFall(h)).."|"..tostring(N.safeDrop(h))')
    item("vis: Feather Fall status read", "PASS" if re.fullmatch(r"(true\|60(\.0)?|false\|4(\.0)?)", v) else "FAIL", v)
    # 3b. jump statuses (V27, S086 the Astral Prism: Astral Gravity = jump x3, no fall damage; 1|false off the Prism,
    #     3|true in it): mult|noFall|jump_max|every status that names Astral Gravity or a Jump/Fall boost (pins its id)
    v = lua_server(
        g + "local m,nf=G.jump_status(h) local t={} "
        "for _,s in ipairs(Mods.BG3AI.BG3AI.Visibility.statuses(G.entity(h)) or {}) do "
        'local ok,b=pcall(function() return tostring(Ext.Stats.Get(s.id).Boosts or "") end) '
        'if s.name=="Astral Gravity" or (ok and (b:find("Jump") or b:find("Fall"))) then '
        't[#t+1]=s.id.."="..(ok and b or "?") end end '
        'return string.format("%s|%s|%.1f|%s",tostring(m),tostring(nf),G.jump_max(h),table.concat(t,";"))'
    )
    item(
        "vis: jump statuses (Astral Gravity x3, no fall damage)",
        "PASS" if re.match(r"(1(\.0)?\|false|3(\.0)?\|true)\|", v) else "FAIL",
        v[:300],
    )
    # 4. route to a point with no floor near it: NO WALKABLE PATH says why (S039 21:44 "pathfinding unavailable")
    p = (status().get("party") or [{}])[0].get("pos") or [0, 0, 0]
    out, dt = took("route", x=p[0] + 4, y=p[1] + 60, z=p[2])
    ok = "NO WALKABLE PATH: no" in out
    item("vis: route to a point in the air says why", "PASS" if ok else "FAIL", f"{dt:.1f}s; {out[:200]}")
    # 5. an objective handle as a target (S041 talk_to(npc="o1"))
    call("get_map")
    try:
        u = S._uuid("o1")
        item("vis: o1 as a target", "PASS", f"o1 -> {S.H.get(u)} ({S._name_of(u) if hasattr(S, '_name_of') else u})")
    except ValueError as e:
        item(
            "vis: o1 as a target",
            "PASS" if "objective marker" in str(e) or "no objective o1" in str(e) else "FAIL",
            str(e)[:200],
        )
    # 6. an empty name search names what was asked and who is in view (S042 kinds="Jaheira,characters")
    out = call("get_nearby", radius=60, kinds="Zzyzxq,characters")
    item("vis: empty name search says so", "PASS" if "No 'zzyzxq' in view within 60m" in out else "FAIL", out[:200])
    # 6b. no level ids in get_explored / look_at (S040 `level=SCL_Main_A`, S046 look_at `region=SCL_Main_A`)
    ex = call("get_explored")
    me_id = S.H.get((status().get("party") or [{}])[0].get("uuid") or "", None)
    la = call("look_at", target=me_id) if me_id else ""
    ids = sorted(set(re.findall(r"\b[A-Z]{3}_Main(?:_A)?\b|\bTUT_Avernus_C\b", ex + la)))
    item(
        "vis: no level ids in get_explored / look_at",
        "PASS" if not ids and ex else "FAIL",
        f"ids={ids}; explored: {first_line(ex)[:120]}; look_at: {first_line(la)[:80]}",
    )
    # 6c. the nav kill list: the current level not on it unless a jump really crashed the game (04:39 relaunch
    #     false positive on CTY_Main_A); nav.route in budget on an unreachable goal far below (check_all 03:50 258 ms)
    lg = S.game.try_rpc("nav.level_guard", {}) or {}
    here = lg.get("here")
    item(
        "vis: current level not on the nav kill list",
        "PASS" if not here or "jump" in str(here.get("why")) else "FAIL",
        f"level={lg.get('level')} here={here} slow={list((lg.get('slow') or {}) or [])}",
    )
    p = (status().get("party") or [{}])[0].get("pos") or [0, 0, 0]
    r = S.game.try_rpc("nav.route", {"pos": [p[0] + 60, p[1] - 40, p[2]], "connectors": True}) or {}
    ms = r.get("ms") or 0
    item(
        "vis: nav.route on a far-below goal in budget",
        "PASS" if ms < 150 else "FAIL",
        f"{ms} ms; reachable={r.get('reachable')} lower={'yes' if r.get('lower') else 'no'} skipped={r.get('lower_skipped')}",
    )
    # 7. journal: open quests without QuestIsAccepted (Find the Nightsong, companion quests) are listed
    jt = call("get_journal")  # also loads State.quests
    v = lua_server(
        "local who=Osi.GetHostCharacter() local n,names=0,{} "
        "for _,q in ipairs(Mods.BG3AI.BG3AI.State.quests or {}) do "
        "  if q.visible and Osi.QuestIsAccepted(who,q.id)~=1 and Osi.QuestIsClosed(q.id)~=1 then "
        "    for _,st in ipairs(q.steps) do if st.id and Mods.BG3AI.BG3AI.State.stepUnlocked(who,q,st.id) then "
        "      n=n+1 names[#names+1]=Ext.Loca.GetTranslatedString(q.title) or q.id break end end end end "
        'return n.."|"..table.concat(names,";")'
    )
    n, _, names = v.partition("|")
    if need(
        n.isdigit() and int(n) > 0,
        "vis: journal lists unaccepted open quests",
        f"no open quest without QuestIsAccepted at the base save ({v[:120]})",
    ):
        first = names.split(";")[0]
        item(
            "vis: journal lists unaccepted open quests",
            "PASS" if first and first in jt else "FAIL",
            f"{n} such quests ({names[:120]}); '{first}' in get_journal: {first in jt}",
        )
    # 8. wedged (S058 08:00, stuck in a nook after a pick_up): on open ground nav.wedged says no; with every path
    #    refused (N.path stubbed for one call, restored at once) it names a jump spot next to a party member
    w = S.game.try_rpc("nav.wedged", {}) or {}
    v = lua_server(
        g + "local P=N.path N.path=function() return {reachable=false} end "
        "local ok,w=pcall(N.wedged,h,500) N.path=P "
        'if not ok then return "error "..tostring(w) end if not w then return "nil" end '
        'return tostring(w.wedged).."|"..tostring(w.near).."|"..tostring(w.dist_m).."|"..tostring(w.in_reach)'
    )
    parts = v.split("|")
    ok = w.get("wedged") is False and parts[0] == "true"  # near=nil only when no member stands within 12 m
    item(
        "vis: wedged detection (open ground no; all paths refused -> jump next to a member)",
        "PASS" if ok else "FAIL",
        f"open ground: {w}; stubbed: {v[:160]}",
    )
    # 9. look_at on a door/container listed a moment ago and now out of sight (S055 e1174, S056 e603): examinable,
    #    out_of_sight (V.itemVisible stubbed false for one call, restored at once)
    #    S027-start is at camp: no door/container in get_frontier (window 20261016-183253), so a <usable> lead or a
    #    look listing counts too (V.STATIC_KINDS: the same staticSeen rule for every kind)
    fr = call("get_frontier")
    ids = static_lead_ids(fr) or static_lead_ids(call("look", full=True))
    if need(
        bool(ids),
        "vis: look_at on a door seen a moment ago",
        "no door/container/usable in get_frontier or look at the base save",
    ):
        u = S._uuid(ids[0])
        v = lua_server(
            "local B=Mods.BG3AI.BG3AI local V=B.Visibility local f=V.itemVisible "
            "V.itemVisible=function() return false end "
            f'local ok,o=pcall(B.RPC.ops["state.entity"],{{uuid="{u}"}}) V.itemVisible=f '
            'if not ok then return "error "..tostring(o) end return "out_of_sight="..tostring(o.out_of_sight)'
        )
        item(
            "vis: look_at on a door seen a moment ago",
            "PASS" if v == "out_of_sight=true" else "FAIL",
            f"{ids[0]}: {v[:160]}",
        )
    # 10. objective ids survive a re-sort (S059 08:53 route(objective="o3") walked to another objective): the live
    #     markers fetched, then the same list in reverse order (as after the party moved): o<first> is still it
    ms = S._objective_markers()
    if need(len(ms) >= 2, "vis: objective ids survive a re-sort", f"{len(ms)} objective markers at the base save"):
        oid, label = ms[0].get("oid"), S.render.objective_label(ms[0])
        S._set_objectives(list(reversed([{k: v for k, v in m.items() if k != "oid"} for m in ms])))
        got = S.render.objective_label(S.render.find_objective(S._objectives["last"], f"o{oid}"))
        item(
            "vis: objective ids survive a re-sort",
            "PASS" if got == label else "FAIL",
            f"o{oid} = {label!r} before, {got!r} after the reverse; ids {[m.get('oid') for m in S._objectives['last']]}",
        )
    # 11. pulled-up ladder (S059 08:56 e683, foot 5.7 m above the floor): N.ladderRaised reads the floor under a foot
    #     6 m above the party's feet as raised ~6 m and the party's own spot as not raised; no ladder in view whose
    #     foot a walk reaches is flagged (false positive)
    v = lua_server(
        g + 'local p=G.pos(h) return tostring(N.ladderRaised({p[1],p[2]+6,p[3]})).."|"..tostring(N.ladderRaised(p))'
    )
    up, _, here = v.partition("|")
    try:
        ok = abs(float(up) - 6) <= 0.7 and here == "nil"
    except ValueError:
        ok = False
    fr = S.game.try_rpc("nav.frontier", {"ways": False, "hazards": False, "radius": 40}) or {}
    bad = []
    for l in fr.get("leads") or []:
        if l.get("kind") == "ladder" and l.get("raised_m"):
            r = S.game.try_rpc("nav.path", {"pos": l.get("pos")}) or {}  # the foot itself
            if r.get("reachable") is True:
                bad.append(f"{l.get('name')} raised {l['raised_m']} but walkable")
    lads = [l for l in fr.get("leads") or [] if l.get("kind") == "ladder"]
    item(
        "vis: pulled-up ladder read (foot above the floor)",
        "PASS" if ok and not bad else "FAIL",
        f"6 m up -> {up}, here -> {here}; ladders in view {len(lads)}, false raised: {bad}",
    )
    # 12. a name search finds party members (S057 07:49 / S058 07:52 kinds="Shadowheart,Wyll" → "No 'shadowheart'"
    #     while both lay dead nearby): the last party member's name → a PARTY line with its id
    rows = status().get("party") or []
    nm = (rows[-1].get("name") or "") if rows else ""
    if need(bool(nm), "vis: name search finds party members", "no party in state.status"):
        out = call("get_nearby", radius=25, kinds=nm)
        item("vis: name search finds party members", "PASS" if "PARTY " in out and nm in out else "FAIL", out[:200])
    # 13. NAV-3 (audit 2026-10-13): the kill list is per playthrough (nav_guard_<host uuid>.json) and kills expire.
    #     Read-only: the save is the real playthrough's, so its file holds that playthrough's kills. Kills need the
    #     mod's wall clock (L8 2026-10-18: N.nowS read os.time, which the SE sandbox lacks: every kill undated); a
    #     load dates an undated kill, so any kill listed here must carry `t`.
    lg = S.game.try_rpc("nav.level_guard", {}) or {}
    f = str(lg.get("file") or "")
    dated = all(isinstance(v, dict) and v.get("t") for v in (lg.get("slow") or {}).values())
    now = lua_server(g + "return tostring(N.nowS())")
    skew = abs(float(now) - time.time()) if re.fullmatch(r"\d+(\.\d+)?", now) else None
    item(
        "vis: NAV-3 kill list per playthrough, kills dated",
        "PASS"
        if f.startswith("BG3AI/nav_guard_") and lg.get("ttl_s") and dated and skew is not None and skew < 120
        else "FAIL",
        f"file={f} ttl_s={lg.get('ttl_s')} clock={now} (skew {skew}) slow={lg.get('slow')}",
    )
    # 14. NAV-2: on a killed level nav.frontier searches <= 2 new directions and says so (the level is put back after
    #     unless it was already off)
    lvl, was = lg.get("level"), lg.get("here")
    if need(bool(lvl), "vis: NAV-2 killed level frontier throttled", "no current level"):
        # The kill is in memory only: the file (nav_guard_<host>.json) is the real playthrough's, and
        # nav.level_guard kill/clear would save it and wipe that level's strikes. The finally rewrites the file
        # from memory in case a strike saved it with our kill meanwhile.
        key = json.dumps(lvl)
        if not was:
            lua_server(g + f'N.guardState().slow[{key}] = {{why="acceptance NAV-2", t=N.nowS()}} return 1')
        try:
            lua_server(g + "N.waysCache, N.waysCacheN = {}, 0 return 1")
            fr = S.game.try_rpc("nav.frontier", {"hazards": False}) or {}
        finally:
            if not was:
                lua_server(
                    g + f"local gs = N.guardState() gs.slow[{key}] = nil "
                    "Ext.IO.SaveFile(N.guardFile(), Ext.Json.Stringify(gs, {Beautify = false})) return 1"
                )
        n = len(fr.get("ways") or [])
        item(
            "vis: NAV-2 killed level frontier throttled",
            "PASS" if n <= 2 and fr.get("ways_off") and (fr.get("ms") or 0) < 60 else "FAIL",
            f"ways={n} ways_off={fr.get('ways_off')} ms={fr.get('ms')}",
        )
    # 15. NAV-6: the arrival read (ways_cached) never searches: a cold spot answers fast
    lua_server(g + "N.waysCache, N.waysCacheN = {}, 0 return 1")
    fr = S.game.try_rpc("nav.frontier", {"ways_cached": True, "hazards": False}) or {}
    item(
        "vis: NAV-6 arrival frontier without cold searches",
        "PASS" if (fr.get("ms") or 999) < 30 else "FAIL",
        f"ms={fr.get('ms')} ways={len(fr.get('ways') or [])}",
    )
    # 16. NAV-7: a warm-up's answer leaves a jump's in-flight marker alone (markers per op)
    v = lua_server(
        g + 'local p=G.pos(h) N.inflight("jump",p) N.inflight("ways warm",p) N.inflightDone("ways warm") '
        'local ok=N.inflightOf("jump")~=nil N.inflightDone("jump") '
        'return tostring(ok).."|"..tostring(N.inflightOf("jump"))'
    )
    item("vis: NAV-7 in-flight markers per op", "PASS" if v == "true|nil" else "FAIL", v)
    # 17. result compaction (compact.py) on live results: every fact of the raw text survives (compact_check), the
    # second look is shorter (static lines, clock), and the MCP layer serves compacted plain text.
    from bg3ai import compact
    from bg3ai import compact_check as cc

    comp, shown, probs, sizes = compact.Compactor(), [], [], []
    for name, kw in (
        ("get_status", {"full": True}),
        ("look", {"full": True}),
        ("look", {"full": True}),
        ("get_nearby", {}),
        ("get_map", {}),
        ("get_frontier", {}),
    ):
        raw = call(name, **kw)
        out = comp.apply(name, raw)
        probs += [f"{name}: {p}" for p in cc.check(raw, out, comp.elided, "\n".join(shown))]
        shown.append(out)
        sizes.append((cc.est_tokens(raw), cc.est_tokens(out)))
    a, b = sum(x for x, _ in sizes), sum(y for _, y in sizes)
    item(
        "vis: result compaction keeps every fact (live)",
        "PASS" if not probs and b < a and sizes[2][1] <= sizes[1][1] else "FAIL",
        f"tokens ~{a}->~{b}, per call {sizes}; problems {probs[:3]}",
    )
    # 17b. render oddities of window 20261016-183253: one OBJECTIVE line per target (o1/o2), the CAMP line as text
    #      (no key=value dump), get_nearby after look full=true -> "(same as the look a moment ago: N lines)"
    st_txt = call("get_status", full=True)
    objs = [re.sub(r"^OBJECTIVE \S+ ", "", ln) for ln in st_txt.split("\n") if ln.startswith("OBJECTIVE ")]
    camp_ln = grab(r"^CAMP .*$", st_txt) or ""
    near_out = comp.apply("get_nearby", call("get_nearby"))
    ok = (
        len(objs) == len(set(objs))
        and "ALSO camp=" not in st_txt
        and "=" not in camp_ln
        and (not near_out.strip() or "same as" in near_out.split("\n")[0])
    )
    item(
        "vis: one OBJECTIVE line per target, CAMP line readable, repeated block referenced",
        "PASS" if ok else "FAIL",
        f"objective lines {len(objs)} ({len(set(objs))} distinct); {camp_ln[:140] or 'no CAMP line'}; "
        f"get_nearby after look: {first_line(near_out)[:80]}",
    )
    tool = S.mcp._tool_manager.get_tool("look")
    item(
        "vis: MCP results compacted plain text",
        "PASS" if getattr(tool.fn, "__compacted__", False) and tool.output_schema is None else "FAIL",
        f"wrapped={getattr(tool.fn, '__compacted__', False)} output_schema={tool.output_schema}",
    )
    # 18. EventEcho (render.py): a door interact's second UseFinished (`used`) is not shown again in the next result
    # (naut golden 21-22, 128-129: one door use = 2 identical `used` events, the 2nd in the next result)
    raw = S.game.try_rpc("state.nearby", {"radius": 8, "kinds": ["door"]}) or {}
    doors = [it for it in (raw.get("items") or []) if it.get("uuid")]
    if doors:
        h = S.H.get(doors[0]["uuid"], doors[0].get("name"))
        outs = [call("interact", target=h), call("look"), call("interact", target=h), call("look")]
        used = [ln.strip() for o in outs for ln in o.split("\n") if re.match(rf"\s+used\b.*\b{h}\b", ln)]
        item(
            "vis: door use shows one `used` event per interact (EventEcho)",
            "PASS" if len(used) <= 2 else "FAIL",
            f"{h} {doors[0].get('name')}: {len(used)} used lines over 2 interacts + 2 looks: {used[:4]}; "
            f"interacts: {first_line(outs[0])} / {first_line(outs[2])}",
        )
    else:
        item("vis: door use shows one `used` event per interact (EventEcho)", "INFO", "no door within 8 m of BASE")
    # 19. a queue_interrupted event whose last step result is multi-line arrives as ONE EVENTS line through the mod
    # (naut-r2 golden 129: `last=1. travel_to ..` then a stray `STOPPED: leg 1 failed` and a fake CHANGES block)
    who = next((r.get("uuid") for r in status().get("party") or [] if r.get("uuid")), None)
    if who:
        call("look")
        step = (
            "1. travel_to (1,2,3): 68m\nleg 1: move_to: FAILED — acceptance probe\nSTOPPED: leg 1 failed\n"
            "CHANGES\n  e2 Probe moved 3m W"
        )
        S._on_queue_report(who, S.actq.Report(done=0, stopped="acceptance probe", lines=[step]))
        out = call("look")
        ev = [ln for ln in out.split("\n") if "queue_interrupted" in ln]
        stray = [ln for ln in out.split("\n") if ln.startswith("STOPPED: leg 1")]
        item(
            "vis: queue_interrupted event on one line (live mod event path)",
            "PASS"
            if len(ev) == 1 and "FAILED — acceptance probe" in ev[0] and "done=0" in ev[0] and not stray
            else "FAIL",
            f"{ev[:1]} stray={stray[:1]}",
        )
    # 19b. V18 (S076 Gale asleep 3 rounds): the game's own stats give each disabling status its effect and fix
    #      (read-only: StatusGroups / RemoveEvents as SE exposes them, `using` inherited)
    v = lua_server(
        g + 'G._disabling = {} local out = {} for _, id in ipairs({"SLEEPING_EYEBITE", "HOLD_PERSON", '
        '"PRONE", "FRIGHTENED", "BLEEDING"}) do local i = G.disabling_info(id) '
        'out[#out + 1] = i and (tostring(i.effect) .. "/" .. tostring(i.fix)) or "-" end '
        'return table.concat(out, "|")'
    )
    want = (
        "cannot act/an adjacent ally's Help (an action) ends it; any damage ends it|cannot act/it saves again "
        "each turn|stands up at its turn start for half its movement/an adjacent ally's Help (an action) ends "
        "it|cannot move/nil|-"
    )
    item("vis: V18 disabling statuses read from the stats", "PASS" if v == want else "FAIL", v[:400])
    # 20. V10: Mystra's statue in get_nearby (loads its own save)
    mystra_statue_probe()
    load_verified(BASE)


# V10 (S069): the Tabernacle's god statues share one story pattern (Act3b_LOW_StormshoreTabernacle.txt
# DB_LOW_StormshoreTabernacle_SingleGods): Helm/Tyr/Selune were listed, Mystra's never was (get_nearby radius 6 from
# 1.4 m, a name search, interact as Gale: "target is not visible"). The probe reads, for Mystra's and Helm's statue,
# every step of V.itemVisible (flags, kind, CanInteract, bounds, the index, CanSee/LOS per member, the rays) and the
# server components one has and the other lacks. Read-only.
# L7 FAIL: Mystra's has a Bound but no AI bounds (V.bounds nil), so no ray rule ran; CanSee/LOS 0, every ray hit the
# statue first. V14 (V.unboundedPerceived, self-hit rays): expected PASS, with unbounded=true and aib (the raw AIBounds).
MYSTRA_STATUE = "ab6b7142-2333-4b38-9506-fb3e72157145"  # S_LOW_MystraStatue
HELM_STATUE = "858e4c3a-1575-44fa-9955-6ef6a96bff39"  # S_LOW_HelmStatue (listed in S069)
MYSTRA_ITEM = "vis: Mystra's statue listed in get_nearby next to it (V10, S069)"
STATUE_PROBE_LUA = (
    "local B=Mods.BG3AI.BG3AI local V,U=B.Visibility,B.Util local ctx=V.context() local out={} "
    "local function o(fn,...) local ok,r=pcall(fn,...) return ok and tostring(r) or 'err' end "
    "for _,u in ipairs(UUIDS) do local r={} out[u]=r local e=Ext.Entity.Get(u) "
    "if not e then r.entity='none' else "
    "local p=V.pos(e) local kind=V.itemKind(u,e) r.name=tostring(V.name(u)) r.kind=kind r.pos=p "
    "r.tpl=o(Osi.GetTemplate,u) r.onstage=o(Osi.IsOnStage,u) r.byscript=o(Osi.IsInvisibleByScript,u) "
    "r.invisible=o(Osi.IsInvisible,u) r.caninteract=o(Osi.GetCanInteract,u) r.trap=o(Osi.IsTrap,u) r.si={} "
    "for _,k in ipairs({'Invisible','Invisible2','Destroyed','OffStage','CanUse','CanBePickedUp','IsDoor',"
    "'IsLadder','IsContainer','StoryItem','IsSecretDoor'}) do "
    "local ok,v=pcall(function() return e.ServerItem[k] end) r.si[k]=ok and tostring(v) or 'err' end "
    "r.inventory=e.InventoryMember~=nil r.helper=V.helperItem(u) r.bound=e.Bound~=nil r.bounds=V.bounds(e) "
    "r.indexed=false if p then for _,x in ipairs(V.itemsAround(p,1)) do if x==u then r.indexed=true end end end "
    "r.obs={} for _,ob in ipairs(ctx.observers) do local s={name=V.name(ob.uuid),sight=ob.sight,"
    "see=o(Osi.CanSee,ob.uuid,u),los=o(Osi.HasLineOfSight,ob.uuid,u),rays={}} r.obs[#r.obs+1]=s "
    "if p and ob.pos then s.d=U.dist(ob.pos,p) local eye={ob.pos[1],ob.pos[2]+V.EYE_H,ob.pos[3]} "
    "for _,h in ipairs({0.5,1.0,1.6}) do local pt={p[1],p[2]+h,p[3]} "
    "local ok,hit=pcall(Ext.Level.RaycastClosest,eye,pt,V.RAY_TYPE,V.RAY_INCLUDE,V.RAY_EXCLUDE,0) "
    "local ray={h=h,want=U.dist(eye,pt)} s.rays[#s.rays+1]=ray "
    "if not ok then ray.err=tostring(hit) elseif hit then "
    "pcall(function() ray.at=U.dist(eye,{hit.Position[1],hit.Position[2],hit.Position[3]}) end) "
    "pcall(function() local he=hit.Shape.PhysicsObject.Entity ray.hit=he.Uuid.EntityUuid "
    "ray.hit_name=V.name(ray.hit) end) end end end end "
    "r.perceived=V.perceived(ctx,u,p) r.large=V.largePerceived(ctx,u,e,p,kind) "
    "r.fixture=V.fixturePerceived(ctx,u,e,p,kind) r.unbounded=V.unboundedPerceived and V.unboundedPerceived(ctx,u,e,p,kind) "
    "r.aib=o(function() local n,t=0,{} for k,d in pairs(e.Bound.Bound.AIBounds) do n=n+1 "
    "t[#t+1]=tostring(k)..'='..tostring(d.Height)..'/'..tostring(d.ActualHeight)..'/'..tostring(d.Radius) end "
    "return n..' '..table.concat(t,',') end) r.visible=V.itemVisible(ctx,u,e,p) "
    "r.known=V.itemKnown(ctx,u,e,p) r.guard=o(B.Guard.visible,u) "
    "local ok,names=pcall(function() return e:GetAllComponentNames(false) end) r.comps={} "
    "if ok and type(names)=='table' then for k,v in pairs(names) do "
    "r.comps[#r.comps+1]=type(v)=='string' and v or tostring(k) end end end end "
    "return Ext.Json.Stringify(out,{Beautify=false})"
)


def ray_brief(x: dict, u: str) -> str:
    """`h:clear`, `h:self` (the ray hit the item) or `h:<hit at>/<item at> <what it hit>` (m from the eye)."""
    if x.get("err"):
        return f"{x['h']}:err"
    if "at" not in x and "hit" not in x:
        return f"{x['h']}:clear"
    if x.get("hit") == u:
        return f"{x['h']}:self"
    return f"{x['h']}:{x.get('at') or 0:.1f}/{x.get('want') or 0:.1f} {x.get('hit_name') or x.get('hit')}"


def statue_brief(r: dict) -> str:
    """One statue's probe row, the lines that decide V.itemVisible first."""
    if not r or r.get("entity") == "none":
        return "no entity"
    obs = "; ".join(
        f"{s.get('name')} {s.get('d') or 0:.1f}m see={s.get('see')} los={s.get('los')} rays="
        + ",".join(ray_brief(x, r.get("u")) for x in s.get("rays") or [])
        for s in r.get("obs") or []
    )
    si = " ".join(f"{k}={v}" for k, v in (r.get("si") or {}).items() if v not in ("false", "nil"))
    return (
        f"{r.get('name')} kind={r.get('kind')} pos={[round(x, 1) for x in r.get('pos') or []]} "
        f"visible={r.get('visible')} known={r.get('known')} guard={r.get('guard')} | onstage={r.get('onstage')} "
        f"byscript={r.get('byscript')} invisible={r.get('invisible')} caninteract={r.get('caninteract')} "
        f"trap={r.get('trap')} si{{{si}}} inventory={r.get('inventory')} helper={r.get('helper')} "
        f"indexed={r.get('indexed')} bound={r.get('bound')} bounds={r.get('bounds')} | perceived={r.get('perceived')} "
        f"large={r.get('large')} fixture={r.get('fixture')} unbounded={r.get('unbounded')} aib={r.get('aib')} | {obs} | "
        f"tpl={r.get('tpl')}"
    )


def mystra_statue_probe() -> None:
    if not load_verified(MYSTRA_SAVE):
        item(MYSTRA_ITEM, "SKIP", f"{MYSTRA_SAVE} did not load")
        return
    near = call("get_nearby", radius=8)
    named = call("get_nearby", radius=20, kinds="Mystra")
    raw = lua_server(f"local UUIDS={{'{MYSTRA_STATUE}','{HELM_STATUE}'}} " + STATUE_PROBE_LUA)
    try:
        rows = json.loads(raw)
    except ValueError:
        item(MYSTRA_ITEM, "FAIL", f"probe error: {raw[:600]}")
        return
    for u in rows:
        rows[u]["u"] = u
    m, h = rows.get(MYSTRA_STATUE) or {}, rows.get(HELM_STATUE) or {}
    cm, ch = set(m.get("comps") or []), set(h.get("comps") or [])
    log(f"statue probe raw: {raw}")
    listed = "Statue of Mystra" in near and "Statue of Mystra" in named
    item(
        MYSTRA_ITEM,
        "PASS" if listed else "FAIL",
        f"radius 8: {'listed' if 'Statue of Mystra' in near else 'missing'}; kinds=Mystra: {first_line(named)[:100]} "
        f"|| MYSTRA {statue_brief(m)} || HELM {statue_brief(h)} || components only Mystra: {sorted(cm - ch)}; "
        f"only Helm: {sorted(ch - cm)}",
    )
