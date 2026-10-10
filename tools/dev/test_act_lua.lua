-- Offline tests for Server/Guard.lua + Server/Actions.lua (worker act): verified
-- approach points for entity targets, the position-only walker, teleport
-- detection, interact/pickup approach, cast range/arity, combat movement budget.
-- Ext/Osi/Nav are stubbed; Shared/Util.lua and Shared/RPC.lua are the real ones.
-- Run: tools/dev/test_act_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

local clock = 1000
local tickers = {}
local world = {}   -- uuid -> {pos={x,y,z}, comps={}}
local calls = {}   -- list of {name, args...}
local osiv = {}    -- "Fn:a:b" -> value (queries)
local nav = {}     -- nav.fn(char, pos) -> path result table
local stats = {}   -- name -> stats table

local function entity(u)
    local w = world[u]
    if not w then error("no entity " .. tostring(u)) end
    local e = {}
    for k, v in pairs(w.comps or {}) do e[k] = v end
    function e:Replicate() end
    return e
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end,
        Version = function() return 0 end },
    Events = { Tick = { Subscribe = function(_, fn) tickers[#tickers + 1] = fn end } },
    Entity = { Get = entity },
    Level = { GetHeightsAt = function() return { 0.0 } end },
    Stats = { Get = function(n) return stats[n] end },
    StaticData = { Get = function(uuid) return { Name = uuid } end },
    Loca = { GetTranslatedString = function() return nil end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    Osiris = { RegisterListener = function() end },
}

local function key(name, ...)
    local parts = { name }
    for _, a in ipairs({ ... }) do parts[#parts + 1] = tostring(a) end
    return table.concat(parts, ":")
end

local function record(name)
    return function(...)
        local c = { name, n = select("#", ...), ... }
        calls[#calls + 1] = c
    end
end

local P1 = "aaaaaaaa-0000-0000-0000-000000000001"
local NPC = "bbbbbbbb-0000-0000-0000-000000000002"
local DOOR = "cccccccc-0000-0000-0000-000000000003"
local GEM = "dddddddd-0000-0000-0000-000000000004"
local GOB = "eeeeeeee-0000-0000-0000-000000000005"
local P2 = "ffffffff-0000-0000-0000-000000000006"
local KEY = "abababab-0000-0000-0000-000000000007"
local LADDER = "acacacac-0000-0000-0000-000000000008"
local party_rows = { P1 }

local usp_arity = 6 -- UseSpellAtPosition inputs the "game" accepts
Osi = setmetatable({
    DB_PartyMembers = { Get = function()
        local rows = {}
        for _, u in ipairs(party_rows) do rows[#rows + 1] = { "S_Player_" .. u } end
        return rows
    end },
    Unlock = record("Unlock"),
    GetPosition = function(u)
        local w = world[u]
        if w then return w.pos[1], w.pos[2], w.pos[3] end
        error("no position")
    end,
    CharacterMoveTo = record("CharacterMoveTo"),
    CharacterMoveToPosition = record("CharacterMoveToPosition"),
    PurgeOsirisQueue = record("PurgeOsirisQueue"),
    Use = record("Use"),
    Pickup = record("Pickup"),
    UseSpell = record("UseSpell"),
    MakePlayerActive = record("MakePlayerActive"),
    EndTurn = record("EndTurn"),
    ToInventory = record("ToInventory"),
    ForceTurnBasedMode = record("ForceTurnBasedMode"),
    UseSpellAtPosition = function(...)
        calls[#calls + 1] = { "UseSpellAtPosition?", n = select("#", ...) }
        if select("#", ...) ~= usp_arity then
            error("Incorrect number of IN arguments for 'UseSpellAtPosition'; expected 6, got 5", 0)
        end
        record("UseSpellAtPosition")(...)
    end,
}, { __index = function(_, name)
    return function(...) return osiv[key(name, ...)] end
end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
BG3AI.Nav = { path = function(char, pos) return nav.fn(char, pos) end }
dofile(LUA .. "/Server/Guard.lua")
dofile(LUA .. "/Server/Actions.lua")
local G, A, RPC = BG3AI.Guard, BG3AI.Actions, BG3AI.RPC

local function tick(n)
    for _ = 1, n or 1 do
        clock = clock + 60
        for _, f in ipairs(tickers) do f() end
    end
end
local function job(r) return RPC.jobs[r.job] end
local function called(name)
    local out = {}
    for _, c in ipairs(calls) do if c[1] == name then out[#out + 1] = c end end
    return out
end

local function reset()
    calls, osiv, stats = {}, {}, {}
    Ext.System = nil
    party_rows = { P1 }
    world = {
        [P1] = { pos = { 0, 0, 0 }, comps = {} },
        [NPC] = { pos = { 10, 0, 0 }, comps = {} },
        [DOOR] = { pos = { 10, 0, 0 }, comps = { ServerItem = { IsDoor = true } } },
        [LADDER] = { pos = { 10, 0, 0 }, comps = { ServerItem = { IsLadder = true } } },
        [GEM] = { pos = { 10, 0, 0 }, comps = {} },
        [GOB] = { pos = { 2.4, 0, 0 }, comps = {} },
    }
    for _, u in ipairs({ P1, NPC, GOB }) do osiv[key("IsCharacter", u)] = 1 end
    for _, u in ipairs({ DOOR, GEM, LADDER }) do osiv[key("IsItem", u)] = 1 end
    osiv[key("IsOnStage", P1)] = 1
    for _, u in ipairs({ NPC, DOOR, GEM, GOB, LADDER }) do
        osiv[key("IsOnStage", u)] = 1
        osiv[key("CanSee", P1, u)] = 1
        osiv[key("HasLineOfSight", P1, u)] = 1
    end
    nav.fn = function(_, pos) return { reachable = true, length_m = U.dist(world[P1].pos, pos) } end
    for _, j in pairs(RPC.jobs) do j.status = "done" end
    A.active = {}
end
U = BG3AI.Util

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end
local function near(a, b, what) if math.abs(a - b) > 0.01 then error((what or "value") .. ": expected " .. b .. ", got " .. a, 2) end end
local function refused(fn, pat)
    local ok, err = pcall(fn)
    if ok then error("expected a refusal matching '" .. pat .. "'", 2) end
    local msg = G.is_refusal(err) and err.reason or tostring(err)
    if not msg:find(pat, 1, true) then error("refusal '" .. msg .. "' does not match '" .. pat .. "'", 2) end
end
local function op(name, args) return RPC.ops["act." .. name](args) end

-- Walk the character to `to` in small steps and fire the walk's EntityEvent.
local function arrive(j, to)
    local from = world[P1].pos
    for i = 1, 5 do
        world[P1].pos = { from[1] + (to[1] - from[1]) * i / 5, to[2], from[3] + (to[3] - from[3]) * i / 5 }
        tick()
    end
    A.dispatch("EntityEvent", { P1, j.walk.ev })
end

-- ---- G.approach_point --------------------------------------------------------

test("approach: already within reach -> here", function()
    world[NPC].pos = { 2, 0, 0 }
    local p = G.approach_point(P1, NPC, 2.5)
    eq(p.here, true, "here")
end)

test("approach: ring point on the actor's side, shortest walk first", function()
    local p = G.approach_point(P1, NPC, 2.5) -- radii 1.0/1.5/2.2 fit in reach 2.5
    near(p.pos[1], 7.8, "x"); near(p.pos[3], 0, "z")
    near(p.length_m, 7.8, "length")
end)

test("approach: skips unreachable candidates", function()
    nav.fn = function(_, pos)
        if pos[3] < -0.5 then return { reachable = true, length_m = 12 } end
        return { reachable = false }
    end
    local p = G.approach_point(P1, NPC, 2.5)
    assert(p.pos[3] < -0.5, "picked an unreachable point")
    near(p.length_m, 12, "length")
end)

test("pickpocket: an NPC behind a counter is reached from the 2.2 m ring (setup 14:xx, Last Light Inn)", function()
    local npc = world[NPC].pos
    nav.fn = function(_, pos)  -- the counter: nothing walkable within 2 m of the NPC
        if U.dist(pos, npc) >= 2.0 then return { reachable = true, length_m = U.dist(world[P1].pos, pos) } end
        return { reachable = false }
    end
    local j = job(op("pickpocket", { char = P1, npc = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.phase, "approach", "phase")
end)

test("approach: nothing reachable -> refused", function()
    nav.fn = function() return { reachable = false } end
    refused(function() G.approach_point(P1, NPC, 2.5) end, "no walkable path")
end)

test("approach / point move: no path -> names the closest walkable spot (S021)", function()
    nav.fn = function() return { reachable = false } end
    BG3AI.Nav.closestReach = function(_, goal)
        return { pos = { goal[1] - 3, goal[2] - 4.6, goal[3] }, path_m = 21, to_goal_m = 5.2, dy_to_goal = 4.6 }
    end
    refused(function() G.approach_point(P1, NPC, 2.5) end, "closest walkable spot (")
    refused(function() G.point_reachable(P1, { 5, 0, 0 }) end, "4.6m above that spot")
    BG3AI.Nav.closestReach = nil
    refused(function() G.point_reachable(P1, { 5, 0, 0 }) end, "may need a jump or another route)")
end)

test("approach: only with a jump -> refused, says jump", function()
    nav.fn = function() return { reachable = true, length_m = 5, needs_jump = true } end
    refused(function() G.approach_point(P1, NPC, 2.5) end, "needs a jump")
end)

test("approach: pathfinding unknown, close and in sight -> unverified", function()
    nav.fn = function() return { reachable = nil } end
    world[NPC].pos = { 6, 0, 0 }
    local p = G.approach_point(P1, NPC, 2.5)
    eq(p.unverified, true, "unverified")
end)

test("approach: pathfinding unknown and far -> refused", function()
    nav.fn = function() return { reachable = nil } end
    world[NPC].pos = { 20, 0, 0 }
    refused(function() G.approach_point(P1, NPC, 2.5) end, "no walkable path")
end)

test("approach: a ladder seen from above is approached at its top (S025 08:03)", function()
    world[P1].pos = { 10, 15, 0 }
    world[NPC].pos = { 0, 0, 0 } -- the ladder's foot
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 15.0 } end
    BG3AI.Nav.ladderTop = function(from, u) if u == NPC and from[2] > 2.5 then return { 0, 15, 0 } end end
    nav.fn = function(_, pos) return { reachable = pos[2] > 10, length_m = 9 } end
    local ok, err = pcall(function()
        local p = G.approach_point(P1, NPC, 1.8)
        near(p.pos[2], 15, "on the top floor")
        near(p.tpos[2], 0, "tpos stays the foot (the walker's did-not-move check)")
        world[P1].pos = { 1, 15, 0 }
        eq(G.approach_point(P1, NPC, 1.8).here, true, "at the top: here")
        world[P1].pos = { 10, 0, 0 } -- from below: the foot, as before
        refused(function() G.approach_point(P1, NPC, 1.8) end, "no walkable path")
    end)
    Ext.Level.GetHeightsAt, BG3AI.Nav.ladderTop = heights, nil
    if not ok then error(err, 0) end
end)

-- ---- move_to -------------------------------------------------------------

test("move_to entity: walks to a verified point, never CharacterMoveTo", function()
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "status")
    eq(#called("CharacterMoveTo"), 0, "CharacterMoveTo calls")
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "move calls"); near(mv[1][3], 8.5, "dest x")
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    near(j.result.dist, 1.5, "dist to target"); eq(j.result.target, NPC, "target")
end)

test("move_to entity: unreachable -> failed job, no movement", function()
    nav.fn = function() return { reachable = false } end
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("no walkable path"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "moves")
end)

test("move_to entity: already there -> done without walking", function()
    world[NPC].pos = { 1.5, 0, 0 }
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "done", "status"); eq(j.result.already_there, true, "already_there")
    eq(#called("CharacterMoveToPosition"), 0, "moves")
end)

test("move_to entity within: stops inside the edge-to-edge melee reach (S025 Spiritual Weapon 2.4 m)", function()
    world[NPC].pos = { 2.0, 0, 0 }
    world[NPC].comps.Bound = { Bound = { AIBounds = { Stand = { Radius = 0.25 } } } }
    -- 2.0 m is 'already there' for a plain move (2.2 m centres) but outside 1.5 + 0.5 + 0.25 - 0.4 = 1.85 m.
    local j = job(op("move_to", { char = P1, target = NPC, within = 1.5 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "move calls"); near(mv[1][3], 0.5, "dest x (ring 1.5 m on the actor's side)")
    arrive(j, { 0.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.in_reach, true, "in_reach")
    world[NPC].comps.Bound = nil
end)

test("move_to entity within: already in reach -> done, in_reach", function()
    world[NPC].pos = { 1.8, 0, 0 }
    local j = job(op("move_to", { char = P1, target = NPC, within = 1.5 }))
    eq(j.status, "done", "status"); eq(j.result.already_there, true, "already_there")
    eq(j.result.in_reach, true, "in_reach")
end)

test("move_to: teleport is detected and purged", function()
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 30, 0, 0 }
    tick()
    eq(j.status, "failed", "status"); assert(j.error:find("teleported"), j.error)
    assert(#called("PurgeOsirisQueue") >= 1, "purged")
end)

-- S001 leg 5: a 7 m single-tick step along the engine's own route (a jump link
-- or a frame hitch) is the walk, not a teleport. Nav.route/onRoute are stubbed
-- here; their geometry is tested in tools/dev/test_nav_lua.lua.
local function with_route(on)
    BG3AI.Nav.route = function() return { { 0, 0, 0 }, { 30, 0, 0 } } end
    BG3AI.Nav.onRoute = function() return on end
end
local function without_route() BG3AI.Nav.route, BG3AI.Nav.onRoute = nil, nil end

test("move_to: a big step along the walk's own route is not a teleport", function()
    with_route(true)
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 7, 0, 0 }
    tick()
    without_route()
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "purged")
end)

test("move_to: a big step off the route is still a teleport", function()
    with_route(false)
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 7, 0, 9 }
    tick()
    without_route()
    eq(j.status, "failed", "status"); assert(j.error:find("teleported"), j.error)
end)

-- ---- combat movement -------------------------------------------------------

local function combat_setup(mv)
    osiv[key("IsInCombat", P1)] = 1
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true }
    world[P1].comps.ActionResources = { Resources = { Movement = { { Level = 0, Amount = mv } },
        ActionPoint = { { Level = 0, Amount = 1 } } } }
end

test("combat move_to entity: path beyond movement -> refused", function()
    combat_setup(6)
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("beyond remaining movement"), j.error)
end)

test("combat move_to entity: within movement, guard charges what the engine did not", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    near(j.result.movement_charged_by_guard, 8.5, "charged")
    near(j.result.after.movement, 3.5, "movement left")
end)

test("combat loot approach: budget enforced (click ops walk in combat too)", function()
    combat_setup(4)
    osiv[key("IsDead", NPC)] = 1
    local j = job(op("loot_corpse", { char = P1, target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("beyond remaining movement"), j.error)
end)

-- ---- interact / pickup -----------------------------------------------------

test("interact: approach first, Use only after arriving", function()
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 0, "Use before arrival")
    eq(j.phase, "approach", "phase")
    local mv = called("CharacterMoveToPosition")
    near(mv[1][3], 8.5, "dest x") -- reach 1.8: radii 1.0/1.5
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Use"), 1, "Use after arrival")
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
end)

test("interact: a ladder-like jump next to the item is not a teleport", function()
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use right away")
    tick()
    world[P1].pos = { 9, 8, 0 }
    tick()
    eq(j.status, "running", "status " .. tostring(j.error))
end)

test("interact: a ladder used from its top landing (item position 3.2 m below) uses, no re-plan loop", function()
    world[GEM].pos = { 10, -3, 0 }
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.phase, "approach", "phase")
    j.approach.plan.pos = { 10, 0.2, 0 } -- the verified landing next to the ladder's top
    arrive(j, { 10, 0.2, 0 })
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(j.phase, "act", "phase"); eq(#called("Use"), 1, "Use"); eq(j.approach.replans, 0, "replans")
end)

test("interact: Use's own walk teleporting from afar is caught", function()
    nav.fn = function() return { reachable = nil } end -- unverified short approach
    world[DOOR].pos = { 6, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    -- the walk ends 2.7 m short (within reach + slack): Use must walk the rest
    world[P1].pos = { 3.3, 0, 0 }; tick()
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    eq(#called("Use"), 1, "Use")
    tick()
    world[P1].pos = { 3.3, 0, 9 }; tick()
    eq(j.status, "failed", "status"); assert(j.error:find("teleported"), tostring(j.error))
end)

test("interact: an item in the actor's own bag is used in place", function()
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("CanSee", P1, GEM)] = nil -- carried items are not "visible" in the world
    world[GEM].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("Use"), 1, "Use"); eq(#called("CharacterMoveToPosition"), 0, "moves")
end)

test("interact: an item in another character's bag is refused", function()
    osiv[key("GetInventoryOwner", GEM)] = NPC
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.status, "failed", "status"); assert(j.error:find("another character"), j.error)
end)

test("pickup: approach first, then Pickup without force", function()
    local j = job(op("pickup", { char = P1, item = GEM }))
    eq(#called("Pickup"), 0, "Pickup before arrival")
    arrive(j, { 9, 0, 0 })
    local pk = called("Pickup")
    eq(#pk, 1, "Pickup after arrival"); eq(pk[1][5], 0, "ForcePickUpOnFailure")
    osiv[key("GetInventoryOwner", GEM)] = P1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
end)

test("talk: approach point within talk range, then DialogStartRequested", function()
    local j = job(op("talk", { char = P1, npc = NPC }))
    eq(#called("CharacterMoveTo"), 0, "CharacterMoveTo")
    arrive(j, { 9, 0, 0 })
    eq(j.phase, "requested", "phase")
end)

-- ---- cast ---------------------------------------------------------------------

local function spell(name, st)
    stats[name] = st
    world[P1].comps.SpellBook = world[P1].comps.SpellBook or { Spells = {} }
    table.insert(world[P1].comps.SpellBook.Spells, { Id = { Prototype = name } })
end

test("cast at position: engine's 6-argument UseSpellAtPosition first (no story-form try)", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local c = called("UseSpellAtPosition")
    eq(#c, 1, "calls"); eq(c[1].n, 6, "arity"); eq(c[1][7], 1, "WithoutMove")
    eq(#called("UseSpellAtPosition?"), 1, "attempts")
end)

test("cast at position: falls back to 5 arguments on a build without the 6th", function()
    usp_arity = 5
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    usp_arity = 6
    eq(called("UseSpellAtPosition")[1].n, 5, "arity")
end)

test("cast: a ranged cast out of range is refused out of combat too (no walk, no Osiris approach walk)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    world[NPC].pos = { 30, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("out of range"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no walk"); eq(#called("UseSpell"), 0, "no engine cast")
end)

test("cast: in range uses UseSpell/5 with WithoutMove=1", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    local c = called("UseSpell")
    eq(#c, 1, "calls"); eq(c[1].n, 5, "arity"); eq(c[1][6], 1, "WithoutMove")
end)

test("cast: a projectile's late hit is in the result, not no_damage (acceptance 14:5x Ranged Attack at 18 m)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful" } })
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(8) -- 480 ms: the bolt is still in flight
    eq(j.status, "running", "still waiting for the bolt")
    A.dispatch("AttackedBy", { NPC, P1, P1, "Piercing", 2, "", 0 })
    tick(5)
    eq(j.status, "done", "status")
    eq(j.result.total_damage, 2, "damage"); eq(j.result.no_damage, nil, "no_damage")
    j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(30) -- 1.8 s, no hit: a miss
    eq(j.status, "done", "miss status"); eq(j.result.no_damage, true, "miss")
end)

test("cast: an order the engine silently drops fails after CAST_NO_START_MS (S035 jump_to 25 s x3)", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    tick(100) -- 6 s: still waiting
    eq(j.status, "running", "early")
    tick(40) -- 8.4 s, no CastSpell, no step
    eq(j.status, "failed", "status"); assert(j.error:find("never started"), j.error)
    -- the cast began (CastSpell): waits on
    j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Target_Thing", "Target", "None", 0 })
    tick(140)
    eq(j.status, "running", "began")
    -- the character stepped (an engine approach / the jump took off): waits on
    j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    world[P1].pos = { 1, 0, 0 }; tick(140)
    eq(j.status, "running", "moved")
    world[P1].pos = { 0, 0, 0 }
end)

test("cast: a once-per-rest item spell is refused after one use until a rest (S030 Spell Slot Restoration x3)", function()
    PersistentVars = nil
    spell("Shout_MAG_Restore", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        Cooldown = "OncePerRestPerItem", ContainerSpells = "Shout_MAG_Restore_1;Shout_MAG_Restore_2" })
    spell("Shout_MAG_Restore_1", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        Cooldown = "OncePerRestPerItem", SpellContainerID = "Shout_MAG_Restore" })
    spell("Shout_MAG_Restore_2", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        Cooldown = "OncePerRestPerItem", SpellContainerID = "Shout_MAG_Restore" })
    local j = job(op("cast", { char = P1, spell = "Shout_MAG_Restore_2" }))
    eq(j.status, "running", "first use " .. tostring(j.error))
    A.dispatch("CastedSpell", { P1, "Shout_MAG_Restore_2" })
    tick(10)
    j = job(op("cast", { char = P1, spell = "Shout_MAG_Restore_1" }))
    eq(j.status, "failed", "second use (another variant)"); assert(j.error:find("used up until the next long rest"), j.error)
    BG3AI.Guard.item_rest("short", P1)
    eq(job(op("cast", { char = P1, spell = "Shout_MAG_Restore_1" })).status, "failed", "a short rest keeps it used")
    BG3AI.Guard.item_rest("long")
    eq(job(op("cast", { char = P1, spell = "Shout_MAG_Restore_1" })).status, "running", "after a long rest")
    PersistentVars = nil
end)

test("cast: a melee hit still reports at 250 ms", function()
    spell("Target_Hit", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful" } })
    world[NPC].pos = { 1.5, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Hit", target = NPC }))
    A.dispatch("AttackedBy", { NPC, P1, P1, "Slashing", 5, "", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Hit" })
    tick(5)
    eq(j.status, "done", "status"); eq(j.result.total_damage, 5, "damage")
    j = job(op("cast", { char = P1, spell = "Target_Hit", target = NPC }))
    A.dispatch("CastedSpell", { P1, "Target_Hit" })
    tick(5)
    eq(j.status, "done", "miss status"); eq(j.result.no_damage, true, "miss")
end)

test("cast: Extra Attack (EXTRA_ATTACK status) makes the next weapon attack cost no action (S023-S028 0 of 7)", function()
    combat_setup(9)
    world[P1].comps.ActionResources.Resources.ActionPoint = { { Level = 0, Amount = 0 } } -- the first attack spent it
    world[GOB].pos = { 1.2, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" }, SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Target_Smite_Divine", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful" }, SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Target_Slash_New", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsDefaultWeaponAction" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Projectile_FireBolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful", "IsSpell" }, SpellRoll = { Default = "Attack(AttackType.RangedSpellAttack)" } })
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "no status: refused"); assert(j.error:find("not enough action"), j.error)
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    for _, sp in ipairs({ "Target_MainHandAttack", "Target_Smite_Divine" }) do
        j = job(op("cast", { char = P1, spell = sp, target = GOB }))
        eq(j.status, "running", sp .. ": " .. tostring(j.error))
        A.dispatch("CastedSpell", { P1, sp })
        tick(10)
        eq(j.status, "done", sp .. " done"); eq(j.result.extra_attack, true, sp .. " extra_attack")
        eq(j.result.charged_by_guard, nil, sp .. ": no action charged")
    end
    for _, sp in ipairs({ "Target_Slash_New", "Projectile_FireBolt" }) do -- a weapon action, a spell attack
        j = job(op("cast", { char = P1, spell = sp, target = GOB }))
        eq(j.status, "failed", sp); assert(j.error:find("not enough action"), j.error)
    end
end)

test("cast out of combat, out of range: walks into range on our walker, then casts (S028 Help at 5 m)", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.phase, "approach", "phase")
    eq(#called("UseSpell"), 0, "no cast before arriving")
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "one walk")
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    local us = called("UseSpell")
    eq(#us, 1, "cast after arriving"); eq(us[1][6], 1, "WithoutMove")
    A.dispatch("CastedSpell", { P1, "Target_Help" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error)); assert(j.result.walked_into_range, "walked_into_range")
end)

test("cast out of range in combat (or a point target): refused as before, no walk", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    combat_setup(9)
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("move closer first"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no walk")
end)

test("cast out of combat: still out of reach after the walk -> fails with the guard's reason", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    local mv = called("CharacterMoveToPosition")
    world[NPC].pos = { 30, 0, 0 } -- it walked off: the approach re-plans or fails; never a cast from afar
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    tick(3)
    eq(#called("UseSpell"), 0, "no cast out of reach")
end)

test("transfer: GetStackAmount's extra return values don't break tonumber (S018-S024 give/camp crash)", function()
    party_rows = { P1, NPC }
    osiv[key("GetInventoryOwner", GEM)] = P1
    local real = rawget(Osi, "GetStackAmount")
    rawset(Osi, "GetStackAmount", function() return 3, 3 end)
    local ok, r = pcall(op, "transfer", { item = GEM, to = NPC })
    rawset(Osi, "GetStackAmount", real)
    assert(ok, tostring(r))
    eq(job(r).status, "running", "status " .. tostring(job(r).error))
    eq(#called("ToInventory"), 1, "ToInventory calls")
end)

test("transfer: a stack that arrives as new items finishes the give (S031 Potion of Healing e64 timeout)", function()
    party_rows = { P1, NPC }
    local NEW = "15151515-0000-0000-0000-00000000000b"
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("GetTemplate", GEM)] = "POT_Healing_tpl"
    osiv[key("GetTemplate", NEW)] = "POT_Healing_tpl"
    local j = job(op("transfer", { item = GEM, to = NPC }))
    tick()
    eq(j.status, "running", "still with the giver")
    A.dispatch("AddedTo", { "16161616-0000-0000-0000-00000000000c", NPC, "" }) -- another kind: not ours
    eq(j.status, "running", "other template ignored")
    A.dispatch("AddedTo", { NEW, NPC, "" })
    eq(j.status, "done", "status " .. tostring(j.error))
end)

test("transfer: the whole stack merged away (original gone from the giver) is done on the tick", function()
    party_rows = { P1, NPC }
    osiv[key("GetInventoryOwner", GEM)] = P1
    local j = job(op("transfer", { item = GEM, to = NPC }))
    tick()
    eq(j.status, "running", "still held")
    osiv[key("GetInventoryOwner", GEM)] = nil -- merged into the receiver's stack and destroyed
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
end)

test("equip: a failed equip says why (S025: two-handed Sword of Justice; the Moonlantern does not block a shield)", function()
    local LANTERN, SHIELD = "12121212-0000-0000-0000-000000000008", "13131313-0000-0000-0000-000000000009"
    local SWORD = "14141414-0000-0000-0000-00000000000a"
    world[LANTERN] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "Quest_GLO_Moonlantern" } } }
    world[SWORD] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "WPN_Greatsword_Justice" } } }
    world[SHIELD] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "ARM_Shield_Sentinel" } } }
    stats.Quest_GLO_Moonlantern = { ["Weapon Properties"] = { "Torch", "Light", "Melee", "Unstowable", "Versatile" } }
    stats.WPN_Greatsword_Justice = { ["Weapon Properties"] = { "Twohanded", "Heavy", "Melee" } }
    stats.ARM_Shield_Sentinel = { Slot = "Melee Offhand Weapon" }
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = LANTERN
    osiv[key("GetInventoryOwner", SHIELD)] = P1
    osiv[key("IsCharacter", P1)] = 1
    osiv[key("IsProficientWith", P1, SHIELD)] = 1
    eq(A.equip_why(P1, SHIELD), "cannot equip that", "live: a shield goes on beside the Moonlantern")
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = SWORD
    local why = A.equip_why(P1, SHIELD)
    assert(why:find("two%-handed") and why:find("one%-handed weapon first"), why)
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = nil
end)

test("cast: an item next to the caster needs no engine line of sight (S024 Knock on a door at 2 m)", function()
    spell("Target_Knock", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    world[DOOR].pos = { 2.0, 0, 0 }
    osiv[key("HasLineOfSight", P1, DOOR)] = 0
    local j = job(op("cast", { char = P1, spell = "Target_Knock", target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    world[DOOR].pos = { 10, 0, 0 } -- far: the engine's check still decides
    j = job(op("cast", { char = P1, spell = "Target_Knock", target = DOOR }))
    eq(j.status, "failed", "far status"); assert(j.error:find("line of sight"), j.error)
    world[GOB].pos = { 2.0, 0, 0 } -- a character this close still needs it
    osiv[key("HasLineOfSight", P1, GOB)] = 0
    j = job(op("cast", { char = P1, spell = "Target_Knock", target = GOB }))
    eq(j.status, "failed", "char status"); assert(j.error:find("line of sight"), j.error)
end)

test("cast: melee reach is edge to edge (weapon 1.5 m + AIBounds.Stand radii)", function()
    combat_setup(9) -- the range refusal (out of combat the cast walks into range first)
    eq(G.radius(P1), 0.5, "fallback radius")
    world[GOB].comps.Bound = { Bound = { AIBounds = { Move = { Radius = 0.4 }, Hit = { Radius = 1.28 }, Stand = { Radius = 0.7 } } } }
    eq(G.radius(GOB), 0.7, "Stand radius")
    world[GOB].comps.Bound = { Bound = { AIBounds = { Move = { Radius = 0.25 } } } }
    eq(G.radius(GOB), 0.25, "Move fallback")
    world[GOB].comps.Bound = nil
    spell("Target_Slash", { TargetRadius = "MeleeMainWeaponRange", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Slash", target = GOB })) -- 2.4 m
    eq(j.status, "running", "status " .. tostring(j.error))
    world[GOB].pos = { 2.7, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Target_Slash", target = GOB }))
    eq(j.status, "failed", "status"); assert(j.error:find("out of range"), j.error)
end)

test("cast: ranged weapon range from the equipped weapon's stats", function()
    combat_setup(9) -- the range refusal (out of combat the cast walks into range first)
    spell("Projectile_RangedAttack", { TargetRadius = "RangedMainWeaponRange", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    osiv[key("GetEquippedItem", P1, "Ranged Main Weapon")] = "BOW"
    osiv[key("GetStatString", "BOW")] = "WPN_HandCrossbow"
    stats.WPN_HandCrossbow = { WeaponRange = 900 }
    local j = job(op("cast", { char = P1, spell = "Projectile_RangedAttack", target = NPC })) -- 10 m > 9 m
    eq(j.status, "failed", "status"); assert(j.error:find("out of range"), j.error)
end)

test("cast: upcast checks slots at the upcast level", function()
    spell("Target_Heal", { TargetRadius = "18", SpellType = "Target", Level = 1,
        UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:1" })
    stats.Target_Heal_2 = { RootSpellID = "Target_Heal", UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:2" }
    world[P1].comps.ActionResources = { Resources = { SpellSlot = { { Level = 1, Amount = 0 }, { Level = 2, Amount = 1 } } } }
    local j = job(op("cast", { char = P1, spell = "Target_Heal", target = NPC, upcast = 2 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(called("UseSpell")[1][3], "Target_Heal_2", "variant")
end)

test("cast: an upcast with no variant in the game data casts at the base level (S030 Guiding Bolt)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", Level = 1,
        UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:1" })
    world[P1].comps.ActionResources = { Resources = { SpellSlot = { { Level = 1, Amount = 1 }, { Level = 2, Amount = 1 } } } }
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC, upcast = 2 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(called("UseSpell")[1][3], "Projectile_Bolt", "base spell")
end)

test("sneak: UseSpell with the full 5 arguments", function()
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    job(op("sneak", { char = P1, on = true }))
    eq(called("UseSpell")[1].n, 5, "arity")
end)

-- ---- think-while-acting support ---------------------------------------------

test("reply: running job carries an ETA, a refusal its outcome", function()
    local r = op("move_to", { char = P1, pos = { 8, 0, 0 } })
    near(r.eta_s, 2.4, "eta"); near(r.route_m, 8, "route"); eq(r.status, nil, "status")
    nav.fn = function() return { reachable = false } end
    r = op("move_to", { char = P1, pos = { 8, 0, 0 } })
    eq(r.status, "failed", "status"); assert(r.error:find("no walkable"), r.error)
end)

test("talk: NPC walks off mid-approach -> re-planned walk with a new move id", function()
    local j = job(op("talk", { char = P1, npc = NPC }))
    local first = j.walk.move_id
    world[P1].pos = { 3, 0, 0 }; tick()
    world[NPC].pos = { 10, 0, 6 }; tick()
    eq(j.status, "running", "status " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 2, "walks"); assert(mv[2][8] ~= first, "same move id")
    -- a late cancel of the first walk must not fail the new one
    A.dispatch("CharacterMoveToCancelled", { P1, first })
    eq(j.status, "running", "status after stale cancel")
    arrive(j, { mv[2][3], 0, mv[2][5] })
    eq(j.phase, "requested", "phase")
end)

test("talk: arrival out of reach -> re-plan, gone target -> clear reason", function()
    local j = job(op("talk", { char = P1, npc = NPC }))
    for x = 2, 7.8, 1.5 do world[P1].pos = { x, 0, 0 }; tick() end
    world[P1].pos = { 7.8, 0, 0 }; tick()
    world[NPC].pos = { 11.5, 0, 0 }  -- moved 1.5 m: below the mid-walk threshold
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    eq(j.status, "running", "re-planned")
    eq(#called("CharacterMoveToPosition"), 2, "walks")
    osiv[key("CanSee", P1, NPC)] = nil
    world[NPC].pos = { 20, 0, 0 }; tick()
    eq(j.status, "failed", "status"); assert(j.error:find("no longer visible"), j.error)
end)

test("jump: capped at half the Strength score (UNVERIFIED formula), not TargetRadius", function()
    -- live stats: TargetRadius 4.5, below a Str 16 jump
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 8.4, 0, 0 } }))
    eq(j.status, "running", "8.4 m ok " .. tostring(j.error))
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 9, 0, 0 } }))
    eq(j.status, "failed", "status"); assert(j.error:find("too far to jump"), j.error)
    osiv[key("HasActiveStatus", P1, "ENHANCE_LEAP")] = 1
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 20, 0, 0 } }))
    eq(j.status, "running", "enhance leap " .. tostring(j.error))
end)

test("target not visible: names where the party last saw it (S026 09:02)", function()
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        characterVisible = function() return false end,
        itemVisible = function() return false end,
        lastSeen = function(u) if u == NPC then return { pos = { -67.2, 28.2, 141.7 }, ago_s = 669 } end end,
    }
    local ok, err = pcall(function()
        refused(function() G.target_visible(NPC) end, "target is not visible; last seen @(-67.2,28.2,141.7) 669s ago: travel_to")
        BG3AI.Visibility.lastSeen = function() return nil end
        refused(function() G.target_visible(NPC) end, "target is not visible")
        eq(G.last_seen_hint(NPC), "", "never seen: no hint")
    end)
    BG3AI.Visibility = nil
    if not ok then error(err, 0) end
end)

test("target not visible: one that turned invisible says so (S030 Soft-Step Shadow 1.8 m away)", function()
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        characterVisible = function() return false end,
        itemVisible = function() return false end,
        lastSeen = function() return { pos = { -806.7, -6.5, -746.3 }, ago_s = 2 } end,
        vanished = function(u) if u == NPC then return true, { pos = { -806.7, -6.5, -746.3 }, ago_s = 2 } end return false end,
    }
    local ok, err = pcall(function()
        refused(function() G.target_visible(NPC) end, "target is not visible; it turned invisible 2s ago")
        assert(not G.last_seen_hint(NPC):find("travel_to"), "no walk advice for an invisible one")
        BG3AI.Visibility.vanished = function() return false end
        assert(G.last_seen_hint(NPC):find("travel_to that spot first"), "a plain out-of-sight one keeps the old hint")
    end)
    BG3AI.Visibility = nil
    if not ok then error(err, 0) end
end)

test("act.watch: party bars, mode, visible characters with hostility", function()
    world[P1].comps.Health = { Hp = 7, MaxHp = 12 }
    osiv[key("IsEnemy", P1, GOB)] = 1
    osiv[key("IsInCombat", P1)] = 1
    world.FAR = { pos = { 500, 0, 0 } }
    local scans = 0
    BG3AI.Visibility = {
        context = function() return { is_party = { [P1] = true } } end,
        -- the shared per-frame scan; act.watch no longer walks ServerCharacter itself
        charactersNear = function(centers, radius)
            scans = scans + 1
            local out = {}
            for _, u in ipairs({ NPC, GOB, P1, "FAR" }) do
                local p = world[u].pos
                for _, c in ipairs(centers) do
                    local d = math.sqrt((p[1] - c[1]) ^ 2 + (p[2] - c[2]) ^ 2 + (p[3] - c[3]) ^ 2)
                    if d <= radius then out[#out + 1] = { { u = u }, p, d }; break end
                end
            end
            return out
        end,
        entityUuid = function(e) return e.u end,
        name = function(u) return u == GOB and "Goblin" or "Npc" end,
        characterVisible = function(_, u) return u ~= NPC end,
    }
    local w = RPC.ops["act.watch"]({})
    BG3AI.Visibility = nil
    eq(w.combat, true, "combat"); eq(w.party[1].hp, 7, "hp")
    eq(#w.seen, 1, "seen"); eq(w.seen[1].uuid, GOB, "goblin"); eq(w.seen[1].hostile, true, "hostile")
    eq(scans, 1, "one shared radius query")
    world.FAR = nil
end)

test("act.note: only queue_* kinds reach the event stream", function()
    RPC.ops["act.note"]({ kind = "queue_done", data = { n = 2 } })
    eq(RPC.events[#RPC.events].kind, "queue_done", "kind")
    assert(not pcall(RPC.ops["act.note"], { kind = "combat_started" }), "spoofed event accepted")
end)

test("act.target_state: carried item -> held", function()
    osiv[key("GetInventoryOwner", GEM)] = P1
    local r = RPC.ops["act.target_state"]({ uuid = GEM })
    eq(r.held, true, "held")
end)

-- ---- live bug S001 (claude-run-2): melee "range 0.0 m", shared turn groups -----

test("cast: melee keyword wins over the stats' Range 0 (live: Target_MainHandAttack Range=0)", function()
    combat_setup(9) -- the range refusal (out of combat the cast walks into range first)
    spell("Target_MainHandAttack", { TargetRadius = "MeleeMainWeaponRange", Range = 0, SpellType = "Target",
        UseCosts = "ActionPoint:1", Level = 0 })
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = "HAMMER"
    osiv[key("GetStatString", "HAMMER")] = "WPN_Warhammer"
    stats.WPN_Warhammer = { WeaponRange = 150 }
    world[GOB].comps.Bound = { Bound = { AIBounds = { Stand = { Radius = 0.49 } } } } -- an imp
    for _, d in ipairs({ 1.8, 2.2, 2.45 }) do -- the live refusals were at 1.8 m and 2.2 m
        world[GOB].pos = { d, 0, 0 }
        local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
        eq(j.status, "running", d .. " m: " .. tostring(j.error))
    end
    world[GOB].pos = { 8.8, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "status"); assert(j.error:find("range 1.5 m"), j.error)
    local r, melee = G.spell_range(stats.Target_MainHandAttack, P1)
    eq(r, 1.5, "range"); eq(melee, true, "melee")
end)

test("spell_range: numeric TargetRadius, Range 0 ignored, ranged keyword, unknown -> nil", function()
    eq(G.spell_range({ TargetRadius = "18", Range = 0 }, P1), 18, "numeric")
    eq(select(2, G.spell_range({ TargetRadius = 1.5, Range = 0 }, P1)), true, "shove-like reach is melee")
    eq(G.spell_range({ TargetRadius = "RangedMainWeaponRange", Range = 0 }, P1), G.DEFAULT_RANGED_M, "ranged default")
    eq(G.spell_range({ TargetRadius = "", Range = 0 }, P1), nil, "unknown")
    eq(G.spell_range({ TargetRadius = "MeleeMainWeaponRange", Range = 0 }, P1), 1.5, "unarmed default")
end)

local P2 = "ffffffff-0000-0000-0000-000000000006"
local function group_setup()
    world[P2] = { pos = { -1, 0, 0 }, comps = {} }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("IsOnStage", P2)] = 1
    osiv[key("IsInCombat", P1)] = 1
    osiv[key("IsInCombat", P2)] = 1
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    world[P2].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    local saved = Osi.DB_PartyMembers
    Osi.DB_PartyMembers = { Get = function() return { { "S_Player_" .. P1 }, { "S_Player_" .. P2 } } end }
    return function() Osi.DB_PartyMembers = saved end
end

test("end_turn in a shared turn group: done on RequestedEndTurn, lists who is still to act", function()
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("EndTurn"), 1, "EndTurn called")
    world[P1].comps.TurnBased.RequestedEndTurn = true -- the group stays active (live)
    tick(3)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.ended, true, "ended")
    eq(j.result.still_to_act and j.result.still_to_act[1], P2, "Lae'zel still to act")
    eq(G.is_my_turn(P1), false, "an ended member may not act again")
    eq(G.is_my_turn(P2), true, "the other member may")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "second end_turn"); assert(j2.error:find("already ended its turn"), j2.error)
    restore()
end)

test("end_turn in a group whose EndTurn stays pending: done {requested} after 1.5 s, counts as ended", function()
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    tick(10) -- 0.6 s: still waiting for RequestedEndTurn
    eq(j.status, "running", "status " .. tostring(j.error))
    tick(20) -- past A.END_REQUEST_AFTER_MS; RequestedEndTurn never set (live S002)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.requested, true, "requested")
    eq(j.result.still_to_act and j.result.still_to_act[1], P2, "Lae'zel still to act")
    eq(G.is_my_turn(P1), false, "a pending member may not act again")
    eq(G.turn_requested_end(P1), true, "counts as ended")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "second end_turn refused at once")
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), true, "TurnEnded clears the request (next round)")
    restore()
end)

