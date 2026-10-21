-- Offline tests for Server/Navigation.lua route helpers (S001 fixes): route
-- points, the on-route check the teleport detector uses, harmful surfaces
-- along a route, hazard-aware reach_toward, and the safe-spot finder.
-- Ext and Visibility are stubbed; Shared/Util.lua and Shared/RPC.lua are real.
-- Run: tools/dev/test_nav_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

clock = 1000
P1 = "aaaaaaaa-0000-0000-0000-000000000001"
world = { [P1] = { 0, 0, 0 } }
fire = function() return false end      -- (x, z) -> burning?
blocked = function() return false end   -- (x, z) -> no path there
wall = function() return false end      -- (x, z) -> WalkBlock tile (wall, hole)
nodesFor = nil                          -- optional (from, target) -> node list
seen = function() return true end       -- V.pointSeen: (pt) -> true|false|nil (default: all in view)
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
    Require = function(p) if p:find("^Server/Data/") then return nil end return dofile(LUA .. "/" .. p) end, -- module parts load; data stays out
}
Osi = {}
TILE = Ext.Level.GetTileDebugInfo

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
    pointSeen = function(ctx, pt) return seen(pt) end,
}
dofile(LUA .. "/Server/Navigation.lua")
N, U, RPC = BG3AI.Nav, BG3AI.Util, BG3AI.RPC

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
function reset()
    world[P1] = { 0, 0, 0 }
    fire = function() return false end
    blocked = function() return false end
    wall = function() return false end
    nodesFor = nil
    seen = function() return true end
    N.seenPts, N.seenPtsN = {}, 0
    clock = clock + 1000
    N.frame = (N.frame or 0) + 1 -- new frame: drop the path cache
    N.lowerMemo = nil
    N.waysCache, N.waysCacheN, N.waysWarmState = {}, 0, { t = 0 }
    N.slowPaths, N.slowPathsN = {}, 0
    N.asyncPaths, N.asyncPathsN = {}, 0
    N.warmAsync = { ok = 0, timeouts = 0, byLevel = {}, broken = false }
    N.guardReset()
    N.slowHits = {}
    asyncQ = {}
end
function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end
function near(a, b, what, tol)
    if math.abs(a - b) > (tol or 0.01) then error((what or "value") .. ": expected " .. b .. ", got " .. a, 2) end
end

-- ---- the tests: these files, in order, on the stubs above (split for the 1000-line cap) ----
local DIR = arg[0]:match("^(.*)/[^/]*$") or "."
for _, part in ipairs({ "test_nav_lua_reach.lua", "test_nav_lua_ways.lua",
    "test_nav_lua_guard.lua", "test_nav_lua_legs.lua" }) do dofile(DIR .. "/" .. part) end

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
