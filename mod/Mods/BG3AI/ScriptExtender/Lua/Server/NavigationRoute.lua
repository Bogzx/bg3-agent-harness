-- Navigation, part 4 of 5 (Navigation.lua loads the parts in order; split for the 1000-line cap):
-- far routes (jump across, lower ground, route ahead), objective markers, ways and their warm-up.
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local N = BG3AI.Nav

local truthy = U.truthy
local osi = V.osi
local round = U.round
local I = N.inner
local currentLevel, DIR_VEC, floorAt, readResult = I.currentLevel, I.DIR_VEC, I.floorAt, I.readResult
local pathKey, activeChar, hdist = I.pathKey, I.activeChar, I.hdist

-- The engine's route to a far goal: the goal on its floor, else 4 spots
-- FAR_RING_M around it (a character's own tile blocks the goal itself).
local function farPath(charUuid, goal)
    local g = { goal[1], floorAt(goal[1], goal[3], goal[2]) or goal[2], goal[3] }
    local r = N.path(charUuid, g)
    if r and r.reachable ~= false then return r end
    for _, d in ipairs({ { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 } }) do
        local x, z = goal[1] + d[1] * N.FAR_RING_M, goal[3] + d[2] * N.FAR_RING_M
        local fy = floorAt(x, z, goal[2])
        if fy then
            local r2 = N.path(charUuid, { x, fy, z })
            if r2 and r2.reachable then return r2 end
        end
    end
    return r
end

-- A jump across when no walk leads to the goal (S032 17:30-17:35, Yurgir: the wiki's "jump over the broken
-- staircase", a 3 m gap 2.5 m down E of the Gauntlet's Stone Door hall; route() said only "NO WALKABLE PATH" and its
-- closest spot lay 40 m W of the stairs). A floor grid (JUMPX_CELL_M cells, <= JUMPX_R_M around the character; Chasm
-- and WalkBlock tiles are no floor) gives pairs: an edge cell, open air (no floor within the jump's height band) for
-- >= 1 cell, then a floor within reach that is nearer the goal. The few best pairs are checked with real paths: the
-- edge walkable, the landing not (else it is a walk). -> {from (takeoff), to (landing), gap_m, dy, walk_m, to_goal_m}
-- or nil. Cost: ~(2R/cell)^2 height+tile reads (~1.7k) + <= JUMPX_TRIES paths.
-- deadline (MonotonicTime ms, optional, NAV-5): the scan and the searches stop there (nil: none found in time).
N.JUMPX_R_M, N.JUMPX_CELL_M, N.JUMPX_TRIES = 30.0, 1.5, 6
function N.jumpAcross(charUuid, from, goal, deadline)
    if N.levelSlow() then return nil end -- kill switch: no extra searches on this level
    local function late() return deadline and Ext.Utils.MonotonicTime() > deadline end
    local cap = N.jumpLandCap(charUuid)
    local G = BG3AI.Guard
    if G and G.jump_max then
        local ok, m = pcall(G.jump_max, charUuid)
        if ok and type(m) == "number" then cap = math.min(cap, m) end
    end
    -- no fall damage (Feather Fall, Astral Gravity): a floor deeper than JUMP_SAFE_DROP_M across the gap is a landing
    -- too (S086 the Astral Prism: the next island 7-11 m down)
    local drop = N.safeDrop(charUuid)
    local c, R = N.JUMPX_CELL_M, N.JUMPX_R_M
    local n = math.floor(R / c)
    local takeLo = from[2] - 3 * N.JUMP_SAFE_DROP_M
    local lo, hi = from[2] - math.max(3 * N.JUMP_SAFE_DROP_M, drop), from[2] + 3 * N.JUMP_UP_MAX_M
    local grid = {}
    local function floors(i, j)
        local key = i * 4096 + j
        local f = grid[key]
        if f then return f end
        f = {}
        local x, z = from[1] + i * c, from[3] + j * c
        local ok, hs = N.floorHeights(x, z)
        for _, h in ipairs(ok and type(hs) == "table" and hs or {}) do
            if type(h) == "number" and h < takeLo and h >= lo then
                f.deep = f.deep or { x = x, z = z } -- read only when a deep landing is looked for (deepNear)
                f.deep[#f.deep + 1] = h
            elseif type(h) == "number" and h >= lo and h <= hi then
                local t = N.tileAt({ x, h, z })
                if t and t.blocked and t.surface ~= "Chasm" then f.wall = f.wall or h -- a wall's foot, not open air
                elseif not (t and (t.blocked or t.surface)) then f[#f + 1] = h end
            end
        end
        grid[key] = f
        return f
    end
    local function near(f, a, b) for _, h in ipairs(f) do if h >= a and h <= b then return h end end end
    local function deepNear(f, a, b)
        local d = f.deep
        if not d then return nil end
        for k, h in ipairs(d) do
            if h >= a and h <= b then
                if d[-k] == nil then
                    local t = N.tileAt({ d.x, h, d.z })
                    d[-k] = not (t and (t.blocked or t.surface))
                end
                if d[-k] then return h end
            end
        end
    end
    local goalD = function(x, z) return math.sqrt((x - goal[1]) ^ 2 + (z - goal[3]) ^ 2) end
    local steps = { { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 }, { 1, 1 }, { 1, -1 }, { -1, 1 }, { -1, -1 } }
    local cands = {}
    for i = -n, n do
        if late() then return nil end
        for j = -n, n do
            for _, ha in ipairs(floors(i, j)) do
                if ha >= takeLo then -- a takeoff near our height (the deep band is for landings)
                    local ax, az = from[1] + i * c, from[3] + j * c
                    for _, st in ipairs(steps) do
                        local unit = c * math.sqrt(st[1] ^ 2 + st[2] ^ 2)
                        local k, open = 1, 0
                        while k * unit <= cap + 0.01 do
                            local bi, bj = i + st[1] * k, j + st[2] * k
                            if math.abs(bi) > n or math.abs(bj) > n then break end
                            local f = floors(bi, bj)
                            local land = near(f, ha - N.JUMP_SAFE_DROP_M, ha + N.JUMP_UP_MAX_M)
                            if not land and open >= 1 and drop > N.JUMP_SAFE_DROP_M then
                                local a, b = ha - drop, ha - N.JUMP_SAFE_DROP_M
                                land = near(f, a, b) or deepNear(f, a, b)
                            end
                            if not land and (f.wall or near(f, ha + N.JUMP_UP_MAX_M, ha + N.JUMP_GAP_HEAD_M)) then
                                break -- a wall or a building in the way, not a chasm
                            end
                            if land then
                                if open >= 1 then
                                    local bx, bz = from[1] + bi * c, from[3] + bj * c
                                    local gain = goalD(ax, az) - goalD(bx, bz)
                                    if gain >= 1.0 then
                                        cands[#cands + 1] = { a = { ax, ha, az }, b = { bx, land, bz }, gap = k * unit,
                                            score = goalD(bx, bz) + 0.25 * hdist(from, { ax, ha, az }) }
                                    end
                                end
                                break
                            end
                            open = open + 1
                            k = k + 1
                        end
                    end
                end
            end
        end
    end
    if #cands == 0 then return nil end
    table.sort(cands, function(p, q) return p.score < q.score end)
    local tries, seen = 0, {}
    for _, cd in ipairs(cands) do
        if tries >= N.JUMPX_TRIES or late() then break end
        local key = math.floor(cd.b[1]) .. "," .. math.floor(cd.b[3])
        -- only a takeoff and a landing the party sees or has walked (N.known)
        if not seen[key] and N.known(nil, cd.b) and N.known(nil, cd.a) then
            seen[key] = true
            tries = tries + 1
            local rb = N.path(charUuid, cd.b)
            if not (rb and rb.reachable and not rb.partial) then
                local ra = not late() and N.path(charUuid, cd.a)
                if ra and ra.reachable and not ra.partial then
                    -- the grid's cells are 1.5 m: slide both ends to the real edges in 0.5 m steps (live 18:5x:
                    -- the grid said 4.5 m, the broken stair's gap is ~3 m)
                    local ux, uz = cd.b[1] - cd.a[1], cd.b[3] - cd.a[3]
                    local L = math.sqrt(ux * ux + uz * uz)
                    ux, uz = ux / L, uz / L
                    local function floorNear(x, z, y)
                        local okh, hs = N.floorHeights(x, z)
                        for _, h in ipairs(okh and type(hs) == "table" and hs or {}) do
                            if type(h) == "number" and math.abs(h - y) <= N.JUMP_GAP_LEVEL_M then
                                local t = N.tileAt({ x, h, z })
                                if not (t and (t.blocked or t.surface)) then return h end
                            end
                        end
                    end
                    local a, b = cd.a, cd.b
                    for _ = 1, 3 do
                        local h = floorNear(a[1] + ux * 0.5, a[3] + uz * 0.5, a[2])
                        if not h then break end
                        a = { a[1] + ux * 0.5, h, a[3] + uz * 0.5 }
                    end
                    for _ = 1, 3 do
                        local h = floorNear(b[1] - ux * 0.5, b[3] - uz * 0.5, b[2])
                        if not h or hdist(a, { b[1] - ux * 0.5, h, b[3] - uz * 0.5 }) < 1.0 then break end
                        b = { b[1] - ux * 0.5, h, b[3] - uz * 0.5 }
                    end
                    if hdist(a, b) < cd.gap then
                        local rr = N.path(charUuid, a)
                        if rr and rr.reachable and not rr.partial then ra = rr else a = cd.a end
                        cd.gap = hdist(a, b)
                    end
                    cd.a, cd.b = a, b
                    return { from = U.vec(cd.a, 1), to = U.vec(cd.b, 1), gap_m = round(cd.gap, 1),
                        dy = round(cd.b[2] - cd.a[2], 1), walk_m = ra.length_m,
                        to_goal_m = round(goalD(cd.b[1], cd.b[3]), 1) }
                end
            end
        end
    end
    return nil
end

-- Lower ground in view when the goal lies far below and no walk leads there (S038/S039, Nightsong's Prison: the
-- way down is islands 20-30 m below the arrival ledge, 15+ m out over the void; get_layout drew them blank, route
-- said only "find a ladder/ledge", and the agent spent 2 sessions on screenshots, map clicks and ~15 jump guesses).
-- A height scan of the disc around the character (no path search): the floors at our level (the ledge) and the
-- clear floors LOWER_MIN_M..LOWER_MAX_M below; for each lower floor the nearest ledge cell, both seen or walked
-- (N.known: a floor in the dark void that no ray reaches is not listed). Best per compass
-- direction (from the character to the lower floor), toward the goal first. Returns a list of
-- {dir, from (the ledge cell), to, gap_m (horizontal), drop_m, to_goal_m, toward (gets nearer the goal)} or nil.
N.LOWER_R_M = 40
N.LOWER_CELL_M = 1.5
N.LOWER_MIN_M = 10    -- a drop below this the layout's "_" and jumpGap already cover
N.LOWER_MAX_M = 120
N.LOWER_LEDGE_M = 3   -- a floor this near our height is the ledge we stand on
N.LOWER_SHOWN = 3
N.LOWER_MEMO_MS = 30000
function N.lowerGround(charUuid, from, goal, deadline)
    local n = math.floor(N.LOWER_R_M / N.LOWER_CELL_M)
    local c = N.LOWER_CELL_M
    -- memo: check_all FAIL 03:50 (Lower City, Bhaal Temple below): nav.route 258 ms, lowerGround alone ~115 ms
    local mkey = string.format("%s|%d|%d|%d|%d|%d", tostring(currentLevel()), math.floor(from[1] / 3),
        math.floor(from[3] / 3), math.floor(from[2]), goal and math.floor(goal[1] / 10) or 0, goal and math.floor(goal[3] / 10) or 0)
    local mm = N.lowerMemo
    if mm and mm.key == mkey and Ext.Utils.MonotonicTime() - mm.t < N.LOWER_MEMO_MS then return mm.v end
    local ledge, low = {}, {}
    local ledgeAt, lowAt = {}, {}
    local function clear(p)
        local t = N.tileAt(p)
        return t and not t.blocked and not t.surface
    end
    for i = -n, n do
        -- nav.route's deadline (acceptance 06:27: 151 ms with the scan finding nothing): a partial scan says so
        if deadline and Ext.Utils.MonotonicTime() > deadline then return nil, "budget" end
        for j = -n, n do
            if i * i + j * j <= n * n then
                local x, z = from[1] + i * c, from[3] + j * c
                local ok, hs = N.floorHeights(x, z)
                local lh, dh
                for _, h in ipairs(ok and type(hs) == "table" and hs or {}) do
                    if type(h) == "number" then
                        if math.abs(h - from[2]) <= N.LOWER_LEDGE_M then lh = lh or h
                        elseif from[2] - h >= N.LOWER_MIN_M and from[2] - h <= N.LOWER_MAX_M and (not dh or h > dh) then
                            dh = h -- the highest lower floor: what you'd land on first
                        end
                    end
                end
                if lh and clear({ x, lh, z }) then ledgeAt[i * 1000 + j] = { x, lh, z }
                elseif dh and not lh and clear({ x, dh, z }) then lowAt[i * 1000 + j] = { x, dh, z } end
            end
        end
    end
    -- Only edge cells matter (the narrowest gap is edge to edge): a cell with a 4-neighbour outside its own set.
    -- Matching every lower cell against every ledge cell was O(n^2) (~1e6 distances in a dense city).
    local function edges(set, outList)
        for k, p in pairs(set) do
            if not (set[k + 1] and set[k - 1] and set[k + 1000] and set[k - 1000]) then outList[#outList + 1] = p end
        end
    end
    edges(ledgeAt, ledge); edges(lowAt, low)
    if #ledge == 0 or #low == 0 then
        N.lowerMemo = { key = mkey, t = Ext.Utils.MonotonicTime(), v = nil }
        return nil
    end
    local function gd(p) return goal and hdist(p, goal) or 0 end
    local here = gd(from)
    local best = {}
    for _, b in ipairs(low) do
        local a, ad
        for _, l in ipairs(ledge) do
            local d = hdist(l, b)
            if not ad or d < ad then a, ad = l, d end
        end
        local dir = N.direction(from, b)
        local toward = gd(b) < here - 1
        local cur = best[dir]
        -- per direction: toward the goal first, then the narrowest gap; only floors the party sees (N.known)
        if (not cur or (toward and not cur.toward) or (toward == cur.toward and ad < cur.gap))
            and N.known(nil, b) and N.known(nil, a) then
            best[dir] = { dir = dir, from = a, to = b, gap = ad, toward = toward }
        end
    end
    local out = {}
    local G = BG3AI.Guard
    for _, v in pairs(best) do
        local reach
        if G and G.jump_reach then
            local okr, r = pcall(G.jump_reach, charUuid, v.from, v.to)
            if okr and type(r) == "number" then reach = round(r, 1) end
        end
        out[#out + 1] = { dir = v.dir, from = U.vec(v.from, 1), to = U.vec(v.to, 1), gap_m = round(v.gap, 1),
            drop_m = round(v.from[2] - v.to[2], 1), to_goal_m = goal and round(gd(v.to), 1) or nil,
            toward = v.toward or nil, reach_m = reach }
    end
    table.sort(out, function(p, q)
        if (p.toward or false) ~= (q.toward or false) then return p.toward == true end
        if math.abs(p.gap_m - q.gap_m) > 0.75 then return p.gap_m < q.gap_m end
        if (p.to_goal_m or 0) ~= (q.to_goal_m or 0) then return (p.to_goal_m or 0) < (q.to_goal_m or 0) end
        return p.dir < q.dir
    end)
    while #out > N.LOWER_SHOWN do table.remove(out) end
    N.lowerMemo = { key = mkey, t = Ext.Utils.MonotonicTime(), v = out }
    return out
end

-- Everything above for one goal. {from, goal, straight_m, dir, dy,
-- reachable, length_m?, needs_jump?, legs?, links?,
-- exposure?, straight_hazard?, closest?{..., legs, links}, connectors?,
-- standing_in?, far? (beyond PATH_MAX_M: probed, no detail), too_far? (beyond
-- ROUTE_FAR_M: not probed), unknown_goal? (not seen or walked, N.known: not
-- probed), waypoints? and visited_near? (far, unknown or no path), phases (ms per
-- step: probe, lower, connectors, rest; nav.route adds ways, note)}.
-- knownGoal: true for a visible target; nil asks N.known.
-- check_all FAIL 03:50 (Lower City → the Bhaal Temple far below, no walk): nav.route 258 ms = closestReach (rings +
-- the walk toward it) ~90 + lowerGround ~115 + connectors ~55. Route gives closestReach ROUTE_CLOSEST_BUDGET_MS and
-- skips the lower-ground scan once ROUTE_BUDGET_MS is spent (lower_skipped).
N.ROUTE_CLOSEST_BUDGET_MS = 80
N.ROUTE_BUDGET_MS = 110          -- the whole route (check_route: < 150 ms in the mod)
N.ROUTE_CONNECT_RESERVE_MS = 25  -- kept for the connectors' path probes after the lower-ground scan
N.ROUTE_JUMPX_BUDGET_MS = 40     -- NAV-5: jumpAcross's grid scan + its (expected-failing) landing searches
function N.routeAhead(ctx, charUuid, goal, withConnectors, knownGoal)
    local t0 = Ext.Utils.MonotonicTime()
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local out = { from = U.vec(from, 1), goal = U.vec(goal, 1), straight_m = round(U.dist(from, goal), 1),
        dir = N.direction(from, goal), dy = round(goal[2] - from[2], 1) }
    -- live4 check_all: 274 ms for an unreachable goal 133 m away, and nothing said which step took it
    local ph, last = {}, t0
    out.phases = ph
    local function lap(step)
        local t = Ext.Utils.MonotonicTime()
        ph[step] = round(t - last, 1)
        last = t
    end
    if knownGoal == nil then knownGoal = N.known(ctx, goal) end
    if U.dist(from, goal) > N.ROUTE_FAR_M then
        out.too_far = true
    elseif not knownGoal then
        out.unknown_goal = true
    elseif U.dist(from, goal) > N.PATH_MAX_M then
        out.far = true
        local r = farPath(charUuid, goal)
        if r and r.reachable == nil and r.reason then out.unknown_reason = r.reason end
        if r and r.reachable then
            out.reachable = true
            out.length_m, out.needs_jump = r.length_m, r.needs_jump
            out.legs, out.links = N.routeLegs(r.points, r.portals)
            out.drop = r.drop -- walks refuse it (S053 05:49: route said PATH, travel_to's leg "drops 8.4m")
        elseif r and r.reachable == false then
            out.reachable = false
            if (r.no_floor or r.chasm) and r.reason then out.no_path_reason = r.reason end
            local t = N.reachToward(charUuid, nil, goal, N.CONNECT_M, 2, false, nil, t0 + N.ROUTE_CLOSEST_BUDGET_MS)
            if t and t.reachable and t.pos and (t.straight_m or 0) >= 1 then
                out.closest = { pos = t.pos, path_m = t.path_m, to_goal_m = round(U.dist(t.pos, goal), 1),
                    dy_to_goal = round(goal[2] - t.pos[2], 1), via = "toward" }
            end
        end
    else
        local r = N.path(charUuid, goal)
        if r and r.reachable == nil and r.reason then out.unknown_reason = r.reason end
        -- S039 21:44: route to a point in the void said "path unknown"; the precheck knows (no ground / Chasm there)
        if r and r.reachable == false and (r.no_floor or r.chasm) and r.reason then out.no_path_reason = r.reason end
        if r and r.reachable then
            out.reachable = true
            out.length_m, out.needs_jump = r.length_m, r.needs_jump
            if r.partial then out.end_off_m = r.end_off end -- the goal itself is off the walkable ground
            out.legs, out.links = N.routeLegs(r.points, r.portals)
            out.drop = r.drop
            out.exposure = N.seenHazard(N.routeExposure(r.points))
        elseif r and r.reachable == false then
            out.reachable = false
            out.straight_hazard = N.seenHazard(N.routeExposure({ from, goal }))
            local okj, jx = pcall(N.jumpAcross, charUuid, from, goal, t0 + N.ROUTE_JUMPX_BUDGET_MS)
            if okj and jx then out.jump = jx end
            local c = N.closestReach(charUuid, goal, N.ROUTE_CLOSEST_BUDGET_MS)
            if c then
                local rc = N.path(charUuid, c.pos)
                if rc and rc.reachable then
                    c.legs, c.links = N.routeLegs(rc.points, rc.portals)
                    c.exposure = N.seenHazard(N.routeExposure(rc.points))
                end
                out.closest = c
            else
                local okw, w = pcall(N.wedged, charUuid, nil, goal)
                if okw and w then out.wedged = w end
            end
        end
    end
    lap("probe")
    local lowerBy = t0 + N.ROUTE_BUDGET_MS - N.ROUTE_CONNECT_RESERVE_MS
    if out.reachable == false and goal[2] - from[2] <= -N.LOWER_MIN_M and Ext.Utils.MonotonicTime() > lowerBy then
        out.lower_skipped = true
    elseif out.reachable == false and goal[2] - from[2] <= -N.LOWER_MIN_M then
        local okl, lower, why = pcall(N.lowerGround, charUuid, from, goal, lowerBy)
        if okl and why == "budget" then out.lower_skipped = true end
        if okl and lower and #lower > 0 then
            out.lower = lower
            if N.featherFall(charUuid) then out.feather_fall = true end -- no fall damage: route drops its warning
        end
    end
    lap("lower")
    if withConnectors or not out.reachable then
        out.connectors = N.connectors(ctx, charUuid, goal, N.CONNECT_M, N.CONNECT_MAX,
            math.max(10, t0 + N.ROUTE_BUDGET_MS - Ext.Utils.MonotonicTime()))
    end
    lap("connectors")
    if out.far or out.too_far or out.unknown_goal or out.reachable == false then
        local wps = N.unlockedWaypointsHere(goal, from)
        while #wps > N.WAYPOINTS_SHOWN do table.remove(wps) end
        if #wps > 0 then out.waypoints = wps end
        out.visited_near = N.nearestVisited(currentLevel(), goal)
    end
    local under = N.harmfulUnder(charUuid)
    if under then out.standing_in = under end
    lap("rest")
    return out
end

-- nav.waypoints {pos?}: the party's unlocked waypoints on this level with
-- positions (the map shows them), nearest pos first (travel_to's fast-travel
-- shortcut for far goals).
RPC.register("nav.waypoints", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    local e = char and V.entity(char)
    local from = e and V.pos(e)
    local goal = type(args.pos) == "table" and #args.pos >= 3 and args.pos or nil
    return { level = currentLevel(), here = from and U.vec(from, 1), waypoints = N.unlockedWaypointsHere(goal, from) }
end)

------------------------------------------------------------------------
-- Objective markers (what the map/minimap shows for tracked objectives).
-- Active objective = the objective of each accepted, open quest's latest
-- unlocked step (prototype order). Objective -> markers -> target entity
-- comes from Server/Data/JournalMarkers.lua (generated from game files).
------------------------------------------------------------------------

N.journalData = N.journalData or nil
local function journalData()
    if N.journalData == nil then
        local ok, d = pcall(Ext.Require, "Server/Data/JournalMarkers.lua")
        N.journalData = (ok and type(d) == "table") and d or false
        if not ok then Ext.Utils.PrintWarning("[BG3AI] JournalMarkers: " .. tostring(d)) end
    end
    return N.journalData or nil
end

-- quest_prototypes steps -> objective ids (parsed once, like State.journal).
N.stepObjectives = N.stepObjectives or nil
local function stepObjectives()
    if N.stepObjectives then return N.stepObjectives end
    local quests = {}
    local ok, s = pcall(Ext.IO.LoadFile, "Mods/GustavDev/Story/Journal/quest_prototypes.lsx", "data")
    if ok and s then
        local pos = 1
        while true do
            local qs = s:find('<node id="Quest">', pos, true)
            if not qs then break end
            local qe = s:find('<node id="Quest">', qs + 10, true) or (#s + 1)
            local block = s:sub(qs, qe - 1)
            local qid = block:match('id="QuestID" type="FixedString" value="([^"]*)"')
            local q = { id = qid, title = block:match('id="QuestTitle" type="TranslatedString" handle="([^"]*)"'),
                parent = block:match('id="ParentQuestID" type="FixedString" value="([^"]*)"'), steps = {} }
            for st in block:gmatch('<node id="QuestStep">(.-)</node>') do
                q.steps[#q.steps + 1] = {
                    id = st:match('id="ID" type="FixedString" value="([^"]*)"'),
                    objective = st:match('id="Objective" type="FixedString" value="([^"]*)"'),
                }
            end
            -- Fairness: technical quests the journal never shows (QuestVisiblity
            -- false, as State.journal skips them) have no map markers either.
            local hidden = block:match('id="QuestVisiblity" type="bool" value="([^"]*)"') == "false"
            if qid and not hidden then quests[#quests + 1] = q end
            pos = qe
        end
    end
    N.stepObjectives = quests
    return quests
end

-- Accepted, open quests and the objective of their latest unlocked step.
-- Walking every quest's steps costs a few hundred Osiris calls, and get_status
-- asks every turn, so the list is reused for MARKER_CACHE_MS.
-- With State's quest generation (bumped by quest listeners) the list lives
-- until a quest event, up to MARKER_GEN_TTL_MS; without it, MARKER_CACHE_MS.
N.MARKER_CACHE_MS = 2000
N.MARKER_GEN_TTL_MS = 30000
N.activeCache = N.activeCache or nil
local function activeObjectives(who)
    local now = Ext.Utils.MonotonicTime()
    local c = N.activeCache
    local S = BG3AI.State
    local gen = S and S.questGen
    local ttl = gen and N.MARKER_GEN_TTL_MS or N.MARKER_CACHE_MS
    if c and c.who == who and c.gen == gen and now - c.t < ttl then return c.list end
    local list = {}
    for _, q in ipairs(stepObjectives()) do
        -- not only accepted quests: an unlocked step is enough (State.journal: SHA_Nightsong, companion quests)
        if not truthy(osi(Osi.QuestIsClosed, q.id)) then
            local current
            for _, st in ipairs(q.steps) do
                -- a sub-quest's steps are unlocked under its parent quest (State.stepUnlocked)
                if st.id and (truthy(osi(Osi.QuestUpdateIsUnlocked, who, q.id, st.id))
                    or (q.parent and q.parent ~= "" and truthy(osi(Osi.QuestUpdateIsUnlocked, who, q.parent, st.id)))) then
                    current = st
                end
            end
            if current and current.objective then list[#list + 1] = { quest = q, objective = current.objective } end
        end
    end
    N.activeCache = { who = who, gen = gen, t = now, list = list }
    return list
end

-- Marker targets are items, characters or triggers. Triggers may lack a
-- Transform on the server, so fall back to Osiris' position query.
local function markerPos(uuid)
    if not uuid or uuid == "" then return nil end
    local te = V.entity(uuid)
    local p = te and V.pos(te)
    if p then return p end
    local ok, x, y, z = pcall(Osi.GetPosition, uuid)
    if ok and type(x) == "number" and type(y) == "number" and type(z) == "number" then return { x, y, z } end
    return nil
end

-- Active objective markers with positions (server truth). Each:
-- {label, quest, objective, kind, pos, dist, dir, level, here}
function N.objectiveMarkers(ctx)
    local data = journalData()
    if not data then return {}, "no marker index" end
    local who = ctx.party[1]
    if not who then return {} end
    local active = nil
    for _, u in ipairs(ctx.party) do
        local e = V.entity(u)
        if e and U.comp(e, "ClientControl") then active = u end
    end
    active = active or who
    local ae = V.entity(active)
    local from = ae and V.pos(ae)
    local lvl = currentLevel()
    local out = {}
    for _, a in ipairs(activeObjectives(who)) do
        local q = a.quest
        local ob = data.objectives[a.objective]
        if ob then
            for _, mid in ipairs(ob.markers) do
                local m = data.markers[mid]
                if m then
                    local o = { label = U.loca(m.text) or U.loca(ob.desc) or mid, quest = U.loca(q.title) or q.id,
                        objective = U.loca(ob.desc), kind = m.icon ~= "" and m.icon or "QuestMarker", level = m.level ~= "" and m.level or nil }
                    -- an area marker (a circle on the map), not an exact spot
                    if type(m.radius) == "number" and m.radius > 0 then o.area_m = m.radius end
                    -- the thing the marker sits on (an item or a character, not a trigger): interact(o1) acts on it
                    -- (vis V32); the bridge maps it to an id, the mod's visibility check still decides
                    if (m.type == "Item" or m.type == "Character") and m.uuid and m.uuid ~= "" then o.target = m.uuid end
                    local p = markerPos(m.uuid)
                    if p and (not o.level or o.level == lvl) then
                        o.pos = U.vec(p, 1)
                        o.here = true
                        if from then
                            o.dist = round(U.dist(from, p), 1)
                            o.dir = N.direction(from, p)
                        end
                    end
                    out[#out + 1] = o
                end
            end
        end
    end
    table.sort(out, function(a, b)
        if (a.here or false) ~= (b.here or false) then return a.here == true end
        return (a.dist or 1e9) < (b.dist or 1e9)
    end)
    return out
end

-- Items the active objectives' markers sit on: {uuid = true} (V.objectivePerceived). Rebuilt when
-- activeObjectives' cached list changes, so a scan's per-item check is a table lookup.
function N.objectiveTargets(ctx)
    local data = journalData()
    local who = ctx and ctx.party and ctx.party[1]
    if not (data and who) then return {} end
    local list = activeObjectives(who)
    local c = N.targetsCache
    if c and c.list == list then return c.set end
    local set = {}
    for _, a in ipairs(list) do
        local ob = data.objectives[a.objective]
        for _, mid in ipairs(ob and ob.markers or {}) do
            local m = data.markers[mid]
            if m and m.type == "Item" and m.uuid and m.uuid ~= "" then set[m.uuid] = true end
        end
    end
    N.targetsCache = { list = list, set = set }
    return set
end

RPC.register("nav.markers", function(args)
    local t0 = Ext.Utils.MonotonicTime()
    local list, err = N.objectiveMarkers(V.context())
    return { markers = list, error = err, ms = Ext.Utils.MonotonicTime() - t0 }
end)

-- Open directions from where the character stands: what a player reads off
-- the screen at a glance ("the road goes on north, cliffs to the west"). Live
-- S010-S015: screenshots "to find the way out / the exits" were the most common
-- reason to look at the screen. Per compass direction one path search `far` m
-- out (open ground: ~3 ms live), and only if that fails one `near` m out
-- (a blocked search costs ~15 ms). A direction counts as open when the path is
-- direct (<= DETOUR x straight), the same test walk_toward uses. budget_ms caps
-- the total; directions left unsearched are omitted (unknown).
-- -> { {dir, open_m (0 = blocked within near), dy?}, ... }, ms
-- Open directions are cached per spot (level, character, 1 m cell) and filled
-- in increments: a call searches the directions not done yet within its
-- budget, and the Tick warms the cache while the active character stands
-- still (N.waysWarm), so get_frontier / route usually answer from the cache
-- (S025/S026 live: the 8-16 searches cost ~100 ms, the largest part of a
-- 125-330 ms frontier). A result with fewer than 8 directions lists the ones
-- searched so far.
N.WAYS_DIRS = { "N", "NE", "E", "SE", "S", "SW", "W", "NW" }
N.WAYS_TTL_MS = 120000
N.WAYS_CACHE_MAX = 200
N.waysCache = N.waysCache or {}
N.waysCacheN = N.waysCacheN or 0

local function waysKey(charUuid, from, far, near)
    return table.concat({ tostring(currentLevel()), charUuid, math.floor(from[1] + 0.5), math.floor(from[2] / 2 + 0.5),
        math.floor(from[3] + 0.5), far, near }, ":")
end

-- ways(char, far, near, budgetMs, maxDirs) -> list of {dir, open_m, dy?} (searched
-- directions, compass order), elapsed ms. maxDirs caps the new searches.
-- noJump (the warm-up): no jumpGap probe (its two sync searches); the entry is marked jump_pending and the next
-- call without noJump (get_frontier, route) fills it in.
-- noSearch (the warm-up): built only from N.waysWarm's async answers (N.path noSearch); a direction with a probe
-- unanswered is left out (not cached), so the warm-up asks it again instead of searching on the server thread.
-- NAV-2: on a level the kill switch is on for (N.levelSlow) there are no warm-ups, so every direction would be a
-- cold sync search on the very grid that got the level killed: at most WAYS_SLOW_DIRS new directions per call within
-- WAYS_SLOW_BUDGET_MS, no jump probes; the 3rd value is then the kill reason (the reply says ways are throttled).
N.WAYS_SLOW_DIRS = 2
N.WAYS_SLOW_BUDGET_MS = 30
function N.ways(charUuid, far, near, budgetMs, maxDirs, noJump, noSearch)
    far, near, budgetMs = far or 20, near or 8, budgetMs or 100
    local slow = not noSearch and N.levelSlow()
    if slow then
        budgetMs = math.min(budgetMs, N.WAYS_SLOW_BUDGET_MS)
        maxDirs = math.min(maxDirs or N.WAYS_SLOW_DIRS, N.WAYS_SLOW_DIRS)
        noJump = true
    end
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil end
    local t0 = Ext.Utils.MonotonicTime()
    local key = waysKey(charUuid, from, far, near)
    local c = N.waysCache[key]
    if not c or t0 - c.t > N.WAYS_TTL_MS or (c.settling and not N.geoSettling(t0)) then
        if not c then
            N.waysCacheN = N.waysCacheN + 1
            if N.waysCacheN > N.WAYS_CACHE_MAX then N.waysCache, N.waysCacheN = {}, 1 end
        end
        -- searched while a wall/door may still be moving (V13): kept only until it has settled
        c = { t = t0, by = {}, settling = N.geoSettling(t0) or nil }
        N.waysCache[key] = c
    end
    -- true (open), false, "unseen" (a probe point the party neither sees nor has walked: N.known), or nil
    -- (noSearch: no answer yet)
    local function open(v, dd)
        local x, z = round(from[1] + v[1] * dd, 1), round(from[3] + v[2] * dd, 1)
        local fy = floorAt(x, z, from[2])
        if not N.known(nil, { x, fy or from[2], z }) then return "unseen" end
        if not fy then return false end
        local r = N.path(charUuid, { x, round(fy, 1), z }, nil, noSearch)
        if not r and noSearch then return nil end
        if r and r.reachable and not r.partial and r.length_m <= math.max(N.DETOUR * dd, dd + 2.0) then
            return true, fy - from[2]
        end
        return false
    end
    local searched = 0
    for _, d in ipairs(N.WAYS_DIRS) do
        local pend = c.by[d] and c.by[d].jump_pending and not noJump
        if pend then
            if Ext.Utils.MonotonicTime() - t0 > budgetMs then break end
            local w = c.by[d]
            w.jump_pending = nil
            local okj, j = pcall(N.jumpGap, charUuid, from, DIR_VEC[d])
            if okj and j then w.jump = j end
        elseif not c.by[d] then
            if Ext.Utils.MonotonicTime() - t0 > budgetMs or (maxDirs and searched >= maxDirs) then break end
            searched = searched + 1
            local v = DIR_VEC[d]
            local ok, dy = open(v, far)
            local m = far
            if ok == false or ok == "unseen" then
                m = near
                ok, dy = open(v, near)
                if ok == false then m = 0 end
            end
            if ok == nil then goto nextDir end
            if ok == "unseen" then c.by[d] = { dir = d, unseen = true }; goto nextDir end -- not listed
            local w = { dir = d, open_m = m }
            if dy and math.abs(dy) >= 2 then w.dy = round(dy, 1) end
            if m < far and noJump then
                w.jump_pending = true
            elseif m < far then
                local okj, j = pcall(N.jumpGap, charUuid, from, v)
                if okj and j then w.jump = j end
            end
            c.by[d] = w
        end
        ::nextDir::
    end
    local out = {}
    for _, d in ipairs(N.WAYS_DIRS) do
        local w = c.by[d]
        if w and not w.unseen then out[#out + 1] = { dir = w.dir, open_m = w.open_m, dy = w.dy, jump = w.jump } end
    end
    return out, Ext.Utils.MonotonicTime() - t0, slow and tostring(slow.why) or nil
end

-- A gap a jump crosses in a direction the walk can't go (setup's screenshot reason: ledges, jump chains, the burnt
-- bridge, S021/S023/S024): the floor heights along the line (what a player sees: the ground stops, a chasm, ground
-- again) give the edge and the far side; the edge must be walkable from here and the far side not (or only by a
-- route with a jump link). -> {gap_m, from (takeoff), to (landing), dy?} or nil. Cost: ~25 height reads + 2 paths.
N.JUMP_GAP_MIN_M = 1.0      -- narrower: a step the walk crosses anyway
N.JUMP_GAP_SCAN_M = 12.0
N.JUMP_GAP_STEP_M = 0.5
N.JUMP_GAP_LEVEL_M = 1.0    -- ground this near the edge's height continues the walk
N.JUMP_GAP_DOWN_M = 4.0     -- a landing at most this far below the edge (= JUMP_SAFE_DROP_M: what jump_to lands on)
N.JUMP_GAP_HEAD_M = 6.0     -- a surface 2.5..this above the edge inside the "gap" is a wall/building, not a chasm
-- Live 14:15 (Blighted Village, S013 save): a house read as "jump: NW gap 3m": its columns had a floor 4.2 m up
-- (the building) and the edge stood on a WalkBlock tile (the wall's foot); the walk stopped 3.6 m short and
-- jump_to called the landing a blocked tile. A chasm has nothing at head height and a walkable edge and landing.
local function tileBlocked(p)
    local t = N.tileAt(p)
    return t ~= nil and t.blocked
end
function N.jumpGap(charUuid, from, v)
    if N.levelSlow() then return nil end -- kill switch: no extra searches on this level
    local cap = N.jumpLandCap(charUuid)
    local G = BG3AI.Guard
    if G and G.jump_max then
        local ok, m = pcall(G.jump_max, charUuid)
        if ok and type(m) == "number" then cap = math.min(cap, m) end
    end
    -- no fall damage (Feather Fall, Astral Gravity): a clear floor deeper than JUMP_GAP_DOWN_M across the gap lands
    -- too (S086 the Astral Prism: "WAYS walkable: none" on every island, the next one 10-15 m out and 7-11 m down);
    -- the void's bottom is a Chasm tile, never a landing. A longer jump needs a longer scan.
    local drop = math.max(N.JUMP_GAP_DOWN_M, N.safeDrop(charUuid))
    local scan = N.JUMP_GAP_SCAN_M + math.max(0, cap - N.JUMP_LAND_CAP_M)
    local y, edge, gapAt = from[2], nil, nil
    local function heightsAt(x, z)
        local ok, hs = N.floorHeights(x, z)
        return ok and type(hs) == "table" and hs or {}
    end
    local s = N.JUMP_GAP_STEP_M
    while s <= scan do
        local x, z = from[1] + v[1] * s, from[3] + v[2] * s
        local level, land, head, deep
        for _, h in ipairs(heightsAt(x, z)) do
            if type(h) == "number" then
                if math.abs(h - y) <= N.JUMP_GAP_LEVEL_M then level = h end
                if h >= y - N.JUMP_GAP_DOWN_M and h <= y + N.JUMP_UP_MAX_M and (not land or math.abs(h - y) < math.abs(land - y)) then land = h end
                if h > y + N.JUMP_UP_MAX_M and h <= y + N.JUMP_GAP_HEAD_M then head = true end
                if gapAt and h < y - N.JUMP_GAP_DOWN_M and h >= y - drop and (not deep or h > deep) then
                    local t = N.tileAt({ x, h, z })
                    if not (t and (t.blocked or t.surface)) then deep = h end
                end
            end
        end
        -- a deep floor ends the gap like a level one, once the gap is a real one (a drop at the edge's foot is not)
        if gapAt and not land and deep and s - edge[4] >= N.JUMP_GAP_MIN_M then land = deep end
        if not gapAt then
            if level then edge, y = { x, level, z, s }, level
            elseif land then return nil -- a step up/down, not a gap
            elseif not edge then return nil -- no ground at our feet's line (a wall right here)
            elseif head or tileBlocked(edge) then return nil -- a wall/building (its foot is the "edge"), not a chasm
            else gapAt = s end
        elseif head and not land then
            return nil -- a wall or building inside the "gap", not open air
        elseif land then
            local gap = s - edge[4]
            if gap < N.JUMP_GAP_MIN_M or gap > cap + N.JUMP_GAP_STEP_M then return nil end
            local to = { x, land, z }
            if tileBlocked(to) then return nil end -- lands on a wall top or inside a building
            if not (N.known(nil, to) and N.known(nil, edge)) then return nil end -- only ground the party sees
            local re = N.path(charUuid, { edge[1], edge[2], edge[3] })
            if not (re and re.reachable and not re.partial and re.length_m <= math.max(N.DETOUR * edge[4], edge[4] + 2.0)) then return nil end
            local rl = N.path(charUuid, to)
            if rl and rl.reachable and not rl.partial and not rl.needs_jump and rl.length_m <= 2 * s + 5 then return nil end -- walkable around
            local out = { gap_m = round(gap, 1), from = U.vec({ edge[1], edge[2], edge[3] }, 1), to = U.vec(to, 1) }
            if math.abs(land - from[2]) >= 1 then out.dy = round(land - from[2], 1) end
            return out
        elseif s - gapAt > cap + N.JUMP_GAP_STEP_M then
            return nil -- wider than a jump
        end
        s = s + N.JUMP_GAP_STEP_M
    end
    return nil
end

-- Tick: while the active character has stood still WAYS_WARM_STILL_MS (out of
-- combat), warm get_frontier's ways so the next frontier/route reads them from the cache.
-- On arrival (a walk's end, a level load) all eight are warm ~0.5-1 s later, before
-- the agent's next read (~2 s of think); interiors were ~100 ms cold (BACKLOG #7).
-- S038 20:48 (se's verdict): a synchronous FindPath never returned on the Shadowfell and took the RSS to 13 GB; the
-- warm-up was one of the two searches started from that ledge. So the warm-up never searches on the server thread:
-- each probe point of a direction (20 m, then 8 m) goes out as the engine's own async request
-- (Ext.Level.BeginPathfinding, the per-frame-budgeted search NPC moves use), ONE at a time for the whole game. A
-- request unanswered after WARM_ASYNC_TIMEOUT_MS is released, which takes it off the engine's list and ends that
-- search; two of those on a level turn the level's kill switch on (N.killLevel). With every probe answered,
-- N.ways(noJump) builds the direction from those answers (no engine search). No warm-up while a job runs (a walk,
-- a jump, a cast), on a level the kill switch is on for, or over the Lua heap cap. If async answers never come at
-- all (3 timeouts, none answered ever), the warm-up is off for the session.
N.WAYS_WARM_STILL_MS = 300
N.WAYS_WARM_EVERY_MS = 60
N.WARM_ASYNC_TIMEOUT_MS = 2000
N.WARM_TIMEOUTS_KILL = 2
N.WARM_ASYNC_BROKEN_AFTER = 3
N.waysWarmState = N.waysWarmState or { t = 0 }
N.warmAsync = N.warmAsync or { ok = 0, timeouts = 0, byLevel = {}, broken = false }

local function jobRunning()
    for _, j in pairs(RPC.jobs or {}) do if j.status == "running" then return true end end
    return false
end

-- The next probe point of the first direction not warm yet at `from`, or nil when all eight are. An answer older
-- than ASYNC_TTL_MS is no answer (N.path wouldn't use it): it is dropped and asked again (NAV-1: the ways entry
-- outlives the answers, WAYS_TTL_MS > ASYNC_TTL_MS, and a re-warm after 2 min still read them as answered).
local function warmNext(active, from, c, now)
    for _, d in ipairs(N.WAYS_DIRS) do
        if not c.by[d] then
            local v = DIR_VEC[d]
            for _, dd in ipairs({ 20, 8 }) do
                local x, z = round(from[1] + v[1] * dd, 1), round(from[3] + v[2] * dd, 1)
                local fy = floorAt(x, z, from[2])
                if fy then
                    local t = { x, round(fy, 1), z }
                    local key = pathKey(active, t, from)
                    local a = N.asyncPaths[key]
                    if a and now - a.t > N.ASYNC_TTL_MS then
                        N.asyncPaths[key], N.asyncPathsN, a = nil, N.asyncPathsN - 1, nil
                    end
                    if not a then return { pos = t, key = key } end
                    local r = a.r
                    if r.reachable and not r.partial and r.length_m <= math.max(N.DETOUR * dd, dd + 2.0) then break end
                end
            end
            return { dir = d } -- answered: N.ways can build it without a search
        end
    end
    return nil
end

function N.waysWarm(now)
    local st, wa = N.waysWarmState, N.warmAsync
    local req = st.req
    if req then
        if now - req.t >= N.WARM_STUCK_MS and not req.stuck then req.stuck = true; N.inflightStuck() end
        if now - req.t <= N.WARM_ASYNC_TIMEOUT_MS then return end
        pcall(Ext.Level.ReleasePath, req.path) -- ends the engine's search of it
        st.req = nil
        wa.timeouts = wa.timeouts + 1
        local lv = tostring(req.level)
        wa.byLevel[lv] = (wa.byLevel[lv] or 0) + 1
        Ext.Utils.PrintWarning(string.format("[BG3AI] ways warm: async path to (%.1f,%.1f,%.1f) unanswered after %d ms",
            req.pos[1], req.pos[2], req.pos[3], N.WARM_ASYNC_TIMEOUT_MS))
        if wa.ok == 0 and wa.timeouts >= N.WARM_ASYNC_BROKEN_AFTER and not wa.broken then
            wa.broken = true
            Ext.Utils.PrintWarning("[BG3AI] ways warm: async path requests never answer here: warm-up off")
        elseif wa.ok > 0 and wa.byLevel[lv] >= N.WARM_TIMEOUTS_KILL then
            N.killLevel(req.level, "async path searches timed out " .. wa.byLevel[lv] .. "x")
        end
        if not st.req then N.inflightDone("ways warm") end
        return
    end
    if wa.broken or not Ext.Level.BeginPathfinding then return end
    if now - st.t < N.WAYS_WARM_EVERY_MS / 4 then return end
    st.t = now
    if Ext.Utils.GetGameState and tostring(Ext.Utils.GetGameState()) ~= "Running" then return end
    local ctx = V.context()
    if not ctx.party or #ctx.party == 0 then return end
    local active = activeChar(ctx)
    local e = active and V.entity(active)
    local p = e and V.pos(e)
    if not p then return end
    if not st.pos or st.who ~= active or U.dist(st.pos, p) > 0.3 then
        st.pos, st.who, st.since = p, active, now
        return
    end
    if now - st.since < N.WAYS_WARM_STILL_MS then return end
    if truthy(osi(Osi.IsInCombat, active)) then return end
    local Mem = BG3AI.Mem
    if Mem and Mem.paused() then return end -- over the Lua heap cap: no warm-up
    if N.levelSlow() or jobRunning() then return end
    local key = waysKey(active, p, 20, 8)
    local c = N.waysCache[key]
    if c and (now - c.t > N.WAYS_TTL_MS or (c.settling and not N.geoSettling(now))) then c = nil end
    local nxt = warmNext(active, p, c or { by = {} }, now)
    if not nxt then return end
    if nxt.dir then
        N.ways(active, 20, 8, 1000, 1, true, true) -- every probe answered: built from the async answers, never a search
        return
    end
    -- breadcrumb: S038 20:48 the server froze in a FindPath ~0.3 s after a walk ended (this, or nav.landing)
    if Mem then Mem.crumb(string.format("ways warm @(%.1f,%.1f,%.1f)", p[1], p[2], p[3])) end
    local from, target, level, gen = { p[1], p[2], p[3] }, nxt.pos, currentLevel(), N.geoGen
    local okb, path = pcall(Ext.Level.BeginPathfinding, e, target, function(pv)
        local cur = st.req
        if not cur or cur.key ~= nxt.key then return end
        st.req = nil
        N.inflightDone("ways warm")
        wa.ok = wa.ok + 1
        N.warmAnswered(level)
        local okr, r = pcall(readResult, pv, pv.GoalFound, from, target, N.PATH_END_TOL_M)
        if not okr or type(r) ~= "table" then return end
        -- asked before a wall/door changed (V13): the old geometry's answer; the warm-up asks again
        if gen ~= N.geoGen or (not r.reachable and N.geoSettling()) then return end
        r.ms = Ext.Utils.MonotonicTime() - cur.t
        if N.asyncPathsN >= N.ASYNC_MAX then N.asyncPaths, N.asyncPathsN = {}, 0 end
        N.asyncPaths[nxt.key] = { t = Ext.Utils.MonotonicTime(), r = r }
        N.asyncPathsN = N.asyncPathsN + 1
    end)
    if not okb or not path then return end
    pcall(function() path.IgnoreEntities = { e } end)
    if N.PATH_HORIZON then pcall(function() path.SearchHorizon = N.PATH_HORIZON end) end
    st.req = { path = path, key = nxt.key, pos = target, t = now, level = level }
    N.inflight("ways warm", target)
end

-- Ways on and off a level a player spots by name even where they have
-- already walked (S015: "looking for the hatch to the windmill basement").
-- Lua patterns matched at a word start ("Gatekeeper" and "Hatchet" are not exits).
N.EXIT_NAME = { "hatch%f[%A]", "hatches", "trap ?door", "stair", "ladder", "vines", "gates?%f[%A]", "passage",
    "tunnel", "entrance", "exit%f[%A]", "portal", "lift%f[%A]", "elevator" }
