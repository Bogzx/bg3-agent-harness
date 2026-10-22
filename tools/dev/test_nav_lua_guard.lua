-- Part of test_nav_lua.lua (split for the 1000-line cap): S038 pre-checks and the level guard, S039 lower ground, NAV-2/NAV-3, fairness.
-- test_nav_lua.lua runs it after its stubs and helpers, which it shares as globals.

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

test("kill switch: SLOW_SEARCH_STRIKES searches over PATH_LEVEL_KILL_MS turn off jump planning, jumpGap and the warm-up there", function()
    onLevel("SHADOWFELL", function()
        local real = Ext.Level.FindPath
        Ext.Level.FindPath = function(path) clock = clock + N.PATH_LEVEL_KILL_MS + 10; return false end
        N.path(P1, { -9, 0, 3 })
        eq(N.levelSlow(), nil, "NAV-3: one slow search is a strike, not a kill")
        eq(N.slowHits.SHADOWFELL, 1, "one strike")
        N.frame = N.frame + 1
        N.path(P1, { -19, 0, 7 })
        Ext.Level.FindPath = real
        assert(N.levelSlow(), "killed")
        assert(N.levelSlow().why:find("2 path searches"), "why: " .. tostring(N.levelSlow().why))
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
        eq(N.guardState().inflights, nil, "marker cleared")
        eq(saved, true, "written back")
    end)
    Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile = realL, realP, realS
    N.guardReset()
    if not ok then error(err, 0) end
end)

test("kill switch: one stop during a ways warm is a strike, not a kill (04:39 relaunch put the Lower City off)", function()
    local realL, realP, realS = Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile
    local file = { slow = {}, inflight = { level = "CTY_Main_A", op = "ways warm", stuck = true } }
    Ext.IO.LoadFile = function(p) if p == N.GUARD_FILE then return "x" end end
    Ext.Json.Parse = function() return file end
    Ext.IO.SaveFile = function() end
    local ok, err = pcall(function()
        N.guardReset()
        eq(N.levelSlow("CTY_Main_A"), nil, "first stop: not killed")
        eq(N.guardState().strikes.CTY_Main_A, 1, "one strike")
        file = { slow = {}, strikes = { CTY_Main_A = 1 }, inflight = { level = "CTY_Main_A", op = "ways warm", stuck = true } }
        N.guardReset()
        local sl = N.levelSlow("CTY_Main_A")
        assert(sl and sl.why:find("stuck ways warm"), "second stop on the same level: killed")
        -- an old-rule kill is dropped at load
        file = { slow = { BGO_Main_A = { why = "the game died during ways warm", ms = -1 } }, strikes = { BGO_Main_A = 2 } }
        N.guardReset()
        eq(N.levelSlow("BGO_Main_A"), nil, "old-rule kill dropped"); eq(N.guardState().strikes.BGO_Main_A, nil, "strikes too")
    end)
    Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile = realL, realP, realS
    N.guardReset()
    if not ok then error(err, 0) end
end)

test("kill switch: a stop during a FRESH warm-up is no strike (09:08 relaunch + 09:11 eval crash put BGO_Main_A off)", function()
    local realL, realP, realS = Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile
    local file = { slow = {}, strikes = { BGO_Main_A = 1 }, inflight = { level = "BGO_Main_A", op = "ways warm" } }
    Ext.IO.LoadFile = function(p) if p == N.GUARD_FILE then return "x" end end
    Ext.Json.Parse = function() return file end
    Ext.IO.SaveFile = function() end
    local ok, err = pcall(function()
        N.guardReset()
        eq(N.levelSlow("BGO_Main_A"), nil, "not killed")
        eq(N.guardState().strikes.BGO_Main_A, 1, "no new strike")
        eq(N.guardState().inflights, nil, "marker cleared")
        -- a stuck one marks the file; N.WARM_CLEAN_RESET answered warm-ups clear an old strike
        N.inflight("ways warm", { 1, 2, 3 })
        N.inflightStuck()
        eq(N.inflightOf("ways warm").stuck, true, "stuck marked")
        N.inflight(nil)
        N.warmClean = {}
        for _ = 1, N.WARM_CLEAN_RESET - 1 do N.warmAnswered("BGO_Main_A") end
        eq(N.guardState().strikes.BGO_Main_A, 1, "not yet")
        N.warmAnswered("BGO_Main_A")
        eq(N.guardState().strikes.BGO_Main_A, nil, "cleared after healthy warm-ups")
    end)
    Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile = realL, realP, realS
    N.guardReset()
    if not ok then error(err, 0) end
end)

