-- State, part 3 of 3 (State.lua loads the parts in order; split for the 1000-line cap):
-- the state.* RPC ops.
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local S = BG3AI.State

local truthy = U.truthy
local osi = V.osi
local comp = U.comp
local round = U.round
local I = S.inner
local now, selectedCharacter, health, resistances = I.now, I.selectedCharacter, I.health, I.resistances
local skills, pos1, resources, concentration = I.skills, I.pos1, I.resources, I.concentration
local moveView, level, passives, saves, classes = I.moveView, I.level, I.passives, I.saves, I.classes
local armorClass, levelUpTo, abilityBlock, raceName = I.armorClass, I.levelUpTo, I.abilityBlock, I.raceName
local partyBrief, itemView = I.partyBrief, I.itemView

------------------------------------------------------------------------
-- Ops
------------------------------------------------------------------------

local function timed(name, fn)
    RPC.register(name, function(args)
        local t0 = now()
        local r = fn(args or {})
        S.lastMs = S.lastMs or {}
        S.lastMs[name] = now() - t0
        return r
    end)
end

-- {uuid = true} for the player's avatar(s) (DB_Avatars): a companion is in the party but not one of them.
local function avatarSet()
    local avatars = {}
    pcall(function()
        for _, r in ipairs(Osi.DB_Avatars:Get(nil)) do avatars[V.uuid(r[1])] = true end
    end)
    return avatars
end

