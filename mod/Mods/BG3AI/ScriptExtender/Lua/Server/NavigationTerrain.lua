-- Navigation, part 3 of 5 (Navigation.lua loads the parts in order; split for the 1000-line cap):
-- safe spots, fire crossings, jumps and landings, route legs, wedges, ladders and connectors.
local U = BG3AI.Util
local V = BG3AI.Visibility
local N = BG3AI.Nav

local truthy = U.truthy
local osi = V.osi
local round = U.round
local I = N.inner
local store, DIRS, DIR_VEC, floorAt = I.store, I.DIRS, I.DIR_VEC, I.floorAt

function N.safeSpot(charUuid, maxM, toward)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local under = N.surfaceAt(from)
    if not under then return { safe = true } end
    maxM = maxM or 8
    local best, bestCost
    local d0 = toward and U.dist(from, toward)
    for _, rad in ipairs({ 1.5, 3, 5, 8, 12 }) do
        if rad > maxM + 0.01 then break end
        for _, dname in ipairs(DIRS) do
            local v = DIR_VEC[dname]
            local x, z = from[1] + v[1] * rad, from[3] + v[2] * rad
            local fy = floorAt(x, z, from[2])
            if fy and not N.surfaceNear({ x, fy, z }) then
                local r = N.path(charUuid, { x, fy, z })
                if r and r.reachable and not r.partial and r.length_m <= rad * 2 + 2 then
                    -- the start is burning anyway: only the rest must be clear;
                    -- toward: any burning metres cost CROSS_COST each instead
                    local hz, cost = nil, r.length_m
                    if toward then
                        local ex = N.routeExposure(r.points, 0.5)
                        if ex and ex.m > N.ESCAPE_MAX_EXPOSURE_M then hz = ex end
                        cost = cost + (U.dist({ x, fy, z }, toward) - d0) + N.CROSS_COST * (ex and ex.m or 0)
                    else
                        hz = N.routeHazard(r.points, math.min(rad, 2.0))
                    end
                    if not hz and (not best or cost < bestCost) then
                        best = { pos = U.vec({ x, fy, z }, 1), path_m = r.length_m, dir = dname }
                        bestCost = cost
                    end
                end
            end
        end
        if best and not toward then break end
    end
    if not best then return { safe = false, standing_in = under, found = false } end
    best.safe = false
    best.standing_in = under
    best.found = true
    return best
end

local function hdist(a, b) return math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2) end

