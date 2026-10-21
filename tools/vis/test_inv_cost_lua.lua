-- Offline cost harness for state.inventory (Server/State.lua, V12): bg3le's Ext.Stats.Get is Lua (a fresh proxy per
-- call, each attribute read a lookup plus a decode; third_party/bg3le src/lua_host.cpp stat_proxy), so the stats
-- reads are counted here the way bg3le pays them: one find per Get, one decode per attribute a proxy reads first.
-- An Act 3 sized party (4 members x 60 carried items over 45 stats entries, a camp chest of 300 items over 120
-- entries) is read the way look/get_status read it: a full rebuild each time (the read cache's TTL is 20 s and the
-- reads in S066-S070 came 20-300 s apart). The memo must keep the reply identical and leave a rebuild with no stats
-- reads once each entry was read once.
-- Run: tools/vis/test_lua.sh  (Docker, Lua 5.3)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

local counts = { find = 0, decode = 0 }
local entries = {}   -- StatsId -> attribute table

local function proxy(attrs)
    local cache = {}
    return setmetatable({}, { __index = function(_, k)
        if cache[k] == nil then
            counts.decode = counts.decode + 1
            local v = attrs[k]
            cache[k] = v == nil and false or v
        end
        local v = cache[k]
        if v == false and attrs[k] == nil then return nil end
        return v
    end })
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return 0 end, PrintWarning = function() end, Print = function() end },
    Events = { Tick = { Subscribe = function() end } },
    Stats = { Get = function(id)
        counts.find = counts.find + 1
        local a = entries[id]
        return a and proxy(a) or nil
    end },
    Enums = { ItemSlot = setmetatable({}, { __index = function(_, k) return "Slot" .. tostring(k) end }) },
    Loca = { GetTranslatedString = function() return nil end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    StaticData = { Get = function() return nil end },
    Osiris = { RegisterListener = function() end },
}