timed("state.status", function()
    local ctx = V.context()
    local host = ctx.party[1]
    local active = selectedCharacter(ctx.party)
    local out = { party = {}, active = active }
    local lvl = host and osi(Osi.GetRegion, host)
    out.level = lvl
    out.region = lvl and (S.REGION_NAMES[lvl] or lvl)
    -- bumped when a wall/door/blocker near the party changed (Navigation.lua geometryChanged, V13): the bridge's
    -- dead ends from before it no longer apply
    out.geo_gen = BG3AI.Nav and BG3AI.Nav.geoGen or nil
    local anyCombat, ftb = false, false
    local avatars = avatarSet()
    for _, u in ipairs(ctx.party) do
        local b = partyBrief(ctx, u, active)
        b.avatar = avatars[u]
        out.party[#out.party + 1] = b
        if b.in_combat then anyCombat = true end
        if truthy(osi(Osi.IsInForceTurnBasedMode, u)) then ftb = true end
    end
    out.in_combat = anyCombat
    out.turn_based = anyCombat or ftb
    local ae = V.entity(active)
    out.in_dialogue = ae ~= nil and V.inDialog(active, ae)
    out.gold = host and osi(Osi.PartyGetGold, host)
    if anyCombat then
        local c = S.combat(ctx)
        out.combat = { round = c.round, current = c.current }
    end
    out.camp = S.campRoster(ctx)
    return out
end)

-- Equipped items {slot = {uuid, name}} (the character sheet's paper doll).
local function equipmentOf(e)
    local eq = {}
    pcall(function()
        for _, inv in ipairs(e.InventoryOwner.Inventories) do
            if tostring(inv.InventoryData.Type) == "Equipment" then
                for slot, it in pairs(inv.InventoryContainer.Items) do
                    local iu = V.entityUuid(it.Item)
                    if iu then eq[tostring(Ext.Enums.ItemSlot[slot])] = { uuid = iu, name = V.name(iu) } end
                end
            end
        end
    end)
    return eq
end

-- Companions waiting at camp (recruited, not in the party). The engine gives exactly those a CampPresence
-- component (live 2026-10-10, SCL: Wyll and Halsin had it; party members and unrecruited origins such as
-- Lae'zel, Astarion, Minsc did not). Fair: the player sees them at camp and knows whom they recruited.
-- Rows: {uuid, name, level, classes, level_up}; full=true adds skills and equipment (state.camp).
--
-- "At camp" is V.partyInCamp (the story's DB_PlayerInCamp), not a GetRegion match: GetRegion is the level id, and the
-- Act 3 camp may share the Lower City's level (S081's camp() pushed no level_changed), which gave the "!" and the camp
-- spot anywhere in the city. An unreadable DB reads as not at camp (fail closed).
function S.campRoster(ctx, full)
    local out = {}
    local ok, rows = pcall(function() return Osi.DB_Origins:Get(nil) end)
    if not ok or not rows then return out end
    local atCamp = V.partyInCamp(ctx)
    for _, r in ipairs(rows) do
        local u = V.uuid(r[1])
        local e = u and not ctx.is_party[u] and V.entity(u)
        if e and comp(e, "CampPresence") and not truthy(osi(Osi.IsDead, u)) then
            local o = { uuid = u, name = V.name(u), level = level(e), level_up = levelUpTo(e) }
            -- Its "!" is seen only at camp.
            if atCamp and comp(e, "HasExclamationDialog") then o.wants_talk = true end
            local cl = classes(e)
            if #cl > 0 then o.classes = cl end
            if full then
                o.skills = skills(e)
                o.equipment = equipmentOf(e)
                local okx, avail = pcall(function() return e.AvailableLevel.Level end)
                if okx and avail and o.level and avail > o.level then o.can_reach = avail end
                -- Where it stands when the party is at camp (a player sees the camp companions and walks over to
                -- talk): swap_party walks up when it's past talk range (Halsin stood 66 m off, live).
                if atCamp then o.pos = pos1(V.pos(e)) end
            end
            out[#out + 1] = o
        end
    end
    return out
end

-- state.camp {}: the camp roster with skills and gear, plus the party's skills (classes, level) so the
-- bridge can tell what a swap would add (party hint: locks, face, perception).
timed("state.camp", function()
    local ctx = V.context()
    local party, avatars = {}, avatarSet()
    for _, u in ipairs(ctx.party) do
        local e = V.entity(u)
        if e and not comp(e, "IsSummon") then
            local cl = classes(e)
            party[#party + 1] = { uuid = u, name = V.name(u), level = level(e), classes = #cl > 0 and cl or nil,
                skills = skills(e), dead = truthy(osi(Osi.IsDead, u)) or nil,
                avatar = avatars[u] }
        end
    end
    return { camp = S.campRoster(ctx, true), party = party }
end)

timed("state.character", function(args)
    local ctx = V.context()
    local u = args.uuid or selectedCharacter(ctx.party)
    local e = V.entity(u)
    if not e then error("unknown entity", 0) end
    if not ctx.is_party[u] then
        -- Stale handles must not leak: re-check visibility on every call.
        if not comp(e, "ServerCharacter") or not V.characterVisible(ctx, u, e) then error("not visible", 0) end
        return S.examine(ctx, u, e)
    end
    local want
    if type(args.sections) == "table" then
        want = {}
        for _, s in ipairs(args.sections) do want[s] = true end
    end
    local function sec(n) return not want or want[n] end
    local h = health(e) or {}
    local out = { uuid = u, name = V.name(u), level = level(e), hp = h.hp, max_hp = h.max_hp }
    if h.temp_hp and h.temp_hp > 0 then out.temp_hp = h.temp_hp end
    out.classes = classes(e)
    out.race = raceName(e)
    if sec("abilities") then
        out.abilities = abilityBlock(e)
        out.saves = saves(e)
        out.skills = skills(e)
        out.ac = armorClass(e)
        pcall(function() out.proficiency_bonus = e.Stats.ProficiencyBonus; out.initiative = e.Stats.InitiativeBonus end)
        local res = resistances(e)
        if #res > 0 then out.resistances = res end
    end
    if sec("resources") then out.resources = moveView(u, resources(e)) end
    if sec("conditions") then out.statuses = V.statuses(e) end
    if sec("passives") then out.features = passives(e) end
    if sec("equipment") then out.equipment = equipmentOf(e) end
    if sec("xp") then
        pcall(function()
            local x = e.Experience
            out.xp = { current = x.CurrentLevelExperience, next_level = x.NextLevelExperience, total = x.TotalExperience }
        end)
        local ok, lu = pcall(function() return e.CanLevelUp ~= nil end)
        if ok and lu then out.can_level_up = true end
    end
    out.concentration = concentration(e)
    pcall(function() out.darkvision_m = round(e.Sight.DarkvisionRange, 1) end)
    pcall(function() out.weight = round(e.EncumbranceState and 0 or 0, 1) end)
    out.weight = nil
    return out
end)

timed("state.actions", function(args)
    local ctx = V.context()
    local u = args.uuid or args.char or selectedCharacter(ctx.party)
    if not ctx.is_party[u] then error("not a party member", 0) end
    local e = V.entity(u)
    return { uuid = u, resources = moveView(u, resources(e)), actions = S.actions(u) }
end)

timed("state.nearby", function(args)
    return S.nearby(V.context(), args)
end)

timed("state.inventory", function(args)
    return S.inventory(V.context(), args)
end)

timed("state.combat", function()
    return S.combat(V.context())
end)

timed("state.journal", function()
    -- only the host's id: V.party(), not V.context() (its per-frame sight data cost ~6 ms on a cached journal,
    -- check_route 18:30 "second 6 ms (cached; expect ~0)")
    return S.journal({ party = V.party() })
end)

timed("state.map", function()
    return S.map(V.context())
end)

timed("state.entity", function(args)
    local ctx = V.context()
    local u = args.uuid
    local e = V.entity(u)
    if not e then error("unknown entity", 0) end
    if comp(e, "ServerCharacter") then
        if ctx.is_party[u] then return partyBrief(ctx, u, selectedCharacter(ctx.party)) end
        if not V.characterVisible(ctx, u, e) then
            local m, seen = V.memory[u], V.lastSeen(u) -- this level only (a position elsewhere leads nowhere)
            if m and seen then return { uuid = u, name = m.name, visible = false, last_pos = seen.pos, ago_s = seen.ago_s } end
            error("not visible", 0)
        end
        -- The position get_nearby shows for it: travel_to/talk_to/move_to walk a far target in legs from it (S083:
        -- without it travel_to Nocturne at 90 m was one capped move_to that failed).
        local o = S.examine(ctx, u, e)
        o.pos = pos1(V.pos(e))
        return o
    end
    if comp(e, "ServerItem") then
        local inv = comp(e, "InventoryMember")
        if inv then
            -- Items carried by the party are always inspectable. Walk the inventory owners up (bags in bags):
            -- e.InventoryTopOwner is nil on carried items (probe 15:5x), which made every look_at on an
            -- inventory item "not visible" (S018, S025, S026).
            local cur, top = u, nil
            for _ = 1, 6 do
                local o = osi(Osi.GetInventoryOwner, cur)
                o = type(o) == "string" and o:match("%x+%-%x+%-%x+%-%x+%-%x+$") or nil
                if not o then break end
                if ctx.is_party[o] then top = o break end
                cur = o
            end
            if not top then error("not visible", 0) end
        end
        -- A door/container/usable the party saw on this level minutes ago is still examinable, the rule interact and
        -- route already follow (V.itemKnown). S055 e1174, S056 e603: doors listed in LEADS 10-20 s before, look_at
        -- "not visible", interact on e1174 then worked.
        local outOfSight = false
        if not inv and not V.itemVisible(ctx, u, e, V.pos(e)) then
            if not (V.itemKnown and V.itemKnown(ctx, u, e, V.pos(e))) then error("not visible", 0) end
            outOfSight = true
        end
        local o = itemView(e, nil)
        if outOfSight then o.out_of_sight = true end
        if not inv then o.pos = pos1(V.pos(e)) end
        local okd, desc = pcall(function()
            local st = Ext.Stats.Get(e.Data.StatsId)
            return st and U.loca(st.Description) or nil
        end)
        if okd and desc then o.description = desc end
        if S.lockRead(u) then o.locked = true end
        o.effect = V.itemEffects(e, true)
        return o
    end
    error("not visible", 0)
end)

timed("state.preview", function(args)
    return S.preview(V.context(), args)
end)

RPC.register("state.timings", function() return S.lastMs or {} end)

-- Fairness self-test: per-rule counts of what the filter hid around the
-- party (no names, no ids), so the filter can be audited without leaking.
RPC.register("dev.fairness_probe", function(args)
    local ctx = V.context()
    local radius = tonumber(args.radius) or 30
    local center = ctx.observers[1] and ctx.observers[1].pos
    if not center then error("no party member in the world", 0) end
    local c = { shown = 0, off_stage_or_script_hidden = 0, invisible = 0, sneaking = 0, not_perceived = 0 }
    for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter")) do
        local p, u = V.pos(e), V.entityUuid(e)
        if p and u and not ctx.is_party[u] and U.dist(p, center) <= radius then
            local ally = V.isAlly(ctx, u)
            if V.characterVisible(ctx, u, e, p) then c.shown = c.shown + 1
            elseif not truthy(osi(Osi.IsOnStage, u)) or truthy(osi(Osi.IsInvisibleByScript, u)) then c.off_stage_or_script_hidden = c.off_stage_or_script_hidden + 1
            elseif not ally and truthy(osi(Osi.IsInvisible, u)) then c.invisible = c.invisible + 1
            elseif not ally and truthy(osi(Osi.HasActiveStatus, u, "SNEAKING")) then c.sneaking = c.sneaking + 1
            else c.not_perceived = c.not_perceived + 1 end
        end
    end
    local i = { shown = 0, hidden_or_invisible = 0, carried = 0, undiscovered_trap = 0, secret_door = 0, scenery = 0, not_perceived = 0 }
    for _, u in ipairs(V.itemsAround(center, radius)) do
        local e = V.entity(u)
        local p = e and V.pos(e)
        if p and U.dist(p, center) <= radius then
            local vis = V.itemVisible(ctx, u, e, p)
            local interact = truthy(osi(Osi.GetCanInteract, u))
            local si = comp(e, "ServerItem")
            local okS, secret = pcall(function() return si and si.IsSecretDoor end)
            if vis then i.shown = i.shown + 1
            elseif not truthy(osi(Osi.IsOnStage, u)) or truthy(osi(Osi.IsInvisible, u)) or truthy(osi(Osi.IsInvisibleByScript, u)) then i.hidden_or_invisible = i.hidden_or_invisible + 1
            elseif comp(e, "InventoryMember") then i.carried = i.carried + 1
            elseif truthy(osi(Osi.IsTrap, u)) and not truthy(osi(Osi.IsTrapDiscovered, u)) then i.undiscovered_trap = i.undiscovered_trap + 1
            elseif okS and secret == true then i.secret_door = i.secret_door + 1
            elseif not interact then i.scenery = i.scenery + 1
            else i.not_perceived = i.not_perceived + 1 end
        end
    end
    return { radius = radius, characters = c, items = i }
end)
