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
    inDialog = function() return false end,
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
    N.waysCache, N.waysCacheN, N.waysWarmState = {}, 0, { t = 0 }
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

test("landing: a guessed y with no floor near it is a jump into air (S021)", function()
    local r = N.landing(P1, { -4, 8, 0 })
    eq(r.safe, false, "safe"); eq(r.no_floor, true, "no_floor"); eq(r.floors[1], 0, "floor named")
end)

test("landing: no floor at the guessed y -> the floor there is the landing, unless far below (S024 07:14)", function()
    world[P1] = { 0, 2, 0 }
    local r = N.landing(P1, { -4, 9, 0 })
    eq(r.no_floor, true, "no_floor")
    assert(r.suggest and math.abs(r.suggest[2]) < 0.01, "suggests the real floor")
    world[P1] = { 0, 20, 0 } -- the floor is 20 m below the jumper: never swapped in
    r = N.landing(P1, { -4, 9, 0 })
    eq(r.no_floor, true, "no_floor"); eq(r.suggest, nil, "no suggestion 20 m down")
end)

local function withJump(m, fn)
    local saved = BG3AI.Guard
    BG3AI.Guard = { jump_max = function() return m end }
    local ok, err = pcall(fn)
    BG3AI.Guard = saved
    if not ok then error(err, 0) end
end

test("landing: too far but plain walking -> plan says walk (S021 9.6 m 'jump')", function()
    withJump(4, function()
        local r = N.landing(P1, { -9, 0, 0 })
        eq(r.too_far, true, "too_far")
        assert(r.plan and r.plan.walk, "walk plan")
        near(r.plan.land[1], -9, "land x")
    end)
end)

test("landing: too far over a gap -> walk to a stand spot, then a jump in reach", function()
    blocked = function(x) return x < -6 end -- the far side: no walk reaches it
    withJump(4, function()
        local r = N.landing(P1, { -9, 0, 0 })
        eq(r.too_far, true, "too_far")
        local pl = r.plan
        assert(pl and pl.stand, "stand plan")
        assert(pl.stand[1] >= -6, "stand is walkable: " .. pl.stand[1])
        assert(pl.jump_m <= 4, "jump within reach")
        near(pl.land[1], -9, "land x")
    end)
end)

test("landing: blocked tile, no clear spot in reach -> plan to a clear spot near it", function()
    wall = function(x) return x < -6 and x > -10 end
    blocked = function(x) return x < -10.5 end
    withJump(4, function()
        local r = N.landing(P1, { -8, 0, 0 })
        eq(r.blocked, true, "blocked"); eq(r.suggest, nil, "no suggest")
        assert(r.plan, "plan")
        local off = math.abs(r.plan.land[1] + 8) + math.abs(r.plan.land[3])
        assert(off <= 3.01 and off > 0, "land near it, off the wall")
    end)
end)

-- ---- walk_toward: winding walks, other directions ------------------------------------

test("reachToward loose: a winding walk counts when nothing direct does (S021 interiors)", function()
    nodesFor = function(_, t) return { { 0, 0, 6 }, { t[1], 0, 6 }, t } end
    local strict = N.reachToward(P1, "W", nil, 20, 2, true)
    eq(strict.reachable, false, "strict")
    reset(); nodesFor = function(_, t) return { { 0, 0, 6 }, { t[1], 0, 6 }, t } end
    local r = N.reachToward(P1, "W", nil, 20, 2, true, true)
    eq(r.reachable, true, "loose"); eq(r.winding, true, "winding"); near(r.pos[1], -20, "farthest")
end)

test("nav.reach_toward alts: a dead direction names the ones that walk", function()
    blocked = function(x) return x < -1 end
    local r = RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 20, alts = true, loose = true })
    eq(r.reachable, false, "W dead")
    eq(#r.others, 3, "three others"); eq(r.others[1].dir, "S", "nearest bearing first")
    eq(r.others[2].dir, "N", "then the other side")
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

-- ---- losSpot: a spot to cast from (S018: 14 casts refused "no line of sight") ----

-- A pillar: no sight from x in (-6, -2) with z < 1 to a target at (-12, 0, 0).
local function pillarLOS(p) return not (p[1] < -2 and p[1] > -9 and p[3] < 1 and p[3] > -1) end

test("losSpot: first point along the route with the target in sight and in range", function()
    nodesFor = function(_, t) return { { 0, 0, 3 }, t } end -- the route bends north of the pillar
    local r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 12.5, 9, true, pillarLOS)
    eq(r.found, true, "found"); eq(r.via, "route", "via")
    assert(r.dist_m <= 12.5, "in range: " .. r.dist_m)
    assert(pillarLOS(r.pos), "in sight")
    assert(r.walk_m <= 9, "within movement")
end)

