-- Actions, part 4 of 6 (Actions.lua loads the parts in order; split for the 1000-line cap):
-- act.cast.
local U = BG3AI.Util
local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = BG3AI.Actions
local I = A.inner
local reg, walk_eta, start, begin_approach = I.reg, I.walk_eta, I.start, I.begin_approach
local walk_timeout, approach_tick, with_walk = I.walk_timeout, I.approach_tick, I.with_walk

------------------------------------------------------------------------
-- act.cast {char, spell, target? | pos? | targets?, upcast?}   (untested live)
------------------------------------------------------------------------

-- A spell picked on one creature, then aimed at a ground spot (Teleportation with OriginTargetConditions:
-- Revivify and its scroll, Resurrection, Dimension Door on an ally...). The UI asks for two clicks.
function A.two_step_spell(spell)
    local ok, r = pcall(function()
        local st = spell and Ext.Stats.Get(spell)
        return st ~= nil and tostring(st.SpellType) == "Teleportation"
            and tostring(st.OriginTargetConditions or "") ~= "" and tostring(st.TeleportSelf or "") ~= "Yes"
    end)
    return ok and r or false
end

-- A two-step cast that stands a body up (Revivify, Resurrection): its first pick must be dead/downed.
function A.revive_spell(spell)
    local ok, r = pcall(function()
        local st = spell and Ext.Stats.Get(spell)
        return st ~= nil and tostring(st.OriginTargetConditions or ""):find("Dead()", 1, true) ~= nil
    end)
    return ok and r or false
end

-- How far a two-step teleport's landing may be (stats AreaRadius; Dimension Door 50, Revivify 9). nil = unknown.
function A.landing_range(spell)
    local ok, r = pcall(function() return tonumber(Ext.Stats.Get(spell).AreaRadius) end)
    return ok and r and r > 0 and r or nil
end

-- A one-step teleport to a point (Misty Step: SpellType Target, SpellProperties "GROUND:TeleportSource()" in Shared
-- Spell_Target.txt; Target_MistyStep_4/_Free and the racial ones use it). bg3se gives SpellProperties as functor
-- tables (shape UNVERIFIED: any "TeleportSource" string in them counts); the id names the usual ones if that fails.
A.TELEPORT_ID = { "MistyStep", "ThunderStep", "FarStep", "ShadowStep", "Teleport" }
local function teleport_functor(v, depth)
    if type(v) == "string" then return v:find("TeleportSource", 1, true) ~= nil end
    if depth > 4 or (type(v) ~= "table" and type(v) ~= "userdata") then return false end
    local found = false
    pcall(function()
        for _, x in pairs(v) do if teleport_functor(x, depth + 1) then found = true; break end end
    end)
    return found
end
function A.point_teleport(spell)
    if type(spell) ~= "string" or A.two_step_spell(spell) or A.revive_spell(spell) then return false end
    local ok, st = pcall(Ext.Stats.Get, spell)
    if not ok or not st then return false end
    if tostring(st.SpellType or "") == "Teleportation" then return true end
    local okp, props = pcall(function() return st.SpellProperties end)
    if okp and teleport_functor(props, 0) then return true end
    for _, k in ipairs(A.TELEPORT_ID) do if spell:find(k, 1, true) then return true end end
    return false
end

