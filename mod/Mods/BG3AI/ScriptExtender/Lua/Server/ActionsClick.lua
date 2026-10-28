-- Actions, part 6 of 6 (Actions.lua loads the parts in order; split for the 1000-line cap):
-- click-on-entity flows (loot_corpse, lockpick, disarm, pickpocket), act.watch, spell prep reads, act.note, move_to
-- range's plan (A.range_point) and act.spell_reach.
local U = BG3AI.Util
local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = BG3AI.Actions
local I = A.inner
local reg, listen, pos_view, walk_eta, start = I.reg, I.listen, I.pos_view, I.walk_eta, I.start
local charge_movement, holder, begin_approach = I.charge_movement, I.holder, I.begin_approach
local walk_timeout, approach_tick, with_walk = I.walk_timeout, I.approach_tick, I.with_walk
local combat_budget, name_of = I.combat_budget, I.name_of

------------------------------------------------------------------------
-- Click-on-entity flows: act.loot_corpse / act.lockpick / act.disarm / act.pickpocket
--
-- The game starts these from a *click*: the client sends an interaction
-- request, the engine raises RequestCanLoot / RequestCanLockpick(char, item,
-- requestId) / RequestCanDisarmTrap, the story answers (LootRequestProcessed /
-- RequestProcessed(char, requestId, result)), and the engine then opens the
-- loot UI or the roll. The requestId belongs to the engine, so lockpicking and
-- disarming cannot be started from script: these ops walk the actor next to
-- the target, select it, and finish "done" with
--   {needs_click = uuid, expect = "container"|"roll"|"pickpocket", button = "L"}
-- for the bridge (bridge/src/bg3ai/clicker.py) to click and verify.
-- Exceptions with an engine path (no pixels):
--   * pickpocket: raise RequestPickpocket(char, npc), the engine's own event;
--     the story answers StartPickpocket(char, npc, 1|0) (_CRIME_CrimeTriggers).
--   * loot_corpse (when A.LOOT_ENGINE_SHORTCUT): OpenCharacterLootUI(char,
--     corpse), only when no story listener of RequestCanLoot would object
--     (no custom response, not an owned corpse, not Philgrave's lair).
------------------------------------------------------------------------

-- Verified live (Nautiloid): OpenCharacterLootUI makes the engine raise
-- RequestCanLoot itself (same story/crime handshake as a click), and
-- CharacterLootedCharacter fires when items are taken (Take All), as with a
-- click. The story-DB gate below stays as a conservative extra.
A.LOOT_ENGINE_SHORTCUT = true
-- No engine lockpick request: SE's esv::lock::LockpickingSystem.PickLockRequests is mapped 8 bytes off on
-- this build. Two pushes (live 12:2x, "0 -> 1 -> 2") wrote {capacity 2, size 2} over the game's own buffer
-- pointer (0x0000000200000002); the game's next real request (the right-click at 12:35) grew the array and
-- freed that pointer: SIGSEGV bg3+0x218f104 (core 1137503, impl-se 2026-10-10 crash analysis). Never write
-- engine system arrays through Ext.System. The right-click menu below is the S024 07:23 hand route.

-- Walk next to `target` (verified approach point; in combat within the
-- remaining Movement, which the walk is charged for), select `char`, then
-- call finish(job).
local function approach(kind, char, target, finish, extra)
    G.within(char, target, G.MAX_INTERACT_DIST, "target")
    local reach = extra and extra.reach or A.USE_REACH
    local plan = G.approach_point(char, target, reach)
    local budget, mv_before = combat_budget(char, plan.length_m)
    local spec = {
        eta_s = walk_eta(plan.length_m) + 0.5, route_m = plan.length_m,
        timeout_ms = 30000 + walk_timeout(plan.length_m),
        interrupt = { combat = budget == nil, dialogue = true, attacked = budget == nil },
        osi = with_walk(extra and extra.osi),
        start = function(job)
            begin_approach(job, target, plan, reach, function(j)
                pcall(G.osi, "MakePlayerActive", char)
                finish(j)
            end, budget)
        end,
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if extra and extra.on_tick then extra.on_tick(job) end
        end,
        on_finish = function(job) charge_movement(job, char, mv_before) end,
    }
    return start(kind, char, spec)
end

local function needs_click(job, target, expect, menu)
    RPC.finish_job(job, "done", { needs_click = target, expect = expect, button = menu and "R" or "L", menu = menu })
end

-- A combatant out of sight whose portrait has left the turn order (S033 18:39-18:41: Umbral Tremors read
-- "not visible" for 4 turns of retargets and skipped attacks): the party is in a fight and it isn't (dead,
-- off stage or gone). A player sees only that, never why (audit C9: "dead"/"off_stage" for an unseen
-- character got past Events.lua's drop of unseen deaths), and only for one that was on the bar.
local function left_fight(t)
    if G.in_combat(t) and not G.is_dead(t) and G.b(G.q("IsOnStage", t)) then return false end
    for _, p in ipairs(G.party()) do
        if G.in_combat(p) then return true end
    end
    return false
end
A.left_fight = left_fight

-- Read-only follow-up for the bridge after a click: what a player sees on the
-- target (locked padlock icon, open/closed, trap marker of a discovered trap).
-- act.landing_check {char, pos}: A.landing_ok for a teleport the bridge clicks itself (a Scroll of Misty Step read
-- and aimed at the ground: no act.cast checks it). {ok = true} or {ok = false, why}.
RPC.register("act.landing_check", function(args)
    local char = G.actor(args.char)
    local ok, err = pcall(A.landing_ok, char, nil, args.pos)
    if ok then return { ok = true } end
    if G.is_refusal(err) then return { ok = false, why = err.reason } end
    error(err, 0)
end)

RPC.register("act.target_state", function(args)
    local t = G.guid(args.uuid)
    if t and G.b(G.q("IsItem", t)) then
        local h = holder(t)
        if h and G.is_party(h) then return { visible = true, held = true } end
    end
    -- on_bar: the bridge saw it on the turn order (a combatant the party saw)
    if t and not G.is_party(t) and not G.b(G.q("IsItem", t)) and not G.visible(t) then
        return { visible = false, gone = args.on_bar and left_fight(t) and "left_fight" or nil }
    end
    if not t or not G.visible(t) then return { visible = false } end
    local out = { visible = true }
    if G.b(G.q("IsItem", t)) then
        local h = holder(t)
        if h and G.is_party(h) then out.held = true end
        out.locked = G.b(G.q("IsLocked", t))
        out.opened = G.b(G.q("IsOpened", t))
        if G.b(G.q("IsTrapDiscovered", t)) then out.trap_armed = G.b(G.q("IsTrapArmed", t)) end
    else
        out.dead = G.is_dead(t)
    end
    return out
end)

reg("loot_corpse", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local corpse = G.guid(args.corpse or args.target) -- bridge sends {target}
    if not corpse or not G.b(G.q("IsCharacter", corpse)) then G.refuse("that is not a body") end
    if not G.is_dead(corpse) then G.refuse("they are not dead") end
    if G.is_party(corpse) then G.refuse("cannot loot a party member") end
    G.target_visible(corpse)
    local story_cares = G.db_has("DB_CustomLootCorpseResponse", 3, char, corpse)
        or G.db_has("DB_OwnedCorpse", 2, corpse)
        or G.db_has("DB_LOW_PhilgravesMansion_LairEntities", 1, corpse)
    local engine = A.LOOT_ENGINE_SHORTCUT and not story_cares and not G.in_combat(char)
    return approach("loot_corpse", char, corpse, function(job)
        if not engine then return needs_click(job, corpse, "container") end
        local ok = G.osi("OpenCharacterLootUI", char, corpse)
        job.opened_at = RPC.now_ms()
        if ok == false then RPC.finish_job(job, "failed", nil, "cannot loot that") end
    end, {
        osi = {
            CharacterLootedCharacter = function(job, a)
                if G.same(a[1], char) and G.same(a[2], corpse) then job.looted_event = true end
            end,
        },
        on_tick = function(job)
            if job.opened_at and RPC.now_ms() - job.opened_at > 300 then
                RPC.finish_job(job, "done", { opened = "container", via = "engine",
                    looted_event = job.looted_event or false })
            end
        end,
    })
end)
listen("CharacterLootedCharacter", 2)

reg("lockpick", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local target = G.guid(args.target)
    if not target or not G.b(G.q("IsItem", target)) then G.refuse("that cannot be lockpicked") end
    G.target_visible(target)
    if not G.b(G.q("IsLocked", target)) then G.refuse("it is not locked") end
    local h = G.party_key(target, char)
    if h then
        local ok, n = pcall(function() return BG3AI.Visibility.name(h) end)
        G.refuse((ok and n or "a party member") .. " carries its key: use interact on it (opens it with the key)")
    end
    return approach("lockpick", char, target, function(job) needs_click(job, target, "roll", "Lockpick") end)
end)
listen("StartedLockpicking", 2)

reg("disarm", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local trap = G.guid(args.trap or args.target)
    if not trap or not G.b(G.q("IsItem", trap)) then G.refuse("that is not a trap") end
    -- Only traps the party has discovered (passive Perception / search).
    if not G.b(G.q("IsTrap", trap)) or not G.b(G.q("IsTrapDiscovered", trap)) then
        G.refuse("no known trap there")
    end
    -- No sight check: a trap the game marks discovered is one the party spotted (the game outlines it), wherever the
    -- spotter stands now. S064 11:37: the walk stopped for e302 Sanguine Vent 10 m away ("TRAP spotted on the way"),
    -- then disarm(e302) was refused "target is not visible" (S065 11:40 the same). The approach walks up to it.
    -- Right-click menu "Disarm", as for Lockpick (S031 17:10: three left-clicks on a trapped bookcase did nothing;
    -- the agent's right-click -> Disarm by hand was the route).
    return approach("disarm", char, trap, function(job) needs_click(job, trap, "roll", "Disarm") end)
end)

reg("pickpocket", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local npc = G.guid(args.npc or args.target)
    if not npc or not G.b(G.q("IsCharacter", npc)) or G.is_dead(npc) then G.refuse("nobody to pickpocket") end
    if G.is_party(npc) then G.refuse("cannot pickpocket a party member") end
    G.target_visible(npc)
    return approach("pickpocket", char, npc, function(job)
        job.requested_at = RPC.now_ms()
        G.osi("RequestPickpocket", char, npc)
    end, {
        -- Live (setup 14:xx, Last Light Inn): every Harper behind the counter was "no walkable path" with the
        -- 1.8 m use reach (rings 1.0/1.5 m only). Stand off like talk_to: the 2.2 m ring reaches over a counter.
        reach = G.TALK_RANGE,
        on_tick = function(job)
            -- Server-side sign of the pickpocket screen: the ServerPickpocket
            -- component on the actor (UNVERIFIED); otherwise hand over to the
            -- bridge to confirm with ui.state.
            if not job.requested_at then return end
            local e = G.entity(char)
            if e and U.comp(e, "ServerPickpocket") then
                RPC.finish_job(job, "done", { opened = "pickpocket", via = "engine" })
            elseif RPC.now_ms() - job.requested_at > 1500 then
                RPC.finish_job(job, "done", { requested = true, expect = "pickpocket", verify_ui = true })
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.watch {radius?}: one cheap read for the bridge's action queue (its
-- interrupt checks) and the do_actions diff. Only what the player sees: the
-- party's portraits (pos, HP, downed, a harmful surface underfoot), the mode (combat / conversation), and
-- the characters in view (visibility-filtered, as in state.nearby).
-- act.note {kind, data}: the bridge's queue reports (queue_*) go into the
-- same event stream as the game's events.
------------------------------------------------------------------------

A.WATCH_RADIUS = 30
RPC.register("act.watch", function(args)
    local radius = tonumber(args.radius) or A.WATCH_RADIUS
    local out = { party = {}, seen = {}, combat = false, dialogue = false }
    local centers = {}
    for _, u in ipairs(G.party()) do
        local row = { uuid = u, pos = pos_view(G.pos(u)) }
        local e = G.entity(u)
        local h = e and U.comp(e, "Health")
        if h then pcall(function() row.hp = h.Hp; row.max_hp = h.MaxHp end) end
        if G.is_downed(u) then row.downed = true end
        -- on a ladder: the bridge's queue starts the step after a climb only once the climber is off it (actq)
        if G.b(G.q("HasActiveStatus", u, "CLIMBING")) then row.climbing = true end
        -- a harmful surface underfoot the player sees (fire, acid, ...): the queue's "surface" interrupt
        local N = BG3AI.Nav
        if row.pos and type(N) == "table" and type(N.surfaceAt) == "function" then
            local ok, sf = pcall(N.surfaceAt, G.pos(u))
            -- not Chasm: a jumper over a gap reads Chasm in the air, and the queue cancelled the jump ("STOPPED: e4
            -- Claude is standing in Chasm", S040 22:04:40 / 22:06:25: two Shadowfell jumps; the same jump_to alone landed)
            if ok and sf and sf ~= "Chasm" then row.surface = sf end
        end
        if G.in_combat(u) then out.combat = true end
        if G.in_dialogue(u) then out.dialogue = true end
        if row.pos then centers[#centers + 1] = row.pos end
        out.party[#out.party + 1] = row
    end
    local V = BG3AI.Visibility
    if #centers == 0 or type(V) ~= "table" or not V.context or not V.characterVisible or not V.charactersNear then return out end
    local ctx = V.context()
    local host = G.host() or G.party()[1]
    -- the per-frame shared scan (state.nearby in the same eval reuses it)
    for _, c in ipairs(V.charactersNear(centers, radius)) do
        local e, p = c[1], c[2]
        local u = V.entityUuid(e)
        if u and not ctx.is_party[u] and V.characterVisible(ctx, u, e, p) then
            local row = { uuid = u, name = V.name and V.name(u) or nil }
            if G.is_dead(u) then row.dead = true
            elseif host and G.b(G.q("IsEnemy", host, u)) then row.hostile = true end
            out.seen[#out.seen + 1] = row
        end
    end
    return out
end)

------------------------------------------------------------------------
-- act.prepare_plan {char, add = [spell], remove = [spell]} (A5): the spellbook change prepare_spells will make,
--   checked first (Guard.prep_plan); {refused = reason} instead of an error. Read-only: the change itself is the
--   spellbook UI's (Client/Spellbook.lua ui.prepare_spells).
-- act.prepared {uuids?}: each preparing caster's prepared spells and limit (party by default). Read-only.
------------------------------------------------------------------------

RPC.register("act.prepare_plan", function(args)
    local ok, res = pcall(G.prep_plan, args.char, args.add, args.remove)
    if ok then return res end
    if G.is_refusal(res) then return { refused = res.reason } end
    error(res, 0)
end)

-- rows without the page look (it rides only in a plan, for the page's cells; this answer goes out every tick)
local function plain_rows(rows)
    local out = {}
    for i, r in ipairs(rows) do out[i] = { id = r.id, name = r.name, level = r.level } end
    return out
end

RPC.register("act.prepared", function(args)
    local out = {}
    for _, u in ipairs(args.uuids or G.party()) do
        local g = G.guid(u)
        local info = g and G.prep_info(g)
        if info and info.prepares then
            out[#out + 1] = { uuid = g, name = name_of(g), max = info.max, count = info.count, read = info.read,
                              prepared = plain_rows(info.prepared), always = plain_rows(info.always),
                              classes = info.classes, stats_max = info.stats_max, class_max = info.class_max }
        end
    end
    return { casters = out }
end)

-- act.scroll_settle {char, final?} (A7): after a combat scroll's target clicks. Once the scroll is used up (gone from
-- the reader's bags, or its stack smaller) it charges what reading it owed and the engine did not -> {read = true,
-- charged = [{resource, amount}]}. Not used up yet: {read = false, casting = true while the reader's cast plays} (the
-- pending entry stays until final=true). At final=true a scroll still there whose cast went off (CastedSpell) is read.
-- approaching = true while the reader walks (moved over SCROLL_WALK_M since the last poll): app review of #31, a
-- touch-range scroll clicked on a member 8-10 m off makes the reader walk into range before the cast starts.
A.SCROLL_WALK_M = 0.05
RPC.register("act.scroll_settle", function(args)
    local char = G.guid(args.char)
    local p = char and A.scroll_pending[char]
    if not p then return { read = false, none = true } end
    local left = 0
    if G.b(G.q("IsItem", p.item)) and G.same(holder(p.item), char) then
        left = tonumber((G.q("GetStackAmount", p.item))) or 1
    end
    if left >= p.stack and not (args.final and p.casted) then
        if args.final then A.scroll_pending[char] = nil end
        local here = G.pos(char)
        local walking = here and p.last_pos and BG3AI.Util.dist(here, p.last_pos) > A.SCROLL_WALK_M or nil
        p.last_pos = here or p.last_pos
        return { read = false, casting = (p.cast and not p.casted) or nil, approaching = walking }
    end
    A.scroll_pending[char] = nil
    local after, charged = G.resources(char), {}
    for _, q in ipairs(p.payers) do
        local owed = q.amount - (q.before - ((after[q.name] or {})[q.level] or 0))
        if owed > 0.01 and G.deduct(char, q.name, q.level, owed) then
            charged[#charged + 1] = { resource = G.friendly(q.name), amount = owed }
        end
    end
    return { read = true, charged = charged }
end)

RPC.register("act.note", function(args)
    local kind = tostring(args.kind or "")
    if not kind:match("^queue_[%w_]+$") then error("act.note: only queue_* events") end
    RPC.push(kind, args.data)
    return { ok = true }
end)

-- move_to {target, range}: a ranged spell's reach (centres; app review of #37: a Scroll of Fireball read 20 m from a
-- goblin walked the reader to the 2.2 m approach ring, inside its own blast). The walk ends on the way in, `reach` m
-- from the target (RANGE_STOP_SLACK inside the range): the walker stops itself there (ActionsMove walk_tick, stop_at).
A.RANGE_STOP_SLACK = 1.0
-- Live N10-N13 (Claude 9.4 m from Wyll, Longstrider's 2.5 m touch reach): each walk to the point 1.5 m from him, the
-- stop point itself, ended 1.8-2.4 m short of it, 3.3-3.9 m off, out of reach. The walk's goal lies RANGE_PAST_M
-- beyond the stop point (past the target for a short reach, never nearer its centre than RANGE_BODY_M), on the line
-- toward the walker, then 22.5 and 45 degrees off it; that goal's path may be at most RANGE_DETOUR_M longer than the
-- straight line (else it is a way round, not in). When none is walkable, the stop point itself is the goal, as before.
A.RANGE_PAST_M = 4.0
A.RANGE_BODY_M = 1.0
A.RANGE_DETOUR_M = 3.0
-- Live N11 step 2: the first leg ended 3.3 m from Wyll, 1.8 m short of its point; the second leg's path to the same
-- point had no nodes (0.0 m) and the walk ordered on it went 10.2 m round, ending 7.8 m off. A point more than
-- RANGE_END_TOL_M away whose path has no nodes is not one the engine walks to: the next angle is tried. (A normal path
-- ends ~1 m from its goal: that is not this.)
A.RANGE_END_TOL_M = 1.0
-- App review of night2/vis: the goal past the target is ground the walker may reach when it does not stop in time, so
-- it is used only when that ground is clear: no harmful surface from the target to it or around it, no known floor
-- plate, discovered trap (PAST_TRAP_M) or warned area (args.avoid) on that stretch; else the stop point is the goal.
A.PAST_TRAP_M = 2.0
local function past_clear(a, t, goal, avoid)
    local N, seg = BG3AI.Nav, { t, goal }
    if N and N.routeHazard then
        local ok, h = pcall(N.routeHazard, seg, 0)
        if not ok or h then return false end
    end
    if N and N.surfaceNear then
        local ok, sf = pcall(N.surfaceNear, goal)
        if not ok or sf then return false end
    end
    local rows = A.known_plates(t, goal)
    for _, ar in ipairs(A.warned_areas(avoid, a, goal)) do rows[#rows + 1] = ar end
    for u in pairs((BG3AI.Events or {}).knownTraps or {}) do
        local p = G.pos(u)
        if p then rows[#rows + 1] = { uuid = u, pos = p, r = A.PAST_TRAP_M } end
    end
    return #A.plates_on(seg, rows, {}) == 0
end

-- -> {here, dist} | {pos (the goal), stop (the stop point: move_to's area/plate checks), length_m, stop_m (the route
-- to the stop point: a combat walk's budget), dist, tpos}
function A.range_point(char, target, reach, range, avoid)
    local a, t = G.pos(char), G.pos(target)
    if not a or not t then G.refuse("cannot locate target") end
    local d = U.dist(a, t)
    if d <= range then return { here = true, dist = d, length_m = 0, tpos = t } end
    local base = math.atan(a[3] - t[3], a[1] - t[1])
    local sx, sz = t[1] + reach * math.cos(base), t[3] + reach * math.sin(base)
    local stop = { sx, G.floor_at(sx, sz, t[2]) or t[2], sz }
    local past = reach - A.RANGE_PAST_M
    if past < A.RANGE_BODY_M then past = math.min(past, -A.RANGE_BODY_M) end
    local short
    for _, r in ipairs({ past, reach }) do
        for _, k in ipairs({ 0, 1, -1, 2, -2 }) do
            local ang = base + k * math.pi / 8
            local x, z = t[1] + r * math.cos(ang), t[3] + r * math.sin(ang)
            local y = G.floor_at(x, z, t[2])
            if y and (r ~= past or past_clear(a, t, { x, y, z }, avoid)) then
                local p = { x, y, z }
                local ok, info = G.reachable(char, p)
                local off = ok and info.no_nodes and U.dist(a, p) or 0
                local round = r == past and ok and (info.length_m or 0) > U.dist(a, p) + A.RANGE_DETOUR_M
                if ok and not info.needs_jump and off > A.RANGE_END_TOL_M then
                    short = math.min(short or off, off)
                elseif ok and not info.needs_jump and not round then
                    local len = info.length_m or U.dist(a, p)
                    if r ~= past then return { pos = p, stop = p, length_m = len, stop_m = len, dist = d, tpos = t } end
                    local sok, sinfo = G.reachable(char, stop)
                    return { pos = p, stop = stop, length_m = len, dist = d, tpos = t,
                        stop_m = sok and sinfo.length_m or math.max(U.dist(a, stop), len - U.dist(stop, p)) }
                end
            end
        end
    end
    if short then
        G.refuse(string.format("no walkable point %.1f m from the target on this side (its range): the game's paths to"
            .. " them have no nodes (they end where the character stands, %.1f m or more from them)", reach, short))
    end
    G.refuse(string.format("no walkable point %.1f m from the target on this side (its range)", reach))
end

-- act.spell_reach {char, spell, target} -> {range_m, melee, reach_m, dist_m} | {}
-- Live N9 step 3: a touch-range Scroll of Longstrider read 9.47 m from Wyll: the click on Wyll landed, the game cast
-- nothing and Claude never walked. The bridge reads the spell in hand (ui.cast_state) and asks how far it reaches
-- from the reader to the target (G.spell_range; touch/melee spells edge to edge, G.melee_reach), centre to centre.
RPC.register("act.spell_reach", function(args)
    local char, target = G.guid(args.char), G.guid(args.target)
    local ok, st = pcall(Ext.Stats.Get, tostring(args.spell or ""))
    local range, melee = G.spell_range(ok and st or nil, char)
    local p, q = char and G.pos(char), target and G.pos(target)
    if not range or not p or not q then return {} end
    return { range_m = range, melee = melee or false, dist_m = U.round(U.dist(p, q), 2),
        reach_m = U.round(melee and G.melee_reach(char, target, range) or range, 2) }
end)
