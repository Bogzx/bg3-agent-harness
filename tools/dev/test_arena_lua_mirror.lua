-- Part of test_arena_lua.lua (split for the 1000-line cap): the mirror twins: Shared/ArenaStats.lua's stat entries,
-- arena.mirror_spawn (two copies of one template with one entry) and arena.mirror_kit (the same choices on both, by
-- the game's calls, checked against the game's lists). The game here is a scripted fake recording Osiris calls.
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local T1 = "dddddddd-0000-0000-0000-0000000000d1"
local T2 = "dddddddd-0000-0000-0000-0000000000d2"
local T3 = "dddddddd-0000-0000-0000-0000000000d3"
local T4 = "dddddddd-0000-0000-0000-0000000000d4"
local HOST = "eeeeeeee-0000-0000-0000-0000000000e1"
local TPL = "d9401d40-453b-4512-84ee-d1c6ab29afea"
local HALFELF = "76057327-da03-4398-aaf0-c59345ef3a8b"
-- The extender's array as the game hands it (live check 13c: SkillList.Skills, PassiveList.Passives are userdata;
-- ipairs gives the ids): a full userdata (a temp file's handle) given its own metatable, so type() is "userdata".
local function udArray(ids)
    local u = io.tmpfile()
    debug.setmetatable(u, { __index = function(_, i) return ids[i] end, __len = function() return #ids end })
    return u
end

-- The extender's HashSet as the game hands it (SpellList.Spells; bg3se LuaSetProxy.inl): its index is membership, so
-- an id reads true or false and every integer reads false (live check 13d: ipairs over it never ended); # is its size.
local function udSet(ids)
    local members = {}
    for _, id in ipairs(ids) do members[id] = true end
    local u = io.tmpfile()
    debug.setmetatable(u, { __index = function(_, k) return members[k] == true end,
        __len = function() return #ids end })
    return u
end

-- Runs fn with an instruction cap: a walk that never ends fails the test instead of hanging it.
local function capped(fn)
    debug.sethook(function() error("no end within the instruction cap (a walk that never ends)", 2) end, "", 50000000)
    local ok, err = pcall(fn)
    debug.sethook()
    if not ok then error(err, 0) end
end

local LISTS = { -- the game's lists by uuid: {resource type, field, ids}
    -- live check 13c's reads of the Fighter's SkillList 6a4f8598 and PassiveList da3203d8
    ["skills-fighter"] = { "SkillList", "Skills", { "Acrobatics", "AnimalHandling", "Athletics", "History", "Insight",
        "Intimidation", "Perception", "Survival" } },
    ["styles"] = { "PassiveList", "Passives", { "FightingStyle_Archery", "FightingStyle_Defense",
        "FightingStyle_Dueling", "FightingStyle_GreatWeaponFighting", "FightingStyle_Protection",
        "FightingStyle_TwoWeaponFighting" } },
    ["cantrips"] = { "SpellList", "Spells", { "Target_FireBolt", "Target_RayOfFrost", "Target_MageHand" }, set = true },
    ["skills-unread"] = { "SkillList", "Skills", { "Acrobatics", false, "Athletics" } },
}
for _, l in pairs(LISTS) do l[3] = l.set and udSet(l[3]) or udArray(l[3]) end

local saved, calls, stats, created, tpl, inv

local function stub(name, fn)
    if saved[name] == nil then saved[name] = rawget(Osi, name) or false end
    rawset(Osi, name, fn)
end

local NULL = "00000000-0000-0000-0000-000000000000"

-- A copy as it reads once it has landed; over.fresh: as it reads in its spawn tick (live check 13b: Race empty; the
-- rest assumed missing too, the worst case).
local function twin(u, over)
    over = over or {}
    world[u] = { pos = { 0, 0, 0 }, comps = { ServerCharacter = {}, Race = { Race = over.race or HALFELF },
        Data = { StatsId = over.stats }, EocLevel = { Level = over.level or 1 },
        BoostsContainer = {}, PassiveContainer = {}, SpellBook = {},
        InventoryOwner = { Inventories = {
            { InventoryData = { Type = "Equipment" }, InventoryContainer = { Items = {} } },
            { InventoryData = { Type = "Inventory" }, InventoryContainer = { Items = over.bag or {} } } } } } }
    if over.fresh then
        world[u].comps = { ServerCharacter = {}, Race = { Race = NULL }, Data = {}, EocLevel = { Level = 1 } }
    end
    osiv[key("IsOnStage", u)], osiv[key("IsCharacter", u)] = 1, 1
end

local function game(opts)
    opts = opts or {}
    saved, calls, created, inv = { ext = { Ext.Stats, Ext.StaticData.Get, Ext.Template,
        Ext.Level.GetTileDebugInfo, Ext.Level.GetHeightsAt } }, {}, {}, {}
    stats = {}
    Ext.Stats = { Get = function(n) return stats[n] end,
        Create = function(n, typ, from)
            calls[#calls + 1] = { "Create", n, typ, from }
            stats[n] = { using = from }
            return stats[n]
        end }
    tpl = { Race = opts.race or HALFELF, Stats = "HeroHalfElfFemale" }
    Ext.Template = { GetRootTemplate = function(id) return id == TPL and tpl or nil end }
    Ext.StaticData.Get = function(id, typ)
        local l = LISTS[id]
        if l and l[1] == typ then return { [l[2]] = l[3] } end
        return nil
    end
    osiv[key("GetHostCharacter")] = HOST
    local pending, landing = { T1, T2, T3, T4 }, {}
    stub("CreateAt", function(t, x, y, z)
        calls[#calls + 1] = { "CreateAt", t, x, y, z, tpl.Stats }
        if opts.create_fails then error("CreateAt refused") end
        local u = table.remove(pending, 1)
        local st = opts.stats_id or tpl.Stats
        twin(u, { stats = st, fresh = opts.ready_lag_ms ~= nil })
        if opts.ready_lag_ms then landing[#landing + 1] = { twin = u, stats = st, at = clock + opts.ready_lag_ms } end
        created[#created + 1] = u
        return u
    end)
    for _, n in ipairs({ "MakePlayer", "AddBoosts", "AddPassive", "AddSpell", "Equip" }) do
        stub(n, function(...) calls[#calls + 1] = { n, ... } end)
    end
    -- The ground's surfaces: opts.tiles {x, z, ground surface, stays = the clear doesn't remove it} (a tile per metre;
    -- every other tile reads None); opts.no_tiles: no tile reads. opts.ground(x, z): the floor heights there (default
    -- {0}); a tile reads only at one of them.
    local tiles = opts.tiles or {}
    local ground = opts.ground or function() return { 0.0 } end
    Ext.Level.GetHeightsAt = function(x, z) return ground(x, z) end
    Ext.Level.GetTileDebugInfo = function(p)
        if opts.no_tiles then error("no tile there") end
        local on = false
        for _, h in ipairs(ground(p[1], p[3])) do on = on or math.abs(h - p[2]) < 0.01 end
        if not on then error("no tile at that height") end
        for _, t in ipairs(tiles) do
            if math.abs(p[1] - t[1]) < 0.5 and math.abs(p[3] - t[2]) < 0.5 then
                return { GroundSurface = t[3], CloudSurface = "None" }
            end
        end
        return { GroundSurface = "None", CloudSurface = "None" }
    end
    stub("RemoveSurfaceLayerAtPosition", function(...)
        calls[#calls + 1] = { "RemoveSurfaceLayerAtPosition", ... }
        if opts.surface_call_fails then error("RemoveSurfaceLayerAtPosition: wrong arguments") end
        local x, _, z, layer, r = ...
        for i = #tiles, 1, -1 do
            local t = tiles[i]
            if layer == "Ground" and not t.stays and (t[1] - x) ^ 2 + (t[2] - z) ^ 2 <= r * r then
                table.remove(tiles, i)
            end
        end
    end)
    stub("SetStoryDisplayName", function(u, name)
        calls[#calls + 1] = { "SetStoryDisplayName", u, name }
        if opts.shared_name then -- as scn.rename did live: one handle for every copy of the template
            for _, c in ipairs(created) do NAMES[c] = name end
        elseif opts.name_lag_ms then -- live check 13: the name lands after the call
            landing[#landing + 1] = { u = u, name = name, at = clock + opts.name_lag_ms }
        else
            NAMES[u] = name
        end
    end)
    saved.ticker = function()
        for _, l in ipairs(landing) do
            if clock >= l.at and not l.done then
                l.done = true
                if l.twin then twin(l.twin, { stats = l.stats }) else NAMES[l.u] = l.name end
            end
        end
    end
    tickers[#tickers + 1] = saved.ticker
    stub("TemplateAddTo", function(root, u, n, show)
        calls[#calls + 1] = { "TemplateAddTo", root, u, n, show }
        if not opts.items_never_arrive then inv[u .. root] = u .. "-item-" .. root end
    end)
    stub("GetItemByTemplateInInventory", function(root, u) return inv[u .. root] end)
end

local function restore()
    for _, j in pairs(RPC.jobs) do
        if j.kind == "arena.mirror_kit" and j.status == "running" then j.status = "interrupted" end
    end
    for i = #tickers, 1, -1 do if tickers[i] == saved.ticker then table.remove(tickers, i) end end
    for name, v in pairs(saved) do
        if name ~= "ext" and name ~= "ticker" then rawset(Osi, name, v or nil) end
    end
    Ext.Stats, Ext.StaticData.Get, Ext.Template = saved.ext[1], saved.ext[2], saved.ext[3]
    Ext.Level.GetTileDebugInfo, Ext.Level.GetHeightsAt = saved.ext[4], saved.ext[5]
    NAMES[T1], NAMES[T2], NAMES[T3], NAMES[T4] = nil, nil, nil, nil
end

local function guarded(fn)
    return function()
        game()
        local ok, err = pcall(fn)
        restore()
        if not ok then error(err, 0) end
    end
end

local function named(name)
    local out = {}
    for _, c in ipairs(calls) do if c[1] == name then out[#out + 1] = c end end
    return out
end

local function spawnArgs(over)
    local a = { stats = "BG3AI_Arena_Fighter_L1", template = TPL, race = HALFELF,
        spots = { { 1, 2, 3 }, { 4, 5, 6 } }, names = { "Ash", "Birch" } }
    for k, v in pairs(over or {}) do a[k] = v end
    return a
end

local function kit(over)
    local k = { race = { boosts = "ActionResource(Movement,9,0)", passives = { "Darkvision", "FeyAncestry" } },
        player = { passives = { "DeathSavingThrows" } },
        ability_bonus = { "Strength", "Constitution" },
        choices = { { kind = "skills", list = "skills-fighter", n = 2, exact = true,
            ids = { "Athletics", "Intimidation" } },
            { kind = "passives", list = "styles", n = 1, exact = true, ids = { "FightingStyle_Defense" } } },
        items = { { item = "WPN_Longsword", root = "root-sword", count = 1, equip = true },
            { item = "OBJ_Potion_Healing", root = "root-potion", count = 2, equip = false } } }
    for kk, v in pairs(over or {}) do k[kk] = v end
    return k
end

local function runJob(j, limit)
    for _ = 1, limit or 100 do
        if j.status ~= "running" then return end
        tick()
    end
end

test("stat entries: one per class and level at StatsLoaded with the switch, from _Hero, the class's own table",
    guarded(function()
        env.BG3AI_ARENA = nil
        for _, f in ipairs(statsLoaded) do f() end
        eq(#named("Create"), 0, "nothing without the switch")
        env.BG3AI_ARENA = "1"
        for _, f in ipairs(statsLoaded) do f() end
        local made = named("Create")
        eq(#made, 4, "4 classes at level 1")
        eq(enc({ made[1][2], made[1][3], made[1][4] }), enc({ "BG3AI_Arena_Fighter_L1", "Character", "_Hero" }),
            "the first entry")
        local f1 = stats.BG3AI_Arena_Fighter_L1
        eq(enc({ f1.Class, f1.Level, f1.Progressions, f1.Strength, f1.Charisma }),
            enc({ "Fighter", 1, "ba4e707e-4bbd-4630-963b-ce2f2947f971", 15, 8 }), "Fighter L1")
        eq(stats.BG3AI_Arena_Ranger_L1.SpellCastingAbility, "Wisdom", "the Ranger casts with Wisdom")
        local cost = { [8] = 0, [9] = 1, [10] = 2, [11] = 3, [12] = 4, [13] = 5, [14] = 7, [15] = 9 }
        for class, c in pairs(Ar.Stats.CLASSES) do
            local n = 0
            for _, v in ipairs(c.abilities) do n = n + cost[v] end
            eq(n, 27, class .. "'s point-buy")
        end
    end))

test("mirror_spawn: both copies made with the entry, the template set back, players with their own names",
    guarded(function()
        stats.BG3AI_Arena_Fighter_L1 = {}
        local r = Ar.Mirror.spawn(spawnArgs())
        local made = named("CreateAt")
        eq(#made, 2, "two CreateAt")
        eq(made[1][6] .. "," .. made[2][6], "BG3AI_Arena_Fighter_L1,BG3AI_Arena_Fighter_L1", "the entry at each")
        eq(tpl.Stats, "HeroHalfElfFemale", "the template's stats set back")
        eq(enc(r.twins), enc({ T1, T2 }), "twins")
        eq(enc(named("MakePlayer")), enc({ { "MakePlayer", T1, HOST }, { "MakePlayer", T2, HOST } }), "players")
        eq(NAMES[T1] .. "," .. NAMES[T2], "Ash,Birch", "names")
        eq(r.reads[2].stats, "BG3AI_Arena_Fighter_L1", "stats read back")
    end))

test("mirror_spawn refusals: no entry, another race, one name twice; a failing CreateAt still sets the template back",
    guarded(function()
        refused(function() Ar.Mirror.spawn(spawnArgs()) end, "no stat entry BG3AI_Arena_Fighter_L1")
        stats.BG3AI_Arena_Fighter_L1 = {}
        refused(function() Ar.Mirror.spawn(spawnArgs({ race = "race-gith" })) end, "is of race " .. HALFELF)
        refused(function() Ar.Mirror.spawn(spawnArgs({ names = { "Ash", "ash" } })) end, "two names")
        eq(#named("CreateAt"), 0, "nothing made")
        restore()
        game({ create_fails = true })
        stats.BG3AI_Arena_Fighter_L1 = {}
        refused(function() Ar.Mirror.spawn(spawnArgs()) end, "CreateAt refused")
        eq(tpl.Stats, "HeroHalfElfFemale", "set back after the error")
    end))

test("mirror_spawn first = 2: spot 2's copy is made, made a player and named first; twins stay in spot order",
    guarded(function()
        stats.BG3AI_Arena_Fighter_L1 = {}
        local r = Ar.Mirror.spawn(spawnArgs({ first = 2 }))
        local made = named("CreateAt")
        eq(enc({ made[1][3], made[2][3] }), enc({ 4, 1 }), "spot 2 created first")
        eq(enc(r.twins), enc({ T2, T1 }), "twins in spot order: spot 1's copy is the second made")
        eq(enc(r.order), enc({ 2, 1 }), "the creation order")
        eq(enc(named("MakePlayer")), enc({ { "MakePlayer", T1, HOST }, { "MakePlayer", T2, HOST } }),
            "players in creation order")
        eq(enc({ NAMES[T1], NAMES[T2] }), enc({ "Birch", "Ash" }), "each copy its spot's name")
        refused(function() Ar.Mirror.spawn(spawnArgs({ first = 3 })) end, "first must be 1 or 2")
    end))

test("mirror_spawn: a name that hasn't landed yet is no refusal; reads.name is what the copy reads at the call",
    guarded(function()
        restore()
        game({ name_lag_ms = 3000 })
        stats.BG3AI_Arena_Fighter_L1 = {}
        local r = Ar.Mirror.spawn(spawnArgs())
        eq(enc(r.twins), enc({ T1, T2 }), "twins")
        eq(r.reads[1].name == "Ash", false, "Ash not read yet")
    end))

test("mirror_kit: the same calls on both twins in the kit's order, then each item added, waited for and equipped",
    guarded(function()
        twin(T1)
        twin(T2)
        local r = Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() })
        local j = RPC.jobs[r.job]
        eq(#calls, 0, "nothing at the call")
        tick()
        local per = {}
        for _, c in ipairs(calls) do
            local u = c[2]
            per[u] = per[u] or {}
            local args = {}
            for i = 3, #c do args[#args + 1] = c[i] == u and "self" or c[i] end
            per[u][#per[u] + 1] = c[1] .. "(" .. table.concat(args, ",", 1, #args) .. ")"
        end
        local want = { "AddBoosts(ActionResource(Movement,9,0),BG3AI_ArenaKit,self)", "AddPassive(Darkvision)",
            "AddPassive(FeyAncestry)", "AddPassive(DeathSavingThrows)",
            "AddBoosts(Ability(Strength,2);Ability(Constitution,1),BG3AI_ArenaKit,self)",
            "AddBoosts(ProficiencyBonus(Skill,Athletics);ProficiencyBonus(Skill,Intimidation),BG3AI_ArenaKit,self)",
            "AddPassive(FightingStyle_Defense)" }
        eq(enc(per[T1]), enc(want), "twin 1's calls")
        eq(enc(per[T2]), enc(want), "twin 2's, the same")
        runJob(j)
        eq(j.status, "done", "job: " .. tostring(j.error))
        local adds, eqs = named("TemplateAddTo"), named("Equip")
        eq(enc(adds), enc({ { "TemplateAddTo", "root-sword", T1, 1, 0 }, { "TemplateAddTo", "root-sword", T2, 1, 0 },
            { "TemplateAddTo", "root-potion", T1, 2, 0 }, { "TemplateAddTo", "root-potion", T2, 2, 0 } }), "added")
        eq(enc(eqs), enc({ { "Equip", T1, T1 .. "-item-root-sword", 1, 0, 1 },
            { "Equip", T2, T2 .. "-item-root-sword", 1, 0, 1 } }), "the sword equipped (5 arguments), the potions not")
    end))

test("mirror_kit refusals, nothing applied: a pick not in the game's list, a wrong count, a list that doesn't read",
    guarded(function()
        twin(T1)
        twin(T2)
        local function with(choice) return kit({ choices = { choice } }) end
        local cases = {
            { with({ kind = "skills", list = "skills-fighter", n = 2, exact = true, ids = { "Athletics", "Stealth" } }),
                "Stealth is not in the game's list skills-fighter" },
            { with({ kind = "passives", list = "styles", n = 1, exact = true, ids = {} }),
                "0 picked, the game's selector takes 1" },
            { with({ kind = "spells", list = "cantrips", n = 2, exact = false,
                ids = { "Target_FireBolt", "Target_RayOfFrost", "Target_MageHand" } }), "takes 1..2" },
            { with({ kind = "spells", list = "spellbook", n = 6, exact = false, ids = { "Target_Sleep" } }),
                "the game's SpellList spellbook doesn't read" },
            { with({ kind = "skills", list = "skills-fighter", n = 2, exact = true,
                ids = { "Athletics", "Athletics" } }),
                "Athletics picked twice" },
        }
        for _, c in ipairs(cases) do
            refused(function() Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = c[1] }) end, c[2])
        end
        eq(#calls, 0, "no call made")
    end))

test("mirror_kit fails, nothing applied, on a ready twin of another race or one holding items", guarded(function()
    twin(T1)
    twin(T2)
    local j = RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = "race-gith", kit = kit() }).job]
    runJob(j)
    eq(enc({ j.status, tostring(j.error):find("is of race " .. HALFELF .. ", not race-gith", 1, true) ~= nil }),
        enc({ "failed", true }), "another race: " .. tostring(j.error))
    twin(T2, { bag = { { Item = { Uuid = { EntityUuid = "bag-1" } } } } })
    osiv[key("GetTemplate", "bag-1")] = "OBJ_X"
    j = RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() }).job]
    runJob(j)
    eq(enc({ j.status, tostring(j.error):find("already holds items", 1, true) ~= nil }), enc({ "failed", true }),
        "items: " .. tostring(j.error))
    eq(#calls, 0, "no call made")
end))

-- Live check 13b: setup started the kits in the spawn tick, where a copy's Race read 00000000-...; a moment later it
-- read the template's race. The kit waits for both twins to read ready, then applies its calls.
test("mirror_kit waits for copies whose components land 3 s after the spawn, applying nothing before",
    guarded(function()
    restore()
    game({ ready_lag_ms = 3000 })
    stats.BG3AI_Arena_Fighter_L1 = {}
    Ar.Mirror.spawn(spawnArgs())
    local j = RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() }).job]
    local from = clock
    for _ = 1, 100 do
        if j.status ~= "running" or #named("AddBoosts") > 0 then break end
        tick()
    end
    eq(clock - from >= 3000, true, "the first call after the components landed (" .. (clock - from) .. " ms)")
    runJob(j)
    eq(j.status, "done", "job: " .. tostring(j.error))
    eq(#named("AddPassive") > 0 and #named("Equip"), 2, "both kitted, the sword equipped")
end))

test("mirror_kit: a twin not ready in READY_WAIT_MS fails the job naming it and what it reads", guarded(function()
    restore()
    game({ ready_lag_ms = 60000 })
    stats.BG3AI_Arena_Fighter_L1 = {}
    Ar.Mirror.spawn(spawnArgs())
    local from = clock
    local j = RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() }).job]
    runJob(j)
    eq(j.status, "failed", "failed")
    local waited = clock - from
    eq(waited >= Ar.Mirror.READY_WAIT_MS and waited <= Ar.Mirror.READY_WAIT_MS + 1000, true, "failed at " .. waited)
    eq(tostring(j.error):find(T1 .. ": race " .. NULL, 1, true) ~= nil, true, tostring(j.error))
    eq(#calls - #named("CreateAt") - #named("MakePlayer") - #named("SetStoryDisplayName"), 0, "no kit call")
end))

test("mirror_kit: a twin with its race but without a container the kit writes, or unreadable items, isn't ready",
    guarded(function()
        for _, c in ipairs({ "BoostsContainer", "PassiveContainer", "SpellBook", "InventoryOwner" }) do
            twin(T1)
            twin(T2)
            world[T2].comps[c] = nil
            local j = RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() }).job]
            runJob(j)
            local want = T2 .. ": " .. (c == "InventoryOwner" and "items don't read" or "no " .. c)
            eq(enc({ j.status, tostring(j.error):find(want, 1, true) ~= nil }), enc({ "failed", true }),
                c .. ": " .. tostring(j.error))
            eq(#calls, 0, "no call made")
        end
    end))

test("a spells choice asks the game's SpellList set per pick, never walks it: the kit applies, a stray pick is refused",
    guarded(function()
        twin(T1)
        twin(T2)
        local spells = { kind = "spells", list = "cantrips", n = 2, exact = true,
            ids = { "Target_FireBolt", "Target_RayOfFrost" } }
        capped(function()
            local r = Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit({ choices = { spells } }) })
            local j = RPC.jobs[r.job]
            runJob(j)
            eq(j.status, "done", "job: " .. tostring(j.error))
        end)
        eq(enc(named("AddSpell")), enc({ { "AddSpell", T1, "Target_FireBolt", 0, 1 },
            { "AddSpell", T1, "Target_RayOfFrost", 0, 1 }, { "AddSpell", T2, "Target_FireBolt", 0, 1 },
            { "AddSpell", T2, "Target_RayOfFrost", 0, 1 } }), "each pick on both twins")
        spells.ids = { "Target_FireBolt", "Target_Sleep" }
        capped(function()
            local args = { twins = { T1, T2 }, race = HALFELF, kit = kit({ choices = { spells } }) }
            refused(function() Ar.Mirror.kit(args) end, "Target_Sleep is not in the game's list cantrips")
        end)
    end))

test("a list that can't be checked fails closed naming it: an element that doesn't read, a set that answers no bool",
    guarded(function()
        twin(T1)
        twin(T2)
        local skills = { kind = "skills", list = "skills-unread", n = 1, exact = true, ids = { "Athletics" } }
        refused(function() Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit({ choices = { skills } }) })
        end, "the game's SkillList skills-unread can't be checked: element 2 of 3 reads false")
        local get = Ext.StaticData.Get
        local answersNil = setmetatable({}, { __index = function() return nil end })
        Ext.StaticData.Get = function() return { Spells = answersNil } end
        local spells = { kind = "spells", list = "cantrips", n = 1, exact = true, ids = { "Target_FireBolt" } }
        local args = { twins = { T1, T2 }, race = HALFELF, kit = kit({ choices = { spells } }) }
        local ok, err = pcall(Ar.Mirror.kit, args)
        Ext.StaticData.Get = get
        eq(ok, false, "refused")
        eq(tostring(err):find("the game's SpellList cantrips can't be checked: Target_FireBolt reads nil", 1, true)
            ~= nil, true, tostring(err))
        eq(#calls, 0, "no call made")
    end))

test("U.items: an extender list's elements 1..#, bounded; a length past the cap is an error", function()
    local s = udSet({ "a", "b", "c" })
    local items, n = U.items(s)
    eq(enc({ n, items[1], items[3] }), enc({ 3, false, false }), "a set's integers read false: 3 of them, no more")
    eq(enc((U.items(udArray({ "x", "y" })))), enc({ "x", "y" }), "an array")
    local ok, err = pcall(U.items, s, 2)
    eq(enc({ ok, err }), enc({ false, "a list of length 3" }), "the cap")
end)

test("mirror_kit: an item that never arrives fails the job naming it", guarded(function()
    restore()
    game({ items_never_arrive = true })
    twin(T1)
    twin(T2)
    local j = RPC.jobs[Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() }).job]
    runJob(j)
    eq(j.status, "failed", "failed")
    eq(tostring(j.error):find("WPN_Longsword (root-sword) didn't reach both twins", 1, true) ~= nil, true,
        "names the item: " .. tostring(j.error))
    eq(#named("Equip"), 0, "nothing equipped")
end))

-- arena.setup with arena.mirror: the twins are made for the match (live check 12 step 13: a save keeps neither the
-- entries' grants nor the kit's boosts), pair i = teams.A[i], teams.B[i]; the job then goes on to the party step.
local function mirrorArena(over)
    local a = { location = { spawns = { A = { 0, 0, 0 }, B = { 0, 0, 10 } } }, seed = 7, karmic = false, level = 2,
        mirror = { pairs = {
            { template = TPL, race = HALFELF, stats = "BG3AI_Arena_Fighter_L1", names = { A = "Ash", B = "Birch" },
                kit = kit() },
            { template = TPL, race = HALFELF, stats = "BG3AI_Arena_Fighter_L1", names = { A = "Alder", B = "Bramble" },
                kit = kit() } } } }
    for k, v in pairs(over or {}) do a[k] = v end
    return a
end

-- Runs a mirror setup until it reaches the party step (or ends); the symmetry check answers `sym`.
local function mirrorSetup(a, sym)
    stats.BG3AI_Arena_Fighter_L1 = {}
    local scenario, check = BG3AI.Scenario, Ar.Symmetry.check
    local asked = {}
    BG3AI.Scenario = { enabled = function() return true end }
    Ar.Symmetry.check = function(p) asked[#asked + 1] = p return sym or { ok = true, problems = {} } end
    local ok, r = pcall(function()
        local j = RPC.jobs[Ar.Setup.start({ arena = a }).job]
        for _ = 1, 100 do
            if j.status ~= "running" or j.step >= 1 then break end
            tick()
        end
        return j
    end)
    BG3AI.Scenario, Ar.Symmetry.check = scenario, check
    if not ok then error(r, 0) end
    if r.status == "running" then r.status = "interrupted" end -- the party step on is test_arena_lua_setup.lua's
    return r, asked
end

test("a mirror setup: each pair spawned and kitted, names and symmetry checked, pair i at index i of both teams",
    guarded(function()
        local j, asked = mirrorSetup(mirrorArena())
        eq(j.step, 1, "at the party step: " .. tostring(j.error))
        -- seed 7: pair 1's A twin made first (7 + 1 even), pair 2's B twin first
        eq(enc(j.teams), enc({ A = { T1, T4 }, B = { T2, T3 } }), "teams")
        eq(enc({ j.names[T1], j.names[T3] }), enc({ "Ash", "Bramble" }), "names")
        eq(enc(asked[1]), enc({ { T1, T2 }, { T4, T3 } }), "symmetry over the pairs")
        eq(#named("AddPassive") > 0 and #named("Equip"), 4, "every twin kitted and its sword equipped")
        local made = named("CreateAt")
        eq(enc({ made[1][3], made[1][5], made[2][5], made[3][3], made[3][5], made[4][5] }),
            enc({ -0.75, 0, 10, 0.75, 10, 0 }), "pair 1 on each team's line first, pair 2 next")
    end))

test("a mirror setup alternates which side's twin is made first, pair to pair, flipped by the seed; recorded",
    guarded(function()
        eq(enc(Ar.Setup.spawn_order(7, 4)), enc({ "A", "B", "A", "B" }), "seed 7")
        eq(enc(Ar.Setup.spawn_order(8, 4)), enc({ "B", "A", "B", "A" }), "seed 8")
        local j = mirrorSetup(mirrorArena({ seed = 8 }))
        eq(j.step, 1, "at the party step: " .. tostring(j.error))
        local z = {}
        for _, c in ipairs(named("CreateAt")) do z[#z + 1] = c[5] end
        eq(enc(z), enc({ 10, 0, 0, 10 }), "B then A for pair 1, A then B for pair 2 (B's line is at z 10)")
        eq(enc(j.teams), enc({ A = { T2, T3 }, B = { T1, T4 } }), "teams by spot, not by creation")
        eq(enc(j.spawn_order), enc({ "B", "A" }), "recorded on the job")
        eq(enc(j.partial.spawn_order), enc({ "B", "A" }), "and in its partial result")
    end))

test("a mirror arena without an integer seed is refused before anything spawns", guarded(function()
    for _, seed in ipairs({ "none", -1, 1.5, "x" }) do
        local a = mirrorArena()
        a.seed = seed ~= "none" and seed or nil
        local ok, err = pcall(mirrorSetup, a)
        eq(ok, false, "refused: " .. tostring(seed))
        eq(tostring(err):find("a mirror arena needs arena.seed", 1, true) ~= nil, true, tostring(err))
    end
    eq(#named("CreateAt"), 0, "nothing made")
end))

test("a mirror setup fails on a twin that doesn't read its own name, or on any symmetry problem", guarded(function()
    local j = mirrorSetup(mirrorArena(), { ok = false, problems = { "pair 1 (Ash, Birch): passives differs" } })
    eq(j.status, "failed", "failed")
    eq(tostring(j.error):find("the twins are not mirrors: pair 1 (Ash, Birch): passives differs", 1, true) ~= nil,
        true, tostring(j.error))
    restore()
    game()
    stats.BG3AI_Arena_Fighter_L1 = {}
    local a = mirrorArena()
    local kitRun = Ar.Mirror.kit
    Ar.Mirror.kit = function(args) NAMES[T1] = "Alder" return kitRun(args) end -- a later rename took pair 1's
    local ok, got = pcall(mirrorSetup, a)
    Ar.Mirror.kit = kitRun
    if not ok then error(got, 0) end
    j = got
    eq(tostring(j.error):find("Ash (" .. T1 .. ") reads Alder", 1, true) ~= nil, true, tostring(j.error))
end))

-- Live check 13: setup failed in 0.58 s, both Fighters reading the template's name; a minute later each read its own.
test("a mirror setup waits for the names to land: 3 s after the renames it goes on", guarded(function()
    restore()
    game({ name_lag_ms = 3000 })
    local j = mirrorSetup(mirrorArena())
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    eq(enc({ NAMES[T1], NAMES[T2], NAMES[T3], NAMES[T4] }), enc({ "Ash", "Birch", "Bramble", "Alder" }),
        "names (seed 7: pair 2's B twin made first)")
end))

test("a name that never lands fails setup at NAME_WAIT_MS, naming the copy and what it reads", guarded(function()
    restore()
    game({ shared_name = true }) -- one handle for every copy: each reads the last name, Alder (seed 7: made last)
    local from = clock
    local j = mirrorSetup(mirrorArena())
    eq(j.status, "failed", "failed")
    local waited = clock - from
    eq(waited >= Ar.Mirror.NAME_WAIT_MS and waited <= Ar.Mirror.NAME_WAIT_MS + 1000, true, "failed at " .. waited)
    eq(tostring(j.error):find("Ash (" .. T1 .. ") reads Alder", 1, true) ~= nil, true, tostring(j.error))
end))

test("a mirror setup whose copies' components land 3 s after the spawn goes on to the party step", guarded(function()
    restore()
    game({ ready_lag_ms = 3000 })
    local j = mirrorSetup(mirrorArena())
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    eq(#named("Equip"), 4, "every twin kitted")
end))

test("a twin with another StatsId fails setup at the limit, naming it", guarded(function()
    restore()
    game({ stats_id = "HeroHalfElfFemale" })
    local j = mirrorSetup(mirrorArena())
    eq(j.status, "failed", "failed")
    eq(tostring(j.error):find("Ash (" .. T1 .. ") has stats HeroHalfElfFemale", 1, true) ~= nil, true,
        tostring(j.error))
end))

-- Live check 13d: Cedar spawned on a surface at spot A (INSURFACE), Dune not: the pair's statuses differed.
test("a mirror setup clears Ground and Cloud surfaces at every spot before the first twin spawns", guarded(function()
    local j = mirrorSetup(mirrorArena())
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    local got = {}
    for _, c in ipairs(calls) do
        if c[1] == "CreateAt" then break end
        if c[1] == "RemoveSurfaceLayerAtPosition" then got[#got + 1] = c end
    end
    local want = { -- the whole arena first: midpoint (0, 0, 5), radius 10/2 + two Dashes 18
        { "RemoveSurfaceLayerAtPosition", 0.0, 0.0, 5.0, "Ground", 23.0 },
        { "RemoveSurfaceLayerAtPosition", 0.0, 0.0, 5.0, "Cloud", 23.0 } }
    for i = 1, 2 do
        for _, s in ipairs({ "A", "B" }) do
            local p = Ar.Setup.line(({ A = { 0, 0, 0 }, B = { 0, 0, 10 } })[s], 2)[i]
            for _, layer in ipairs({ "Ground", "Cloud" }) do
                want[#want + 1] = { "RemoveSurfaceLayerAtPosition", p[1], p[2], p[3], layer, 2.0 }
            end
        end
    end
    eq(enc(got), enc(want), "both layers over the arena, then at all 4 spots, before any CreateAt")
    eq(enc(j.surfaces), enc({ points = 207, read = 207, radius = 11.75, off = 0,
        wide = { points = 596, read = 596, radius = 23.0, off = 0, tiles = {} } }),
        "every tile pair read: checked within 11.75 m, report-only out to 23 m")
end))

-- Fairness audit, site 2: every match opened with Fire 2 m from B's line (outside the 2 m spot clears), 8 m from A's.
test("surfaces the clear leaves that don't mirror fail setup naming them, before any twin spawns", guarded(function()
    restore()
    game({ tiles = { { 1, 8, "Fire", stays = true }, { 2, 8, "Fire", stays = true }, { 3, 3, "Water" } } })
    local j = mirrorSetup(mirrorArena())
    eq(j.status, "failed", "failed")
    eq(tostring(j.error), "the surfaces don't mirror after the clear (2 of 207 tiles read within 11.8 m): "
        .. "(1.0, 2.0) on A's half reads None/None, its mirror (1.0, 8.0) Fire/None; "
        .. "(2.0, 2.0) on A's half reads None/None, its mirror (2.0, 8.0) Fire/None", "names each (the water cleared)")
    eq(#named("CreateAt"), 0, "no twin")
end))

test("surfaces that mirror after the clear pass; no tile that reads fails", guarded(function()
    restore()
    game({ tiles = { { 1, 8, "Fire", stays = true }, { 1, 2, "Fire", stays = true }, { -4, 9, "Water" } } })
    local j = mirrorSetup(mirrorArena())
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    restore()
    game({ no_tiles = true })
    j = mirrorSetup(mirrorArena())
    eq(j.status, "failed", "failed")
    eq(tostring(j.error), "no tile pair on A's half within 11.8 m of the arena's midpoint reads its surfaces "
        .. "(207 tried)", "fails closed")
    eq(#named("CreateAt"), 0, "no twin")
end))

-- Cross-review 7828a6f8 (1): site 2 is not flat (A's back line lower). A tile is read on its own ground, not at its
-- side's spawn height; a pair where one tile doesn't read (no floor there) is unread, not a mismatch.
test("surface tiles are read on their own ground; a tile that doesn't read leaves its pair unread", guarded(function()
    restore()
    game({ tiles = { { 1, 2, "Fire", stays = true }, { 1, 8, "Fire", stays = true } },
        ground = function(x, z)
            if x == 2 and z == 2 then return {} end -- a hole on A's half
            return { z < 5 and -2.0 or 0.0 } -- A's half 2 m lower than its spawn
        end })
    local j = mirrorSetup(mirrorArena())
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    eq(j.surfaces.points, 207, "tiles on A's half")
    eq(j.surfaces.read, 206, "every pair read but the hole's")
    eq(j.surfaces.off, 0, "the fires mirror")
end))

-- Cross-review 7828a6f8 (2): seed-7 m2's later fights ran into Fire 14.6-20 m from the midpoint. Out there the read
-- is report-only: the result names it, setup goes on.
test("a surface that doesn't mirror beyond the checked radius is reported, not a setup failure", guarded(function()
    restore()
    game({ tiles = { { 0, -12, "Fire", stays = true } } }) -- 17 m from the midpoint, on A's half
    local j = mirrorSetup(mirrorArena())
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    eq(j.surfaces.off, 0, "nothing within the checked radius")
    eq(j.surfaces.wide.off, 1, "one tile out to the clear's reach")
    eq(j.surfaces.wide.tiles[1], "(0.0, -12.0) on A's half reads Fire/None, its mirror (0.0, 22.0) None/None", "named")
end))

-- Cross-review 7828a6f8 (3): does a cleared fire come back mid-match? The grid read again at round 2 and at the end.
test("a mirror arena's surfaces are read again at round 2's start and at the end, before arena_end", guarded(function()
    restore()
    game({ tiles = { { 3, 1, "Fire", stays = true } } })
    reset()
    begin({ surfaces = { spawns = { A = { 0, 0, 0 }, B = { 0, 0, 10 } }, n = 2 } })
    fire("CombatRoundStarted", "combat1", 2)
    fire("CombatRoundStarted", "combat1", 2) -- once only
    Ar.finish(nil, "aborted")
    local r = refs("arena_surfaces")
    eq(#r, 2, "round 2 and the end")
    eq(enc({ r[1].when, r[1].round, r[1].off, r[1].points, r[1].read }), enc({ "round 2", 2, 1, 803, 803 }), "round 2")
    eq(r[1].tiles[1], "(3.0, 1.0) on A's half reads Fire/None, its mirror (3.0, 9.0) None/None", "the fire named")
    eq(r[2].when, "end", "the end")
    local ring = BG3AI.Referee.ring
    eq(ring[#ring].ev, "arena_end", "arena_end last")
    reset()
    begin()
    Ar.finish(nil, "aborted")
    eq(#refs("arena_surfaces"), 0, "an arena without mirror surfaces: no re-read")
end))

test("a surface clear the game refuses fails setup naming it, before any twin spawns", guarded(function()
    restore()
    game({ surface_call_fails = true })
    local j = mirrorSetup(mirrorArena())
    eq(j.status, "failed", "failed")
    eq(tostring(j.error):find("RemoveSurfaceLayerAtPosition Ground over the arena (23.0 m): ", 1, true) ~= nil, true,
        tostring(j.error))
    eq(#named("CreateAt"), 0, "no twin")
end))

test("mirror arena refusals: a pair short of a field, one name twice, teams that name other twins", guarded(function()
    local function start(a) return Ar.Setup.check_mirror(a.mirror, a.teams) end
    local a = mirrorArena()
    a.mirror.pairs[2].kit = nil
    refused(function() start(a) end, "pairs[2] needs template, race, stats, names and kit")
    a = mirrorArena()
    a.mirror.pairs[2].names.B = "ash"
    refused(function() start(a) end, "the name ash is used twice")
    a = mirrorArena({ teams = { A = { { name = "Ash" }, { name = "Alder" } }, B = { { name = "Birch" } } } })
    refused(function() start(a) end, "arena.teams.B[2] is nil, the mirror's pair 2 names Bramble")
    a = mirrorArena({ teams = { A = { { name = "Ash" }, { name = "Alder" } }, B = { { name = "Birch" },
        { name = "Bramble" } } } })
    start(a)
    eq(#named("CreateAt"), 0, "nothing made")
end))