-- A Target spell that takes only a creature or an object. S094 10:18, S095 11:04/11:06: Karsus' Compulsion
-- (Target_END_Mindflayer_CrownDomination, TargetConditions Tagged('END_CROWN_OF_KARSUS'), no AreaRadius) cast at a
-- ground point read "done — no_damage" and did nothing (no Channelling, no portal), twice. A point takes a Target
-- spell only as an area (AreaRadius), a teleport, a CannotTargetCharacter spell, or one whose functors act there (a
-- GROUND context, a summon/spawn, a surface, an explosion: GustavDev Spell_Target.txt, 1405 of ~2100 Target entries
-- have none of these). nil = can't tell (functors with no readable string).
A.POINT_FUNCTOR = { "summon", "spawn", "surface", "teleportsource", "explosion" }
local function functor_words(v, depth, out)
    if type(v) == "string" then out[#out + 1] = v:lower(); return end
    if depth > 5 or (type(v) ~= "table" and type(v) ~= "userdata") then return end
    pcall(function()
        for k, x in pairs(v) do
            if type(k) == "string" then out[#out + 1] = k:lower() end
            functor_words(x, depth + 1, out)
        end
    end)
end
function A.needs_entity(spell)
    if type(spell) ~= "string" or A.two_step_spell(spell) or A.point_teleport(spell) then return false end
    local ok, st = pcall(Ext.Stats.Get, spell)
    if not ok or not st or tostring(st.SpellType or "") ~= "Target" then return false end
    if (tonumber(st.AreaRadius) or 0) > 0 then return false end
    local flags = {}
    pcall(function() for _, f in ipairs(st.SpellFlags) do flags[f] = true end end)
    if flags.CannotTargetCharacter then return false end
    local words = {}
    for _, field in ipairs({ "SpellProperties", "SpellSuccess" }) do
        local okp, props = pcall(function() return st[field] end)
        if okp then functor_words(props, 0, words) end
    end
    if #words == 0 then return nil end
    for _, w in ipairs(words) do
        if w == "ground" then return false end
        for _, f in ipairs(A.POINT_FUNCTOR) do if w:find(f, 1, true) then return false end end
    end
    return true
end

-- The landing of a teleport (Dimension Door's second pick, Misty Step's point): within its range and on a floor
-- (stats TargetConditions CanStand; the game's cursor shows no landing in the air), and that floor not a Chasm.
-- S093 09:47 (the Netherbrain's psyche): Misty Step at (230.5,-55.7,4260.5), 1.5 m off the platform's edge, had no
-- check; Gale fell to y -73.1 (the void's bottom, a Chasm floor like the Astral Prism's, vis V27) and died.
A.LANDING_FLOOR_SLACK_M = 2.0
A.LANDING_CHASM_MARGIN_M = 0.5 -- a Chasm this close to the landing (an edge): the lander can slip in
local function chasm_at(p)
    local N = BG3AI.Nav
    if not (N and N.surfaceNear) then return false end
    local ok, s = pcall(N.surfaceNear, p, A.LANDING_CHASM_MARGIN_M)
    return ok and s == "Chasm"
end
function A.landing_ok(char, spell, dest)
    G.point_ok(char, dest)
    local p, rng = G.pos(char), A.landing_range(spell)
    if p and rng and U.dist(p, dest) > rng + 0.5 then
        G.refuse(string.format("the landing is out of range (%.1f m, range %.1f m): move closer first",
            U.dist(p, dest), rng))
    end
    -- Over the Faith-Leap chasm at the walkway's height (Navigation.lua N.helperZone): the game decides, as for move_to
    local N = BG3AI.Nav
    if N and N.helperZone and N.helperZone(dest) then return end
    local ok, hs = pcall(Ext.Level.GetHeightsAt, dest[1], dest[3])
    if not ok or type(hs) ~= "table" then return end -- can't tell
    local near, chasm = false, false
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - dest[2]) <= A.LANDING_FLOOR_SLACK_M then
            if chasm_at({ dest[1], h, dest[3] }) then chasm = true else near = true end
        end
    end
    if near then return end
    if #hs == 0 then
        G.refuse(string.format("no ground at all under the landing (%.1f,%.1f,%.1f) (the void): pick a floor you can"
            .. " see", dest[1], dest[2], dest[3]))
    end
    if chasm then
        G.refuse(string.format("the landing (%.1f,%.1f,%.1f) is in a Chasm or within %.1f m of one (the void): a fall"
            .. " there is fatal; pick a spot on the floor you stand on or can see, away from its edge",
            dest[1], dest[2], dest[3], A.LANDING_CHASM_MARGIN_M))
    end
    -- the floors named below are only ones the party sees or has walked (N.known); others go unnamed
    local best, void
    for _, h in ipairs(hs) do
        if type(h) == "number" and (not best or math.abs(h - dest[2]) < math.abs(best - dest[2]))
            and N and N.known and N.known(nil, { dest[1], h, dest[3] }) then
            if chasm_at({ dest[1], h, dest[3] }) then void = void or h else best = h end
        end
    end
    G.refuse(string.format("the landing (%.1f,%.1f,%.1f) is in the air: no floor within %.0f m of that height",
        dest[1], dest[2], dest[3], A.LANDING_FLOOR_SLACK_M)
        .. (best and string.format(" (the nearest floor there is at y=%.1f); give the floor's own y", best)
            or void and string.format(" (the only floor there is the void's bottom at y=%.1f, a Chasm: a fall there"
                .. " is fatal); pick a spot on the floor you stand on or can see", void)
            or "; give the floor's own y"))
end

-- A teleport's caster has gone from where the cast was ordered (or died / went down there): it was cast, whatever
-- event the engine sent.
A.TELEPORT_MOVED_M = 1.0
function A.teleport_moved(job, char)
    local p = G.pos(char)
    if p and job.ordered_pos and U.dist(p, job.ordered_pos) > A.TELEPORT_MOVED_M then return true end
    return G.is_dead(char) or G.is_downed(char) or false
end

-- The ground spot for a two-step cast: beside the body, A.PLACE_M toward the caster (the body itself is a
-- character, and the second step's TargetConditions are "not Character()").
A.PLACE_M = 1.2
function A.place_spot(char, target)
    local a, b = G.pos(target), G.pos(char)
    if not a then return nil end
    if not b then return { a[1], a[2], a[3] } end
    local dx, dz = b[1] - a[1], b[3] - a[3]
    local d = math.sqrt(dx * dx + dz * dz)
    if d < 0.01 then return { a[1], a[2], a[3] } end
    local k = math.min(A.PLACE_M, d) / d
    return { a[1] + dx * k, a[2], a[3] + dz * k }
end

-- Writing TargetPos2 into the engine's queued task goes through a struct mapping (bg3se OsirisTask.h) never proven
-- on this build, and the read-back uses the same mapping, so a wrong offset would pass it: the lockpick write that
-- corrupted the heap was this class (HANDOFF §7, audit ACT-9). OFF until a live check proves the layout
-- (acceptance "revivify two-step spot set", opt-in); meanwhile a two-step spell cast is refused with the routes
-- that work (a scroll is clicked through the UI). Turn on for a probe: Mods.BG3AI.BG3AI.Actions.TASK_POS2 = true
A.TASK_POS2 = false
A.TASK_POS2_OFF = "the engine-task write for a second pick is off (audit ACT-9)"

-- Give the queued UseSpell task its second target (OsirisUseSpellTask.TargetPos2, bg3se OsirisTask.h). Returns
-- true, or nil + why. Read-checked before the write; any proxy error is caught. Nothing is written while
-- A.TASK_POS2 is off.
function A.set_task_pos2(char, spell, p)
    if not A.TASK_POS2 then return nil, A.TASK_POS2_OFF end
    local ok, err = pcall(function()
        local tasks = Ext.Entity.Get(char).ServerCharacter.OsirisController.Tasks
        local t
        for i = #tasks, 1, -1 do
            local ok2, sp = pcall(function() return tasks[i].Spell end)
            local id = ok2 and sp and (type(sp) == "string" and sp or sp.OriginatorPrototype or sp.Prototype)
            if id == spell then t = tasks[i] break end
        end
        if not t then error("no queued spell task", 0) end
        t.TargetPos2 = { p[1], p[2], p[3] }
        local q = t.TargetPos2
        if not q or math.abs((q[1] or 0) - p[1]) > 0.01 then error("the task kept no TargetPos2", 0) end
    end)
    if ok then return true end
    return nil, tostring(err)
end

-- After UseSpell(first pick): a two-step cast's second pick goes into the queued task. Revivify: the spot beside
-- the body; a teleport (Dimension Door): the landing given.
-- clicked: the hotbar route (act.cast ui=true) gives the second pick by a click, nothing is written; only the
-- result's landing / revive report is set up.
function A.give_second_pick(job, char, spell, targets, landing, clicked)
    if targets[2] or not targets[1] or not A.two_step_spell(spell) then return end
    if clicked then
        job.place = landing and { U.round(landing[1], 1), U.round(landing[2], 1), U.round(landing[3], 1) } or nil
        job.teleport = landing and true or nil
        job.revive = (not landing and A.revive_spell(spell)) or nil
        return
    end
    local p = landing or A.place_spot(char, targets[1])
    local ok2, why = nil, "the body's position is unknown"
    if p then ok2, why = A.set_task_pos2(char, spell, p) end
    A.last_place_why = (not ok2) and why or nil
    job.place = p and { U.round(p[1], 1), U.round(p[2], 1), U.round(p[3], 1) } or nil
    job.teleport = landing and true or nil
end

local function name_of(uuid)
    local V = BG3AI.Visibility
    if V and V.name then
        local ok, n = pcall(V.name, uuid)
        if ok and n then return n end
    end
    return U.loca(G.q("GetDisplayName", uuid))
end

local function hp_of(uuid)
    local e = G.entity(uuid)
    local h = e and U.comp(e, "Health")
    local ok, hp = pcall(function() return h.Hp end)
    return ok and hp or nil
end

-- A30: the enemy cast seen during our cast, as a player sees it. An unseen caster is "someone", as the combat log
-- shows it: not named, and not called an enemy either (audit C23).
function A.react_name(rx)
    if not G.visible(rx.caster) then return "someone's reaction cast" end
    local ok, st = pcall(Ext.Stats.Get, rx.spell)
    local spell = ok and st and U.loca(st.DisplayName) or nil
    local who = name_of(rx.caster)
    if who and spell then return who .. "'s " .. spell .. " (an enemy reaction)" end
    return "an enemy's reaction cast"
end

-- A30: what of a cast's cost (check.payers) the game took since the check's snapshot, e.g. "action"; nil = nothing.
function A.cost_spent(char, check)
    if not (check and check.payers and check.snapshot) then return nil end
    local now = G.resources(char)
    for _, p in ipairs(check.payers) do
        local before = (check.snapshot[p.name] or {})[p.level] or 0
        if before - ((now[p.name] or {})[p.level] or 0) > 0.01 then return G.friendly(p.name) end
    end
    return nil
end

local NULL_GUID = "NULL_00000000-0000-0000-0000-000000000000"

-- A27: a variant of a call-allies container (Gather Your Allies' groups) summons allies at its point.
function A.ally_call(spell)
    local st = Ext.Stats.Get(spell)
    local box = st and tostring(st.SpellContainerID or "") or ""
    local bst = box ~= "" and Ext.Stats.Get(box)
    local call = false
    pcall(function() for _, f in ipairs(bst.SpellFlags) do if f == "CallAlliesSpell" then call = true end end end)
    return call
end

-- Living characters within ALLY_CALL_R of p: {uuid = true} (a group's call needs combat: nobody else walks in
-- while it is cast, so whoever is new there came with it, party or not).
function A.callers_near(p)
    local V, out = BG3AI.Visibility, {}
    if not (p and V and V.charactersNear and V.entityUuid) then return out end
    for _, row in ipairs(V.charactersNear(p, A.ALLY_CALL_R)) do
        local u = V.entityUuid(row[1])
        if u and not G.is_dead(u) then out[u] = true end
    end
    return out
end

local function resolve_spell(spell, upcast)
    if upcast then
        local variant = spell .. "_" .. tostring(upcast)
        local st = Ext.Stats.Get(variant)
        if not st or st.RootSpellID ~= spell then G.refuse("that spell cannot be cast at that level") end
        return variant
    end
    return spell
end

-- act.ui_clicked {id}: the bridge's hotbar clicks for that act.cast {ui=true} job are done (A17). A CastSpellFailed
-- held from before then fails the job CAST_UI_REFUSED_MS later unless the cast starts; one after it fails it at once.
RPC.register("act.ui_clicked", function(args)
    local job = RPC.jobs[tonumber(args.id)]
    if not job then error("no such job " .. tostring(args.id)) end
    if job.ui and job.status == "running" then job.clicked_ms = RPC.now_ms() end
    return { status = job.status, held = job.ui_failed }
end)

reg("cast", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    G.cast_options_ok(args.options) -- callers may not request Ignore*/Forced options
    local base = args.spell
    if type(base) ~= "string" then G.refuse("no action given") end
    if args.which ~= nil and tostring(args.which) ~= "" then
        if not Ext.Stats.Get(base) then G.refuse("unknown action " .. base) end
        base = G.pick_variant(char, base, args.which) -- A27: Gather Your Allies, which = "The Watch"
        if A.ally_call(base) and not G.in_combat(char) then
            G.refuse("an ally group is called only in a fight (the game's own rule)")
        end
    end

    local targets = {}
    if args.target then targets[1] = G.guid(args.target) end
    for _, t in ipairs(args.targets or {}) do targets[#targets + 1] = G.guid(t) end
    -- ui: the player's route (hotbar cell, one click per target, the tick), clicked by the bridge once this job runs
    local ui = args.ui == true
    if ui then
        -- #38 review: a hotbar cast is not parked for a running stand-up (Actions.lua park): the bridge clicks as soon
        -- as the reply comes, so the clicks would land during the stand-up and the cast run later would get none
        local up = A.active[char]
        if up and up.status == "running" and up.kind == "act.end_standup" then
            G.refuse((name_of(char) or "the character") .. " is still standing up from the revive: not cast")
        end
        -- a two-step spell (ACT-9's player route): Revivify = the body, then the ground beside it; Dimension Door =
        -- who goes (the caster when nobody is named), then the landing given as pos. The bridge clicks both.
        local two = A.two_step_spell(base)
        local tele_land = two and args.pos ~= nil and not A.revive_spell(base)
        if #targets < 1 and not tele_land then G.refuse("a hotbar cast needs its targets") end
        if #targets > A.CAST_UI_MAX_TARGETS then
            G.refuse("more than " .. A.CAST_UI_MAX_TARGETS .. " targets in one cast")
        end
        if args.pos and not tele_land then G.refuse("a hotbar cast clicks characters, not a position") end
        if args.upcast then G.refuse("an upcast through the hotbar's level picker is not built yet") end
        if two and A.revive_spell(base) and #targets ~= 1 then
            G.refuse(tostring(base) .. " through the hotbar clicks one body, then the ground beside it")
        end
    elseif #targets > 2 then
        G.refuse("more than two targets is not supported yet")
    end
    for _, t in ipairs(targets) do if not t then G.refuse("bad target") end end
    -- night2 (live N4, N7-N12 vs N10): every stuck plain C1 replay had Shadowheart's Cure Wounds on the revived Claude
    -- ~5 s into his stand-up and his layer-1 action never finished; N10's probe, with nothing aimed at him, stood up at
    -- 8.3 s. A cast at another member whose stand-up job runs waits for it as his own orders do (start(): standup_of);
    -- a hotbar cast is refused (its clicks come as soon as the reply does).
    local standup_of
    for _, t in ipairs(targets) do
        local up = not G.same(t, char) and A.active[t] or nil
        if up and up.status == "running" and up.kind == "act.end_standup" then
            if ui then
                G.refuse((name_of(t) or "the character") .. " is still standing up from the revive: not cast on them")
            end
            standup_of = up
        end
    end
    local pos = args.pos
    if not Ext.Stats.Get(base) then
        -- e.g. Shout_Dodge: BG3 has no Dodge action (live: no such stats entry).
        G.refuse("unknown action " .. base .. " (not an ability id; get_actions lists them)")
    end
    -- S059 08:57-08:59, Dimension Door x3 (targets=Claude + a point, or a point alone): a two-step teleport picks
    -- who goes (an ally within its TargetRadius 2 m, or the caster), then the landing (AreaRadius). The point is the
    -- landing, never the first pick: it was checked against the 2 m pick range and cast at a position with nobody
    -- to send (the game refused), or replaced by the Revivify spot beside the ally.
    local landing
    if pos and A.two_step_spell(base) and not A.revive_spell(base) then
        landing, pos = pos, nil
        if #targets == 0 then targets[1] = char end -- nobody named: the caster goes alone
        if #targets > 1 then G.refuse("a teleport takes one who goes (the caster or an ally beside them), then the landing") end
    end
    -- ACT-9: with the second-pick write off, a two-step cast would be ordered with its first pick only: the game
    -- then waits for the second click (or refuses), and the next action cancels it. Refused before any cost.
    if A.two_step_spell(base) and not targets[2] and not A.TASK_POS2 and not ui then
        if A.revive_spell(base) then
            G.refuse(tostring(base) .. " is a two-step cast (the body, then the spot they stand up on) and this"
                .. " action can't give its second pick yet. Read a Scroll of Revivify by name (use_ability"
                .. " ability=\"Scroll of Revivify\" target=<the body>: it clicks the body, then the ground beside it),"
                .. " or Withers at camp")
        end
        G.refuse(tostring(base) .. " is a two-step teleport (who goes, then the landing) and this action can't give"
            .. " its landing yet: walk, jump or climb there (route),")
    end
    if #targets == 0 and not pos then
        local st = Ext.Stats.Get(base)
        local ground = false
        pcall(function() for _, f in ipairs(st.SpellFlags) do if f == "CannotTargetCharacter" then ground = true end end end)
        if ground then G.refuse("this action needs x,y,z: a ground point (it can't target a character)") end
        if tostring(st.SpellType) ~= "Shout" then G.refuse("this action needs a target") end
        targets[1] = char -- self-centred actions (Dash, Hide, Disengage...)
    end
    if pos and #targets == 0 and not ui and A.needs_entity(base) then
        G.refuse(tostring(base) .. " takes a creature or an object as its target, not a ground point (cast at the"
            .. " point it does nothing): give target=<its id> (get_nearby lists objects too)")
    end

    -- Upcasting to the spell's own level is just a normal cast.
    local upcast = args.upcast and tonumber(args.upcast)
    local bst = Ext.Stats.Get(base)
    if upcast and bst and upcast == (tonumber(bst.Level) or 0) then upcast = nil end
    -- S030: Guiding Bolt upcast 2 was refused ("cannot be cast at that level"), a wasted call: the game data has
    -- no Projectile_GuidingBolt_2 (nor Inflict Wounds / Chromatic Orb variants), and UseSpell has no slot-level
    -- input. Cast it at its own level and say so.
    local upcast_ignored
    if upcast and bst and not Ext.Stats.Get(base .. "_" .. tostring(upcast)) then
        upcast_ignored = "no level-" .. tostring(upcast) .. " form of this spell can be cast here: cast at level "
            .. tostring(tonumber(bst.Level) or 0)
        upcast = nil
    end

    -- Validate the base spell (known/prepared/cooldown) and the cast itself;
    -- with an upcast, slots are checked at the upcast level (not the base
    -- level, which may be spent).
    local spell = resolve_spell(base, upcast)
    local function prepare()
        local ok0, c = pcall(G.check_cast, char, base, targets[1], pos, upcast, args.force)
        if not ok0 then
            if landing and G.is_refusal(c) and c.reason:find("^target is out of range") then
                G.refuse(c.reason .. " (the ally who goes with the caster must stand beside them; leave targets"
                    .. " empty to go alone)")
            end
            error(c, 0)
        end
        if landing then A.landing_ok(char, base, landing)
        elseif pos and A.point_teleport(base) then A.landing_ok(char, base, pos) end
        for i = 2, #targets do
            -- every later pick passes the first one's checks (seen, near, in range); a repeat = another dart/beam
            local t = targets[i]
            G.target_visible(t)
            G.within(char, t, G.MAX_INTERACT_DIST, "target")
            if c.range and U.dist(G.pos(char), G.pos(t)) > c.range + 0.5 then
                G.refuse((i == 2 and "second" or ("target " .. i)) .. " is out of range: move closer first")
            end
        end
        if spell ~= base then
            -- Resources for the upcast variant's own costs.
            local vst = Ext.Stats.Get(spell)
            c.costs = G.parse_costs(vst.UseCosts)
            c.payers = {}
            local in_combat = G.in_combat(char)
            for _, cc in ipairs(c.costs) do
                local turn_res = cc.name == "ActionPoint" or cc.name == "BonusActionPoint"
                    or cc.name == "Movement" or cc.name == "ReactionActionPoint"
                if in_combat or not turn_res then
                    local n, l = G.payer(c.snapshot, cc)
                    if not n then G.refuse("not enough " .. G.friendly(cc.name)) end
                    c.payers[#c.payers + 1] = { name = n, level = l, amount = cc.amount }
                end
            end
        end
        return c
    end
    -- Out of combat, a cast refused only for its range walks into range first, as a click on the ability
    -- does in the game (S018-S028: ~1-3 per session, e.g. S028 Help on a downed Shadowheart 5 m away
    -- stopped a do_actions batch). Our own verified walk, never the engine's (WithoutMove stays 1).
    local ok, check = pcall(prepare)
    local walk
    if not ok then
        local why = G.is_refusal(check) and check.reason or ""
        if why:find("^target is out of range") and targets[1] and not targets[2] and not pos and not ui
            and not G.in_combat(char) and not G.same(char, targets[1]) then
            -- Touch range only (Help, Cure Wounds, a melee attack): approach_point stands beside the target. A
            -- ranged cast keeps the refusal: closing in on a hostile is the agent's call (move_to within).
            local rng, melee = G.spell_range(bst, char)
            local reach = rng and (melee and G.melee_reach(char, targets[1], rng) - A.MELEE_STOP_SLACK or rng - 0.3)
            if reach and reach <= A.CAST_WALK_MAX_REACH then
                reach = math.max(1.2, reach)
                walk = { plan = G.approach_point(char, targets[1], reach), reach = reach }
            end
        end
        if not walk then error(check, 0) end
        check = nil
    end
    local combat = G.in_combat(char)
    -- Never let the cast walk: the Osiris spell task's approach has no path
    -- check (teleports). The range was checked by G.check_cast whenever it is
    -- known; only an unknown range out of combat lets the engine walk into range.
    local without_move = (combat or walk or check.range) and 1 or 0
    local harmful = false
    pcall(function()
        for _, f in ipairs(bst.SpellFlags) do if f == "IsHarmful" then harmful = true end end
    end)

    local function matches(a)
        return G.same(a[1], char) and (a[2] == spell or a[2] == base)
    end
    -- A projectile (arrow, bolt, Fire Bolt) lands after CastedSpell: acceptance 14:5x, a Ranged Attack at
    -- 18 m reported no_damage while its 2 damage arrived later (the agent read a miss). Wait for its hits.
    local settle = A.CAST_SETTLE_MS
    if base:find("^Projectile_") or (bst and bst.SpellType == "Projectile") then
        settle = A.PROJECTILE_SETTLE_MS
    end

    local no_start_ms = (combat and without_move == 1 and base ~= "Projectile_Jump") and A.CAST_NO_START_COMBAT_MS
        or A.CAST_NO_START_MS
    if ui then no_start_ms = A.CAST_UI_NO_START_MS end
    local walk_m = walk and walk.plan.length_m or 0
    local function order(job)
        job.osi_task = "spell"
        job.ordered_ms, job.ordered_pos = RPC.now_ms(), G.pos(char)
        if pos then
            if A.ally_call(spell) then job.ally_before = A.callers_near(pos) end
            if A.point_teleport(base) then -- the result says where the caster landed (A.landing_report)
                job.place, job.teleport = { U.round(pos[1], 1), U.round(pos[2], 1), U.round(pos[3], 1) }, true
            end
            -- Engine form has 6 inputs (6th = WithoutMove), verified live
            -- on bg3le; the 5-input form is kept as a fallback for builds
            -- without it.
            G.osi_forms("UseSpellAtPosition", { char, spell, pos[1], pos[2], pos[3], without_move },
                { char, spell, pos[1], pos[2], pos[3] })
        else
            G.osi("UseSpell", char, spell, targets[1], targets[2] or NULL_GUID, without_move)
            A.give_second_pick(job, char, spell, targets, landing)
        end
    end
    return start("cast", char, {
        standup_of = standup_of,
        eta_s = 2.0 + (walk and walk_eta(walk_m) or 0),
        timeout_ms = 30000 + (walk and walk_timeout(walk_m) or 0),
        interrupt = { combat = walk ~= nil, dialogue = true, attacked = false },
        -- a hotbar cast cancelled by the bridge after the game began it (a late start): say so (the bridge then never
        -- casts it again by Osiris) and leave the player's own cast alone (no purge)
        on_cancel = ui and function(job)
            job.stopping = true
            if job.began then RPC.finish_job(job, "interrupted", { began = true, cast = spell }, "cancelled") end
        end or nil,
        osi = with_walk({
            CastSpell = function(job, a)
                -- the HP the jump verdict compares with, read as the cast starts: a hit taken after the order (or
                -- before a re-order) is not the fall's
                if matches(a) and not job.began then job.began, job.hp0 = true, hp_of(char) end
            end,
            CastedSpell = function(job, a)
                if matches(a) and not job.casted then
                    -- Hits usually land before CastedSpell; give stragglers a
                    -- short settle window (see on_tick), then report.
                    job.casted = RPC.now_ms()
                    G.item_used(char, spell)
                end
            end,
            CastSpellFailed = function(job, a)
                if not matches(a) then return end
                if job.teleport and job.place and not job.casted and A.teleport_moved(job, char) then
                    -- S093 09:47: Misty Step "FAILED — the game refused the cast" while Gale had gone (and fell to
                    -- her death): the caster moved, so it was cast; on_tick reports where they landed
                    job.casted, job.failed_event = RPC.now_ms(), true
                    return
                end
                local why = A.cast_failed_why(char, targets[1], spell)
                if job.ui and not job.clicked_ms and not job.began then
                    -- L8 23:59 (se, Dimension Door): picking the level in the upcast picker sent this 0.12 s
                    -- before the first target click, and the job failed while the bridge went on clicking. In the
                    -- hotbar route it is held until the bridge's clicks are done (act.ui_clicked).
                    job.ui_failed = job.ui_failed or why
                    return
                end
                RPC.finish_job(job, "failed", nil, why)
            end,
            -- AttackedBy(defender, attackerOwner, attacker, damageType, amount, cause, storyActionId)
            AttackedBy = function(job, a)
                if G.same(a[3], char) or G.same(a[2], char) then
                    local tgt = G.guid(a[1])
                    if tgt and G.visible(tgt) then
                        job.hits[#job.hits + 1] = { target = tgt, name = name_of(tgt),
                            amount = tonumber(a[5]), type = a[4] ~= "None" and a[4] or nil }
                        job.last_hit = RPC.now_ms()
                    end
                end
            end,
            -- StatusApplied(target, status, causee, storyActionId): visible statuses only.
            StatusApplied = function(job, a)
                if G.same(a[3], char) then
                    local tgt = G.guid(a[1])
                    local V = BG3AI.Visibility
                    local info = V and V.statusInfo and V.statusInfo(tostring(a[2]))
                    if tgt and info and G.visible(tgt) then
                        job.statuses[#job.statuses + 1] = { target = tgt, status = info.name }
                    end
                end
            end,
        }),
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if job.ui_failed and job.clicked_ms and not job.casted and not job.began
                and RPC.now_ms() - job.clicked_ms >= A.CAST_UI_REFUSED_MS then
                RPC.finish_job(job, "failed", nil, job.ui_failed)
                return
            end
            if not job.casted and not job.began and job.ordered_ms then
                local p = G.pos(char)
                if p and job.ordered_pos and U.dist(p, job.ordered_pos) > 0.5 then
                    job.began = true -- the engine walks into range / the jump took off
                elseif RPC.now_ms() - job.ordered_ms > no_start_ms then
                    local now = RPC.now_ms()
                    local rx = combat and not job.ui and A.react_seen
                    if rx and rx.t < job.ordered_ms - A.CAST_HIT_WINDOW_MS then rx = nil end
                    if rx and now - job.ordered_ms < A.CAST_REACT_HOLD_MS
                        and (not rx.done or now - rx.done < A.CAST_REACT_SETTLE_MS) then
                        return -- an enemy's reaction to it still plays: the game may start ours after it
                    end
                    local hit = combat and not job.ui and A.hit_ms[G.guid(char)]
                    if hit and hit.t < job.ordered_ms - A.CAST_HIT_WINDOW_MS then hit = nil end
                    local spent = rx and A.cost_spent(char, check)
                    if not hit and rx and not spent then hit = { t = rx.t, why = A.react_name(rx) } end
                    if hit and not job.reordered then
                        job.reordered, job.reorder_why = (hit.t - job.ordered_ms) / 1000, hit.why
                        pcall(G.osi, "PurgeOsirisQueue", char)
                        order(job)
                        return
                    end
                    local why = job.ui_failed or ("the game never started it ("
                        .. tostring(math.floor(no_start_ms / 1000)) .. " s, no cast, no step)" .. A.held_text(char))
                    if job.reordered then
                        why = why .. " twice: ordered again after " .. job.reorder_why .. " "
                            .. string.format("%.1f s %s the first order", math.abs(job.reordered),
                                job.reordered < 0 and "before" or "after")
                    end
                    if spent then
                        why = why .. ": " .. A.react_name(rx) .. " came just then and the game took the "
                            .. spent .. ": most likely countered"
                    end
                    if base == "Projectile_Jump" then
                        why = why .. ": it ignores a landing the jump can't reach (too far or too high for this"
                            .. " character, or no clear arc): pick a nearer, lower landing (route / get_layout)"
                    end
                    local blk = combat and G.blocking_status(char)
                    if blk then
                        why = why .. ": the caster is " .. tostring(blk.name) .. (blk.type == "FEAR"
                            and " (a fear status: the game dropped orders at such a turn's start before; nothing"
                                .. " was spent)" or " (it can't act while that lasts)")
                    end
                    pcall(G.osi, "PurgeOsirisQueue", char)
                    RPC.finish_job(job, "failed", nil, why)
                    return
                end
            end
            local now = job.casted and RPC.now_ms()
            -- Report CAST_SETTLE_MS after the cast once the hits are in (250 ms after the last one), at
            -- the latest `settle` after it (a projectile still in flight, or a miss).
            if now and now - job.casted >= A.CAST_SETTLE_MS
                and ((job.last_hit and now - job.last_hit >= A.CAST_SETTLE_MS) or now - job.casted >= settle) then
                local landing
                local jump = base == "Projectile_Jump" and pos
                local dest = jump or (job.teleport and job.place)
                if dest and job.ordered_pos then
                    local p = G.pos(char)
                    local last = job.jump_last
                    job.jump_last = p
                    -- still in the air (moved since the last tick): wait for the landing, up to a cap
                    if p and (not last or U.dist(p, last) > 0.05) and now - job.casted < settle + A.JUMP_AIRBORNE_MAX_MS then
                        return
                    end
                    if p and not jump then
                        -- a teleport: a landing far below its point is a fall (the void), reported with its outcome
                        local drop = dest[2] - p[2]
                        if drop > A.JUMP_FALL_M then
                            job.fell_ms = job.fell_ms or now
                            if now - job.fell_ms < A.JUMP_FALL_CHECK_MS then return end -- the fall damage lands
                            local dead, downed = G.is_dead(char), G.is_downed(char)
                            if dead or downed then
                                RPC.finish_job(job, "failed", { cast = spell, fell_m = U.round(drop, 1),
                                    now_at = U.vec(p, 1), landing = dest },
                                    string.format("teleported, then FELL %.1fm below the landing (to y=%.1f) and %s:"
                                        .. " the point had no floor under it (the void or an edge). %s Teleport only"
                                        .. " onto a floor you stand on or can see, away from its edge",
                                        drop, p[2], dead and "DIED" or "is DOWNED",
                                        dead and "Revive them or load a save." or "Help them up."))
                                return
                            end
                            job.fell_m = U.round(drop, 1)
                        end
                    elseif p then
                        local function flat(a, b) return math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2) end
                        local off, moved = flat(p, pos), flat(p, job.ordered_pos)
                        -- how far the jumper ended from the arc (the flat line from the start to the landing): a
                        -- jump the game shortened or that went nowhere ends on it, a fall that put the body back
                        -- elsewhere (job 13) off it
                        local o, ax, az = job.ordered_pos, pos[1] - job.ordered_pos[1], pos[3] - job.ordered_pos[3]
                        local len2 = ax * ax + az * az
                        local t = len2 > 0 and ((p[1] - o[1]) * ax + (p[3] - o[3]) * az) / len2 or 0
                        t = math.max(0, math.min(1, t))
                        local fell = flat(p, { o[1] + t * ax, 0, o[3] + t * az }) > A.JUMP_OFF_M
                        local drop = (pos[2] or p[2]) - p[2]
                        -- Bench B3 job 13 16:47:04: a jump at a Chasm's edge fell in, the game put the body back on
                        -- the ledge 18.6 m from the landing at the jumper's own height, dead (HP 18->0), and jump_to
                        -- said "done": no drop to read. A jump that ends off its landing or below it waits for the
                        -- fall damage, then death, a down or lost HP is the verdict, whatever the height.
                        if off > A.JUMP_OFF_M or drop > A.JUMP_FALL_M then
                            job.fell_ms = job.fell_ms or now
                            if now - job.fell_ms < A.JUMP_FALL_CHECK_MS then return end -- the fall damage lands
                        end
                        local dead, downed = G.is_dead(char), G.is_downed(char)
                        local hp = hp_of(char)
                        local lost = (job.hp0 and hp) and job.hp0 - hp or 0
                        if drop <= A.JUMP_FALL_M and (dead or downed or (fell and lost > 0)) then
                            local verdict = dead and "DIED" or downed and "is DOWNED" or ("lost " .. lost .. " HP")
                            local hps = string.format("HP %s->%s", tostring(job.hp0 or "?"), tostring(hp or "?"))
                            local why = fell and string.format("FELL on the way and %s: ended at"
                                .. " (%.1f,%.1f,%.1f), %.1fm from the landing (%s)", verdict, p[1], p[2], p[3], off,
                                hps)
                                or string.format("landed and %s (%s) at (%.1f,%.1f,%.1f)", verdict, hps,
                                    p[1], p[2], p[3])
                            RPC.finish_job(job, "failed", { cast = spell, now_at = U.vec(p, 1),
                                landing = U.vec(pos, 1), off_m = U.round(off, 1), hp = hp, hp_before = job.hp0 }, why)
                            return
                        end
                        if off > A.JUMP_OFF_M and moved < A.JUMP_NOWHERE_M then
                            RPC.finish_job(job, "failed", { cast = spell, moved_m = U.round(moved, 1) },
                                string.format("the jump went nowhere: moved %.1fm, the landing is still %.1fm away"
                                    .. " (the game shortens a landing it can't reach: too far or too high, no clear arc,"
                                    .. " or no floor there): pick a nearer, lower spot on solid floor (get_layout)",
                                    moved, off))
                            return
                        elseif off > A.JUMP_OFF_M then
                            landing = U.round(off, 1)
                        end
                        if drop > A.JUMP_FALL_M then
                            if dead or downed then
                                RPC.finish_job(job, "failed", { cast = spell, fell_m = U.round(drop, 1) },
                                    string.format("FELL %.1fm below the landing (to y=%.1f) and %s: the landing had no"
                                        .. " floor at that height (a chasm or a drop). %s Never jump there again; walk"
                                        .. " (route / get_layout) or aim at solid floor at your own height",
                                        drop, p[2], dead and "DIED" or "is DOWNED",
                                        dead and "Load a save or revive them." or "Help them up."))
                                return
                            end
                            job.fell_m = U.round(drop, 1)
                        end
                    end
                end
                local allies
                if job.ally_before then
                    allies = {}
                    for u in pairs(A.callers_near(pos)) do
                        if not job.ally_before[u] then allies[#allies + 1] = { uuid = u, name = name_of(u) } end
                    end
                    if #allies == 0 and now - job.casted < A.ALLY_CALL_WAIT_MS then return end
                    table.sort(allies, function(x, y) return x.uuid < y.uuid end)
                end
                local res = { cast = spell, extra_attack = check.extra_attack, walked_into_range = job.walked,
                    upcast_ignored = upcast_ignored, landed_m_from_target = landing, fell_m = job.fell_m,
                    via = ui and "hotbar" or nil, targets_n = ui and #targets or nil }
                if #job.hits > 0 then
                    res.damage = job.hits
                    local total = 0
                    for _, h in ipairs(job.hits) do total = total + (h.amount or 0) end
                    res.total_damage = total
                elseif harmful then
                    res.no_damage = true -- missed, saved, or no damage component
                end
                if #job.statuses > 0 then res.statuses = job.statuses end
                if allies then
                    res.allies = #allies > 0 and allies or nil
                    res.no_allies = #allies == 0 and string.format("nobody arrived within %.0f m of the point %.0f s"
                        .. " after the cast", A.ALLY_CALL_R, A.ALLY_CALL_WAIT_MS / 1000) or nil
                end
                if job.teleport and job.place then
                    -- Dimension Door: where the caster stands now vs the landing asked for.
                    local p = G.pos(char)
                    res.landing = job.place
                    if p then
                        res.now_at = { U.round(p[1], 1), U.round(p[2], 1), U.round(p[3], 1) }
                        res.landed_m_from_target = U.round(U.dist(p, job.place), 1)
                        res.teleported = (job.ordered_pos and U.dist(p, job.ordered_pos) > 1.0) or false
                    end
                end
                -- Target state as the target's health bar shows it (if visible).
                if targets[1] and not G.same(targets[1], char) and G.visible(targets[1]) then
                    res.target = { uuid = targets[1], dead = G.is_dead(targets[1]) or nil,
                        downed = G.is_downed(targets[1]) or nil }
                    if job.place and not job.teleport then
                        res.placed_at = job.place
                        res.revived = not res.target.dead or nil
                    elseif job.revive then
                        res.revived = not res.target.dead or nil
                    end
                end
                RPC.finish_job(job, "done", res)
            end
        end,
        start = function(job)
            job.hits, job.statuses = {}, {}
            if walk then
                return begin_approach(job, targets[1], walk.plan, walk.reach, function(j)
                    local ok2, c = pcall(prepare)
                    if not ok2 then
                        RPC.finish_job(j, "failed", { walked = U.round(walk_m, 1) },
                            G.is_refusal(c) and c.reason or tostring(c))
                        return
                    end
                    check = c
                    j.walked = U.round(walk_m, 1)
                    j.osi_task = "spell"
                    j.ordered_ms, j.ordered_pos = RPC.now_ms(), G.pos(char)
                    G.osi("UseSpell", char, spell, targets[1], NULL_GUID, without_move)
                    A.give_second_pick(j, char, spell, targets, landing)
                end)
            end
            if ui then
                -- no order: the bridge clicks the hotbar now (the player's input); CastSpell/CastedSpell tell the rest
                job.ui = true
                job.ordered_ms, job.ordered_pos = RPC.now_ms(), G.pos(char)
                A.give_second_pick(job, char, spell, targets, landing, true)
                return
            end
            -- S039 21:53: the engine froze inside a jump cast and the crash left no trace: mark it in flight, so a
            -- crash during the jump turns this level's jump planning off at the next start (Navigation.lua guard)
            if pos and base == "Projectile_Jump" and BG3AI.Nav and BG3AI.Nav.inflight then
                job.jump_marked = pcall(BG3AI.Nav.inflight, "jump", pos)
            end
            order(job)
        end,
        on_finish = function(job)
            if job.jump_marked then pcall(BG3AI.Nav.inflightDone, "jump") end -- NAV-7: only its own marker
            if not job.casted then return end
            -- A hotbar cast is the player's own: the game charged it (nothing extra from us).
            if job.ui then return end
            -- Resource audit: charge whatever the engine did not.
            local after = G.resources(char)
            local charged = {}
            for _, p in ipairs(check.payers) do
                local before = (check.snapshot[p.name] or {})[p.level] or 0
                local now = (after[p.name] or {})[p.level] or 0
                local owed = p.amount - (before - now)
                if owed > 0.01 and G.deduct(char, p.name, p.level, owed) then
                    charged[#charged + 1] = { resource = G.friendly(p.name), level = p.level > 0 and p.level or nil, amount = owed }
                end
            end
            if #charged > 0 and type(job.result) == "table" then job.result.charged_by_guard = charged end
        end,
    })
end)

A.inner.name_of, A.inner.NULL_GUID = name_of, NULL_GUID
