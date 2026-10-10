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
local walkblock -- (x, z) -> true: the tile carries WalkBlock (a wall's foot); nil = none
local osiCalls = 0
local searches, budget = 0, nil

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
            return { GroundSurface = fire(p[1], p[3]) and "Fire" or "None", CloudSurface = "None",
                Flags = (walkblock and walkblock(p[1], p[3])) and { "WalkBlock" } or {} }
        end,
        BeginPathfindingImmediate = mkpath,
        FindPath = function(path)
            local t = path.target
            searches = searches + 1
            -- the engine's per-frame search budget: past it a search never runs,
            -- unless IgnoreEntities sends it through the grid's own step
            if budget and searches > budget and not (path.IgnoreEntities and #path.IgnoreEntities > 0) then return false end
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
    itemKind = function(u) local it = items[u]; return it and it.kind or "item" end,
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
    searches, budget = 0, nil
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

test("routeAhead: beyond ROUTE_FAR_M: no path probe, connectors only", function()
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -500, 0, 0 })
    eq(r.too_far, true, "too far"); eq(r.reachable, nil, "not probed"); eq(type(r.connectors), "table", "connectors")
end)

local function withWaypoints(list, fn)
    local wh, wu = N.waypointsHere, N.unlockedWaypointIds
    N.waypointsHere = function() return list end
    N.unlockedWaypointIds = function() return { WAYP_A = true, WAYP_B = true } end
    local ok, err = pcall(fn)
    N.waypointsHere, N.unlockedWaypointIds = wh, wu
    if not ok then error(err, 0) end
end

test("routeAhead far (S010 Karlach 120 m): the engine's route is asked, legs only, no floors at the goal", function()
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -150, 0, 0 })
    eq(r.far, true, "far"); eq(r.too_far, nil, "probed"); eq(r.reachable, true, "reachable")
    near(r.length_m, 150, "len"); eq(#r.legs >= 1, true, "legs"); eq(r.levels_goal, nil, "no goal floors")
    eq(r.connectors, nil, "no connectors with a path")
end)

test("routeAhead far, no path: closest straight walk, unlocked waypoints by distance to the goal, nearest explored spot", function()
    blocked = function(x) return x < -20 end
    PersistentVars = { nav = { levels = { [0] = { cells = { ["-10:0:0"] = 1, ["-2:0:0"] = 1, ["5:0:5"] = 1 }, n = 3, places = {} } } } }
    withWaypoints({
        { id = "WAYP_A", name = "Grove", pos = { 50, 0, 0 } },
        { id = "WAYP_B", name = "Road", pos = { -170, 0, 10 } },
        { id = "WAYP_C", name = "Locked one", pos = { -150, 0, 0 } },
    }, function()
        local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -150, 0, 0 })
        eq(r.far, true, "far"); eq(r.reachable, false, "no path")
        eq(r.closest.via, "toward", "straight walk"); near(r.closest.to_goal_m, 130, "20 m along, 130 short")
        eq(#r.waypoints, 2, "only unlocked ones"); eq(r.waypoints[1].name, "Road", "nearest the goal first")
        near(r.waypoints[1].to_goal_m, 22.4, "road to goal", 0.1); near(r.waypoints[1].dist, 170.3, "road from here", 0.1)
        near(r.visited_near.pos[1], -38, "explored cell -10 is the nearest"); near(r.visited_near.to_goal_m, 112, "112 m short")
    end)
    PersistentVars = nil
end)

test("routeAhead far: the goal tile itself blocked (a character stands there) -> a spot 3 m around it", function()
    blocked = function(x, y, z) return math.abs(x + 150) < 0.5 and math.abs(z) < 0.5 end
    local r = N.routeAhead(BG3AI.Visibility.context(), P1, { -150, 0, 0 })
    eq(r.reachable, true, "reachable via the ring")
end)

