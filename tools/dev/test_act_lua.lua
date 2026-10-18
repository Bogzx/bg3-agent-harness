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
    DetachFromPartyGroup = record("DetachFromPartyGroup"),
    AttachToPartyGroup = record("AttachToPartyGroup"),
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
    if A then A.TASK_POS2 = false end
    if G then G.turn_over = {} end -- TurnEnded's marker (turn over until TurnStarted) is per test
    if G then G.turn_start, G.foe_start = {}, {} end -- TurnStarted records (Guard.turn_moved_on) too
    if A then A.hit_ms = {} end -- AttackedBy on a party member (cast re-order window) is per test
    -- the stub world moves in synthetic multi-metre steps per 60 ms tick: NAV-8's small-step rule off (own tests)
    if A then A.TELEPORT_SMALL_M = 1e9 end
    if A then A.trip_t = nil end -- a fast travel's arrival (act.trip_arrived) is per test
    if A then A.unlocked_ms = {} end -- Unlocked/Opened seen on an item (V24) is per test
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

test("within: a distance that rounds to the max shows one decimal (S028 15:53 '40m ..., max 40m')", function()
    world[NPC].pos = { 40.3, 0, 0 }
    refused(function() G.within(P1, NPC, 40, "target") end, "(40.3m from this character, max 40m)")
    world[NPC].pos = { 47, 0, 0 }
    refused(function() G.within(P1, NPC, 40, "target") end, "(47m from this character, max 40m)")
end)

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

test("approach: a big target is used from its rim (S044 23:50 the Hollow Tower: closest spot 3.0 m from it)", function()
    nav.fn = function() return { reachable = false } end -- every ring point lies over the hole
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ bounds = function() return { radius = 2.0, height = 1 } end }, { __index = savedV })
    BG3AI.Nav.closestReach = function(_, goal)
        return { pos = { goal[1] - 3, goal[2], goal[3] }, path_m = 6, to_goal_m = 3.0, dy_to_goal = 0 }
    end
    local ok, err = pcall(function()
        local p = G.approach_point(P1, NPC, 1.5)
        eq(p.rim, true, "rim"); near(p.length_m, 6, "walk")
        BG3AI.Nav.closestReach = function(_, goal)  -- far from the rim: still refused
            return { pos = { goal[1] - 8, goal[2], goal[3] }, path_m = 6, to_goal_m = 8.0, dy_to_goal = 0 }
        end
        refused(function() G.approach_point(P1, NPC, 1.5) end, "no walkable path")
    end)
    BG3AI.Visibility, BG3AI.Nav.closestReach = savedV, nil
    if not ok then error(err, 0) end
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

test("NAV-8: approach with this frame's path budget spent -> refused (no walk without a path proof)", function()
    nav.fn = function() return { reachable = nil, budget = true, unfinished = true } end
    world[NPC].pos = { 6, 0, 0 }
    refused(function() G.approach_point(P1, NPC, 2.5) end, "busy")
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

-- A jump that lands where the walk was going waits A.TELEPORT_SETTLE_MS for a story signal (teleport_check).
local function settle() tick(math.ceil(A.TELEPORT_SETTLE_MS / 60) + 1) end
local function last_event(kind, since)
    for i = #RPC.events, 1, -1 do
        local ev = RPC.events[i]
        if ev.seq <= (since or 0) then return nil end
        if ev.kind == kind then return ev end
    end
end

test("move_to: teleport is detected and purged", function()
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 30, 0, 0 }
    tick()
    eq(j.status, "running", "settling"); assert(#called("PurgeOsirisQueue") >= 1, "purged at once")
    settle()
    eq(j.status, "failed", "status"); assert(j.error:find("engine teleported"), j.error)
    eq(j.result.teleported, true, "teleported"); eq(j.result.by_game, nil, "ours")
    local ev = last_event("teleport_blocked")
    assert(ev and ev.data.m == 30, "teleport_blocked m")
end)

test("V9 S066: a jump onto the walk's spot + the walker pulled into a conversation = the game moved it", function()
    local seq = RPC.eventSeq
    local j = job(op("move_to", { char = P1, pos = { 21.7, -1, 0 } }))
    tick()
    world[P1].pos = { 21.7, -1.3, 0.4 } -- Orin's arena: 21.7 m in one tick, 0.5 m from where the walk was going
    tick()
    eq(j.status, "running", "settling")
    A.dispatch("DialogActorJoined", { "ORIN_DLG", 7, P1, 0 })
    eq(j.status, "interrupted", "status"); eq(j.result.by_game, true, "by_game")
    assert(j.error:find("the game moved", 1, true) and j.error:find("a conversation started", 1, true), j.error)
    near(j.result.m, 21.7, "m"); eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
    eq(last_event("teleported_by_game", seq).data.why, "a conversation started", "event")
end)

test("V9 S066: a quest step unlocking right after the jump is the story's too", function()
    local j = job(op("move_to", { char = P1, pos = { 21.7, -1, 0 } }))
    tick()
    world[P1].pos = { 21.7, -1.3, 0.4 }
    tick(5)
    A.dispatch("QuestUpdateUnlocked", { P1, "CTY_OrinNetherstone", "Step" })
    eq(j.status, "interrupted", "status"); eq(j.result.why, "a quest updated", "why")
end)

test("V9: a conversation another member joins while the jump settles is no signal", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 50, 0, 0 }, comps = {} }
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 30, 0, 0 }
    tick()
    A.dispatch("DialogActorJoined", { "OTHER_DLG", 8, P2, 0 })
    eq(j.status, "running", "still settling")
    settle()
    eq(j.status, "failed", "the engine's")
end)

test("V9 S063: a jump that lands far from where the walk was going is the game's at once", function()
    local j = job(op("move_to", { char = P1, pos = { 3, 0, 0 } }))
    tick()
    world[P1].pos = { 0.5, -22, 0 } -- the ritual drop: 22 m straight down, the walk was going 3 m E
    tick()
    eq(j.status, "interrupted", "status"); eq(j.result.by_game, true, "by_game")
    assert(j.error:find("landed 22 m from where it was walking", 1, true), j.error)
end)

