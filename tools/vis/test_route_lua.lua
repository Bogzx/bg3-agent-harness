-- Offline tests for the route-ahead helpers in Server/Navigation.lua (nav.route:
-- legs, links, floor levels, closest reachable spot, ladders/doors toward a
-- goal; ladder height in nav.frontier) and the journal cache in
-- Server/State.lua. Ext and Visibility are stubbed; Shared/Util.lua is real.
-- Run: tools/vis/test_route.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

local clock = 1000
local P1 = "aaaaaaaa-0000-0000-0000-000000000001"
local world, items                            -- uuid -> pos; uuid -> {kind, visible, height, locked}
local fire, blocked, heights, nodesFor, portalAt
local osiCalls = 0

local function mkpath(e, target)
    return { from = world[P1], target = target, Nodes = {}, ErrorCause = 3 }
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end,
        IsValidHandle = function(h) return h == "portal" end },
    Events = { Tick = { Subscribe = function() end } },
    Level = {
        GetHeightsAt = function(x, z) return heights(x, z) end,
        GetTileDebugInfo = function(p)
            return { GroundSurface = fire(p[1], p[3]) and "Fire" or "None", CloudSurface = "None", Flags = {} }
        end,
        BeginPathfindingImmediate = mkpath,
        FindPath = function(path)
            local t = path.target
            if blocked(t[1], t[2], t[3]) then return false end
            local nodes = nodesFor and nodesFor(path.from, t) or { t }
            for _, n in ipairs(nodes) do
                path.Nodes[#path.Nodes + 1] = { Position = { n[1], n[2], n[3] }, Portal = portalAt and portalAt(n) and "portal" or nil }
            end
            return true
        end,
        ReleasePath = function() end,
    },
    IO = { LoadFile = function() return nil end, SaveFile = function() end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    Loca = { GetTranslatedString = function(h) return h and ("T:" .. h) or nil end,
        GetTranslatedStringKey = function() return nil end },
    Require = function() return nil end,
    StaticData = { Get = function() return nil end },
}
Osi = setmetatable({}, { __index = function(_, name)
    return function(u)
        osiCalls = osiCalls + 1
        if name == "IsLocked" then return items[u] and items[u].locked and 1 or 0 end
        if name == "QuestIsAccepted" or name == "QuestUpdateIsUnlocked" then return 1 end
        return 0
    end
end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
BG3AI.Visibility = {
    osi = function(f, ...) if f then return f(...) end end,
    entity = function(u) return (world[u] or items[u]) and { uuid = u } or nil end,
    pos = function(e) local p = world[e.uuid]; return p and { p[1], p[2], p[3] } or nil end,
    party = function() return { P1 } end,
    uuid = function(u) return u end,
    name = function(u) return "N:" .. u end,
    context = function() return { party = { P1 }, is_party = { [P1] = true } } end,
    itemsAround = function() local out = {}; for u in pairs(items) do out[#out + 1] = u end; table.sort(out); return out end,
    itemVisible = function(ctx, u) local it = items[u]; return it and it.visible ~= false, it and it.kind end,
    characterVisible = function() return false end,
    bounds = function(e) local it = items[e.uuid]; return it and it.height and { height = it.height, radius = 0.5 } or nil end,
}
dofile(LUA .. "/Server/Navigation.lua")
local N, RPC = BG3AI.Nav, BG3AI.RPC

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
local function reset()
    world = { [P1] = { 0, 0, 0 } }
    items = {}
    fire = function() return false end
    blocked = function() return false end
    heights = function() return { 0.0 } end
    nodesFor, portalAt = nil, nil
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
    if math.abs(a - b) > (tol or 0.06) then error((what or "value") .. ": expected " .. b .. ", got " .. tostring(a), 2) end
end
local function item(u, pos, kind, extra)
    world[u] = pos
    items[u] = { kind = kind }
    for k, v in pairs(extra or {}) do items[u][k] = v end
end

-- ---- legs and links ------------------------------------------------------------

test("routeLegs: a straight 25 m walk is legs of 10 m", function()
    local legs, links = N.routeLegs({ { 0, 0, 0 }, { -25, 0, 0 } })
    eq(#links, 0, "links")
    -- one 25 m segment: a single leg (legs split between points, never inside one)
    eq(#legs, 1, "legs"); near(legs[1].m, 25, "m"); eq(legs[1].dir, "W", "dir")
    legs = N.routeLegs({ { 0, 0, 0 }, { -6, 0, 0 }, { -12, 0, 0 }, { -18, 0, 0 }, { -25, 0, 0 } })
    eq(#legs, 2, "legs"); near(legs[1].at_m, 12, "first leg ends after >= 10 m"); near(legs[2].at_m, 25, "end")
end)

test("routeLegs: a turn starts a new leg", function()
    local legs = N.routeLegs({ { 0, 0, 0 }, { 0, 0, 5 }, { 6, 0, 5 } })
    eq(#legs, 2, "legs"); eq(legs[1].dir, "N", "first"); eq(legs[2].dir, "E", "second"); near(legs[2].at_m, 11, "total")
end)

test("routeLegs: a portal node up 9 m is a climb link (the Nautiloid ladder)", function()
    local pts = { { 0, 0, 0 }, { 0, 0, 3 }, { 0.5, 9, 3.5 }, { 6, 9, 3.5 } }
    local legs, links = N.routeLegs(pts, { 3 })
    eq(#links, 1, "links"); eq(links[1].kind, "climb", "kind"); near(links[1].dy, 9, "dy"); near(links[1].at_m, 3, "at")
    eq(#legs, 3, "walk, climb, walk"); eq(legs[2].link, "climb", "leg flag"); eq(legs[3].dir, "E", "after")
end)

test("routeLegs: a steep step down without a portal is a drop; a level portal hop is a jump", function()
    local _, links = N.routeLegs({ { 0, 0, 0 }, { 1, -3, 0 }, { 5, -3, 0 } })
    eq(links[1].kind, "drop", "drop"); near(links[1].dy, -3, "dy")
    _, links = N.routeLegs({ { 0, 0, 0 }, { 4, 0.2, 0 }, { 8, 0.2, 0 } }, { 2 })
    eq(links[1].kind, "jump", "jump"); near(links[1].m, 4, "gap")
end)

test("routeLegs: a ramp (1.5 m up over 6 m) is not a link", function()
    local _, links = N.routeLegs({ { 0, 0, 0 }, { 6, 1.5, 0 } })
    eq(#links, 0, "ramp")
end)

test("routeLegs: a long winding route is capped at ROUTE_MAX_LEGS legs", function()
    local pts = { { 0, 0, 0 } }
    for i = 1, 40 do pts[#pts + 1] = { i * 5, 0, 0 } end -- 200 m straight in 5 m steps
    local legs = N.routeLegs(pts)
    eq(#legs <= N.ROUTE_MAX_LEGS, true, "cap (" .. #legs .. ")")
end)

-- ---- levels ----------------------------------------------------------------------

test("levelsAt: floors relative to us, sorted, deduplicated, within range", function()
    heights = function() return { 9.2, 0.1, -6.1, 30, 0.0 } end
    local l = N.levelsAt(0, 0, 0)
    eq(#l, 3, "count"); near(l[1], -6.1, "below"); near(l[2], 0.1, "here"); near(l[3], 9.2, "above")
end)

-- ---- route ahead -----------------------------------------------------------------

test("routeAhead: reachable goal: legs, links, fire on the way", function()
    nodesFor = function(from, t) return { { -5, 0, 0 }, { -10, 0, 0 }, { t[1], t[2], t[3] } } end
    fire = function(x) return x < -6 and x > -8 end
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -14, 0, 0 })
    eq(r.reachable, true, "reachable"); near(r.length_m, 14, "len"); eq(r.dir, "W", "dir")
    eq(#r.legs >= 1, true, "legs"); eq(r.exposure ~= nil, true, "exposure"); eq(r.exposure.surface, "Fire", "fire")
    eq(r.connectors, nil, "no connectors when there is a path")
end)

test("routeAhead: the goal 9 m up: the spot under it, levels, the ladder that climbs there", function()
    local goal = { -10, 9, 10 }
    -- everything on the upper deck (y > 5) is unreachable from here
    blocked = function(x, y) return y > 5 end
    heights = function(x, z) if x < -4 then return { 0.0, 9.0 } end return { 0.0 } end
    item("L1", { -3, 0, 2 }, "ladder", { height = 9.2 })
    item("D1", { 8, 0, 0 }, "door", { locked = true })
    item("L2", { 2, 0, -2 }, "ladder", { visible = false, height = 9 })
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, goal)
    eq(r.reachable, false, "unreachable"); near(r.dy, 9, "dy")
    near(r.levels_goal[2], 9, "a floor 9 m up at the goal")
    -- the deck around the goal is unreachable: the walk toward it ends right under it, 9 m below
    eq(r.closest ~= nil, true, "closest"); eq(r.closest.via, "toward", "straight walk")
    near(r.closest.dy_to_goal, 9, "it is 9 m below the goal"); near(r.closest.to_goal_m, 9, "under it")
    eq(#r.connectors, 2, "the hidden ladder is not listed")
    local l = r.connectors[1]
    eq(l.uuid, "L1", "the ladder ranks first"); eq(l.leads, "up", "leads up"); near(l.height_m, 9.2, "height")
    eq(l.helps, true, "its top is nearer the goal"); near(l.path_m, 3.6, "walk to it", 0.1)
    eq(r.connectors[2].locked, true, "locked door")
end)

test("routeAhead: the goal itself is blocked (a pod): the nearest ring spot on its floor", function()
    blocked = function(x, y, z) return math.abs(x + 10) < 1 and math.abs(z) < 1 end
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -10, 0, 0 })
    eq(r.reachable, false, "unreachable"); eq(r.closest.via, "near_goal", "ring")
    near(r.closest.to_goal_m, 1.5, "first ring"); eq(r.closest.legs ~= nil, true, "legs to it")
end)

test("routeAhead: standing on top, a ladder leads down", function()
    world[P1] = { 0, 9, 0 }
    item("L1", { -2, 0, 0 }, "ladder", { height = 9 })
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { 0, 9, 5 }, true)
    eq(r.connectors[1].leads, "down", "down"); near(r.connectors[1].far_dy, -9, "far end")
end)

test("routeAhead: no ring spot reachable -> the farthest straight walk toward it", function()
    blocked = function(x) return x < -6 end
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -20, 0, 0 })
    eq(r.reachable, false, "unreachable"); eq(r.closest.via, "toward", "via")
    near(r.closest.to_goal_m, 14, "6 m along, 14 m short")
end)

test("routeAhead: the straight line's fire is reported when there is no path", function()
    blocked = function(x) return x < -3 end
    fire = function(x) return x < -4 and x > -7 end
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -10, 0, 0 })
    eq(r.straight_hazard ~= nil, true, "hazard"); eq(r.straight_hazard.surface, "Fire", "fire")
end)

test("routeAhead: beyond PATH_MAX_M: no path probe, connectors only", function()
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -200, 0, 0 })
    eq(r.too_far, true, "too far"); eq(r.reachable, nil, "not probed"); eq(type(r.connectors), "table", "connectors")
end)

test("nav.route: a pos goal; a target must be visible", function()
    local r = RPC.ops["nav.route"]({ pos = { -5, 0, 0 } })
    eq(r.reachable, true, "pos")
    item("X1", { 3, 0, 3 }, "usable", { visible = false })
    local ok, err = pcall(RPC.ops["nav.route"], { target = "X1" })
    eq(ok, false, "refused"); eq(tostring(err):find("not visible") ~= nil, true, "why")
end)

test("nav.path: route portals stay internal (not in path_preview output)", function()
    portalAt = function(n) return n[2] > 1 end
    nodesFor = function(from, t) return { { 0, 0, 2 }, { 0, 4, 3 }, { t[1], t[2], t[3] } } end
    local r = RPC.ops["nav.path"]({ pos = { 3, 4, 3 } })
    eq(r.needs_jump, true, "jump"); eq(r.portals, nil, "no portals"); eq(r.points, nil, "no points")
    local raw = N.path(P1, { 3, 4, 3 })
    eq(raw.portals[1], 3, "portal index into points")
end)

test("nav.frontier: a ladder lead carries its height and way; leads on another level carry dy", function()
    N.visited = function() return false end
    N.waypointsHere = function() return {} end
    Ext.Entity = { GetAllEntitiesWithComponent = function() return {} end }
    item("L1", { -3, 0, 2 }, "ladder", { height = 9.2 })
    item("D2", { 5, 9, 0 }, "door")
    local r = RPC.ops["nav.frontier"]({ hazards = false })
    local byId = {}
    for _, l in ipairs(r.leads) do byId[l.uuid] = l end
    near(byId.L1.height_m, 9.2, "height"); eq(byId.L1.leads, "up", "up"); eq(byId.L1.dy, nil, "same floor")
    near(byId.D2.dy, 9, "door on the deck above")
end)

-- ---- journal cache (State.lua) -----------------------------------------------------

local listeners = {}
Ext.Osiris = { RegisterListener = function(name, arity, when, fn) listeners[name] = fn end }
BG3AI.State = nil
dofile(LUA .. "/Server/State.lua")
local S = BG3AI.State

test("journal: cached until a quest event, then rebuilt", function()
    S.quests = { { id = "Q1", title = "h1", visible = true, steps = { { id = "s1", desc = "d1" } } } }
    S.journalCache = nil
    local ctx = { party = { P1 } }
    osiCalls = 0
    local a = S.journal(ctx)
    local first = osiCalls
    eq(#a, 1, "one quest"); eq(a[1].title, "T:h1", "title"); eq(first > 0, true, "walked")
    clock = clock + 100
    S.journal(ctx)
    eq(osiCalls, first, "second call is cached (no Osiris calls)")
    eq(type(listeners.QuestUpdateUnlocked), "function", "listener registered")
    listeners.QuestUpdateUnlocked("P1", "Q1", "s1")
    S.journal(ctx)
    eq(osiCalls > first, true, "a quest event rebuilds it")
end)

test("journal: the cache expires after JOURNAL_TTL_MS and is per host", function()
    S.journalCache = nil
    local ctx = { party = { P1 } }
    S.journal(ctx)
    osiCalls = 0
    clock = clock + S.JOURNAL_TTL_MS + 1
    S.journal(ctx)
    eq(osiCalls > 0, true, "expired")
    osiCalls = 0
    S.journal({ party = { "other" } })
    eq(osiCalls > 0, true, "another host")
end)

test("nav.markers objectives share the quest generation (no 2 s re-walk)", function()
    eq(N.MARKER_GEN_TTL_MS >= 10000, true, "long ttl with generations")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