test("nav.waypoints: unlocked ones with positions, nearest the pos first", function()
    withWaypoints({ { id = "WAYP_A", name = "Grove", pos = { 50, 0, 0 } }, { id = "WAYP_B", name = "Road", pos = { -170, 0, 10 } } }, function()
        local r = RPC.ops["nav.waypoints"]({ pos = { -150, 0, 0 } })
        eq(#r.waypoints, 2, "two"); eq(r.waypoints[1].name, "Road", "nearest first"); eq(r.here[1], 0, "here")
    end)
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
    near(byId.L1.to[2], 9.2, "leads to its top"); eq(byId.L1.to_unvisited, true, "the top is unexplored")
    near(byId.D2.dy, 9, "door on the deck above")
end)

-- ---- jump gaps in the ways (setup: jump spots were only found from screenshots) ------------------------------

local function chasm(x0, x1, below)
    heights = function(x) if x >= x0 and x <= x1 then return below and { below } or {} end return { 0.0 } end
    blocked = function(x) return x >= x0 end -- no walk reaches the far side
end

test("jumpGap: a 3 m chasm E: takeoff edge, landing, gap width", function()
    chasm(3, 5.6, -12)
    local j = N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 })
    near(j.gap_m, 3.5, "gap"); near(j.from[1], 2.5, "edge"); near(j.to[1], 6, "landing"); eq(j.dy, nil, "level")
end)

test("jumpGap: none when the far side is walkable, too wide, a wall, or a plain step down", function()
    chasm(3, 5.6, -12); blocked = function() return false end
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "a walk reaches the far side")
    chasm(3, 9.6)
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "7 m: wider than a jump")
    heights = function() return { 6.0 } end
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "a wall (no ground ahead at all)")
    heights = function(x) return { x >= 3 and -1.5 or 0.0 } end; blocked = function(x) return x >= 3 end
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "a 1.5 m step down")
end)

test("jumpGap: none at a building (live 14:15 Blighted Village: floor 4.2 m up, WalkBlock edge/landing)", function()
    -- the house: its columns carry its upper floor 4.2 m above the edge (nothing near the edge's height)
    chasm(3, 5.6, -12)
    heights = function(x) if x >= 3 and x <= 5.6 then return { 4.2, 10.5 } end return { 0.0 } end
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "a surface at head height inside the gap")
    chasm(3, 5.6, -12); walkblock = function(x) return x >= 2.5 and x < 3 end
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "the edge stands on a WalkBlock tile")
    walkblock = function(x) return x > 5.6 end
    eq(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }), nil, "the landing is a blocked tile")
    walkblock = nil
    heights = function(x) if x >= 3 and x <= 5.6 then return { -12, 9.0 } end return { 0.0 } end
    near(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }).gap_m, 3.5, "a bridge/roof high overhead does not hide a chasm")
    heights = function(x) if x >= 3 and x <= 5.6 then return { -12 } end if x > 5.6 then return { 0.0, 3.4 } end return { 0.0 } end
    near(N.jumpGap(P1, { 0, 0, 0 }, { 1, 0 }).gap_m, 3.5, "a landing under a low roof is still a landing")
end)