-- V15 S073 22:33:39: Lower City Central Wall reached at 23509.9 (bg3le log clock), the walk from the waypoint began 0.5 s later;
-- at 23518.86, 19 m along it, Claude jumped 29.8 m (7.8 m down) onto the walk's own end, no story signal with it.
local S073_FROM, S073_END = { -48.5, 27.9, -73.5 }, { -47.7, 20.1, -102.2 }
local function s073_jump(after_ms, land)
    op("trip_arrived", {})
    world[P1].pos = { S073_FROM[1], S073_FROM[2], S073_FROM[3] }
    local j = job(op("move_to", { char = P1, pos = S073_END }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local sway = 0.05
    while clock - A.trip_t < after_ms do -- 5 cm to and fro a tick: not standing still (the 5 s stuck check)
        sway = -sway
        world[P1].pos = { S073_FROM[1] + sway, S073_FROM[2], S073_FROM[3] }
        tick()
    end
    world[P1].pos = land or S073_END
    tick()
    return j
end

test("V15 S073: a jump onto the walk's end 9 s after a fast travel is the game's: the walk is done", function()
    local seq = RPC.eventSeq
    local j = s073_jump(9000)
    eq(j.status, "running", "settling (a story signal may still come)")
    settle()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.by_trip, true, "by_trip")
    near(j.result.m, 29.8, "m"); near(j.result.dist, 0, "at the walk's end")
    assert(j.result.after_trip_s >= 9 and j.result.after_trip_s < 9.2, "after_trip_s " .. j.result.after_trip_s)
    eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
    eq(last_event("teleported_by_game", seq), nil, "no story move (it stops the member's queue)")
    near(last_event("moved_after_trip", seq).data.m, 29.8, "event")
end)

test("V15: the same jump 16 s after the arrival is still the engine's teleport", function()
    local j = s073_jump(16000)
    settle()
    eq(j.status, "failed", "status"); assert(j.error:find("engine teleported"), j.error)
    eq(j.result.by_trip, nil, "by_trip")
end)

test("V15: a jump off the walk's end right after a fast travel is the game's move, not ours", function()
    local seq = RPC.eventSeq
    local j = s073_jump(3000, { -48.0, 27.9, -78.5 }) -- 5 m along, 24 m short of the end
    settle()
    eq(j.status, "interrupted", "status"); eq(j.result.by_game, true, "by_game")
    assert(j.error:find("3 s after the fast travel", 1, true), j.error)
    eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
end)

test("V15: an approach put on its spot right after a fast travel does not count its action as done", function()
    op("trip_arrived", {})
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.phase, "approach", "phase")
    tick()
    world[P1].pos = { j.walk.pos[1], j.walk.pos[2], j.walk.pos[3] }
    tick()
    settle()
    eq(j.status, "interrupted", "status " .. tostring(j.error)); eq(#called("Use"), 0, "no Use")
end)

test("V9 S066: the members walking to him keep walking when his conversation starts", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 0, 0, 5 }, comps = {} }
    osiv[key("IsCharacter", P2)] = 1; osiv[key("IsOnStage", P2)] = 1
    local j = job(op("move_to", { char = P2, pos = { 20, 0, 5 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    A.dispatch("DialogActorJoined", { "ORIN_DLG", 7, P1, 0 })
    eq(j.status, "running", "another member's conversation")
    A.dispatch("DialogActorJoined", { "ORIN_DLG", 7, P2, 0 })
    eq(j.status, "interrupted", "its own"); eq(j.error, "a conversation started", "why")
end)

-- vis V25, S084 04:16-04:22 (Counting House vault, 9 Marble Plates, code 1-3-5-6): every walk crossed other plates
-- and reset the code. Plates the party has seen (V.fixtureSeen) are walked round; move_to(target=plate) stands on it.
-- Nav.route/routeFrom are straight lines here (the plates' geometry is the test).
local PLATE_A = "a1a1a1a1-0000-0000-0000-00000000000a"
local PLATE_T = "a2a2a2a2-0000-0000-0000-00000000000b"
local function with_plates(plates, seen, fn)
    local savedV, savedR, savedF = BG3AI.Visibility, BG3AI.Nav.route, BG3AI.Nav.routeFrom
    local names, fixtureSeen = {}, {}
    for u, p in pairs(plates) do
        world[u] = { pos = p, comps = {} }
        names[u] = "Marble Plate"
        osiv[key("IsItem", u)] = 1
        osiv[key("IsOnStage", u)] = 1
        osiv[key("CanSee", P1, u)] = 1
        osiv[key("HasLineOfSight", P1, u)] = 1
    end
    for _, u in ipairs(seen) do fixtureSeen[u] = { t = clock, level = "CTY_Main_A" } end
    BG3AI.Visibility = setmetatable({
        PLATE_NAMES = { ["Marble Plate"] = true }, fixtureSeen = fixtureSeen, FIXTURE_REMEMBER_MS = 300000,
        levelNow = function() return "CTY_Main_A" end, name = function(u) return names[u] end,
        bounds = function() return { radius = 0.6, height = 0.1 } end,
    }, { __index = savedV })
    BG3AI.Nav.route = function(_, to) return { { world[P1].pos[1], world[P1].pos[2], world[P1].pos[3] }, to } end
    BG3AI.Nav.routeFrom = function(_, from, to) return { from, to } end
    local ok, err = pcall(fn)
    BG3AI.Visibility, BG3AI.Nav.route, BG3AI.Nav.routeFrom = savedV, savedR, savedF
    if not ok then error(err, 0) end
end

test("move_to V25: a seen plate on the straight way is walked round via a point beside it", function()
    with_plates({ [PLATE_A] = { 5, 0, 0 } }, { PLATE_A }, function()
        local j = job(op("move_to", { char = P1, pos = { 10, 0, 0 } }))
        eq(j.status, "running", "status " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "one walk at a time")
        near(mv[1][3], 5, "via x"); near(math.abs(mv[1][5]), 1.6, "via z: beside the plate")
        arrive(j, { mv[1][3], 0, mv[1][5] })
        mv = called("CharacterMoveToPosition")
        eq(#mv, 2, "then the destination"); near(mv[2][3], 10, "dest x"); near(mv[2][5], 0, "dest z")
        arrive(j, { 10, 0, 0 })
        eq(j.status, "done", "status " .. tostring(j.error))
        near(j.result.round_plates[1], 5, "round_plates")
        eq(j.result.over_plates, nil, "over_plates")
    end)
end)

test("move_to V25: no clear way round -> walks as before and names the plate it crosses", function()
    with_plates({ [PLATE_A] = { 5, 0, 0 } }, { PLATE_A }, function()
        BG3AI.Nav.routeFrom = function() return nil end
        local j = job(op("move_to", { char = P1, pos = { 10, 0, 0 } }))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "moves"); near(mv[1][3], 10, "straight to the destination")
        arrive(j, { 10, 0, 0 })
        eq(j.status, "done", "status " .. tostring(j.error))
        eq(j.result.over_plates and j.result.over_plates[1], PLATE_A, "over_plates")
    end)
end)

test("move_to V25: a plate the party has not seen is not avoided (fair), nor the one it stands on", function()
    with_plates({ [PLATE_A] = { 5, 0, 0 }, [PLATE_T] = { 0.3, 0, 0 } }, { PLATE_T }, function()
        local j = job(op("move_to", { char = P1, pos = { 10, 0, 0 } }))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "moves"); near(mv[1][3], 10, "straight"); near(mv[1][5], 0, "straight z")
        arrive(j, { 10, 0, 0 })
        eq(j.result.round_plates, nil, "round_plates"); eq(j.result.over_plates, nil, "over_plates")
    end)
end)

test("move_to V25: target=<plate> walks onto its centre (not the ring beside it), round the other plates", function()
    with_plates({ [PLATE_A] = { 3, 0, 0 }, [PLATE_T] = { 6, 0, 0 } }, { PLATE_A, PLATE_T }, function()
        local j = job(op("move_to", { char = P1, target = PLATE_T }))
        eq(j.status, "running", "status " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        near(math.abs(mv[1][5]), 1.6, "via beside plate A")
        arrive(j, { mv[1][3], 0, mv[1][5] })
        mv = called("CharacterMoveToPosition")
        near(mv[2][3], 6, "onto the plate x"); near(mv[2][5], 0, "onto the plate z")
        arrive(j, { 6, 0, 0.2 })
        eq(j.status, "done", "status " .. tostring(j.error))
        eq(j.result.on_plate, PLATE_T, "on_plate")
    end)
end)

test("move_to V25: a plate walk that stops beside the plate fails, saying so", function()
    with_plates({ [PLATE_T] = { 6, 0, 0 } }, { PLATE_T }, function()
        local j = job(op("move_to", { char = P1, target = PLATE_T }))
        arrive(j, { 4.8, 0, 0 })
        eq(j.status, "failed", "status")
        assert(j.error:find("not on it (1.2 m from its centre)", 1, true), j.error)
    end)
end)

test("move_to V25: already on the plate -> done, no walk", function()
    with_plates({ [PLATE_T] = { 0.2, 0, 0 } }, { PLATE_T }, function()
        local j = job(op("move_to", { char = P1, target = PLATE_T }))
        eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.on_plate, PLATE_T, "on_plate")
        eq(#called("CharacterMoveToPosition"), 0, "moves")
    end)
end)

-- vis V26, S084 04:09-04:12 (the Counting House): let off "with a warning. This time.", then travel_to e578 walked
-- into the vault guards' off-limits area (Dadger's talk at (-111,4,-177)) and the second offence started a fight.
-- The bridge passes the areas the game warned about as avoid; walks go round them, never into them.
local DADGER = { id = 2, pos = { -111, 4, -177 }, r = 6, level = "CTY_Main_A",
    name = "the off-limits area by Cashguard Artillerist Dadger (-111,4,-177)" }
local function move_refused(args, pat)
    local r = op("move_to", args)
    eq(r.status, "failed", "status")
    assert(r.error:find(pat, 1, true), r.error)
end

test("move_to V26: a destination inside a warned off-limits area is refused, naming it (S084 travel_to e578)", function()
    with_plates({}, {}, function()
        world[P1].pos = { -92.2, 5.7, -188.2 } -- the east office, after the loot
        world[DOOR].pos = { -112, 4, -176 }    -- e578 Grated Iron Doors, by the guards
        move_refused({ char = P1, target = DOOR, avoid = { DADGER } },
            "the destination is inside the off-limits area by Cashguard Artillerist Dadger (-111,4,-177) you were warned about")
        eq(#called("CharacterMoveToPosition"), 0, "no walk")
        move_refused({ char = P1, pos = { -110, 4, -178 }, avoid = { DADGER } }, "trespass=true")
        local j = job(op("move_to", { char = P1, pos = { -110, 4, -178 }, avoid = { DADGER }, trespass = true }))
        eq(j.status, "running", "trespass=true walks in: " .. tostring(j.error))
    end)
end)

test("move_to V26: a way through a warned area goes round it and says so", function()
    with_plates({}, {}, function()
        world[P1].pos = { -100, 4, -177 }
        local j = job(op("move_to", { char = P1, pos = { -122, 4, -177 }, avoid = { DADGER } }))
        eq(j.status, "running", "status " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "via first")
        local via = { mv[1][3], mv[1][4], mv[1][5] }
        assert(math.abs(via[3] + 177) > 6, "via clear of the area: z=" .. via[3])
        arrive(j, via)
        arrive(j, { -122, 4, -177 })
        eq(j.status, "done", "status " .. tostring(j.error))
        assert(j.result.avoiding and j.result.avoiding:find("Dadger", 1, true), tostring(j.result.avoiding))
        assert(j.result.avoiding:find("you were warned about", 1, true), j.result.avoiding)
    end)
end)

test("move_to V26: no way round a warned area -> refused, never walked through", function()
    with_plates({}, {}, function()
        world[P1].pos = { -100, 4, -177 }
        BG3AI.Nav.routeFrom = function() return nil end
        move_refused({ char = P1, pos = { -122, 4, -177 }, avoid = { DADGER } },
            "crosses the off-limits area by Cashguard Artillerist Dadger (-111,4,-177) you were warned about and no way round")
        eq(#called("CharacterMoveToPosition"), 0, "no walk")
    end)
end)

test("move_to V26: no warning, another level, another floor, or standing in it -> walks as before", function()
    with_plates({}, {}, function()
        world[P1].pos = { -100, 4, -177 }
        local function straight(avoid)
            calls = {}
            local j = job(op("move_to", { char = P1, pos = { -122, 4, -177 }, avoid = avoid }))
            local mv = called("CharacterMoveToPosition")
            eq(#mv, 1, "one walk"); near(mv[1][3], -122, "straight to the destination")
            j.status = "done"
            A.active = {}
        end
        straight(nil)
        straight({ { id = 1, pos = DADGER.pos, r = 6, level = "SCL_Main_A", name = "x" } })
        straight({ { id = 1, pos = { -111, 12.5, -177 }, r = 6, level = "CTY_Main_A", name = "upstairs" } })
        world[P1].pos = { -108, 4, -177 } -- inside it already: walking out is right
        calls = {}
        local j = job(op("move_to", { char = P1, pos = { -122, 4, -177 }, avoid = { DADGER } }))
        eq(j.status, "running", "status " .. tostring(j.error))
        near(called("CharacterMoveToPosition")[1][3], -122, "straight out")
    end)
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
    eq(j.status, "interrupted", "status"); eq(j.result.teleported, true, "teleported") -- 23 m off the walk's end
end)

test("NAV-8: a 3 m jump within one 60 ms job tick is a teleport (both old tests passed it)", function()
    A.TELEPORT_SMALL_M = 1.5
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 0.3, 0, 0 }
    tick()
    eq(j.status, "running", "a walk's step")
    world[P1].pos = { 3.3, 0, 0 } -- jobs tick every TICK_MS 50 ms: 60 ms here
    tick()
    settle() -- a 3 m jump is never the story's at once
    eq(j.status, "failed", "status"); assert(j.error:find("teleported"), j.error)
end)

test("NAV-8: a walk's step after a frame hitch (2 m in 300 ms) is not a teleport", function()
    A.TELEPORT_SMALL_M = 1.5
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    clock = clock + 240 -- a 300 ms tick
    world[P1].pos = { 2, 0, 0 }
    tick()
    eq(j.status, "running", "status " .. tostring(j.error))
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

test("combat move_to: a walk that never starts fails after 2.5 s naming a Lethargic walker (S074 r2)", function()
    combat_setup(12)
    stats["HASTE_LETHARGY"] = { StatusType = "INCAPACITATED" }
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function() return { { id = "HASTE_LETHARGY", name = "Lethargic" } } end },
        { __index = savedV })
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    tick(40) -- 2.4 s standing still: the old walk waited 5 s for "no path to the destination"
    eq(j.status, "running", "before the window")
    tick(3)
    BG3AI.Visibility = savedV
    eq(j.status, "failed", "status")
    assert(j.error:find("never started moving (2.5 s, the route was there); the character is Lethargic", 1, true), j.error)
    assert(not j.error:find("not this character's turn", 1, true), "the turn goes on: " .. j.error)
    assert(#called("PurgeOsirisQueue") >= 1, "the order is purged")
end)

test("combat move_to: the turn passing mid-walk stops it at once with the fatal turn text (S074 r2)", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    world[P1].pos = { 1, 0, 0 }
    tick()
    world[P1].comps.TurnBased.IsActiveCombatTurn = false -- the game ended the turn (now Cazador's)
    tick(2)
    eq(j.status, "running", "a flag seen for 2 ticks is not a stop yet")
    tick()
    eq(j.status, "failed", "status")
    assert(j.error:find("not this character's turn any more: the game passed the turn on during the walk (travelled 1.0 m)",
        1, true), j.error)
end)

test("combat move_to: the flag dropping because the fight ended is not a stop; no start window out of combat", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    osiv[key("IsInCombat", P1)] = nil -- the last enemy died: out of combat, the walk goes on
    tick(60) -- 3.6 s, past the combat no-start window too: the walk began in combat, but the fight is over
    eq(j.status, "running", "walks on " .. tostring(j.error))
    reset()
    j = job(op("move_to", { char = P1, target = NPC }))
    tick(60)
    eq(j.status, "running", "out of combat: the old 5 s rule only")
    tick(30)
    eq(j.status, "failed", "5 s"); assert(j.error:find("no path to the destination", 1, true), j.error)
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

-- V24, S083 03:55:56-58: Knock unlocked e720 (look: "unlocked"), 2 s later IsLocked read true again and interact
-- refused "nobody in the party carries its key" while EVENTS had door_opened e720.
local function knocked_door()
    world[DOOR].comps.Lock = { Key_M = "", LockDifficultyClassID = "11111111-2222-3333-4444-555555555555" }
    osiv[key("IsLocked", DOOR)] = 1
end
local function interact_refused(pat)
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status")
    assert(j.error:find(pat, 1, true), j.error)
end

test("interact V24: a door the game unlocked 2 s ago that reads locked again is clicked, not refused", function()
    knocked_door()
    interact_refused("nobody in the party carries its key")
    A.dispatch("Unlocked", { DOOR, P2, "NULL_00000000-0000-0000-0000-000000000000" })
    clock = clock + 2000
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Use"), 1, "the click is sent")
    osiv[key("IsLocked", DOOR)] = nil -- the game finishes unlocking it; the Use opens it
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
end)

test("interact V24: still locked after that click says the game locked it again, not 'no lock to pick'", function()
    knocked_door()
    A.dispatch("Opened", { DOOR })
    local j = job(op("interact", { char = P1, target = DOOR }))
    arrive(j, { 8.5, 0, 0 })
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    clock = clock + 1600 tick()
    eq(j.status, "failed", "status"); eq(j.error, A.RELOCKED, "error")
end)

test("interact V24: a door opened moments ago that still reads locked: 'it is open now', no Use", function()
    knocked_door()
    A.dispatch("Opened", { DOOR })
    osiv[key("IsOpened", DOOR)] = 1
    clock = clock + 2000
    interact_refused("it is open now (the game unlocked or opened it 2 s ago)")
    eq(#called("Use"), 0, "Use")
end)

test("interact V24: an unlock older than LOCK_FRESH_MS no longer counts: refused as locked", function()
    knocked_door()
    A.dispatch("Unlocked", { DOOR, P2, "" })
    clock = clock + A.LOCK_FRESH_MS + 1
    interact_refused("nobody in the party carries its key")
end)

-- Live L3 helm probe 2026-10-17 (e39 at the Nautiloid helm): the ring point facing the actor lies under the upper
-- landing; GetHeightsAt there gives only the upper floor, and N.path to it is a partial path (ends 4.2 m short).
local function helm_heights(x) if x < 9.2 then return { -12.0, 3.1 } end return { 0.0, 3.1 } end

test("G.reachable: a partial path (ends short of the point) is not reachable; a point move names where it ends", function()
    nav.fn = function() return { reachable = true, partial = true, end_off = 4.2, end_pos = { 6.8, 0, 1.4 }, length_m = 1.8 } end
    local r, info = G.reachable(P1, { 8.2, 3.1, 0 })
    eq(r, false, "reachable"); eq(info.partial, true, "partial"); eq(info.end_off, 4.2, "end_off")
    refused(function() G.point_reachable(P1, { 8.2, 3.1, 0 }) end, "the walkable ground ends 4.2m short of it, at (6.8,0.0,1.4)")
end)

test("approach: a ladder's foot is approached on the foot's floor, not under its upper landing (live L3 helm probe)", function()
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = helm_heights
    nav.fn = function(_, pos)
        if pos[2] > 1.5 then return { reachable = true, partial = true, end_off = 4.2, length_m = 1.8 } end
        return { reachable = true, length_m = 6.7 }
    end
    local ok, err = pcall(function()
        local p = G.approach_point(P1, LADDER, A.USE_REACH)
        near(p.pos[2], 0, "on the foot's floor"); near(p.length_m, 6.7, "the full path")
        near(p.use_y, 0, "use_y = the foot")
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("approach: a ladder ring point on the other floor is skipped even when it reads reachable", function()
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = helm_heights
    nav.fn = function(_, pos) return { reachable = true, length_m = pos[2] > 1.5 and 1.8 or 6.7 } end
    local ok, err = pcall(function()
        near(G.approach_point(P1, LADDER, A.USE_REACH).pos[2], 0, "ladder: on the foot's floor")
        near(G.approach_point(P1, GEM, A.USE_REACH).pos[2], 3.1, "not a ladder: the nearest floor point, as before")
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("interact ladder: the approach walk already climbed it -> done climbed=up, no Use (live L3 helm probe)", function()
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "walks first")
    arrive(j, { 9.6, 2.4, 0 }) -- the engine's walk went up the ladder and stepped off at the top
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.climbed, "up", "climbed"); eq(j.result.climbed_on_walk, true, "on the walk")
    eq(#called("Use"), 0, "no Use (it would climb back down)")
end)

test("interact ladder: a walk that ends on the foot's floor still settles and Uses", function()
    local j = job(op("interact", { char = P1, target = LADDER }))
    arrive(j, { 9.0, 0, 0 })
    eq(j.status, "running", "status " .. tostring(j.error))
    tick() tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use")
end)

test("interact in combat names the tool a player uses: click_on <target> (naut3 x3: '(use the UI)')", function()
    osiv[key("IsInCombat", P1)] = 1
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.status, "failed", "status")
    assert(j.error:find("click_on " .. GEM, 1, true), tostring(j.error))
    eq(#called("Use"), 0, "no Use")
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
    settle()
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

test("job.notify (ACT-10): a waited job that outlived the wait announces its end as job_finished", function()
    local r = op("interact", { char = P1, target = DOOR })
    local j = job(r)
    local function finished()
        for _, ev in ipairs(RPC.events) do
            if ev.kind == "job_finished" and ev.data.job == "j" .. tostring(j.id) then return ev end
        end
    end
    eq(finished(), nil, "no event before")
    local v = RPC.ops["job.notify"]({ id = j.id })
    eq(v.status, "running", "view")
    RPC.finish_job(j, "done", { used = true })
    assert(finished(), "job_finished pushed")
    eq(RPC.ops["job.notify"]({ id = j.id }).status, "done", "an ended job's view")
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

test("cast in combat (ACT-11): a dropped order fails after CAST_NO_START_COMBAT_MS, not 8 s", function()
    combat_setup(9)
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", target = NPC }))
    eq(j.status, "running", "started " .. tostring(j.error))
    tick(40) -- 2.4 s
    eq(j.status, "running", "early")
    tick(12) -- 3.1 s, no CastSpell, no step
    eq(j.status, "failed", "status"); assert(j.error:find("never started it %(3 s"), j.error)
    -- a cast the engine took waits on
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Thing", target = NPC }))
    A.dispatch("CastSpell", { P1, "Target_Thing", "Target", "None", 0 })
    tick(60)
    eq(j.status, "running", "began")
end)

test("cast in combat while Stunned is refused; a dropped order while Fearful names the fear (S066/S067)", function()
    combat_setup(9)
    spell("Projectile_EB", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    spell("STUNNED", { StatusType = "INCAPACITATED" })
    spell("FEARED", { StatusType = "FEAR" })
    spell("BLEEDING", { StatusType = "BOOST" })
    local savedV = BG3AI.Visibility
    local on = {}
    BG3AI.Visibility = setmetatable({ statuses = function() return on end }, { __index = savedV })
    -- S067 #43/#54: Wyll Stunned, Eldritch Blast "never started it" x2: refused before any order now
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "STUNNED", name = "Stunned" } }
    local j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    eq(j.status, "failed", "stunned"); assert(j.error:find("cannot act while Stunned"), j.error)
    eq(#called("UseSpell"), 0, "no order")
    -- S066 #100: Karlach's Rage at a Fearful turn start: ordered (not refused), and the drop says why
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "FEARED", name = "Fearful" } }
    j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    tick(52) -- 3.1 s, no CastSpell, no step
    eq(j.status, "failed", "status")
    assert(j.error:find("never started it %(3 s, no cast, no step%): the caster is Fearful %(a fear status"), j.error)
    -- no such status: the plain message
    on = { { id = "BLEEDING", name = "Bleeding" } }
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    tick(52)
    eq(j.status, "failed", "plain"); assert(not j.error:find("the caster is"), j.error)
    -- S072 21:53:33: Lethargic (HASTE_LETHARGY, INCAPACITATED in the stats) let Karlach move and attack: ordered
    spell("HASTE_LETHARGY", { StatusType = "INCAPACITATED" })
    on = { { id = "HASTE_LETHARGY", name = "Lethargic" } }
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    local n0 = #called("UseSpell")
    j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    eq(j.status, "running", "lethargic ordered " .. tostring(j.error))
    eq(#called("UseSpell"), n0 + 1, "lethargic: ordered once")
    tick(52)
    BG3AI.Visibility = savedV
end)

test("disabling statuses carry the game's fix; the Sleeping refusal names it (S076 Gale, vis V18)", function()
    combat_setup(9)
    G._disabling = {}
    spell("Projectile_SR", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    -- the stats as the game resolves them (Shared Status_*.txt, `using` inherited)
    spell("SLEEPING_EYEBITE", { StatusType = "INCAPACITATED", RemoveEvents = { "OnDamage", "OnStatusApplied" },
        StatusGroups = { "SG_Unconscious", "SG_Condition", "SG_Sleeping", "SG_Sleeping_Magical" } })
    spell("HOLD_PERSON", { StatusType = "INCAPACITATED", RemoveEvents = { "OnTurn" },
        RemoveConditions = "SavingThrow(Ability.Wisdom, SourceSpellDC())" })
    spell("PRONE", { StatusType = "KNOCKED_DOWN", RemoveEvents = { "OnTurn" } })
    spell("FRIGHTENED", { StatusType = "BOOST", StatusGroups = { "SG_Frightened", "SG_Condition" },
        Boosts = "Disadvantage(AllAbilities);Disadvantage(AttackRoll);ActionResourceBlock(Movement)" })
    spell("HASTE_LETHARGY", { StatusType = "INCAPACITATED" })
    spell("BLEEDING", { StatusType = "BOOST", StatusGroups = { "SG_Condition" } })
    spell("DOWNED", { StatusType = "DOWNED", StatusGroups = "SG_Unconscious;SG_Incapacitated" })
    local savedV = BG3AI.Visibility
    local on = {}
    BG3AI.Visibility = setmetatable({ statuses = function() return on end }, { __index = savedV })
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "DOWNED", name = "Downed" } }
    eq(G.disabling(P1), nil, "bleeding + downed (its own line)")
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "SLEEPING_EYEBITE", name = "Sleeping", turns = 7 },
        { id = "HOLD_PERSON", name = "Held" }, { id = "PRONE", name = "Prone" },
        { id = "FRIGHTENED", name = "Frightened", turns = 2 }, { id = "HASTE_LETHARGY", name = "Lethargic" } }
    local d = G.disabling(P1)
    eq(#d, 5, "five")
    eq(d[1].name, "Sleeping"); eq(d[1].turns, 7); eq(d[1].effect, "cannot act")
    eq(d[1].fix, "an adjacent ally's Help (an action) ends it; any damage ends it", "sleep fix")
    eq(d[2].effect, "cannot act"); eq(d[2].fix, "it saves again each turn", "hold fix")
    eq(d[3].effect, "stands up at its turn start for half its movement")
    eq(d[3].fix, "an adjacent ally's Help (an action) ends it", "prone fix")
    eq(d[4].effect, "cannot move"); eq(d[4].fix, nil, "fear: no fix")
    eq(d[5].name, "Lethargic"); eq(d[5].effect, nil, "lethargic acts (S072)")
    -- S076 00:40:39: Gale's Scorching Ray while asleep: the refusal says how to wake her
    on = { { id = "SLEEPING_EYEBITE", name = "Sleeping" } }
    local j = job(op("cast", { char = P1, spell = "Projectile_SR", target = NPC }))
    eq(j.status, "failed", "asleep")
    eq(j.error, "cannot act while Sleeping (it takes the whole turn): end its turn; an adjacent ally's Help "
        .. "(an action) ends it; any damage ends it")
    BG3AI.Visibility = savedV
end)

test("cast in combat: a dropped order right after a hit on the caster is ordered once more (S066-S069 x6)", function()
    combat_setup(9)
    spell("Target_Swing", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    world[NPC].pos = { 1.5, 0, 0 }
    -- S069 r3 Karlach: an opportunity attack at her during the walk, then the swing never started; the bonus
    -- ordered 3 s later worked. A hit 1 s before the order: the first order is dropped, the second one starts.
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 0, "Attack", 0 })
    clock = clock + 1000
    local j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    eq(#called("UseSpell"), 1, "first order")
    tick(52) -- 3.1 s, no CastSpell, no step
    eq(j.status, "running", "ordered again " .. tostring(j.error))
    eq(#called("UseSpell"), 2, "second order"); eq(#called("PurgeOsirisQueue"), 1, "the dropped one purged first")
    A.dispatch("CastSpell", { P1, "Target_Swing", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Swing", "Target", "None", 0 })
    tick(10)
    eq(j.status, "done", "the second order cast")
    -- dropped twice: fails, naming the hit and the second order
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 2, "", 0 }) -- a Bleeding tick at the turn start
    clock = clock + 500
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "running", "second order")
    tick(52)
    eq(j.status, "failed", "dropped twice")
    assert(j.error:find("never started it %(3 s, no cast, no step%) twice: ordered again after a hit on the caster"
        .. " 0%.5 s before the first order"), j.error)
    eq(#called("UseSpell"), 4, "two orders, no third")
    -- no hit near the order (one 3 s before it): fails at 3 s as before, one order
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 2, "", 0 })
    clock = clock + 3000
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "failed", "no hit: plain failure"); assert(not j.error:find("twice"), j.error)
    eq(#called("UseSpell"), 5, "one order")
    -- a hit after the order (during the 3 s) counts too
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(10)
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 0, "Attack", 0 })
    tick(42)
    eq(j.status, "running", "hit after the order: ordered again")
    eq(#called("UseSpell"), 7, "second order")
    -- S070 Guiding Bolt: no hit, Prone ended at the turn start (shown statuses only, by name)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statusInfo = function(id)
        return ({ PRONE = { name = "Prone" } })[id] end }, { __index = savedV })
    clock = clock + 3000
    A.dispatch("StatusRemoved", { P1, "SOME_TECHNICAL_STATUS", "", 0 })
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "failed", "a technical status ending is no reason")
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    A.dispatch("StatusRemoved", { P1, "PRONE", "", 0 })
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "running", "Prone ended: ordered again")
    tick(52)
    assert(j.error:find("twice: ordered again after the caster's Prone ending"), j.error)
    BG3AI.Visibility = savedV
end)

test("select: done once the character holds ClientControl; a no-op order resyncs via another member (S069)", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 3, 0, 0 }, comps = { ClientControl = {} } }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("GetReservedUserID", P1)] = 1
    osiv[key("GetCurrentCharacter", 1)] = P1 -- the server's current character is already P1, the client controls P2
    local orders, follow = {}, nil
    Osi.MakePlayerActive = function(u)
        orders[#orders + 1] = u
        if follow then follow(u) end
    end
    -- S069 20:45:31: the old post-condition (GetCurrentCharacter) finished at once; now it waits for ClientControl
    local j = job(op("select", { char = P1 }))
    tick(5) -- 300 ms
    eq(j.status, "running", "P2 still holds ClientControl")
    eq(#orders, 1, "one order so far")
    follow = function(u) -- the game follows a real change of selection
        for _, w in ipairs({ P1, P2 }) do world[w].comps.ClientControl = (w == u) and {} or nil end
    end
    tick(6) -- 660 ms: resync, P2 first, then P1 on the next tick
    tick(2)
    eq(orders[2], P2, "via another member"); eq(orders[3], P1, "then the character")
    eq(j.status, "done", "selected " .. tostring(j.error)); eq(j.result.resynced, true, "resynced")
    -- already holding it: done on the first tick, one order
    orders = {}
    j = job(op("select", { char = P1 }))
    tick()
    eq(j.status, "done", "already selected"); eq(#orders, 1, "one order")
    -- the client never follows: failed at 2.5 s, naming who holds it (not a 3 s timeout)
    follow = nil
    world[P1].comps.ClientControl, world[P2].comps.ClientControl = nil, {}
    j = job(op("select", { char = P1 }))
    tick(43) -- 2.58 s
    eq(j.status, "failed", "kept"); assert(j.error:find("the game kept .* selected"), j.error)
    eq(j.result.holder, P2, "holder")
    -- nobody holds ClientControl (the host fallback): GetCurrentCharacter decides, as before
    world[P2].comps.ClientControl = nil
    j = job(op("select", { char = P1 }))
    tick()
    eq(j.status, "done", "host fallback")
    Osi.MakePlayerActive = nil
end)

test("select is not an action: a walk running for the member goes on (L8 helm ladder 'superseded by select')", function()
    osiv[key("GetReservedUserID", P1)] = 1
    osiv[key("GetCurrentCharacter", 1)] = P1
    Osi.MakePlayerActive = function() end
    local w = job(op("move_to", { char = P1, target = NPC }))
    eq(w.status, "running", "walk started")
    local s = job(op("select", { char = P1 }))
    tick()
    eq(s.status, "done", "selected " .. tostring(s.error))
    eq(w.status, "running", "the walk was not superseded: " .. tostring(w.error))
    eq(A.active[P1], w, "the walk stays the member's job")
    arrive(w, { 8.5, 0, 0 })
    eq(w.status, "done", "walk " .. tostring(w.error))
    Osi.MakePlayerActive = nil
end)

test("cast ui=true (ACT-8 hotbar route): 3 targets checked, no Osiris order, CastedSpell finishes, nothing charged", function()
    combat_setup(9)
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    -- without ui: still refused over 2
    local j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB } }))
    eq(j.status, "failed", "osiris >2"); assert(j.error:find("more than two"), j.error)
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    eq(j.status, "running", "ui started: " .. tostring(j.error))
    eq(#called("UseSpell"), 0, "no UseSpell: the bridge clicks the hotbar")
    tick(60) -- 3.6 s: past the combat no-start (3 s), the clicks are still going
    eq(j.status, "running", "ui no-start is longer")
    A.dispatch("CastSpell", { P1, "Target_Bless", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Bless" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(j.result.via, "hotbar", "via"); eq(j.result.targets_n, 3, "targets_n")
    eq(j.result.charged_by_guard, nil, "the game charged it (the action point stays as the game left it)")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 1, "no extra charge")
end)

test("cast ui=true: a CastSpellFailed before the clicks are done is held (L8 DD upcast picker), after them it fails", function()
    combat_setup(9)
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    -- the picker's CastSpellFailed mid-clicks, then the real cast: done
    local j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    A.dispatch("CastSpellFailed", { P1, "Target_Bless", "Target", "None", 0 })
    tick(5)
    eq(j.status, "running", "held while clicking: " .. tostring(j.error))
    A.dispatch("CastSpell", { P1, "Target_Bless", "Target", "None", 0 })
    RPC.ops["act.ui_clicked"]({ id = j.id })
    A.dispatch("CastedSpell", { P1, "Target_Bless" })
    tick(10)
    eq(j.status, "done", "the cast went off " .. tostring(j.error))
    -- held, clicks done, nothing starts: failed CAST_UI_REFUSED_MS later with the game's reason
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    A.dispatch("CastSpellFailed", { P1, "Target_Bless", "Target", "None", 0 })
    local r = RPC.ops["act.ui_clicked"]({ id = j.id })
    assert(r.held and r.held:find("the game refused the cast"), tostring(r.held))
    tick(10) -- 0.6 s
    eq(j.status, "running", "not yet")
    tick(20) -- 1.8 s
    eq(j.status, "failed", "refused"); assert(j.error:find("the game refused the cast"), j.error)
    -- after the clicks: at once
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    RPC.ops["act.ui_clicked"]({ id = j.id })
    A.dispatch("CastSpellFailed", { P1, "Target_Bless", "Target", "None", 0 })
    eq(j.status, "failed", "refused after the clicks")
end)

test("cast ui=true cancelled: after CastSpell it says began (no Osiris redo), before it a plain cancel; no purge", function()
    combat_setup(9)
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    local j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    eq(j.status, "running", "ui started: " .. tostring(j.error))
    local purged = #called("PurgeOsirisQueue")
    RPC.ops["job.cancel"]({ id = j.id })
    eq(j.status, "interrupted", "cancelled"); eq(j.result and j.result.began, nil, "nothing began")
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    A.dispatch("CastSpell", { P1, "Target_Bless", "Target", "None", 0 })
    RPC.ops["job.cancel"]({ id = j.id })
    eq(j.status, "interrupted", "late cancel"); eq(j.error, "cancelled", "why")
    eq(j.result and j.result.began, true, "began reported")
    eq(#called("PurgeOsirisQueue"), purged, "a ui cast is never purged on cancel")
end)

test("cast ui=true: every later target is checked; a repeat is a 2nd dart; no position / upcast; no-start backstop", function()
    combat_setup(9)
    spell("Projectile_MagicMissile", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 1 })
    world[GOB].pos = { 30, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC, NPC, GOB }, ui = true }))
    eq(j.status, "failed", "3rd out of range"); assert(j.error:find("target 3 is out of range"), j.error)
    world[GOB].pos = { 2.4, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC, NPC, GOB }, ui = true }))
    eq(j.status, "running", "darts: " .. tostring(j.error))
    tick(260) -- 15.6 s, nothing cast: the backstop
    eq(j.status, "failed", "backstop"); assert(j.error:find("never started it %(15 s"), j.error)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC }, pos = { 5, 0, 0 }, ui = true }))
    eq(j.status, "failed", "pos"); assert(j.error:find("not a position"), j.error)
    j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC }, upcast = 2, ui = true }))
    eq(j.status, "failed", "upcast"); assert(j.error:find("level picker"), j.error)
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

