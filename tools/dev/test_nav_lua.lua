-- Offline tests for Server/Navigation.lua route helpers (S001 fixes): route
-- points, the on-route check the teleport detector uses, harmful surfaces
-- along a route, hazard-aware reach_toward, and the safe-spot finder.
-- Ext and Visibility are stubbed; Shared/Util.lua and Shared/RPC.lua are real.
-- Run: tools/dev/test_nav_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

local clock = 1000
local P1 = "aaaaaaaa-0000-0000-0000-000000000001"
local world = { [P1] = { 0, 0, 0 } }
local fire = function() return false end      -- (x, z) -> burning?
local blocked = function() return false end   -- (x, z) -> no path there
local wall = function() return false end      -- (x, z) -> WalkBlock tile (wall, hole)
local nodesFor = nil                          -- optional (from, target) -> node list

local function mkpath(e, target)
    return { from = world[P1], target = target, Nodes = {}, ErrorCause = 3 }
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end,
        GameTime = function() return 0 end, GetGameState = function() return "Running" end },
    Events = { Tick = { Subscribe = function() end } },
    Level = {
        GetHeightsAt = function() return { 0.0 } end,
        GetTileDebugInfo = function(p)
            return { GroundSurface = fire(p[1], p[3]) and "Fire" or "None", CloudSurface = "None",
                Flags = wall(p[1], p[3]) and { "WalkBlock", "StaticBlock" } or {} }
        end,
        BeginPathfindingImmediate = mkpath,
        FindPath = function(path)
            local t = path.target
            if blocked(t[1], t[3]) then return false end
            local nodes = nodesFor and nodesFor(path.from, t) or { t }
            for _, n in ipairs(nodes) do path.Nodes[#path.Nodes + 1] = { Position = { n[1], n[2], n[3] } } end
            return true
        end,
        ReleasePath = function() end,
    },
    IO = { LoadFile = function() return nil end, SaveFile = function() end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    Loca = { GetTranslatedStringKey = function() return nil end },
    Require = function() return nil end,
}
Osi = {}
local TILE = Ext.Level.GetTileDebugInfo

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
BG3AI.Visibility = {
    osi = function(f, ...) if f then return f(...) end end,
    entity = function(u) return world[u] and { uuid = u } or nil end,
    pos = function(e) local p = world[e.uuid]; return p and { p[1], p[2], p[3] } or nil end,
    party = function() return { P1 } end,
    uuid = function(u) return u end,
    context = function() return { party = { P1 }, is_party = { [P1] = true } } end,
}
dofile(LUA .. "/Server/Navigation.lua")
local N, U, RPC = BG3AI.Nav, BG3AI.Util, BG3AI.RPC

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
local function reset()
    world[P1] = { 0, 0, 0 }
    fire = function() return false end
    blocked = function() return false end
    wall = function() return false end
    nodesFor = nil
    clock = clock + 1000
    N.frame = (N.frame or 0) + 1 -- new frame: drop the path cache
end
local function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end
local function near(a, b, what, tol)
    if math.abs(a - b) > (tol or 0.01) then error((what or "value") .. ": expected " .. b .. ", got " .. a, 2) end
end

-- ---- route points + on-route ------------------------------------------------

local STRAIGHT = { { 0, 0, 0 }, { -10, 0, 0 }, { -20, 0.8, 0 } }

test("path: result carries the route points, starting where we stand", function()
    local r = N.path(P1, { -12, 0, 0 })
    eq(r.reachable, true, "reachable")
    eq(#r.points, 2, "points"); near(r.points[1][1], 0, "start x"); near(r.points[2][1], -12, "end x")
end)

test("onRoute: a 7 m single-tick step along the route is the walk (S001 leg 5)", function()
    eq(N.onRoute(STRAIGHT, { -14.1, 0.5, 0.2 }, { -21.2, 0.8, -0.1 }), true, "on route")
end)

test("onRoute: a step off the route is a teleport", function()
    eq(N.onRoute(STRAIGHT, { -5, 0, 0 }, { -12, 0, 6 }), false, "off route")
end)

test("onRoute: backwards along the route is not the walk", function()
    eq(N.onRoute(STRAIGHT, { -15, 0, 0 }, { -6, 0, 0 }), false, "backwards")
end)

test("onRoute: snapping across a U-shaped route skips more than it covers", function()
    local U_ROUTE = { { 0, 0, 0 }, { 0, 0, 20 }, { 6, 0, 20 }, { 6, 0, 0 } }
    eq(N.onRoute(U_ROUTE, { 0, 0, 1 }, { 6, 0, 1 }), false, "shortcut")
end)

test("onRoute: a jump link up a ledge stays on the route", function()
    local LEDGE = { { 0, 0, 0 }, { -4, 0, 0 }, { -6, 2.5, 0 }, { -12, 2.5, 0 } }
    eq(N.onRoute(LEDGE, { -3.8, 0, 0 }, { -8.5, 2.5, 0 }), true, "ledge")
end)

test("onRoute: no route -> false", function()
    eq(N.onRoute(nil, { 0, 0, 0 }, { 5, 0, 0 }), false, "nil")
end)

-- ---- harmful surfaces -----------------------------------------------------------

test("surfaceAt: Fire is harmful, None is not", function()
    fire = function(x) return x < -18 end
    eq(N.surfaceAt({ -20, 0, 0 }), "Fire", "fire")
    eq(N.surfaceAt({ -10, 0, 0 }), nil, "clear")
end)

test("surfaceAt: invisible surfaces are never reported", function()
    Ext.Level.GetTileDebugInfo = function() return { GroundSurface = "InvisibleWeb", CloudSurface = "None" } end
    local ok, s = pcall(N.surfaceAt, { 0, 0, 0 })
    Ext.Level.GetTileDebugInfo = TILE
    eq(ok, true, "no error"); eq(s, nil, "invisible web")
end)

test("routeHazard: the first burning tile along the way, with how far in", function()
    fire = function(x) return x < -18 and x > -23 end
    local h = N.routeHazard(STRAIGHT)
    eq(h.surface, "Fire", "surface"); near(h.at_m, 18.0, "at", 1.01)
end)

test("routeHazard: standing in it (the first metre) does not count", function()
    fire = function(x) return x > -0.5 end
    eq(N.routeHazard(STRAIGHT), nil, "start skipped")
end)

-- ---- reach_toward avoids hazards --------------------------------------------------

test("reachToward: stops short of a burning patch on the way", function()
    fire = function(x) return x < -18 and x > -23 end
    local r = N.reachToward(P1, "W", nil, 30, 2, true)
    eq(r.reachable, true, "reachable")
    assert(r.pos[1] >= -17, "picked a point at or past the fire's edge: " .. r.pos[1])
    assert(r.avoided and r.avoided > 0, "avoided count")
    eq(r.hazard_beyond.surface, "Fire", "hazard beyond")
end)

test("reachToward: avoid=false keeps the old farthest-point behaviour", function()
    fire = function(x) return x < -18 and x > -23 end
    local r = N.reachToward(P1, "W", nil, 30, 2, false)
    near(r.pos[1], -30, "farthest")
end)

test("reachToward: everything ahead burns -> not reachable, says why", function()
    fire = function(x) return x < -1.2 end
    local r = N.reachToward(P1, "W", nil, 20, 2, true)
    eq(r.reachable, false, "reachable"); eq(r.hazard.surface, "Fire", "hazard")
end)

test("reachToward: toward a point samples the destination itself", function()
    local r = N.reachToward(P1, nil, { -7, 0, 0 }, 30, 2, true)
    near(r.pos[1], -7, "destination x")
end)

-- ---- safe spot ------------------------------------------------------------------

test("safeSpot: not in a surface -> safe", function()
    eq(N.safeSpot(P1, 8).safe, true, "safe")
end)

test("safeSpot: in fire with clear ground east -> nearest clear spot east", function()
    world[P1] = { -20, 0, 0 }
    fire = function(x) return x < -18 and x > -23 end
    local r = N.safeSpot(P1, 8)
    eq(r.found, true, "found"); eq(r.standing_in, "Fire", "standing in")
    assert(r.pos[1] >= -18, "still in the fire: " .. r.pos[1])
    eq(r.dir, "E", "dir")
end)

test("safeSpot: fire everywhere -> found=false", function()
    world[P1] = { -20, 0, 0 }
    fire = function() return true end
    local r = N.safeSpot(P1, 8)
    eq(r.found, false, "found")
end)


-- ---- exposure + least-exposure crossing (S001 run 2: stuck at the burning corridor) ----

test("routeExposure: metres in fire, patches, where it starts", function()
    fire = function(x) return x < -13 and x > -18 end
    local ex = N.routeExposure(STRAIGHT)
    eq(ex.surface, "Fire", "surface"); eq(ex.patches, 1, "patches")
    near(ex.m, 5.0, "metres", 0.6); near(ex.at_m, 13.5, "starts at", 0.6)
end)

test("routeExposure: two patches, longest run; clear route -> nil", function()
    fire = function(x) return (x < -3 and x > -5) or (x < -10 and x > -16) end
    local ex = N.routeExposure(STRAIGHT)
    eq(ex.patches, 2, "patches"); near(ex.longest_m, 6.0, "longest", 0.6); near(ex.m, 8.0, "total", 1.1)
    fire = function() return false end
    eq(N.routeExposure(STRAIGHT), nil, "clear")
end)

-- A burning band 5 m deep across the way W, except a 1 m strip further S
-- (S001: the band at z -423..-425, a quick 2 m crossing at z -429).
local function band(x, z) return (x > -9 and x < -4 and z > -1.5) or (x > -6 and x < -5) end

test("crossing: picks the shortest crossing, not the straight 5 m one", function()
    fire = band
    local c = N.crossing(P1, { -30, 0, 0 }, 20)
    eq(c.reachable, true, "reachable")
    assert(c.exposure and c.exposure.m <= 2.0, "exposure " .. tostring(c.exposure and c.exposure.m))
    assert(c.pos[3] < -1.5, "went S of the band: z=" .. c.pos[3])
    assert(c.progress_m >= 1, "progress")
    eq(N.surfaceNear(c.pos, 0.5), nil, "ends on clear ground")
end)

test("crossing: a clear detour beats any crossing", function()
    fire = function(x, z) return x > -9 and x < -4 and z > -2 end
    local c = N.crossing(P1, { -30, 0, 0 }, 20)
    eq(c.reachable, true, "reachable"); eq(c.detour, true, "detour"); eq(c.exposure, nil, "no exposure")
end)

test("crossing: nothing walkable ahead -> not reachable", function()
    blocked = function(x) return x < -1 end
    local c = N.crossing(P1, { -30, 0, 0 }, 20)
    eq(c.reachable, false, "reachable")
end)

test("nav.reach_toward avoid=least: falls back to the crossing, keeps the straight hazard", function()
    fire = band
    local r = RPC.ops["nav.reach_toward"]({ pos = { -30, 0, 0 }, max_m = 20, avoid = "least" })
    eq(r.reachable, true, "reachable")
    assert(r.exposure and r.exposure.surface == "Fire", "exposure")
    eq(r.straight.hazard.surface, "Fire", "straight hazard")
    eq(r.points, nil, "no route points in the reply")
end)

test("nav.reach_toward avoid=least: a clear straight walk is kept as is", function()
    local r = RPC.ops["nav.reach_toward"]({ pos = { -30, 0, 0 }, max_m = 20, avoid = "least" })
    near(r.pos[1], -20, "straight"); eq(r.exposure, nil, "exposure")
end)

test("safeSpot toward: steps out forward when that is as cheap (no undoing the way on)", function()
    world[P1] = { -21, 0, 0 } -- 2 m of fire W, 3 m E
    fire = function(x) return x < -18 and x > -23 end
    local r = N.safeSpot(P1, 8, { -40, 0, 0 })
    eq(r.found, true, "found")
    assert(r.pos[1] <= -23, "stepped W out of it: " .. r.pos[1])
end)

test("safeSpot toward: forward is all fire -> back out", function()
    world[P1] = { -20, 0, 0 }
    fire = function(x) return x < -18 end
    local r = N.safeSpot(P1, 8, { -40, 0, 0 })
    eq(r.found, true, "found"); eq(r.dir, "E", "dir")
end)

-- ---- jump landings ----------------------------------------------------------------

test("landing: clear spot in range -> safe", function()
    local r = N.landing(P1, { -4, 0, 0 })
    eq(r.safe, true, "safe"); near(r.dist_m, 4, "dist")
end)

test("landing: target in fire -> suggests the nearest clear spot in reach", function()
    fire = function(x) return x < -2.5 and x > -5 end
    local r = N.landing(P1, { -4, 0, 0 })
    eq(r.safe, false, "safe"); eq(r.surface, "Fire", "surface")
    assert(r.suggest, "suggest")
    eq(N.surfaceNear(r.suggest, 0.5), nil, "suggestion is clear")
    assert(r.suggest_dist_m <= 4.5, "within reach")
end)

test("landing: a long jump that may fall short into fire (S001: -26 landed at -21.2)", function()
    fire = function(x) return x < -3 and x > -6 end
    local r = N.landing(P1, { -7.8, 0, 0 })
    eq(r.safe, false, "safe"); eq(r.surface, "Fire", "surface")
    near(r.short_pos[1], -4.5, "short landing")
end)

test("landing: a wall tile is not a landing", function()
    wall = function(x) return x < -3.5 and x > -4.5 end
    local r = N.landing(P1, { -4, 0, 0 })
    eq(r.safe, false, "safe"); eq(r.blocked, true, "blocked")
    assert(r.suggest, "suggest")
end)

test("landing: the nearest clear spot is far from the target -> named, not swapped in", function()
    fire = function(x) return x < -2.5 and x > -9.5 end
    local r = N.landing(P1, { -6, 0, 0 })
    eq(r.safe, false, "safe"); eq(r.suggest, nil, "suggest")
    assert(r.nearest_clear and r.nearest_clear_off_m > 3, "nearest clear")
end)

test("landing: no clear spot near -> no suggestion", function()
    fire = function(x) return x < -1 end
    local r = N.landing(P1, { -4, 0, 0 })
    eq(r.safe, false, "safe"); eq(r.suggest, nil, "suggest")
end)

-- ---- visible hazard patches -----------------------------------------------------------

test("hazardPatches: two patches, nearest first, with size and direction", function()
    fire = function(x, z) return (x >= -5 and x <= -3 and z >= -1 and z <= 2) or (x >= 6 and x <= 9 and z >= 6 and z <= 7) end
    local hz = N.hazardPatches(P1, 12, 1.0)
    eq(#hz, 2, "patches")
    eq(hz[1].surface, "Fire", "surface"); near(hz[1].dist, 3, "dist"); eq(hz[1].dir, "W", "dir")
    near(hz[1].w_m, 3, "width"); near(hz[1].d_m, 4, "depth"); near(hz[1].area_m2, 12, "area")
    eq(hz[2].dir, "NE", "second dir")
end)

test("nav.hazards: compact list, standing in one is flagged", function()
    fire = function(x, z) return math.abs(x) <= 1 and math.abs(z) <= 1 end
    local r = RPC.ops["nav.hazards"]({ radius = 6 })
    eq(#r.hazards, 1, "patches"); eq(r.hazards[1].under, true, "under")
end)

-- ---- approach along the real route (S002: "move within 1.5 m of Zhalk" stopped 6-16 m short) ----

test("pointAlong: interpolates along the polyline and clamps at the end", function()
    local pts = { { 0, 0, 0 }, { 0, 0, 4 }, { -6, 0, 4 } }
    local p = N.pointAlong(pts, 7)
    near(p[1], -3, "x"); near(p[3], 4, "z")
    p = N.pointAlong(pts, 99)
    near(p[1], -6, "end x")
end)

test("arcWithin: first point of the route within range, nil if it never gets there", function()
    local pts = { { 0, 0, 0 }, { 0, 0, 5 }, { -10, 0, 5 }, { -10, 0, 0 } }
    local a, L = N.arcWithin(pts, { -10, 0, 0 }, 1.5)
    near(L, 20, "route length"); near(a, 18.5, "arc", 0.3)
    eq(N.arcWithin(pts, { -10, 0, -6 }, 1.5), nil, "never within")
end)

test("approach: follows a detour that straight-line sampling rejects", function()
    nodesFor = function(_, t) return { { 0, 0, 5 }, { t[1], 0, 5 }, t } end
    local straight = N.reachToward(P1, nil, { -10, 0, 0 }, 9, 2, true)
    assert(not straight.reachable or straight.pos[1] > -3, "straight sampling should stall near the start")
    local r = N.approach(P1, { -10, 0, 0 }, { -10, 0, 0 }, 1.5, nil, true)
    eq(r.limit, "in_range", "limit"); near(r.walk_m, 18.5, "walk", 0.3)
    assert(r.end_m <= 1.5, "ends in range: " .. r.end_m)
end)

test("approach: out of movement walks the budget and says so", function()
    nodesFor = function(_, t) return { { 0, 0, 5 }, { t[1], 0, 5 }, t } end
    local r = N.approach(P1, { -10, 0, 0 }, { -10, 0, 0 }, 1.5, 9, true)
    eq(r.limit, "movement", "limit"); near(r.walk_m, 9, "walk"); near(r.need_m, 18.5, "need", 0.3)
    near(r.pos[1], -4, "x"); near(r.pos[3], 5, "z")
end)

test("approach: stops before fire on the route, avoid=false walks through", function()
    fire = function(x) return x < -4 and x > -7 end
    local r = N.approach(P1, { -12, 0, 0 }, { -12, 0, 0 }, 1.5, nil, true)
    eq(r.limit, "hazard", "limit"); eq(r.hazard.surface, "Fire", "surface")
    assert(r.pos[1] > -4, "kept out of the fire: " .. r.pos[1])
    r = N.approach(P1, { -12, 0, 0 }, { -12, 0, 0 }, 1.5, nil, false)
    eq(r.limit, "in_range", "no avoid")
end)

test("approach: route ends short of the target (e.g. a raised platform)", function()
    nodesFor = function() return { { -6, 0, 0 } } end
    local r = N.approach(P1, { -10, 0, 0 }, { -10, 0, 0 }, 1.5, nil, true)
    eq(r.limit, "route_end", "limit"); near(r.end_m, 4, "end_m")
end)

test("approach: no path at all", function()
    blocked = function() return true end
    local r = N.approach(P1, { -10, 0, 0 }, { -10, 0, 0 }, 1.5, nil, true)
    eq(r.reachable, false, "reachable"); eq(r.reason, "no path to it", "reason")
end)

test("nav.approach RPC: budget_m caps the walk", function()
    local r = RPC.ops["nav.approach"]({ pos = { -20, 0, 0 }, within = 1.5, budget_m = 6 })
    eq(r.limit, "movement", "limit"); near(r.pos[1], -6, "x")
end)

-- ---- RPC replies stay compact ------------------------------------------------------

test("nav.path reply drops the route points and adds the hazard", function()
    fire = function(x) return x < -5 end
    local r = RPC.ops["nav.path"]({ pos = { -10, 0, 0 } })
    eq(r.points, nil, "points"); eq(r.hazard.surface, "Fire", "hazard")
end)

test("nav.activity reports a harmful surface under a party member", function()
    Osi.IsOnStage = function() return 1 end
    fire = function() return true end
    local r = RPC.ops["nav.activity"]({})
    eq(r.surfaces[P1], "Fire", "surface")
end)

-- ---- connectors: probe budget + unreachable memo (live 2026-10-10: one unreachable ladder, 189 ms) ----------

local L1, L2, D1 = "ladder-1", "ladder-2", "door-1"
local function withConnectorWorld(fn)
    local V = BG3AI.Visibility
    local saved = { V.itemsAround, V.itemKind, V.itemVisible, V.name, V.bounds, Osi.IsLocked, Ext.Level.FindPath }
    world[L1], world[L2], world[D1] = { -13, 0, 0 }, { 0, 0, -20 }, { 8, 0, 0 }
    local kinds = { [L1] = "ladder", [L2] = "ladder", [D1] = "door", rock = "object" }
    world.rock = { 1, 0, 0 }
    local visCalls = 0
    V.itemsAround = function() return { "rock", L1, L2, D1 } end
    V.itemKind = function(u) return kinds[u] or "item" end
    V.itemVisible = function(_, u) visCalls = visCalls + 1; return true, kinds[u] end
    V.name = function(u) return u end
    V.bounds = function() return nil end
    Osi.IsLocked = function() return 0 end
    local find = Ext.Level.FindPath
    Ext.Level.FindPath = function(path)
        clock = clock + 20 -- each engine search costs ~20 ms of game thread
        return find(path)
    end
    local ok, err = pcall(fn, function() return visCalls end)
    V.itemsAround, V.itemKind, V.itemVisible, V.name, V.bounds, Osi.IsLocked, Ext.Level.FindPath = table.unpack(saved)
    world[L1], world[L2], world[D1], world.rock = nil, nil, nil, nil
    N.connectFail = {}
    if not ok then error(err, 0) end
end
local function byUuid(list)
    local m = {}
    for _, o in ipairs(list) do m[o.uuid] = o end
    return m
end

test("connectors: only ladders/doors get the visibility test", function()
    withConnectorWorld(function(visCalls)
        N.connectors({}, P1, nil, 30, 5)
        eq(visCalls(), 3, "itemVisible calls (the rock is skipped by kind)")
    end)
end)

test("connectors: an unreachable ladder eats the budget, the rest are unprobed", function()
    withConnectorWorld(function()
        blocked = function(x) return x < -10 end -- the ladder at x=-13 and its approach ring: no path
        local t0 = clock
        local m = byUuid(N.connectors({}, P1, nil, 30, 5))
        if clock - t0 > N.CONNECT_BUDGET_MS + 20 then error("spent " .. (clock - t0) .. " ms", 0) end
        eq(m[D1].path_m ~= nil, true, "nearest (door, 8 m) probed")
        eq(m[L1].unprobed, true, "ladder ring cut at the deadline: unprobed, not unreachable")
        eq(m[L1].unreachable, nil, "not unreachable")
        eq(m[L2].unprobed, true, "past the budget")
    end)
end)

test("connectors: an unreachable verdict is kept while we stand still, dropped after a move", function()
    withConnectorWorld(function()
        N.CONNECT_BUDGET_MS, world[L2] = 1000, nil
        blocked = function(x) return x < -10 end
        local m = byUuid(N.connectors({}, P1, nil, 30, 5))
        eq(m[L1].unreachable, true, "probed: unreachable")
        N.frame = N.frame + 1 -- no per-frame path cache
        local t0 = clock
        m = byUuid(N.connectors({}, P1, nil, 30, 5))
        eq(m[L1].unreachable, true, "memo: still unreachable")
        eq(clock - t0, 20, "only the door was searched again")
        world[P1] = { 3, 0, 0 } -- moved 3 m: probe again
        N.frame = N.frame + 1
        t0 = clock
        N.connectors({}, P1, nil, 30, 5)
        eq(clock - t0 > 20, true, "ladder probed again after the move")
        clock = clock + N.CONNECT_FAIL_TTL_MS + 1 -- expiry also prunes
        N.connectors({}, P1, nil, 30, 5)
        N.CONNECT_BUDGET_MS = 40
    end)
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