test("jump plan: the in-flight marker is set during its searches and cleared after", function()
    local marks = {}
    local realS, realJ = Ext.IO.SaveFile, Ext.Json.Stringify
    Ext.Json.Stringify = function(t) local k = t.inflights and next(t.inflights); return k and ("in:" .. k) or "clear" end
    Ext.IO.SaveFile = function(p, txt) if p:find("nav_guard", 1, true) then marks[#marks + 1] = txt end end
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

test("lowerGround: memoized 30 s per spot and goal; route skips it once its budget is spent (check_all 03:50, 258 ms)", function()
    withIslands(function()
        local a = N.lowerGround(P1, { 0, 0, 0 }, { -200, -140, 0 })
        assert(a and #a >= 1, "found")
        local savedH, calls = Ext.Level.GetHeightsAt, 0
        Ext.Level.GetHeightsAt = function(...) calls = calls + 1; return savedH(...) end
        local b = N.lowerGround(P1, { 0.5, 0, 0.5 }, { -200, -140, 0 })
        Ext.Level.GetHeightsAt = savedH
        eq(b, a, "same result"); eq(calls, 0, "no height reads")
        clock = clock + N.LOWER_MEMO_MS + 1
        N.lowerMemo = nil
        local savedB = N.ROUTE_BUDGET_MS
        N.ROUTE_BUDGET_MS = -1
        local savedIA = BG3AI.Visibility.itemsAround
        BG3AI.Visibility.itemsAround = function() return {} end
        local ok, r = pcall(N.routeAhead, { party = { P1 }, is_party = { [P1] = true } }, P1, { -200, -140, 0 })
        BG3AI.Visibility.itemsAround = savedIA
        N.ROUTE_BUDGET_MS = savedB
        assert(ok, tostring(r))
        if r.reachable == false then eq(r.lower_skipped, true, "skipped"); eq(r.lower, nil, "no scan") end
    end)
end)

test("lowerGround: a passed deadline stops the scan (acceptance 06:27: route 151 ms)", function()
    withIslands(function()
        local lo, why = N.lowerGround(P1, { 0, 0, 0 }, { -200, -140, 0 }, clock - 1)
        eq(lo, nil, "no partial answer"); eq(why, "budget", "says why")
        eq(N.lowerMemo, nil, "a partial scan is not memoized")
        assert(N.lowerGround(P1, { 0, 0, 0 }, { -200, -140, 0 }), "full scan without a deadline")
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

test("routeAhead: a goal in the void -> no_path_reason (S039 21:44 route (-516,-60,-1397))", function()
    local savedH = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function(x) if x < -5 then return {} end return { 0.0 } end
    local savedIA = BG3AI.Visibility.itemsAround
    BG3AI.Visibility.itemsAround = function() return {} end
    local ok, r = pcall(N.routeAhead, { party = { P1 }, is_party = { [P1] = true } }, P1, { -10, -20, 0 })
    BG3AI.Visibility.itemsAround = savedIA
    Ext.Level.GetHeightsAt = savedH
    assert(ok, tostring(r))
    eq(r.reachable, false, "no path")
    eq(r.no_path_reason, "no walkable ground at the target", "reason")
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


-- ---- NAV-2/NAV-3 (audit 2026-10-13) ------------------------------------------------------------------------------

test("NAV-2: ways on a killed level searches at most WAYS_SLOW_DIRS directions per call, no jump probes", function()
    onLevel("SHADOWFELL", function()
        N.killLevel(nil, "by hand")
        local c, restore = countingFind()
        local ok, err = pcall(function()
            blocked = function(x) return x < -5 end -- the W side is closed (a jump probe would search there)
            local ways, _, off = N.ways(P1, 20, 8, 100)
            eq(#ways, N.WAYS_SLOW_DIRS, "two directions")
            eq(off, "by hand", "the kill reason comes back")
            assert(c.n <= 2 * N.WAYS_SLOW_DIRS, "at most 2 probes per direction, got " .. c.n)
            local ways2 = N.ways(P1, 20, 8, 100)
            eq(#ways2, 2 * N.WAYS_SLOW_DIRS, "the next read adds two more (cached ones kept)")
            local V = BG3AI.Visibility
            V.itemsAround, V.charactersAround = function() return {} end, function() return {} end
            local okf, r = pcall(RPC.ops["nav.frontier"], { hazards = false })
            V.itemsAround, V.charactersAround = nil, nil
            assert(okf, "nav.frontier: " .. tostring(r))
            eq(r.ways_off, "by hand", "frontier says ways are throttled")
        end)
        restore()
        if not ok then error(err, 0) end
    end)
    onLevel("WLD_Main_A", function()
        local ways, _, off = N.ways(P1, 20, 8, 1000)
        eq(#ways, 8, "a healthy level: all eight"); eq(off, nil, "no reason")
    end)
end)

test("NAV-3: the guard file is per playthrough (host uuid); the machine-wide file only before a host exists", function()
    eq(N.guardFile(), N.GUARD_FILE, "no host")
    onLevel("WLD_Main_A", function()
        eq(N.guardFile(), "BG3AI/nav_guard_" .. P1 .. ".json", "per host")
        N.killLevel("WLD_Main_A", "x")
        assert(N.levelSlow(), "killed in this playthrough")
    end)
    eq(N.levelSlow("WLD_Main_A"), nil, "another file (no host): not killed there")
end)

test("NAV-3: a kill expires after KILL_TTL_S; an undated kill from an older file is dated at load", function()
    local realL, realP, realS, realNow = Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile, N.nowS
    local now = 1000000
    N.nowS = function() return now end
    local file = { slow = { CTY_Main_A = { why = "old", ms = 900 } }, strikes = { CTY_Main_A = 1 } }
    Ext.IO.LoadFile = function(p) if p == N.GUARD_FILE then return "x" end end
    Ext.Json.Parse = function() return file end
    Ext.IO.SaveFile = function() end
    local ok, err = pcall(function()
        N.guardReset()
        local v = N.levelSlow("CTY_Main_A")
        assert(v, "still killed"); eq(v.t, now, "dated now")
        now = now + N.KILL_TTL_S - 1
        assert(N.levelSlow("CTY_Main_A"), "not yet")
        now = now + 2
        eq(N.levelSlow("CTY_Main_A"), nil, "expired while running")
        eq(N.guardState().strikes.CTY_Main_A, nil, "its strikes too")
        file = { slow = { BGO_Main_A = { why = "old", t = now - N.KILL_TTL_S - 5 } } }
        N.guardReset()
        eq(N.levelSlow("BGO_Main_A"), nil, "expired at load")
        N.killLevel("SCL_Main_A", "new")
        eq(N.levelSlow("SCL_Main_A").t, now, "a new kill is dated")
    end)
    Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile, N.nowS = realL, realP, realS, realNow
    N.guardReset()
    if not ok then error(err, 0) end
end)

test("NAV-3: kills are dated by the SE wall clock (the game's sandbox has no os: L8 2026-10-18 undated CTY kill)", function()
    local realOs, realS = os, Ext.IO.SaveFile
    local saved
    Ext.Timer = { ClockEpoch = function() return 1760000000 end }
    Ext.IO.SaveFile = function(_, txt) saved = txt end
    os = nil
    local ok, err = pcall(function()
        N.guardReset()
        N.killLevel("CTY_Main_A", "async path searches timed out 2x")
        eq(N.levelSlow("CTY_Main_A").t, 1760000000, "dated")
        assert(saved, "saved")
    end)
    os, Ext.IO.SaveFile, Ext.Timer = realOs, realS, nil
    N.guardReset()
    if not ok then error(err, 0) end
end)

test("NAV-3: a jump marker older than JUMP_MARK_MAX_MS with the game alive is dropped (no kill at a later death)", function()
    N.inflight("jump", { 1, 2, 3 })
    N.inflightExpire(clock + 1000)
    eq(N.inflightOf("jump") and N.inflightOf("jump").op, "jump", "fresh: kept")
    N.inflightExpire(clock + N.JUMP_MARK_MAX_MS + 1)
    eq(N.inflightOf("jump"), nil, "stale: dropped")
    N.inflight("ways warm", { 1, 2, 3 })
    N.inflightExpire(clock + N.JUMP_MARK_MAX_MS + 1)
    eq(N.inflightOf("ways warm").op, "ways warm", "a warm-up marker has its own rule (stuck)")
    N.inflight(nil)
end)

test("NAV-7: a warm-up answer no longer erases a jump cast's marker (one marker per op)", function()
    N.inflight("jump", { 4, 0, 0 })
    N.inflight("ways warm", { 1, 2, 3 })
    N.inflightDone("ways warm")
    assert(N.inflightOf("jump"), "the jump marker survives the warm-up's answer")
    eq(N.inflightOf("ways warm"), nil, "the warm-up's own cleared")
    N.inflightDone("jump")
    eq(N.guardState().inflights, nil, "all clear")
end)

test("NAV-7: a crash with both a jump and a fresh warm-up in flight kills the level (jump) at the next load", function()
    local realL, realP, realS = Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile
    Ext.IO.LoadFile = function(p) if p == N.GUARD_FILE then return "x" end end
    Ext.Json.Parse = function() return { slow = {}, inflights = {
        ["ways warm"] = { level = "SCL_Main_A", op = "ways warm" }, jump = { level = "SCL_Main_A", op = "jump" } } } end
    Ext.IO.SaveFile = function() end
    local ok, err = pcall(function()
        N.guardReset()
        local s = N.levelSlow("SCL_Main_A")
        assert(s and s.why:find("died during jump"), "killed by the jump marker: " .. tostring(s and s.why))
        eq(N.guardState().inflights, nil, "markers consumed")
    end)
    Ext.IO.LoadFile, Ext.Json.Parse, Ext.IO.SaveFile = realL, realP, realS
    N.guardReset()
    if not ok then error(err, 0) end
end)


test("NAV-5: nav.route on an unreachable goal stays bounded when every search is slow (jumpAcross, walk toward)", function()
    onLevel("WLD_Main_A", function()
        local real = Ext.Level.FindPath
        local n = 0
        blocked = function(x) return x < -6 end
        Ext.Level.FindPath = function(path)
            n = n + 1
            clock = clock + 30 -- each search 30 ms (live: failing searches 20 ms .. 2 s)
            return real(path)
        end
        local V = BG3AI.Visibility
        V.itemsAround, V.charactersAround = function() return {} end, function() return {} end
        local t0 = clock
        local ok, out = pcall(N.routeAhead, V.context(), P1, { -40, 0, 0 }, false)
        V.itemsAround, V.charactersAround = nil, nil
        Ext.Level.FindPath = real
        assert(ok, tostring(out))
        eq(out.reachable, false, "no path")
        local ms = clock - t0
        assert(ms <= N.ROUTE_BUDGET_MS + N.ROUTE_CLOSEST_BUDGET_MS + 3 * 30,
            "route took " .. ms .. " ms over " .. n .. " searches")
        -- live4: the reply says where the time went; the steps add up to the whole
        local ph = out.phases
        local sum = 0
        for _, k in ipairs({ "probe", "lower", "connectors", "rest" }) do
            assert(type(ph[k]) == "number", "phase " .. k)
            sum = sum + ph[k]
        end
        eq(sum, ms, "phases add up")
        assert(ph.probe >= 30, "the slow searches are in the probe step: " .. ph.probe)
    end)
end)

test("NAV-5: reachToward stops at its deadline (unfinished, never a false 'no walkable ground')", function()
    blocked = function(x) return x < -6 end
    local real = Ext.Level.FindPath
    Ext.Level.FindPath = function(path) clock = clock + 30; return real(path) end
    local r = N.reachToward(P1, "W", nil, 20, 1, false, false, clock + 50)
    Ext.Level.FindPath = real
    assert(r.samples <= 3, "searches stop at the deadline: " .. r.samples)
    assert(r.reachable ~= false, "cut short is not 'no way'")
end)

test("NAV-5: nearestVisited parses each breadcrumb cell once", function()
    PersistentVars = { nav = { levels = { WLD_Main_A = { cells = { ["1:0:1"] = 1, ["-5:0:2"] = 1 }, n = 2, places = {} } } } }
    N.cellCenter = {}
    local a = N.nearestVisited("WLD_Main_A", { -30, 0, 10 })
    assert(a and a.pos[1] < 0, "the western cell")
    eq(type(N.cellCenter["-5:0:2"]), "table", "cached")
    local b = N.nearestVisited("WLD_Main_A", { -30, 0, 10 })
    eq(b.pos[1], a.pos[1], "same answer from the cache")
    PersistentVars = nil
end)

-- ---- fairness (audit C1, user 2026-10-18): nav answers only from ground the party sees or has walked ----------

local function noItems(fn)
    local saved = BG3AI.Visibility.itemsAround
    BG3AI.Visibility.itemsAround = function() return {} end
    local ok, err = pcall(fn)
    BG3AI.Visibility.itemsAround = saved
    PersistentVars = nil
    if not ok then error(err, 0) end
end

test("known (C1): an unseen, unwalked coordinate gets the straight line only (path_preview, route, approach)", function()
    seen = function(p) return p[1] > -10 end -- W past 10 m: behind a wall
    noItems(function()
        local r = RPC.ops["nav.path"]({ pos = { -20, 0, 0 } })
        eq(r.unknown_goal, true, "path_preview: unknown"); eq(r.reachable, nil, "no reachable")
        eq(r.length_m, nil, "no length"); near(r.straight_m, 20, "straight"); eq(r.dir, "W", "direction")
        eq(RPC.ops["nav.path"]({ pos = { -5, 0, 0 } }).reachable, true, "a spot in view: the preview")
        local ra = N.routeAhead(BG3AI.Visibility.context(), P1, { -20, 0, 0 })
        eq(ra.unknown_goal, true, "route: unknown"); eq(ra.reachable, nil, "route: no reachable")
        eq(ra.legs, nil, "no legs"); eq(ra.closest, nil, "no closest"); eq(ra.jump, nil, "no jump")
        eq(RPC.ops["nav.approach"]({ pos = { -20, 0, 0 } }).unknown_goal, true, "approach: unknown")
        local l = RPC.ops["nav.landing"]({ pos = { -20, 0, 0 } })
        eq(l.unknown_goal, true, "jump_to's landing check: unknown"); eq(l.safe, nil, "no verdict on its ground")
        -- a target the RPC proved visible needs no ray
        eq(N.routeAhead(BG3AI.Visibility.context(), P1, { -20, 0, 0 }, false, true).reachable, true, "visible target")
        -- walked there (its 4 m breadcrumb cell): known
        PersistentVars = { nav = { levels = { ["?"] = { cells = { ["-5:0:0"] = 1 }, n = 1, places = {} } } } }
        eq(N.routeAhead(BG3AI.Visibility.context(), P1, { -20, 0, 0 }).reachable, true, "walked: the route")
    end)
end)

test("known (C1): a seen spot stays known; no ray answer is not seen (fail closed)", function()
    eq(N.known(nil, { -30, 0, 0 }), true, "in view")
    seen = function() return false end
    eq(N.known(nil, { -30, 0, 0 }), true, "remembered")
    eq(N.known(nil, { -40, 0, 0 }), false, "never seen")
    seen = function() return nil end
    eq(N.known(nil, { -50, 0, 0 }), false, "rays unusable: not seen")
end)

test("known (C1/C5): walks end on known ground; travel_to's direct leg needs a known goal", function()
    seen = function(p) return p[1] > -10 end
    local r = N.reachToward(P1, "W", nil, 30, 2, false)
    eq(r.reachable, true, "walks")
    assert(r.pos[1] > -10, "stops where the view ends: " .. r.pos[1])
    local d, why = N.directLeg(P1, { -100, 0, 0 }, true)
    eq(d, nil, "no direct leg"); eq(why, "unseen", "why")
    local w = RPC.ops["nav.reach_toward"]({ pos = { -100, 0, 0 }, max_m = 30, direct = true })
    eq(w.direct, nil, "sampled legs instead"); assert(w.pos[1] > -10, "the leg ends in view: " .. w.pos[1])
end)

test("known (C1): WAYS lists a direction only as far as it is seen", function()
    seen = function(p) return p[1] > -10 end
    local ways = N.ways(P1, 20, 8, 1000)
    local W
    for _, x in ipairs(ways) do if x.dir == "W" then W = x end end
    eq(W and W.open_m, 8, "W: open as far as seen")
    seen = function(p) return p[1] > -5 end
    N.waysCache, N.waysCacheN, N.seenPts = {}, 0, {} -- another spot, nothing remembered
    for _, x in ipairs(N.ways(P1, 20, 8, 1000)) do assert(x.dir ~= "W", "W unseen: not listed") end
end)

test("route (C4): no floors under or above you or the goal", function()
    local savedH = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { -13.0, 0.0 } end -- a crypt 13 m under the floor
    local ok, err = pcall(noItems, function()
        local ra = N.routeAhead(BG3AI.Visibility.context(), P1, { -5, 0, 0 })
        eq(ra.reachable, true, "route")
        eq(ra.levels_here, nil, "no floors here"); eq(ra.levels_goal, nil, "no floors at the goal")
        eq(N.levelsAt, nil, "no floor lister")
    end)
    Ext.Level.GetHeightsAt = savedH
    if not ok then error(err, 0) end
end)

test("hazards (C19): a patch on the route is named only where seen; hazardPatches fails closed", function()
    fire = function(x) return x < -4 and x > -6 end
    seen = function(p) return not (p[1] < -4 and p[1] > -6) end -- the patch behind a pillar
    local r = RPC.ops["nav.path"]({ pos = { -8, 0, 0 } })
    eq(r.reachable, true, "path"); eq(r.hazard, nil, "unseen fire not named")
    seen = function() return true end
    N.frame = N.frame + 1
    eq(RPC.ops["nav.path"]({ pos = { -8, 0, 0 } }).hazard.surface, "Fire", "seen fire named")
    seen = function() return nil end
    eq(#N.hazardPatches(P1, 12, 1.0), 0, "no ray answer: no patch listed")
end)

test("layout: ground not seen or walked is '?', a wall shows next to seen floor", function()
    wall = function(x) return x > 3 and x < 5 end
    seen = function(p) return p[1] > -4 end
    local r = N.layout(P1, 6, 1.5)
    local mid = r.rows[r.n + 1]
    eq(mid:sub(r.n + 1, r.n + 1), ".", "our cell [" .. mid .. "]")
    eq(mid:sub(r.n - 2, r.n - 2), "?", "x=-4.5 unseen")
    eq(mid:sub(r.n + 4, r.n + 4), "#", "x=4.5 the wall's face")
    eq(mid:sub(r.n, r.n), ".", "x=-1.5 near: drawn")
end)

test("lower ground / jumps (C14/C15): only floors the party sees", function()
    withIslands(function()
        seen = function(p) return p[2] > -10 end -- the island 22 m down, in the dark: no ray reaches it
        local lo = N.lowerGround(P1, { 0, 0, 0 }, { -200, -140, 0 })
        assert(not lo or #lo == 0, "no unseen island listed")
    end)
    local savedH, savedT = Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo
    Ext.Level.GetHeightsAt = function(x)
        if x > -2.9 then return { 0.0 } end
        if x > -5.9 then return { -30.0 } end
        return { -2.0 }
    end
    Ext.Level.GetTileDebugInfo = function(p)
        local chasm = p[2] < -20
        return { GroundSurface = chasm and "Chasm" or "None", CloudSurface = "None", Flags = chasm and { "WalkBlock" } or {} }
    end
    blocked = function(x) return x < -5 end
    seen = function(p) return p[1] > -5.9 end -- the far side not in view
    local ok, err = pcall(function()
        eq(N.jumpAcross(P1, { 0, 0, 0 }, { -20, -2, 0 }), nil, "no jump to an unseen landing")
        eq(N.jumpGap(P1, { 0, 0, 0 }, { -1, 0 }), nil, "no WAYS jump to an unseen landing")
        local l = N.landing(P1, { -8, 8, 0 })
        eq(l.floors, nil, "jump_to names no unseen floor"); eq(l.suggest, nil, "nor suggests one")
    end)
    Ext.Level.GetHeightsAt, Ext.Level.GetTileDebugInfo = savedH, savedT
    if not ok then error(err, 0) end
end)