test("cast: a door/wall past arm's length is in sight when an eye ray reaches its body (S083 e284/e703/e720)", function()
    spell("Target_Knock", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local WALL = "cdcdcdcd-0000-0000-0000-00000000000b"
    local function hit(u, p) return { Position = p, Shape = { PhysicsObject = { Entity = { Uuid = { EntityUuid = u } } } } } end
    local rays, rayfn = {}, nil
    Ext.Level.RaycastClosest = function(a, b) rays[#rays + 1] = b; return rayfn(a, b) end
    BG3AI.Visibility = { rayCalibrated = function() return true end, EYE_H = 1.6, RAY_TYPE = 5, RAY_INCLUDE = 0xFFFFFFFF,
        RAY_EXCLUDE = 0x820200, bounds = function() return { height = 3.0, radius = 1.0 } end }
    local function knock(t)
        return job(op("cast", { char = P1, spell = "Target_Knock", target = t }))
    end
    local ok, err = pcall(function()
        -- e284 Threshold of Loss: 6.3 m, its position 5.9 m up at the door's top inside the frame. The ray to the
        -- position hits the frame (another object); one lower down the door hits the door itself.
        world[DOOR].pos = { 2.2, 5.9, 0 }
        osiv[key("HasLineOfSight", P1, DOOR)] = 0
        rayfn = function(_, b)
            if b[2] < 5.9 then return hit(DOOR, { 2.0, b[2], 0 }) end
            return hit(WALL, { 1.9, 5.5, 0 })
        end
        local j = knock(DOOR)
        eq(j.status, "running", "e284 status " .. tostring(j.error))
        -- the same door behind a wall: every ray stops at the wall (fail: no line of sight)
        rays, rayfn = {}, function() return hit(WALL, { 1.0, 3.0, 0 }) end
        j = knock(DOOR)
        eq(j.status, "failed", "walled status"); assert(j.error:find("line of sight"), j.error)
        assert(#rays >= 8, "every sample tried: " .. #rays)
        -- e703 sewer Wall (hidden destructible wall) 7 m away: the ray to its position ends on the wall itself
        world[DOOR].pos = { 7.0, 0, 0 }
        rayfn = function(_, b) return hit(DOOR, { 6.6, b[2], 0 }) end
        j = knock(DOOR)
        eq(j.status, "running", "e703 status " .. tostring(j.error))
        -- e720 No Entry - No Exit, caster ~4.5 m back: nothing hit on the way to its middle (no hit = clear)
        world[DOOR].pos = { 4.5, 0, 0 }
        rayfn = function(_, b) if b[2] > 1.0 then return nil end return hit(WALL, { 4.4, 0, 0 }) end
        j = knock(DOOR)
        eq(j.status, "running", "e720 status " .. tostring(j.error))
        -- a clear ray only below the position (outside the bounds) proves nothing: refused
        world[DOOR].pos = { 2.2, 5.9, 0 }
        rayfn = function(_, b) if b[2] < 5.9 then return nil end return hit(WALL, { 1.9, 5.5, 0 }) end
        j = knock(DOOR)
        eq(j.status, "failed", "air below status"); assert(j.error:find("line of sight"), j.error)
        -- a character keeps the engine's check, whatever the rays say
        world[GOB].pos = { 6.0, 0, 0 }
        osiv[key("HasLineOfSight", P1, GOB)] = 0
        rayfn = function(_, b) return hit(GOB, b) end
        j = knock(GOB)
        eq(j.status, "failed", "char status"); assert(j.error:find("line of sight"), j.error)
        -- rays uncalibrated (the filter flags or the API wrong): fail closed
        world[DOOR].pos = { 7.0, 0, 0 }
        rayfn = function(_, b) return hit(DOOR, b) end
        BG3AI.Visibility.rayCalibrated = function() return false end
        j = knock(DOOR)
        eq(j.status, "failed", "uncalibrated status"); assert(j.error:find("line of sight"), j.error)
        -- the ray API throwing: fail closed
        BG3AI.Visibility.rayCalibrated = function() return true end
        rayfn = function() error("bad ray") end
        j = knock(DOOR)
        eq(j.status, "failed", "ray error status"); assert(j.error:find("line of sight"), j.error)
    end)
    BG3AI.Visibility, Ext.Level.RaycastClosest = nil, nil
    if not ok then error(err, 0) end
end)

test("cast: a harmful area spell says where it bursts and who is in the blast before it is cast (S085 Fireball)", function()
    -- S085 05:03:37: Gale @(-22,27,748) on the Guildhall's upper floor, Fireball at (-27,17,736) ~10 m below; it hit
    -- the ledge and burst on e606 Lady Croup @(-26,27,745), a neutral Guild ally: killed.
    spell("Projectile_Fireball", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1",
        Level = 0, ExplodeRadius = "4", SpellFlags = { "IsHarmful" } })
    local function hit(p) return { Position = p, Shape = { PhysicsObject = { Entity = { Uuid = { EntityUuid = "" } } } } } end
    local rays, rayfn = {}, nil
    Ext.Level.RaycastClosest = function(a, b) rays[#rays + 1] = { a, b }; return rayfn(a, b) end
    local seen = { [P1] = true, [NPC] = true, [GOB] = true }
    BG3AI.Visibility = { rayCalibrated = function() return true end, EYE_H = 1.6, RAY_TYPE = 5, RAY_INCLUDE = 0xFFFFFFFF,
        RAY_EXCLUDE = 0x820200, context = function() return { is_party = { [P1] = true }, observers = {} } end,
        entityUuid = function(e) return e.uuid end,
        charactersNear = function(c, r)
            local out = {}
            for _, u in ipairs({ P1, NPC, GOB }) do
                local d = U.dist(world[u].pos, c)
                if d <= r then out[#out + 1] = { { uuid = u }, world[u].pos, d } end
            end
            return out
        end,
        characterVisible = function(_, u) return seen[u] end,
        isAlly = function() return false end,
        name = function(u) return ({ [NPC] = "Lady Croup", [GOB] = "Fishbait Arno", [P1] = "Gale" })[u] or "Unknown" end }
    osiv[key("IsEnemy", P1, GOB)] = 1
    local aim = { -27, 17, 736 }
    local function fireball(extra)
        local a = { char = P1, spell = "Projectile_Fireball", pos = aim }
        for k, v in pairs(extra or {}) do a[k] = v end
        return job(op("cast", a))
    end
    local ok, err = pcall(function()
        world[P1].pos = { -22, 27, 748 }
        world[NPC].pos = { -26, 27, 745 }
        world[GOB].pos = { -27, 17, 736.5 }
        -- the eye ray to the point stops on the ledge, ~6.7 m out on the caster's floor
        rayfn = function() return hit({ -24.5, 27, 742 }) end
        local j = fireball()
        eq(j.status, "failed", "S085 status")
        assert(j.error:find("the way to that point is blocked: it would burst at %(%-24%.5,27%.0,742%.0%)"), j.error)
        assert(j.error:find(NPC:gsub("%-", "%%-") .. " Lady Croup %(neutral, 3%.4 m%)"), j.error)
        assert(j.error:find("blast %(4 m%)"), j.error); assert(j.error:find("force=true to cast anyway"), j.error)
        assert(not j.error:find(GOB, 1, true), "the hostile below is out of that blast: " .. j.error)
        eq(#called("UseSpellAtPosition"), 0, "nothing cast")
        local r = rays[#rays]
        near(r[1][2], 28.6, "eye height"); near(r[2][2], 17.5, "the ray ends 0.5 m above the point")
        -- force=true: cast as asked
        j = fireball({ force = true })
        eq(j.status, "running", "forced " .. tostring(j.error)); eq(#called("UseSpellAtPosition"), 1, "cast")
        -- the way clear (no hit, or a hit at the point itself), only the hostile in the blast: cast, no word
        rayfn = function() return nil end
        j = fireball()
        eq(j.status, "running", "clear " .. tostring(j.error))
        rayfn = function(_, b) return hit({ b[1], b[2] - 0.4, b[3] + 0.2 }) end
        j = fireball()
        eq(j.status, "running", "hit at the point " .. tostring(j.error))
        -- the way clear, but the neutral stands in the ring at the point
        world[NPC].pos = { -25, 17, 737 }
        rayfn = function() return nil end
        j = fireball()
        eq(j.status, "failed", "neutral in the ring")
        assert(j.error:find("Projectile_Fireball at that point; in its blast %(4 m%) there: " .. NPC:gsub("%-", "%%-")
            .. " Lady Croup %(neutral, 2%.2 m%) and 1 hostile"), j.error)
        -- a neutral the party does not see is not named (fair)
        seen[NPC] = false
        j = fireball()
        eq(j.status, "running", "unseen neutral " .. tostring(j.error))
        seen[NPC] = true
        -- blocked far short with nobody there: says so (the spell would be wasted)
        world[NPC].pos = { 30, 0, 0 }
        rayfn = function(a, b) -- a wall half way there
            return hit({ a[1] + (b[1] - a[1]) * 0.5, a[2] + (b[2] - a[2]) * 0.5, a[3] + (b[3] - a[3]) * 0.5 })
        end
        world[P1].pos = { -14, 27, 752 } -- nobody (the caster neither) within 4 m of the ledge point
        aim = { -20, 17, 745 }
        j = fireball()
        eq(j.status, "failed", "wasted"); assert(j.error:find("in its blast %(4 m%) there: nobody"), j.error)
        -- blocked just before the point (within the radius), only the hostile in the blast: cast
        world[P1].pos = { -22, 27, 748 }
        aim = { -27, 17, 736 }
        rayfn = function() return hit({ -26.5, 18.5, 737 }) end
        j = fireball()
        eq(j.status, "running", "short but on target " .. tostring(j.error))
        -- at a creature: who stands beside it (the target itself is not listed)
        world[NPC].pos = { -26, 17, 736.5 }
        j = job(op("cast", { char = P1, spell = "Projectile_Fireball", target = GOB }))
        eq(j.status, "failed", "at a target"); assert(j.error:find("Projectile_Fireball at that target; in its blast"), j.error)
        assert(j.error:find("Lady Croup"), j.error); assert(not j.error:find("hostile"), j.error)
        -- a harmless area spell is not checked
        spell("Projectile_Blessing", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1",
            Level = 0, ExplodeRadius = "4" })
        j = job(op("cast", { char = P1, spell = "Projectile_Blessing", pos = aim }))
        eq(j.status, "running", "harmless " .. tostring(j.error))
        -- no ray API: the ring at the point is still checked, the path is not
        BG3AI.Visibility.rayCalibrated = function() return false end
        j = fireball()
        eq(j.status, "failed", "uncalibrated ring"); assert(j.error:find("at that point"), j.error)
    end)
    BG3AI.Visibility, Ext.Level.RaycastClosest = nil, nil
    if not ok then error(err, 0) end
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

test("jump: Astral Gravity triples it, by name or by its JumpMaxDistanceMultiplier boost (S087 11.4 m refused at 9.5)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 19
    local savedV = BG3AI.Visibility
    local on = {}
    BG3AI.Visibility = setmetatable({ statuses = function() return on end }, { __index = savedV })
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 11.4, 0, 0 } }))
        eq(j.status, "failed", "11.4 m at 9.5"); assert(j.error:find("can jump 9.5 m"), j.error)
        on = { { id = "UNKNOWN_ASTRAL_ID", name = "Astral Gravity" } }
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 11.4, 0, 0 } }))
        eq(j.status, "running", "astral by name " .. tostring(j.error))
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 29.5, 0, 0 } }))
        eq(j.status, "failed", "past 28.5 m"); assert(j.error:find("can jump 28.5 m"), j.error)
        stats.SOME_GRAVITY = { Boosts = "JumpMaxDistanceMultiplier(3);IgnoreFallDamage()" }
        on = { { id = "SOME_GRAVITY", name = "Something" } }
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 20, 0, 0 } }))
        eq(j.status, "running", "by boost " .. tostring(j.error))
        local m, nf = BG3AI.Guard.jump_status(P1)
        eq(m, 3, "mult"); eq(nf, true, "no fall damage")
    end)
    BG3AI.Visibility = savedV
    if not ok then error(err, 0) end