local members = {
    "aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002",
    "aaaaaaaa-0000-0000-0000-000000000003", "aaaaaaaa-0000-0000-0000-000000000004",
}
local CHEST = "cccccccc-0000-0000-0000-00000000c4e5"
local ents = {}
Osi = setmetatable({
    PartyGetGold = function() return 1234 end,
    GetStackAmount = function() return 1 end,
    DB_Camp_UserCampChest = { Get = function() return { { members[1], "CampChest_" .. CHEST } } end },
}, { __index = function() return function() return nil end end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
BG3AI.Visibility = {
    osi = function(f, ...) if f then local ok, v = pcall(f, ...); if ok then return v end end end,
    entity = function(u) return ents[u] end,
    entityUuid = function(e) return e.uuid end,
    name = function(u) return "N:" .. u end,
    context = function() return { party = members, is_party = { [members[1]] = true, [members[2]] = true,
                                                               [members[3]] = true, [members[4]] = true } } end,
}
Ext.Require = function(p) return dofile(LUA .. "/" .. p) end -- the modules load their parts
dofile(LUA .. "/Server/State.lua")
local S = BG3AI.State

-- a stats entry per kind: armour, weapon, food, misc
local function entry(i)
    local k = i % 4
    if k == 0 then
        return { ModifierList = "Armor", ArmorClass = 14, ArmorType = "ChainMail", Boosts = "AC(1)",
                 ["Proficiency Group"] = "MediumArmor", ObjectCategory = "Armor;Body" }
    elseif k == 1 then
        return { ModifierList = "Weapon", Damage = "1d8", ["Proficiency Group"] = "Longswords",
                 ["Weapon Properties"] = { "Versatile", "Melee" }, DefaultBoosts = "WeaponEnchantment(1)" }
    elseif k == 2 then
        return { ModifierList = "Object", SupplyValue = 5, ObjectCategory = "Food" }
    end
    return { ModifierList = "Object", InventoryTab = "Magical", PassivesOnEquip = "MAG_SpellSaveDC" }
end

local function item(u, sid)
    return { uuid = u, Data = { StatsId = sid, Weight = 500 }, Value = { Value = 10, Rarity = 0 }, ServerItem = {} }
end

local function owner(u, list)
    local items = {}
    for i, ie in ipairs(list) do items[i] = { Item = ie } end
    return { uuid = u, InventoryOwner = { PrimaryInventory = { InventoryContainer = { Items = items } },
                                          Inventories = {} } }
end

for i = 1, 120 do entries["STAT_" .. i] = entry(i) end
for m, u in ipairs(members) do
    local list = {}
    for i = 1, 60 do
        local iu = string.format("%08d-1111-0000-0000-%012d", m, i)
        list[i] = item(iu, "STAT_" .. ((m * 7 + i) % 45 + 1))
    end
    ents[u] = owner(u, list)
end
local chest = {}
for i = 1, 300 do chest[i] = item(string.format("%08d-2222-0000-0000-%012d", 9, i), "STAT_" .. (i % 120 + 1)) end
ents[CHEST] = owner(CHEST, chest)

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end

local function rebuild()
    S.invCache = nil  -- the TTL expired (or an item event): a full read
    counts.find, counts.decode = 0, 0
    local t = os.clock()
    local out = S.inventory(BG3AI.Visibility.context(), {})
    return out, counts.find, counts.decode, (os.clock() - t) * 1000
end

local function flat(out)
    local rows = {}
    for _, it in ipairs(out.items) do
        local keys = {}
        for k in pairs(it) do keys[#keys + 1] = k end
        table.sort(keys)
        local parts = {}
        for _, k in ipairs(keys) do parts[#parts + 1] = k .. "=" .. tostring(it[k]) end
        rows[#rows + 1] = table.concat(parts, ",")
    end
    return table.concat(rows, "\n") .. "|supplies=" .. tostring(out.supplies) .. "|camp=" .. tostring(out.camp_supplies)
end

test("state.inventory: a full rebuild reads each stats entry once, then none; the reply is the same", function()
    S.statsFactsMemo = {}
    local cold, f1, d1, ms1 = rebuild()
    local warm, f2, d2, ms2 = rebuild()
    print(string.format("     cold rebuild: %4d stats gets, %5d attribute decodes (%.1f ms Lua here)", f1, d1, ms1))
    print(string.format("     warm rebuild: %4d stats gets, %5d attribute decodes (%.1f ms Lua here)", f2, d2, ms2))
    eq(#cold.items, 240, "240 carried items")
    eq(cold.supplies > 0 and cold.camp_supplies > 0, true, "supplies counted (carried and camp chest)")
    -- before the memo every rebuild was a cold one with no memo: 2 gets per carried item + 1 per chest item
    eq(f1 <= 120, true, "cold: at most one get per stats entry (was 780 per rebuild): " .. f1)
    eq(f2, 0, "warm: no stats reads")
    eq(d2, 0, "warm: no attribute decodes")
    eq(flat(warm), flat(cold), "same reply")
end)

test("state.inventory: the memoised facts match a fresh read of the entry", function()
    S.statsFactsMemo = {}
    local cold = rebuild()
    local fresh = {}
    for _, it in ipairs(cold.items) do fresh[it.uuid] = it end
    -- the same items read with the memo filled by other items of the same entries
    local warm = rebuild()
    for _, it in ipairs(warm.items) do
        local c = fresh[it.uuid]
        eq(tostring(it.ac) .. tostring(it.dmg) .. tostring(it.ench) .. tostring(it.supply) .. tostring(it.spellgear),
           tostring(c.ac) .. tostring(c.dmg) .. tostring(c.ench) .. tostring(c.supply) .. tostring(c.spellgear), it.uuid)
    end
    local sword
    for _, it in ipairs(warm.items) do if it.dmg then sword = it break end end
    eq(sword.ench, 1, "weapon enchantment from DefaultBoosts"); eq(sword.prof, "Longswords", "proficiency")
end)

test("state.inventory: an item whose stats entry doesn't read keeps no facts, and is read again next time", function()
    S.statsFactsMemo = {}
    local u = members[1]
    local list = ents[u].InventoryOwner.PrimaryInventory.InventoryContainer.Items
    list[#list + 1] = { Item = item("99999999-1111-0000-0000-000000000001", "STAT_MISSING") }
    local out, f = rebuild()
    local _, f2 = rebuild()
    list[#list] = nil
    local row
    for _, it in ipairs(out.items) do if it.uuid == "99999999-1111-0000-0000-000000000001" then row = it end end
    eq(row ~= nil and row.ac == nil and row.supply == nil, true, "listed, no stats facts")
    eq(f2, 2, "warm: only the missing entry is looked up again (supplyTotal + itemView)")
    eq(f > f2, true, "cold read more")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
