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
            return { GroundSurface = fire(p[1], p[3]) and "Fire" or "None", CloudSurface = "None" }
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
    Ext.Level.GetTileDebugInfo = function(p)
        return { GroundSurface = fire(p[1], p[3]) and "Fire" or "None", CloudSurface = "None" }
    end
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

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