end)

test("jump: a drop extends the reach (high ground, S040 the game's own jumps 18-28 m out over 18-23 m drops)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 17
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, -18.0 } end
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 18, -18, 0 } }))
        eq(j.status, "running", "18 m out, 18 m down " .. tostring(j.error))
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 18, 0, 0 } }))
        eq(j.status, "failed", "18 m flat"); assert(j.error:find("too far to jump"), j.error)
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 30, -18, 0 } }))
        eq(j.status, "failed", "30 m out"); assert(j.error:find("18.0 m down"), j.error)
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("jump: a landing far above the jumper is refused (S039 21:53 jump_to 20 m up froze the game)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 17
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 10.0, 2.0 } end
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 3, 10, 0 }, force = true }))
        eq(j.status, "failed", "10 m up"); assert(j.error:find("above you"), j.error)
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 3, 2, 0 } }))
        eq(j.status, "running", "2 m up ok " .. tostring(j.error))
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("jump: a jump that went nowhere fails, a short landing says how far off (S039 jump_to done x3, moved 0.6 m)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, -8.0 } end -- a floor at the landing's height (Guard.jump_floor_ok)
    local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, -8, 0 } }))
    Ext.Level.GetHeightsAt = heights
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 0.5, 0, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    tick(40)
    eq(j.status, "failed", "went nowhere"); assert(j.error:find("went nowhere: moved 0.5m"), j.error)
    -- landed 4 m short: done, with the distance
    world[P1].pos = { 0, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 8, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 4, 0, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    tick(40)
    eq(j.status, "done", "short"); eq(j.result.landed_m_from_target, 4, "off")
    -- still in the air at the settle: waits for the landing, then a clean done
    world[P1].pos = { 0, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    for i = 1, 30 do world[P1].pos = { i * 0.2, 0, 0 }; tick() end -- 1.8 s, flying
    eq(j.status, "running", "airborne")
    tick(5)
    eq(j.status, "done", "landed"); eq(j.result.landed_m_from_target, nil, "on target (6 m)")
    world[P1].pos = { 0, 0, 0 }
end)

test("known_spell: a missing Ranged Attack names the missing ranged weapon (S031 17:04 Gale)", function()
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Target_MainHandAttack" } } } }
    refused(function() G.known_spell(P1, "Projectile_MainHandAttack") end, "no ranged weapon equipped")
    refused(function() G.known_spell(P1, "Target_Fireball_X") end, "does not have that action")
end)

-- bg3se's SpellMetaId: OriginatorPrototype, SourceType, Source, ProgressionSource; any other name raises
-- "Property does not exist" (LuaCppObjectProxy.inl LightObjectProxyMetatable::Index).
local function meta_id(orig)
    local fields = { OriginatorPrototype = orig, SourceType = "Progression" }
    return setmetatable({}, { __index = function(_, k)
        if fields[k] ~= nil then return fields[k] end
        error("Property does not exist: SpellMetaId::" .. tostring(k) .. " - property does not exist")
    end })
end

test("known_spell: an unprepared class spell is refused (A4: S065 Claude's Bless off the hotbar)", function()
    local function book(id, kind) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = kind } end
    world[P1].comps.SpellBook = { Spells = { book("Target_Bless", "RequiresPreparation"),
        book("Target_CureWounds", "RequiresPreparation"), book("Target_Sanctuary", "AlwaysPrepared"),
        book("Target_MainHandAttack", "Unknown") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds"), meta_id("Projectile_Jump") } }
    refused(function() G.known_spell(P1, "Target_Bless") end, "that spell is not prepared")
    eq(G.known_spell(P1, "Target_CureWounds").Id.Prototype, "Target_CureWounds", "prepared")
    eq(G.known_spell(P1, "Target_Sanctuary").Id.Prototype, "Target_Sanctuary", "always prepared, not listed")
    eq(G.known_spell(P1, "Target_MainHandAttack").Id.Prototype, "Target_MainHandAttack", "an action")
    -- an unreadable prepared list: nothing counts as prepared
    world[P1].comps.SpellBookPrepares = setmetatable({}, { __index = function() error("bad component") end })
    refused(function() G.known_spell(P1, "Target_CureWounds") end, "that spell is not prepared")
    eq(G.known_spell(P1, "Target_Sanctuary").Id.Prototype, "Target_Sanctuary", "always prepared still casts")
    world[P1].comps.SpellBookPrepares = nil
    world[P1].comps.SpellBook = nil
end)

test("cast: the engine's CastSpellFailed says why (S024-S041 'the action failed' x12)", function()
    osiv[key("HasLineOfSight", P1, GOB)] = 1
    eq(A.cast_failed_why(P1, nil), "the game refused the cast (no reason given)", "no target")
    assert(A.cast_failed_why(P1, GOB):find("path is blocked", 1, true))
    osiv[key("HasLineOfSight", P1, GOB)] = 0
    assert(A.cast_failed_why(P1, GOB):find("no line of sight to the target now", 1, true))
    osiv[key("IsDead", GOB)] = 1
    assert(A.cast_failed_why(P1, GOB):find("already dead", 1, true))
    stats.Teleportation_Revivify = { SpellType = "Teleportation", OriginTargetConditions = "Dead()", TargetRadius = "9" }
    local w = A.cast_failed_why(P1, GOB, "Teleportation_Revivify")
    assert(w:find("two-step cast", 1, true) and w:find("Scroll of Revivify", 1, true), w)
    assert(w:find("not a party member", 1, true), w) -- GOB has no PLAYABLE tag
    osiv[key("IsDead", GOB)] = 0
end)

test("cast: a refused melee swing says how far it swung from and our reach (ACT-3, S065 x9)", function()
    osiv[key("HasLineOfSight", P1, GOB)] = 1
    stats.Target_MainHandAttack = { SpellType = "Target", TargetRadius = "MeleeMainWeaponRange" }
    world[GOB].pos = { 2.1, 0, 0 }
    local w = A.cast_failed_why(P1, GOB, "Target_MainHandAttack")
    assert(w:find("no reason given", 1, true) and w:find("melee swing from 2.1 m centre to centre", 1, true), w)
    assert(w:find("our reach 2.5 m: range 1.5 m", 1, true) and not w:find("path is blocked", 1, true), w)
    stats.Projectile_Bolt = { SpellType = "Projectile", TargetRadius = "18" }
    assert(A.cast_failed_why(P1, GOB, "Projectile_Bolt"):find("path is blocked", 1, true)) -- ranged: unchanged
end)

test("cast: Revivify is two-step: the body, then the ground beside it (S057/S058 0 of 14 casts)", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Dead() and Tagged('PLAYABLE')",
        TargetConditions = "not Character()", TargetRadius = "9", UseCosts = "ActionPoint:1", Level = 0 })
    stats.Projectile_Bolt = stats.Projectile_Bolt or { SpellType = "Projectile" }
    eq(A.two_step_spell("Teleportation_Revivify"), true, "two-step")
    eq(A.two_step_spell("Projectile_Bolt"), false, "one-step")
    eq(A.two_step_spell("No_Such_Spell"), false, "unknown")
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local orig = Osi.UseSpell
    Osi.UseSpell = function(c, sp, ...)
        tasks[#tasks + 1] = { Spell = { OriginatorPrototype = sp } }
        return orig(c, sp, ...)
    end
    osiv[key("IsDead", NPC)] = 1
    world[NPC].pos = { 4, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Teleportation_Revivify", target = NPC }))
    Osi.UseSpell = orig
    eq(j.status, "running", "status " .. tostring(j.error))
    local p = tasks[1] and tasks[1].TargetPos2
    assert(p, "TargetPos2 set on the queued task")
    eq(p[1], 4 - A.PLACE_M, "spot beside the body, toward the caster"); eq(p[3], 0, "z")
    eq(A.last_place_why, nil, "no placement error")
    osiv[key("IsDead", NPC)] = 0
    A.dispatch("CastedSpell", { P1, "Teleportation_Revivify" })
    tick(10)
    eq(j.status, "done", "status"); eq(j.result.revived, true, "revived")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: two-step spell without a queued task names the reason", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = {} } }
    local ok, why = A.set_task_pos2(P1, "Teleportation_Revivify", { 1, 0, 1 })
    eq(ok, nil, "not set"); assert(why:find("no queued spell task", 1, true), why)
    world[P1].comps.ServerCharacter = nil
end)

test("cast (ACT-9): with the TargetPos2 write off (default), a two-step spell is refused before any order", function()
    eq(A.TASK_POS2, false, "off by default")
    spell("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Dead() and Tagged('PLAYABLE')",
        TargetConditions = "not Character()", TargetRadius = "9", UseCosts = "ActionPoint:1", Level = 0 })
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = { { Spell = { OriginatorPrototype = "Teleportation_Revivify" } } } } }
    osiv[key("IsDead", NPC)] = 1
    world[NPC].pos = { 4, 0, 0 }
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_Revivify", target = NPC }, "Scroll of Revivify")
    eq(#called("UseSpell"), 0, "no UseSpell")
    local ok, why = A.set_task_pos2(P1, "Teleportation_Revivify", { 1, 0, 1 })
    eq(ok, nil, "no write"); assert(why:find("ACT-9", 1, true), why)
    eq(world[P1].comps.ServerCharacter.OsirisController.Tasks[1].TargetPos2, nil, "the task is untouched")
    spell("Teleportation_DimensionDoor", { SpellType = "Teleportation", TargetRadius = "2", AreaRadius = "50",
        OriginTargetConditions = "Ally()", TargetConditions = "CanStand('')", TeleportSelf = "No",
        UseCosts = "ActionPoint:1", Level = 0 })
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 3, 0, 3 } }, "two-step teleport")
    eq(#called("UseSpell"), 0, "no UseSpell"); eq(#called("UseSpellAtPosition"), 0, "no UseSpellAtPosition")
    world[P1].comps.ServerCharacter = nil
end)

local DD = { SpellType = "Teleportation", TargetRadius = "2", AreaRadius = "50",
    TargetConditions = "CanStand('') and not Character()",
    OriginTargetConditions = "Ally() and not Grounded() and (Tagged('WEIGHT_MEDIUM') or Tagged('WEIGHT_SMALL'))",
    TeleportSelf = "No", UseCosts = "ActionPoint:1", Level = 0 }