test("losSpot: route stays behind the pillar -> a ring spot beside it", function()
    local r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 18, 9, true, function(p)
        return math.abs(p[3]) >= 1 -- a long wall along z=0 to the west: step sideways
    end)
    eq(r.found, true, "found"); eq(r.via, "ring", "via")
    near(math.abs(r.pos[3]), 1.5, "smallest ring that clears", 0.5)
end)

test("losSpot: nothing within movement -> found=false with a reason", function()
    local r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 18, 4, true, function() return false end)
    eq(r.found, false, "found"); assert(r.reason:find("no spot in sight"), r.reason)
end)

test("losSpot: ring spot needing more path than the movement left is skipped", function()
    -- every point has sight only north at z>=4; paths there detour 20 m.
    nodesFor = function(_, t) return { { 10, 0, 0 }, { 10, 0, t[3] }, t } end
    local r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 18, 9, true, function(p) return p[3] >= 4 end)
    eq(r.found, false, "found")
end)

test("losSpot: rays unusable -> found=false (the caller falls back)", function()
    local r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 18, 9, true, function() return nil end)
    eq(r.found, false, "found"); eq(r.reason, "line-of-sight rays unavailable", "reason")
end)

test("losSpot: keeps out of fire", function()
    fire = function(x, z) return z > 0.5 end
    local r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 18, 9, true, function(p) return p[3] >= 1 end)
    eq(r.found, false, "only burning spots see it")
    r = N.losSpot(P1, { -12, 0, 0 }, { -12, 0, 0 }, 18, 9, false, function(p) return p[3] >= 1 end)
    eq(r.found, true, "avoid=false")
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

-- ---- open directions + named exits (S010-S015: screenshots "to find the way out") ----------