-- The AI-grid tile under pos: {blocked, surface} (blocked: a WalkBlock flag,
-- i.e. a wall, a hole or a burning tile the pathfinder won't enter). Tiles
-- of another floor (MaxHeight far from pos.y) count as unknown (nil).
function N.tileAt(pos)
    local ok, t = pcall(Ext.Level.GetTileDebugInfo, { pos[1], pos[2], pos[3] })
    if not ok or not t then return nil end
    local hi
    pcall(function() hi = t.MaxHeight end)
    if type(hi) == "number" and math.abs(hi - pos[2]) > 3.0 then return nil end
    local blocked = false
    pcall(function()
        for _, f in pairs(t.Flags or {}) do if tostring(f) == "WalkBlock" then blocked = true end end
    end)
    return { blocked = blocked, surface = N.surfaceAt(pos) }
end

-- Least-exposure way on (travel_to when no fire-free walk leads further):
-- a fan of bearings around the goal direction x distances. A candidate must
-- end on clear ground (0.5 m margin), get >= 1 m closer to the goal and be
-- walkable, on ground the party sees or has walked (N.known: Guard's point_ok
-- refuses a move_to anywhere else; E2E run 1: 9 of 50 travel_to legs, all
-- crossing candidates, refused "not in view"); its score is progress -
-- CROSS_COST x metres of harmful surface
-- on its route - a small detour penalty. A clear detour (0 m) beats any
-- crossing that gains less than CROSS_COST m per burning metre; otherwise
-- the shortest crossing that still gets on wins (patch edges, quick hops).
-- nav.reach_toward avoid="least" also takes it over a straight walk that
-- stops short of a hazard when it scores better than that walk's gain.
-- Returns {reachable=true, pos, path_m, straight_m, dir, progress_m,
-- exposure={m, patches, longest_m, surface, pos, at_m}|nil, detour=true|nil}
-- or {reachable=false, samples}.
N.CROSS_COST = 8.0  -- a human walks ~8 m around rather than through 1 m of fire
N.CROSS_MAX_EXPOSURE_M = 8.0
N.CROSS_STEP_M = 3.0
N.CROSS_BEARINGS = { 0, 20, -20, 40, -40, 65, -65, 90, -90 }
function N.crossing(charUuid, toward, maxM)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local dx, dz = toward[1] - from[1], toward[3] - from[3]
    local len = math.sqrt(dx * dx + dz * dz)
    if len < 0.5 then return { reachable = false, samples = 0 } end
    local d0 = U.dist(from, toward)
    local skip = N.surfaceAt(from) and N.HAZARD_SKIP_M or 0.5
    local best, bestScore, tried = nil, nil, 0
    local ctx = V.context()
    local function try(x, z)
        local fy = floorAt(x, z, from[2])
        if not fy then return end
        local p = U.vec({ x, fy, z }, 1) -- the 0.1 m point handed out is the one checked
        local progress = d0 - U.dist(p, toward)
        if progress < 1.0 or N.surfaceNear(p, 0.5) or not N.known(ctx, p) then return end
        tried = tried + 1
        local dd = U.dist(from, p)
        local r = N.path(charUuid, p)
        if not (r and r.reachable and not r.partial and r.length_m <= math.max(2.5 * dd, dd + 6)) then return end
        local ex = N.routeExposure(r.points, skip)
        local exm = ex and ex.m or 0
        if exm > N.CROSS_MAX_EXPOSURE_M then return end
        local score = progress - N.CROSS_COST * exm - 0.3 * math.max(0, r.length_m - dd)
        if not best or score > bestScore then
            best, bestScore = { pos = p, path_m = r.length_m, straight_m = round(dd, 1), progress = progress,
                exposure = ex, needs_jump = r.needs_jump }, score
        end
    end
    for _, deg in ipairs(N.CROSS_BEARINGS) do
        local a = math.rad(deg)
        local vx = (dx * math.cos(a) - dz * math.sin(a)) / len
        local vz = (dx * math.sin(a) + dz * math.cos(a)) / len
        local d = 2.0
        while d <= maxM + 0.01 do
            try(from[1] + vx * d, from[3] + vz * d)
            d = d + N.CROSS_STEP_M
        end
    end
    if len <= maxM then try(toward[1], toward[3]) end
    if not best then return { reachable = false, samples = tried } end
    local out = { reachable = true, pos = U.vec(best.pos, 1), path_m = best.path_m, straight_m = best.straight_m,
        progress_m = round(best.progress, 1), dir = N.direction(from, best.pos), needs_jump = best.needs_jump,
        samples = tried, exposure = best.exposure, score = round(bestScore, 1) }
    if not best.exposure then out.detour = true end
    return out
end

-- Jump landing check (jump_to): a landing in a harmful surface, or on a
-- blocked tile, is moved to the nearest clear spot near the target within the
-- reliable jump range. Live S001: jumps of 6.1 m and 7.8 m landed 4.8 m and
-- 3 m out (the second one in Fire), so a long jump is also checked where it
-- may fall short. Returns {safe, dist_m, reliable_m, max_m?, surface?,
-- blocked?, short_pos?, suggest?, suggest_off_m?, suggest_dist_m?}.
N.JUMP_LAND_CAP_M = 4.5
N.JUMP_SAFE_DROP_M = 4.0  -- no_floor: a floor this far below the jumper is still a landing to look for
-- FEATHER_FALL = IgnoreFallDamage() (setup's live probe S039 21:42; the Shadowfell gives it to everyone): with it a
-- deep floor is a landing too (the drop is reach, not damage: S040 jumps 18-25 m down).
N.FEATHER_FALL_STATUS = "FEATHER_FALL"
N.FEATHER_SAFE_DROP_M = 60.0
-- Guard.jump_status: a status's jump multiplier and fall immunity (S086 the Astral Prism's Astral Gravity: x3, no fall
-- damage) -> mult, noFall
local function jumpStatus(charUuid)
    local G = BG3AI.Guard
    if not (G and G.jump_status) then return 1, false end
    local ok, m, nf = pcall(G.jump_status, charUuid)
    if not ok or type(m) ~= "number" then return 1, false end
    return m, nf == true
end
function N.featherFall(charUuid)
    local ok, v = pcall(Osi.HasActiveStatus, charUuid, N.FEATHER_FALL_STATUS)
    if ok and (v == 1 or v == true) then return true end
    local _, nf = jumpStatus(charUuid)
    return nf
end
-- The reliable landing distance (JUMP_LAND_CAP_M) grows with the jump multiplier: under Astral Gravity (x3) the
-- Prism's island hops are 10-15 m (S087: 15.2 m out landed on the spot).
function N.jumpLandCap(charUuid)
    local m = jumpStatus(charUuid)
    return N.JUMP_LAND_CAP_M * math.max(1, m)
end
function N.safeDrop(charUuid)
    return N.featherFall(charUuid) and N.FEATHER_SAFE_DROP_M or N.JUMP_SAFE_DROP_M
end
N.JUMP_SUGGEST_OFF_M = 3.0
function N.landing(charUuid, pos)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local G = BG3AI.Guard
    local maxM
    if G and G.jump_reach then -- the drop to the landing extends it (high ground)
        local ok, m = pcall(G.jump_reach, charUuid, from, pos)
        if ok and type(m) == "number" then maxM = m end
    elseif G and G.jump_max then
        local ok, m = pcall(G.jump_max, charUuid)
        if ok and type(m) == "number" then maxM = m end
    end
    -- cap: where a long jump may fall short (S001: a 7.8 m jump landed 4.8 m out); reach: where a landing may be
    -- (S040: jump_to 6.7 m landed on the spot, the game's own jumps 9-28 m; "no clear spot within 3m of it is in
    -- reach (4.5m)" refused 3 jumps the agent then made by hand).
    local landCap = N.jumpLandCap(charUuid)
    local cap = math.min(landCap, maxM or landCap)
    local reach = math.max(cap, maxM or cap)
    local safeDrop = N.safeDrop(charUuid)
    local d = hdist(from, pos)
    local out = { dist_m = round(d, 1), reliable_m = cap, reach_m = round(reach, 1), max_m = maxM and round(maxM, 1) or nil }
    local function bad(p)
        local s = N.surfaceNear(p, 0.5)
        if s then return s end
        local t = N.tileAt(p)
        if t and t.blocked then return "blocked" end
        return nil
    end
    -- What is at the landing's column: an agent-guessed y with no floor near
    -- it is a jump into air (S021: y 45 where the ground is 37.4; y 32.4 over
    -- a 10 m drop to 22.5).
    local okh, hs = N.floorHeights(pos[1], pos[3])
    local fl, all = {}, 0
    if okh and type(hs) == "table" then
        -- floors named or suggested: only ones the party sees or has walked (N.known)
        for _, h in ipairs(hs) do
            if type(h) == "number" then
                all = all + 1
                if N.known(nil, { pos[1], h, pos[3] }) then fl[#fl + 1] = round(h, 1) end
            end
        end
    end
    local why = bad(pos)
    if not why and all == 0 and okh then
        -- S038 20:48 / S039 21:53: a column with no AI-grid floor at all (the Shadowfell's void) was "safe", and the
        -- jump cast there froze the engine (13 GB, OOM) both times. Never a landing.
        why, out.void = "no_floor", true
    elseif not why and all > 0 and not floorAt(pos[1], pos[3], pos[2]) then
        why = "no_floor"
        table.sort(fl, function(a, b2) return math.abs(a - pos[2]) < math.abs(b2 - pos[2]) end)
        if #fl > 0 then out.floors = fl end
    end
    local slowLevel = N.levelSlow()
    if slowLevel then out.level_off = tostring(slowLevel.why) end
    -- A guessed y over a chasm with a real floor in the same column (S032 17:32 / 17:35, the Gauntlet's broken
    -- staircase, the wiki's "jump over it": y -1 and -2 asked, both "in Chasm", Yurgir parked): the Chasm is the void
    -- around the floor, so jump to that floor when it is in jumping height (up JUMP_UP_MAX_M, down JUMP_SAFE_DROP_M).
    if why and why ~= "no_floor" then
        local alt
        for _, h in ipairs(fl) do
            if math.abs(h - pos[2]) > 0.5 and h <= from[2] + N.JUMP_UP_MAX_M and h >= from[2] - safeDrop
                and not bad({ pos[1], h, pos[3] }) and (not alt or math.abs(h - pos[2]) < math.abs(alt - pos[2])) then
                alt = h
            end
        end
        if alt and d <= reach + 0.3 then
            out.safe, out.surface, out.blocked = false, why ~= "blocked" and why or nil, why == "blocked" or nil
            out.suggest, out.suggest_off_m, out.suggest_dist_m = U.vec({ pos[1], alt, pos[3] }, 1), 0, round(d, 1)
            out.floor_y = round(alt, 1)
            return out
        end
    end
    if not why and d > cap + 0.3 then
        local f = cap / d
        local sx, sz = from[1] + (pos[1] - from[1]) * f, from[3] + (pos[3] - from[3]) * f
        local sy = floorAt(sx, sz, pos[2])
        if sy then
            local sp = { sx, sy, sz }
            out.short_pos = U.vec(sp, 1)
            local s = bad(sp)
            if s and s ~= "blocked" then why = s end
        end
    end
    local tooFar = maxM and d > maxM + N.JUMP_SLACK_M
    if tooFar then out.too_far = true end
    if not why then
        out.safe = true
        if tooFar then out.plan, out.plan_skipped = N.jumpPlan(charUuid, from, pos, cap, bad) end
        return out
    end
    out.safe = false
    if why == "blocked" then out.blocked = true elseif why == "no_floor" then out.no_floor = true else out.surface = why end
    -- A guessed y with no floor: look for landings (and the plan) on the floor
    -- nearest that y instead, unless it lies more than JUMP_SAFE_DROP_M below
    -- the jumper (S024 07:14: y 10 asked, the ground there at 17, 2 m above
    -- the jumper; the reply said "pick another landing" and route() then
    -- found a plain walk to it).
    if why == "no_floor" and fl[1] and fl[1] >= from[2] - safeDrop then
        pos = { pos[1], fl[1], pos[3] }
    end
    -- the nearest clear landing to the target that this jump surely reaches
    local best, bd
    local function cand(x, z)
        local fy = floorAt(x, z, pos[2])
        if not fy or math.abs(fy - pos[2]) > 2.5 then return end
        local p = { x, fy, z }
        local r = hdist(from, p)
        if r < 1.5 or r > reach + 0.05 or bad(p) or not N.known(nil, p) then return end
        local off = hdist(p, pos)
        if not bd or off < bd then best, bd = p, off end
    end
    for _, rad in ipairs({ 0.75, 1.5, 2.25, 3.0 }) do
        for _, dname in ipairs(DIRS) do
            local v = DIR_VEC[dname]
            cand(pos[1] + v[1] * rad, pos[3] + v[2] * rad)
        end
    end
    if d > 0.1 then
        local k = math.min(d, reach)
        while k >= 1.5 do
            cand(from[1] + (pos[1] - from[1]) * k / d, from[3] + (pos[3] - from[3]) * k / d)
            k = k - 0.5
        end
    end
    if best and bd <= N.JUMP_SUGGEST_OFF_M then
        out.suggest = U.vec(best, 1)
        out.suggest_off_m = round(bd, 1)
        out.suggest_dist_m = round(hdist(from, best), 1)
    elseif best then -- too far from what was asked to swap in silently
        out.nearest_clear = U.vec(best, 1)
        out.nearest_clear_off_m = round(bd, 1)
    end
    if not out.suggest or tooFar then out.plan, out.plan_skipped = N.jumpPlan(charUuid, from, pos, cap, bad) end
    return out
end

-- A jump that works (jump_to's FAILED reply hands it out instead of "pick
-- another landing"): a clear landing at or near `pos` (<= JUMP_SUGGEST_OFF_M
-- off), then either it is plain walking (walk=true), or a clear spot to stand
-- that a walk reaches and from which the landing is within `cap`. S021: 11
-- jump_to refusals (blocked slope tiles, 8-10 m gaps) each cost the agent
-- 1-3 more model turns of guessing. Budget: N.JUMP_PLAN_TRIES path searches.
-- Returns {walk=true, land, land_off_m, path_m} | {stand, walk_m, land,
-- land_off_m, jump_m} | nil.
N.JUMP_SLACK_M = 0.5        -- Guard's own slack over jump_max
N.JUMP_PLAN_TRIES = 14
N.JUMP_PLAN_BUDGET_MS = 300 -- and this long (a failing search over a big level can take 100+ ms)
N.JUMP_UP_MAX_M = 2.5       -- UNVERIFIED: how high a jump climbs
-- On a level the kill switch turned off (N.levelSlow): nil, why (nav.landing's plan_skipped). Otherwise the plan runs
-- under the in-flight marker, so a crash inside its searches turns jump planning off on this level for good.
local jumpPlanImpl
function N.jumpPlan(charUuid, from, pos, cap, bad)
    local slow = N.levelSlow()
    if slow then return nil, "jump planning is off on this level (" .. tostring(slow.why) .. ")" end
    N.inflight("jump plan", pos)
    local ok, r = pcall(jumpPlanImpl, charUuid, from, pos, cap, bad)
    N.inflightDone("jump plan")
    if not ok then error(r, 0) end
    return r
end
jumpPlanImpl = function(charUuid, from, pos, cap, bad)
    local lands = {}
    local function addLand(x, z, off)
        local fy = floorAt(x, z, pos[2])
        if not fy or math.abs(fy - pos[2]) > 2.5 then return end
        local p = { x, fy, z }
        if bad(p) or not N.known(nil, p) then return end
        lands[#lands + 1] = { p = p, off = off }
    end
    addLand(pos[1], pos[3], 0)
    for _, rad in ipairs({ 1.0, 2.0, N.JUMP_SUGGEST_OFF_M }) do
        if #lands >= 3 then break end
        for _, dname in ipairs(DIRS) do
            local v = DIR_VEC[dname]
            addLand(pos[1] + v[1] * rad, pos[3] + v[2] * rad, rad)
        end
    end
    if #lands == 0 then return nil end
    -- nearer the target first, then nearer us
    table.sort(lands, function(a, b2)
        if a.off ~= b2.off then return a.off < b2.off end
        return hdist(from, a.p) < hdist(from, b2.p)
    end)
    local tries, deadline = 0, Ext.Utils.MonotonicTime() + N.JUMP_PLAN_BUDGET_MS
    local function walkable(p, linkOk)
        if Ext.Utils.MonotonicTime() > deadline then tries = N.JUMP_PLAN_TRIES; return nil end
        tries = tries + 1
        local r = N.path(charUuid, p)
        if r and r.reachable and not r.partial and (linkOk or not r.needs_jump) then return r end
        return nil
    end
    -- the landing itself may just be a walk (the agent jumps where a path
    -- goes); a route with a jump link is a move_to out of combat (Guard)
    local r0 = walkable(lands[1].p, true)
    if r0 then
        return { walk = true, land = U.vec(lands[1].p, 1), land_off_m = round(lands[1].off, 1), path_m = r0.length_m,
            needs_jump = r0.needs_jump }
    end
    local best
    for li = 1, math.min(#lands, 3) do
        local L = lands[li]
        local stands = {}
        for _, rad in ipairs({ cap - 0.5, cap - 1.5 }) do
            if rad >= 1.5 then
                for _, dname in ipairs(DIRS) do
                    local v = DIR_VEC[dname]
                    local x, z = L.p[1] + v[1] * rad, L.p[3] + v[2] * rad
                    local fy = floorAt(x, z, from[2]) or floorAt(x, z, L.p[2])
                    if fy and L.p[2] - fy <= N.JUMP_UP_MAX_M then
                        local sp = { x, fy, z }
                        if not bad(sp) and N.known(nil, sp) then
                            stands[#stands + 1] = { p = sp, d = hdist(from, sp), jump = rad }
                        end
                    end
                end
            end
        end
        table.sort(stands, function(a, b2) return a.d < b2.d end)
        for _, st in ipairs(stands) do
            if tries >= N.JUMP_PLAN_TRIES then break end
            local r = walkable(st.p)
            if r and (not best or r.length_m < best.walk_m) then
                best = { stand = U.vec(st.p, 1), walk_m = r.length_m, land = U.vec(L.p, 1),
                    land_off_m = round(L.off, 1), jump_m = round(st.jump, 1) }
                break -- nearest stand for this landing; a nearer landing was tried first
            end
        end
        if best or tries >= N.JUMP_PLAN_TRIES then break end
    end
    return best
end

-- Visible harmful patches around a character (get_frontier / look): grid
-- samples every `step` m within `radius` on the character's floor (+-3 m),
-- grouped 8-connected per surface. Each: {surface, dist, dir (to its
-- nearest tile), near, center, w_m (E-W), d_m (N-S), area_m2, under?}.
-- What a player sees burning/bubbling on screen; invisible surfaces are
-- never reported (surfaceAt skips them).
N.HAZARD_SCAN_M = 12
N.HAZARD_SCAN_STEP = 1.0
N.PATCH_SAMPLES = 5

-- Fairness: a patch is listed only if a party member sees part of it (sight
-- range + an eye ray, V.pointSeen) -- not fire behind a wall or in the next
-- room. Probes the nearest cell, then cells spread over the patch. Fail
-- closed: without a usable ray API (nil) a patch is not listed (walks avoid
-- patches on their own route anyway: routeHazard).
local function patchSeen(ctx, members, near)
    if not ctx then return false end
    local k = #members
    local picks = { near }
    local nS = math.min(N.PATCH_SAMPLES - 1, k)
    for s = 1, nS do picks[#picks + 1] = members[math.max(1, math.floor(s * k / nS))].p end
    for _, p in ipairs(picks) do
        if V.pointSeen(ctx, p) == true then return true end
    end
    return false
end
function N.hazardPatches(charUuid, radius, step)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    radius = radius or N.HAZARD_SCAN_M
    step = step or N.HAZARD_SCAN_STEP
    local n = math.floor(radius / step)
    -- Coarse pass on every other cell (i, j even), then a flood from each hit
    -- over its 8 neighbours: the same cells as a full scan for every patch
    -- with a 2x2-cell block (any such block holds an even cell), at ~1/4 of
    -- the probes (live: 707 probes, ~20 ms, for radius 15). A 1-cell-wide
    -- sliver can be missed; walks still check their own route (routeHazard).
    local cells, probed, queue, nprobe = {}, {}, {}, 0
    local function probe(i, j)
        local key = i .. ":" .. j
        if probed[key] or i * i + j * j > n * n then return end
        probed[key] = true
        nprobe = nprobe + 1
        local x, z = from[1] + i * step, from[3] + j * step
        local fy = floorAt(x, z, from[2])
        if fy and math.abs(fy - from[2]) <= 3.0 then
            local s = N.surfaceAt({ x, fy, z })
            if s then
                local c = { s = s, i = i, j = j, p = { x, fy, z } }
                cells[key] = c
                queue[#queue + 1] = c
            end
        end
    end
    local i0 = n % 2 == 0 and -n or -n + 1
    for i = i0, n, 2 do
        for j = i0, n, 2 do probe(i, j) end
    end
    while #queue > 0 do
        local c = table.remove(queue)
        for di = -1, 1 do
            for dj = -1, 1 do probe(c.i + di, c.j + dj) end
        end
    end
    N.lastScanProbes = nprobe
    local ctx = V.pointSeen and V.context and V.context() or nil
    local seen, patches, hidden = {}, {}, 0
    for key, c in pairs(cells) do
        if not seen[key] then
            seen[key] = true
            local stack, members = { c }, {}
            while #stack > 0 do
                local m = table.remove(stack)
                members[#members + 1] = m
                for di = -1, 1 do
                    for dj = -1, 1 do
                        local k2 = (m.i + di) .. ":" .. (m.j + dj)
                        local c2 = cells[k2]
                        if c2 and not seen[k2] and c2.s == c.s then seen[k2] = true; stack[#stack + 1] = c2 end
                    end
                end
            end
            local i0, i1, j0, j1, sx, sy, sz = math.huge, -math.huge, math.huge, -math.huge, 0, 0, 0
            local near, nd
            for _, m in ipairs(members) do
                i0, i1 = math.min(i0, m.i), math.max(i1, m.i)
                j0, j1 = math.min(j0, m.j), math.max(j1, m.j)
                sx, sy, sz = sx + m.p[1], sy + m.p[2], sz + m.p[3]
                local dd = hdist(from, m.p)
                if not nd or dd < nd then near, nd = m.p, dd end
            end
            local k = #members
            local o = { surface = c.s, dist = round(nd, 1), dir = nd > 0.5 and N.direction(from, near) or nil,
                near = U.vec(near, 1), center = U.vec({ sx / k, sy / k, sz / k }, 1),
                w_m = round((i1 - i0 + 1) * step, 1), d_m = round((j1 - j0 + 1) * step, 1),
                area_m2 = round(k * step * step, 1) }
            if nd < 0.75 then o.under = true end
            if o.under or patchSeen(ctx, members, near) then
                patches[#patches + 1] = o
            else
                hidden = hidden + 1
            end
        end
    end
    N.lastHiddenPatches = hidden
    table.sort(patches, function(a, b) return a.dist < b.dist end)
    return patches
end

------------------------------------------------------------------------
-- Route ahead (the bridge's `route` tool and travel_to/walk_toward hints):
-- what a player reads off the path preview and the scenery between here and
-- a goal, so the agent needs no screenshot (live S001: 23 screenshots and 13
-- clicks, mostly at the fire maze and the ladder). The legs of the walk, the
-- climbs/jumps/drops on it, burning stretches;
-- with no path: the walkable spot nearest the goal and the ladders/doors in
-- view, ranked by how much they help.
------------------------------------------------------------------------

N.ROUTE_LEG_M = 10       -- a leg ends after this many metres...
N.ROUTE_TURN_DEG = 35    -- ...or at a turn this sharp
N.ROUTE_MAX_LEGS = 8
N.LINK_DY_M = 1.0        -- a step this high and steeper than 1:1 is a climb/drop
N.CONNECT_M = 30         -- ladders/doors considered within 30 m
N.CONNECT_MAX = 5
N.CONNECT_PROBE = 8      -- path probes for the nearest 8 only
N.NEAR_GOAL_RINGS = { 1.5, 3, 5, 8 }
-- An unreachable ladder/door costs a failing search plus its 8-spot approach
-- ring, ~20 ms each (live 2026-10-10: one ladder 13 m away, 189 ms of game
-- thread for a route that itself took 3 ms). So connector probes share a
-- budget (the rest are listed unprobed), and an unreachable verdict is kept
-- CONNECT_FAIL_TTL_MS while the character stays within CONNECT_FAIL_MOVE_M.
N.CONNECT_BUDGET_MS = 40
N.CONNECT_FAIL_TTL_MS = 30000
N.CONNECT_FAIL_MOVE_M = 2
N.connectFail = N.connectFail or {}

local function heading(a, b)
    local dx, dz = b[1] - a[1], b[3] - a[3]
    if dx * dx + dz * dz < 0.09 then return nil end
    return math.deg(math.atan(dz, dx))
end

local function turned(h0, h1)
    if not h0 or not h1 then return false end
    local d = math.abs(h1 - h0) % 360
    if d > 180 then d = 360 - d end
    return d >= N.ROUTE_TURN_DEG
end

-- Pure (unit-tested): split a route (points from rawPath; portals = indices of
-- points reached through a jump/climb link) into legs and links. A leg ends
-- after ROUTE_LEG_M (stretched so there are at most ROUTE_MAX_LEGS), at a
-- turn >= ROUTE_TURN_DEG, or at a link. A link is a portal step or a steep
-- step (|dy| >= LINK_DY_M and steeper than 1:1): climb (up), drop (down) or
-- jump (level). legs: {{to, at_m, m, dir, dy}}; links: {{at_m, from, to, dy, m, kind}}.
function N.routeLegs(points, portals)
    local legs, links = {}, {}
    if type(points) ~= "table" or #points < 2 then return legs, links end
    local isPortal = {}
    for _, i in ipairs(portals or {}) do isPortal[i] = true end
    local total = 0
    for i = 2, #points do total = total + U.dist(points[i - 1], points[i]) end
    local legM = math.max(N.ROUTE_LEG_M, total / N.ROUTE_MAX_LEGS)
    local walked, start, startAt, h0 = 0, points[1], 0, nil
    local function close(p, at)
        if at - startAt < 0.3 then start, startAt, h0 = p, at, nil; return end
        legs[#legs + 1] = { to = U.vec(p, 1), at_m = round(at, 1), m = round(at - startAt, 1),
            dir = N.direction(start, p), dy = round(p[2] - start[2], 1) }
        start, startAt, h0 = p, at, nil
    end
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local seg, hd, dy = U.dist(a, b), hdist(a, b), b[2] - a[2]
        if isPortal[i] or (math.abs(dy) >= N.LINK_DY_M and math.abs(dy) > hd) then
            close(a, walked)
            local kind = dy >= N.LINK_DY_M and "climb" or (dy <= -N.LINK_DY_M and "drop" or "jump")
            links[#links + 1] = { at_m = round(walked, 1), from = U.vec(a, 1), to = U.vec(b, 1),
                dy = round(dy, 1), m = round(hd, 1), kind = kind }
            walked = walked + seg
            legs[#legs + 1] = { to = U.vec(b, 1), at_m = round(walked, 1), m = round(seg, 1),
                dir = N.direction(a, b), dy = round(dy, 1), link = kind }
            start, startAt, h0 = b, walked, nil
        else
            local h = heading(a, b)
            if turned(h0, h) then close(a, walked) end
            h0 = h0 or h
            walked = walked + seg
            if walked - startAt >= legM - 0.01 then close(b, walked) end
        end
    end
    close(points[#points], walked)
    return legs, links
end

-- The walkable spot nearest `goal`: rings around it (floors near its height,
-- nearest ring first), else the farthest direct walk straight toward it.
-- {pos, path_m, to_goal_m, dy_to_goal, via=near_goal|toward} or nil.
-- The ring searches stop after CLOSEST_BUDGET_MS: a failing FindPath explores
-- all the ground it can reach (S025 live: no-path routes of 1.6 and 2.3 s, the
-- game frozen meanwhile); the walk toward the goal still runs after.
N.CLOSEST_BUDGET_MS = 250
function N.closestReach(charUuid, goal, budgetMs)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil end
    local t0, budget = Ext.Utils.MonotonicTime(), budgetMs or N.CLOSEST_BUDGET_MS
    for _, rad in ipairs(N.NEAR_GOAL_RINGS) do
        if Ext.Utils.MonotonicTime() - t0 > budget then break end
        local best, bd
        for _, dname in ipairs(DIRS) do
            local v = DIR_VEC[dname]
            local x, z = goal[1] + v[1] * rad, goal[3] + v[2] * rad
            local fy = Ext.Utils.MonotonicTime() - t0 <= budget and floorAt(x, z, goal[2])
            if fy then
                local p = { x, fy, z }
                local r = N.path(charUuid, p)
                if r and r.reachable and not r.partial then
                    local d = U.dist(p, goal)
                    if not bd or d < bd then best, bd = { pos = p, path_m = r.length_m }, d end
                end
            end
        end
        if best then
            return { pos = U.vec(best.pos, 1), path_m = best.path_m, to_goal_m = round(bd, 1),
                dy_to_goal = round(goal[2] - best.pos[2], 1), via = "near_goal" }
        end
    end
    -- a caller with a tighter budget (nav.route) gets a coarser walk toward it (half the path searches)
    -- NAV-5: the walk toward it is bounded too (half the budget past what the rings left; one search may overrun)
    local step = budget < N.CLOSEST_BUDGET_MS and 4 or 2
    local by = math.max(t0 + budget, Ext.Utils.MonotonicTime() + budget / 2)
    local r = N.reachToward(charUuid, nil, goal, math.min(U.dist(from, goal), N.CONNECT_M), step, false, nil, by)
    if r and r.reachable and r.pos and (r.straight_m or 0) >= 1 then
        return { pos = r.pos, path_m = r.path_m, to_goal_m = round(U.dist(r.pos, goal), 1),
            dy_to_goal = round(goal[2] - r.pos[2], 1), via = "toward" }
    end
    return nil
end

-- Wedged (S058 08:00 Wyrm's Rock mess hall): after a pick_up off a table Claude stood in a nook the AI grid
-- doesn't connect to; interact, travel_to, walk_toward and route all said "no walkable path" (WAYS none, no closest
-- spot) and the agent guessed a jump out next to Wyll after 6 calls (~35 s). Wedged = no floor WEDGE_RING_M around
-- is walkable; the way out is a jump next to the nearest party member in WEDGE_PARTY_M (they stand on walkable
-- ground). nil = not wedged (or unknown: out of budget); {wedged=true, to?, near?, dist_m?, in_reach?}.
N.WEDGE_RING_M = 3.0
N.WEDGE_PARTY_M = 12.0
N.WEDGE_SHORT_M = 1.2 -- land this far short of the member (not on top of them)
N.WEDGE_BUDGET_MS = 80
-- naut2 run 3 (Nautiloid helm): right after the climb up e44 the hint said "jump_to (-42.3,4.5,…) next to Lae'zel":
-- she still stood at the ladder's foot, 2.9 m below, and the goal (the helm) was 11 m up; the jump undid the climb
-- and cost ~60 s. The landing is next to a member on the actor's own floor first; with a goal (optional) never next
-- to one on another floor (more than WEDGE_FLOOR_M off) farther from the goal's height than the actor: then
-- {wedged, away = that member's name, away_dy} and no jump.
N.WEDGE_FLOOR_M = 1.5
function N.wedged(charUuid, budgetMs, goal)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil end
    local t0, budget = Ext.Utils.MonotonicTime(), budgetMs or N.WEDGE_BUDGET_MS
    local floors = 0
    for _, dname in ipairs(DIRS) do
        if Ext.Utils.MonotonicTime() - t0 > budget then return nil end
        local v = DIR_VEC[dname]
        local x, z = from[1] + v[1] * N.WEDGE_RING_M, from[3] + v[2] * N.WEDGE_RING_M
        local fy = floorAt(x, z, from[2])
        if fy then
            floors = floors + 1
            local r = N.path(charUuid, { x, fy, z })
            if r == nil or r.reachable == nil then return nil end -- pathfinding can't say: not a verdict
            if r.reachable and not r.partial then return nil end
        end
    end
    if floors == 0 then return nil end -- a ledge/pillar top with no floor around: route's LOWER GROUND/jump lines
    local best, away
    -- (interact's goal is the item itself: run 3's e45 foot stood on the actor's own floor, Lae'zel 2.9 m below)
    local toGoal = goal and math.abs(goal[2] - from[2]) or nil
    for _, u in ipairs(V.party()) do
        local pe = u ~= charUuid and V.entity(u)
        local p = pe and V.pos(pe)
        local dead = pe and V.osi(Osi.IsDead, u)
        if p and dead ~= 1 and dead ~= true then
            local d = hdist(from, p)
            if d >= N.WEDGE_SHORT_M + 0.5 and d <= N.WEDGE_PARTY_M and math.abs(p[2] - from[2]) <= 4 then
                local other = math.abs(p[2] - from[2]) > N.WEDGE_FLOOR_M -- another floor: after one on ours
                if toGoal and other and math.abs(goal[2] - p[2]) > toGoal + 0.5 then
                    if not away or d < away.d then away = { u = u, p = p, d = d } end
                elseif not best or (best.other and not other) or (best.other == other and d < best.d) then
                    best = { u = u, p = p, d = d, other = other }
                end
            end
        end
    end
    local out = { wedged = true }
    if not best and away then
        out.away, out.away_dy = V.name(away.u), round(away.p[2] - from[2], 1)
    end
    if best then
        local k = (best.d - N.WEDGE_SHORT_M) / best.d
        local to = { from[1] + (best.p[1] - from[1]) * k, best.p[2], from[3] + (best.p[3] - from[3]) * k }
        to[2] = floorAt(to[1], to[3], best.p[2]) or best.p[2]
        out.to, out.near, out.dist_m = U.vec(to, 1), V.name(best.u), round(hdist(from, to), 1)
        local G = BG3AI.Guard
        if G and G.jump_reach then
            local ok, m = pcall(G.jump_reach, charUuid, from, to)
            if ok and type(m) == "number" then out.in_reach = hdist(from, to) <= m end
        end
    end
    return out
end

-- Ladders are used from either end, but a ladder's position is its foot
-- (S024 07:12 Knotted Roots, S025 08:03 the Moonrise lobby ladder: from the
-- upper floor, interact and route aimed 12-16 m below the actor, every try
-- was refused "no walkable path", and the party split and fast-travelled
-- round). For an actor at least LADDER_TOP_MIN_M above the foot: the top end,
-- at the foot's x/z, y = foot + AI-bounds height, raised to the actor's own
-- height when the actor stands up to LADDER_TOP_SLACK_M above that (bounds
-- can fall short of the landing: e366 10.9 m, landing ~15 m up). nil when it
-- is not a ladder or the actor is at the foot's level.
N.LADDER_TOP_MIN_M = 2.5
N.LADDER_TOP_SLACK_M = 6.0
-- A ladder pulled up out of reach (S059 08:56-09:00 Wyrm's Rock: e683's foot hung 5.7 m above the scaffold
-- room's floor; frontier said "climbs 6.5m up", interact refused "no walkable path … move_to that, then a
-- ladder/jump_to", 2 Dimension Doors and a screenshot later the agent saw it raised; a lever on top lowers it).
-- The foot's gap above the nearest floor at/below it (the foot and 4 points LADDER_RAISED_PROBE_M around it;
-- the side against the wall reads the wall top or the floor behind it), or nil: a floor at the foot, or no
-- AI-grid heights there (can't tell). What a player sees: the ladder hanging above the floor.
N.LADDER_RAISED_MIN_M = 2.5
N.LADDER_RAISED_PROBE_M = 0.8
function N.ladderRaised(p)
    if type(p) ~= "table" then return nil end
    local best
    local r = N.LADDER_RAISED_PROBE_M
    for _, o in ipairs({ { 0, 0 }, { r, 0 }, { -r, 0 }, { 0, r }, { 0, -r } }) do
        local ok, hs = pcall(Ext.Level.GetHeightsAt, p[1] + o[1], p[3] + o[2])
        if ok and type(hs) == "table" then
            for _, h in ipairs(hs) do
                local g = type(h) == "number" and p[2] - h
                if g and g >= -1.0 and (not best or g < best) then best = g end
            end
        end
    end
    if best and best >= N.LADDER_RAISED_MIN_M then return round(best, 1) end
    return nil
end

function N.ladderTop(from, uuid, ie)
    ie = ie or V.entity(uuid)
    local p = ie and V.pos(ie)
    if not p or type(from) ~= "table" or from[2] - p[2] < N.LADDER_TOP_MIN_M then return nil end
    if V.itemKind(uuid, ie) ~= "ladder" then return nil end
    local b = V.bounds and V.bounds(ie)
    local h = b and type(b.height) == "number" and b.height >= 1.5 and b.height or (from[2] - p[2])
    local y = p[2] + h
    if from[2] > y and from[2] - y <= N.LADDER_TOP_SLACK_M then y = from[2] end
    return { p[1], y, p[3] }
end

-- Ladders and doors in view, ranked for reaching `goal` (or by distance):
-- walking metres to it + straight metres from its far end to the goal (a
-- ladder's top when we stand at its foot, from its AI bounds). Each: {uuid,
-- name, kind, pos, dist, dir, dy, path_m?|unreachable?, height_m?, leads?
-- (up|down), far_dy?, to_goal_m?, helps?, locked?}.
function N.connectors(ctx, charUuid, goal, radius, limit, budgetMs)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return {} end
    local d0 = goal and U.dist(from, goal)
    -- candidates first; only the nearest CONNECT_PROBE get path probes (an
    -- unreachable one costs up to 9: the approach ring), bounding game-thread time
    local cands = {}
    for _, u in ipairs(V.itemsAround(from, radius)) do
        local ie = V.entity(u)
        local p = ie and V.pos(ie)
        local k0 = p and U.dist(from, p) <= radius and V.itemKind(u, ie)
        if k0 == "ladder" or k0 == "door" then -- the full visibility test (raycasts) only for these
            local vis, kind = V.itemVisible(ctx, u, ie, p)
            if vis and (kind == "ladder" or kind == "door") then
                cands[#cands + 1] = { u = u, ie = ie, p = p, kind = kind, d = U.dist(from, p) }
            end
        end
    end
    table.sort(cands, function(a, b) return a.d < b.d end)
    local out = {}
    local now = Ext.Utils.MonotonicTime()
    local deadline = now + math.min(N.CONNECT_BUDGET_MS, budgetMs or N.CONNECT_BUDGET_MS)
    for k, f in pairs(N.connectFail) do
        if now - f.t > N.CONNECT_FAIL_TTL_MS then N.connectFail[k] = nil end
    end
    for i, cd in ipairs(cands) do
        if i > N.CONNECT_PROBE then break end
        local u, ie, p, kind = cd.u, cd.ie, cd.p, cd.kind
        local o = { uuid = u, name = V.name(u), kind = kind, pos = U.vec(p, 1), dist = round(U.dist(from, p), 1),
            dir = N.direction(from, p), dy = round(p[2] - from[2], 1) }
        local far = p
        if kind == "ladder" then
            local b = V.bounds and V.bounds(ie)
            if b and type(b.height) == "number" and b.height >= 1.5 then
                o.height_m = round(b.height, 1)
                local top = { p[1], p[2] + b.height, p[3] }
                if math.abs(from[2] - top[2]) < math.abs(from[2] - p[2]) then
                    o.leads = "down"
                else
                    far, o.leads = top, "up"
                end
                o.far_dy = round(far[2] - from[2], 1)
                o.raised_m = N.ladderRaised(p)
            end
        elseif truthy(osi(Osi.IsLocked, u)) then
            o.locked = true
        end
        local fkey = charUuid .. "|" .. u
        local f = N.connectFail[fkey]
        if f and (now - f.t > N.CONNECT_FAIL_TTL_MS or U.dist(from, f.from) > N.CONNECT_FAIL_MOVE_M) then
            N.connectFail[fkey], f = nil, nil
        end
        if f then
            o.unreachable = true
        elseif Ext.Utils.MonotonicTime() > deadline then
            o.unprobed = true -- out of budget: no path probe (ranked by straight distance)
        else
            local r = N.path(charUuid, u, deadline)
            if r and r.reachable then
                o.path_m = r.length_m
            elseif r and r.reachable == false then
                o.unreachable = true
                if not N.geoSettling() then N.connectFail[fkey] = { t = now, from = from } end
            elseif r and r.unfinished then
                o.unprobed = true
            end
        end
        if goal then
            o.to_goal_m = round(U.dist(far, goal), 1)
            if o.to_goal_m < d0 - 2 then o.helps = true end
        end
        out[#out + 1] = o
    end
    local function score(o)
        return ((o.unreachable or (o.raised_m and o.leads == "up")) and 1000 or 0) + (o.path_m or o.dist) + (o.to_goal_m or 0) + (o.locked and 20 or 0)
    end
    table.sort(out, function(a, b) return score(a) < score(b) end)
    while #out > (limit or N.CONNECT_MAX) do table.remove(out) end
    return out
end

-- Long trips (S010/S011: Karlach 90-150 m away, route said only "too far to
-- preview", and travel_to died on the same boulder wall again and again).
-- Beyond PATH_MAX_M the route is still asked of the engine (up to
-- ROUTE_FAR_M), but only reachable/length/legs come back: no floors at the
-- goal, no ring probes (a failing far search costs ~25 ms of game thread).
N.ROUTE_FAR_M = 400
N.FAR_RING_M = 3
N.WAYPOINTS_SHOWN = 3

-- The party's unlocked waypoints on this level (what the map shows), with
-- distances to `from` and `goal`, nearest the goal first.
function N.unlockedWaypointsHere(goal, from)
    local unl = N.unlockedWaypointIds()
    local out = {}
    for _, w in ipairs(N.waypointsHere()) do
        if unl[w.id] then
            local o = { name = w.name, id = w.id, pos = U.vec(w.pos, 1) }
            if from then o.dist = round(U.dist(from, w.pos), 1) end
            if goal then o.to_goal_m = round(U.dist(w.pos, goal), 1) end
            out[#out + 1] = o
        end
    end
    if goal then table.sort(out, function(a, b) return a.to_goal_m < b.to_goal_m end) end
    return out
end

-- The explored breadcrumb cell nearest `goal` (horizontally): the closest the
-- party has been. {pos, to_goal_m} or nil. Pure over the party's own cells.
-- NAV-5: a cell key is parsed once (N.cellCenter, kept for the Lua state): this ran a string match per explored
-- cell on every far route.
N.cellCenter = N.cellCenter or {}
function N.nearestVisited(level, goal)
    local l = level and store().levels[level]
    if not l or not goal then return nil end
    local best, bd
    local cc = N.cellCenter
    for k in pairs(l.cells) do
        local p = cc[k]
        if p == nil then
            local cx, cy, cz = tostring(k):match("^(-?%d+):(-?%d+):(-?%d+)$")
            p = cx and { (tonumber(cx) + 0.5) * N.CELL_XZ, (tonumber(cy) + 0.5) * N.CELL_Y, (tonumber(cz) + 0.5) * N.CELL_XZ } or false
            cc[k] = p
        end
        if p then
            local d = math.sqrt((p[1] - goal[1]) ^ 2 + (p[3] - goal[3]) ^ 2)
            if not bd or d < bd then best, bd = p, d end
        end
    end
    if not best then return nil end
    best = { best[1], best[2], best[3] }
    best[2] = floorAt(best[1], best[3], best[2]) or best[2]
    return { pos = U.vec(best, 1), to_goal_m = round(bd, 1) }
end

N.inner.hdist = hdist
