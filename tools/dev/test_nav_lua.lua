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
printedErrors = {}
asyncQ = {}                                    -- pending async path requests
function pumpPaths()                           -- the engine answers every pending async request
    local q = asyncQ
    asyncQ = {}
    for _, r in ipairs(q) do
        if not r.path.released then
            r.path.GoalFound = Ext.Level.FindPath(r.path) and true or false
            r.cb(r.path)
        end
    end
end

local function mkpath(e, target)
    return { from = world[P1], target = target, Nodes = {}, ErrorCause = 3 }
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end,
        PrintError = function(m) printedErrors[#printedErrors + 1] = m end,
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
        ReleasePath = function(path) if path then path.released = true end end,
        -- async (N.waysWarm): queued here, answered by pumpPaths() (the engine's own update in the game)
        BeginPathfinding = function(e, target, cb)
            local p = mkpath(e, target)
            asyncQ[#asyncQ + 1] = { path = p, cb = cb }
            return p
        end,
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
    N.slowPaths, N.slowPathsN = {}, 0
    N.asyncPaths, N.asyncPathsN = {}, 0
    N.warmAsync = { ok = 0, timeouts = 0, byLevel = {}, broken = false }
    N.guardReset()
    asyncQ = {}
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

test("landing: a guessed y over the chasm around a floor -> that floor (S032 Gauntlet broken staircase)", function()
    world[P1] = { 0, 1, 0 }
    local savedH, savedT = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo
    local deep = false
    Ext.Level.GetHeightsAt = function(x) if x < -2 then return deep and { -12 } or { -2.5, -12 } end return { 1.0 } end
    Ext.Level.GetTileDebugInfo = function(p)
        local void = p[1] < -2 and (deep or p[2] > -2)
        return { GroundSurface = void and "Chasm" or "None", CloudSurface = "None", Flags = {} }
    end
    local ok, err = pcall(function()
        local r = N.landing(P1, { -4, -1, 0 })
        eq(r.safe, false, "safe"); eq(r.surface, "Chasm", "surface")
        assert(r.suggest, "suggest")
        near(r.suggest[2], -2.5, "the stairs' floor"); near(r.suggest[1], -4, "same column")
        eq(r.floor_y, -2.5, "floor_y")
        deep = true
        r = N.landing(P1, { -4, -1, 0 })
        eq(r.safe, false, "safe"); eq(r.floor_y, nil, "only the chasm bottom 13 m down: never the landing")
        assert(not r.suggest or r.suggest[2] > 0, "no landing at the bottom")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo = savedH, savedT
    if not ok then error(err, 0) end
end)

test("jumpAcross: no walk to the goal -> the jump over the chasm toward it (S032 Yurgir broken staircase)", function()
    reset()
    local savedH, savedT = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo
    local wallMode = false
    Ext.Level.GetHeightsAt = function(x)
        if x > -2.9 then return { 0.0 } end
        if x > -5.9 then return wallMode and { 0.0 } or { -30.0 } end
        return { -2.0 }
    end
    Ext.Level.GetTileDebugInfo = function(p)
        local chasm = p[2] < -20
        local w = wallMode and p[1] <= -2.9 and p[1] > -5.9
        return { GroundSurface = chasm and "Chasm" or "None", CloudSurface = "None",
            Flags = (chasm or w) and { "WalkBlock" } or {} }
    end
    blocked = function(x) return x < -5 end
    local ok, err = pcall(function()
        local j = N.jumpAcross(P1, { 0, 0, 0 }, { -20, -2, 0 })
        assert(j, "a jump found")
        assert(j.from[1] > -2.9 and j.from[1] <= -1.4, "takeoff at the edge: " .. j.from[1])
        assert(j.to[1] <= -5.9, "landing past the chasm: " .. j.to[1])
        near(j.to[2], -2, "landing floor"); eq(j.dy, -2, "2 m down")
        assert(j.gap_m <= 3.6, "slid to the real edges (gap 3 m): " .. j.gap_m)
        local savedIA = BG3AI.Visibility.itemsAround
        BG3AI.Visibility.itemsAround = function() return {} end
        local okr, r = pcall(N.routeAhead, { party = { P1 }, is_party = { [P1] = true } }, P1, { -20, -2, 0 })
        BG3AI.Visibility.itemsAround = savedIA
        assert(okr, tostring(r))
        eq(r.reachable, false, "no walk"); assert(r.jump, "route names the jump")
        wallMode = true
        N.frame = N.frame + 1
        eq(N.jumpAcross(P1, { 0, 0, 0 }, { -20, -2, 0 }), nil, "a wall in between is no jump")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo = savedH, savedT
    if not ok then error(err, 0) end
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
        clock = clock + 200; pumpPaths(); N.waysWarm(clock)
        eq(n, 0, "not still long enough")
        world[P1] = { 1, 0, 0 } -- moved
        clock = clock + 1000; pumpPaths(); N.waysWarm(clock)
        eq(n, 0, "moving: nothing")
        for _ = 1, 20 do clock = clock + N.WAYS_WARM_STILL_MS; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock) end
        local searched = n
        eq(searched >= 8, true, "all eight warmed: " .. searched)
        local ways = N.ways(P1, 20, 8, 0)
        eq(#ways, 8, "frontier reads them from the cache")
        eq(n, searched, "no search at read time")
    end)
    Ext.Level.FindPath = real
    assert(ok, err)
end)

test("waysWarm: all eight warm within ~1 s of stopping (arrival), with no search on the server thread", function()
    local real, realB, sync = Ext.Level.FindPath, Ext.Level.BeginPathfindingImmediate, 0
    Ext.Level.BeginPathfindingImmediate = function(...) sync = sync + 1; return realB(...) end
    local ok, err = pcall(function()
        local t0 = clock
        N.waysWarm(clock) -- arrival: first sight of the spot
        while clock - t0 < 1000 do clock = clock + 16; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock) end
        eq(sync, 0, "no synchronous search in the warm-up")
        eq(#N.ways(P1, 20, 8, 0), 8, "eight directions warm 1 s after arrival")
        eq(sync, 0, "the frontier read them from the async answers")
    end)
    Ext.Level.FindPath, Ext.Level.BeginPathfindingImmediate = real, realB
    assert(ok, err)
end)

test("waysWarm: nothing while the Lua heap is over the cap (Shared/Mem.lua); a crumb names the spot", function()
    local real, n = Ext.Level.FindPath, 0
    local crumbs, paused = {}, true
    BG3AI.Mem = { paused = function() return paused end, crumb = function(l) crumbs[#crumbs + 1] = l end }
    Ext.Level.FindPath = function(path) n = n + 1; return real(path) end
    local ok, err = pcall(function()
        pumpPaths(); N.waysWarm(clock)
        for _ = 1, 10 do clock = clock + N.WAYS_WARM_STILL_MS; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock) end
        eq(n, 0, "paused: no search")
        eq(#crumbs, 0, "paused: no crumb")
        paused = false
        clock = clock + N.WAYS_WARM_STILL_MS; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock); pumpPaths()
        eq(n > 0, true, "resumed")
        eq(crumbs[1], "ways warm @(0.0,0.0,0.0)", "crumb")
    end)
    Ext.Level.FindPath = real
    BG3AI.Mem = nil
    assert(ok, err)
end)

test("rawPath: a FindPath over PATH_SLOW_MS is logged with its endpoints (S038 OOM attribution)", function()
    local real, realW, warned = Ext.Level.FindPath, Ext.Utils.PrintWarning, {}
    Ext.Level.FindPath = function(path) clock = clock + N.PATH_SLOW_MS + 5; return real(path) end
    Ext.Utils.PrintWarning = function(s) warned[#warned + 1] = s end
    local ok, err = pcall(function()
        N.path(P1, { 5, 0, 0 })
        eq(#warned, 1, "one warning")
        eq(warned[1]:find("slow FindPath 1005 ms (0.0,0.0,0.0)->(5.0,0.0,0.0) found=true", 1, true) ~= nil, true,
            warned[1])
    end)
    Ext.Level.FindPath, Ext.Utils.PrintWarning = real, realW
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

test("hidden floor: walks stop at the invisible-floor trial's edge; a short step onto it is left to the player (S035)", function()
    reset()
    local savedE, savedHelper, savedPos = Ext.Entity, BG3AI.Visibility.helperItem, BG3AI.Visibility.pos
    -- the Faith-Leap layout: the engine mesh is one flat floor (y 0) over the chasm W of x=-9; the hidden
    -- platforms' helpers sit just under it (y -0.2) from x=-10 to -20
    local cubes = {}
    for i = 0, 5 do cubes["H" .. i] = { -10 - 2 * i, -0.2, 0 } end
    Ext.Entity = { GetAllEntitiesWithComponent = function()
        local out = {}
        for u in pairs(cubes) do out[#out + 1] = { Uuid = { EntityUuid = u } } end
        return out
    end }
    BG3AI.Visibility.helperItem = function(u) return cubes[u] ~= nil end
    BG3AI.Visibility.pos = function(e) local p = cubes[e.uuid or e.Uuid and e.Uuid.EntityUuid] or world[e.uuid]; return p and { p[1], p[2], p[3] } end
    N.helperCache = {}
    local ok, err = pcall(function()
        local r = N.reachToward(P1, "W", nil, 20, 2, true, true)
        eq(r.reachable, true, "the visible floor up to the edge walks")
        assert(r.pos[1] >= -8.01, "stops before the hidden layer: " .. r.pos[1])
        assert(r.hidden_beyond and r.hidden_beyond.pos, "says why it stopped")
        local p = N.path(P1, { -15, 0, 0 })
        eq(p.reachable, true, "a straight walk the agent picked is its own risk")
        assert(p.hidden_floor and p.hidden_floor.at_m >= 8.5 and p.hidden_floor.at_m <= 9.5, "marked where: " .. tostring(p.hidden_floor and p.hidden_floor.at_m))
        eq(p.hidden_floor.winds, nil, "straight")
        local savedNodes = nodesFor
        nodesFor = function(from, t) return { { -6, 0, 0 }, { -12, 0, 4 }, t } end
        local w = N.path(P1, { -16, 0, 0 })
        nodesFor = savedNodes
        assert(w.hidden_floor and w.hidden_floor.winds, "a bend over the hidden floor is flagged (Guard refuses it)")
        local rt = N.reachToward(P1, nil, { -16, 0, 0 }, 20, 2, true, true)
        assert(rt.reachable and rt.pos[1] >= -8.01, "walk_toward a point across it stops at the edge too")
        world[P1] = { -8.5, 0, 0 }
        eq(N.path(P1, { -10.5, 0, 0 }).reachable, true, "a 2 m step onto it: the player's own risk")
        world[P1] = { 0, 0, 0 }
        eq(N.path(P1, { 0, 0, 15 }).reachable, true, "walks elsewhere unaffected")
        eq(N.directLeg(P1, { -15, 0, 0 }, true), nil, "travel_to's direct leg refuses it too")
        N.helperCache = {}
        N.frame = N.frame + 1
        cubes = {}
        eq(N.path(P1, { -15, 0, 0 }).hidden_floor, nil, "no helpers on the level: plain floor")
    end)
    Ext.Entity, BG3AI.Visibility.helperItem, BG3AI.Visibility.pos = savedE, savedHelper, savedPos
    N.helperCache = {}
    world[P1] = { 0, 0, 0 }
    if not ok then error(err, 0) end
end)

test("drop: a route that falls more than 6 m is marked and walk_toward never takes it (S035)", function()
    reset()
    local saved = nodesFor
    nodesFor = function(from, t)
        if t[1] < -10 then return { { -9, 0, 0 }, { -10, -8, 0 }, { t[1], -8, 0 } } end
        return { t }
    end
    local realH = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function(x) return x < -10 and { -8.0 } or { 0.0 } end
    local ok, err = pcall(function()
        local p = N.path(P1, { -14, -8, 0 })
        assert(p.reachable and p.drop and p.drop.m == 8, "marked: " .. tostring(p.drop and p.drop.m))
        local r = N.reachToward(P1, "W", nil, 20, 2, true, true)
        assert(r.reachable and r.pos[1] >= -10, "stops above the drop: " .. tostring(r.pos and r.pos[1]))
        eq(N.path(P1, { -6, 0, 0 }).drop, nil, "a plain walk")
    end)
    nodesFor = saved
    Ext.Level.GetHeightsAt = realH
    if not ok then error(err, 0) end
end)

-- ---- S038 OOM: bounded engine reads ------------------------------------------------
-- The game went 5 -> 13 GB in <40 s with the server hung inside nav.landing (Shadowfell). The node walk was the
-- one unbounded loop: an engine array that never ends (or a garbage count) must not be walked without a cap.

local function endlessNodes(len)
    -- every index answers a node, as a proxy over a garbage Size would; # says `len`
    return setmetatable({}, { __len = function() return len end,
        __index = function(_, i) return { Position = { -i * 0.5, 0, 0 } } end })
end

test("path: a runaway engine node count is not walked (S038) -> reachable unknown", function()
    local real = Ext.Level.FindPath
    Ext.Level.FindPath = function(path) path.Nodes = endlessNodes(1e9); return true end
    N.frame = N.frame + 1
    local ok, r = pcall(N.path, P1, { -9, 0, 0 })
    Ext.Level.FindPath = real
    assert(ok, tostring(r))
    eq(r.reachable, nil, "unknown")
    assert(tostring(r.reason):find("unreadable"), "reason: " .. tostring(r.reason))
end)

test("path: nodes past the engine's count are never read", function()
    local real, reads = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path)
        path.Nodes = setmetatable({}, { __len = function() return 3 end,
            __index = function(_, i) reads = reads + 1; return { Position = { -i, 0, 0 } } end })
        return true
    end
    N.frame = N.frame + 1
    local r = N.path(P1, { -3, 0, 0 })
    Ext.Level.FindPath = real
    eq(r.reachable, true, "found")
    eq(reads, 3, "nodes read")
    eq(#r.points, 4, "from + 3 nodes")
    near(r.length_m, 3, "length")
end)

test("landing: the jump plan's path searches stop at its time budget (S038)", function()
    local real, n = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path) n = n + 1; clock = clock + 200; return false end
    local saved = blocked
    blocked = function() return true end
    N.frame = N.frame + 1
    local plan = N.jumpPlan(P1, { 0, 0, 0 }, { -12, 0, 0 }, 4.5, function() return nil end)
    Ext.Level.FindPath, blocked = real, saved
    eq(plan, nil, "no plan")
    assert(n <= 2, "searches after the budget: " .. n)
end)

-- ---- S038: bounds over every caller (a huge synthetic level) ----------------

test("huge level: chained searches in one frame stop at PATH_FRAME_BUDGET_MS (S038 OOM)", function()
    -- every search is a 120 ms no-path over a vast mesh; every column holds 200 floors (a stack of islands)
    local real, realH, n = Ext.Level.FindPath, Ext.Level.GetHeightsAt, 0
    Ext.Level.FindPath = function(path) n = n + 1; clock = clock + 120; return false end
    Ext.Level.GetHeightsAt = function()
        local hs = {}
        for i = 1, 200 do hs[i] = (i - 100) * 7.5 end
        hs[#hs + 1] = 0.0
        return hs
    end
    N.frame = N.frame + 1
    local t0 = clock
    N.ways(P1, 20, 8, 1e9)
    N.reachToward(P1, "W", nil, 30, 1, true, true)
    N.otherDirections(P1, "W", 30, true, 3)
    N.landing(P1, { -7, 0, 0 })
    N.closestReach(P1, { -60, 0, 0 }, 1e9)
    local spent = clock - t0
    Ext.Level.FindPath, Ext.Level.GetHeightsAt = real, realH
    assert(n <= N.PATH_FRAME_MAX, "searches: " .. n)
    assert(spent <= N.PATH_FRAME_BUDGET_MS + 120, "frame ms: " .. spent)
    eq(N.pathFrame.skipped > 0, true, "skipped some")
end)

test("frame budget: at most PATH_FRAME_MAX searches per frame, the rest unknown and not cached", function()
    local real, n = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path) n = n + 1; return real(path) end
    N.frame = N.frame + 1
    local last
    for i = 1, N.PATH_FRAME_MAX + 50 do last = N.path(P1, { -i * 0.5, 0, 3 }) end
    eq(n, N.PATH_FRAME_MAX, "engine searches")
    eq(last.reachable, nil, "over budget: unknown")
    eq(last.budget, true, "budget flag")
    N.frame = N.frame + 1 -- next frame: searches run again
    local r = N.path(P1, { -(N.PATH_FRAME_MAX + 50) * 0.5, 0, 3 })
    Ext.Level.FindPath = real
    eq(r.reachable, true, "next frame searched")
end)

test("slow memo: a search slower than PATH_SLOW_MEMO_MS is not run again for the TTL", function()
    local real, n = Ext.Level.FindPath, 0
    Ext.Level.FindPath = function(path) n = n + 1; clock = clock + N.PATH_SLOW_MEMO_MS + 50; return false end
    N.slowPaths, N.slowPathsN = {}, 0
    N.frame = N.frame + 1
    local r1 = N.path(P1, { -30, 0, 9 })
    N.frame = N.frame + 1
    local r2 = N.path(P1, { -30, 0, 9 })
    eq(n, 1, "searched once")
    eq(r1.reachable, false, "first: no path"); eq(r2.reachable, false, "memo: no path"); eq(r2.memo, true, "memo flag")
    clock = clock + N.PATH_SLOW_MEMO_TTL_MS + 1
    N.frame = N.frame + 1
    N.path(P1, { -30, 0, 9 })
    world[P1] = { 5, 0, 0 } -- another start is another search
    N.path(P1, { -30, 0, 9 })
    Ext.Level.FindPath = real
    eq(n, 3, "after the TTL / from elsewhere: searched again")
end)

test("PATH_HORIZON caps the engine's own search when set", function()
    local real, seen = Ext.Level.FindPath, nil
    Ext.Level.FindPath = function(path) seen = path.SearchHorizon; return real(path) end
    N.frame = N.frame + 1
    N.path(P1, { -3, 0, 1 })
    eq(seen, nil, "default: engine's own")
    N.PATH_HORIZON = 8000
    N.frame = N.frame + 1
    N.path(P1, { -3, 0, 2 })
    N.PATH_HORIZON = nil
    Ext.Level.FindPath = real
    eq(seen, 8000, "horizon set")
end)

-- ---- S038 (se's verdict: the engine's FindPath never ended): pre-checks, async warm-up, kill switch ------

local function onLevel(name, fn)
    local h, r = Osi.GetHostCharacter, Osi.GetRegion
    Osi.GetHostCharacter = function() return P1 end
    Osi.GetRegion = function() return name end
    local ok, err = pcall(fn)
    Osi.GetHostCharacter, Osi.GetRegion = h, r
    if not ok then error(err, 0) end
end
local function countingFind()
    local real, c = Ext.Level.FindPath, { n = 0 }
    Ext.Level.FindPath = function(path) c.n = c.n + 1; return real(path) end
    return c, function() Ext.Level.FindPath = real end
end

test("precheck: no engine search for a goal with no floor, a floor far from its y, a Chasm, too far, mid-air", function()
    local c, restore = countingFind()
    local realH, realT = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo
    local ok, err = pcall(function()
        Ext.Level.GetHeightsAt = function(x) if x < -50 then return {} end return { 0.0 } end
        local r = N.path(P1, { -60, 0, 0 })
        eq(r.reachable, false, "no tile column"); eq(r.no_floor, true, "no_floor")
        r = N.path(P1, { -8, -30, 0 })
        eq(r.reachable, false, "30 m below the only floor (the void)")
        Ext.Level.GetTileDebugInfo = function(p)
            return { GroundSurface = p[1] < -5 and "Chasm" or "None", CloudSurface = "None", Flags = {} }
        end
        r = N.path(P1, { -7, 0, 0 })
        eq(r.reachable, false, "Chasm"); eq(r.chasm, true, "chasm flag")
        Ext.Level.GetTileDebugInfo = realT
        r = N.path(P1, { 500, 0, 0 })
        eq(r.reachable, nil, "too far: unknown"); eq(r.too_far, true, "too_far")
        world[P1] = { 0, 12, 0 } -- mid-jump: 12 m over the floor
        N.frame = N.frame + 1
        r = N.path(P1, { -4, 0, 0 })
        eq(r.reachable, nil, "mid-air: unknown"); eq(r.midair, true, "midair")
        eq(c.n, 0, "no engine search at all")
        world[P1] = { 0, 0, 0 }
        N.frame = N.frame + 1
        eq(N.path(P1, { -4, 0, 0 }).reachable, true, "a real goal still searches")
        eq(c.n, 1, "one search")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo = realH, realT
    restore()
    if not ok then error(err, 0) end
end)

local function stillWarm(steps, pump)
    N.waysWarm(clock)
    for _ = 1, steps do
        clock = clock + 100; N.frame = N.frame + 1
        if pump then pumpPaths() end
        N.waysWarm(clock)
    end
end

test("waysWarm: one async request at a time; unanswered ones are released, and twice kills the level", function()
    onLevel("SHADOWFELL", function()
        stillWarm(10, true) -- async works here (answers came)
        eq(N.warmAsync.ok > 0, true, "answered")
        N.waysCache, N.waysCacheN, N.asyncPaths, N.asyncPathsN = {}, 0, {}, 0
        world[P1] = { 3, 0, 0 } -- a new spot where the engine never answers
        N.waysWarmState = { t = 0 }
        stillWarm(10, false)
        eq(#asyncQ, 1, "one request outstanding, no more")
        local first = asyncQ[1].path
        clock = clock + N.WARM_ASYNC_TIMEOUT_MS + 1; N.waysWarm(clock)
        eq(first.released, true, "the stuck search is released (ends the engine's search)")
        eq(N.levelSlow(), nil, "one timeout: not yet")
        stillWarm(5, false)
        clock = clock + N.WARM_ASYNC_TIMEOUT_MS + 1; N.waysWarm(clock)
        assert(N.levelSlow(), "two timeouts: the level's kill switch is on")
        local before = #asyncQ
        stillWarm(10, false)
        eq(#asyncQ, before, "no warm-up on a killed level")
    end)
end)

test("waysWarm: async that never answers anywhere turns the warm-up off (no kill switch)", function()
    onLevel("WLD_Main_A", function()
        for _ = 1, N.WARM_ASYNC_BROKEN_AFTER do
            stillWarm(5, false)
            clock = clock + N.WARM_ASYNC_TIMEOUT_MS + 1; N.waysWarm(clock)
        end
        eq(N.warmAsync.broken, true, "broken")
        eq(N.levelSlow(), nil, "not the level's fault")
    end)
end)

test("waysWarm: nothing while a job (walk, jump, cast) runs", function()
    RPC.jobs[999] = { status = "running" }
    stillWarm(10, true)
    eq(N.warmAsync.ok, 0, "no request")
    RPC.jobs[999] = nil
    stillWarm(10, true)
    eq(N.warmAsync.ok > 0, true, "after the job")
end)

test("kill switch: a search over PATH_LEVEL_KILL_MS turns off jump planning, jumpGap and the warm-up there", function()
    onLevel("SHADOWFELL", function()
        local real = Ext.Level.FindPath
        Ext.Level.FindPath = function(path) clock = clock + N.PATH_LEVEL_KILL_MS + 10; return false end
        N.path(P1, { -9, 0, 3 })
        Ext.Level.FindPath = real
        assert(N.levelSlow(), "killed")
        local c, restore = countingFind()
        local plan, why = N.jumpPlan(P1, { 0, 0, 0 }, { -12, 0, 0 }, 4.5, function() return nil end)
        eq(plan, nil, "no plan"); assert(tostring(why):find("off on this level"), "why: " .. tostring(why))
        eq(N.jumpGap(P1, { 0, 0, 0 }, { -1, 0 }), nil, "no jumpGap")
        local savedG = BG3AI.Guard
        BG3AI.Guard = { jump_max = function() return 8.5 end } -- 11 m: too far, so a plan is wanted
        local l = N.landing(P1, { -11, 0, 0 })
        BG3AI.Guard = savedG
        restore()
        assert(l.plan_skipped, "landing says why there is no plan")
        eq(c.n, 0, "no searches for the plan")
    end)
    onLevel("WLD_Main_A", function() eq(N.levelSlow(), nil, "other levels unaffected") end)
end)

test("kill switch: a crash during a jump plan (marker left in the file) turns that level off at the next load", function()
    local realL, realP, realS = Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile
    local saved
    Ext.IO.LoadFile = function(p) if p == N.GUARD_FILE then return "x" end end
    Ext.Json.Parse = function() return { slow = {}, inflight = { level = "SHADOWFELL", op = "jump plan" } } end
    Ext.IO.SaveFile = function(p, txt) if p == N.GUARD_FILE then saved = true end end
    local ok, err = pcall(function()
        N.guardReset()
        local s = N.levelSlow("SHADOWFELL")
        assert(s and s.why:find("died during jump plan"), "killed by the marker")
        eq(N.guardState().inflight, nil, "marker cleared")
        eq(saved, true, "written back")
    end)
    Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile = realL, realP, realS
    N.guardReset()
    if not ok then error(err, 0) end
end)

test("jump plan: the in-flight marker is set during its searches and cleared after", function()
    local marks = {}
    local realS, realJ = Ext.IO.SaveFile, Ext.Json.Stringify
    Ext.Json.Stringify = function(t) return t.inflight and ("in:" .. t.inflight.op) or "clear" end
    Ext.IO.SaveFile = function(p, txt) if p == N.GUARD_FILE then marks[#marks + 1] = txt end end
    local ok, err = pcall(function()
        onLevel("SCL_Main_A", function()
            N.jumpPlan(P1, { 0, 0, 0 }, { -6, 0, 0 }, 4.5, function() return nil end)
        end)
    end)
    Ext.IO.SaveFile, Ext.Json.Stringify = realS, realJ
    if not ok then error(err, 0) end
    eq(marks[1], "in:jump plan", "set first"); eq(marks[#marks], "clear", "cleared after")
end)

-- ---- S039: the Shadowfell's way down (lower ground far below, deep floors in the layout, route's unknown reason) --

-- The arrival ledge at y 0 for x > -3, the void (a Chasm 200 m down) to x -15, an island 22 m below past it.
local function withIslands(fn)
    local savedH, savedT, savedG = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo, BG3AI.Guard
    Ext.Level.GetHeightsAt = function(x)
        if x > -3 then return { 0.0 } end
        if x > -15 then return { -200.0 } end
        return { -22.0 }
    end
    Ext.Level.GetTileDebugInfo = function(p)
        local chasm = p[2] < -100
        return { GroundSurface = chasm and "Chasm" or "None", CloudSurface = "None", Flags = chasm and { "WalkBlock" } or {} }
    end
    BG3AI.Guard = { jump_max = function() return 8.5 end,
        jump_reach = function(c, a, b) return 8.5, 8.5 end }
    blocked = function(x) return x < -3 end
    local ok, err = pcall(fn)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo, BG3AI.Guard = savedH, savedT, savedG
    if not ok then error(err, 0) end
end

test("lowerGround: the island 22 m below, 12+ m out from the ledge, toward the goal (S039 Nightsong's Prison)", function()
    withIslands(function()
        local lo = N.lowerGround(P1, { 0, 0, 0 }, { -200, -140, 0 })
        assert(lo and #lo >= 1, "found")
        eq(lo[1].dir, "W", "dir"); eq(lo[1].toward, true, "toward the goal")
        near(lo[1].drop_m, 22, "drop"); assert(lo[1].gap_m >= 12 and lo[1].gap_m <= 13.6, "gap " .. lo[1].gap_m)
        assert(lo[1].from[1] > -3 and lo[1].to[1] <= -15, "edge -> island")
        eq(lo[1].reach_m, 8.5, "flat reach (no high-ground model)")
    end)
end)

test("lowerGround: nothing below -> nil; the Chasm bottom is no ground", function()
    local savedH = Ext.Level.GetHeightsAt
    withIslands(function()
        Ext.Level.GetHeightsAt = function(x) if x > -3 then return { 0.0 } end return { -200.0 } end
        eq(N.lowerGround(P1, { 0, 0, 0 }, { -200, -140, 0 }), nil, "only void")
    end)
    Ext.Level.GetHeightsAt = savedH
end)

test("routeAhead: no walk to a goal far below -> lower ground listed; a level goal -> none", function()
    withIslands(function()
        local savedIA = BG3AI.Visibility.itemsAround
        BG3AI.Visibility.itemsAround = function() return {} end
        local ctx = { party = { P1 }, is_party = { [P1] = true } }
        local ok, r = pcall(N.routeAhead, ctx, P1, { -40, -22, 0 })
        local ok2, r2 = pcall(N.routeAhead, ctx, P1, { -40, -2, 0 })
        BG3AI.Visibility.itemsAround = savedIA
        assert(ok, tostring(r)); assert(ok2, tostring(r2))
        eq(r.reachable, false, "no walk"); assert(r.lower and r.lower[1].dir == "W", "lower ground W")
        eq(r2.lower, nil, "a goal 2 m down: no lower-ground list")
    end)
end)

test("layoutCell: ground 14 m+ below is ',' ; the Faith-Leap bottom (11.7 m) stays blank; 4-10 m is '_'", function()
    local savedH, savedT = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo
    local h = -22
    Ext.Level.GetHeightsAt = function() return { h } end
    Ext.Level.GetTileDebugInfo = function() return { GroundSurface = "None", CloudSurface = "None", Flags = {} } end
    local ok, err = pcall(function()
        eq(N.layoutCell(0, 0, 0), ",", "22 m down")
        h = -11.7; eq(N.layoutCell(0, 0, 0), " ", "11.7 m down")
        h = -8; eq(N.layoutCell(0, 0, 0), "_", "8 m down")
        h = -130; eq(N.layoutCell(0, 0, 0), " ", "130 m down")
        h = -22
        Ext.Level.GetTileDebugInfo = function() return { GroundSurface = "Chasm", CloudSurface = "None", Flags = {} } end
        eq(N.layoutCell(0, 0, 0), " ", "a Chasm far below is void")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo = savedH, savedT
    if not ok then error(err, 0) end
end)

test("routeAhead: standing off the walkable grid -> unknown_reason says so (S039 21:44 'pathfinding unavailable')", function()
    local savedH = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { -20.0 } end
    local savedIA = BG3AI.Visibility.itemsAround
    BG3AI.Visibility.itemsAround = function() return {} end
    local ok, r = pcall(N.routeAhead, { party = { P1 }, is_party = { [P1] = true } }, P1, { -10, -20, 0 })
    BG3AI.Visibility.itemsAround = savedIA
    Ext.Level.GetHeightsAt = savedH
    assert(ok, tostring(r))
    eq(r.reachable, nil, "unknown")
    assert(r.unknown_reason and r.unknown_reason:find("no floor under"), tostring(r.unknown_reason))
end)

test("landing: a column with no ground at all is never a landing (S038/S039 void jumps froze the game)", function()
    local savedH = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function(x) if x > 2 then return {} end return { 0.0 } end
    local ok, err = pcall(function()
        local r = N.landing(P1, { 4, 20, 0 })
        eq(r.safe, false, "not safe"); eq(r.no_floor, true, "no_floor"); eq(r.void, true, "void")
        onLevel("SCL_Main_A", function()
            N.killLevel(nil, "the game died twice")
            r = N.landing(P1, { 4, 20, 0 })
        end)
        eq(r.level_off, "the game died twice", "says planning is off")
    end)
    Ext.Level.GetHeightsAt = savedH
    if not ok then error(err, 0) end
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
