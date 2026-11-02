-- Offline cost harness for the game-side reads (Server/Navigation.lua): every
-- engine call is counted and charged its live cost (measured on WLD_Main_A,
-- 2026-10-10: FindPath ~5.6 ms, GetTileDebugInfo ~0.02 ms, GetHeightsAt
-- ~0.01 ms), and each op must stay under COST_TARGET_MS of simulated engine
-- time. Also checks that the coarse+flood hazard scan finds the same patches
-- as the full scan it replaced. Ext and Visibility are stubbed; Util is real.
-- Run: tools/vis/test_read_cost.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local COST = { FindPath = 5.6, GetTileDebugInfo = 0.02, GetHeightsAt = 0.01 }
local COST_TARGET_MS = 30

-- ---- stubs ---------------------------------------------------------------

local P1 = "aaaaaaaa-0000-0000-0000-000000000001"
local world, items, fire, blocked, heights
asyncQ = {}
local function pumpPaths()
    local q = asyncQ
    asyncQ = {}
    for _, r in ipairs(q) do
        local t = r.path.target
        r.path.GoalFound = not blocked(t[1], t[2], t[3])
        if r.path.GoalFound then r.path.Nodes[1] = { Position = { t[1], t[2], t[3] } } end
        r.cb(r.path)
    end