test("cast: Dimension Door alone: the caster goes, the point is the landing (S059 08:59:47 'range 2.0 m')", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_DimensionDoor", DD)
    eq(A.revive_spell("Teleportation_DimensionDoor"), false, "not a revive")
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local orig = Osi.UseSpell
    Osi.UseSpell = function(c, sp, ...)
        tasks[#tasks + 1] = { Spell = { OriginatorPrototype = sp } }
        return orig(c, sp, ...)
    end
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 13.0 } end
    local j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 13.1, 5 } }))
    Osi.UseSpell, Ext.Level.GetHeightsAt = orig, heights
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("UseSpellAtPosition"), 0, "not cast at a bare position")
    local c = called("UseSpell")
    eq(c[1][2], P1, "caster"); eq(c[1][4], P1, "first pick: the caster")
    local p = tasks[1] and tasks[1].TargetPos2
    assert(p, "TargetPos2 = the landing")
    eq(p[1], 0); eq(p[2], 13.1); eq(p[3], 5)
    A.dispatch("CastedSpell", { P1, "Teleportation_DimensionDoor" })
    world[P1].pos = { 0, 13.0, 5 }
    tick(10)
    eq(j.status, "done", "status"); eq(j.result.teleported, true, "teleported")
    eq(j.result.revived, nil, "no revive flag"); near(j.result.landed_m_from_target, 0.1, "landed")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: Dimension Door with an ally: the ally is the first pick, the landing the second (S059 08:57:42)", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_DimensionDoor", DD)
    local tasks = {}
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    world[GOB].pos = { 1.5, 0, 0 }
    local orig = Osi.UseSpell
    Osi.UseSpell = function(c, sp, ...)
        tasks[#tasks + 1] = { Spell = { OriginatorPrototype = sp } }
        return orig(c, sp, ...)
    end
    local j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB }, pos = { 8, 0, 8 } }))
    Osi.UseSpell = orig
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(called("UseSpell")[1][4], GOB, "first pick: the ally")
    local p = tasks[1].TargetPos2
    eq(p[1], 8, "landing x, not the Revivify spot beside the ally"); eq(p[3], 8, "landing z")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: Dimension Door landing checks: range from AreaRadius, a landing in the air names the floor", function()
    A.TASK_POS2 = true -- the ACT-9 flag: these probe the write itself
    spell("Teleportation_DimensionDoor", DD)
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 0, 55 } },
        "the landing is out of range (55.0 m, range 50.0 m)")
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 13.0 } end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 7, 5 } }, "nearest floor there is at y=")
    Ext.Level.GetHeightsAt = heights
    fails({ char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB, NPC }, pos = { 0, 0, 5 } }, "one who goes")
end)

test("cast ui=true (ACT-9 player route): Dimension Door alone/with an ally, no order and no task write", function()
    eq(A.TASK_POS2, false, "the write stays off")
    spell("Teleportation_DimensionDoor", DD)
    local tasks = { { Spell = { OriginatorPrototype = "Teleportation_DimensionDoor" } } }
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0 } end
    local j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 0, 10 }, ui = true }))
    eq(j.status, "running", "alone: " .. tostring(j.error))
    eq(#called("UseSpell"), 0, "no UseSpell: the bridge clicks"); eq(#called("UseSpellAtPosition"), 0, "no position cast")
    eq(tasks[1].TargetPos2, nil, "nothing written")
    A.dispatch("CastSpell", { P1, "Teleportation_DimensionDoor", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Teleportation_DimensionDoor" })
    world[P1].pos = { 0, 0, 9.8 }
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error)); eq(j.result.via, "hotbar", "via")
    eq(j.result.teleported, true, "teleported"); near(j.result.landed_m_from_target, 0.2, "landed")
    eq(j.result.landing[3], 10, "landing reported")
    world[P1].pos = { 0, 0, 0 }
    world[GOB].pos = { 1.5, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB }, pos = { 0, 0, 10 }, ui = true }))
    eq(j.status, "running", "with an ally: " .. tostring(j.error))
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_DimensionDoor", pos = { 0, 0, 55 }, ui = true }, "the landing is out of range")
    fails({ char = P1, spell = "Teleportation_DimensionDoor", targets = { GOB, NPC }, pos = { 0, 0, 5 }, ui = true }, "one who goes")
    Ext.Level.GetHeightsAt = heights
    world[P1].comps.ServerCharacter = nil
end)

test("cast ui=true (ACT-9 player route): Revivify clicks one body; the result says revived; no write", function()
    spell("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Dead() and Tagged('PLAYABLE')",
        TargetConditions = "not Character()", TargetRadius = "9", UseCosts = "ActionPoint:1", Level = 0 })
    local tasks = { { Spell = { OriginatorPrototype = "Teleportation_Revivify" } } }
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    osiv[key("IsDead", NPC)] = 1
    world[NPC].pos = { 4, 0, 0 }
    local function fails(args, pat)
        local ok, r = pcall(op, "cast", args)
        local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        assert(msg:find(pat, 1, true), msg)
    end
    fails({ char = P1, spell = "Teleportation_Revivify", target = NPC, pos = { 3, 0, 0 }, ui = true }, "not a position")
    fails({ char = P1, spell = "Teleportation_Revivify", targets = { NPC, GOB }, ui = true }, "one body")
    local j = job(op("cast", { char = P1, spell = "Teleportation_Revivify", target = NPC, ui = true }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("UseSpell"), 0, "no UseSpell"); eq(tasks[1].TargetPos2, nil, "nothing written")
    osiv[key("IsDead", NPC)] = 0
    A.dispatch("CastedSpell", { P1, "Teleportation_Revivify" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error)); eq(j.result.revived, true, "revived")
    eq(j.result.via, "hotbar", "via"); eq(j.result.teleported, nil, "not a teleport")
    -- without ui, still ACT-9's refusal
    osiv[key("IsDead", NPC)] = 1
    fails({ char = P1, spell = "Teleportation_Revivify", target = NPC }, "Scroll of Revivify")
    world[P1].comps.ServerCharacter = nil
end)

test("cast: a refused Dimension Door says the landing must be in sight, not 'the shot's path is blocked'", function()
    stats.Teleportation_DimensionDoor = DD
    A.last_place_why = nil
    local w = A.cast_failed_why(P1, P1, "Teleportation_DimensionDoor")
    assert(w:find("two-step teleport", 1, true) and w:find("SEE", 1, true), w)
    assert(not w:find("path is blocked", 1, true), w)
    assert(not A.cast_failed_why(P1, GOB, "Teleportation_DimensionDoor"):find("Revivify", 1, true))
end)

