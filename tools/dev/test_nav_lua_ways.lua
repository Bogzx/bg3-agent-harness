-- Part of test_nav_lua.lua (split for the 1000-line cap): open directions and named exits, S038 bounded engine reads.
-- test_nav_lua.lua runs it after its stubs and helpers, which it shares as globals.

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

-- naut2 runs 2/3 (Nautiloid helm): a ladder's foot and top share x/z, and the path key had only the start's x/z, so the
-- warm-up's answers from the deck below answered N.path for 30 s after the climb.
test("N.path: a warm-up answer from the floor below is not the answer after climbing a ladder (same x/z)", function()
    local t0 = clock
    N.waysWarm(clock)
    while clock - t0 < 1000 do clock = clock + 16; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock) end
    local T = { { 20, 0, 0 }, { -20, 0, 0 }, { 0, 0, 20 }, { 0, 0, -20 } }
    local warm = 0
    for _, t in ipairs(T) do if (N.path(P1, t) or {}).async then warm = warm + 1 end end
    eq(warm > 0, true, "the warm-up answered some of them below")
    world[P1] = { 0, 2.9, 0 } -- up the ladder: same x/z
    N.frame = N.frame + 1
    for _, t in ipairs(T) do eq((N.path(P1, t) or {}).async, nil, "async answer reused after the climb") end
end)

-- naut2 run 3 (Nautiloid helm): right after climbing e44 the wedged hint said "jump_to (-42.3,4.5,…) next to Lae'zel",
-- who stood at the ladder's foot 2.9 m below; the jump undid the climb (+60 s). With a goal no landing on a floor
-- farther from it; a member on the actor's own floor comes first.
test("wedged: no landing on the floor below, away from the goal (naut2 run 3); own floor first", function()
    local realH, realParty, realName = Ext.Level.GetHeightsAt, BG3AI.Visibility.party, BG3AI.Visibility.name
    local P2, P3 = "P2-laezel", "P3-gale"
    Ext.Level.GetHeightsAt = function() return { 4.5, 7.4 } end
    local party = { P1, P2 }
    BG3AI.Visibility.party = function() return party end
    BG3AI.Visibility.name = function(u) return u == P2 and "Lae'zel" or u == P3 and "Gale" or "Corran" end
    local ok, err = pcall(function()
        world[P1] = { -42.0, 7.4, -362.0 }
        world[P2] = { -42.3, 4.5, -365.0 }
        blocked = function() return true end
        local w = N.wedged(P1, 1000) -- no goal: a jump next to her, as before
        eq(w.wedged, true, "wedged"); eq(w.near, "Lae'zel", "no goal: next to her")
        w = N.wedged(P1, 1000, { -41.0, 7.7, -357.0 }) -- interact e45: the ladder's foot, on the actor's floor
        eq(w.to, nil, "no jump down"); eq(w.away, "Lae'zel", "says why"); near(w.away_dy, -2.9, "how far down")
        w = N.wedged(P1, 1000, { -31.8, 18.2, -354.8 }) -- the helm, 11 m up
        eq(w.to, nil, "no jump down toward a goal above"); eq(w.away, "Lae'zel", "away")
        w = N.wedged(P1, 1000, { -42.0, 0.0, -380.0 }) -- a goal below: her floor is on the way
        eq(w.near, "Lae'zel", "goal below"); eq(w.away, nil, "not away")
        world[P3] = { -42.0, 7.4, -367.0 } -- on the actor's floor, farther than Lae'zel
        party = { P1, P2, P3 }
        w = N.wedged(P1, 1000)
        eq(w.near, "Gale", "own floor first")
    end)
    Ext.Level.GetHeightsAt, BG3AI.Visibility.party, BG3AI.Visibility.name = realH, realParty, realName
    world[P2], world[P3] = nil, nil
    assert(ok, err)
end)