end
local calls = {}
local nowMs = 0      -- Ext.Utils.MonotonicTime (V13 tests move it; 0 for the rest)
local listeners = {} -- Osiris listeners by event name (V13)
local function charge(name) calls[name] = (calls[name] or 0) + 1 end
local function simMs()
    local ms = 0
    for k, n in pairs(calls) do ms = ms + (COST[k] or 0) * n end
    return ms
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return nowMs end, PrintWarning = function() end, Print = function() end,
        IsValidHandle = function() return false end },
    Osiris = { RegisterListener = function(name, _, _, fn) listeners[name] = fn end },
    Events = { Tick = { Subscribe = function() end } },
    Level = {
        GetHeightsAt = function(x, z) charge("GetHeightsAt"); return heights(x, z) end,
        GetTileDebugInfo = function(p)
            charge("GetTileDebugInfo")
            return { GroundSurface = fire(p[1], p[3]) or "None", CloudSurface = "None" }
        end,
        BeginPathfindingImmediate = function(_, target) return { from = world[P1], target = target, Nodes = {} } end,
        FindPath = function(path)
            charge("FindPath")
            local t = path.target
            if blocked(t[1], t[2], t[3]) then return false end
            path.Nodes[1] = { Position = { t[1], t[2], t[3] } }
            return true
        end,
        ReleasePath = function() end,
        -- async (N.waysWarm, NAV-6): queued, answered by pumpPaths() (the engine's update); charged like a FindPath
        -- but off the server thread, so not counted in an op's cost
        BeginPathfinding = function(_, target, cb)
            local p = { from = world[P1], target = target, Nodes = {} }
            asyncQ[#asyncQ + 1] = { path = p, cb = cb }
            return p
        end,
    },
    IO = { LoadFile = function() return nil end, SaveFile = function() end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    Loca = { GetTranslatedString = function() return nil end, GetTranslatedStringKey = function() return nil end },
    Require = function(p) if p:find("^Server/Data/") then return nil end return dofile(LUA .. "/" .. p) end, -- module parts load; data stays out
    StaticData = { Get = function() return nil end },
}
Osi = setmetatable({ IsItem = function(u) return items and items[u] and 1 or 0 end },
    { __index = function() return function() return 0 end end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
BG3AI.Visibility = {
    osi = function(f, ...) if f then return f(...) end end,
    entity = function(u) return (world[u] or items[u]) and { uuid = u } or nil end,
    pos = function(e) local p = world[e.uuid]; return p and { p[1], p[2], p[3] } or nil end,
    party = function() return { P1 } end,
    selected = function() return nil end, -- nobody holds ClientControl here: the first member
    uuid = function(u) return u end,
    name = function(u) return "N:" .. u end,
    context = function() return { party = { P1 }, is_party = { [P1] = true },
                                  observers = { { uuid = P1, pos = world[P1] } } } end,
    itemKind = function(u) local it = items[u]; return it and it.kind or "item" end,
    itemsAround = function() local out = {}; for u in pairs(items) do out[#out + 1] = u end; table.sort(out); return out end,
    itemVisible = function(_, u) local it = items[u]; return it ~= nil, it and it.kind end,
    characterVisible = function() return false end,
    characters = function() return {} end,
    pointSeen = function() return true end, -- all in view (N.known, hazard patches)
}
dofile(LUA .. "/Server/Navigation.lua")
local N, RPC = BG3AI.Nav, BG3AI.RPC

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
local function reset()
    world = { [P1] = { 0, 0, 0 } }
    items = {}
    fire = function() return nil end
    blocked = function() return false end
    heights = function() return { 0.0 } end
    calls = {}
    N.frame = (N.frame or 0) + 1
    N.waysCache, N.waysCacheN, N.waysWarmState = {}, 0, { t = 0 }
    N.asyncPaths, N.asyncPathsN, asyncQ = {}, 0, {}
    N.waysWarmState = { t = 0 }
    N.slowPaths, N.slowPathsN, N.connectFail, N.geoLast = {}, 0, {}, nil
    nowMs = 0
end
local function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end
local function under(what, limit)
    local ms = simMs()
    print(string.format("     %-34s %6.1f ms  (FindPath %d, tiles %d, heights %d)", what, ms,
        calls.FindPath or 0, calls.GetTileDebugInfo or 0, calls.GetHeightsAt or 0))
    if ms > (limit or COST_TARGET_MS) then error(string.format("%s: %.1f ms > %d ms", what, ms, limit or COST_TARGET_MS), 2) end
end

-- ---- the full hazard scan (the code before the coarse pass), as reference --------

local function fullScan(radius, step)
    local from = world[P1]
    local n = math.floor(radius / step)
    local cells = {}
    for i = -n, n do
        for j = -n, n do
            if i * i + j * j <= n * n then
                local x, z = from[1] + i * step, from[3] + j * step
                local s = fire(x, z)
                if s then cells[i .. ":" .. j] = s end
            end
        end
    end
    -- patches as 8-connected same-surface components: sorted "surface:size" list
    local seen, out = {}, {}
    for key, s in pairs(cells) do
        if not seen[key] then
            seen[key] = true
            local stack, k = { key }, 0
            while #stack > 0 do
                local m = table.remove(stack)
                k = k + 1
                local i, j = m:match("(-?%d+):(-?%d+)")
                for di = -1, 1 do
                    for dj = -1, 1 do
                        local k2 = (tonumber(i) + di) .. ":" .. (tonumber(j) + dj)
                        if cells[k2] == s and not seen[k2] then seen[k2] = true; stack[#stack + 1] = k2 end
                    end
                end
            end
            out[#out + 1] = s .. ":" .. k
        end
    end
    table.sort(out)
    return table.concat(out, ",")
end
local function patchesKey(hz)
    local out = {}
    for _, p in ipairs(hz) do out[#out + 1] = p.surface .. ":" .. math.floor(p.area_m2 + 0.5) end
    table.sort(out)
    return table.concat(out, ",")
end

-- ---- hazard scan ------------------------------------------------------------------

test("hazard scan, radius 15, nothing burning: ~1/4 of the probes", function()
    local hz = N.hazardPatches(P1, 15, 1.0)
    eq(#hz, 0, "patches")
    eq(N.lastScanProbes < 707 / 3, true, "probes " .. tostring(N.lastScanProbes))
    under("nav.hazards r=15 (clear)")
end)

test("hazard scan finds the same patches as the full scan (rectangles >= 2x2, discs, ragged blobs)", function()
    local layouts = {
        function(x, z) if x >= 3 and x <= 4 and z >= -1 and z <= 0 then return "Fire" end end,         -- 2x2, odd corner
        function(x, z) if x >= -9 and x <= -5 and z >= 5 and z <= 11 then return "Acid" end end,
        function(x, z) if (x - 6) ^ 2 + (z - 6) ^ 2 <= 9 then return "Fire" end
            if (x + 7) ^ 2 + (z + 2) ^ 2 <= 4.5 then return "Grease" end end,
        function(x, z) -- an L of two 2-wide arms, next to a different surface
            if (x >= 1 and x <= 2 and z >= 1 and z <= 9) or (z >= 8 and z <= 9 and x >= 1 and x <= 10) then return "Fire" end
            if x >= 3 and x <= 5 and z >= 1 and z <= 3 then return "Poison" end end,
    }
    math.randomseed(7)
    for _ = 1, 6 do -- random unions of 2x2..4x4 squares
        local sq = {}
        for k = 1, 6 do
            sq[k] = { math.random(-13, 11), math.random(-13, 11), math.random(1, 3), k % 2 == 0 and "Fire" or "Acid" }
        end
        layouts[#layouts + 1] = function(x, z)
            for _, q in ipairs(sq) do
                if x >= q[1] and x <= q[1] + q[3] and z >= q[2] and z <= q[2] + q[3] then return q[4] end
            end
        end
    end
    for li, f in ipairs(layouts) do
        fire = f
        eq(patchesKey(N.hazardPatches(P1, 15, 1.0)), fullScan(15, 1.0), "layout " .. li)
    end
end)

test("hazard scan with a big fire still under target", function()
    fire = function(x, z) if (x - 5) ^ 2 + z ^ 2 <= 36 then return "Fire" end end
    local hz = N.hazardPatches(P1, 15, 1.0)
    eq(#hz, 1, "patches")
    under("nav.hazards r=15 (6 m fire)")
end)

-- ---- reach_toward ------------------------------------------------------------------

test("reach_toward 30 m on open ground: one path search", function()
    local r = RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 30 })
    eq(r.reachable, true, "reachable"); eq(r.pos[1], -30, "x")
    eq(calls.FindPath, 1, "searches")
    under("nav.reach_toward 30 m open")
end)

test("reach_toward toward a pos (travel_to leg): the leg end is not searched twice", function()
    local r = RPC.ops["nav.reach_toward"]({ pos = { -25, 0, 0 }, max_m = 30, avoid = "least" })
    eq(r.reachable, true, "reachable"); eq(r.verified, true, "verified")
    eq(calls.FindPath, 1, "searches")
    under("nav.reach_toward leg 25 m")
end)

test("reach_toward 30 m with a wall at 20 m: still under target", function()
    blocked = function(x) return x < -20.5 end
    local r = RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 30 })
    eq(r.pos[1], -20, "x")
    under("nav.reach_toward 30 m, wall at 20")
end)

test("reach_toward 30 m, wall at 4 m (worst common case): reported", function()
    blocked = function(x) return x < -4.5 end
    local r = RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 30 })
    eq(r.pos[1], -4, "x")
    under("nav.reach_toward 30 m, wall at 4", 90) -- the old front-to-back cost: no regression
end)

test("reach_toward follows a ramp but not a ledge", function()
    heights = function(x) return x < -10.5 and { 0.0, 6.0 } or { 0.0 } end -- a ledge 6 m up past 10 m
    local r = N.reachToward(P1, "W", nil, 20, 2, false)
    eq(r.pos[2], 0, "stays on the ground floor")
    heights = function(x) return { -x * 0.3 } end -- a 1:3 ramp up to the west
    r = N.reachToward(P1, "W", nil, 20, 2, false)
    eq(math.abs(r.pos[2] - 6) < 0.15, true, "y on the ramp top " .. tostring(r.pos[2]))
end)

-- ---- frontier ----------------------------------------------------------------------

local function frontierWorld()
    for k = 1, 25 do
        local u = string.format("item-%02d", k)
        world[u] = { k - 12, 0, (k % 5) * 3 }
        items[u] = { kind = k % 3 == 0 and "container" or "item" }
    end
    fire = function(x, z) if (x + 6) ^ 2 + (z - 4) ^ 2 <= 9 then return "Fire" end end
end

test("nav.frontier cold (ways not searched yet at this spot): reported", function()
    frontierWorld()
    local r = RPC.ops["nav.frontier"]({})
    eq(#r.leads > 0, true, "leads"); eq(#(r.hazards or {}), 1, "hazards"); eq(#r.ways, 8, "ways")
    under("nav.frontier cold (25 items, 1 fire)", 60)
end)

test("nav.frontier after standing still (ways warmed by the Tick): under target", function()
    frontierWorld()
    local now = 0
    -- NAV-6: the Tick's warm-up is async (BeginPathfinding, one request at a time, answered by the engine)
    for _ = 1, 40 do now = now + 100; N.waysWarm(now); pumpPaths() end
    calls = {}
    local r = RPC.ops["nav.frontier"]({})
    eq(#r.ways, 8, "ways from the cache")
    eq(calls.FindPath or 0, 0, "no sync search")
    under("nav.frontier warm (25 items, 1 fire)")
end)

test("nav.frontier at a walk's arrival (ways_cached, NAV-6): no sync search, under target", function()
    frontierWorld()
    local r = RPC.ops["nav.frontier"]({ ways_cached = true })
    eq(#r.leads > 0, true, "leads"); eq(r.ways, nil, "no ways warm yet")
    eq(calls.FindPath or 0, 0, "no sync search")
    under("nav.frontier arrival (ways_cached)")
end)


-- ---- walls the game opens (V13, S071 Wyrmway) ------------------------------------------

-- The S071 shape: a Stone Wall 4.5 m W of the party (a door-kind item the party can see), opened by a use.
local WALL = "wall-0001"
local function wallWorld(dist, kind, seen)
    world[WALL] = { -(dist or 4.5), 0, 0 }
    items[WALL] = { kind = kind or "door" }
    if seen == false then items[WALL].hidden = true end
    blocked = function(x) return x < -(dist or 4.5) + 0.5 and x > -(dist or 4.5) - 0.5 or x < -(dist or 4.5) end
end
local function wayW(ways)
    for _, w in ipairs(ways or {}) do if w.dir == "W" then return w.open_m end end
end
-- the Tick stands the party still and warms every direction (answers pumped as the engine would)
local function warm(ms)
    for _ = 1, 40 do nowMs = nowMs + (ms or 100); N.waysWarm(nowMs); pumpPaths() end
end

test("V13: ways cached before a wall opened are dropped when it opens (S071 e371 Stone Wall)", function()
    wallWorld()
    warm()
    eq(wayW(N.ways(P1, 20, 8, 1000)), 0, "W blocked by the wall")
    blocked = function() return false end   -- the wall slid open
    listeners.Opened(WALL)
    nowMs = nowMs + N.GEO_SETTLE_MS + 1
    eq(wayW(N.ways(P1, 20, 8, 1000)), 20, "W open right after (was the cached 'blocked' for 120 s)")
end)

test("V13: a warm-up answer asked before the wall opened is not kept", function()
    wallWorld()
    nowMs = 1000
    for _ = 1, 10 do nowMs = nowMs + 100; N.waysWarm(nowMs) end   -- a request in flight on the old geometry
    eq(#asyncQ, 1, "one request in flight")
    listeners.Opened(WALL)        -- opens; the engine answers from the grid it searched (still closed)
    pumpPaths()
    for _, a in pairs(N.asyncPaths) do eq(a.r.reachable ~= false, true, "no old 'unreachable' kept") end
end)

test("V13: while the wall may still be moving a no-path is not kept, and the reply says try again", function()
    wallWorld()
    listeners.Opened(WALL)        -- the event comes first; the walk grid lags the slide
    nowMs = nowMs + 1000
    eq(wayW(N.ways(P1, 20, 8, 1000)), 0, "W still blocked during the slide")
    local r = RPC.ops["nav.path"]({ pos = { -8, 0, 0 } })
    eq(r.reachable, false, "no path yet")
    eq(r.opened and r.opened.uuid, WALL, "names the wall"); eq(r.opened.how, "opened", "how")
    eq(r.opened.settling, true, "may still be moving")
    blocked = function() return false end   -- the slide ended
    nowMs = nowMs + N.GEO_SETTLE_MS
    N.frame = N.frame + 1                   -- a later tick (the one-frame path cache)
    eq(wayW(N.ways(P1, 20, 8, 1000)), 20, "searched again once settled (not the slide's 'blocked')")
    eq(RPC.ops["nav.path"]({ pos = { -8, 0, 0 } }).reachable, true, "path through it")
end)

test("V13: after it settled a no-path says it was searched after the opening; later, nothing", function()
    wallWorld()
    listeners.Opened(WALL)
    nowMs = nowMs + N.GEO_SETTLE_MS + 2000   -- the opening led elsewhere: still no way W
    local r = RPC.ops["nav.path"]({ pos = { -8, 0, 0 } })
    eq(r.opened and r.opened.settling, nil, "not moving any more"); eq(r.opened.ago_s, 8, "ago")
    nowMs = nowMs + N.GEO_NOTE_MS
    eq(RPC.ops["nav.path"]({ pos = { -8, 0, 0 } }).opened, nil, "old news")
end)

test("V13: a hidden blocker going off stage empties the caches but is never named (fair)", function()
    wallWorld(4.5, "item")
    warm()
    eq(wayW(N.ways(P1, 20, 8, 1000)), 0, "blocked")
    N.slowPaths["x"], N.slowPathsN = { t = nowMs, r = { reachable = false } }, 1
    blocked = function() return false end
    listeners.WentOnStage(WALL, 0)
    eq(next(N.slowPaths), nil, "slow-search memo dropped")
    nowMs = nowMs + N.GEO_SETTLE_MS + 1
    eq(wayW(N.ways(P1, 20, 8, 1000)), 20, "W open")
    blocked = function(x) return x < -4 end
    eq(RPC.ops["nav.path"]({ pos = { -8, 0, 0 } }).opened, nil, "no note for something the party can't see")
end)

test("V13: a door opening far from the party (Lower City crowds) keeps the caches", function()
    wallWorld(4.5)
    warm()
    local gen = N.geoGen
    world["door-far"], items["door-far"] = { 300, 0, 0 }, { kind = "door" }
    listeners.Opened("door-far")
    eq(N.geoGen, gen, "no change"); eq(wayW(N.ways(P1, 20, 8, 1000)), 0, "the cached ways stand")
    listeners.Opened("not-an-item")
    eq(N.geoGen, gen, "only items")
end)

-- ---- known ground = the camera range (user 2026-11-02): what N.known's rays cost per op, Sight 18.5 vs camera 40 ----
-- N.known asks V.pointSeen for every point not walked or remembered; a point out of range costs a distance check, one
-- in range casts rays (in view: 1; blocked: each member's eye, then the look over an edge). Charged per point in
-- range at the live bound: launch-v1 get_layout (1.5 m cells) took p50 137 ms at radius 10 (~220 cells, all in
-- range) and 153 ms at radius 20 (~476 in range of 729): +256 points in range and 500 more cell reads for +16 ms,
-- so <= 0.06 ms a point, the seen-point memory included. Rays are counted too (blocked: 5 rays, the dear case).
COST.KnownPoint = 0.06
local function knownRun(range, scene, op)
    reset()
    N.seenPts, N.seenPtsN = {}, 0
    local V = BG3AI.Visibility
    local real = V.pointSeen
    local n = { asked = 0, cast = 0 }
    V.pointSeen = function(_, pt)
        n.asked = n.asked + 1
        local o = world[P1]
        if math.sqrt((pt[1] - o[1]) ^ 2 + (pt[2] + 0.5 - o[2]) ^ 2 + (pt[3] - o[3]) ^ 2) > range then return false end
        n.cast = n.cast + 1
        charge("KnownPoint")
        if not blocked(pt[1], pt[2], pt[3]) then calls.Raycast = (calls.Raycast or 0) + 1; return true end
        calls.Raycast = (calls.Raycast or 0) + 5
        return false
    end
    scene()
    local ok, err = pcall(op)
    V.pointSeen = real
    if not ok then error(err, 0) end
    n.ms, n.rays = simMs(), calls.Raycast or 0
    return n
end
local KNOWN_SCENES = {
    { "route 35 m W, open", nil, function() RPC.ops["nav.route"]({ pos = { -35, 0, 0 } }) end },
    { "route 35 m W, wall at 4 m (ways)", function() blocked = function(x) return x < -4.5 end end,
      function() RPC.ops["nav.route"]({ pos = { -35, 0, 0 } }) end, 90 },
    { "reach_toward 30 m W, open", nil, function() RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 30 }) end },
    { "reach_toward 30 m W, wall at 4 m", function() blocked = function(x) return x < -4.5 end end,
      function() RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 30 }) end, 90 },
    { "travel leg toward 25 m", nil,
      function() RPC.ops["nav.reach_toward"]({ pos = { -25, 0, 0 }, max_m = 30, avoid = "least" }) end },
    { "frontier cold (25 items, 1 fire)", frontierWorld, function() RPC.ops["nav.frontier"]({}) end, 60 },
    -- the square's corners (to 27.6 m) now cast too: +248 points, ~+15 ms (old 50 ms here, live p50 153 ms)
    { "layout radius 20", nil, function() RPC.ops["nav.layout"]({ radius = 20 }) end, 75 },
    { "layout radius 12 (the default)", nil, function() RPC.ops["nav.layout"]({}) end },
}
test("known ground: the camera range's rays per op stay under each op's target (old vs new printed)", function()
    for _, sc in ipairs(KNOWN_SCENES) do
        local old = knownRun(18.5, sc[2] or function() end, sc[3])
        local new = knownRun(40, sc[2] or function() end, sc[3])
        print(string.format("     %-34s asked %3d | in range %3d -> %3d | rays %3d -> %3d | %5.1f -> %5.1f ms",
            sc[1], new.asked, old.cast, new.cast, old.rays, new.rays, old.ms, new.ms))
        if new.ms > (sc[4] or COST_TARGET_MS) then
            error(string.format("%s: %.1f ms > %d ms", sc[1], new.ms, sc[4] or COST_TARGET_MS))
        end
    end
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
