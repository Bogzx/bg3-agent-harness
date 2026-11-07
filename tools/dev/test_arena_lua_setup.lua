-- Part of test_arena_lua.lua (split for the 1000-line cap): arena.setup {arena, round_cap}, the job that stands the
-- two teams up (party, lines, factions, one combat, the IsEnemy check) and begins the match. The game here is a
-- scripted fake: Osiris calls are recorded and change what the queries answer, as the arena spike saw live.
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local A3 = "aaaaaaaa-0000-0000-0000-0000000000a3"
local A4 = "aaaaaaaa-0000-0000-0000-0000000000a4"
local B3 = "bbbbbbbb-0000-0000-0000-0000000000b3"
local B4 = "bbbbbbbb-0000-0000-0000-0000000000b4"
local AV = "eeeeeeee-0000-0000-0000-0000000000e1" -- the avatar (the host)
local SH = "cccccccc-0000-0000-0000-0000000000c1" -- a party member on no team (Shadowheart, live 6)
local TA, TB = { A1, A2, A3, A4 }, { B1, B2, B3, B4 }
local ALL = { A1, A2, A3, A4, B1, B2, B3, B4 }
NAMES[A3], NAMES[A4], NAMES[B3], NAMES[B4], NAMES[AV], NAMES[SH] = "Ari", "Axe", "Bix", "Bree", "Tav", "Sha"

local saved = {}
local calls, factionOf, enemyFrom, combatOf, scn, canJoin

local FACTIONS = { -- static data: guid -> name
    g0 = "Hero", g1 = "Hero Player1", g2 = "Hero Player2", g3 = "Hero Player3", g4 = "Hero Player4",
    g5 = "Hero Player5", g9 = "Companion4",
}
local function form(g) return FACTIONS[g] .. "_" .. g end

local function stub(name, fn)
    if saved[name] == nil then saved[name] = rawget(Osi, name) or false end
    rawset(Osi, name, fn)
end

