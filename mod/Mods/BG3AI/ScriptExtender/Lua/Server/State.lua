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
    END_Main = "Upper City", EPI_Main_A = "Epilogue", SYS_CC_I = "Character creation",
}

-- An item's lock as the game shows it now. A read of "unlocked" after one of "locked" is the game unlocking it
-- (S083 03:55:56: Knock on e720, look showed `unlocked`; 2 s later IsLocked read true again for a moment and interact
-- refused it as locked): Actions.lua's interact counts it as a fresh unlock (A.unlock_seen, vis V24).
S.lockedSeen = S.lockedSeen or {}
function S.lockRead(u)
    local locked = truthy(osi(Osi.IsLocked, u))
    if locked then
        S.lockedSeen[u] = true
    elseif S.lockedSeen[u] then
        S.lockedSeen[u] = nil
        local A = BG3AI.Actions
        if A and A.unlock_seen then pcall(A.unlock_seen, u) end
    end
    return locked
end

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

-- The character's level (EocLevel). AvailableLevel is the level its XP allows: live (2026-10-10) Gale had
-- EocLevel 1, AvailableLevel 2 and a CanLevelUp component, the HUD showing his level-up button.
local function level(e)
    local ok, l = pcall(function() return e.EocLevel.Level end)
    if ok and l then return l end
    local ok2, l2 = pcall(function() return e.AvailableLevel.Level end)
    return ok2 and l2 or nil
end

-- The level a pending level up reaches, or nil. Fair: the portrait's level-up button shows exactly this.
local function levelUpTo(e)
    local cur = level(e)
    local ok, avail = pcall(function() return e.AvailableLevel.Level end)
    local okc, can = pcall(function() return e.CanLevelUp ~= nil end)
    if ok and avail and cur and avail > cur then return cur + 1 end
    if okc and can and cur then return cur + 1 end
    return nil
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
    return V.selected(party) or osi(Osi.GetHostCharacter) and V.uuid(osi(Osi.GetHostCharacter)) or party[1]
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
    o.level_up = levelUpTo(e)  -- bridge: "LEVEL UP: Gale (2)"
    -- The "!" the game shows over a companion who wants to talk (eoc::tag::HasExclamationDialog; live
    -- 2026-10-10: Wyll at camp). Bridge: "TALK: Wyll wants to talk" (S023 Gale, S026 Withers took screenshots).
    if comp(e, "HasExclamationDialog") then o.wants_talk = true end
    -- Party cohesion (bridge party.py: PARTY APART, re-chain): the member's region (a member on another
    -- level has coordinates in another space) and whether it follows the active character.
    o.region = osi(Osi.GetRegion, uuid)
    if active and uuid ~= active then o.chained = truthy(osi(Osi.InSamePartyGroup, uuid, active)) end
    -- Summons (Spiritual Weapon, Find Familiar, Mage Hand...) sit in DB_PartyMembers too: tagged so the
    -- bridge keeps them out of PARTY APART / re-chain / regroup but shows them as allies in combat.
    local sc = comp(e, "IsSummon")
    if not sc and not truthy(osi(Osi.IsSummon, uuid)) and S.isFollower and S.isFollower(uuid, e) then
        o.follower = true  -- temporary (Zanner Toobin S053): not dressed by equip_best
    end
    if sc or truthy(osi(Osi.IsSummon, uuid)) then
        o.summon = true
        if sc then
            local ow
            pcall(function() ow = V.entityUuid(sc.Owner) or V.entityUuid(sc.Summoner) end)
            o.owner = ow
        end
    end
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

-- resourcesView for a character: a member that can't walk (Guard.move_blocked: Lethargic, Stunned …) shows Movement 0
-- and MoveBlocked = the status. S091 08:30:24: `DISABLED e5 Claude: Lethargic` beside `Move 9/9m`, and the agent
-- planned a walk that could not happen.
local function moveView(u, res)
    local out = resourcesView(res)
    local Gd = BG3AI.Guard
    local ok, blk = pcall(function() return Gd and Gd.move_blocked and Gd.move_blocked(u) end)
    if ok and blk and out.Movement then
        out.Movement[1] = 0
        out.MoveBlocked = blk
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
            -- A27: a container with variants and no ContainerSpells list (Gather Your Allies)
            linked = flags.IsLinkedSpellContainer and (f("ContainerSpells") or "") == ""
                and (f("SpellContainerID") or "") == "" or nil,
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
    -- a prepared caster's book holds its whole class list: list only what is prepared (Guard.unprepared, A4)
    local Gd = BG3AI.Guard
    local prepared = Gd and Gd.prepared_ids and Gd.prepared_ids(e)
    pcall(function()
        for _, s in ipairs(book.Spells) do
            local id = s.Id.Prototype
            local info = not (Gd and Gd.unprepared and Gd.unprepared(s, prepared)) and spellInfo(id)
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
    local extra -- EXTRA_ATTACK on uuid (read once, on the first action-cost entry)
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
        local Gv = BG3AI.Guard
        if info.linked and not a.variants and Gv and Gv.box_variants then
            -- A27: Gather Your Allies' groups by name (use_ability "The Watch"), from the book / its container entries
            local ok, ids = pcall(Gv.box_variants, uuid, id)
            for _, v in ipairs(ok and ids or {}) do
                local f = Gv.spell_facts(v)
                a.variants = a.variants or {}
                a.variants[#a.variants + 1] = { spell = v, name = f and f.name or v }
            end
        end
        local pay = cost
        if cost.action and extra == nil then
            local Gd = BG3AI.Guard
            extra = (Gd and Gd.extra_attack_free and Gd.in_combat(uuid)
                and U.truthy(Gd.q("HasActiveStatus", uuid, Gd.EXTRA_ATTACK_STATUS))) or false
        end
        if cost.action and extra then
            -- Extra Attack: the next weapon attack costs no action (Guard.extra_attack_free), as on the hotbar
            local flags, st = {}, Ext.Stats.Get(id)
            pcall(function() for _, x in ipairs(st.SpellFlags) do flags[x] = true end end)
            if st and BG3AI.Guard.extra_attack_free(uuid, st, flags) then
                pay = {}
                for k, v in pairs(cost) do if k ~= "action" then pay[k] = v end end
                a.extra_attack = true
            end
        end
        local ok, why = canPay(pay, res)
        if cds[id] then ok, why = false, "cooldown (" .. cds[id] .. ")" end
        local Gi = BG3AI.Guard
        local ik = Gi and Gi.item_cd_key and Gi.item_cd_key(uuid, id)
        if ik and Gi.item_uses()[ik] then ok, why = false, "used until the next rest (once per rest, its item)" end
        a.available = ok
        if not ok then a.reason = why end
        out[#out + 1] = a
    end
    return out
end

-- File-local helpers the other parts use.
S.inner = { ABBR = ABBR, now = now, pos1 = pos1, classes = classes, level = level, levelUpTo = levelUpTo,
    health = health, relation = relation, concentration = concentration, selectedCharacter = selectedCharacter,
    inCombat = inCombat, abilityBlock = abilityBlock, mod = mod, saves = saves, skills = skills,
    armorClass = armorClass, resistances = resistances, passives = passives, raceName = raceName,
    partyBrief = partyBrief, resources = resources, moveView = moveView, spellInfo = spellInfo,
    standRadius = standRadius, weaponRanges = weaponRanges }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads S and the
-- helpers in S.inner.
for _, part in ipairs({ "Server/StateWorld.lua", "Server/StateOps.lua" }) do
    Ext.Require(part)
end

return S