test("end_turn: done when the turn passes (IsActiveCombatTurn drops), no group left", function()
    local restore = group_setup()
    world[P2].comps.TurnBased.IsActiveCombatTurn = false
    local j = job(op("end_turn", { char = P1 }))
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    tick(3)
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.still_to_act, nil, "nobody left")
    restore()
end)

test("combat gate: an ended group member cannot cast", function()
    local restore = group_setup()
    world[P1].comps.TurnBased.RequestedEndTurn = true
    spell("Shout_Dodge", { SpellType = "Shout", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Shout_Dodge" }))
    eq(j.status, "failed", "status"); assert(j.error:find("turn"), j.error)
    restore()
end)

-- ---- leftover Osiris tasks and the loop watchdog (se, S001 ladder loop) ----

-- P1 with `n` queued Osiris tasks; PurgeOsirisQueue empties them (and is recorded).
local function with_tasks(n)
    local tasks = {}
    for i = 1, n do tasks[i] = { Type = "UseItem" } end
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local orig = Osi.PurgeOsirisQueue
    Osi.PurgeOsirisQueue = function(...)
        orig(...)
        for i = #tasks, 1, -1 do tasks[i] = nil end
    end
    return function() Osi.PurgeOsirisQueue = orig end
end

test("interact: a ladder Use that keeps climbing after UseFinished is purged when the job is done", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(#called("Use"), 1, "Use right away")
    A.dispatch("UseFinished", { P1, GEM, 1 })
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 1, "purged"); eq(j.result.purged_tasks, 1, "purged_tasks")
    restore()
end)

test("interact ladder: the job waits for the climb (no purge at the top), done once down and still", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(#called("Use"), 1, "Use right away")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.status, "running", "still climbing " .. tostring(j.error)); eq(#called("PurgeOsirisQueue"), 0, "no purge yet")
    world[P1].pos = { 9.6, -3, 0 } tick()
    world[P1].pos = { 9.7, -5.8, 0 } tick()
    eq(j.status, "running", "moving")
    world[P1].pos = { 10.2, -5.8, 0.4 } clock = clock + 100 tick() -- stepping off sideways
    world[P1].pos = { 10.6, -5.8, 0.8 } clock = clock + 300 tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    eq(#called("PurgeOsirisQueue"), 1, "purged after the climb")
    restore()
end)

test("interact ladder: a UseFinished during the walk to the top (before our Use) is ignored", function()
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "phase")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.status, "running", "status " .. tostring(j.error)); eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("interact ladder: the loop watchdog leaves a climb in progress alone (several UseFinished)", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    for _ = 1, 4 do A.dispatch("UseFinished", { P1, LADDER, 1 }) end
    tick()
    eq(j.status, "running", "status " .. tostring(j.error))
    restore()
end)

test("interact ladder: no climb after the Use -> failed, says so", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_CLIMB_MS + 1 tick()
    eq(j.status, "failed", "status"); assert(j.error:find("did not climb"), j.error)
end)

test("move_to: a timed-out walk purges its CharacterMoveToPosition task", function()
    local restore = with_tasks(1)
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status")
    clock = j.deadline + 1
    for _, f in ipairs(tickers) do f() end
    eq(j.status, "timeout", "status")
    eq(#called("PurgeOsirisQueue"), 1, "purged")
    restore()
end)

test("move_to: done with an empty queue purges nothing", function()
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    arrive(j, { 5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("loop watchdog: an orphaned task re-using the same ladder is purged after 3 uses", function()
    local restore = with_tasks(2)
    A.use_streak = {}
    A.dispatch("UseFinished", { P1, DOOR, 1 }); clock = clock + 5000
    A.dispatch("UseFinished", { P1, DOOR, 1 }); clock = clock + 5000
    eq(#called("PurgeOsirisQueue"), 0, "not after 2")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    eq(#called("PurgeOsirisQueue"), 1, "purged after 3")
    restore()
end)

test("loop watchdog: uses far apart, other items or no tasks are not a loop", function()
    A.use_streak = {}
    for _ = 1, 4 do A.dispatch("UseFinished", { P1, DOOR, 1 }) end -- no tasks queued
    eq(#called("PurgeOsirisQueue"), 0, "no tasks")
    local restore = with_tasks(1)
    A.use_streak = {}
    for _ = 1, 3 do A.dispatch("UseFinished", { P1, DOOR, 1 }); clock = clock + 25000 end
    A.dispatch("UseFinished", { P1, GEM, 1 })
    eq(#called("PurgeOsirisQueue"), 0, "gaps > 20 s / another item")
    restore()
end)

test("walk: a 0-damage trap puff doesn't interrupt; damage or a character attacker does", function()
    local NULL = "NULL_00000000-0000-0000-0000-000000000000"
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    A.dispatch("AttackedBy", { P1, NULL, GEM, "None", 0, "", 0 })  -- Mausoleum vent: Blinded, 0 dmg
    eq(j.status, "running", "trap puff")
    A.dispatch("AttackedBy", { P1, NULL, GEM, "Force", 5, "", 0 })
    eq(j.status, "interrupted", "trap damage"); assert(j.error:find("attacked"), j.error)
    local k = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    A.dispatch("AttackedBy", { P1, GOB, GOB, "None", 0, "", 0 })    -- a goblin's miss
    eq(k.status, "interrupted", "character miss")
end)

test("loop watchdog: a running job routed through a ladder loop is interrupted", function()
    local restore = with_tasks(1)
    A.use_streak = {}
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    for _ = 1, 3 do A.dispatch("UseFinished", { P1, DOOR, 1 }) end
    eq(j.status, "interrupted", "status"); assert(j.error:find("stuck"), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "purged once")
    restore()
end)

test("act.stop: interrupts the running job and purges the queue", function()
    local restore = with_tasks(3)
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    local s = job(op("stop", { char = P1 }))
    eq(j.status, "interrupted", "walk"); eq(s.status, "done", "stop")
    eq(s.result.stopped_job, j.id, "stopped_job"); eq(s.result.purged_tasks, 3, "purged"); eq(s.result.left, 0, "left")
    restore()
end)

test("cast: a finished spell does not purge the queue", function()
    local restore = with_tasks(1)
    spell("Shout_Dash", { SpellType = "Shout", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Shout_Dash" }))
    eq(j.osi_task, "spell", "marked as a spell task")
    RPC.finish_job(j, "done", {})
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
    restore()
end)


-- ---- locked doors: keys and lockpicking --------------------------------------

-- `who` carries KEY (in a keychain bag) that fits DOOR's lock.
local function keyed_door(who, door_key, key_name)
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = door_key or "PriestessKey" }
    local key_e = { Key = { Key = key_name or "PriestessKey" }, Uuid = { EntityUuid = KEY } }
    local chain = { Uuid = { EntityUuid = "12121212-0000-0000-0000-000000000008" },
        InventoryOwner = { PrimaryInventory = { InventoryContainer = { Items = { { Item = key_e } } } } } }
    local junk = { Uuid = { EntityUuid = GEM } }
    world[who].comps.InventoryOwner = { PrimaryInventory = { InventoryContainer = {
        Items = { { Item = junk }, { Item = chain } } } } }
end

local function add_p2(pos)
    party_rows = { P1, P2 }
    world[P2] = { pos = pos or { 1, 0, 0 }, comps = {} }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("IsOnStage", P2)] = 1
end

test("interact locked: nobody carries the key -> refused with the options", function()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "PriestessKey" }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status"); assert(j.error:find("nobody in the party carries its key"), j.error)
    eq(#called("Unlock"), 0, "Unlock")
end)

test("interact locked: a key with another name does not fit", function()
    keyed_door(P1, "PriestessKey", "PrisonKey")
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status"); assert(j.error:find("carries its key"), j.error)
end)

test("interact locked: actor's own key (keychain) -> walk, Unlock, Use, reported", function()
    keyed_door(P1)
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("Unlock"), 0, "no Unlock before arrival")
    arrive(j, { 8.5, 0, 0 })
    local ul = called("Unlock")
    eq(#ul, 1, "Unlock after arrival"); eq(ul[1][2], DOOR, "door"); eq(ul[1][3], P1, "by")
    eq(#called("Use"), 1, "Use")
    osiv[key("IsLocked", DOOR)] = nil
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.opened, true, "opened"); assert(j.result.unlocked_with, "unlocked_with"); assert(j.result.key_holder, "holder")
end)

test("interact locked: another party member's key -> that member opens it", function()
    add_p2({ 1, 0, 0 })
    keyed_door(P2)
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(j.char, P2, "the key holder acts")
    local mv = called("CharacterMoveToPosition"); local d = mv[#mv]
    for i = 1, 5 do world[P2].pos = { 1 + (d[3] - 1) * i / 5, d[4], d[5] * i / 5 }; tick() end
    A.dispatch("EntityEvent", { P2, j.walk.ev })
    assert(j.status == "running", tostring(j.status) .. " " .. tostring(j.error) .. " " .. tostring(j.phase))
    local ul = called("Unlock")
    eq(#ul, 1, "Unlock"); eq(ul[1][3], P2, "by the holder")
    local use = called("Use"); eq(#use, 1, "Use"); eq(use[1][2], P2, "Use by the holder")
end)

test("interact locked: Use reports failure and it stays locked -> says the key did not open it", function()
    keyed_door(P1)
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Unlock"), 1, "Unlock in place")
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    eq(j.status, "failed", "status"); assert(j.error:find("still locked"), j.error)
end)

test("guard: Unlock stays blocked through G.osi; unlock_with_key needs the key and adjacency", function()
    local refused_or_error = pcall(G.osi, "Unlock", DOOR, P1)
    eq(refused_or_error, false, "G.osi Unlock")
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "PriestessKey" }
    eq(pcall(G.unlock_with_key, DOOR, P1, 2.5), false, "no key")
    keyed_door(P1)
    eq(pcall(G.unlock_with_key, DOOR, P1, 2.5), false, "too far (10 m)")
    world[DOOR].pos = { 1, 0, 0 }
    eq(pcall(G.unlock_with_key, DOOR, P1, 2.5), true, "key + adjacent")
    eq(#called("Unlock"), 1, "one Unlock")
end)

test("lockpick: a party member carries the key -> refused, points to interact", function()
    keyed_door(P1)
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status"); assert(j.error:find("use interact"), j.error)
end)

-- The click's own request (NETMSG_CHARACTER_LOCKPICK -> LockpickingSystem.PickLockRequests).
local function pick_system()
    Ext.System = { ServerLockpicking = { PickLockRequests = {} } }
    return Ext.System.ServerLockpicking.PickLockRequests
end

test("lockpick (default): no engine request; right-click -> context menu 'Lockpick' (S024 hand route)", function()
    osiv[key("IsLocked", DOOR)] = 1
    local reqs = pick_system()
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#reqs, 0, "no PickLockRequest (live: never consumed, stale ones pile up)")
    eq(j.result.needs_click, DOOR, "needs_click"); eq(j.result.expect, "roll", "expect")
    eq(j.result.button, "R", "right-click"); eq(j.result.menu, "Lockpick", "menu item")
end)

test("lockpick: a lock with no lockpick difficulty (zero DC id) -> refused before walking, no Use/click", function()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "NO_KEY", LockDifficultyClassID = "00000000-0000-0000-0000-000000000000", LockDC = 0 }
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status"); assert(j.error:find("cannot be picked"), j.error)
    eq(#called("Use"), 0, "no Use"); eq(#called("CharacterMoveToPosition"), 0, "no walk")
end)

test("lockpick: a lock with a difficulty id -> proceeds to the right-click, no engine request", function()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "NO_KEY", LockDifficultyClassID = "0d9484eb-0000-4000-8000-000000000001", LockDC = 0 }
    local reqs = pick_system()
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    arrive(j, { 8.5, 0, 0 })
    eq(#reqs, 0, "no PickLockRequest (it corrupts the game's array: crash 12:35)")
    eq(j.result.needs_click, DOOR, "needs_click"); eq(j.result.menu, "Lockpick", "menu")
end)

-- ---- turn-based mode outside combat (S024-S026 Shadow Curse) ---------------

test("tb gate: a walk in turn-based mode outside combat is refused at once, no walk", function()
    osiv[key("IsInForceTurnBasedMode", P1)] = 1
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    eq(j.status, "failed", "status"); assert(j.error:find("turn-based mode outside combat", 1, true), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no walk")
    j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "failed", "interact status")
end)

test("tb gate: tb_ok (the agent's own turn-based mode) walks; in combat no gate", function()
    osiv[key("IsInForceTurnBasedMode", P1)] = 1
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 }, tb_ok = true }))
    eq(j.status, "running", "tb_ok status " .. tostring(j.error))
end)

test("act.turn_based off: ForceTurnBasedMode for every party member still in it", function()
    party_rows = { P1, NPC }
    osiv[key("IsInForceTurnBasedMode", P1)] = 1
    osiv[key("IsInForceTurnBasedMode", NPC)] = 1
    local j = job(op("turn_based", { on = false }))
    tick(2)
    eq(#called("ForceTurnBasedMode"), 2, "both members")
    local m = RPC.ops["act.mode"]({})
    eq(m.ftb, true, "act.mode ftb"); eq(#m.ftb_members, 2, "members")
    eq(j.status, "running", "running until they leave it")
end)

test("interact: in combat a carried potion is drunk on the drinker's turn for its own cost (S031 at 7 HP)", function()
    combat_setup(9)
    local POT = "a0a0a0a0-0000-0000-0000-000000000009"
    world[POT] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "OBJ_Potion_Healing" } } }
    osiv[key("IsItem", POT)] = 1
    osiv[key("GetInventoryOwner", POT)] = P1
    stats.OBJ_Potion_Healing = { ItemUseType = "Potion", UseCosts = "BonusActionPoint:1" }
    world[P1].comps.ActionResources.Resources.BonusActionPoint = { { Level = 0, Amount = 1 } }
    local j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "running", "drinks: " .. tostring(j.error))
    eq(#called("Use"), 1, "one Use")
    A.dispatch("UseFinished", { P1, POT, 1 })
    tick(10)
    eq(j.status, "done", "done"); eq(j.result.drunk, true, "drunk")
    eq(j.result.charged_by_guard[1].resource, "bonus action", "bonus charged")
    eq(world[P1].comps.ActionResources.Resources.BonusActionPoint[1].Amount, 0, "bonus spent")
    -- no bonus left: refused, no Use
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "no bonus"); assert(j.error:find("not enough bonus action", 1, true), j.error)
    -- not this character's turn
    world[P1].comps.ActionResources.Resources.BonusActionPoint[1].Amount = 1
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "not my turn"); assert(j.error:find("not this character's turn", 1, true), j.error)
    -- a scroll (not a potion) stays out-of-combat only
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    stats.OBJ_Potion_Healing.ItemUseType = "Scroll"
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "scroll"); assert(j.error:find("during combat", 1, true), j.error)
    eq(#called("Use"), 1, "still one Use")
end)

test("cast: Oath of Devotion refuses a killing blow on a Temporarily Hostile target unless force (S026/S028)", function()
    combat_setup(9)
    world[GOB].pos = { 1.2, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" }, SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    world[P1].comps.PassiveContainer = { Passives = { { Passive = { PassiveId = "Oath_Devotion_Tenents" } },
        { Passive = { PassiveId = "NonLethal", Disabled = true } } } }
    world[GOB].comps.Health = { Hp = 2, MaxHp = 67 }
    osiv[key("HasActiveStatus", GOB, "TEMPORARILY_HOSTILE")] = 1
    osiv[key("IsEnemy", P1, GOB)] = 1
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "refused"); assert(j.error:find("Oath of Devotion", 1, true), j.error)
    eq(#called("UseSpell"), 0, "no cast")
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB, force = true }))
    eq(j.status, "running", "force: " .. tostring(j.error))
    RPC.jobs[j.id] = nil; A.active[P1] = nil
    -- healthy target: no risk yet
    world[GOB].comps.Health = { Hp = 50, MaxHp = 67 }
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "running", "healthy target: " .. tostring(j.error))
    RPC.jobs[j.id] = nil; A.active[P1] = nil
    -- a real enemy at 2 HP: fine
    world[GOB].comps.Health = { Hp = 2, MaxHp = 67 }
    osiv[key("HasActiveStatus", GOB, "TEMPORARILY_HOSTILE")] = nil
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "running", "enemy: " .. tostring(j.error))
    RPC.jobs[j.id] = nil; A.active[P1] = nil
    -- a neutral at 2 HP: refused; Non-Lethal Attacks on: allowed
    osiv[key("IsEnemy", P1, GOB)] = nil
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "neutral"); assert(j.error:find("neutral", 1, true), j.error)
    world[P1].comps.PassiveContainer.Passives[2].Passive.Disabled = false
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "running", "non-lethal on: " .. tostring(j.error))
    RPC.jobs[j.id] = nil; A.active[P1] = nil
    -- not a Devotion paladin: never refused
    world[P1].comps.PassiveContainer.Passives[2].Passive.Disabled = true
    world[P1].comps.PassiveContainer.Passives[1].Passive.PassiveId = "Something_Else"
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "running", "not devotion: " .. tostring(j.error))
end)

test("go_to_camp: a dead selected character doesn't block the party's camp button (S035 19:38)", function()
    Osi.TeleportToFromCamp = record("TeleportToFromCamp")
    add_p2()
    osiv[key("GetHostCharacter")] = P1
    osiv[key("IsDead", P1)] = 1
    local j = job(op("go_to_camp", {}))
    eq(j.status, "running", "status: " .. tostring(j.error))
    local tp = called("TeleportToFromCamp")
    eq(#tp, 1, "teleport"); eq(tp[1][2], P2, "the living member presses it")
    RPC.jobs[j.id] = nil; A.active[P2] = nil
    -- an explicit char is still checked
    j = job(op("go_to_camp", { char = P1 }))
    eq(j.status, "failed", "explicit dead char"); assert(j.error:find("character is dead", 1, true), j.error)
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