local function db(rows)
    return { Get = function()
        local out = {}
        for _, u in ipairs(rows) do out[#out + 1] = { "S_Player_" .. u } end
        return out
    end }
end

-- The fake game: 8 team members, the avatar and a party member on no team (SH) on the level; the combat call puts
-- the given characters into combat1 (opts.combat, default all 8) and makes the teams enemies after opts.enemy_after
-- reads of the matrix (default 0).
local function game(opts)
    opts = opts or {}
    reset()
    for i, u in ipairs({ A3, A4, B3, B4, AV, SH }) do
        world[u] = { pos = { 50 + i, 0, 0 }, comps = { ServerCharacter = {}, Health = { Hp = 20, MaxHp = 20 } } }
        osiv[key("IsOnStage", u)], osiv[key("IsCharacter", u)] = 1, 1
    end
    world[GOB].comps.ServerCharacter = {} -- an NPC in the level, holding Hero Player1
    partyRows = { A1, A2, B1, AV, SH } -- the rest are origins not in the party yet
    calls, factionOf, enemyFrom, combatOf = {}, {}, nil, {}
    for _, u in ipairs(ALL) do
        factionOf[u] = form("g9")
        osiv[key("IsInCombat", u)], osiv[key("CombatGetGuidFor", u)] = 0, nil
        osiv[key("GetLevel", u)], osiv[key("GetMaxHitpoints", u)], osiv[key("GetHitpoints", u)] = 4, 20, 20
    end
    osiv[key("GetMaxHitpoints", B2)] = 52 -- joining resets her stat block (Minthara, spike)
    osiv[key("GetHitpoints", A2)] = 7     -- short after RestoreParty
    factionOf[AV], factionOf[GOB] = form("g1"), form("g1")
    osiv[key("IsInCombat", AV)], osiv[key("CombatGetGuidFor", AV)] = 0, nil
    osiv[key("GetHostCharacter")] = AV
    stub("DB_Avatars", db({ AV }))
    stub("DB_Origins", db({ A1, A2, A3, A4, B1, B2, B3, B4 }))
    stub("DB_Players", db({ AV }))
    local reads = 0
    local function record(name) return function(...) calls[#calls + 1] = { name, ... } end end
    for _, n in ipairs({ "SetMaxPartySizeOverride", "DetachFromPartyGroup", "AttachToPartyGroup",
        "RestoreParty", "SetHitpointsPercentage", "SetRelation" }) do
        stub(n, record(n))
    end
    stub("TeleportToPosition", function(u, x, y, z, ...) -- opts.land[u]: where the game puts it off the asked spot
        calls[#calls + 1] = { "TeleportToPosition", u, x, y, z, ... }
        local d = (opts.land or {})[u] or { 0, 0, 0 }
        world[u].pos = { x + d[1], y + d[2], z + d[3] }
    end)
    canJoin = {}
    stub("CanJoinCombat", function(u) return canJoin[u] or 1 end)
    stub("SetCanJoinCombat", function(u, v)
        calls[#calls + 1] = { "SetCanJoinCombat", u, v }
        if not opts.join_switch_ignored then canJoin[u] = v end
    end)
    stub("PROC_GLO_PartyMembers_Add", function(u, host)
        calls[#calls + 1] = { "PROC_GLO_PartyMembers_Add", u, host }
        partyRows[#partyRows + 1] = u
        osiv[key("GetMaxHitpoints", B2)], osiv[key("GetHitpoints", B2)] = 11, 11
    end)
    stub("SetFaction", function(u, f) calls[#calls + 1] = { "SetFaction", u, f }; factionOf[u] = f end)
    stub("GetFaction", function(u) return factionOf[u] end)
    stub("SetHostileAndEnterCombat", function(...)
        calls[#calls + 1] = { "SetHostileAndEnterCombat", ... }
        for _, u in ipairs(opts.combat or ALL) do combatOf[u] = "combat1" end
        enemyFrom = reads + (opts.enemy_after or 0)
        fire("CombatRoundStarted", "combat1", 1) -- round 1 starts before the match begins (live 7)
    end)
    stub("EnterCombat", function(x, y)
        calls[#calls + 1] = { "EnterCombat", x, y }
        if opts.fallback_joins then combatOf[x], combatOf[y] = "combat1", "combat1" end
    end)
    stub("CombatGetGuidFor", function(u) return combatOf[u] end)
    stub("IsInCombat", function(u) return combatOf[u] and 1 or 0 end)
    stub("IsEnemy", function(x, y)
        if x == A1 and y == B1 then reads = reads + 1 end -- one read of the matrix
        if not enemyFrom or reads <= enemyFrom then return 0 end
        local sx = (x:sub(1, 1) == "a") and "A" or "B"
        local sy = (y:sub(1, 1) == "a") and "A" or "B"
        return sx ~= sy and 1 or 0
    end)
    if opts.avatar_joins then
        local hostile = Osi.SetHostileAndEnterCombat
        stub("SetHostileAndEnterCombat", function(...) hostile(...); combatOf[AV] = "combat1" end)
    end
    saved.static = saved.static or { Ext.StaticData.Get, Ext.StaticData.GetAll }
    Ext.StaticData.GetAll = function(t)
        if t ~= "Faction" then return {} end
        local out = {}
        for g in pairs(FACTIONS) do out[#out + 1] = g end
        table.sort(out)
        return out
    end
    Ext.StaticData.Get = function(g, t)
        if t == "Faction" then return { Faction = FACTIONS[g] } end
        return { Name = g }
    end
    scn = { seeds = {}, karmic = {} }
    BG3AI.Scenario = { enabled = function() return env.BG3AI_SCENARIO_SETUP == "1" end,
        set_seed = function(s) scn.seeds[#scn.seeds + 1] = s return { seed = s } end,
        setKarmic = function(on) scn.karmic[#scn.karmic + 1] = on return { karmic = on } end }
    env.BG3AI_SCENARIO_SETUP = "1"
end

local function restore()
    for _, j in pairs(RPC.jobs) do -- a setup a test started and left running must not run on in the next one
        if j.kind == "arena.setup" and j.status == "running" then j.status = "interrupted" end
    end
    for name, v in pairs(saved) do
        if name ~= "static" then rawset(Osi, name, v or nil) end
    end
    if saved.static then Ext.StaticData.Get, Ext.StaticData.GetAll = saved.static[1], saved.static[2] end
    saved = {}
end

local function arena(over)
    local a = { teams = { A = { { name = "Ana" }, { name = "abe" }, { name = "Ari" }, { name = "Axe" } },
        B = { { name = "Bo", uuid = B1 }, { name = "Bea" }, { name = "Bix" }, { name = "Bree" } } },
        location = { spawns = { A = { 0, 0, 0 }, B = { 0, 0, 10.5 } } }, seed = 7, karmic = false, round_cap = 20,
        level = 4 }
    for k, v in pairs(over or {}) do a[k] = v end
    return a
end

local function lua(v) -- a Lua literal of v (rpc() takes the request's args as one)
    if type(v) == "table" then
        local out = {}
        for i, x in ipairs(v) do out[i] = lua(x) end
        for k, x in pairs(v) do
            if type(k) ~= "number" then out[#out + 1] = k .. "=" .. lua(x) end
        end
        return "{" .. table.concat(out, ",") .. "}"
    end
    return type(v) == "string" and string.format("%q", v) or tostring(v)
end

-- The answer, and the job it started (if any).
local function setup(a, side)
    local r = rpc("arena.setup", lua({ arena = a, round_cap = 12 }), side)
    local id = type(r.result) == "table" and r.result.job
    return r, id and RPC.jobs[id]
end

local function run(j, limit)
    for _ = 1, limit or 200 do
        if j.status ~= "running" then return end
        tick()
    end
end

local function named(name)
    local out = {}
    for _, c in ipairs(calls) do if c[1] == name then out[#out + 1] = c end end
    return out
end

local function test_setup(title, fn)
    test(title, function()
        local ok, err = pcall(fn)
        restore()
        if not ok then error(err, 0) end
    end)
end

-- A build's reference (website ops/logs/arena-4v4-builds.md G7): read after RestoreParty, before anyone is hostile.
local function refJob(profiles)
    local r, j = setup(arena())
    eq(r.result and r.result.status, "running", "a job: " .. enc(r))
    local base = {}
    for _, f in ipairs(Ar.Symmetry.FIELDS) do base[f] = {} end
    base.passives = { "DiscipleOfLife" }
    j.mirror = { pairs = { { build = "fighter", reference = { profile = base } },
        { build = "cleric", reference = { profile = base } }, {}, {} } }
    local profile, check = Ar.Symmetry.profile, Ar.Symmetry.check
    local read = {}
    Ar.Symmetry.profile = function(u)
        read[#read + 1] = { u, #named("RestoreParty"), #named("SetHostileAndEnterCombat") }
        local p = {}
        for k, v in pairs(base) do p[k] = v end
        p.passives = profiles[u] or base.passives
        return p
    end
    Ar.Symmetry.check = function() return { ok = true, problems = {} } end
    local ok, err = pcall(run, j)
    Ar.Symmetry.profile, Ar.Symmetry.check = profile, check
    if not ok then error(err, 0) end
    return j, read
end

test_setup("every twin that differs from its build's reference is named and setup fails before anyone is hostile",
    function()
        game()
        local j = refJob({ [A1] = { "Other" }, [A2] = { "Another" } })
        eq(j.status, "failed", "failed")
        assert(j.error:find("pair 1", 1, true) and j.error:find("build fighter", 1, true)
            and j.error:find("pair 2", 1, true) and j.error:find("Another", 1, true), j.error)
        eq(#named("SetHostileAndEnterCombat"), 0, "nobody hostile")
        eq(j.result.positions[A1] ~= nil and j.result.positions[B4] ~= nil, true, "where they stood, for the record")
        eq(enc(j.result.reference[2].diffs[1].only_reference), enc({ "DiscipleOfLife" }), "the rows in the result")
    end)

test_setup("twins that are their builds: read after RestoreParty, the rows in the result, the match begins", function()
    game()
    local j, read = refJob({})
    eq(j.status, "done", tostring(j.error))
    eq(read[1][1], A1, "the A twin read")
    eq(read[1][2], 1, "after RestoreParty")
    eq(read[1][3], 0, "before the hostile call")
    eq(enc({ j.result.reference[1].ok, j.result.reference[2].build, #j.result.reference }), enc({ true, "cleric", 2 }),
        "pairs without a reference aren't listed")
end)

-- A mirror arena's pairs are checked again as the fight starts, statuses included (live check 13d: a twin on a
-- surface read INSURFACE after setup, its pair not). The teams stand in for the twins: only the last step reads mirror.
test_setup("a mirror arena whose pairs differ as the fight starts fails setup; the match doesn't begin", function()
    game()
    local r, j = setup(arena())
    eq(r.result and r.result.status, "running", "a job: " .. enc(r))
    j.mirror = { pairs = { {}, {}, {}, {} } }
    local check, asked = Ar.Symmetry.check, {}
    Ar.Symmetry.check = function(p, opts)
        asked[#asked + 1] = p
        asked.start = opts and opts.start
        return { ok = false, problems = { "pair 2 (Abe, Bea): statuses differs" } }
    end
    local ok, err = pcall(run, j)
    Ar.Symmetry.check = check
    if not ok then error(err, 0) end
    eq(j.status, "failed", "failed")
    eq(j.error, "the twins are not mirrors as the fight starts: pair 2 (Abe, Bea): statuses differs", "error")
    eq(asked.start, true, "the start-of-fight check (current and temp HP too, audit S3-8)")
    asked.start = nil
    eq(enc(asked), enc({ { { A1, B1 }, { A2, B2 }, { A3, B3 }, { A4, B4 } } }), "the pairs, once")
    eq(Ar.active(), false, "no match")
end)

test_setup("arena.setup: party, lines, spare factions, one combat, IsEnemy right: the match begins", function()
    game()
    local r, j = setup(arena())
    eq(r.result and r.result.status, "running", "a job: " .. enc(r))
    j.spawn_order = { "B", "A", "B", "A" } -- a mirror arena's (test_arena_lua_mirror.lua): the result carries it
    run(j)
    eq(j.status, "done", "setup: " .. tostring(j.error))
    eq(enc(j.result.spawn_order), enc({ "B", "A", "B", "A" }), "the spawn order in the result")
    eq(enc({ j.result.positions[A1], j.result.positions[B4] }), enc({ { -2.25, 0, 0 }, { 2.25, 0, 10.5 } }),
        "where the members stand as the fight starts")
    local pc = 0
    for _ in pairs(j.result.positions) do pc = pc + 1 end
    eq(pc, 8, "every member's position")
    eq(enc(refs("arena_start")[1].positions), enc(j.result.positions), "arena_start records them too")
    eq(Ar.state.seed, 7, "the arena's seed reaches the match (its tie fallback side)")
    eq(#named("SetMaxPartySizeOverride"), 1, "party size")
    eq(named("SetMaxPartySizeOverride")[1][2], 8, "8")
    local added = {}
    for _, c in ipairs(named("PROC_GLO_PartyMembers_Add")) do added[#added + 1] = c[2]; eq(c[3], AV, "by the host") end
    eq(table.concat(added, ","), table.concat({ A3, A4, B2, B3, B4 }, ","), "the 5 not in the party added")
    local det = {}
    for _, c in ipairs(named("DetachFromPartyGroup")) do det[#det + 1] = c[2] end
    eq(table.concat(det, ","), table.concat({ A1, A2, A3, A4, B1, B2, B3, B4, AV, SH }, ","),
        "all detached, the avatar and the party member on no team too")
    local att = {}
    for _, c in ipairs(named("AttachToPartyGroup")) do att[#att + 1] = c[2] .. ">" .. c[3] end
    eq(table.concat(att, ","), table.concat({ A2 .. ">" .. A1, A3 .. ">" .. A1, A4 .. ">" .. A1, B2 .. ">" .. B1,
        B3 .. ">" .. B1, B4 .. ">" .. B1 }, ","), "each team chained to its first")
    local tp = named("TeleportToPosition")
    eq(#tp, 8, "8 placed, the sitters not")
    eq(enc({ tp[1][2], tp[1][3], tp[1][5] }), enc({ A1, -2.25, 0 }), "A1 at the line's left end")
    eq(enc({ tp[8][2], tp[8][3], tp[8][5] }), enc({ B4, 2.25, 10.5 }), "B4 at B's right end")
    eq(#named("RestoreParty"), 1, "RestoreParty")
    local hp = named("SetHitpointsPercentage")
    eq(#hp, 1, "only the short one healed")
    eq(hp[1][2], A2, "A2")
    eq(enc(scn.seeds), enc({ 7 }), "the arena's seed")
    eq(enc(scn.karmic), enc({ false }), "Karmic Dice off")
    local fa, fb = form("g2"), form("g3") -- Hero Player1 is used (an NPC, the avatar); never the bare Hero
    for _, c in ipairs(named("SetFaction")) do
        eq(c[3], c[2]:sub(1, 1) == "a" and fa or fb, "faction of " .. c[2])
    end
    eq(#named("SetFaction"), 8, "8 factions set")
    local rel = named("SetRelation")
    eq(enc({ rel[1][2], rel[1][3], rel[1][4], rel[2][2], rel[2][3], rel[2][4] }), enc({ fa, fb, 0, fb, fa, 0 }),
        "hostile both ways")
    local h = named("SetHostileAndEnterCombat")[1]
    eq(enc({ h[2], h[3], h[4], h[5] }), enc({ fa, fb, A1, B1 }), "the combat call")
    eq(#named("EnterCombat"), 0, "no fallback")
    eq(j.result.enemy.across, "32/32", "across")
    eq(j.result.enemy.within, "0/24", "within")
    eq(j.result.combat, "combat1", "one combat")
    eq(enc(j.result.members[B2].max_hp), enc({ 52, 11 }), "B2's max HP before/after")
    eq(enc(j.result.members[A1].faction), enc({ form("g9"), fa }), "A1's faction before/after")
    eq(enc(j.result.factions), enc({ A = fa, B = fb }), "factions")
    eq(table.concat(Ar.state.teams.A, ","), table.concat(TA, ","), "team A begun")
    eq(table.concat(Ar.state.teams.B, ","), table.concat(TB, ","), "team B begun")
    eq(Ar.state.round_cap, 12, "the runner's round cap")
    eq(Ar.state.round, 1, "the match begins in round 1")
    eq(j.result.status.round, 1, "its status: round 1")
    eq(#refs("arena_start"), 1, "arena_start recorded")
    eq(enc(j.result.sitters), enc({ { uuid = AV, name = "Tav" }, { uuid = SH, name = "Sha" } }), "the sitters")
    local sw = {}
    for _, c in ipairs(named("SetCanJoinCombat")) do sw[#sw + 1] = c[2] .. "=" .. c[3] end
    eq(table.concat(sw, ","), AV .. "=0," .. SH .. "=0", "the sitters can't join the fight")
    eq(enc(Ar.state.sitters), enc({ [AV] = 1, [SH] = 1 }), "their CanJoinCombat before, for arena.reset")
    rpc("arena.reset")
    sw = {}
    for _, c in ipairs(named("SetCanJoinCombat")) do sw[#sw + 1] = c[2] .. "=" .. c[3] end
    table.sort(sw)
    eq(table.concat(sw, ","), SH .. "=0," .. SH .. "=1," .. AV .. "=0," .. AV .. "=1", "arena.reset gives it back")
end)

test_setup("arena.setup: a sitter the game still lets join the fight fails setup, named", function()
    game({ join_switch_ignored = true })
    local _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "failed")
    eq(j.error, "sitters that can still join the fight after SetCanJoinCombat 0: Tav CanJoinCombat 1, "
        .. "Sha CanJoinCombat 1", "error")
    eq(#named("TeleportToPosition"), 0, "nobody placed")
end)

test_setup("arena.setup: 3 v 3: the 2 others in the party sit out; IsEnemy 18/18 across, 0/12 within", function()
    local six = { A1, A2, A3, B1, B2, B3 }
    game({ combat = six })
    local a = arena()
    a.teams.A[4], a.teams.B[4] = nil, nil
    local _, j = setup(a)
    run(j)
    eq(j.status, "done", "setup: " .. tostring(j.error))
    eq(j.result.enemy.across, "18/18", "across")
    eq(j.result.enemy.within, "0/12", "within")
    eq(#named("TeleportToPosition"), 6, "6 placed")
    eq(enc(j.result.sitters), enc({ { uuid = AV, name = "Tav" }, { uuid = SH, name = "Sha" } }), "the sitters")
    eq(table.concat(Ar.state.teams.B, ","), table.concat({ B1, B2, B3 }, ","), "team B begun")
end)

test_setup("arena.setup: anyone on no team in the fight fails setup, named: a party member, an NPC", function()
    game({ combat = { A1, A2, A3, A4, B1, B2, B3, B4, SH, GOB } })
    local _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "others joined")
    eq(j.error, "not on a team and in the fight: Sha, Goblin", "error")
    eq(Ar.state, nil, "no match begun")
    game({ combat = { A1, A2, A3, B1, B2, B3, A4 } }) -- 3 v 3: A4, an origin on no team, joined
    local a = arena()
    a.teams.A[4], a.teams.B[4] = nil, nil
    _, j = setup(a)
    run(j)
    eq(j.error, "not on a team and in the fight: Axe", "an origin on no team")
end)

-- fairness audit S1-3 / live 14: a Nautiloid Tank and a Cartilaginous Chest took turns in the combat
test_setup("arena.setup: an item in the fight fails setup, named as an item", function()
    local TANK = "dddddddd-0000-0000-0000-000000000001"
    game({ combat = { A1, A2, A3, A4, B1, B2, B3, B4, TANK } })
    world[TANK], NAMES[TANK] = { pos = { 5, 0, 5 }, comps = { ServerItem = {} } }, "Nautiloid Tank"
    local _, j = setup(arena())
    run(j)
    world[TANK], NAMES[TANK] = nil, nil
    eq(j.status, "failed", "an item joined")
    eq(j.error, "not on a team and in the fight: Nautiloid Tank (an item)", "error")
    eq(Ar.state, nil, "no match begun")
end)

-- fairness audit S1-3: the sitters are bodies in the field; none may stand near one team's line
test_setup("arena.setup: a sitter under 10 m from a member of either line fails setup, named; 10 m passes", function()
    game()
    world[SH].pos = { 12, 0, 10.5 } -- Bree, B's 4th, lands at (2.25, 10.5): 9.75 m
    local _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "Sha 9.75 m from B's line")
    eq(j.error, "the sitter Sha stands 9.8 m from Bree (team B's line); a sitter must stand 10 m or more from both "
        .. "lines", "error")
    eq(#named("RestoreParty"), 0, "stopped before RestoreParty")
    game()
    world[SH].pos = { 12.3, 0, 10.5 } -- 10.05 m
    _, j = setup(arena())
    run(j)
    eq(j.status, "done", "10.05 m: " .. tostring(j.error))
end)

test_setup("arena.setup: the names are the display names after joining the party; one that differs fails setup",
    function()
    local got = {}
    for i, entry in ipairs({ { name = "bea!", uuid = B2 }, { name = "Nightwarden Bea", uuid = B2 },
        { name = "Bea", uuid = B3 } }) do
        game()
        NAMES[B2] = "Nightwarden Bea" -- joining renames her (live: Nightwarden Minthara -> Minthara)
        local add = Osi.PROC_GLO_PartyMembers_Add
        stub("PROC_GLO_PartyMembers_Add", function(u, host)
            add(u, host)
            if u == B2 then NAMES[B2] = "Bea" end
        end)
        local a = arena()
        a.teams.B[2] = entry
        if i == 3 then a.teams.B[3] = { name = "Bea2", uuid = B2 } end
        local r, j = setup(a)
        if j then run(j) end
        got[i] = { r = r, status = j and j.status, error = j and j.error, tp = #named("TeleportToPosition") }
    end
    NAMES[B2] = "Bea"
    eq(got[1].status, "done", "the post-join name: " .. enc(got[1]))
    eq(got[2].status, "failed", "the pre-join name")
    eq(got[2].error, "team entries whose display name after joining the party differs: Nightwarden Bea (" .. B2
        .. ") is Bea", "both names")
    eq(got[2].tp, 0, "nobody placed")
    eq(got[3].error, "team entries whose display name after joining the party differs: Bea (" .. B3 .. ") is Bix; "
        .. "Bea2 (" .. B2 .. ") is Bea", "another character's uuid")
end)

test_setup("arena.setup: IsEnemy wrong at first, right within 3 s: done; wrong for 3 s: failed, the matrix", function()
    game({ enemy_after = 2 })
    local _, j = setup(arena())
    run(j)
    eq(j.status, "done", "right on the 3rd read: " .. tostring(j.error))
    eq(j.result.enemy.reads, 3, "3 reads")
    game({ enemy_after = 100 })
    _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "still wrong")
    assert(j.error:find("across the teams 0/32, within a team 0/24", 1, true), j.error)
    assert(j.error:find("Ana not an enemy of Bo", 1, true), j.error)
    eq(Ar.state, nil, "no match begun")
end)

test_setup("arena.setup: not all 8 in one combat: EnterCombat pairs, then done; still not: failed", function()
    game({ combat = { A1, A2, B1, B2 }, fallback_joins = true })
    local _, j = setup(arena())
    run(j)
    eq(j.status, "done", "after the fallback: " .. tostring(j.error))
    eq(#named("EnterCombat"), 4, "one pair per slot")
    game({ combat = { A1, B1 } })
    _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "no combat for all")
    assert(j.error:find("not in one combat", 1, true), j.error)
end)

test_setup("arena.setup: the avatar sits out: one in the fight fails setup; one named in a team is refused", function()
    game({ avatar_joins = true })
    local _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "the avatar joined")
    eq(j.error, "not on a team and in the fight: Tav", "the avatar named")
    game()
    local a = arena()
    a.teams.A[4] = { name = "Tav", uuid = AV }
    local r = setup(a)
    assert(enc(r):find("Tav is the avatar", 1, true), enc(r))
end)

test_setup("arena.setup refusals: no scenario switch, a name twice or unknown, a side, the arena switch off", function()
    game()
    env.BG3AI_SCENARIO_SETUP = nil
    assert(enc(setup(arena())):find("BG3AI_SCENARIO_SETUP=1", 1, true), "scenario switch")
    game()
    NAMES[A3] = "Ana" -- two origins named Ana
    local ok, r = pcall(setup, arena())
    NAMES[A3] = "Ari"
    assert(ok and enc(r):find("Ana names 2 characters", 1, true), enc(r))
    game()
    local a = arena()
    a.teams.A[4] = { name = "Nobody" }
    assert(enc(setup(a)):find("no party member, player or origin named Nobody", 1, true), "unknown")
    a = arena()
    a.teams.B[2] = { name = "Ana" }
    assert(enc(setup(a)):find("Ana is listed twice", 1, true), "twice")
    game()
    a = arena()
    a.teams.B[4] = nil
    assert(enc(setup(a)):find("team A lists 4 characters, team B 3: the teams must be the same size", 1, true),
        "sizes differ")
    a = arena()
    a.teams.A = {}
    assert(enc(setup(a)):find("team A must list 1 to 4 characters", 1, true), "empty")
    a = arena()
    a.teams.B[5] = { name = "Tav" }
    assert(enc(setup(a)):find("team B must list 1 to 4 characters", 1, true), "5")
    assert(enc(setup(arena(), "A")):find("arena ops are the harness's", 1, true), "a side")
    env.BG3AI_ARENA = nil
    assert(enc(setup(arena())):find("BG3AI_ARENA=1", 1, true), "switch off")
    eq(#calls, 0, "no game call")
end)

-- Live 15: the file's spawns 0.18 m apart in height, the twins landed 0.73 m and 0.35 m apart (the game stands a
-- teleported character on the ground).
test_setup("arena.setup fails where a pair landed 0.3 m or more apart in height, or off the cross distances",
    function()
        game({ land = { [B4] = { 0, 0.35, 0 } } })
        local _, j = setup(arena())
        run(j)
        eq(j.status, "failed", "failed")
        eq(j.error, "pair 4 (Axe, Bree) landed 0.35 m apart in height (y 0.00, 0.35); a pair must stand under 0.3 m "
            .. "apart", "the pair and both heights")
        eq(#named("RestoreParty") + #named("SetFaction"), 0, "stopped right after placing")
        eq(Ar.active(), false, "no match")
        game({ land = { [B4] = { 0.5, 0.2, 0 } } })
        _, j = setup(arena())
        run(j)
        local want = "where the members landed: A's member 1 is 11.63 m from B's member 4"
        eq(tostring(j.error):find(want, 1, true) ~= nil, true, tostring(j.error))
        game({ land = { [B4] = { 0, 0.29, 0 } } })
        _, j = setup(arena())
        run(j)
        eq(j.status, "done", "0.29 m: " .. tostring(j.error))
        game()
        _, j = setup(arena())
        local tp = Osi.TeleportToPosition
        rawset(Osi, "TeleportToPosition", function(u, ...)
            tp(u, ...)
            if u == A2 then world[A2] = nil end -- gone: no position
        end)
        run(j)
        rawset(Osi, "TeleportToPosition", tp)
        eq(tostring(j.error):find("its position doesn't read after placing", 1, true) ~= nil, true, tostring(j.error))
    end)

-- Both sides on the same terms (design review F2): one team higher, or the lines offset along x (pair i's A member
-- then stands nearer B's later pairs than B's member does to A's), is refused before any game call.
test_setup("arena.setup refuses spawns 0.3 m or more apart in height, or offset along the lines; 0.29 m is fine",
    function()
        game()
        local a = arena({ location = { spawns = { A = { 229.8, 5.1, 268.2 }, B = { 229.8, 5.6, 278.7 } } } })
        assert(enc(setup(a)):find("location.spawns A and B are 0.50 m apart in height (y); they must be under 0.3 m",
            1, true), enc(setup(a)))
        a = arena({ location = { spawns = { A = { 0, 0, 0 }, B = { 0.5, 0, 10.5 } } } })
        local r = enc(setup(a))
        assert(r:find("A's member 1 is 10.69 m from B's member 2, B's member 1 10.55 m from A's member 2", 1, true), r)
        eq(#calls, 0, "no game call")
        a = arena({ location = { spawns = { A = { 0, 5.1, 0 }, B = { 0, 5.39, 10.5 } } } })
        local _, j = setup(a)
        eq(j and j.status, "running", "a job")
        j.status = "interrupted"
    end)

-- A site can be too narrow for a line of 4 at the default gap (S1's P5: exact landing overlaps 4.0 m, a 1.5 m line
-- of 4 needs 4.5 m), so an arena may name its own; its limits stay the same.
test_setup("arena.setup: location.line_gap stands the members that far apart; the fairness limits don't move",
    function()
        game()
        local spawns = { A = { 0, 0, 0 }, B = { 0, 0, 10.5 } }
        local _, j = setup(arena({ location = { spawns = spawns, line_gap = 1.25 } }))
        eq(j and j.status, "running", "a job")
        for _ = 1, 100 do
            if #named("TeleportToPosition") >= 8 or j.status ~= "running" then break end
            tick()
        end
        local xs = {}
        for i, c in ipairs(named("TeleportToPosition")) do xs[i] = c[3] end
        eq(enc(xs), enc({ -1.875, -0.625, 0.625, 1.875, -1.875, -0.625, 0.625, 1.875 }), "both lines at 1.25 m")
        j.status = "interrupted"
        calls = {}
        local off = { A = { 0, 0, 0 }, B = { 0.5, 0, 10.5 } }
        local r = enc(setup(arena({ location = { spawns = off, line_gap = 1.25 } })))
        assert(r:find("must be the same within 0.1 m", 1, true), r)
        eq(#calls, 0, "no game call")
        for _, bad in ipairs({ 0, -1, "1.25" }) do
            r = enc(setup(arena({ location = { spawns = spawns, line_gap = bad } })))
            assert(r:find("location.line_gap must be a number above 0", 1, true), r)
        end
    end)

test_setup("arena.setup: a member not at the arena's level after joining fails setup with every level; none begun",
    function()
    game()
    local add = Osi.PROC_GLO_PartyMembers_Add
    stub("PROC_GLO_PartyMembers_Add", function(u, host)
        add(u, host)
        if u == B3 or u == B4 then osiv[key("GetLevel", u)] = 1 end -- joining reset their level (live: Karlach 3 -> 1)
    end)
    local _, j = setup(arena())
    run(j)
    eq(j.status, "failed", "level off")
    eq(j.error, "levels after setup differ from the arena's level 4: Bix 1, Bree 1", "error")
    eq(enc(j.result.members[B3].level), enc({ 4, 1 }), "Bix before/after kept in the result")
    local stood = 0
    for _ in pairs(j.result.positions) do stood = stood + 1 end
    eq(stood, 8, "a failed setup says where its members stood")
    eq(Ar.state, nil, "no match begun")
    game()
    for _, bad in ipairs({ "nil", "0", "2.5", '"4"', "13" }) do
        local a = arena()
        a.level = load("return " .. bad)()
        assert(enc(setup(a)):find("arena.level must be the members' character level", 1, true), "level " .. bad)
    end
end)

-- Seat fix (D643): the arena file's turn_order, "alternate" (default) or "snake"; anything else fails before a job.
test_setup("arena.setup: turn_order other than alternate / snake is refused before any job", function()
    game()
    for _, bad in ipairs({ '"team"', "1", '"Snake"' }) do
        local a = arena()
        a.turn_order = load("return " .. bad)()
        local r = setup(a)
        assert(enc(r):find('turn_order must be \\"alternate\\" or \\"snake\\"', 1, true), bad .. ": " .. enc(r))
    end
    local r = setup(arena({ turn_order = "snake" }))
    eq(r.result and r.result.status, "running", "snake: a job: " .. enc(r))
end)