test("set_party: unchain/chain already in that state is done, not FAILED (S062 10:00:20 x3)", function()
    local j = job(op("unchain", { char = P1 }))
    tick(1)
    eq(j.status, "done", "unchain status " .. tostring(j.error)); eq(j.result.already, true, "already")
    eq(#called("DetachFromPartyGroup"), 0, "no order sent")
    osiv[key("InSamePartyGroup", P1, GOB)] = 1
    party_rows = { P1, GOB }
    local c = job(op("chain", { char = P1, leader = GOB }))
    tick(1)
    eq(c.status, "done", "chain status " .. tostring(c.error)); eq(c.result.already, true, "already")
    eq(#called("AttachToPartyGroup"), 0, "no order sent")
end)

test("jump: a landing far below the aim is a FALL; a death says DIED, not done (S038 20:44:25)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, -1.0 } end
    local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 6, -47, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    osiv[key("IsDead", P1)] = 1
    tick(60)
    eq(j.status, "failed", "dead"); assert(j.error:find("FELL 47.0m below the landing", 1, true), j.error)
    assert(j.error:find("DIED", 1, true), j.error)
    -- alive after a fall: done, with fell_m
    osiv[key("IsDead", P1)] = 0
    world[P1].pos = { 0, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 6, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
    world[P1].pos = { 6, -6, 0 }
    A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
    tick(60)
    Ext.Level.GetHeightsAt = heights
    eq(j.status, "done", "alive " .. tostring(j.error)); eq(j.result.fell_m, 6, "fell_m")
    world[P1].pos = { 0, 0, 0 }
end)

test("jump: never at the void or into the air, even forced (S038 20:48 / S039 21:53: two OOM freezes)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local heights = Ext.Level.GetHeightsAt
    local ok, err = pcall(function()
        Ext.Level.GetHeightsAt = function() return {} end
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, 20, 0 }, force = true }))
        eq(j.status, "failed", "void"); assert(j.error:find("no ground at all"), j.error)
        Ext.Level.GetHeightsAt = function() return { -155.0 } end
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, -12, 0 } }))
        eq(j.status, "failed", "air over a chasm"); assert(j.error:find("is in the air"), j.error)
        Ext.Level.GetHeightsAt = function() return { 0.0 } end
        j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, 0, 0 } }))
        eq(j.status, "running", "a floor: goes " .. tostring(j.error))
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("jump: the cast runs under the nav in-flight marker, cleared at the end (a crash inside kills the level)", function()
    stats.Projectile_Jump = { TargetRadius = "4.5", SpellType = "Projectile", UseCosts = "", Level = 0 }
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_Jump" } } } }
    osiv[key("GetAbility", P1, "Strength")] = 16
    world[P1].pos = { 0, 0, 0 }
    local marks = {}
    BG3AI.Nav.inflight = function(o) marks[#marks + 1] = o or "clear" end
    BG3AI.Nav.inflightDone = function(o) marks[#marks + 1] = "clear " .. tostring(o) end -- NAV-7: only its own
    local ok, err = pcall(function()
        local j = job(op("cast", { char = P1, spell = "Projectile_Jump", pos = { 4, 0, 0 } }))
        eq(marks[1], "jump", "marked at the order")
        A.dispatch("CastSpell", { P1, "Projectile_Jump", "Projectile", "None", 0 })
        world[P1].pos = { 4, 0, 0 }
        A.dispatch("CastedSpell", { P1, "Projectile_Jump" })
        tick(60)
        eq(j.status, "done", "landed " .. tostring(j.error))
        eq(marks[#marks], "clear jump", "cleared at the end")
    end)
    BG3AI.Nav.inflight, BG3AI.Nav.inflightDone = nil, nil
    world[P1].pos = { 0, 0, 0 }
    if not ok then error(err, 0) end
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

test("act.watch: Chasm underfoot is no surface interrupt (S040: the queue cancelled two jumps over a gap)", function()
    local savedNav = BG3AI.Nav
    local under = "Chasm"
    BG3AI.Nav = setmetatable({ surfaceAt = function() return under end }, { __index = savedNav })
    local ok, err = pcall(function()
        local w = RPC.ops["act.watch"]({})
        eq(w.party[1].surface, nil, "Chasm not reported")
        under = "Fire"
        w = RPC.ops["act.watch"]({})
        eq(w.party[1].surface, "Fire", "fire still reported")
    end)
    BG3AI.Nav = savedNav
    if not ok then error(err, 0) end
end)

test("wedged hint: a landing only away from the goal says don't jump (naut2 run 3: back down the ladder just climbed)", function()
    local savedNav = BG3AI.Nav
    local got
    BG3AI.Nav = setmetatable({ wedged = function(_, _, goal)
        got = goal
        if goal then return { wedged = true, away = "Lae'zel", away_dy = -2.9 } end
        return { wedged = true, to = { -42.3, 4.5, -363.3 }, near = "Lae'zel", dist_m = 0.6 }
    end }, { __index = savedNav })
    local ok, err = pcall(function()
        local h = G.wedged_hint(P1, { -41, 7.7, -357 })
        eq(got[2], 7.7, "the goal reaches N.wedged")
        assert(h:find("next to Lae'zel%) is 2.9m below, away from the goal: don't jump there"), h)
        assert(not h:find("jump_to"), h)
        assert(G.wedged_hint(P1):find("jump_to %(%-42.3,4.5,%-363.3%) next to Lae'zel"), "no goal: as before")
    end)
    BG3AI.Nav = savedNav
    if not ok then error(err, 0) end
end)

test("act.watch: a climber on a ladder reads climbing (the queue's next step waits for it, naut2 helm)", function()
    eq(RPC.ops["act.watch"]({}).party[1].climbing, nil, "standing")
    osiv[key("HasActiveStatus", P1, "CLIMBING")] = 1
    eq(RPC.ops["act.watch"]({}).party[1].climbing, true, "climbing")
    osiv[key("HasActiveStatus", P1, "CLIMBING")] = nil
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

test("act.target_state: a combatant out of sight says if it is gone (S033 Umbral Tremors)", function()
    osiv[key("CanSee", P1, NPC)] = nil
    osiv[key("IsInCombat", P1)] = 1
    osiv[key("IsInCombat", NPC)] = 1
    local r = RPC.ops["act.target_state"]({ uuid = NPC })
    eq(r.visible, false, "hidden")
    eq(r.gone, nil, "only out of sight: still in the fight")
    osiv[key("IsInCombat", NPC)] = 0
    eq(RPC.ops["act.target_state"]({ uuid = NPC }).gone, "left_fight", "left the turn order")
    osiv[key("IsOnStage", NPC)] = 0
    eq(RPC.ops["act.target_state"]({ uuid = NPC }).gone, "off_stage", "dissipated / despawned")
    osiv[key("IsDead", NPC)] = 1
    eq(RPC.ops["act.target_state"]({ uuid = NPC }).gone, "dead", "died out of sight")
    osiv[key("IsDead", NPC)] = 0
    osiv[key("IsOnStage", NPC)] = 1
    osiv[key("IsInCombat", P1)] = 0
    eq(RPC.ops["act.target_state"]({ uuid = NPC }).gone, nil, "no fight: nothing to leave")
    osiv[key("CanSee", P1, NPC)] = 1
    r = RPC.ops["act.target_state"]({ uuid = NPC })
    eq(r.visible, true, "in sight")
    eq(r.gone, nil, "in sight: never gone")
    osiv[key("CanSee", P1, DOOR)] = nil
    eq(RPC.ops["act.target_state"]({ uuid = DOOR }).gone, nil, "items: no gone")
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
    -- no pending ends from an earlier test (a leftover request now stays pending END_DROP_MS)
    G.end_requested, G.end_alone, G.turn_over = {}, {}, {}
    G.turn_start, G.foe_start = {}, {}
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

test("end_turn: an EndTurn the game ignores fails after END_IGNORED_MS (S044/S045 summon, 10 s TIMEOUT x9)", function()
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    osiv[key("IsInCombat", P1)] = 1
    local j = job(op("end_turn", { char = P1 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    clock = clock + A.END_IGNORED_MS tick()
    eq(j.status, "failed", "status"); eq(j.error, A.END_IGNORED, "error")
end)

test("stuck shared turn (S052-S055): a dropped group end is forgotten once nobody else acts, and re-sent", function()
    local restore = group_setup()
    osiv[key("IsInCombat", P1)] = 1
    osiv[key("IsInCombat", P2)] = 1
    -- P1 ends while P2 still acts: requested (the guard remembers it)
    local j = job(op("end_turn", { char = P1 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "requested " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    eq(G.is_my_turn(P1), false, "pending while P2 acts")
    -- P2 ends; the game dropped P1's request (P1 still active, no RequestedEndTurn)
    local j2 = job(op("end_turn", { char = P2 }))
    world[P2].comps.TurnBased.RequestedEndTurn = true
    A.dispatch("TurnEnded", { P2 })
    eq(j2.status, "done", "last member " .. tostring(j2.error))
    local ends = called("EndTurn")
    eq(ends[#ends][2], P1, "P1's EndTurn sent again by the last member")
    -- nobody else acting: still ended while the game may be applying the group's requests (S084), then forgotten
    eq(G.is_my_turn(P1), false, "not forgotten at once")
    clock = clock + G.END_DROP_MS - 100
    eq(G.is_my_turn(P1), false, "still pending just short of END_DROP_MS")
    clock = clock + 200
    -- it lasted: the guard no longer blocks P1 (it can act or end its turn)
    eq(G.is_my_turn(P1), true, "the game's open turn is P1's again")
    eq(G.turn_requested_end(P1), false, "not 'already ended'")
    restore()
end)

test("S084 shared turn end: a member ended earlier stays ended while the game applies the group's requests", function()
    local restore = group_setup()
    -- P1 ended first (requested, the game hasn't set RequestedEndTurn: live S002)
    local j = job(op("end_turn", { char = P1 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "requested " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    -- P2, the last member: its EndTurn starts the group's end (ignored by Lua at 3 s; the bridge presses the HUD)
    local j2 = job(op("end_turn", { char = P2 }))
    clock = clock + A.END_IGNORED_MS tick()
    eq(j2.status, "failed", "ignored " .. tostring(j2.error))
    -- the game applies P2's request first; P1's comes ~2 s later (one by one)
    world[P2].comps.TurnBased.RequestedEndTurn = true
    for _, dt in ipairs({ 0, 1000, 2500, 6000 }) do
        clock = clock + dt
        eq(G.end_pending(P1), true, "P1 still counts as ended +" .. dt .. " ms (was: forgotten, shown acting)")
        eq(G.is_my_turn(P1), false, "P1 may not act +" .. dt .. " ms")
    end
    world[P1].comps.TurnBased.RequestedEndTurn = true
    A.dispatch("TurnEnded", { P2 })
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), false, "group over")
    -- the next round: a fresh request in the same alone shape is timed from its own start, not the old one
    A.dispatch("TurnStarted", { "S_Player_" .. P1 })
    world[P1].comps.TurnBased.RequestedEndTurn = false
    clock = clock + G.END_DROP_MS * 2
    G.end_requested[G.guid(P1)] = G.now_ms()
    eq(G.end_pending(P1), true, "a new request is not forgotten at once")
    restore()
end)

test("S087 group end: the last member's end the game showed stays ended while the game applies the group (18 s)", function()
    local restore = group_setup()
    G.end_confirmed = {}
    local ok, err = pcall(function()
    -- 05:57:40 High Hall Courtyard: Wyll (P2 here) ended earlier (requested); Claude (P1), the group's last, ends
    local j = job(op("end_turn", { char = P2 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "P2 requested " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    local j1 = job(op("end_turn", { char = P1 }))
    world[P1].comps.TurnBased.RequestedEndTurn = true -- the game shows Claude's end
    tick()
    eq(j1.status, "done", "P1 " .. tostring(j1.error)); eq(j1.result.requested, nil, "ended by the game's flags")
    -- then, applying the group's ends one by one, it shows both turns open again (active, no RequestedEndTurn) until
    -- TurnEnded for all 18 s later
    world[P1].comps.TurnBased.RequestedEndTurn = false
    for _, dt in ipairs({ 0, 2000, 16000 }) do
        clock = clock + dt
        eq(G.is_my_turn(P1), false, "Claude may not act +" .. dt .. " ms (was: shown acting, 'e4 Claude must act first')")
        eq(G.is_my_turn(P2), false, "Wyll may not act +" .. dt .. " ms")
    end
    A.dispatch("TurnEnded", { P1 })
    A.dispatch("TurnEnded", { P2 })
    eq(G.end_requested[G.guid(P1)], nil, "TurnEnded clears the pending end")
    -- next round: TurnStarted, and the turn is P1's again
    A.dispatch("TurnStarted", { P1 })
    eq(G.is_my_turn(P1), true, "P1's next turn is open")
    end)
    restore()
    if not ok then error(err, 0) end
end)

test("S087 stunned: a party member's INCAPACITATED status makes its turn pass (Guard.turn_passes)", function()
    local st = {}
    BG3AI.Visibility = { statuses = function() return st end }
    stats.STUNNED = { StatusType = "INCAPACITATED" }
    stats.FEAR = { StatusType = "FEAR" }
    local ok, err = pcall(function()
        st = { { id = "STUNNED", name = "Stunned" } }
        eq(G.turn_passes(P1), "Stunned", "stunned")
        st = { { id = "FEAR", name = "Frightened" } }
        eq(G.turn_passes(P1), nil, "frightened still acts (moves away)")
        st = {}
        eq(G.turn_passes(P1), nil, "nothing")
    end)
    BG3AI.Visibility = nil
    if not ok then error(err, 0) end
end)

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

test("end_turn in a group whose EndTurn stays pending: done {requested} after END_REQUEST_AFTER_MS, counts as ended", function()
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    tick(math.floor(A.END_REQUEST_AFTER_MS / 60) - 1) -- just short of it: still waiting for RequestedEndTurn
    eq(j.status, "running", "status " .. tostring(j.error))
    tick(3) -- past A.END_REQUEST_AFTER_MS; RequestedEndTurn never set (live S002)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.requested, true, "requested")
    eq(j.result.still_to_act and j.result.still_to_act[1], P2, "Lae'zel still to act")
    eq(G.is_my_turn(P1), false, "a pending member may not act again")
    eq(G.turn_requested_end(P1), true, "counts as ended")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "second end_turn refused at once")
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), false, "TurnEnded: the turn is over (until the next TurnStarted; live window 2026-10-16)")
    A.dispatch("TurnStarted", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), true, "the next round's TurnStarted: P1 acts again")
    restore()
end)

test("latency: a shared-group end is reported within 0.5 s (was 1.5 s; naut 12 x 1.6 s)", function()
    assert(A.END_REQUEST_AFTER_MS <= 500, "END_REQUEST_AFTER_MS " .. A.END_REQUEST_AFTER_MS)
    assert(A.END_REQUEST_AFTER_MS < A.END_IGNORED_MS, "a request is reported before the ignored-end fallback")
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    tick(9) -- 0.54 s
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.requested, true, "requested")
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

test("turn over (live window 2026-10-16): after TurnEnded the stale open-turn flags don't let a move through", function()
    local restore = group_setup()
    -- both members ended; the game fired TurnEnded for the group but still shows the turn open for a moment
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    A.dispatch("TurnEnded", { P2 })
    eq(G.is_my_turn(P1), false, "turn over until P1's next TurnStarted")
    eq(G.end_pending(P1), true, "counts as ended (state.combat marks it ended)")
    local j = job(op("move_to", { char = P1, pos = { 3, 0, 0 } }))
    eq(j.status, "failed", "move refused"); assert(j.error:find("not this character's turn"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no move order sent")
    -- the game shows the turn inactive: the marker goes (the next active flag is a new turn)
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    eq(G.turn_is_over(P1), false, "cleared once inactive")
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    eq(G.is_my_turn(P1), true, "the next turn is P1's")
    -- TurnStarted clears it too (a turn that never showed inactive between the two)
    A.dispatch("TurnEnded", { P2 })
    eq(G.is_my_turn(P2), false, "P2 over")
    A.dispatch("TurnStarted", { P2 })
    eq(G.is_my_turn(P2), true, "P2's TurnStarted: its turn")
    -- the TTL is the last safety
    A.dispatch("TurnEnded", { P2 })
    clock = clock + G.TURN_OVER_TTL_MS + 1
    eq(G.is_my_turn(P2), true, "TTL")
    restore()
end)

test("S088 whose turn: another side's TurnStarted after the group's ends the members' turns (Guard.turn_moved_on)", function()
    local restore = group_setup()
    local ok, err = pcall(function()
    local C = "COMBAT_HIGHHALL"
    for _, u in ipairs({ P1, P2, NPC }) do osiv[key("CombatGetGuidFor", u)] = C end
    -- round 4: the group's turn starts (one burst for both members)
    A.dispatch("TurnStarted", { "S_Player_" .. P1 })
    clock = clock + 50
    A.dispatch("TurnStarted", { P2 })
    eq(G.turn_moved_on(P1), false, "own turn")
    eq(G.is_my_turn(P1), true, "P1 acts")
    -- 06:19:16 Claude (P1) ends first: requested; 06:19:30 Shadowheart (P2), the last, ends through the HUD (the Lua
    -- job gave up at 3 s: nothing recorded for her)
    local j = job(op("end_turn", { char = P1 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "P1 requested " .. tostring(j.error))
    world[P2].comps.TurnBased.RequestedEndTurn = true
    -- the game applies the group's ends: P1 flagged open again, and its pending end long forgotten (the S052 rule)
    clock = clock + 14000
    G.end_requested[G.guid(P1)] = nil
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    eq(G.is_my_turn(P1), true, "on the flags alone P1 reads open (the S088 'e6 Claude (yours) must act first')")
    -- the Inquisitor's turn starts (e402): the game's turn has moved on, whatever P1's flags show
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), true, "moved on")
    eq(G.is_my_turn(P1), false, "P1 may not act")
    eq(G.is_my_turn(P2), false, "P2 may not act")
    eq(G.turn_requested_end(P1), false, "not 'already ended (the rest of its group still acting)': not its turn")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "end_turn refused"); assert(j2.error:find("not this character's turn"), j2.error)
    eq(#called("EndTurn"), 1, "no EndTurn order sent for P1 again")
    -- round 5: P1's own TurnStarted opens its next turn
    clock = clock + 20000
    A.dispatch("TurnStarted", { P1 })
    eq(G.turn_moved_on(P1), false, "own TurnStarted")
    eq(G.is_my_turn(P1), true, "P1's next turn")
    -- a start outside the party in the same burst as ours (an NPC ally sharing the group) is not "after"
    clock = clock + 300
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), false, "same burst")
    -- another combat's turns say nothing about this one
    clock = clock + 5000
    osiv[key("CombatGetGuidFor", NPC)] = "COMBAT_ELSEWHERE"
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), false, "another combat")
    -- fail-safe: a missed TurnStarted of ours never holds a turn shut for good
    osiv[key("CombatGetGuidFor", NPC)] = C
    A.dispatch("TurnStarted", { NPC })
    eq(G.is_my_turn(P1), false, "moved on")
    clock = clock + G.TURN_MOVED_TTL_MS
    eq(G.is_my_turn(P1), true, "the flags decide again after TURN_MOVED_TTL_MS")
    -- no record (a save loaded mid-turn: no TurnStarted seen): the flags decide
    G.turn_start = {}
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), false, "no record")
    end)
    restore()
    if not ok then error(err, 0) end
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

test("interact ladder: a climb loop that turns round at the foot at once is done there (live probe 10-11 01:4x)", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    local lp = world[LADDER].pos
    world[LADDER].pos = { 10, -6, 0 }   -- live: the item stands at the ladder's foot
    for _, y in ipairs({ -0.6, -2.1, -3.6 }) do world[P1].pos = { 9.9, y, 0 } tick() end
    eq(j.status, "running", "climbing")
    eq(#called("PurgeOsirisQueue"), 1, "the Use task purged once the climb began (no re-climb loop)")
    world[P1].pos = { 9.9, -5.6, 0.3 } tick()   -- at the foot, still moving (no stillness before it climbs back)
    world[LADDER].pos = lp
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    eq(#called("PurgeOsirisQueue"), 1, "the loop is purged")
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
    world[P1].pos = { 9.0, 0, 0 } clock = clock + A.LADDER_RETRY_MS + 1 tick() -- still walking: no re-Use
    eq(#called("Use"), 1, "no re-Use while the Use walks")
    tick()
    eq(#called("CharacterMoveToPosition"), 1, "stepped back from the ladder first (probe C: walk, then Use)")
    local mv = called("CharacterMoveToPosition")[1]
    near(mv[3], 9.0 - A.LADDER_STEP_M, "1.5 m away from the ladder")
    eq(#called("Use"), 1, "no in-place re-Use (probe B did nothing)")
    tick(2) clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(j.status, "running", "re-Used, not failed yet"); eq(#called("Use"), 2, "a second Use after the settle")
    clock = clock + A.LADDER_CLIMB_MS + 1 tick()
    eq(j.status, "failed", "status"); assert(j.error:find("did not climb"), j.error)
    assert(j.error:find("Used twice", 1, true), j.error)
end)

test("interact ladder: the re-Use starts the climb -> done", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick() tick()
    tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 2, "re-Used")
    world[P1].pos = { 9.6, -3, 0 } tick()
    world[P1].pos = { 9.7, -5.8, 0 } tick()
    world[P1].pos = { 10.2, -5.8, 0.4 } clock = clock + 100 tick()
    world[P1].pos = { 10.6, -5.8, 0.8 } clock = clock + 300 tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    eq(#called("Use"), 2, "one re-Use")
end)

test("NAV-8: the ladder step back walks only with a path proof", function()
    local function steps() local n = 0; for _, c in ipairs(called("CharacterMoveToPosition")) do
        if tostring(c[7]):find("lstep") then n = n + 1 end end; return n end
    world[P1].pos = { 9.5, 0, 0 }
    job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick()
    eq(steps(), 1, "a proven step back")
end)

test("NAV-8: no step back where the engine finds no path (it would be the engine's teleport)", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    nav.fn = function() return { reachable = false } end
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick()
    local n = 0
    for _, c in ipairs(called("CharacterMoveToPosition")) do if tostring(c[7]):find("lstep") then n = n + 1 end end
    eq(n, 0, "no step back")
    eq(j.status, "running", "the re-Use still comes " .. tostring(j.error))
end)

test("interact ladder: after a walk to the top, the Use waits until the actor stands still (acceptance 01:5x)", function()
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "walks first")
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })   -- the walk's arrival
    eq(#called("Use"), 0, "no Use at the walk's arrival")
    world[P1].pos = { 9.4, 0, 0 } tick()            -- still settling
    eq(#called("Use"), 0, "no Use while moving")
    tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use once still")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.status, "running", "climb awaited")
end)

test("interact ladder: a settled Use with no UseFinished still watches the climb (acceptance 10-11 08:37/10:43)", function()
    local restore = with_tasks(1)
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    tick() tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use once still")
    assert(j.used_at, "the climb watch starts at our Use")
    local lp = world[LADDER].pos
    world[LADDER].pos = { 10, -6, 0 }
    for _, y in ipairs({ -0.6, -2.1, -3.6 }) do world[P1].pos = { 9.9, y, 0 } tick() end
    eq(#called("PurgeOsirisQueue"), 1, "the Use purged once the climb began, no UseFinished needed")
    world[P1].pos = { 9.9, -5.6, 0.3 } tick()
    world[LADDER].pos = lp
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    restore()
end)

test("interact ladder: LADDER_WATCH_AT_USE=false keeps the old wait for UseFinished", function()
    A.LADDER_WATCH_AT_USE = false
    local ok, err = pcall(function()
        world[P1].pos = { 0, 0, 0 }
        local j = job(op("interact", { char = P1, target = LADDER }))
        world[P1].pos = { 9.3, 0, 0 }
        A.dispatch("EntityEvent", { P1, j.walk.ev })
        tick() tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
        eq(#called("Use"), 1, "Use once still")
        eq(j.used_at, nil, "no watch before UseFinished")
    end)
    A.LADDER_WATCH_AT_USE = true
    if not ok then error(err, 0) end
end)

test("interact ladder: a standing Use with no UseFinished: no-climb retry still comes", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    assert(j.used_at, "watch from the Use")
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick()
    eq(#called("CharacterMoveToPosition"), 1, "stepped back for the re-Use")
end)

test("interact ladder: a UseFinished before our settled Use is not ours", function()
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.used_at, nil, "not counted"); eq(j.status, "running", "status " .. tostring(j.error))
end)

test("pickup: an order gone from the queue is re-sent, not a 40 s timeout (S043 batch step 2 x3)", function()
    world[P1].pos = { 9, 0, 0 }   -- next to the gem: no walk (the 2nd pick_up of a batch)
    local j = job(op("pickup", { char = P1, item = GEM }))
    eq(#called("Pickup"), 1, "ordered")
    tick()
    eq(#called("Pickup"), 1, "not at once")
    clock = clock + A.PICKUP_REORDER_MS tick()
    eq(#called("Pickup"), 2, "re-sent: no task left, nothing picked")
    osiv[key("GetInventoryOwner", GEM)] = P1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    -- a running task (walking/animating) is left alone
    osiv[key("GetInventoryOwner", GEM)] = nil
    local restore = with_tasks(1)
    j = job(op("pickup", { char = P1, item = GEM }))
    clock = clock + A.PICKUP_REORDER_MS * 3 tick()
    eq(#called("Pickup"), 3, "no re-send while its task runs")
    restore()
end)

test("pickup: the item stacked into a party stack is done at once, not a 45 s timeout (S080 #9 apples)", function()
    local STACK = "17171717-0000-0000-0000-00000000000d"
    party_rows = { P1, P2 }
    osiv[key("GetTemplate", GEM)] = "Apple_tpl"
    osiv[key("GetTemplate", STACK)] = "Apple_tpl"
    osiv[key("GetInventoryOwner", STACK)] = P2
    osiv[key("IsCharacter", P2)] = 1
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("pickup", { char = P1, item = GEM }))
    A.dispatch("AddedTo", { "16161616-0000-0000-0000-00000000000c", P2, "" }) -- another kind: not ours
    tick()
    eq(j.status, "running", "other template ignored")
    A.dispatch("AddedTo", { STACK, P2, "" })   -- the add names the stack; the gem still exists this frame
    eq(j.status, "running", "ours still in the world")
    world[GEM] = nil                           -- merged: it left the world
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.by, P2, "by"); eq(j.result.stacked_into, STACK, "stacked_into")
end)

test("pickup: our item's own AddedTo counts even when it merged at once (no holder left)", function()
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("pickup", { char = P1, item = GEM }))
    A.dispatch("AddedTo", { GEM, P1, "" })
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.by, P1, "by")
end)

test("pickup: an item that left the world mid-walk stops the walk at once (no timeout)", function()
    local restore = with_tasks(1)
    local j = job(op("pickup", { char = P1, item = GEM }))
    eq(j.phase, "approach", "walking")
    tick()
    world[GEM] = nil
    tick()
    eq(j.status, "failed", "status")
    assert(j.error:find("no longer in the world", 1, true), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "walk purged")
    restore()
    local r = job(op("pickup", { char = P1, item = GEM }))   -- already gone when ordered: refused, no walk
    eq(r.status, "failed", "refused"); assert(r.error:find("no longer in the world", 1, true), r.error)
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

-- naut S001 16:37 (Nautiloid helm): interact e53 (the 2nd ladder) walked back down e54 (the 1st) and up again 6x
local LADDER2 = "acacacac-0000-0000-0000-000000000018"
test("walk: the approach going up a ladder and back down it stops at the reversal, naming it (naut S001 helm)", function()
    local restore = with_tasks(1)
    A.use_streak = {}
    world[LADDER2] = { pos = { 3, 0, 0 }, comps = { ServerItem = { IsLadder = true } } }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "walks first")
    world[P1].pos = { 3, 0, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 })   -- the climb starts at the foot
    world[P1].pos = { 3, 1.2, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 }) -- several per climb (live 14:1x)
    world[P1].pos = { 3, 3, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 })   -- at the top
    eq(j.status, "running", "one climb is a route " .. tostring(j.error))
    world[P1].pos = { 3, 3, 0.2 } A.dispatch("UseFinished", { P1, LADDER2, 1 }) -- the same ladder again, from its top
    eq(j.status, "running", "not yet back down")
    world[P1].pos = { 3, 0, 0.2 } A.dispatch("UseFinished", { P1, LADDER2, 1 })
    eq(j.status, "interrupted", "stopped at the reversal")
    assert(j.error:find("the walk took ladder " .. LADDER2 .. " up and then back down", 1, true), j.error)
    assert(j.error:find("(interact " .. LADDER2 .. ")", 1, true), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "purged once (the watchdog does not fire after it)")
    restore()
end)

test("walk: ladder uses that never reverse, the target ladder's own use, and a door are no reversal", function()
    world[LADDER2] = { pos = { 3, 0, 0 }, comps = { ServerItem = { IsLadder = true } } }
    local j = job(op("move_to", { char = P1, pos = { 5, 6, 0 } }))
    world[P1].pos = { 3, 3, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 })
    world[P1].pos = { 3, 6, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 }) -- a long ladder, still going up
    world[P1].pos = { 3, 0, 0 } A.dispatch("UseFinished", { P1, DOOR, 1 })    -- not a ladder
    eq(j.status, "running", "move_to " .. tostring(j.error))
    world[P1].pos = { 0, 0, 0 }
    local k = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9, 0, 0 } A.dispatch("UseFinished", { P1, LADDER, 1 })  -- the walk to the target's top
    world[P1].pos = { 9, 3, 0 } A.dispatch("UseFinished", { P1, LADDER, 1 })
    world[P1].pos = { 9, 0, 0 } A.dispatch("UseFinished", { P1, LADDER, 1 })
    eq(k.status, "running", "the target itself is the interact's own business " .. tostring(k.error))
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

-- ACT-6: a story lock (zero lockpick-difficulty id: the Temple of Bhaal door the amulet opens) is clicked anyway.
local function story_door()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "NO_KEY", LockDifficultyClassID = "00000000-0000-0000-0000-000000000000", LockDC = 0 }
end

test("interact story lock, no key: walks and clicks it; the game's script opens it -> done", function()
    story_door()
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Use"), 1, "Use"); eq(#called("Unlock"), 0, "never Unlock")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    osiv[key("IsLocked", DOOR)] = nil
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
    eq(j.result.unlocked_with, nil, "no key reported")
end)

test("interact story lock: the click changes nothing -> failed, says it is a story lock", function()
    story_door()
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use in place")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    tick(30)
    eq(j.status, "failed", "status " .. tostring(j.status)); assert(j.error:find("no lock to pick"), j.error)
end)

test("interact story lock: the Use fails -> the story-lock reason", function()
    story_door()
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(6) -- the failure waits A.USE_FAIL_GRACE_MS for a success
    eq(j.status, "failed", "status"); assert(j.error:find("no lock to pick"), j.error)
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
    tick(6) -- the failure waits A.USE_FAIL_GRACE_MS for a success
    eq(j.status, "failed", "status"); assert(j.error:find("still locked"), j.error)
end)

test("interact: a door's UseFinished success then failure is a use; it opens -> done (S085 Sluice Gate, Vault Door)", function()
    -- S085 04:53:03 e455 Vault Door (Knock had unlocked it): EVENTS `used … success` then `used …`, the job said
    -- "could not use that", the door opened a moment later
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use in place")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick()
    eq(j.status, "running", "still waiting for the door: " .. tostring(j.error))
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
    -- the failure first, the success within the grace: still a use
    osiv[key("IsOpened", DOOR)] = nil
    j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(2)
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    tick(6)
    eq(j.status, "running", "success after the failure: " .. tostring(j.error))
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    -- a failure alone still fails, after the grace
    osiv[key("IsOpened", DOOR)] = nil
    j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(2)
    eq(j.status, "running", "within the grace")
    tick(4)
    eq(j.status, "failed", "status"); eq(j.error, "could not use that", "reason")
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

test("guard (ACT-13): CharacterMoveTo is blocked; G.q runs queries only", function()
    eq(pcall(G.osi, "CharacterMoveTo", P1, NPC, "Walk", "ev"), false, "G.osi CharacterMoveTo")
    eq(#called("CharacterMoveTo"), 0, "no CharacterMoveTo")
    osiv[key("IsLocked", DOOR)] = 1
    eq(G.q("IsLocked", DOOR), 1, "a query runs")
    eq(G.q("Unlock", DOOR, P1), nil, "a blocked action through G.q"); eq(#called("Unlock"), 0, "no Unlock")
    eq(G.q("Use", P1, DOOR, "ev"), nil, "an allowlisted action through G.q"); eq(#called("Use"), 0, "no Use")
    eq(G.q("DB_Foo", P1), nil, "a DB insert"); eq(G.q("SetHitpoints", P1, 1), nil, "a non-query name")
    eq(G.query_ok("InSamePartyGroup"), true, "InSamePartyGroup"); eq(G.query_ok("SpeakerGetDialog"), true, "SpeakerGetDialog")
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
    -- any other carried item (not a potion or a scroll) stays out of combat
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    stats.OBJ_Potion_Healing.ItemUseType = "Food"
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "food"); assert(j.error:find("during combat", 1, true), j.error)
    eq(#called("Use"), 1, "still one Use")
end)

test("interact (A7): in combat a carried scroll is read on the reader's turn for an action; settle charges once", function()
    combat_setup(9)
    local SCR = "a0a0a0a0-0000-0000-0000-00000000000a"
    world[SCR] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "OBJ_Scroll_Fear" } } }
    osiv[key("IsItem", SCR)] = 1
    osiv[key("GetInventoryOwner", SCR)] = P1
    osiv[key("GetStackAmount", SCR)] = 2
    stats.OBJ_Scroll_Fear = { ItemUseType = "Scroll", UseCosts = "ActionPoint:1" }
    local j = job(op("interact", { char = P1, target = SCR }))
    eq(j.status, "running", "reads: " .. tostring(j.error))
    eq(#called("Use"), 1, "one Use")
    A.dispatch("UseFinished", { P1, SCR, 1 })
    tick(10)
    eq(j.status, "done", "done"); eq(j.result.combat_scroll, true, "flagged for the target clicks")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 1, "no charge at the Use")
    -- the target not clicked yet: the scroll is still there -> nothing charged, still pending
    local r = op("scroll_settle", { char = P1 })
    eq(r.read, false, "not used up yet"); eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 1, "kept")
    -- the spell went off (stack 2 -> 1) and the engine charged nothing: the guard charges the action
    osiv[key("GetStackAmount", SCR)] = 1
    r = op("scroll_settle", { char = P1 })
    eq(r.read, true, "read"); eq(r.charged[1].resource, "action", "action charged")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 0, "action spent")
    eq(op("scroll_settle", { char = P1 }).none, true, "settled once")
    -- no action left: refused, no Use
    j = job(op("interact", { char = P1, target = SCR }))
    eq(j.status, "failed", "no action"); assert(j.error:find("not enough action to read it", 1, true), j.error)
    eq(#called("Use"), 1, "still one Use")
    -- not the reader's turn
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    j = job(op("interact", { char = P1, target = SCR }))
    eq(j.status, "failed", "not my turn"); assert(j.error:find("not this character's turn", 1, true), j.error)
    -- the engine charged the action itself (Use took it): settle charges nothing more
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    j = job(op("interact", { char = P1, target = SCR }))
    A.dispatch("UseFinished", { P1, SCR, 1 })
    tick(10)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 0
    osiv[key("IsItem", SCR)] = 0   -- the last scroll is gone
    r = op("scroll_settle", { char = P1 })
    eq(r.read, true, "read (gone)"); eq(#r.charged, 0, "nothing more charged")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 0, "not below 0")
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

test("interact ladder: chained followers settle, are held for the climb and re-chained after (acceptance 04:31)", function()
    add_p2({ 1, 0, 0 })
    osiv[key("InSamePartyGroup", P1, P2)] = 1
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    tick()
    for i = 1, 5 do world[P2].pos = { 8.0 + i * 0.1, 0, 0 } tick() end   -- the follower still arriving
    clock = clock + A.LADDER_SETTLE_MS + 1
    world[P2].pos = { 8.6, 0, 0 } tick()
    eq(#called("Use"), 0, "no Use while a follower moves")
    tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use once everyone stands")
    local d = called("DetachFromPartyGroup")
    eq(#d, 1, "the follower detached for the climb"); eq(d[1][2], P2, "who")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    osiv[key("InSamePartyGroup", P1, P2)] = 0
    world[P1].pos = { 9.6, -3, 0 } tick()
    world[P1].pos = { 9.7, -5.8, 0 } tick()
    world[P1].pos = { 10.2, -5.8, 0.4 } tick() tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    local a = called("AttachToPartyGroup")
    eq(#a, 1, "re-chained"); eq(a[1][2], P2, "follower"); eq(a[1][3], P1, "to the climber")
end)

test("interact on a pulled-up ladder from below: says it hangs out of reach (S059 08:56 e683)", function()
    world[LADDER].pos = { 10, 5.7, 0 }
    nav.fn = function() return { reachable = false } end
    local savedV, savedR = BG3AI.Visibility, BG3AI.Nav.ladderRaised
    BG3AI.Visibility = { itemKind = function(u) return u == LADDER and "ladder" or "item" end }
    BG3AI.Nav.ladderRaised = function(p) return p[2] > 2.5 and 5.7 or nil end
    local ok, err = pcall(G.approach_point, P1, LADDER, 1.5)
    local ok2, err2 = pcall(G.approach_point, P1, DOOR, 1.5)
    world[P1].pos = { 10, 12, 0 }
    local ok3 = pcall(G.raised_ladder_refuse, LADDER, world[P1].pos)
    BG3AI.Visibility, BG3AI.Nav.ladderRaised = savedV, savedR
    eq(ok, false, "refused")
    eq(tostring(err):find("pulled up: its foot hangs 5.7m", 1, true) ~= nil, true, "names the raised foot: " .. tostring(err))
    eq(ok2, false, "door refused too"); eq(tostring(err2):find("pulled up", 1, true), nil, "the door: no ladder text")
    eq(ok3, true, "from above: no refusal")
end)

test("state.actions: lists only prepared class spells (A4: S065 showed Shadowheart every level-1 cleric spell)", function()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ entity = entity, osi = function(f, ...)
        local okf, v = pcall(f, ...)
        if okf then return v end
    end }, { __index = savedV })
    if not BG3AI.State or not BG3AI.State.actions then dofile(LUA .. "/Server/State.lua") end
    for _, id in ipairs({ "Target_Bless", "Target_CureWounds", "Target_Sanctuary" }) do
        stats[id] = { Level = 1, SpellType = "Target", UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:1" }
    end
    local function book(id, kind) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = kind } end
    world[P1].comps.SpellBook = { Spells = { book("Target_Bless", "RequiresPreparation"),
        book("Target_CureWounds", "RequiresPreparation"), book("Target_Sanctuary", "AlwaysPrepared") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds") } }
    BG3AI.State.spellCache = {}
    local ok, list = pcall(BG3AI.State.actions, P1)
    BG3AI.Visibility = savedV
    world[P1].comps.SpellBook, world[P1].comps.SpellBookPrepares = nil, nil
    eq(ok, true, "actions read: " .. tostring(list))
    local ids = {}
    for _, a in ipairs(list) do ids[#ids + 1] = a.spell end
    eq(table.concat(ids, ","), "Target_CureWounds,Target_Sanctuary", "no unprepared Bless")
end)

test("state.combat hp_pct: a member at 1/218 HP reads 1 %, not 0 (S076 Karlach called downed)", function()
    if not BG3AI.State or not BG3AI.State.hp_pct then dofile(LUA .. "/Server/State.lua") end
    eq(BG3AI.State.hp_pct(1, 218), 1, "1/218")
    eq(BG3AI.State.hp_pct(0, 218), 0, "0/218")
    eq(BG3AI.State.hp_pct(109, 218), 50, "half")
    eq(BG3AI.State.hp_pct(218, 218), 100, "full")
end)

-- ---- A5: prepare_spells plan (Guard.prep_info / prep_plan, act.prepare_plan, act.prepared) -------------------

-- Claude the Paladin: Bless, Cure Wounds, Command, Shield of Faith need preparing (Cure Wounds and Shield of Faith
-- prepared), Sanctuary is the oath's, limit 3.
local function paladin(max)
    G.spellFacts = {}  -- each helper redefines the stats; the game's never change (V12 memo)
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h end
    local function book(id, kind) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = kind } end
    local shown = { Target_Bless = "Bless", Target_CureWounds = "Cure Wounds", Target_Command_Container = "Command",
                    Target_ShieldOfFaith = "Shield of Faith", Target_Sanctuary = "Sanctuary",
                    Target_MainHandAttack = "Attack" }
    for id, name in pairs(shown) do stats[id] = { DisplayName = name, Level = id == "Target_MainHandAttack" and 0 or 1 } end
    world[P1].comps.SpellBook = { Spells = { book("Target_Bless", "RequiresPreparation"),
        book("Target_CureWounds", "RequiresPreparation"), book("Target_Command_Container", "RequiresPreparation"),
        book("Target_ShieldOfFaith", "RequiresPreparation"), book("Target_Sanctuary", "AlwaysPrepared"),
        book("Target_MainHandAttack", "Unknown") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds"),
        meta_id("Target_ShieldOfFaith"), meta_id("Target_Sanctuary") } }
    world[P1].comps.Stats = { MaxPreparedSpells = max or 3 }
    osiv[key("GetDisplayName", P1)] = "Claude"
    return function() Ext.Loca.GetTranslatedString = loca end
end

test("prep_info: prepared / unprepared / always-prepared rows and the game's limit", function()
    local undo = paladin()
    local info = G.prep_info(P1)
    undo()
    local function ids(rows) local o = {} for _, r in ipairs(rows) do o[#o + 1] = r.name end return table.concat(o, ",") end
    eq(ids(info.prepared), "Cure Wounds,Shield of Faith", "prepared")
    eq(ids(info.unprepared), "Bless,Command", "unprepared")
    eq(ids(info.always), "Sanctuary", "always (no weapon actions)")
    eq(info.max, 3, "limit = Stats.MaxPreparedSpells"); eq(info.count, 2, "count"); eq(info.prepares, true, "prepares")
end)

test("prep_plan: add within the limit, names or ids in any case; notes for no-ops", function()
    local undo = paladin()
    local p = G.prep_plan(P1, { "bless", "Target_ShieldOfFaith", "Sanctuary" }, { "Command" })
    undo()
    eq(#p.add, 1, "one add"); eq(p.add[1].id, "Target_Bless", "Bless by name")
    eq(#p.remove, 0, "Command was not prepared: nothing to remove")
    eq(p.after, 3, "2 + 1"); eq(p.name, "Claude", "who")
    local notes = table.concat(p.notes, "|")
    eq(notes:find("Command was not prepared", 1, true) ~= nil, true, notes)
    eq(notes:find("Shield of Faith is prepared already", 1, true) ~= nil, true, notes)
    eq(notes:find("Sanctuary is always prepared already", 1, true) ~= nil, true, notes)
end)

test("prep_plan: over the limit names what is prepared; a swap in the same call fits", function()
    local undo = paladin(2)
    refused(function() G.prep_plan(P1, { "Bless" }, {}) end,
        "over the limit: Claude can prepare 2 spells and has 2 prepared (Cure Wounds, Shield of Faith); remove 1")
    local p = G.prep_plan(P1, { "Bless" }, { "Cure Wounds" })
    undo()
    eq(p.add[1].name, "Bless", "add"); eq(p.remove[1].name, "Cure Wounds", "remove"); eq(p.after, 2, "after")
end)

test("prep_plan refusals: in combat, not in the book, a class that doesn't prepare, an unreadable list", function()
    local undo = paladin()
    refused(function() G.prep_plan(P1, { "Fireball" }, {}) end, "Claude doesn't know Fireball: not in their spellbook")
    refused(function() G.prep_plan(P1, {}, { "Fireball" }) end, "Fireball is not in Claude's spellbook")
    osiv[key("IsInCombat", P1)] = 1
    refused(function() G.prep_plan(P1, { "Bless" }, {}) end, "spells are prepared out of combat")
    osiv[key("IsInCombat", P1)] = 0
    world[P1].comps.SpellBookPrepares = setmetatable({}, { __index = function() error("bad component") end })
    refused(function() G.prep_plan(P1, { "Bless" }, {}) end, "the prepared spell list didn't read")
    -- a warlock: every spell is Unknown (learned at level up), nothing to prepare
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_EldritchBlast" }, PrepareType = "Unknown" },
        { Id = { Prototype = "Target_HexSpell" }, PrepareType = "Unknown" } } }
    refused(function() G.prep_plan(P1, { "Hex" }, {}) end, "Claude doesn't prepare spells: sorcerers, warlocks")
    undo()
end)

test("act.prepare_plan returns {refused}; act.prepared lists only preparing casters", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 1, 0, 0 }, comps = {} }
    local undo = paladin()
    local r = op("prepare_plan", { char = P1, add = { "Fireball" } })
    local ok_plan = op("prepare_plan", { char = P1, add = { "Bless" } })
    local list = op("prepared", {})
    undo()
    eq(r.refused ~= nil and r.refused:find("doesn't know Fireball", 1, true) ~= nil, true, tostring(r.refused))
    eq(ok_plan.add[1].id, "Target_Bless", "plan")
    eq(#list.casters, 1, "only P1 prepares"); eq(list.casters[1].uuid, P1, "uuid")
    eq(list.casters[1].max, 3, "max"); eq(#list.casters[1].prepared, 2, "prepared rows")
end)

-- ---- A7: live L4 shapes (Claude S065-end: "31 prepared, limit 9"; page 12/12) -----------------------------------

-- Claude's book as bg3se gives it: upcast entries (_2/_3) with the base as OriginatorPrototype, Command's variants as
-- their own RequiresPreparation entries (stats SpellContainerID), SpellBookPrepares listing only the container.
local function paladin_l4(class_max)
    G.spellFacts = {}  -- each helper redefines the stats; the game's never change (V12 memo)
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h end
    local function book(id, orig, kind) return { Id = { Prototype = id, OriginatorPrototype = orig },
                                                 PrepareType = kind or "RequiresPreparation" } end
    local function sp(id, name, school, flags, costs, extra)
        stats[id] = { DisplayName = name, Level = 1, SpellSchool = school, SpellFlags = flags, UseCosts = costs,
                      Icon = "Spell_" .. name:gsub("%W", "") }
        for k, v in pairs(extra or {}) do stats[id][k] = v end
    end
    sp("Target_Bless", "Bless", "Enchantment", { "IsConcentration", "IsSpell" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Heroism", "Heroism", "Enchantment", { "IsConcentration" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Bane", "Bane", "Enchantment", { "IsConcentration" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_CureWounds", "Cure Wounds", "Evocation", { "IsSpell" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Smite_Wrathful", "Wrathful Smite", "Evocation", { "IsConcentration" },
        "ActionPoint:1;BonusActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Command_Container", "Command", "Enchantment", {}, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Command_Halt", "Command: Halt", "Enchantment", {}, "ActionPoint:1",
        { SpellContainerID = "Target_Command_Container" })
    world[P1].comps.SpellBook = { Spells = {
        book("Target_Bless", "Target_Bless"), book("Target_Bless_2", "Target_Bless"),
        book("Target_Heroism", "Target_Heroism"), book("Target_Heroism_2", "Target_Heroism"),
        book("Target_CureWounds", "Target_CureWounds"), book("Target_CureWounds_2", "Target_CureWounds"),
        book("Target_CureWounds_3", "Target_CureWounds"), book("Target_Smite_Wrathful", "Target_Smite_Wrathful"),
        book("Target_Command_Container", "Target_Command_Container"),
        book("Target_Command_Halt", "Target_Command_Halt"), book("Target_Command_Halt_2", "Target_Command_Halt") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds"), meta_id("Target_Heroism"),
        meta_id("Target_Smite_Wrathful"), meta_id("Target_Command_Container") },
        ClassPreparedSpellCount = class_max and { ["c0de0000-0000-0000-0000-00000000000a"] = class_max } or nil }
    world[P1].comps.Stats = { MaxPreparedSpells = 3 }
    osiv[key("GetDisplayName", P1)] = "Claude"
    return function() Ext.Loca.GetTranslatedString = loca end
end

test("prep_info (A7, live L4): one row per spell (upcasts merged, container variants no rows); the class limit", function()
    local undo = paladin_l4(12)
    local info = G.prep_info(P1)
    undo()
    local function ids(rows) local o = {} for _, r in ipairs(rows) do o[#o + 1] = r.name end return table.concat(o, ",") end
    eq(ids(info.prepared), "Command,Cure Wounds,Heroism,Wrathful Smite", "prepared: 4, once each")
    eq(ids(info.unprepared), "Bless", "unprepared: Bless once, no Command: Halt")
    eq(info.count, 4, "count 4 (not 9 entries)")
    eq(info.max, 12, "limit = SpellBookPrepares.ClassPreparedSpellCount"); eq(info.stats_max, 3, "stats max kept")
    eq(info.prepared[4].look.cost, "BothActions", "Wrathful Smite: BothActions")
    eq(info.unprepared[1].look.icon, "Spell_Bless", "icon"); eq(info.unprepared[1].look.conc, true, "conc")
    -- no class counts: Stats.MaxPreparedSpells
    undo = paladin_l4(nil)
    info = G.prep_info(P1)
    undo()
    eq(info.max, 3, "no class map: Stats.MaxPreparedSpells")
end)

test("unprepared (A7): a container's variant casts with its container prepared", function()
    local undo = paladin_l4(12)
    local set = G.prepared_ids(G.entity(P1))
    local spells = world[P1].comps.SpellBook.Spells
    eq(G.unprepared(spells[10], set), false, "Command: Halt castable (Command prepared)")
    eq(G.unprepared(spells[11], set), false, "Halt's upcast too")
    eq(G.unprepared(spells[2], set), true, "Bless_2 not prepared")
    world[P1].comps.SpellBookPrepares.PreparedSpells = { meta_id("Target_CureWounds") }
    eq(G.unprepared(spells[10], G.prepared_ids(G.entity(P1))), true, "Halt refused once Command isn't prepared")
    undo()
end)

test("prep_plan (A7): no over-limit refusal under the page's limit; each change carries its page look + twins", function()
    local undo = paladin_l4(12)
    local p = G.prep_plan(P1, { "Bless" }, { "Heroism" })
    eq(p.add[1].twins, 0, "Bless: no unprepared look-alike")
    eq(p.remove[1].twins, 0, "Heroism: no prepared look-alike")
    eq(p.add[1].look.school, "Enchantment", "look rides along"); eq(p.add[1].level, 1, "level")
    eq(p.class_max, 12, "class_max"); eq(p.stats_max, 3, "stats_max")
    -- Bane in the book unprepared and alike: Bless now has a twin
    table.insert(world[P1].comps.SpellBook.Spells, { Id = { Prototype = "Target_Bane", OriginatorPrototype = "Target_Bane" },
                                                     PrepareType = "RequiresPreparation" })
    p = G.prep_plan(P1, { "Bless" }, {})
    eq(p.add[1].twins, 1, "Bless and Bane look the same on the page")
    local list = op("prepared", { uuids = { P1 } })
    undo()
    eq(list.casters[1].prepared[1].look, nil, "act.prepared rows carry no look"); eq(list.casters[1].max, 12, "max")
end)

test("act.prepared (V12): a second read gets no stats entry again; same rows", function()
    local undo = paladin_l4(12)
    local get, n = Ext.Stats.Get, 0
    Ext.Stats.Get = function(id) n = n + 1; return get(id) end
    local first = op("prepared", { uuids = { P1 } })
    local cold = n
    n = 0
    local again = op("prepared", { uuids = { P1 } })
    Ext.Stats.Get = get
    undo()
    local function names(l) local o = {} for _, r in ipairs(l.casters[1].prepared) do o[#o + 1] = r.name .. r.level end
        return table.concat(o, ",") end
    eq(cold > 0, true, "the first read gets the entries")
    eq(n, 0, "the second read gets none")
    eq(names(again), names(first), "same rows"); eq(names(first), "Command1,Cure Wounds1,Heroism1,Wrathful Smite1", "rows")
end)

-- ---- A27: Gather Your Allies (S090 Netherbrain: 31 hand clicks over 3 tries) ----------------------------------

local GYA = "Shout_END_AllyAbilities_MultipleSummon"
local WATCH, KEENE, GUR = "Target_END_AllyAbilities_WatchSummon", "Target_END_AllyAbilities_KeeneSummon",
    "Target_END_AllyAbilities_GurSummon"
-- S090's game data (GustavDev Spell_Shout/Spell_Target.txt), the fields the route reads; names by loca handle
local function allies_setup(where)
    G.spellFacts, G.boxData = {}, {}
    local loca = { hGYA = "Gather Your Allies", hW = "The Watch", hK = "Guildmaster Keene's Fixers", hG = "Gur Huntwardens" }
    Ext.Loca.GetTranslatedString = function(h) return loca[h] end
    stats[GYA] = { SpellType = "Shout", DisplayName = "hGYA;4", UseCosts = "",
        SpellFlags = { "IsLinkedSpellContainer", "Temporary", "Wildshape", "CallAlliesSpell" } }
    local function group(id, h, mark)
        stats[id] = { SpellType = "Target", DisplayName = h .. ";2", TargetRadius = "18", Level = 0,
            UseCosts = "ActionPoint:1", SpellContainerID = GYA, Requirements = "Combat",
            RequirementConditions = "not HasStatus('" .. mark .. "INTERDICTED') and not HasStatus('" .. mark .. "CONSUMED')",
            SpellFlags = { "HasVerbalComponent", "CannotTargetItems", "CannotTargetCharacter", "IsLinkedSpellContainer" } }
    end
    group(WATCH, "hW", "END_ALLYABILITIES_WATCHALLIES"); group(KEENE, "hK", "END_ALLYABILTIES_KEENEALLIES")
    group(GUR, "hG", "END_ALLYABILITIES_GURALLIES")
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = GYA } } } }
    local kids = {}
    for _, id in ipairs({ WATCH, KEENE, GUR }) do
        if where == "book" then table.insert(world[P1].comps.SpellBook.Spells, { Id = { Prototype = id } })
        else kids[#kids + 1] = { ContainerSpell = GYA, SpellId = { OriginatorPrototype = id } } end
    end
    world[P1].comps.SpellContainer = { Spells = kids }
    combat_setup(9)
    return function() Ext.Loca.GetTranslatedString = function() return nil end; G.spellFacts, G.boxData = {}, {} end
end

test("A27 Gather Your Allies alone is refused before any cost, listing the groups by name (S090 x2 empty casts)", function()
    for _, where in ipairs({ "book", "container" }) do
        local undo = allies_setup(where)
        osiv[key("HasActiveStatus", P1, "END_ALLYABILITIES_WATCHALLIESINTERDICTED")] = 1 -- Shadowheart called The Watch
        local j = job(op("cast", { char = P1, spell = GYA }))
        undo()
        eq(j.status, "failed", where .. " status")
        -- the usable ids first: combat_turn's quick pick (_VARIANTS) takes Keene's, not the used Watch
        assert(j.error:find("choose a variant: " .. KEENE .. ", " .. GUR .. " (Gather Your Allies casts nothing by itself", 1, true), j.error)
        assert(j.error:find("Guildmaster Keene's Fixers (" .. KEENE .. "), Gur Huntwardens (" .. GUR .. "), The Watch ("
            .. WATCH .. ") [already called]", 1, true), j.error)
        assert(j.error:find("x,y,z = the ground where they appear", 1, true), j.error)
        eq(#called("UseSpell"), 0, where .. " no cast")
    end
end)

test("A27 which='The Watch' casts that group at the ground point (UseSpellAtPosition, the variant's id)", function()
    local undo = allies_setup("container")
    local j = job(op("cast", { char = P1, spell = GYA, which = "the watch", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local c = called("UseSpellAtPosition")
    eq(#c, 1, "one cast"); eq(c[1][3], WATCH, "the group's own spell")
    -- part of a name, any case
    j = job(op("cast", { char = P1, spell = GYA, which = "keene's", pos = { 5, 0, 0 } }))
    eq(called("UseSpellAtPosition")[2][3], KEENE, "Keene by part of the name")
    undo()
end)

test("A27 which: an unknown or used group is refused with the list; no point = 'needs x,y,z'; not in a fight", function()
    local undo = allies_setup("book")
    local function fails(args, pat)
        local j = job(op("cast", args))
        eq(j.status, "failed", pat)
        assert(j.error:find(pat, 1, true), j.error)
    end
    fails({ char = P1, spell = GYA, which = "Harpers", pos = { 5, 0, 0 } },
        "no variant 'Harpers' in Gather Your Allies: The Watch (" .. WATCH .. "), Guildmaster")
    osiv[key("HasActiveStatus", P1, "END_ALLYABILITIES_GURALLIESCONSUMED")] = 1
    fails({ char = P1, spell = GYA, which = "Gur", pos = { 5, 0, 0 } },
        "Gur Huntwardens was already called (END_ALLYABILITIES_GURALLIESCONSUMED): pick another of The Watch")
    fails({ char = P1, spell = GYA, which = "The Watch" }, "needs x,y,z: a ground point")
    osiv[key("IsInCombat", P1)] = nil
    fails({ char = P1, spell = GYA, which = "The Watch", pos = { 5, 0, 0 } }, "called only in a fight")
    undo()
    eq(#called("UseSpellAtPosition"), 0, "nothing cast")
end)

test("A27 the result names who arrived at the point (after the camera wait), or says nobody did", function()
    local undo = allies_setup("container")
    local LADY = "12121212-0000-0000-0000-000000000009"
    local rows = { { "e-npc", { 6, 0, 0 } } }
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({
        charactersNear = function(p, r)
            local out = {}
            for _, row in ipairs(rows) do if U.dist(row[2], p) <= r then out[#out + 1] = { row[1], row[2], 0 } end end
            return out
        end,
        entityUuid = function(e) return e == "e-npc" and NPC or e == "e-lady" and LADY or nil end,
        name = function(u) return u == LADY and "Lady Lockjaw" or "x" end,
    }, { __index = savedV })
    local j = job(op("cast", { char = P1, spell = GYA, which = "The Watch", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpell", { P1, WATCH, "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, WATCH })
    tick(20) -- 1.2 s: the camera wait, nobody yet
    eq(j.status, "running", "waits for them")
    rows[#rows + 1] = { "e-lady", { 7, 0, 1 } }
    tick(2)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#j.result.allies, 1, "one newcomer (the NPC was already there)"); eq(j.result.allies[1].name, "Lady Lockjaw", "name")
    eq(j.result.no_allies, nil, "no warning")
    -- nobody comes: done after ALLY_CALL_WAIT_MS with the warning
    rows = { { "e-npc", { 6, 0, 0 } } }
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = GYA, which = "Keene", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpell", { P1, KEENE, "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, KEENE })
    tick(math.ceil(A.ALLY_CALL_WAIT_MS / 60) + 2)
    BG3AI.Visibility = savedV
    undo()
    eq(j.status, "done", "status"); eq(j.result.allies, nil, "nobody")
    assert(tostring(j.result.no_allies):find("nobody arrived within 9 m of the point 6 s after the cast", 1, true),
        tostring(j.result.no_allies))
end)

test("A27 a linked container with no variants (Shout_Shield_Wizard shape) still casts; forbidden_statuses parse", function()
    G.spellFacts = {}
    spell("Shout_Shield_Wizard", { SpellType = "Shout", UseCosts = "ReactionActionPoint:1", Level = 1,
        SpellFlags = { "IsLinkedSpellContainer" } })
    local j = job(op("cast", { char = P1, spell = "Shout_Shield_Wizard" }))
    eq(j.status, "running", "cast " .. tostring(j.error))
    local f = G.forbidden_statuses({ RequirementConditions = "not HasStatus('A_X') and not HasStatus( 'B_Y' )" })
    eq(#f, 2, "two"); eq(f[2], "B_Y", "second")
    eq(G.forbidden_statuses({ RequirementConditions = "not HasStatus('A') and Combat()" }), nil, "other terms: nil")
    eq(G.forbidden_statuses({}), nil, "none")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
