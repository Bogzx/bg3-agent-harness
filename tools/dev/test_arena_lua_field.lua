-- Part of test_arena_lua.lua (split for the 1000-line cap): a mirror arena's field clean (Server/ArenaField.lua;
-- research R1: one side could start next to an explosive barrel, a lever, a pickable potion or a usable object).
-- The game's items are entities with ServerItem in the stub world; the Osiris commands the clean sends are recorded
-- and change what the queries read. test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local F = Ar.Field
local SPAWNS = { A = { 0, 0, 0 }, B = { 0, 0, 10 } }
local MID, REACH = { 0, 0, 5 }, 23.0 -- the surface clear's reach: 10/2 + 18
local INV = F.INVULNERABLE

local saved, calls

-- An item lying in the world at (x, z): q = its Osiris query answers, e.g. {GetCanPickUp = 1}; tpl its template.
local function item(u, x, z, q, tpl, over)
    world[u] = { pos = { x, 0, z }, comps = { ServerItem = {} } }
    for k, v in pairs(over or {}) do world[u].comps[k] = v end
    NAMES[u] = u
    osiv[key("GetTemplate", u)] = tpl or ("TPL_" .. u)
    for fn, v in pairs(q or {}) do osiv[key(fn, u)] = v end
end

-- The commands the clean sends, recorded; each changes the queries as the game would (refuse = {fn = true}: the
-- game refuses it, nothing changes; broken = {fn = true}: that query fails, as a missing Osiris name does).
local function field(opts)
    opts = opts or {}
    saved, calls = {}, {}
    local function stub(name, fn)
        saved[name] = rawget(Osi, name) or false
        rawset(Osi, name, function(...)
            calls[#calls + 1] = { name, ... }
            if (opts.refuse or {})[name] then error(name .. " refused") end
            return fn(...)
        end)
    end
    stub("RequestDelete", function(u) world[u] = nil end)
    stub("SetCanPickUp", function(u, v) osiv[key("GetCanPickUp", u)] = v end)
    stub("SetTrapArmed", function(u, v) osiv[key("IsTrapArmed", u)] = v end)
    stub("Unlock", function(u) osiv[key("IsLocked", u)] = 0 end)
    stub("Open", function(u) osiv[key("IsOpened", u)] = 1 end)
    stub("SetCanInteract", function(u, v) osiv[key("GetCanInteract", u)] = v end)
    stub("SetDisableUse", function(u, v) osiv[key("GetDisableUse", u)] = v end)
    stub("ApplyStatus", function(u, s) osiv[key("HasActiveStatus", u, s)] = 1 end)
    for name in pairs(opts.broken or {}) do
        saved[name] = rawget(Osi, name) or false
        rawset(Osi, name, function() error("no such query") end)
    end
end

local function unfield()
    for name, v in pairs(saved or {}) do rawset(Osi, name, v or nil) end
    saved = nil
end

local function ftest(title, fn)
    test(title, function()
        local ok, err = pcall(fn)
        unfield()
        if not ok then error(err, 0) end
    end)
end

local function sent(name, u)
    local out = {}
    for _, c in ipairs(calls) do if c[1] == name and (u == nil or c[2] == u) then out[#out + 1] = c end end
    return out
end

ftest("the field clean: pickables deleted, traps disarmed, doors unlocked and opened, objects disabled, barrels "
    .. "invulnerable; inventories, off-stage and far items untouched", function()
    field()
    item("potion", 1, 2, { GetCanPickUp = 1 })
    item("letter", -1, 8, { GetCanPickUp = 1, IsStoryItem = 1 })
    item("trap", 2, 1, { IsTrapArmed = 1, GetCanInteract = 1 })
    item("door", -3, 9, { IsLocked = 1, IsOpened = 0, GetCanInteract = 1 })
    item("lever", 4, 3, { GetCanInteract = 1 })
    item("barrel", -2, 7, { IsDestructible = 1 })
    item("worn", 0, 1, { GetCanPickUp = 1 }, nil, { InventoryMember = {} }) -- a twin's or a container's
    item("ghost", 0, 2, { GetCanPickUp = 1, IsOnStage = 0 })
    item("far", 0, 29, { GetCanPickUp = 1 }) -- 24 m from the midpoint
    local n = F.clean(MID, REACH, A1)
    eq(enc(n), enc({ radius = 23.0, items = 6, removed = 1, disabled = 4, disarmed = 1, unlocked = 1, opened = 1,
        invulnerable = 1, errors = 0 }), "counts")
    eq(enc(sent("RequestDelete")), enc({ { "RequestDelete", "potion" } }), "only the loose pickable deleted")
    eq(enc(sent("SetCanPickUp")), enc({ { "SetCanPickUp", "letter", 0 } }), "a story item kept, not pickable")
    eq(enc(sent("SetTrapArmed")), enc({ { "SetTrapArmed", "trap", 0 } }), "trap")
    eq(enc(sent("Unlock")), enc({ { "Unlock", "door", A1 } }), "door unlocked by the host")
    eq(enc(sent("Open")), enc({ { "Open", "door" } }), "door opened")
    eq(enc(sent("SetCanInteract")), enc({ { "SetCanInteract", "door", 0 }, { "SetCanInteract", "lever", 0 },
        { "SetCanInteract", "trap", 0 } }), "objects disabled")
    eq(#sent("SetDisableUse"), 3, "and their use")
    eq(enc(sent("ApplyStatus")), enc({ { "ApplyStatus", "barrel", INV, -1, 1 } }), "barrel invulnerable")
    eq(world.worn ~= nil and world.far ~= nil and world.ghost ~= nil, true, "the rest left alone")
    eq(enc(F.check(SPAWNS, REACH)), enc({ A = 0, B = 0 }), "nothing active left on either half")
end)

ftest("the read-back fails naming what one half keeps active; the same set on both halves passes", function()
    field({ refuse = { ApplyStatus = true } })
    item("barrel-a", 1, 2, { IsDestructible = 1 }, "BARREL_Explosive")
    item("barrel-b", 1, 8, { IsDestructible = 1 }, "BARREL_Explosive")
    item("crate-a", -2, 1, { IsDestructible = 1 }, "CRATE")
    local n = F.clean(MID, REACH, A1)
    eq(enc({ n.invulnerable, n.errors }), enc({ 0, 3 }), "the game refused all three")
    refused(function() F.check(SPAWNS, REACH) end, "the field doesn't mirror after the clean (1 item(s) within 23.0 "
        .. "m): A's half keeps crate-a at (-2.0, 1.0) (destructible CRATE)")
    world["crate-a"] = nil
    eq(enc(F.check(SPAWNS, REACH)), enc({ A = 1, B = 1 }), "one explosive barrel each: mirrored")
end)

ftest("a query that fails reads the item as unread: fails closed unless both halves read the same", function()
    item("rock-a", 3, 2, nil, "ROCK")
    field({ broken = { GetCanPickUp = true } })
    refused(function() F.check(SPAWNS, REACH) end, "A's half keeps rock-a at (3.0, 2.0) (unread:GetCanPickUp ROCK)")
    item("rock-b", 3, 8, nil, "ROCK")
    eq(enc(F.check(SPAWNS, REACH)), enc({ A = 1, B = 1 }), "mirrored")
end)

ftest("the game's item list that doesn't read fails the clean", function()
    local get = Ext.Entity.GetAllEntitiesWithComponent
    Ext.Entity.GetAllEntitiesWithComponent = function() return nil end
    local ok, err = pcall(F.clean, MID, REACH, A1)
    Ext.Entity.GetAllEntitiesWithComponent = get
    eq(ok, false, "fails")
    eq(tostring(err), "the game's items don't read (ServerItem)", "says so")
end)

test("arena_start records the field clean's counts", function()
    begin({ field = { radius = 23.0, items = 3, removed = 1, left = { A = 0, B = 0 } } })
    eq(enc(refs("arena_start")[1].field), enc({ radius = 23.0, items = 3, removed = 1, left = { A = 0, B = 0 } }),
        "field")
end)
