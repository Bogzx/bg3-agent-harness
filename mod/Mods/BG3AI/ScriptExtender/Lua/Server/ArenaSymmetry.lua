-- arena.symmetry {pairs = [[uuid, uuid], ...]} (server context, harness only, refused without BG3AI_ARENA=1): is each
-- pair of a mirror-team save the same character in gameplay terms? For the save authoring tool (it refuses to save on
-- any difference) and as the status read of a loaded arena save (pair i = teams.A[i] and teams.B[i]).
-- Per member a profile of raw ids (never display text, so nothing compares by translation), from the component
-- paths the mod already reads live (State.lua, GuardSpells.lua, Guard.resources):
--   race, level, classes (class/subclass/level), abilities, max_hp, ac, equipment (slot -> item template),
--   inventory (item templates, sorted), spells (the SpellBook's ids), prepared, passives, statuses, resources
--   (Guard.resources: name -> {level -> amount}; read after a full restore), proficiencies (Proficiency.Flags, as
--   StateWorld), skills (Stats.Skills by SkillId, as State.lua), boosts (BoostsContainer by type, as State.lua; each
--   boost as "type|boost|params|params2" from its BoostInfo.Params, never its cause: an item's or a status's uuid
--   differs between the twins). Character creation and level up write their choices there (skills, expertise,
--   proficiencies); BoostInfo.Params is not read live yet (the mirror probe's profiles read it).
-- Combat numbers and state that are not ids (design review 3 F7-F9, 2026-11-02; field paths from the bg3se headers,
-- not read live yet):
--   spellcast: per SpellBook spell "id|casting ability|prepare type|DC|attack": the prepare type is the always-prepared
--     flag; DC = 8 + Stats.ProficiencyBonus + the ability's Stats.AbilityModifiers + DifficultyCheck.SpellSaveDCBoost,
--     attack = ProficiencyBonus + the modifier (derived from the stored inputs: the game's own roll adds boosts, which
--     `boosts` compares); "-|-" for a spell cast with no ability
--   derived: proficiency and initiative bonus, SpellSaveDCBoost, WeaponActionDC, the stats' casting ability, the
--     Movement resource's maximum, and per main hand (melee, ranged) "properties|ability|attack|roll bonuses": the
--     weapon's WeaponProperties, the ability it attacks with (ranged Dexterity; finesse the better of Strength and
--     Dexterity; else Strength), ProficiencyBonus + that modifier, and the count of its AttackRollBonus entries
--   resource_max: per ActionResources entry "name|level|MaxAmount|ReplenishType" (current amounts are `resources`)
--   passive_state: per passive "id|ToggledOn|Disabled"
-- The template is recorded, not compared (where the copy came from, not how it plays). A field that doesn't read is
-- a problem too: it would hide a difference. Display names must be unique across all members (reaction ownership is
-- matched by name on the client).

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local V = BG3AI.Visibility
local U = BG3AI.Util
local Ar = BG3AI.Arena

local Sym = {}
Ar.Symmetry = Sym
Sym.FIELDS = { "race", "level", "classes", "abilities", "max_hp", "ac", "equipment", "inventory", "spells",
    "prepared", "passives", "statuses", "resources", "proficiencies", "skills", "boosts", "spellcast", "derived",
    "resource_max", "passive_state" }
local ABILITY = { Strength = 1, Dexterity = 2, Constitution = 3, Intelligence = 4, Wisdom = 5, Charisma = 6 }
local FINESSE, MELEE = 0x4, 0x1000 -- bg3se WeaponFlags

local function osi(fn, ...) return V.osi(fn, ...) end

local function read(fn)
    local ok, v = pcall(fn)
    if ok then return v end
    return nil
end

local function sorted(list)
    table.sort(list)
    return list
end

local function inventories(e, equipment)
    return read(function()
        local out = {}
        for _, inv in ipairs(U.items(e.InventoryOwner.Inventories)) do
            if (tostring(inv.InventoryData.Type) == "Equipment") == equipment then
                for slot, it in pairs(inv.InventoryContainer.Items) do
                    local iu = V.entityUuid(it.Item)
                    local tpl = iu and osi(Osi.GetTemplate, iu)
                    if not tpl then error("an item without a template") end
                    if equipment then
                        out[tostring(Ext.Enums.ItemSlot[slot])] = tpl
                    else
                        out[#out + 1] = tpl
                    end
                end
            end
        end
        if not equipment then sorted(out) end
        return out
    end)
end

local function number(v, what)
    if type(v) ~= "number" then error(what .. " doesn't read") end
    return v
end

-- The modifier of an ability by its label (Stats.AbilityModifiers is indexed as Abilities: [1] is None).
local function modifier(e, label)
    local k = ABILITY[tostring(label)]
    if not k then error("ability " .. tostring(label)) end
    return number(e.Stats.AbilityModifiers[k + 1], "the " .. label .. " modifier")
end

local function flag(x, bit) return math.floor(x / bit) % 2 == 1 end

function Sym.spellcast(e)
    local pb = number(e.Stats.ProficiencyBonus, "ProficiencyBonus")
    local boost = number(e.DifficultyCheck.SpellSaveDCBoost, "SpellSaveDCBoost")
    local out = {}
    for _, s in ipairs(U.items(e.SpellBook.Spells)) do
        if s.SpellCastingAbility == nil or s.PrepareType == nil then error("a spell's ability or prepare type") end
        local ab = tostring(s.SpellCastingAbility)
        local nums = "-|-"
        if ABILITY[ab] then
            local m = modifier(e, ab)
            nums = (8 + pb + m + boost) .. "|" .. (pb + m)
        end
        out[#out + 1] = table.concat({ tostring(s.Id.Prototype), ab, tostring(s.PrepareType), nums }, "|")
    end
    return sorted(out)
end

local function resourceName(uuid)
    return read(function() return Ext.StaticData.Get(uuid, "ActionResource").Name end) or tostring(uuid)
end

local function flags(v) -- a bitmask as read: a number, or the extender's flag set
    if type(v) == "number" then return tostring(v) end
    local out = {}
    for _, f in pairs(v) do out[#out + 1] = tostring(f) end
    table.sort(out)
    return table.concat(out, ",")
end

function Sym.resource_max(e)
    local out = {}
    for uuid, list in pairs(e.ActionResources.Resources) do
        local name = resourceName(uuid)
        for _, r in ipairs(U.items(list)) do
            if r.MaxAmount == nil or r.ReplenishType == nil then error("a resource's maximum or replenish type") end
            out[#out + 1] = ("%s|%s|%s|%s"):format(name, tostring(r.Level), tostring(r.MaxAmount),
                flags(r.ReplenishType))
        end
    end
    return sorted(out)
end

local function mainHand(u, e, slot, pb)
    local item = osi(Osi.GetEquippedItem, u, slot)
    if not item then return "none" end
    local w = V.entity(V.uuid(item) or item).Weapon
    local props = number(w.WeaponProperties, "WeaponProperties")
    local ab = "Strength"
    if not flag(props, MELEE) then
        ab = "Dexterity"
    elseif flag(props, FINESSE) and modifier(e, "Dexterity") > modifier(e, "Strength") then
        ab = "Dexterity"
    end
    return ("%d|%s|%d|%d"):format(props, ab, pb + modifier(e, ab), #U.items(w.AttackRollBonus))
end

function Sym.derived(u, e)
    local st, dc = e.Stats, e.DifficultyCheck
    local pb = number(st.ProficiencyBonus, "ProficiencyBonus")
    local out = { proficiency_bonus = pb, initiative_bonus = number(st.InitiativeBonus, "InitiativeBonus"),
        spell_save_dc_boost = number(dc.SpellSaveDCBoost, "SpellSaveDCBoost"),
        weapon_action_dc = number(dc.WeaponActionDC, "WeaponActionDC"), movement = "none" }
    if st.SpellCastingAbility == nil then error("the stats' casting ability") end
    out.spell_casting_ability = tostring(st.SpellCastingAbility)
    for uuid, list in pairs(e.ActionResources.Resources) do
        if resourceName(uuid) == "Movement" then
            for _, r in ipairs(U.items(list)) do out.movement = number(r.MaxAmount, "Movement's maximum") end
        end
    end
    out.melee_main_hand = mainHand(u, e, "MeleeMainHand", pb)
    out.ranged_main_hand = mainHand(u, e, "RangedMainHand", pb)
    return out
end

-- The member's profile: field -> value (nil when it doesn't read), plus template and name.
function Sym.profile(u)
    local e = V.entity(u)
    local p = { name = V.name(u), template = osi(Osi.GetTemplate, u) }
    if not e then return p end
    p.race = read(function() return tostring(e.Race.Race) end)
    p.level = read(function() return e.EocLevel.Level end)
    p.classes = read(function()
        local out = {}
        for _, cl in ipairs(U.items(e.Classes.Classes)) do
            out[#out + 1] = tostring(cl.ClassUUID) .. "/" .. tostring(cl.SubClassUUID) .. "/" .. tostring(cl.Level)
        end
        return sorted(out)
    end)
    p.abilities = read(function()
        local out = {}
        for i = 2, 7 do out[#out + 1] = e.Stats.Abilities[i] end
        if #out ~= 6 then error("abilities") end
        return out
    end)
    p.max_hp = read(function() return e.Health.MaxHp end)
    p.ac = read(function() return e.Resistances.AC end)
    p.equipment = inventories(e, true)
    p.inventory = inventories(e, false)
    p.spells = read(function()
        local out = {}
        for _, s in ipairs(U.items(e.SpellBook.Spells)) do out[#out + 1] = tostring(s.Id.Prototype) end
        return sorted(out)
    end)
    p.prepared = read(function()
        local set = BG3AI.Guard.prepared_ids(e)
        if not set then error("prepared") end
        local out = {}
        for id in pairs(set) do out[#out + 1] = id end
        return sorted(out)
    end)
    p.passives = read(function()
        local out = {}
        for _, pe in ipairs(U.items(e.PassiveContainer.Passives)) do out[#out + 1] = tostring(pe.Passive.PassiveId) end
        return sorted(out)
    end)
    p.statuses = read(function()
        local out = {}
        if e.StatusContainer == nil then return out end -- live: no StatusContainer on a character with no statuses
        for _, id in pairs(e.StatusContainer.Statuses) do out[#out + 1] = tostring(id) end
        return sorted(out)
    end)
    p.resources = read(function() return BG3AI.Guard.resources(u) end)
    p.proficiencies = read(function()
        local out = {}
        for _, f in pairs(e.Proficiency.Flags) do out[#out + 1] = tostring(f) end
        return sorted(out)
    end)
    p.skills = read(function()
        local out = {}
        for i = 0, 17 do
            local v = e.Stats.Skills[i + 1]
            if v == nil then error("skills") end
            out[tostring(Ext.Enums.SkillId[i])] = v
        end
        return out
    end)
    p.spellcast = read(function() return Sym.spellcast(e) end)
    p.derived = read(function() return Sym.derived(u, e) end)
    p.resource_max = read(function() return Sym.resource_max(e) end)
    p.passive_state = read(function()
        local out = {}
        for _, pe in ipairs(U.items(e.PassiveContainer.Passives)) do
            local ps = pe.Passive
            if ps.ToggledOn == nil or ps.Disabled == nil then error("passive state") end
            out[#out + 1] = ("%s|%s|%s"):format(tostring(ps.PassiveId), tostring(ps.ToggledOn), tostring(ps.Disabled))
        end
        return sorted(out)
    end)
    p.boosts = read(function()
        local out = {}
        for _, entry in pairs(e.BoostsContainer.Boosts) do
            for _, b in ipairs(U.items(entry.Boosts)) do
                local pr = b.BoostInfo.Params
                out[#out + 1] = table.concat({ tostring(entry.Type), tostring(pr.Boost), tostring(pr.Params),
                    tostring(pr.Params2) }, "|")
            end
        end
        return sorted(out)
    end)
    return p
end

-- A canonical text of a value (sorted keys), so two reads compare as strings.
local function canon(v)
    if type(v) ~= "table" then return tostring(v) end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    local out = {}
    for _, k in ipairs(keys) do out[#out + 1] = tostring(k) .. "=" .. canon(v[k]) end
    return "{" .. table.concat(out, ",") .. "}"
end
Sym.canon = canon

-- {ok, pairs = [{a, b, names, diffs = [{field, a, b}]}], problems = [text], members = {uuid = profile}}
function Sym.check(list)
    if type(list) ~= "table" or #list == 0 then error("pairs must list [uuid, uuid] pairs", 0) end
    local out, problems, members, byName = { pairs = {} }, {}, {}, {}
    for i, pr in ipairs(list) do
        if type(pr) ~= "table" or #pr ~= 2 then error("pair " .. i .. " must be [uuid, uuid]", 0) end
        local us = {}
        for k = 1, 2 do
            local u = V.uuid(pr[k])
            if not u or not V.entity(u) then error("pair " .. i .. ": no character " .. tostring(pr[k]), 0) end
            us[k] = u:lower()
            if members[us[k]] then error(V.name(us[k]) .. " is in two pairs", 0) end
            members[us[k]] = Sym.profile(us[k])
        end
        local a, b = members[us[1]], members[us[2]]
        local row = { a = us[1], b = us[2], names = { a.name, b.name }, diffs = {} }
        for _, f in ipairs(Sym.FIELDS) do
            for k, p in ipairs({ a, b }) do
                if p[f] == nil then problems[#problems + 1] = ("%s: %s doesn't read"):format(p.name, f) end
                if k == 2 and a[f] ~= nil and b[f] ~= nil and canon(a[f]) ~= canon(b[f]) then
                    row.diffs[#row.diffs + 1] = { field = f, a = a[f], b = b[f] }
                    problems[#problems + 1] = ("pair %d (%s, %s): %s differs"):format(i, a.name, b.name, f)
                end
            end
        end
        out.pairs[i] = row
    end
    for u, p in pairs(members) do
        local n = (p.name or ""):lower()
        if byName[n] then
            problems[#problems + 1] = ("%s: the display name of two members"):format(p.name)
        end
        byName[n] = u
    end
    table.sort(problems)
    out.problems, out.members, out.ok = problems, members, #problems == 0
    return out
end

RPC.register("arena.symmetry", function(args)
    if not Ar.enabled() then error("arena ops need BG3AI_ARENA=1 in the game's environment", 0) end
    if RPC.Ctx.side then error("arena ops are the harness's (no side)", 0) end
    return Sym.check((args or {}).pairs)
end)

return Sym