-- NAV-1: the ways cache entry (WAYS_TTL_MS) outlives N.path's async answers (ASYNC_TTL_MS); a warm-up after a long
-- stand-still used to read the stale answers as "answered" and let N.ways search them synchronously.
test("waysWarm: after standing still past WAYS_TTL_MS the re-warm asks async again, no sync FindPath", function()
    local realF, realB, sync, pumping = Ext.Level.FindPath, Ext.Level.BeginPathfindingImmediate, 0, false
    local realPump = pumpPaths
    pumpPaths = function() pumping = true; realPump(); pumping = false end
    Ext.Level.FindPath = function(path) if not pumping then sync = sync + 1 end; return realF(path) end
    Ext.Level.BeginPathfindingImmediate = function(...) sync = sync + 1; return realB(...) end
    local ok, err = pcall(function()
        local t0 = clock
        N.waysWarm(clock)
        while clock - t0 < 1000 do clock = clock + 16; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock) end
        eq(#N.ways(P1, 20, 8, 0), 8, "warm")
        eq(sync, 0, "no sync search warming")
        clock = clock + 121000 -- the party stands still (the agent thinks, a dialogue, a menu)
        local asked = #asyncQ
        for _ = 1, 20 do clock = clock + 16; N.frame = N.frame + 1; N.waysWarm(clock) end
        eq(sync, 0, "no sync search re-warming from stale answers")
        eq(#asyncQ > asked, true, "a stale answer is asked again async")
        local t1 = clock
        while clock - t1 < 1000 do clock = clock + 16; N.frame = N.frame + 1; pumpPaths(); N.waysWarm(clock) end
        eq(sync, 0, "no sync search")
        eq(#N.ways(P1, 20, 8, 0), 8, "warm again")
        eq(sync, 0, "the frontier read the fresh answers")
    end)
    Ext.Level.FindPath, Ext.Level.BeginPathfindingImmediate, pumpPaths = realF, realB, realPump
    assert(ok, err)
end)

test("ways noSearch: only async answers; a direction without one is left out and not cached", function()
    local realB, sync = Ext.Level.BeginPathfindingImmediate, 0
    Ext.Level.BeginPathfindingImmediate = function(...) sync = sync + 1; return realB(...) end
    local ok, err = pcall(function()
        local w = N.ways(P1, 20, 8, 1000, 8, true, true)
        eq(#w, 0, "nothing answered: nothing built")
        eq(sync, 0, "no sync search")
        eq(#N.ways(P1, 20, 8, 1000, 8, true), 8, "a normal call still searches them")
        eq(sync > 0, true, "searched")
    end)
    Ext.Level.BeginPathfindingImmediate = realB
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

test("layout: the Faith-Leap walkway layer is never floor, and no picture is drawn from the helpers (C3)", function()
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
        -- the floor mosaic is read off the screen (screenshot/zoom), not drawn from the hidden helpers
        eq(r.mosaic, nil, "no mosaic")
        eq(N.mosaic, nil, "no mosaic renderer")
    end)
    Ext.Level.GetHeightsAt, Ext.Entity, BG3AI.Visibility.helperItem, BG3AI.Visibility.pos = savedH, savedE, savedHelper, savedPos
    N.helperCache = {}
    if not ok then error(err, 0) end
end)

test("Faith-Leap walkway: path, walks, ways, route and travel_to's direct leg never answer from the hidden layer (C2)", function()
    reset()
    local savedE, savedHelper, savedPos = Ext.Entity, BG3AI.Visibility.helperItem, BG3AI.Visibility.pos
    -- the Faith-Leap layout: the engine mesh is one flat floor (y 0) over the chasm W of x=-9; the hidden
    -- platforms' helpers sit just under it (y -0.2) from x=-10 to -20. Before the fix every answer below came off
    -- that layer: a path along it, walk_toward/travel_to walking it, WAYS "W 20m".
    local cubes = {}
    for i = 0, 5 do cubes["H" .. i] = { -10 - 2 * i, -0.2, 0 } end
    Ext.Entity = { GetAllEntitiesWithComponent = function()
        local out = {}
        for u in pairs(cubes) do out[#out + 1] = { Uuid = { EntityUuid = u } } end
        return out
    end }
    BG3AI.Visibility.helperItem = function(u) return cubes[u] ~= nil end
    BG3AI.Visibility.pos = function(e) local p = cubes[e.uuid or e.Uuid and e.Uuid.EntityUuid] or world[e.uuid]; return p and { p[1], p[2], p[3] } end
    local savedH = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function(x, z)
        if x < -9 and math.abs(z) > 1 then return {} end -- the chasm beside the walkway: no floor
        return { 0.0 }
    end
    N.helperCache = {}
    local ok, err = pcall(function()
        -- a spot on the layer reads like the chasm: no floor there
        local p = N.path(P1, { -15, 0, 0 })
        eq(p.reachable, false, "no path onto the layer"); eq(p.no_floor, true, "no floor there, as over the chasm")
        -- a visible floor past it: the engine's way runs along the layer -> no path, nothing marked
        nodesFor = function(from, t) return { { -6, 0, 0 }, { -12, 0, 0 }, t } end
        local q = N.path(P1, { -6, 0, 5 })
        eq(q.reachable, false, "no path along the layer")
        eq(q.points, nil, "no points from it")
        nodesFor = nil
        eq(N.path(P1, { 0, 0, 5 }).reachable, true, "a walk off the layer still is one")
        local r = N.reachToward(P1, "W", nil, 20, 2, true, true)
        assert(not r.reachable or r.pos[1] > -9, "walk_toward stops at the chasm edge: " .. tostring(r.pos and r.pos[1]))
        local ways = N.ways(P1, 20, 8, 1000)
        for _, w in ipairs(ways) do
            if w.dir == "W" then assert(w.open_m < 20, "WAYS W is not open over the layer: " .. w.open_m) end
        end
        local d, why = N.directLeg(P1, { -15, 0, 0 }, true)
        eq(d, nil, "travel_to's direct leg is not taken along it")
        local savedIA = BG3AI.Visibility.itemsAround
        BG3AI.Visibility.itemsAround = function() return {} end
        local ra = N.routeAhead(BG3AI.Visibility.context(), P1, { -15, 0, 0 })
        BG3AI.Visibility.itemsAround = savedIA
        eq(ra.reachable, false, "route: no path")
        eq(ra.legs, nil, "route: no legs")
        assert(not ra.closest or ra.closest.pos[1] > -9, "route's closest spot is not on the layer")
        eq(N.helperZone({ -15, 0.1, 0 }), true, "over the chasm at the walkway's height: Guard's zone")
        eq(N.helperZone({ 0, 0, 0 }), false, "the room's floor is not")
    end)
    nodesFor = nil
    Ext.Level.GetHeightsAt = savedH
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
