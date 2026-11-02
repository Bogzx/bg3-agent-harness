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
local LISTS = { -- the game's lists by uuid: {resource type, field, ids}
    ["skills-fighter"] = { "SkillList", "Skills", { "Acrobatics", "Athletics", "Intimidation", "Perception" } },
    ["styles"] = { "PassiveList", "Passives", { "FightingStyle_Defense", "FightingStyle_Duelling" } },
    ["cantrips"] = { "SpellList", "Spells", { "Target_FireBolt", "Target_RayOfFrost", "Target_MageHand" } },
}

local saved, calls, stats, created, tpl, inv

local function stub(name, fn)
    if saved[name] == nil then saved[name] = rawget(Osi, name) or false end
    rawset(Osi, name, fn)
end

local function twin(u, over)
    over = over or {}
    world[u] = { pos = { 0, 0, 0 }, comps = { ServerCharacter = {}, Race = { Race = over.race or HALFELF },
        Data = { StatsId = over.stats }, EocLevel = { Level = over.level or 1 },
        InventoryOwner = { Inventories = {
            { InventoryData = { Type = "Equipment" }, InventoryContainer = { Items = {} } },
            { InventoryData = { Type = "Inventory" }, InventoryContainer = { Items = over.bag or {} } } } } } }
    osiv[key("IsOnStage", u)], osiv[key("IsCharacter", u)] = 1, 1
end

local function game(opts)
    opts = opts or {}
    saved, calls, created, inv = { ext = { Ext.Stats, Ext.StaticData.Get, Ext.Template } }, {}, {}, {}
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
    local pending = { T1, T2, T3, T4 }
    stub("CreateAt", function(t, x, y, z)
        calls[#calls + 1] = { "CreateAt", t, x, y, z, tpl.Stats }
        if opts.create_fails then error("CreateAt refused") end
        local u = table.remove(pending, 1)
        twin(u, { stats = opts.stats_id or tpl.Stats })
        created[#created + 1] = u
        return u
    end)
    for _, n in ipairs({ "MakePlayer", "AddBoosts", "AddPassive", "AddSpell", "Equip" }) do
        stub(n, function(...) calls[#calls + 1] = { n, ... } end)
    end
    local landing = {}
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
        for _, l in ipairs(landing) do if clock >= l.at then NAMES[l.u] = l.name end end
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
        restore()
        game({ stats_id = "HeroHalfElfFemale" })
        stats.BG3AI_Arena_Fighter_L1 = {}
        refused(function() Ar.Mirror.spawn(spawnArgs()) end, T1 .. " has stats HeroHalfElfFemale")
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
        refused(function() Ar.Mirror.kit({ twins = { T1, T2 }, race = "race-gith", kit = kit() }) end, "is of race")
        twin(T2, { bag = { { Item = { Uuid = { EntityUuid = "bag-1" } } } } })
        osiv[key("GetTemplate", "bag-1")] = "OBJ_X"
        refused(function() Ar.Mirror.kit({ twins = { T1, T2 }, race = HALFELF, kit = kit() }) end,
            "already holds items")
        eq(#calls, 0, "no call made")
    end))

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
        eq(enc(j.teams), enc({ A = { T1, T3 }, B = { T2, T4 } }), "teams")
        eq(enc({ j.names[T1], j.names[T4] }), enc({ "Ash", "Bramble" }), "names")
        eq(enc(asked[1]), enc({ { T1, T2 }, { T3, T4 } }), "symmetry over the pairs")
        eq(#named("AddPassive") > 0 and #named("Equip"), 4, "every twin kitted and its sword equipped")
        local made = named("CreateAt")
        eq(enc({ made[1][3], made[1][5], made[2][5], made[3][3] }), enc({ -0.75, 0, 10, 0.75 }),
            "pair 1 on each team's line first, pair 2 next")
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
    eq(enc({ NAMES[T1], NAMES[T2], NAMES[T3], NAMES[T4] }), enc({ "Ash", "Birch", "Alder", "Bramble" }), "names")
end))

test("a name that never lands fails setup at NAME_WAIT_MS, naming the copy and what it reads", guarded(function()
    restore()
    game({ shared_name = true }) -- one handle for every copy: each reads the last name, Bramble
    local from = clock
    local j = mirrorSetup(mirrorArena())
    eq(j.status, "failed", "failed")
    local waited = clock - from
    eq(waited >= Ar.Mirror.NAME_WAIT_MS and waited <= Ar.Mirror.NAME_WAIT_MS + 1000, true, "failed at " .. waited)
    eq(tostring(j.error):find("Ash (" .. T1 .. ") reads Bramble", 1, true) ~= nil, true, tostring(j.error))
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