test("ways: a blocked direction carries the jump across the chasm; open ones don't", function()
    N.waysCache, N.waysCacheN = {}, 0
    chasm(3, 5.6, -12)
    local ways = N.ways(P1, 20, 8, 1000)
    local by = {}
    for _, w in ipairs(ways) do by[w.dir] = w end
    eq(by.E.open_m, 0, "E blocked"); near(by.E.jump.gap_m, 3.5, "E jump")
    eq(by.W.jump, nil, "W open: no jump")
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

test("journal: a sub-quest's steps are read under its parent quest (S032 probe: Ketheric's Relic)", function()
    local unlocked = { ["GLO_Moonrise:Relic_FoundGem"] = 1 }
    rawset(Osi, "QuestUpdateIsUnlocked", function(who, q, st) return unlocked[q .. ":" .. st] end)
    S.quests = { { id = "GLO_Moonrise_SUB_Relic", title = "hR", visible = true, parent = "GLO_Moonrise",
        steps = { { id = "Relic_FoundGem", desc = "dGem" }, { id = "Relic_Other", desc = "dOther" } } } }
    S.journalCache = nil
    local ok, err = pcall(function()
        local a = S.journal({ party = { P1 } })
        eq(#a, 1, "listed"); eq(#a[1].steps, 1, "the parent-unlocked step"); eq(a[1].steps[1], "T:dGem", "its text")
        eq(S.stepUnlocked(P1, { id = "Q", parent = "" }, "Relic_FoundGem"), false, "no parent: own id only")
    end)
    rawset(Osi, "QuestUpdateIsUnlocked", nil)
    S.quests, S.journalCache = nil, nil
    if not ok then error(err, 0) end
end)

test("journal: an unlocked step without QuestIsAccepted lists the quest (SHA_Nightsong 'Find the Nightsong', probe 23:3x)", function()
    local unlocked = { ["SHA_Nightsong:SearchTemple"] = 1 }
    rawset(Osi, "QuestIsAccepted", function() return 0 end)
    rawset(Osi, "QuestUpdateIsUnlocked", function(who, q, st) return unlocked[q .. ":" .. st] end)
    S.quests = { { id = "SHA_Nightsong", title = "hN", visible = true, steps = { { id = "SearchTemple", desc = "dS" } } },
        { id = "Q_Never", title = "hX", visible = true, steps = { { id = "a", desc = "dA" } } } }
    S.journalCache = nil
    local ok, err = pcall(function()
        local a = S.journal({ party = { P1 } })
        eq(#a, 1, "only the quest with an unlocked step"); eq(a[1].title, "T:hN", "Find the Nightsong")
    end)
    rawset(Osi, "QuestIsAccepted", nil); rawset(Osi, "QuestUpdateIsUnlocked", nil)
    S.quests, S.journalCache = nil, nil
    if not ok then error(err, 0) end
end)

test("nav.markers objectives share the quest generation (no 2 s re-walk)", function()
    eq(N.MARKER_GEN_TTL_MS >= 10000, true, "long ttl with generations")
end)

-- ---- travel_to legs: verified ends, alternatives --------------------------------

test("reach_toward: the leg end is the exact point searched (one search on open ground, no re-verify)", function()
    local r = RPC.ops["nav.reach_toward"]({ pos = { -20, 0, 0 }, max_m = 10, avoid = "least" })
    eq(r.reachable, true, "reachable"); eq(r.verified, true, "verified"); eq(r.alt, nil, "no alt")
    near(r.pos[1], -10, "full leg"); eq(searches, 1, "searches"); eq(r.searched, nil, "internal flag dropped")
end)

test("reach_toward: a far sample with no path falls back to the farthest nearer one (S002 leg 2)", function()
    blocked = function(x) return math.abs(x + 10) < 0.01 end
    local r = RPC.ops["nav.reach_toward"]({ pos = { -20, 0, 0 }, max_m = 10, avoid = "least" })
    eq(r.reachable, true, "reachable"); eq(r.verified, true, "verified"); near(r.pos[1], -8, "next sample in")
    eq(searches <= 4, true, "searches (farthest, then a binary search)")
end)

test("reach_toward: a good leg end is verified as is", function()
    local r = RPC.ops["nav.reach_toward"]({ pos = { -20, 0, 0 }, max_m = 10, avoid = "least" })
    eq(r.verified, true, "verified"); eq(r.alt, nil, "no alt"); near(r.pos[1], -10, "full leg")
end)

test("reach_toward alt=true after a failed walk: a leg along the engine's route, away from the failed end", function()
    local goal = { -20, 0, 0 }
    nodesFor = function(_, t)
        if t[1] == goal[1] and t[3] == goal[3] then return { { 0, 0, 5 }, { -20, 0, 5 }, goal } end
        return { t }
    end
    local r = RPC.ops["nav.reach_toward"]({ pos = goal, max_m = 10, avoid = "least", alt = true, exclude = { -10, 0, 0 } })
    eq(r.reachable, true, "reachable"); eq(r.alt, "route", "alt")
    near(r.pos[1], -5, "x along the route"); near(r.pos[3], 5, "z along the route")
end)

test("reach_toward alt=true: no other verified leg -> not reachable, alt_failed", function()
    blocked = function(x) return x < -0.5 end
    local r = RPC.ops["nav.reach_toward"]({ pos = { -20, 0, 0 }, max_m = 10, avoid = "least", alt = true, exclude = { -10, 0, 0 } })
    eq(r.reachable, false, "reachable"); eq(r.alt_failed, true, "alt_failed")
end)

-- ---- open-world navigation (S005, the beach) -------------------------------------

test("path search budget: more searches than the budget run in one frame (IgnoreEntities set)", function()
    budget = 4
    blocked = function(x) return x < -30.5 end -- the 5 farthest samples have no path
    local r = RPC.ops["nav.reach_toward"]({ dir = "W", max_m = 40, step = 2 })
    eq(r.reachable, true, "reachable"); near(r.pos[1], -30, "the farthest pathable sample")
    eq(searches > 4, true, "more than the budget ran")
end)

test("a found path that ends 3.5 m short (goal on a rock) is partial: not a sample, not verified", function()
    nodesFor = function(_, t)
        if math.abs(t[1] + 10) < 0.01 then return { { -6.5, 0, 0 } } end
        return { t }
    end
    local p = N.path(P1, { -10, 0, 0 })
    eq(p.reachable, true, "Guard still walks there"); eq(p.partial, true, "partial"); near(p.end_off, 3.5, "end_off")
    eq((N.verifyPoint(P1, { -10, 0, 0 })), false, "verify")
    local r = N.reachToward(P1, nil, { -10, 0, 0 }, 10, 2, false)
    near(r.pos[1], -8, "the last full sample, not the partial goal")
    eq((N.verifyPoint(P1, { -8, 0, 0 })), true, "a normal end ~1 m short still verifies")
end)

test("travel_to direct: a reachable goal 40 m away is one leg to it (y snapped to the floor)", function()
    nodesFor = function(_, t) return { { -10, 0, 1 }, { -30, 0, 1 }, t } end
    local r = RPC.ops["nav.reach_toward"]({ pos = { -40, 3, 0 }, max_m = 30, avoid = "least", direct = true })
    eq(r.direct, true, "direct"); eq(r.whole, true, "to the goal"); near(r.pos[1], -40, "x"); near(r.pos[2], 0, "y snapped")
end)

test("travel_to direct: 100 m away -> the farthest route node within DIRECT_LEG_M", function()
    nodesFor = function(_, t)
        local out = {}
        for x = -10, math.floor(t[1]) , -10 do out[#out + 1] = { x, 0, 0 } end
        if out[#out][1] ~= t[1] then out[#out + 1] = t end
        return out
    end
    local r = RPC.ops["nav.reach_toward"]({ pos = { -100, 0, 0 }, max_m = 30, avoid = "least", direct = true })
    eq(r.direct, true, "direct"); eq(r.whole, nil, "not the goal"); near(r.pos[1], -50, "node in reach")
    near(r.route_m, 100, "route")
end)

test("travel_to direct: fire on the route, a partial goal or no path -> the sampled legs", function()
    fire = function(x) return x < -14 and x > -16 end
    local r = RPC.ops["nav.reach_toward"]({ pos = { -40, 0, 0 }, max_m = 30, avoid = "least", direct = true })
    eq(r.direct, nil, "not direct"); eq(r.direct_why, "hazard", "why")
    reset()
    nodesFor = function(_, t) if t[1] < -39 then return { { -35, 0, 0 } } end; return { t } end
    r = RPC.ops["nav.reach_toward"]({ pos = { -40, 0, 0 }, max_m = 30, avoid = "least", direct = true })
    eq(r.direct_why, "partial", "why"); eq(r.reachable, true, "a sampled leg"); near(r.pos[1], -30, "leg")
    reset()
    r = RPC.ops["nav.reach_toward"]({ pos = { -40, 0, 0 }, max_m = 30, avoid = "least", direct = true, alt = true, exclude = { -30, 0, 0 } })
    eq(r.direct, nil, "alt never takes the direct leg")
end)


-- ---- nearby: item limit per name, fixture relation (State.lua) ---------------------

test("limitItems: 25 Candles don't push the Buttons out of the 40 slots", function()
    local its = {}
    for i = 1, 25 do its[#its + 1] = { name = "Candles", kind = "usable", d = i * 0.4 } end
    for i = 1, 20 do its[#its + 1] = { name = "Vase" .. i, kind = "usable", d = 10 + i * 0.1 } end
    its[#its + 1] = { name = "Button", kind = "usable", d = 13 }
    its[#its + 1] = { name = "Button", kind = "usable", d = 14 }
    table.sort(its, function(a, b) return a.d < b.d end)
    local got = S.limitItems(its, 40)
    eq(#got, 40, "limit")
    local buttons = 0
    for i, it in ipairs(got) do
        if it.name == "Button" then buttons = buttons + 1 end
        if i > 1 then eq(got[i - 1].d <= it.d, true, "sorted by distance") end
    end
    eq(buttons, 2, "both buttons kept")
    eq(#S.limitItems({ { name = "a", d = 1 } }, 40), 1, "under the limit: unchanged")
end)

test("fixtureRelation: a Button under a painting names it; lookalikes and far things don't", function()
    local btn = { name = "Button", p = { -286.3, -30.6, -848.8 } }
    local all = { btn,
        { name = "Moonrise Towers", p = { -286.2, -28.6, -849.2 } },
        { name = "Gargoyle Head", p = { -284.2, -30.8, -848.3 } },
        { name = "Candles", p = { -286.0, -30.6, -848.5 } }, { name = "Candles", p = { 0, 0, 0 } }, { name = "Candles", p = { 1, 0, 0 } } }
    eq(S.fixtureRelation(btn, all), "under Moonrise Towers", "painting above")
    eq(S.fixtureRelation(all[2], all), nil, "a painting is not a fixture")
    local lone = { name = "Lever", p = { 0, 0, 50 } }
    eq(S.fixtureRelation(lone, { lone, { name = "Statue", p = { 5, 0, 50 } } }), nil, "nothing within 2 m")
    local lever = { name = "Lever", p = { 0, 0, 0 } }
    eq(S.fixtureRelation(lever, { lever, { name = "Gate", p = { 1, 0, 0 } } }), "by Gate", "beside")
    -- live 11:3x: the flush button under General sits ~2.5 m off the 4 m painting helper's pivot
    local gen = { name = "Button", p = { 0, 0, 0 } }
    eq(S.fixtureRelation(gen, { gen, { name = "General", p = { 2.5, 2.0, 0 } } }), "under General", "2.5 m under a tall painting")
    eq(S.fixtureRelation(gen, { gen, { name = "General", p = { 3.5, 2.0, 0 } } }), nil, "beyond 3 m")
end)

-- ---- inventory cache + rev (State.lua) ---------------------------------------------

test("inventory: a caller holding the current rev gets {unchanged}; item events and the TTL rebuild", function()
    local ctx = BG3AI.Visibility.context()
    local a = S.inventory(ctx, {})
    eq(type(a.rev), "number", "full read carries a rev")
    eq(a.unchanged, nil, "first read is full")
    local b = S.inventory(ctx, { rev = a.rev })
    eq(b.unchanged, true, "same rev: unchanged")
    eq(b.rev, a.rev, "rev echoed")
    eq(b.gold, 0, "gold read fresh")
    eq(S.inventory(ctx, { rev = a.rev - 1 }).unchanged, nil, "an old rev gets the full list")
    S.bumpInventory()
    local c = S.inventory(ctx, { rev = a.rev })
    eq(c.unchanged, nil, "after an item event: full")
    eq(c.rev > a.rev, true, "new rev")
    eq(S.inventory(ctx, { rev = c.rev, expand = true }).unchanged, nil, "expand is never cached")
    eq(S.inventory(ctx, { rev = c.rev, filter = "key" }).unchanged, nil, "filter is never cached")
    clock = clock + S.INV_TTL_MS + 1
    eq(S.inventory(ctx, { rev = c.rev }).unchanged, nil, "TTL backstop: full")
end)

-- ---- combat order: traps that join combat pass the item rules (State.lua) ----------

test("combat: an undiscovered trap in the turn order is not listed; a discovered one is", function()
    local V = BG3AI.Visibility
    local trapE = { ServerItem = {} }
    local heroE = { CombatParticipant = {} }
    local ce = { TurnOrder = { Round = 1, CurrentGroups = { { Members = { { Entity = heroE }, { Entity = trapE } } } } } }
    heroE.CombatParticipant.CombatHandle = ce
    local saved = { V.entity, V.entityUuid, V.characterVisible, V.itemVisible }
    local discovered = false
    V.entity = function(u) return u == P1 and heroE or nil end
    V.entityUuid = function(e) return e == heroE and P1 or (e == trapE and "trap-1" or nil) end
    V.characterVisible = function() return true end -- the old path: would list the trap
    V.itemVisible = function(_, u) return u == "trap-1" and discovered end
    local oldOsi = Osi
    Osi = setmetatable({ IsInCombat = function() return 1 end }, { __index = oldOsi })
    local ok, err = pcall(function()
        local ctx = V.context()
        ctx.observers = {}
        ctx.rules = {}
        local out = S.combat(ctx)
        local names = {}
        for _, o in ipairs(out.order) do names[#names + 1] = o.uuid end
        eq(table.concat(names, ","), P1, "undiscovered trap hidden")
        eq(out.unseen_combatants, true, "counted as unseen")
        discovered = true
        out = S.combat(ctx)
        eq(#out.order, 2, "discovered trap listed")
    end)
    Osi = oldOsi
    V.entity, V.entityUuid, V.characterVisible, V.itemVisible = saved[1], saved[2], saved[3], saved[4]
    if not ok then error(err, 0) end
end)

-- ---- look_at on carried items (State.lua state.entity) ------------------------------

test("look_at: an item the party carries (also in a bag) is inspectable; another's is not", function()
    local V = BG3AI.Visibility
    local saved = { V.entity, V.entityUuid, V.itemEffects, V.itemVisible }
    local ITEM, BAGGED, BAG, FOREIGN = "i0000000-0000-0000-0000-000000000001", "i0000000-0000-0000-0000-000000000002",
        "b0000000-0000-0000-0000-000000000003", "i0000000-0000-0000-0000-000000000004"
    local NPC = "c0000000-0000-0000-0000-000000000005"
    local owner = { [ITEM] = P1, [BAGGED] = BAG, [BAG] = P1, [FOREIGN] = NPC }
    V.entity = function(u) return owner[u] and { uuid = u, ServerItem = {}, InventoryMember = {} } or nil end
    V.entityUuid = function(e) return e.uuid end
    V.itemEffects = function() return nil end
    V.itemVisible = function() return true end -- a carried item never takes the world-visibility path
    local oldOsi = Osi
    Osi = setmetatable({ GetInventoryOwner = function(u) return owner[u] and ("Name_" .. owner[u]) or nil end },
        { __index = oldOsi })
    local ok, err = pcall(function()
        eq(RPC.ops["state.entity"]({ uuid = ITEM }).uuid, ITEM, "carried")
        eq(RPC.ops["state.entity"]({ uuid = BAGGED }).uuid, BAGGED, "in a bag")
        local okf, ef = pcall(RPC.ops["state.entity"], { uuid = FOREIGN })
        eq(okf, false, "another's item refused"); eq(tostring(ef):find("not visible", 1, true) ~= nil, true, "message")
    end)
    Osi = oldOsi
    V.entity, V.entityUuid, V.itemEffects, V.itemVisible = saved[1], saved[2], saved[3], saved[4]
    if not ok then error(err, 0) end
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
