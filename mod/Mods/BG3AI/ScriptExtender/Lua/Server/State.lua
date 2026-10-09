-- Fair state ops (server context). Every projection builds a fresh table of
-- allowlisted keys; nothing returns raw components. Visibility decisions come
-- from Server/Visibility.lua. See docs/PROTOCOL.md (op catalogue) and
-- research/02-visibility.md (rules, never-expose list).

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local S = BG3AI.State or {}
BG3AI.State = S

local truthy = U.truthy
local osi = V.osi
local comp = U.comp
local round = U.round

local ABILITIES = { "Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma" }
local ABBR = { Strength = "str", Dexterity = "dex", Constitution = "con", Intelligence = "int", Wisdom = "wis", Charisma = "cha" }

-- Level id -> region name as the game names the area (general game knowledge).
S.REGION_NAMES = {
    TUT_Avernus_C = "Nautiloid", WLD_Main_A = "Wilderness", CRE_Main_A = "Rosymorn Monastery",
    SCL_Main_A = "Shadow-Cursed Lands", INT_Main_A = "Last Light / Moonrise interlude",
    BGO_Main_A = "Rivington & Wyrm's Crossing", CTY_Main_A = "Lower City", IRN_Main_A = "Iron Throne",
    END_Main = "Upper City", SYS_CC_I = "Character creation",
}

------------------------------------------------------------------------
-- Generic helpers
------------------------------------------------------------------------

local function now() return Ext.Utils.MonotonicTime() end

local function pos1(p) return p and U.vec(p, 1) or nil end

local function staticName(guid, kind)
    local ok, r = pcall(Ext.StaticData.Get, guid, kind)
    if not ok or not r then return nil end
    local okd, n = pcall(function() return r.DisplayName.Handle.Handle end)
    return (okd and U.loca(n)) or r.Name
end

S.classCache = S.classCache or {}
local function className(guid)
    local key = tostring(guid)
    if S.classCache[key] == nil then
        S.classCache[key] = staticName(guid, "ClassDescription") or false
    end
    return S.classCache[key] or nil
end