test("ways: open, short and blocked directions", function()
    -- a wall 12 m east (the 20 m search fails, 8 m works), nothing at all to the west
    blocked = function(x, z) return x > 12 or x < -1 end
    local ways = N.ways(P1, 20, 8, 1000)
    local by = {}
    for _, w in ipairs(ways) do by[w.dir] = w end
    eq(#ways, 8, "eight directions")
    eq(by.N.open_m, 20, "N open"); eq(by.S.open_m, 20, "S open")
    eq(by.E.open_m, 8, "E stops between 8 and 20 m")
    eq(by.W.open_m, 0, "W blocked"); eq(by.NW.open_m, 0, "NW blocked")
end)

test("ways: the budget leaves the rest unknown", function()
    local real = Ext.Level.FindPath
    Ext.Level.FindPath = function(path) clock = clock + 30; return real(path) end
    local ok, err = pcall(function()
        local ways = N.ways(P1, 20, 8, 50)
        assert(#ways < 8 and #ways >= 2, "searched until the budget: " .. #ways)
    end)
    Ext.Level.FindPath = real
    assert(ok, err)
end)

test("ways: cached per spot, a budget-cut search continues on the next call (read latency)", function()
    local real, n = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path) n = n + 1; clock = clock + 30; return real(path) end
    local ok, err = pcall(function()
        local first = N.ways(P1, 20, 8, 50)
        assert(#first < 8, "cut by the budget")
        N.frame = N.frame + 1
        local second = N.ways(P1, 20, 8, 1000)
        eq(#second, 8, "the rest searched"); eq(second[1].dir, "N", "compass order")
        N.frame = N.frame + 1
        local before = n
        local third = N.ways(P1, 20, 8, 1000)
        eq(#third, 8, "cached"); eq(n, before, "no new searches at the same spot")
        world[P1] = { 5, 0, 0 } -- another spot: searched again
        N.frame = N.frame + 1
        N.ways(P1, 20, 8, 1000)
        eq(n > before, true, "new spot searched")
        world[P1] = { 0, 0, 0 }
        clock = clock + N.WAYS_TTL_MS + 1 -- expired
        N.frame = N.frame + 1
        before = n
        N.ways(P1, 20, 8, 1000)
        eq(n > before, true, "expired entry searched again")
    end)
    Ext.Level.FindPath = real
    assert(ok, err)
end)

test("waysWarm: one direction per step while the active character stands still, none while moving", function()
    local real, n = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path) n = n + 1; return real(path) end
    local ok, err = pcall(function()
        N.waysWarm(clock) -- first sight of the spot
        clock = clock + 200; N.waysWarm(clock)
        eq(n, 0, "not still long enough")
        world[P1] = { 1, 0, 0 } -- moved
        clock = clock + 1000; N.waysWarm(clock)
        eq(n, 0, "moving: nothing")
        for _ = 1, 20 do clock = clock + N.WAYS_WARM_STILL_MS; N.frame = N.frame + 1; N.waysWarm(clock) end
        local searched = n
        eq(searched >= 8, true, "all eight warmed: " .. searched)
        local ways = N.ways(P1, 20, 8, 0)
        eq(#ways, 8, "frontier reads them from the cache")
        eq(n, searched, "no search at read time")
    end)
    Ext.Level.FindPath = real
    assert(ok, err)
end)

test("waysWarm: all eight warm within ~1 s of stopping (arrival); a slow step pauses the warm-up", function()
    local real, n, cost = Ext.Level.FindPath, 0, 0
    Ext.Level.FindPath = function(path) n = n + 1; clock = clock + cost; return real(path) end
    local ok, err = pcall(function()
        local t0 = clock
        N.waysWarm(clock) -- arrival: first sight of the spot
        while clock - t0 < 1000 do clock = clock + 16; N.frame = N.frame + 1; N.waysWarm(clock) end
        eq(#N.ways(P1, 20, 8, 0), 8, "eight directions warm 1 s after arrival")
        -- a new spot where every search is slow (a failing search over a big level)
        N.waysCache, N.waysCacheN, N.waysWarmState = {}, 0, { t = 0 }
        cost = 200
        local t1 = clock
        N.waysWarm(clock)
        n = 0
        while clock - t1 < 2000 do clock = clock + 16; N.frame = N.frame + 1; N.waysWarm(clock) end
        eq(n <= 2, true, "one slow step (far+near), then paused: " .. n .. " searches")
    end)
    Ext.Level.FindPath = real
    assert(ok, err)
end)

test("closestReach: the ring searches stop at the budget (S025: 2.3 s no-path route)", function()
    local real, n = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path) n = n + 1; clock = clock + 40; return real(path) end
    blocked = function(x) return x > 20 end -- nothing near the goal is walkable
    local ok, err = pcall(function()
        local t0 = clock
        N.closestReach(P1, { 40, 0, 0 })
        local rings = n
        assert(clock - t0 <= N.CLOSEST_BUDGET_MS + 40 * 8, "bounded: " .. (clock - t0) .. " ms")
        assert(rings < #N.NEAR_GOAL_RINGS * 8, "not every ring searched: " .. rings)
    end)
    Ext.Level.FindPath = real
    assert(ok, err)
end)

test("ladderTop: from above the foot, the top end; at the foot or not a ladder, nil (S025 08:03)", function()
    withConnectorWorld(function()
        local V = BG3AI.Visibility
        world[L1] = { 0, 22, 0 }
        V.bounds = function() return { height = 10.9, radius = 0.5 } end
        local top = N.ladderTop({ 3, 37.4, 0 }, L1) -- the landing is above the bounds top: the actor's height
        near(top[2], 37.4, "top y raised to the actor's floor"); near(top[1], 0, "foot x")
        near(N.ladderTop({ 3, 50, 0 }, L1)[2], 32.9, "far above: the bounds top")
        eq(N.ladderTop({ 3, 23, 0 }, L1), nil, "at the foot's level")
        eq(N.ladderTop({ 3, 37, 0 }, D1), nil, "a door")
        V.bounds = function() return nil end
        near(N.ladderTop({ 3, 30, 0 }, L1)[2], 30, "no bounds: the actor's height")
    end)
end)

test("exitName: hatches, stairs, gates; not lookalikes", function()
    for _, n in ipairs({ "Hatch", "Wooden Trapdoor", "Stairs", "Ruined Staircase", "Village Gate", "Secret Passage" }) do
        eq(N.exitName(n), true, n)
    end
    for _, n in ipairs({ "Investigate the Gatekeeper's Note", "Hatchet", "Bottle", nil }) do
        eq(N.exitName(n), false, tostring(n))
    end
end)

test("directLeg: a long winding route is walked along, in <= 70 m of route per leg (S029 16:07)", function()
    reset()
    -- 40 m W, 100 m N, 100 m E, 100 m S to a goal 60 m E: 340 m of route, first leg away from the goal.
    nodesFor = function(from, t)
        local pts, x, z = {}, 0, 0
        local function go(dx, dz, n) for _ = 1, n do x, z = x + dx, z + dz; pts[#pts + 1] = { x, 0, z } end end
        go(-5, 0, 8); go(0, 5, 20); go(5, 0, 20); go(0, -5, 20)
        return pts
    end
    local saved = N.verifyPoint
    N.verifyPoint = function() return true end
    local d, why = N.directLeg(P1, { 60, 0, 0 }, false)
    N.verifyPoint = saved
    eq(why, nil, "no refusal")
    eq(d.direct, true, "direct")
    near(d.pos[1], -40, "leg end x"); near(d.pos[3], 30, "leg end z (70 m of route)")
    near(d.route_left_m, 270, "route left")
end)

local function winding()
    nodesFor = function(from, t)
        local pts, x, z = {}, 0, 0
        local function go(dx, dz, n) for _ = 1, n do x, z = x + dx, z + dz; pts[#pts + 1] = { x, 0, z } end end
        go(-5, 0, 8); go(0, 5, 20); go(5, 0, 20); go(0, -5, 20)
        return pts
    end
end

test("directLeg: Fire on this leg -> refused, the leg comes back with its exposure (S030 16:35)", function()
    reset(); winding()
    fire = function(x, z) return x < -24 and x > -31 and math.abs(z) < 1 end
    local saved = N.verifyPoint
    N.verifyPoint = function() return true end
    local d, why, risky = N.directLeg(P1, { 60, 0, 0 }, true)
    N.verifyPoint = saved
    eq(d, nil, "no clear leg"); eq(why, "hazard", "why")
    assert(risky and risky.exposure and risky.exposure.surface == "Fire", "risky leg with its exposure")
    eq(risky.direct, true, "direct")
end)

test("directLeg: Fire further along the route is the next leg's: this one is walked", function()
    reset(); winding()
    fire = function(x, z) return z > 80 and x > 10 and x < 20 end
    local saved = N.verifyPoint
    N.verifyPoint = function() return true end
    local d = N.directLeg(P1, { 60, 0, 0 }, true)
    N.verifyPoint = saved
    assert(d and d.direct and not d.exposure, "a clear direct leg")
end)

test("nav.reach_toward avoid=least direct: nothing clear gets on -> the route across the surface", function()
    reset(); winding()
    fire = function(x, z) return x < -24 and x > -31 and math.abs(z) < 1 end
    blocked = function(x, z) return x > 0.5 and x ~= 60 end
    local saved = N.verifyPoint
    N.verifyPoint = function() return true end
    local r = RPC.ops["nav.reach_toward"]({ pos = { 60, 0, 0 }, max_m = 30, avoid = "least", direct = true })
    N.verifyPoint = saved
    eq(r.direct, true, "the engine's route")
    assert(r.exposure and r.exposure.surface == "Fire", "exposure for the bridge to weigh")
    assert(r.route_left_m and r.route_left_m > 200, "route left")
end)

test("layout: walls and floor around the character, N up (S030 Soft-Step maze)", function()
    reset()
    wall = function(x, z) return x > 1 and x < 2 end -- a N-S wall 1.5 m E of us
    local r = N.layout(P1, 6, 1.5)
    eq(#r.rows, 2 * r.n + 1, "rows")
    local mid = r.rows[r.n + 1]
    eq(mid:sub(r.n + 1, r.n + 1), ".", "our cell is floor")
    eq(mid:sub(r.n + 2, r.n + 2), "#", "the wall E of us")
    eq(r.mosaic, nil, "no trial room here")
end)

test("layout: a floor 4-10 m below is a drop '_', a chasm or a deeper floor stays blank (S032 broken staircase)", function()
    reset()
    local savedH, savedT = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo
    Ext.Level.GetHeightsAt = function(x, z)
        if x > 1 and x < 4 then return { -9.4 } end    -- E: the stairs' floor 9.4 m down
        if x < -1 and x > -4 then return { -9.4 } end   -- W: the same depth but a Chasm surface
        if z > 1 and z < 4 then return { -12 } end      -- N: too deep
        return { 0.0 }
    end
    Ext.Level.GetTileDebugInfo = function(p)
        return { Flags = {}, GroundSurface = (p[1] < -1 and p[2] < -5) and "Chasm" or "None", CloudSurface = "None" }
    end
    local ok, err = pcall(function()
        local r = N.layout(P1, 6, 1.5)
        local mid = r.rows[r.n + 1]
        eq(mid:sub(r.n + 1, r.n + 1), ".", "our cell")
        eq(mid:sub(r.n + 3, r.n + 3), "_", "x=3 a floor 9.4 m below [" .. mid .. "]")
        eq(mid:sub(r.n - 1, r.n - 1), " ", "x=-3 a chasm [" .. mid .. "]")
        eq(r.rows[r.n - 1]:sub(r.n + 1, r.n + 1), " ", "z=3 12 m down: blank")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo = savedH, savedT
    if not ok then error(err, 0) end
end)

test("layout: the Faith-Leap walkway layer is never floor; the mosaic shows it near the trial", function()
    reset()
    local savedH, savedE, savedHelper = Ext.Level.GetHeightsAt, Ext.Entity, BG3AI.Visibility.helperItem
    -- chasm W of x=-3: the hidden walkway at -2.7 (helpers' top), the chasm bottom at -15; a statue platform at
    -- -1.9 around x=-9 (visible, ~1 m above the walkway layer)
    Ext.Level.GetHeightsAt = function(x, z)
        if x < -3 then
            if x < -8 and x > -10 and z > 1 and z < 3 then return { -1.9, -15 } end
            return { -2.7, -15 }
        end
        return { 0.0 }
    end
    local cubes = {}
    for i = 0, 5 do cubes["C" .. i] = { -4 - 2 * i, -2.9, 0 }; cubes["D" .. i] = { -4 - 2 * i, -2.9, 4 } end
    Ext.Entity = { GetAllEntitiesWithComponent = function()
        local out = {}
        for u in pairs(cubes) do out[#out + 1] = { Uuid = { EntityUuid = u } } end
        return out
    end }
    BG3AI.Visibility.helperItem = function(u) return cubes[u] ~= nil end
    local savedPos = BG3AI.Visibility.pos
    BG3AI.Visibility.pos = function(e) local p = cubes[e.uuid or e.Uuid and e.Uuid.EntityUuid] or world[e.uuid]; return p and { p[1], p[2], p[3] } end
    N.helperCache = {}
    local ok, err = pcall(function()
        local r = N.layout(P1, 12, 1.5)
        local mid = r.rows[r.n + 1]
        eq(mid:sub(r.n - 2, r.n - 2), " ", "x=-4.5 on the walkway layer: no visible floor")
        mid = r.rows[r.n - 0]  -- z = +1.5
        eq(mid:sub(r.n - 5, r.n - 5), "v", "x=-9 the statue platform: a visible lower floor [" .. mid .. "]")
        assert(r.mosaic and r.mosaic.rows, "the mosaic near the trial")
        local all = table.concat(r.mosaic.rows, "\n")
        assert(all:find("#####", 1, true), "the safe line drawn: " .. all)
        assert(all:find("O", 1, true), "the statue platform drawn: " .. all)
    end)
    Ext.Level.GetHeightsAt, Ext.Entity, BG3AI.Visibility.helperItem, BG3AI.Visibility.pos = savedH, savedE, savedHelper, savedPos
    N.helperCache = {}
    if not ok then error(err, 0) end
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
