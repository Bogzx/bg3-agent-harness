-- Part of test_nav_lua.lua (split for the 1000-line cap): route points, hazards, reach_toward, safe spots, crossings, jump landings, approach, losSpot, compact replies, connectors.
-- test_nav_lua.lua runs it after its stubs and helpers, which it shares as globals.

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

test("landing: with Feather Fall a floor 20 m below is a landing (S040 Shadowfell, FEATHER_FALL = no fall damage)", function()
    world[P1] = { 0, 20, 0 }
    Osi.HasActiveStatus = function(_, st) return st == "FEATHER_FALL" and 1 or 0 end
    local ok, r = pcall(N.landing, P1, { -4, 9, 0 })
    Osi.HasActiveStatus = nil
    assert(ok, tostring(r))
    eq(r.no_floor, true, "no_floor")
    assert(r.suggest and math.abs(r.suggest[2]) < 0.01, "suggests the floor 20 m down")
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

test("landing: a Chasm guess past the 4.5 m 'reliable' cap finds the floor beyond, in full reach (S040 22:03/22:10)", function()
    -- S040: jump_to (-599.5,-142,-1431) from 6.9 m away -> "no clear spot within 3m of it is in reach (4.5m)" x3,
    -- then the same jump by hand landed. Jumper on a ledge, Chasm for -7.5 < x < -2, a clear floor 4 m down beyond.
    world[P1] = { 0, 1, 0 }
    local savedH, savedT, savedG = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo, BG3AI.Guard
    Ext.Level.GetHeightsAt = function(x) if x <= -7.5 then return { -3.0 } elseif x < -2 then return { -3.0 } end return { 1.0 } end
    Ext.Level.GetTileDebugInfo = function(p)
        local void = p[1] < -2 and p[1] > -7.5
        return { GroundSurface = void and "Chasm" or "None", CloudSurface = "None", Flags = {} }
    end
    BG3AI.Guard = { jump_reach = function(c, a, b) local m = 8.5; local dr = a[2] - b[2]
        if dr > 2.3 then return m + dr, m end return m, m end }
    local ok, err = pcall(function()
        local r = N.landing(P1, { -7, -3, 0 })
        eq(r.safe, false, "safe"); eq(r.surface, "Chasm", "surface")
        assert(r.suggest, "suggests the floor past the chasm")
        assert(r.suggest[1] <= -7.5, "beyond the chasm: " .. r.suggest[1]); near(r.suggest[2], -3, "floor y")
        near(r.reach_m, 12.5, "reach grows by the 4 m drop"); eq(r.reliable_m, 4.5, "short-landing cap unchanged")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo, BG3AI.Guard = savedH, savedT, savedG
    if not ok then error(err, 0) end
end)

test("Astral Gravity (S086/S087 the Astral Prism): jump x3 + no fall damage -> ways, route and jump_to offer the island hop", function()
    -- S086-r1 05:35-05:39 / S087: "WAYS walkable: none", route "NO WALKABLE PATH ... jump_to over 10.5m of Chasm",
    -- jump_to "in Chasm ... in reach (17.7m)"; the hop the agent then made by hand landed (11 m out, 11 m down).
    -- Ledge at y 0 to x -2.9, the void (its bottom a Chasm at -35) to x -11.9, the next island 8 m down beyond.
    local savedH, savedT, savedG = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo, BG3AI.Guard
    local islandY = -8.0
    Ext.Level.GetHeightsAt = function(x)
        if x > -2.9 then return { 0.0 } elseif x > -11.9 then return { -35.0 } end
        return { islandY, -35.0 }
    end
    Ext.Level.GetTileDebugInfo = function(p)
        local chasm = p[2] < -20
        return { GroundSurface = chasm and "Chasm" or "None", CloudSurface = "None", Flags = chasm and { "WalkBlock" } or {} }
    end
    blocked = function(x) return x < -5 end
    local astral = false
    BG3AI.Guard = {
        jump_max = function() return astral and 28.5 or 9.5 end,
        jump_reach = function(c, a, b) local m = astral and 28.5 or 9.5; local dr = a[2] - b[2]
            if dr > 2.3 then return m + dr, m end return m, m end,
        jump_status = function() if astral then return 3, true end return 1, false end,
    }
    local ok, err = pcall(function()
        eq(N.jumpGap(P1, { 0, 0, 0 }, { -1, 0 }), nil, "no hop without it (4.5 m gaps, 4 m down)")
        eq(N.jumpAcross(P1, { 0, 0, 0 }, { -20, -8, 0 }), nil, "no jump across without it")
        eq(N.featherFall(P1), false, "fall damage without it")
        local r = N.landing(P1, { -13, -3, 0 })
        eq(r.suggest, nil, "a floor 8 m down is no landing without it")
        astral = true
        N.frame = N.frame + 1
        eq(N.featherFall(P1), true, "no fall damage")
        local j = N.jumpGap(P1, { 0, 0, 0 }, { -1, 0 })
        assert(j, "ways: the hop W")
        near(j.to[2], -8, "lands on the island"); eq(j.dy, -8, "8 m down")
        assert(j.to[1] <= -11.9 and j.gap_m >= 9 and j.gap_m <= 10, "over the 9 m void: " .. j.gap_m)
        local x = N.jumpAcross(P1, { 0, 0, 0 }, { -20, -8, 0 })
        assert(x, "route: the jump across")
        assert(x.from[1] > -2.9 and x.to[1] <= -11.9, "edge to island: " .. x.from[1] .. " -> " .. x.to[1])
        near(x.to[2], -8, "landing floor"); eq(x.dy, -8, "dy")
        r = N.landing(P1, { -13, -3, 0 })
        assert(r.suggest, "jump_to: the island's floor under the guessed y")
        near(r.suggest[2], -8, "floor y"); eq(r.reliable_m, 13.5, "reliable cap x3")
        r = N.landing(P1, { -6, -3, 0 })
        eq(r.safe, false, "the void is still no landing"); eq(r.suggest, nil, "nothing over the void")
        islandY = -16.0 -- deeper than the takeoff band: read lazily
        N.frame = N.frame + 1
        x = N.jumpAcross(P1, { 0, 0, 0 }, { -20, -16, 0 })
        assert(x, "route: the jump 16 m down"); near(x.to[2], -16, "deep landing floor")
        j = N.jumpGap(P1, { 0, 0, 0 }, { -1, 0 })
        assert(j, "ways: the hop 16 m down"); eq(j.dy, -16, "16 m down")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo, BG3AI.Guard = savedH, savedT, savedG
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

test("approach: a spot cut by the movement left whose own walk is longer is pulled back to fit (naut3-r3 #56)", function()
    -- the route to the target runs straight; a walk to a spot near the start bends (1.28x the straight line)
    nodesFor = function(_, t)
        if math.abs(t[1]) < 5 then return { { t[1] * 0.5, 0, t[1] * 0.4 }, t } end
        return { t }
    end
    local r = N.approach(P1, { -10, 0, 0 }, { -10, 0, 0 }, 1.5, 1.8, true)
    eq(r.limit, "movement", "limit")
    assert(r.walk_m >= 0.5 and r.walk_m < 1.8, "pulled back: " .. r.walk_m)
    local len = N.path(P1, r.pos).length_m
    assert(len <= 1.85, "its own walk fits the movement left: " .. len)
end)

test("approach: no spot toward the target fits the movement left -> walk 0 (stays put, says out of movement)", function()
    nodesFor = function(_, t)  -- a fixed 2.4 m detour round an obstacle right at the start
        if math.abs(t[1]) < 5 then return { { t[1] * 0.5, 0, 1.2 }, t } end
        return { t }
    end
    local r = N.approach(P1, { -10, 0, 0 }, { -10, 0, 0 }, 1.5, 1.8, true)
    eq(r.limit, "movement", "limit"); eq(r.walk_m, 0, "walk")
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

L1, L2, D1 = "ladder-1", "ladder-2", "door-1"
function withConnectorWorld(fn)
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