local function classes(e)
    local out = {}
    local c = comp(e, "Classes")
    if not c then return out end
    pcall(function()
        for _, cl in ipairs(c.Classes) do
            local cname = className(cl.ClassUUID) -- NPC stat-block classes have no display name
            if cname then
                local entry = { class = cname, level = cl.Level }
                local sub = cl.SubClassUUID and tostring(cl.SubClassUUID)
                if sub and sub ~= "00000000-0000-0000-0000-000000000000" then entry.subclass = className(sub) end
                out[#out + 1] = entry
            end
        end
    end)
    return out
end

local function level(e)
    local ok, l = pcall(function() return e.AvailableLevel.Level end)
    if ok then return l end
    local ok2, l2 = pcall(function() return e.EocLevel.Level end)
    return ok2 and l2 or nil
end

local function health(e)
    local h = comp(e, "Health")
    if not h then return nil end
    local ok, r = pcall(function() return { hp = h.Hp, max_hp = h.MaxHp, temp_hp = h.TemporaryHp } end)
    return ok and r or nil
end

local function relation(ctx, uuid)
    if ctx.is_party[uuid] then return "party" end
    local o = ctx.observers[1] or { uuid = ctx.party[1] }
    if not o.uuid then return "neutral" end
    if truthy(osi(Osi.IsEnemy, o.uuid, uuid)) then return "hostile" end
    if truthy(osi(Osi.IsAlly, o.uuid, uuid)) then return "ally" end
    return "neutral"
end

local function concentration(e)
    local c = comp(e, "Concentration")
    if not c then return nil end
    local ok, sid = pcall(function() return c.SpellId.Prototype end)
    if not ok or not sid or sid == "" then return nil end
    local st = osi(Ext.Stats.Get, sid)
    local okn, dn = pcall(function() return st.DisplayName end)
    return (okn and U.loca(dn)) or sid
end

local function selectedCharacter(party)
    for _, u in ipairs(party) do
        local e = V.entity(u)
        if e and comp(e, "ClientControl") then return u end
    end
    return osi(Osi.GetHostCharacter) and V.uuid(osi(Osi.GetHostCharacter)) or party[1]
end

local function inCombat(uuid) return truthy(osi(Osi.IsInCombat, uuid)) end

local function abilityBlock(e)
    local st = comp(e, "Stats")
    if not st then return nil end
    local out = {}
    local ok = pcall(function()
        for i, a in ipairs(ABILITIES) do
            out[ABBR[a]] = st.Abilities[i + 1]
        end
    end)
    return ok and out or nil
end

local function mod(score) return score and math.floor((score - 10) / 2) or 0 end

local function saveProficiencies(e)
    local prof = {}
    local bc = comp(e, "BoostsContainer")
    if not bc then return prof end
    pcall(function()
        for _, entry in pairs(bc.Boosts) do
            if tostring(entry.Type) == "ProficiencyBonus" then
                for _, b in ipairs(entry.Boosts) do
                    local p = b.ProficiencyBonusBoost
                    if p and tostring(p.Type) == "SavingThrow" then prof[tostring(p.Ability)] = true end
                end
            end
        end
    end)
    return prof
end

local function saves(e)
    local ab = abilityBlock(e)
    local st = comp(e, "Stats")
    if not ab or not st then return nil end
    local pb = 0
    pcall(function() pb = st.ProficiencyBonus end)
    local prof = saveProficiencies(e)
    local out = {}
    for _, a in ipairs(ABILITIES) do
        out[ABBR[a]] = mod(ab[ABBR[a]]) + (prof[a] and pb or 0)
    end
    return out
end

local function skills(e)
    local st = comp(e, "Stats")
    if not st then return nil end
    local out = {}
    pcall(function()
        for i = 0, 17 do
            local name = tostring(Ext.Enums.SkillId[i])
            local v = st.Skills[i + 1]
            if v ~= nil then out[name] = v end
        end
    end)
    return out
end

local function armorClass(e)
    local ok, ac = pcall(function() return e.Resistances.AC end)
    return ok and ac or nil
end

local DAMAGE_TYPES = { "None", "Slashing", "Piercing", "Bludgeoning", "Acid", "Thunder", "Necrotic", "Fire",
    "Lightning", "Cold", "Psychic", "Poison", "Radiant", "Force" }

-- Resistances as the Examine panel lists them: [{type, kind}]
local function resistances(e)
    local out = {}
    local r = comp(e, "Resistances")
    if not r then return out end
    pcall(function()
        for i, flags in ipairs(r.Resistances) do
            local t = DAMAGE_TYPES[i] or tostring(i)
            for _, f in ipairs(flags) do
                local s = tostring(f)
                local kind = s:match("^(%a-)To") or s
                out[#out + 1] = { type = t, kind = kind:lower(), magical = s:find("Magical") and true or nil,
                    nonmagical = s:find("NonMagical") and true or nil }
            end
        end
    end)
    return out
end

S.passiveCache = S.passiveCache or {}
local function passives(e)
    local out = {}
    local pc = comp(e, "PassiveContainer")
    if not pc then return out end
    pcall(function()
        for _, pe in ipairs(pc.Passives) do
            local id = pe.Passive and pe.Passive.PassiveId
            if id then
                local c = S.passiveCache[id]
                if c == nil then
                    c = false
                    local st = osi(Ext.Stats.Get, id)
                    if st then
                        local hidden = false
                        pcall(function()
                            for _, p in ipairs(st.Properties) do if p == "IsHidden" then hidden = true end end
                        end)
                        local okn, dn = pcall(function() return st.DisplayName end)
                        local nm = okn and U.loca(dn)
                        if nm and not hidden then c = nm end
                    end
                    S.passiveCache[id] = c
                end
                if c then out[#out + 1] = c end
            end
        end
    end)
    return out
end

local function raceName(e)
    local ok, g = pcall(function() return e.Race.Race end)
    return ok and staticName(g, "Race") or nil
end

------------------------------------------------------------------------
-- Projections
------------------------------------------------------------------------

-- Examine-panel projection of a perceivable non-party character.
function S.examine(ctx, uuid, e)
    local out = {
        uuid = uuid, name = V.name(uuid), relation = relation(ctx, uuid),
        statuses = V.statuses(e),
    }
    if truthy(osi(Osi.IsDead, uuid)) then out.dead = true end
    local h = health(e)
    if h and not ctx.rules.HIDE_NPC_HP and not ctx.is_party[uuid] then
        out.hp, out.max_hp = h.hp, h.max_hp
        if h.temp_hp and h.temp_hp > 0 then out.temp_hp = h.temp_hp end
    end
    if comp(e, "ExamineDisabled") then return out end
    out.level = level(e)
    out.race = raceName(e)
    local cl = classes(e)
    if #cl > 0 then out.classes = cl end
    out.ac = armorClass(e)
    out.abilities = abilityBlock(e)
    out.saves = saves(e)
    local res = resistances(e)
    if #res > 0 then out.resistances = res end
    local ps = passives(e)
    if #ps > 0 then out.features = ps end
    local ok, ini = pcall(function() return e.Stats.InitiativeBonus end)
    if ok then out.initiative = ini end
    local okd, dv = pcall(function() return e.Sight.DarkvisionRange end)
    if okd and dv and dv > 0 then out.darkvision_m = round(dv, 1) end
    out.concentration = concentration(e)
    return out
end

local function partyBrief(ctx, uuid, active)
    local e = V.entity(uuid)
    if not e then return { uuid = uuid, name = V.name(uuid) } end
    local h = health(e) or {}
    local o = {
        uuid = uuid, name = V.name(uuid), hp = h.hp, max_hp = h.max_hp,
        level = level(e), pos = pos1(V.pos(e)), statuses = V.statuses(e),
    }
    if h.temp_hp and h.temp_hp > 0 then o.temp_hp = h.temp_hp end
    local cl = classes(e)
    if #cl > 0 then o.classes = cl end
    if truthy(osi(Osi.IsDead, uuid)) then o.dead = true end
    if truthy(osi(Osi.HasActiveStatus, uuid, "DOWNED")) then o.downed = true end
    if inCombat(uuid) then o.in_combat = true end
    if uuid == active then o.active = true end
    if not truthy(osi(Osi.IsOnStage, uuid)) then o.off_stage = true end
    o.concentration = concentration(e)
    return o
end

------------------------------------------------------------------------
-- Resources and actions
------------------------------------------------------------------------

S.resourceNames = S.resourceNames or {}
local function resourceName(guid)
    local k = tostring(guid)
    if S.resourceNames[k] == nil then
        local ok, r = pcall(Ext.StaticData.Get, guid, "ActionResource")
        S.resourceNames[k] = ok and r and r.Name or false
    end
    return S.resourceNames[k] or nil
end

-- {ActionPoint={amount,max}, SpellSlot={[1]={amount,max},...}, ...}
local function resources(e)
    local out = {}
    local ar = comp(e, "ActionResources")
    if not ar then return out end
    pcall(function()
        for guid, list in pairs(ar.Resources) do
            local name = resourceName(guid) or tostring(guid)
            for _, r in ipairs(list) do
                if r.Level and r.Level > 0 then
                    out[name] = out[name] or {}
                    out[name][r.Level] = { amount = r.Amount, max = r.MaxAmount }
                else
                    out[name] = { amount = r.Amount, max = r.MaxAmount }
                end
            end
        end
    end)
    return out
end

local function resourcesView(res)
    local out = {}
    for name, v in pairs(res) do
        if v.amount ~= nil then
            out[name] = { round(v.amount, 1), round(v.max, 1) }
        else
            local levels = {}
            for lvl, x in pairs(v) do levels[tostring(lvl)] = { round(x.amount, 1), round(x.max, 1) } end
            out[name] = levels
        end
    end
    return out
end

-- "ActionPoint:1;SpellSlotsGroup:1:1:2;Movement:3" -> {action=1, slot=2, movement=3, other={...}}
local function parseCosts(s)
    local cost = {}
    if type(s) ~= "string" then return cost end
    for part in s:gmatch("[^;]+") do
        local fields = {}
        for f in part:gmatch("[^:]+") do fields[#fields + 1] = f:match("^%s*(.-)%s*$") end
        local name, n = fields[1], tonumber(fields[2]) or 1
        if name == "ActionPoint" then cost.action = n
        elseif name == "BonusActionPoint" then cost.bonus = n
        elseif name == "ReactionActionPoint" then cost.reaction = n
        elseif name == "Movement" then cost.movement = n
        elseif name == "SpellSlotsGroup" or name == "SpellSlot" or name == "WarlockSpellSlot" then
            cost.slot = tonumber(fields[#fields]) or 1
        elseif name and name ~= "" then
            cost.other = cost.other or {}
            cost.other[name] = n
        end
    end
    return cost
end

local function canPay(cost, res)
    local function have(name) local r = res[name]; return r and r.amount or 0 end
    if cost.action and have("ActionPoint") < cost.action then return false, "no action" end
    if cost.bonus and have("BonusActionPoint") < cost.bonus then return false, "no bonus action" end
    if cost.reaction and have("ReactionActionPoint") < cost.reaction then return false, "no reaction" end
    if cost.movement and have("Movement") < cost.movement then return false, "not enough movement" end
    if cost.slot then
        local ok = false
        for _, pool in ipairs({ "SpellSlot", "WarlockSpellSlot" }) do
            local slots = res[pool]
            if type(slots) == "table" and slots.amount == nil then
                for lvl, x in pairs(slots) do
                    if lvl >= cost.slot and x.amount >= 1 then ok = true end
                end
            end
        end
        if not ok then return false, "no spell slot of level " .. cost.slot .. "+" end
    end
    if cost.other then
        for name, n in pairs(cost.other) do
            local r = res[name]
            local amt = r and (r.amount or 0) or 0
            if r and r.amount == nil then for _, x in pairs(r) do amt = amt + x.amount end end
            if amt < n then return false, "no " .. name end
        end
    end
    return true
end

local TARGET_KIND = {
    Target = "entity", Projectile = "entity", ProjectileStrike = "point", Shout = "self", Zone = "point",
    Cone = "point", Rush = "point", Teleportation = "point", Throw = "entity", MultiStrike = "entity",
    Wall = "point", Storm = "point",
}

S.spellCache = S.spellCache or {}
local function spellInfo(id)
    local c = S.spellCache[id]
    if c ~= nil then return c or nil end
    c = false
    local st = osi(Ext.Stats.Get, id)
    if st then
        local function f(name) local ok, v = pcall(function() return st[name] end); if ok then return v end end
        local flags = {}
        pcall(function() for _, x in ipairs(st.SpellFlags) do flags[x] = true end end)
        c = {
            name = U.loca(f("DisplayName")) or id,
            level = f("Level"), type = tostring(f("SpellType") or ""),
            use_costs = f("UseCosts"), root = f("RootSpellID"), container = f("SpellContainerID"),
            power = f("PowerLevel"), range = f("TargetRadius"), area = f("AreaRadius"),
            concentration = flags.IsConcentration or nil, ritual = nil,
            save = f("SpellSaveDC") and nil or nil,
            tooltip_attack_save = f("TooltipAttackSave"),
        }
        if c.root == "" then c.root = nil end
        if c.container == "" then c.container = nil end
    end
    S.spellCache[id] = c
    return c or nil
end

local function cooldowns(e)
    local out = {}
    local cd = comp(e, "SpellBookCooldowns")
    if not cd then return out end
    pcall(function()
        for _, c in ipairs(cd.Cooldowns) do
            out[c.SpellId.Prototype] = tostring(c.CooldownType)
        end
    end)
    return out
end

-- Footprint radius (m), as Guard.radius: AIBounds.Stand, then Move, then 0.5.
local function standRadius(e)
    local ok, r = pcall(function()
        local b = e.Bound.Bound.AIBounds
        return (b.Stand and b.Stand.Radius) or (b.Move and b.Move.Radius)
    end)
    if ok and type(r) == "number" and r > 0 and r < 5 then return r end
    return 0.5
end

-- Weapon reach in metres per range keyword (stats WeaponRange is in cm).
local function weaponRanges(e)
    local r = { MeleeMainWeaponRange = 1.5, MeleeOffHandWeaponRange = 1.5 }
    pcall(function()
        for _, inv in ipairs(e.InventoryOwner.Inventories) do
            if tostring(inv.InventoryData.Type) == "Equipment" then
                for slot, it in pairs(inv.InventoryContainer.Items) do
                    local sn = tostring(Ext.Enums.ItemSlot[slot])
                    local st = Ext.Stats.Get(it.Item.Data.StatsId)
                    local wr = st and tonumber(st.WeaponRange)
                    if wr then
                        local key = ({ MeleeMainHand = "MeleeMainWeaponRange", MeleeOffHand = "MeleeOffHandWeaponRange",
                            RangedMainHand = "RangedMainWeaponRange", RangedOffHand = "RangedOffHandWeaponRange" })[sn]
                        if key then r[key] = wr / 100 end
                    end
                end
            end
        end
    end)
    return r
end

function S.actions(uuid)
    local e = V.entity(uuid)
    if not e then error("unknown character", 0) end
    local wranges = weaponRanges(e)
    local res = resources(e)
    local cds = cooldowns(e)
    local book = comp(e, "SpellBook")
    if not book then return {} end
    local byId, order, upcasts, variants = {}, {}, {}, {}
    pcall(function()
        for _, s in ipairs(book.Spells) do
            local id = s.Id.Prototype
            local info = spellInfo(id)
            if info then
                if info.root and info.root ~= id then
                    upcasts[info.root] = upcasts[info.root] or {}
                    local lvl = parseCosts(info.use_costs).slot
                    if lvl then upcasts[info.root][#upcasts[info.root] + 1] = lvl end
                elseif info.container then
                    variants[info.container] = variants[info.container] or {}
                    table.insert(variants[info.container], { spell = id, name = info.name })
                elseif not byId[id] then
                    byId[id] = { s = s, info = info }
                    order[#order + 1] = id
                end
            end
        end
    end)
    local out = {}
    for _, id in ipairs(order) do
        local info = byId[id].info
        local cost = parseCosts(info.use_costs)
        local a = { spell = id, name = info.name, cost = next(cost) and cost or nil,
            target = TARGET_KIND[info.type] or "entity" }
        if info.level and info.level > 0 then a.level = info.level end
        local rng = tonumber(info.range) or (type(info.range) == "string" and info.range ~= "" and (wranges[info.range] or info.range)) or nil
        if rng and rng ~= 0 then a.range = rng end
        local area = tonumber(info.area)
        if area and area > 0 then info.area = area end
        if type(info.area) == "number" and info.area > 0 then a.area = info.area; if a.target == "entity" then a.target = "point" end end
        if info.concentration then a.concentration = true end
        if upcasts[id] then table.sort(upcasts[id]); a.upcast = upcasts[id] end
        if variants[id] then a.variants = variants[id] end
        local ok, why = canPay(cost, res)
        if cds[id] then ok, why = false, "cooldown (" .. cds[id] .. ")" end
        a.available = ok
        if not ok then a.reason = why end
        out[#out + 1] = a
    end
    return out
end

------------------------------------------------------------------------
-- Inventory
------------------------------------------------------------------------

local function itemView(ie, owner, slot)
    local u = V.entityUuid(ie)
    if not u then return nil end
    local o = { uuid = u, name = V.name(u), owner = owner }
    local amt = osi(Osi.GetStackAmount, u)
    if amt and amt > 1 then o.count = amt end
    pcall(function()
        local v = ie.Value
        if v then
            o.value = v.Value
            if v.Rarity and v.Rarity > 0 then o.rarity = ({ "uncommon", "rare", "very_rare", "legendary" })[v.Rarity] or v.Rarity end
            if v.Unique then o.unique = true end
        end
    end)
    pcall(function() o.weight = round((ie.Data.Weight or 0) / 1000, 2) end)
    pcall(function() if ie.ServerItem.CanUse then o.usable = true end end)
    pcall(function() if ie.ServerItem.IsContainer then o.container = true end end)
    pcall(function() if ie.ServerItem.StoryItem then o.story = true end end)
    pcall(function() local t = ie.Equipable; if t then o.slot_type = tostring(t.Slot) end end)
    if slot then o.equipped_slot = slot end
    if comp(ie, "IsGold") then o.gold = true end
    return o
end

local function inventoryItems(invEntity, owner, out, depth, inContainer)
    if not invEntity then return end
    pcall(function()
        for _, it in pairs(invEntity.InventoryContainer.Items) do
            local ie = it.Item
            local v = itemView(ie, owner)
            if v then
                v["in"] = inContainer
                out[#out + 1] = v
                if v.container then
                    local io = comp(ie, "InventoryOwner")
                    if io and depth > 0 then
                        inventoryItems(io.PrimaryInventory, owner, out, depth - 1, v.name)
                    elseif io then
                        local n = 0
                        pcall(function() for _ in pairs(io.PrimaryInventory.InventoryContainer.Items) do n = n + 1 end end)
                        if n > 0 then v.contains = n end
                    end
                end
            end
        end
    end)
end

function S.inventory(ctx, args)
    local out = { items = {} }
    local owners = args.uuid and { args.uuid } or ctx.party
    for _, u in ipairs(owners) do
        if not ctx.is_party[u] then error("not a party member", 0) end
        local e = V.entity(u)
        local io = e and comp(e, "InventoryOwner")
        if io then
            -- Bags (camp supplies, pouches) are collapsed unless expand=true.
            inventoryItems(io.PrimaryInventory, u, out.items, args.expand and 1 or 0)
            pcall(function()
                for _, inv in ipairs(io.Inventories) do
                    if tostring(inv.InventoryData.Type) == "Equipment" then
                        for slot, it in pairs(inv.InventoryContainer.Items) do
                            local v = itemView(it.Item, u, tostring(Ext.Enums.ItemSlot[slot]))
                            if v then out.items[#out.items + 1] = v end
                        end
                    end
                end
            end)
        end
    end
    local f = args.filter
    if type(f) == "string" and f ~= "" then
        local lf = f:lower()
        local kept = {}
        for _, it in ipairs(out.items) do
            if it.name:lower():find(lf, 1, true) then kept[#kept + 1] = it end
        end
        out.items = kept
    end
    out.gold = osi(Osi.PartyGetGold, ctx.party[1])
    -- The container the party currently has open (contents are visible on screen).
    local open = BG3AI.Events and BG3AI.Events.openContainer
    if open and open.uuid and V.entity(open.uuid) then
        local ce = V.entity(open.uuid)
        local io = comp(ce, "InventoryOwner")
        if io then
            local items = {}
            inventoryItems(io.PrimaryInventory, open.uuid, items, 0)
            for _, it in ipairs(items) do it.owner = nil end
            out.open_container = { uuid = open.uuid, name = V.name(open.uuid), items = items }
        end
    end
    return out
end

------------------------------------------------------------------------
-- Combat
------------------------------------------------------------------------

-- Planning data for a visible entity at p: distance and line of sight from
-- every party member that is in the world: from = {uuid = {dist, los}}.
local function tactical(ctx, uuid, p, o)
    local from = {}
    for _, ob in ipairs(ctx.observers) do
        if ob.pos and ob.uuid ~= uuid then
            from[ob.uuid] = { round(U.dist(ob.pos, p), 1), truthy(osi(Osi.HasLineOfSight, ob.uuid, uuid)) and 1 or 0 }
        end
    end
    if next(from) then o.from = from end
    return o
end
S.tactical = tactical

local function combatEntityFor(uuid)
    local e = V.entity(uuid)
    local cp = e and comp(e, "CombatParticipant")
    if not cp then return nil end
    local ok, h = pcall(function() return cp.CombatHandle end)
    if ok and h then return h end
    return nil
end

function S.combat(ctx)
    local ce
    for _, u in ipairs(ctx.party) do
        if inCombat(u) then ce = combatEntityFor(u); if ce then break end end
    end
    if not ce then return { active = false } end
    local out = { active = true, order = {} }
    local to = comp(ce, "TurnOrder")
    if not to then return out end
    pcall(function() out.round = to.Round end)
    local hidden = 0
    local ended_current -- a group whose members all ended (the game is about to move on)
    pcall(function()
        for _, g in ipairs(to.CurrentGroups) do
            for _, m in ipairs(g.Members) do
                local u = V.entityUuid(m.Entity)
                local me = m.Entity
                if u then
                    if V.characterVisible(ctx, u, me) then
                        local tb = comp(me, "TurnBased")
                        local o = { uuid = u, name = V.name(u), relation = relation(ctx, u) }
                        local p = V.pos(me)
                        if p then o.pos = pos1(p); tactical(ctx, u, p, o) end
                        pcall(function() if m.Initiative then o.initiative = m.Initiative end end)
                        local h = health(me)
                        if h and h.max_hp and h.max_hp > 0 and (ctx.is_party[u] or not ctx.rules.HIDE_NPC_HP) then
                            o.hp_pct = math.floor(100 * h.hp / h.max_hp + 0.5)
                        end
                        if tb then
                            pcall(function()
                                -- A turn group (party members with equal initiative)
                                -- is current together; a member that already ended
                                -- keeps IsActiveCombatTurn with RequestedEndTurn set.
                                if tb.IsActiveCombatTurn then
                                    o.is_current = true
                                    if tb.RequestedEndTurn then
                                        o.ended = true
                                        ended_current = ended_current or u
                                    else
                                        out.turn_group = out.turn_group or {}
                                        table.insert(out.turn_group, u)
                                        out.current = out.current or u
                                    end
                                end
                                if tb.ActedThisRoundInCombat then o.acted = true end
                                if ctx.is_party[u] and tb.CanActInCombat and not tb.ActedThisRoundInCombat
                                    and not (tb.IsActiveCombatTurn and tb.RequestedEndTurn) then
                                    o.can_act = true
                                    out.party_can_act = out.party_can_act or {}
                                    table.insert(out.party_can_act, u)
                                end
                            end)
                        end
                        if truthy(osi(Osi.IsDead, u)) then o.dead = true end
                        o.concentration = concentration(me)
                        out.order[#out.order + 1] = o
                    else
                        hidden = hidden + 1
                    end
                end
            end
        end
    end)
    -- How the turn bar renders unseen combatants is unverified; only report
    -- that the order contains combatants we can't see if any exist.
    if hidden > 0 then out.unseen_combatants = true end
    out.current = out.current or ended_current
    return out
end

------------------------------------------------------------------------
-- Journal (quest prototypes parsed once from game data, state via Osiris)
------------------------------------------------------------------------

S.quests = S.quests or nil

local function loadQuests()
    if S.quests then return S.quests end
    local ok, s = pcall(Ext.IO.LoadFile, "Mods/GustavDev/Story/Journal/quest_prototypes.lsx", "data")
    if not ok or not s then S.quests = {}; return S.quests end
    local quests, pos = {}, 1
    while true do
        local qs = s:find('<node id="Quest">', pos, true)
        if not qs then break end
        local qe = s:find('<node id="Quest">', qs + 10, true) or (#s + 1)
        local block = s:sub(qs, qe - 1)
        local q = {
            id = block:match('id="QuestID" type="FixedString" value="([^"]*)"'),
            title = block:match('id="QuestTitle" type="TranslatedString" handle="([^"]*)"'),
            category = block:match('id="CategoryID" type="FixedString" value="([^"]*)"'),
            parent = block:match('id="ParentQuestID" type="FixedString" value="([^"]*)"'),
            visible = block:match('id="QuestVisiblity" type="bool" value="([^"]*)"') ~= "false",
            steps = {},
        }
        for st in block:gmatch('<node id="QuestStep">(.-)</node>') do
            q.steps[#q.steps + 1] = {
                id = st:match('id="ID" type="FixedString" value="([^"]*)"'),
                desc = st:match('id="Description" type="TranslatedString" handle="([^"]*)"'),
            }
        end
        if q.id then quests[#quests + 1] = q end
        pos = qe
    end
    S.quests = quests
    return quests
end

function S.journal(ctx)
    local who = ctx.party[1]
    local out = {}
    for _, q in ipairs(loadQuests()) do
        if q.visible and truthy(osi(Osi.QuestIsAccepted, who, q.id)) then
            local steps = {}
            for _, st in ipairs(q.steps) do
                if st.id and truthy(osi(Osi.QuestUpdateIsUnlocked, who, q.id, st.id)) then
                    local text = U.loca(st.desc)
                    if text then steps[#steps + 1] = text end
                end
            end
            local entry = { title = U.loca(q.title) or q.id, category = q.category ~= "" and q.category or nil, steps = steps }
            if q.parent and q.parent ~= "" then entry.parent = q.parent end
            entry.id = q.id
            if truthy(osi(Osi.QuestIsClosed, q.id)) then entry.done = true end
            out[#out + 1] = entry
        end
    end
    -- Resolve sub-quest parent ids to titles (ids stay internal).
    local titles = {}
    for _, e in ipairs(out) do titles[e.id] = e.title end
    for _, e in ipairs(out) do
        if e.parent then e.parent = titles[e.parent] end
        e.id = nil
    end
    return out
end

------------------------------------------------------------------------
-- Map
------------------------------------------------------------------------

local function prettyWaypoint(id)
    local s = tostring(id):gsub("^WAYP_", "")
    local area, rest = s:match("^(%u+)_(.+)$")
    rest = (rest or s):gsub("_", " "):gsub("(%l)(%u)", "%1 %2")
    return rest, area
end

function S.map(ctx)
    local host = ctx.party[1]
    local lvl = osi(Osi.GetRegion, host)
    local out = { level = lvl, region = S.REGION_NAMES[lvl] or lvl, waypoints = {} }
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "PartyWaypoints")
    if ok and list and list[1] then
        pcall(function()
            -- Set-like container: ipairs yields booleans, pairs yields the entries.
            for _, w in pairs(list[1].PartyWaypoints.Waypoints) do
                local name, area = prettyWaypoint(w.Name)
                local real = BG3AI.Nav and BG3AI.Nav.waypointName(tostring(w.Name))
                out.waypoints[#out.waypoints + 1] = { id = tostring(w.Name), name = real or name, area = area,
                    level = tostring(w.Level), here = tostring(w.Level) == lvl or nil }
            end
        end)
    end
    return out
end

------------------------------------------------------------------------
-- Nearby
------------------------------------------------------------------------

function S.nearby(ctx, args)
    local radius = tonumber(args.radius) or 25
    local kinds
    if type(args.kinds) == "table" then
        kinds = {}
        for _, k in ipairs(args.kinds) do kinds[k] = true end
    end
    local center = ctx.observers[1] and ctx.observers[1].pos
    local active = selectedCharacter(ctx.party)
    for _, o in ipairs(ctx.observers) do if o.uuid == active and o.pos then center = o.pos end end
    if not center then return { characters = {}, items = {} } end
    local out = { characters = {}, items = {} }
    local shown = {}

    if not kinds or kinds.characters then
        local ok, chars = pcall(Ext.Entity.GetAllEntitiesWithComponent, "ServerCharacter")
        if ok and chars then
            for _, e in ipairs(chars) do
                local p = V.pos(e)
                if p and math.abs(p[1] - center[1]) <= radius and math.abs(p[3] - center[3]) <= radius then
                    local d = U.dist(p, center)
                    local u = d <= radius and V.entityUuid(e)
                    if u and not ctx.is_party[u] and V.characterVisible(ctx, u, e, p) then
                        local o = { uuid = u, name = V.name(u), relation = relation(ctx, u), pos = pos1(p), dist = round(d, 1),
                            statuses = V.statuses(e) }
                        if #o.statuses == 0 then o.statuses = nil end
                        local dead = truthy(osi(Osi.IsDead, u))
                        if dead then o.dead = true end
                        local h = health(e)
                        if h and not dead and not ctx.rules.HIDE_NPC_HP then o.hp, o.max_hp = h.hp, h.max_hp end
                        if inCombat(u) then o.in_combat = true end
                        if not dead and o.relation ~= "hostile" and (truthy(osi(Osi.GetHasDefaultDialog, u)) or truthy(osi(Osi.GetHasOsirisDialog, u))) then
                            o.can_talk = true
                        end
                        if not dead and truthy(osi(Osi.CanTrade, u)) then o.can_trade = true end
                        tactical(ctx, u, p, o)
                        out.characters[#out.characters + 1] = o
                        shown[u] = true
                        V.remember(u, o.name, p, o.relation)
                    end
                end
            end
        end
        table.sort(out.characters, function(a, b) return a.dist < b.dist end)
    end

    if not kinds or kinds.items or kinds.door or kinds.container or kinds.item or kinds.ladder or kinds.usable or kinds.trap then
        local limit = tonumber(args.item_limit) or 40
        local cands = V.itemsAround(center, radius)
        local items = {}
        for _, u in ipairs(cands) do
            local e = V.entity(u)
            local p = e and V.pos(e)
            if p then
                local d = U.dist(p, center)
                if d <= radius then
                    local vis, kind = V.itemVisible(ctx, u, e, p)
                    -- "object" = non-pickup, non-usable scenery-like interactables; only on request.
                    if vis and ((not kinds and kind ~= "object") or (kinds and (kinds[kind] or (kinds.items and kind ~= "object")))) then
                        items[#items + 1] = { u = u, e = e, p = p, d = d, kind = kind }
                    end
                end
            end
        end
        table.sort(items, function(a, b) return a.d < b.d end)
        for i, it in ipairs(items) do
            if i > limit then out.more_items = #items - limit; break end
            local u, e = it.u, it.e
            local o = { uuid = u, name = V.name(u), kind = it.kind, pos = pos1(it.p), dist = round(it.d, 1) }
            if o.name == "Object" or o.name == "Unknown" then goto continue end
            if args.item_los then tactical(ctx, u, it.p, o) end
            if truthy(osi(Osi.IsLocked, u)) then o.locked = true end
            if comp(e, "HasOpened") then o.opened = true end
            if comp(e, "IsOwned") or comp(e, "InventoryIsOwned") then o.owned = true end
            if truthy(osi(Osi.IsTrap, u)) and truthy(osi(Osi.IsTrapDiscovered, u)) then
                o.trap = { armed = truthy(osi(Osi.IsTrapArmed, u)) }
                o.kind = it.kind == "object" and "trap" or it.kind
            end
            pcall(function() if e.ServerItem.StoryItem then o.story = true end end)
            out.items[#out.items + 1] = o
            ::continue::
        end
    end

    local mem = V.recall(shown, center, radius * 2)
    if #mem > 0 then out.remembered = mem end
    return out
end

------------------------------------------------------------------------
-- Preview (what the targeting tooltip would show, cheaply and fairly)
------------------------------------------------------------------------

function S.preview(ctx, args)
    local char, spell, target = args.char, args.spell, args.target
    if not (char and spell and ctx.is_party[char]) then error("need char (party member) and spell", 0) end
    local info = spellInfo(spell)
    if not info then return { unsupported = true } end
    local ce = V.entity(char)
    local out = { spell = spell, name = info.name }
    local rnum = tonumber(info.range)
    local melee = false
    if not rnum and type(info.range) == "string" and info.range:find("WeaponRange") then
        -- Live: attacks have TargetRadius "MeleeMainWeaponRange"/"RangedMainWeaponRange" (Range 0).
        rnum = weaponRanges(ce)[info.range] or (info.range:find("Melee") and 1.5 or nil)
        melee = info.range:find("Melee") ~= nil
    elseif rnum and rnum > 0 and rnum <= 3 then
        melee = true
    end
    if rnum then info = setmetatable({ range = rnum }, { __index = info }) end
    if type(info.range) == "number" then out.range = info.range elseif info.range then out.range = tostring(info.range) end
    local tpos
    if target then
        local te = V.entity(target)
        if not te then error("unknown target", 0) end
        tpos = V.pos(te)
        local visible = comp(te, "ServerCharacter") and V.characterVisible(ctx, target, te, tpos)
            or (comp(te, "ServerItem") and V.itemVisible(ctx, target, te, tpos))
        if not visible then return { visible = false } end
        out.distance = round(U.dist(V.pos(ce), tpos), 1)
        out.line_of_sight = truthy(osi(Osi.HasLineOfSight, char, target))
        if type(info.range) == "number" then
            -- The guard's rule (G.check_cast): melee is edge to edge (both AIBounds.Stand radii).
            local slack = melee and (standRadius(ce) + standRadius(te)) or 0.5
            out.in_range = out.distance <= info.range + slack
        end
    elseif type(args.pos) == "table" then
        out.distance = round(U.dist(V.pos(ce), args.pos), 1)
        if type(info.range) == "number" then out.in_range = out.distance <= info.range + 0.5 end
    end
    -- Save DC / spell attack bonus as the tooltip shows them (8 + PB + mod).
    local tas = info.tooltip_attack_save
    if type(tas) == "string" and tas ~= "" then
        local st = comp(ce, "Stats")
        local pb, castAbility, abil = 0, nil, abilityBlock(ce) or {}
        pcall(function() pb = st.ProficiencyBonus; castAbility = tostring(st.SpellCastingAbility) end)
        local castMod = castAbility and ABBR[castAbility] and mod(abil[ABBR[castAbility]]) or 0
        for part in tas:gmatch("[^;]+") do
            if ABBR[part] then
                out.save = { ability = part, dc = 8 + pb + castMod }
            elseif part == "SpellAttack" or part == "RangedSpellAttack" or part == "MeleeSpellAttack" then
                out.attack_bonus = pb + castMod
            elseif part:find("WeaponAttack") then
                out.weapon_attack = true
            end
        end
    end
    out.hit_chance = nil -- computed by the client targeting UI only; not available server-side
    return out
end

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

timed("state.status", function()
    local ctx = V.context()
    local host = ctx.party[1]
    local active = selectedCharacter(ctx.party)
    local out = { party = {}, active = active }
    local lvl = host and osi(Osi.GetRegion, host)
    out.level = lvl
    out.region = lvl and (S.REGION_NAMES[lvl] or lvl)
    local anyCombat, ftb = false, false
    for _, u in ipairs(ctx.party) do
        local b = partyBrief(ctx, u, active)
        out.party[#out.party + 1] = b
        if b.in_combat then anyCombat = true end
        if truthy(osi(Osi.IsInForceTurnBasedMode, u)) then ftb = true end
    end
    out.in_combat = anyCombat
    out.turn_based = anyCombat or ftb
    local ae = V.entity(active)
    local inDialog = false
    pcall(function() inDialog = ae.ServerCharacter.InDialog == true end)
    if not inDialog then inDialog = truthy(osi(Osi.IsInDialog, active)) end
    out.in_dialogue = inDialog
    out.gold = host and osi(Osi.PartyGetGold, host)
    if anyCombat then
        local c = S.combat(ctx)
        out.combat = { round = c.round, current = c.current }
    end
    return out
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
    if sec("resources") then out.resources = resourcesView(resources(e)) end
    if sec("conditions") then out.statuses = V.statuses(e) end
    if sec("passives") then out.features = passives(e) end
    if sec("equipment") then
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
        out.equipment = eq
    end
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
    return { uuid = u, resources = resourcesView(resources(e)), actions = S.actions(u) }
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
    return S.journal(V.context())
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
            local m = V.memory[u]
            if m then return { uuid = u, name = m.name, visible = false, last_pos = U.vec(m.pos, 1) } end
            error("not visible", 0)
        end
        return S.examine(ctx, u, e)
    end
    if comp(e, "ServerItem") then
        local inv = comp(e, "InventoryMember")
        if inv then
            -- Items carried by the party are always inspectable.
            local ok, top = pcall(function() return V.entityUuid(e.InventoryTopOwner.TopOwner) end)
            if not (ok and top and ctx.is_party[top]) then error("not visible", 0) end
        else
            local vis = V.itemVisible(ctx, u, e, V.pos(e))
            if not vis then error("not visible", 0) end
        end
        local o = itemView(e, nil)
        local okd, desc = pcall(function()
            local st = Ext.Stats.Get(e.Data.StatsId)
            return st and U.loca(st.Description) or nil
        end)
        if okd and desc then o.description = desc end
        if truthy(osi(Osi.IsLocked, u)) then o.locked = true end
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

return S
