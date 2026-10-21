-- Navigation, part 2 of 5 (Navigation.lua loads the parts in order; split for the 1000-line cap):
-- path lengths, hazards on a route, reach toward / approach / line of sight.
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local N = BG3AI.Nav

local truthy = U.truthy
local osi = V.osi
local round = U.round
local I = N.inner
local store, currentLevel, DIRS, DIR_VEC, floorAt = I.store, I.currentLevel, I.DIRS, I.DIR_VEC, I.floorAt
local rawPath, gatedPath = I.rawPath, I.gatedPath

N.WALK_DROP_MAX_M = 6.0 -- BG3 fall damage grows past ~4 m; 6+ m can down a hurt character

-- For Guard.lua / Actions.lua: path length in metres (pos or entity uuid), or nil.
function N.pathLength(charUuid, target)
    local r = N.path(charUuid, target)
    if r and r.reachable then return r.length_m, r.needs_jump end
    return nil
end

local function movementLeft(uuid)
    local e = V.entity(uuid)
    local ar = e and U.comp(e, "ActionResources")
    if not ar then return nil end
    local amt
    pcall(function()
        for guid, list in pairs(ar.Resources) do
            local okn, r = pcall(Ext.StaticData.Get, guid, "ActionResource")
            if okn and r and r.Name == "Movement" then amt = list[1].Amount end
        end
    end)
    return amt
end

------------------------------------------------------------------------
-- Ops
------------------------------------------------------------------------

local function activeChar(ctx)
    for _, u in ipairs(ctx.party) do
        local e = V.entity(u)
        if e and U.comp(e, "ClientControl") then return u end
    end
    return ctx.party[1]
end

RPC.register("nav.path", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local target = args.pos
    if not target and args.target then
        local te = V.entity(args.target)
        if not te then error("unknown target", 0) end
        local tp = V.pos(te)
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tp)
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tp))
        if not visible then error("not visible", 0) end
        target = tp
    end
    if type(target) ~= "table" or #target < 3 then error("need pos [x,y,z] or target", 0) end
    -- Like the in-game path preview (on-screen ground only): no probing far geometry.
    local ce = V.entity(char)
    local cp = ce and V.pos(ce)
    if cp and U.dist(cp, target) > N.PATH_MAX_M then
        return { estimate = true, straight_m = round(U.dist(cp, target), 1), reason = "too far to preview" }
    end
    -- a coordinate the party hasn't seen or walked: the straight line only (N.known)
    if args.pos and cp and not N.known(ctx, target) then
        return { unknown_goal = true, straight_m = round(U.dist(cp, target), 1), dir = N.direction(cp, target),
            dy = round(target[2] - cp[2], 1) }
    end
    local r0 = N.path(char, target)
    if not r0 then error("no path", 0) end
    local r = {}
    for k, v in pairs(r0) do if k ~= "points" and k ~= "portals" then r[k] = v end end
    if r0.points then r.hazard = N.seenHazard(N.routeHazard(r0.points)) end
    local mv = movementLeft(char)
    if mv and r.reachable then r.within_movement = r.length_m <= mv + 0.05; r.movement_left = round(mv, 1) end
    if not r.reachable then r.opened = N.geoNote(cp) end
    return r
end)

RPC.register("nav.set_subregion", function(args)
    if type(args.name) == "string" and args.name ~= "" then N.subregion = args.name end
    return { subregion = N.subregion }
end)

RPC.register("nav.explored", function(args)
    local lvl = currentLevel()
    if not lvl then error("no level", 0) end
    local l = store().levels[lvl]
    local out = { level = lvl, cells = 0, cell_m = N.CELL_XZ, subregion = N.subregion }
    if not l then return out end
    out.cells = l.n
    out.area_m2 = l.n * N.CELL_XZ * N.CELL_XZ
    if l.bbox then out.bbox = { round(l.bbox[1], 0), round(l.bbox[2], 0), round(l.bbox[3], 0), round(l.bbox[4], 0) } end
    -- Fairness: only waypoints the party has unlocked name a place (the map
    -- lists those; an undiscovered shrine nearby is not known yet).
    local wps = {}
    local unlocked = N.unlockedWaypointIds()
    for _, w in ipairs(N.waypointsHere()) do if unlocked[w.id] then wps[#wps + 1] = w end end
    local nPlaces = tonumber(args.last) or 10
    local places = {}
    for i = #l.places, math.max(1, #l.places - nPlaces + 1), -1 do
        local pl = l.places[i]
        local p = { pl.x, pl.y, pl.z }
        local o = { pos = U.vec(p, 1), subregion = pl.sub }
        -- Nearest named landmark the party knows: a waypoint of this level.
        local best, bd
        for _, w in ipairs(wps) do
            local d = U.dist(w.pos, p)
            if d <= 60 and (not bd or d < bd) then best, bd = w, d end
        end
        if best then o.near = best.name; o.near_m = round(bd, 0) end
        places[#places + 1] = o
    end
    out.recent = places
    return out
end)

-- Leads a player would notice: perceived doors/ladders/containers/exits and
-- perceived things outside the explored cells.
RPC.register("nav.frontier", function(args)
    local t0 = Ext.Utils.MonotonicTime()
    local ctx = V.context()
    local lvl = currentLevel()
    local active = activeChar(ctx)
    local ae = V.entity(active)
    local center = ae and V.pos(ae)
    if not center then error("no active character position", 0) end
    local radius = tonumber(args.radius) or 30
    local limit = tonumber(args.limit) or 25
    local leads = {}
    local remembering -- the item being added is remembered (args.known), not in sight

    local function add(kind, uuid, p, extra)
        local d = U.dist(center, p)
        local o = { kind = kind, uuid = uuid, name = V.name(uuid), dist = round(d, 1),
            dir = N.direction(center, p), pos = U.vec(p, 1) }
        if not N.visited(lvl, p, 1) then o.unvisited = true end
        local dy = p[2] - center[2]
        if math.abs(dy) >= 1.5 then o.dy = round(dy, 1) end -- on a ledge above / a floor below
        if extra then for k, v in pairs(extra) do o[k] = v end end
        if remembering then o.remembered = true end
        leads[#leads + 1] = o
    end

    -- Items: doors, ladders, containers, waypoints, and unvisited items.
    local wpSet = {}
    for _, w in ipairs(N.waypointsHere()) do wpSet[w.uuid] = w end
    for _, u in ipairs(V.itemsAround(center, radius)) do
        local e = V.entity(u)
        local p = e and V.pos(e)
        if p and U.dist(center, p) <= radius then
            local vis, kind = V.itemVisible(ctx, u, e, p)
            -- known=true (travel_to's other-floor rule): a way in the party saw minutes ago counts too, as a
            -- player remembers it (acceptance 01:31, live probe: the Mausoleum Entrance 34 m from the Knotted Roots
            -- top is behind the cliff edge, so the 934 m walk started instead of naming it). Listings stay strict.
            local remembered
            if not vis and args.known and V.itemKnown then
                vis, kind = V.itemKnown(ctx, u, e, p)
                remembered = vis or nil
            end
            if vis and remembered and not ((kind == "usable" or kind == "item") and N.exitName(V.name(u)))
                and kind ~= "ladder" and kind ~= "door" then
                vis = false -- remembered: only the ways to another floor / an interior
            end
            remembering = remembered
            if vis then
                if wpSet[u] then
                    add("waypoint", u, p, { waypoint = wpSet[u].name })
                elseif kind == "door" then
                    add("door", u, p, { locked = truthy(osi(Osi.IsLocked, u)) or nil })
                elseif kind == "ladder" then
                    -- how far it climbs (AI bounds), and which way from where we stand
                    local b, extra = V.bounds and V.bounds(e), nil
                    if b and type(b.height) == "number" and b.height >= 1.5 then
                        local top = p[2] + b.height
                        local down = math.abs(center[2] - top) < math.abs(center[2] - p[2])
                        local to = down and { p[1], p[2], p[3] } or { p[1], top, p[3] }
                        -- where it leads: the other end, and whether the party has been there (climbables: vines,
                        -- roots and ladders are all IsLadder items)
                        extra = { height_m = round(b.height, 1), leads = down and "down" or "up", to = U.vec(to, 1),
                            to_unvisited = not N.visited(lvl, to, 3) or nil, raised_m = N.ladderRaised(p) }
                    end
                    add("ladder", u, p, extra)
                elseif kind == "container" and not U.comp(e, "HasOpened") then
                    add("container", u, p, { locked = truthy(osi(Osi.IsLocked, u)) or nil })
                elseif (kind == "usable" or kind == "item") and N.exitName(V.name(u)) then
                    add("exit", u, p, { locked = truthy(osi(Osi.IsLocked, u)) or nil })
                elseif not N.visited(lvl, p, 1) and (kind == "usable" or kind == "item") then
                    add(kind, u, p)
                end
            end
        end
    end
    remembering = nil
    -- Characters the party perceives but hasn't been near.
    local chars = V.characters and V.characters() or {}
    do
        for _, row in ipairs(chars) do
            local e, p = row[1], row[2]
            if math.abs(p[1] - center[1]) <= radius and math.abs(p[3] - center[3]) <= radius then
                local u = V.entityUuid(e)
                if u and not ctx.is_party[u] and U.dist(center, p) <= radius
                    and not truthy(osi(Osi.IsDead, u)) and V.characterVisible(ctx, u, e, p) then
                    if not N.visited(lvl, p, 1) then
                        local rel = truthy(osi(Osi.IsEnemy, active, u)) and "hostile" or "neutral"
                        add("character", u, p, { relation = rel })
                    end
                end
            end
        end
    end
    -- Rank: unvisited things and named exits first, then distance.
    table.sort(leads, function(a, b)
        local na, nb = a.unvisited == true or a.kind == "exit", b.unvisited == true or b.kind == "exit"
        if na ~= nb then return na end
        return a.dist < b.dist
    end)
    local out = { level = lvl, leads = {} }
    for i, o in ipairs(leads) do
        if i > limit then out.more = #leads - limit; break end
        out.leads[#out.leads + 1] = o
    end
    -- Harmful patches in view (fire, acid, ...): plan around them.
    if args.hazards ~= false then
        local hz = N.hazardPatches(active, math.min(tonumber(args.hazard_radius) or 15, 20))
        if hz and #hz > 0 then
            out.hazards = {}
            for i = 1, math.min(#hz, 6) do out.hazards[i] = hz[i] end
        end
    end
    -- Open directions (args.ways=false skips): from the per-spot cache the
    -- Tick warms; a cold spot searches the missing ones (~12 ms each live).
    if args.ways ~= false then
        -- ways_cached (NAV-6, a walk's arrival): only what the warm-up already answered, never a sync search (the
        -- S038 freeze moment: a cold frontier right where a walk ended)
        local cached = args.ways_cached == true
        local ways, _, off = N.ways(active, tonumber(args.ways_far) or 20, 8, tonumber(args.ways_budget_ms) or 100,
            nil, cached or nil, cached or nil)
        if ways and #ways > 0 then out.ways = ways end
        if off and (not ways or #ways < #N.WAYS_DIRS) then out.ways_off = off end
    end
    out.ms = Ext.Utils.MonotonicTime() - t0
    return out
end)

------------------------------------------------------------------------
-- Reachability (for Guard.lua: refuse coordinate moves with no path)
------------------------------------------------------------------------

-- reachable(char, pos|uuid) -> ok:boolean|nil, length_m|nil, reason|nil, info
-- nil (not false) when pathfinding is unavailable, so callers can fall back.
-- info = the full path result (needs_jump, approach_pos, ...).
function N.reachable(charUuid, target)
    local r, err = N.path(charUuid, target)
    if not r then return nil, nil, err or "no position" end
    if r.reachable == true then return true, r.length_m, nil, r end
    if r.reachable == false then return false, nil, "can't get there: no path", r end
    return nil, nil, r.reason or "pathfinding unavailable", r
end

------------------------------------------------------------------------
-- Harmful surfaces (fire, acid, electrified water, ...): what a player sees
-- burning or bubbling on the ground. Read from the AI grid tile under a
-- point (Ext.Level.GetTileDebugInfo), the same tiles the game's own path
-- preview avoids. Invisible surfaces (InvisibleWeb, InvisibleGithAcid) are
-- never reported: a player can't see them.
------------------------------------------------------------------------

N.HARMFUL = {
    Fire = true, TrialFire = true, Hellfire = true, Lava = true, Acid = true, CausticBrine = true,
    Poison = true, WyvernPoison = true, PurpleWormPoison = true, SerpentVenom = true,
    WaterElectrified = true, BloodElectrified = true, Web = true, Grease = true,
    WaterFrozen = true, BloodFrozen = true, SpikeGrowth = true, BlackTentacles = true,
    ShadowCursedVines = true, BladeBarrier = true, Chasm = true, BloodExploding = true,
    -- clouds
    WaterCloudElectrified = true, PoisonCloud = true, ExplosionCloud = true, ShockwaveCloud = true,
    CloudkillCloud = true, Cloudkill6Cloud = true, MaliceCloud = true, StinkingCloud = true,
    SporeWhiteCloud = true, SporeGreenCloud = true, SporeBlackCloud = true, DrowPoisonCloud = true,
    IceCloud = true, VoidCloud = true, CrawlerMucusCloud = true, BlackPowderDetonationCloud = true,
}
N.HAZARD_STEP_M = 1.0   -- sampling step along a route
N.HAZARD_SKIP_M = 1.0   -- the first metre of a route is where we stand (escaping it is fine)

-- Harmful surface name at pos (ground first, then cloud), or nil.
local function tileSurfaces(t) return tostring(t.GroundSurface), tostring(t.CloudSurface) end
function N.surfaceAt(pos)
    if not pos then return nil end
    local ok, t = pcall(Ext.Level.GetTileDebugInfo, { pos[1], pos[2], pos[3] })
    if not ok or not t then return nil end
    -- one pcall, no closures (hazard scans probe hundreds of tiles); field by
    -- field only if that fails
    local okr, g, c = pcall(tileSurfaces, t)
    if not okr then
        g, c = nil, nil
        pcall(function() g = tostring(t.GroundSurface) end)
        pcall(function() c = tostring(t.CloudSurface) end)
    end
    if g and N.HARMFUL[g] then return g end
    if c and N.HARMFUL[c] then return c end
    return nil
end

-- Harmful surface at pos or within `margin` m of it (4 probes), or nil: a
-- spot to stop at should not be on the edge of a fire.
N.HAZARD_MARGIN_M = 1.0
function N.surfaceNear(pos, margin)
    local s = N.surfaceAt(pos)
    if s then return s end
    margin = margin or N.HAZARD_MARGIN_M
    for _, d in ipairs({ { margin, 0 }, { -margin, 0 }, { 0, margin }, { 0, -margin } }) do
        s = N.surfaceAt({ pos[1] + d[1], pos[2], pos[3] + d[2] })
        if s then return s end
    end
    return nil
end

-- Harmful surface the character stands in, or nil.
function N.harmfulUnder(uuid)
    local e = V.entity(uuid)
    local p = e and V.pos(e)
    return p and N.surfaceAt(p) or nil
end

-- First harmful surface along a route (points from rawPath), skipping the
-- first `skip` metres. Returns {surface, pos, at_m} or nil.
function N.routeHazard(points, skip)
    if type(points) ~= "table" or #points < 1 then return nil end
    skip = skip or N.HAZARD_SKIP_M
    local walked = 0
    local function probe(p, at)
        if at < skip then return nil end
        local s = N.surfaceAt(p)
        if s then return { surface = s, pos = U.vec(p, 1), at_m = round(at, 1) } end
    end
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local seg = U.dist(a, b)
        local n = math.max(1, math.ceil(seg / N.HAZARD_STEP_M))
        for k = 1, n do
            local f = k / n
            local p = { a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f }
            local h = probe(p, walked + seg * f)
            if h then return h end
        end
        walked = walked + seg
    end
    if #points == 1 then return probe(points[1], skip) end
    return nil
end

-- A hazard (routeHazard / routeExposure shape) only if a party member sees where it starts, else nil (fail closed).
-- Replies name only those; walks still avoid every patch on their own route (the engine's own pathing).
function N.seenHazard(h)
    if type(h) ~= "table" or type(h.pos) ~= "table" then return nil end
    local ok, s = pcall(V.pointSeen, V.context(), h.pos)
    return ok and s == true and h or nil
end

-- How much of a route (after `skip` m) lies in harmful surfaces, sampled
-- every EXPOSURE_STEP_M: {m, patches, longest_m, surface, pos, at_m} (pos/at_m:
-- where the first patch starts), or nil when the route is clear. A patch is a
-- run of burning samples; `surface` is the one with the most metres.
N.EXPOSURE_STEP_M = 0.5
function N.routeExposure(points, skip)
    if type(points) ~= "table" or #points < 1 then return nil end
    skip = skip or N.HAZARD_SKIP_M
    local st = N.EXPOSURE_STEP_M
    local total, patches, longest, run, inRun = 0, 0, 0, 0, false
    local per, first = {}, nil
    local walked, nextAt = 0, skip
    local function sample(p, at)
        local s = N.surfaceAt(p)
        if s then
            total = total + st
            per[s] = (per[s] or 0) + st
            if not inRun then patches = patches + 1; run = 0; inRun = true end
            run = run + st
            if run > longest then longest = run end
            if not first then first = { pos = U.vec(p, 1), at_m = round(at, 1) } end
        else
            inRun = false
        end
    end
    if #points == 1 then sample(points[1], skip) end
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local seg = U.dist(a, b)
        while nextAt <= walked + seg + 1e-6 do
            local f = seg > 1e-6 and (nextAt - walked) / seg or 1
            sample({ a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f }, nextAt)
            nextAt = nextAt + st
        end
        walked = walked + seg
    end
    if total <= 0 then return nil end
    local main, mm = nil, -1
    for s, m in pairs(per) do if m > mm or (m == mm and s < main) then main, mm = s, m end end
    return { m = round(total, 1), patches = patches, longest_m = round(longest, 1), surface = main,
        pos = first.pos, at_m = first.at_m }
end

-- Pure geometry (unit-tested in tools/dev/test_nav_route.lua): where along a
-- polyline a point projects. Returns arc position (m), horizontal distance
-- off the route (m), vertical offset (m).
function N.project(points, p)
    local bestArc, bestOff, bestDy, walked = nil, math.huge, 0, 0
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local ax, az, bx, bz = a[1], a[3], b[1], b[3]
        local dx, dz = bx - ax, bz - az
        local l2 = dx * dx + dz * dz
        local t = 0
        if l2 > 1e-6 then t = math.max(0, math.min(1, ((p[1] - ax) * dx + (p[3] - az) * dz) / l2)) end
        local qx, qz = ax + dx * t, az + dz * t
        local off = math.sqrt((p[1] - qx) ^ 2 + (p[3] - qz) ^ 2)
        local qy = a[2] + (b[2] - a[2]) * t
        local seg = U.dist(a, b)
        if off < bestOff then bestArc, bestOff, bestDy = walked + seg * t, off, p[2] - qy end
        walked = walked + seg
    end
    return bestArc, bestOff, bestDy
end

-- Is a big displacement prev -> here the character following its own route
-- (a jump/climb link, or a frame hitch mid-walk) rather than the engine's
-- no-path teleport? Both ends must lie on the route, moving forward, and the
-- step can't skip more route than it covers in a straight line plus slack.
N.ROUTE_OFF_M = 2.0
N.ROUTE_DY_M = 3.0
function N.onRoute(points, prev, here)
    if type(points) ~= "table" or #points < 2 or not prev or not here then return false end
    local a0, off0, dy0 = N.project(points, prev)
    local a1, off1, dy1 = N.project(points, here)
    if not a0 or not a1 then return false end
    if off0 > N.ROUTE_OFF_M or off1 > N.ROUTE_OFF_M then return false end
    if math.abs(dy0) > N.ROUTE_DY_M or math.abs(dy1) > N.ROUTE_DY_M then return false end
    if a1 < a0 - 1.0 then return false end
    local step = U.dist(prev, here)
    return (a1 - a0) <= step * 1.5 + 3.0
end

-- The route (points) the engine would walk from char to pos, or nil.
function N.route(charUuid, pos)
    local r = N.path(charUuid, pos)
    return r and r.reachable and r.points or nil
end

-- The engine's route for this character from `from` (not where it stands) to pos, or nil: the second leg of a
-- walk round floor plates (Actions.lua A.plate_detour, vis V25).
function N.routeFrom(charUuid, from, pos)
    local e = V.entity(charUuid)
    if not e or not from or not pos then return nil end
    local r = gatedPath(e, from, pos, N.PATH_END_TOL_M)
    return r and r.reachable and r.points or nil
end

-- Farthest reachable floor point along a compass direction (or toward pos):
-- the "walk west as far as you can" primitive. Samples every `step` m up to
-- max_m; a sample counts only if pathable and the path is direct
-- (length <= DETOUR x straight distance), i.e. floor a player sees that way.
-- avoid (default true): skip samples standing in, or reached through, a
-- harmful surface; the farthest safe sample wins. If every sample is unsafe
-- the result is {reachable=false, hazard={surface,pos,at_m}}.
N.DETOUR = 1.5
N.DETOUR_LOOSE = 3.0 -- loose: a winding walk still counts (walk_toward)
N.LOOSE_TRIES = 5
-- deadline (MonotonicTime ms, optional, NAV-5): no new search past it; what was found so far is the answer
-- (unfinished=true when the sweep was cut short).
function N.reachToward(charUuid, dir, toward, maxM, step, avoid, loose, deadline)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    if avoid == nil then avoid = true end
    local vx, vz
    if toward then
        local dx, dz = toward[1] - from[1], toward[3] - from[3]
        local len = math.sqrt(dx * dx + dz * dz)
        if len < 0.5 then return { pos = U.vec(from, 1), path_m = 0, straight_m = 0, reachable = true } end
        vx, vz = dx / len, dz / len
        maxM = math.min(maxM, len)
    else
        local v = DIR_VEC[tostring(dir or ""):upper()]
        if not v then return nil, "dir must be one of N NE E SE S SW W NW" end
        vx, vz = v[1], v[2]
    end
    -- Sample points on the floor every `step` m (and the destination itself),
    -- rounded to 0.1 m so the point handed out is the one searched (no second
    -- verify search). The floor height follows ramps and stairs: it follows a
    -- sample's floor unless that is steeper than 1:1 (a ledge or a pit).
    local pts = {}
    local y = from[2]
    local function point(dd)
        local x, z = round(from[1] + vx * dd, 1), round(from[3] + vz * dd, 1)
        local fy = floorAt(x, z, y)
        if fy then
            if math.abs(fy - y) <= step + 0.5 then y = fy end
            pts[#pts + 1] = { pos = { x, round(fy, 1), z }, dd = dd }
        end
    end
    local d = step
    while d <= maxM + 0.01 do
        point(d)
        d = d + step
    end
    if maxM - (d - step) > 0.5 then point(maxM) end
    -- Farthest first: on open ground the first search wins (live: ~15 path
    -- searches front to back, ~120 ms, before). Then a binary search for
    -- where the way stops (a wall, a cliff: ~4 more searches), and only if
    -- that finds nothing, every sample left, farthest first. A sample counts
    -- if pathable and direct (length <= DETOUR x straight), and (avoid) clear
    -- of harmful surfaces at its end and along its route.
    local firstHazard, avoided, tried, memo = nil, 0, 0, {}
    local cut = false
    local ctx = V.context()
    local function good(i)
        if memo[i] ~= nil then return memo[i] end
        if deadline and Ext.Utils.MonotonicTime() > deadline then cut = true; return false end
        local c, dd = pts[i], pts[i].dd
        -- a walk ends only on ground the party sees or has walked (N.known), as a click does
        if not N.known(ctx, c.pos) then memo[i] = false; return false end
        tried = tried + 1
        local ok = false
        local r = N.path(charUuid, c.pos)
        if r and r.reachable and not r.drop and not r.partial and r.length_m <= math.max(N.DETOUR * dd, dd + 2.0) then
            local hz = nil
            if avoid then
                local sf = N.surfaceNear(c.pos)
                hz = sf and { surface = sf, pos = U.vec(c.pos, 1), at_m = r.length_m } or N.routeHazard(r.points)
            end
            if hz then
                avoided = avoided + 1
                if not firstHazard or c.dd > firstHazard.dd then firstHazard = hz; firstHazard.dd = c.dd end
            else
                ok = { pos = c.pos, path_m = r.length_m, straight_m = round(dd, 1), needs_jump = r.needs_jump }
            end
        end
        memo[i] = ok
        return ok
    end
    local best = #pts > 0 and good(#pts) or nil
    if not best and #pts > 1 then
        local lo, hi = 0, #pts -- lo: known good (0 = where we stand), hi: known bad
        while hi - lo > 1 do
            local mid = math.floor((lo + hi) / 2)
            if good(mid) then lo = mid else hi = mid end
        end
        best = lo > 0 and memo[lo] or nil
        for i = #pts - 1, 1, -1 do
            if best then break end
            best = good(i) or nil
        end
    end
    -- loose (walk_toward): nothing direct -> the farthest sample whose walk
    -- winds (a room's furniture, a cliff path's switchbacks) up to
    -- DETOUR_LOOSE x the straight line. S021: "no walkable ground toward E"
    -- inside buildings and on the Risen Road cliffs, where a click on that
    -- ground walks there. Same-frame searches come from the path cache.
    local winding = false
    if not best and loose and avoided == 0 then
        local n = 0
        for i = #pts, 1, -1 do
            if n >= N.LOOSE_TRIES then break end
            if deadline and Ext.Utils.MonotonicTime() > deadline then cut = true; break end
            local c, dd = pts[i], pts[i].dd
            if dd >= 2.0 and N.known(ctx, c.pos) then
                if memo[i] == nil then tried = tried + 1 end
                n = n + 1
                local r = N.path(charUuid, c.pos)
                if r and r.reachable and not r.partial and not r.needs_jump and not r.drop
                    and r.length_m <= math.max(N.DETOUR_LOOSE * dd, dd + 6.0)
                    and not (avoid and (N.surfaceNear(c.pos) or N.routeHazard(r.points))) then
                    best = { pos = c.pos, path_m = r.length_m, straight_m = round(dd, 1) }
                    winding = true
                    break
                end
            end
        end
    end
    if firstHazard then firstHazard.dd = nil end
    firstHazard = N.seenHazard(firstHazard)
    if not best then
        if cut then return { reachable = nil, unfinished = true, samples = tried, hazard = firstHazard } end
        if avoided == 0 then return { reachable = false, samples = tried } end
        return { reachable = false, samples = tried, hazard = firstHazard }
    end
    local out = { pos = U.vec(best.pos, 1), path_m = best.path_m, straight_m = best.straight_m,
        needs_jump = best.needs_jump, reachable = true, samples = tried, dir = N.direction(from, best.pos),
        searched = true, winding = winding or nil, unfinished = cut or nil }
    if avoided > 0 then out.avoided = avoided; out.hazard_beyond = firstHazard end
    return out
end

-- Other compass directions that do lead somewhere, nearest bearing to the
-- failed one first (walk_toward's FAILED reply names them, so the next call
-- walks instead of probing: S021 spent 2-4 model turns per dead direction).
-- Stops after `want` hits; each direction is a farthest-first sweep (~1-5
-- searches). Returns {{dir, m, path_m, winding?}, ...}.
N.ALT_DIRS_WANT = 3
local ALT_ORDER = { 1, -1, 2, -2, 3, -3, 4 }
function N.otherDirections(charUuid, dir, maxM, avoid, want)
    local out = {}
    local i0
    for i, d in ipairs(DIRS) do if d == dir then i0 = i end end
    if not i0 then return out end
    for _, k in ipairs(ALT_ORDER) do
        if #out >= (want or N.ALT_DIRS_WANT) then break end
        local d = DIRS[((i0 - 1 + k) % 8) + 1]
        local r = N.reachToward(charUuid, d, nil, maxM, 3, avoid, true)
        if r and r.reachable and (r.straight_m or 0) >= 3 then
            out[#out + 1] = { dir = d, m = r.straight_m, path_m = r.path_m, winding = r.winding }
        end
    end
    return out
end

-- Pure geometry (unit-tested in tools/dev/test_nav_lua.lua): the point at arc
-- length m along a polyline (clamped to its ends).
function N.pointAlong(points, m)
    local walked = 0
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local seg = U.dist(a, b)
        if walked + seg >= m and seg > 1e-6 then
            local f = math.max(0, (m - walked) / seg)
            return { a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f }
        end
        walked = walked + seg
    end
    local p = points[#points]
    return { p[1], p[2], p[3] }
end

-- Pure geometry: walking the route `points`, the first arc length where the
-- straight distance to `tpos` is <= within (sampled every APPROACH_STEP_M),
-- or nil if the route never gets that close. Also returns the route length.
N.APPROACH_STEP_M = 0.25
function N.arcWithin(points, tpos, within)
    local walked = 0
    if #points >= 1 and U.dist(points[1], tpos) <= within then return 0, 0 end
    local found
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local seg = U.dist(a, b)
        if not found then
            local n = math.max(1, math.ceil(seg / N.APPROACH_STEP_M))
            for k = 1, n do
                local f = k / n
                local p = { a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f }
                if U.dist(p, tpos) <= within then found = walked + seg * f; break end
            end
        end
        walked = walked + seg
    end
    return found, walked
end

-- Walk the engine's own route toward a target (entity uuid or pos) until
-- within `within` m of it, at most budget_m of path (movement left), keeping
-- out of harmful surfaces when avoid. Unlike reachToward (straight-line
-- samples), this follows stairs, ramps and detours: S002, the move toward
-- Commander Zhalk's platform stopped 0.3 m in and still said "done".
-- Returns {reachable, pos, walk_m, need_m?, route_m, end_m, limit} where limit
-- says why pos is where it is: "in_range" | "movement" | "hazard" | "route_end";
-- or {reachable=false, reason}.
N.APPROACH_FIT_TRIES = 2
function N.approach(charUuid, target, tpos, within, budget, avoid)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local r = N.path(charUuid, target)
    if not r or r.reachable ~= true or type(r.points) ~= "table" then
        return { reachable = false, reason = r and r.reachable == false and "no path to it"
            or (r and r.reason) or "pathfinding unavailable", straight_m = round(U.dist(from, tpos), 1) }
    end
    local pts = r.points
    local need, routeM = N.arcWithin(pts, tpos, within)
    local walk, limit = need or routeM, need and "in_range" or "route_end"
    if budget and walk > budget then walk, limit = math.max(0, budget), "movement" end
    local out = { reachable = true, route_m = round(routeM, 1), need_m = need and round(need, 1) or nil,
        needs_jump = r.needs_jump }
    if avoid then
        local h = N.routeHazard(pts)
        if h and h.at_m < walk + 0.5 then
            walk, limit = math.max(0, h.at_m - N.HAZARD_STEP_M - 0.2), "hazard" -- samples are 1 m apart
            out.hazard = h
        end
    end
    local p = N.pointAlong(pts, walk)
    -- naut3-r3 #56: the spot 1.8 m along the route to an imp needed a 3.1 m walk of its own (2.1 m left), so the
    -- move was refused "beyond movement" and the turn's shrink retry had too little left to try. A spot cut by the
    -- movement left is checked with its own path and pulled back along the route until that path fits.
    if limit == "movement" and walk > 0 then
        for i = 1, N.APPROACH_FIT_TRIES + 1 do
            local rp = N.path(charUuid, U.vec(p, 2))
            local len = rp and rp.reachable == true and not rp.partial and rp.length_m
            if not len or len <= budget + 0.05 then break end
            walk = i <= N.APPROACH_FIT_TRIES and walk * budget / len - 0.1 or 0 -- no fit: no spot toward it
            if walk < 0.5 then walk = 0 end
            p = N.pointAlong(pts, walk)
            if walk == 0 then break end
        end
    end
    out.pos, out.walk_m, out.limit = U.vec(p, 2), round(walk, 1), limit
    out.end_m = round(U.dist(p, tpos), 1)
    return out
end

-- A spot to cast from: reachable within budget_m of path, within `range` m of
-- tpos, from which los(feet) says the target is in sight (S018: 14 casts
-- refused "no line of sight", pillars in the Shattered Sanctum, a downed ally
-- behind vines; the agent re-planned each by hand or lost the action).
-- Candidates, cheapest walk first: points along the route to the target
-- (1 m apart, known walk), then rings around the character (path-checked:
-- full, no jump, within budget). los(feet) -> true|false|nil (nil: rays
-- unusable -> nothing is found, the caller falls back). avoid skips spots
-- in or behind a harmful surface.
-- Returns {found=true, pos, walk_m, dist_m, via="route"|"ring", tried} or
-- {found=false, reason, tried}.
N.LOS_RINGS = { 1.5, 3, 4.5, 6, 8 }
N.LOS_MAX_PATHS = 12
N.LOS_MAX_RAYS = 40
function N.losSpot(charUuid, target, tpos, range, budget, avoid, los)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    budget = budget or 12
    local tried, rays, paths = 0, 0, 0
    local function sees(p) -- false once the ray cap is spent; nil: rays unusable
        if rays >= N.LOS_MAX_RAYS then return false end
        rays = rays + 1
        return los(p)
    end
    local inRange = function(p) return U.dist(p, tpos) <= range end
    -- 1. Along the route toward the target.
    local r = N.path(charUuid, target)
    if r and r.reachable == true and type(r.points) == "table" and #r.points >= 2 then
        local h = avoid and N.routeHazard(r.points) or nil
        local maxW = math.min(budget, r.length_m or budget)
        if h then maxW = math.min(maxW, h.at_m - N.HAZARD_STEP_M - 0.2) end
        local w = 1
        while w <= maxW + 1e-6 do
            local p = N.pointAlong(r.points, w)
            if inRange(p) then
                tried = tried + 1
                local v = sees(p)
                if v == nil then return { found = false, reason = "line-of-sight rays unavailable", tried = tried } end
                if v then
                    return { found = true, pos = U.vec(p, 2), walk_m = round(w, 1), dist_m = round(U.dist(p, tpos), 1),
                        via = "route", tried = tried }
                end
            end
            w = w + 1
        end
    end
    -- 2. Rings around the character: sight-check first (cheap), then the path.
    local cands = {}
    for _, rad in ipairs(N.LOS_RINGS) do
        if rad <= budget then
            for _, dname in ipairs(DIRS) do
                local v = DIR_VEC[dname]
                local x, z = from[1] + v[1] * rad, from[3] + v[2] * rad
                local fy = floorAt(x, z, from[2])
                if fy then
                    local p = { x, fy, z }
                    if inRange(p) and not (avoid and N.surfaceNear(p)) then
                        -- Same ring: nearer the target first (a short step round a pillar).
                        cands[#cands + 1] = { p = p, rad = rad, key = rad * 100 + U.dist(p, tpos) }
                    end
                end
            end
        end
    end
    table.sort(cands, function(a, b) return a.key < b.key end)
    for _, c in ipairs(cands) do
        if paths >= N.LOS_MAX_PATHS or rays >= N.LOS_MAX_RAYS then break end
        tried = tried + 1
        local v = sees(c.p)
        if v == nil then return { found = false, reason = "line-of-sight rays unavailable", tried = tried } end
        if v then
            paths = paths + 1
            local pr = N.path(charUuid, c.p)
            if pr and pr.reachable == true and not pr.partial and not pr.needs_jump and (pr.length_m or 1e9) <= budget
                and not (avoid and N.routeHazard(pr.points)) then
                return { found = true, pos = U.vec(c.p, 2), walk_m = round(pr.length_m, 1),
                    dist_m = round(U.dist(c.p, tpos), 1), via = "ring", tried = tried }
            end
        end
    end
    return { found = false, reason = string.format("no spot in sight within %.1f m of walking and %.1f m of it", budget, range),
        tried = tried }
end

-- Uncached check of the exact point `pos` from where the character stands
-- now (N.path caches by a 0.1 m key, so a rounded leg end would hit the
-- unrounded sample's entry). Returns reachable (true/false/nil), raw result.
function N.verifyPoint(charUuid, pos)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from or type(pos) ~= "table" then return nil end
    local r = rawPath(e, from, pos, N.PATH_END_TOL_M)
    if r.reachable and r.partial then return false, r end
    return r.reachable, r
end

-- Another travel_to leg toward `toward` (<= maxM of path) when the planned
-- one failed or did not verify (S002: "leg 2: no walkable path there" ended
-- the whole trip). First a point along the engine's own route (it follows
-- stairs and detours the straight-line samples miss), then the farthest
-- straight sample of a half / quarter leg. Each must verify, get >= 1 m
-- closer, lie >= ALT_EXCLUDE_M from `bad` (the leg end that failed) and on
-- known ground (N.known, as Guard's point_ok: run 1's "another way (along the
-- route)" leg ended on an unseen route point and was refused).
-- Returns a reach_toward-shaped result with alt="route"|"shorter", or nil.
N.ALT_EXCLUDE_M = 1.5
function N.altLeg(charUuid, toward, maxM, bad, avoid)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from or type(toward) ~= "table" then return nil end
    local d0 = U.dist(from, toward)
    local function good(p, searched)
        if type(p) ~= "table" or d0 - U.dist(p, toward) < 1.0 then return false end
        if bad and U.dist(p, bad) < N.ALT_EXCLUDE_M then return false end
        if not N.known(nil, p) then return false end
        return searched or N.verifyPoint(charUuid, p) == true
    end
    local ap = N.approach(charUuid, toward, toward, 1.0, maxM, avoid)
    if ap and ap.reachable and good(ap.pos) then
        return { reachable = true, pos = ap.pos, path_m = ap.walk_m, alt = "route", verified = true,
            needs_jump = ap.needs_jump, dir = N.direction(from, ap.pos), hazard_beyond = ap.hazard }
    end
    for _, m in ipairs({ maxM * 0.5, maxM * 0.25 }) do
        if m >= 2 then
            local r = N.reachToward(charUuid, nil, toward, m, 1, avoid)
            if r and r.reachable and good(r.pos, r.searched) then
                r.alt, r.verified = "shorter", true
                return r
            end
        end
    end
    return nil
end

-- travel_to's long leg: the engine's own route to the goal, walked in one
-- move_to instead of 30 m legs of straight-line samples (S005 on the beach:
-- sampled leg ends on rocks and slopes stopped 2-4 m short, "path blocked").
-- The goal's y is snapped to the floor nearest it (an agent's y is a guess).
-- Only when the whole route is plain walking: a full path (not partial) of
-- <= DIRECT_MAX_M, no jump link, and (avoid) no harmful surface on it. The
-- leg ends at the goal when it is <= DIRECT_LEG_M away in a straight line
-- (Guard refuses moves past G.MAX_MOVE_DIST = 60 m), else at the last route
-- node within it and within DIRECT_LEG_PATH_M of walking, verified. Returns
-- a reach_toward-shaped result with direct=true, or nil, why.
-- S029 16:07: Last Light -> Mausoleum was a 325 m route (2.4x the straight
-- line, first legs SE away from the goal); the 150 m cap sent it to the
-- straight-line sampler: "no walkable ground leads further", 2 min by hand.
N.DIRECT_MAX_M = 600
N.DIRECT_LEG_M = 55
N.DIRECT_LEG_PATH_M = 70
function N.directLeg(charUuid, toward, avoid)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from or type(toward) ~= "table" then return nil, "no position" end
    -- 0.1 m, as handed out: N.known's 1 m key of the point checked is the one Guard's point_ok checks
    local goal = U.vec({ toward[1], floorAt(toward[1], toward[3], toward[2]) or toward[2], toward[3] }, 1)
    -- the engine's whole route only to a goal the party has seen or walked (N.known); else the sampled legs
    if not N.known(nil, goal) then return nil, "unseen" end
    local r = rawPath(e, from, goal, N.PATH_END_TOL_M)
    if not r.reachable then return nil, "no path" end
    if r.partial then return nil, "partial" end
    if r.needs_jump then return nil, "jump" end
    if (r.length_m or 0) > N.DIRECT_MAX_M then return nil, "long" end
    local pts = r.points
    if type(pts) ~= "table" or #pts < 2 then return nil, "no route" end
    local pos, walk, last = goal, r.length_m, #pts
    if U.dist(from, goal) > N.DIRECT_LEG_M then
        local walked, idx, acc = 0, nil, 0
        for i = 2, #pts do
            acc = acc + U.dist(pts[i - 1], pts[i])
            if acc > N.DIRECT_LEG_PATH_M then break end -- a switchback node near us is not "along the way"
            -- a leg ends on known ground (Guard's point_ok refuses a move_to anywhere else)
            if U.dist(from, pts[i]) <= N.DIRECT_LEG_M and N.known(nil, U.vec(pts[i], 1)) then idx, walked = i, acc end
        end
        if not idx or walked < 5 then return nil, "no node in reach" end
        pos, walk, last = pts[idx], walked, idx
    end
    -- Hazards on this leg only: one further along is the next leg's to deal with.
    -- A leg that crosses one comes back as the 3rd value (with its exposure):
    -- reach_toward avoid="least" falls back on it when no clear walk leads on
    -- (S030 16:35 Soft-Step Trial: the only way crossed Grease; "no walkable
    -- ground leads further" although route() had walked it).
    local legPts = {}
    for i = 1, last do legPts[i] = pts[i] end
    if pos == goal and U.dist(legPts[#legPts], goal) > 0.5 then legPts[#legPts + 1] = goal end
    local risky = nil
    if avoid and (N.routeHazard(legPts) or (pos == goal and N.surfaceNear(goal))) then
        risky = N.routeExposure(legPts) or { surface = N.surfaceNear(goal), m = 0, pos = U.vec(goal, 1), at_m = round(walk, 1) }
    end
    if pos ~= goal and N.verifyPoint(charUuid, pos) ~= true then return nil, "leg end unverified" end
    local out = { reachable = true, pos = U.vec(pos, 1), path_m = round(walk, 1), straight_m = round(U.dist(from, pos), 1),
        route_m = r.length_m, route_left_m = round(math.max((r.length_m or 0) - walk, 0), 1),
        direct = true, verified = true, dir = N.direction(from, pos),
        whole = pos == goal or nil }
    if risky then
        out.exposure = N.seenHazard(risky)
        if not out.exposure then return nil, "hazard" end -- not in view: the sampled legs avoid it unnamed
        return nil, "hazard", out
    end
    return out
end

-- Nearest safe floor out of a harmful surface: rings of 8 directions at
-- 1.5..max_m; the shortest path whose end and route (after the first metre)
-- are clear. nil when the character is not in a harmful surface.
-- toward (optional pos, e.g. travel_to's destination): every ring is tried
-- and a spot costs its path plus the distance it loses toward that point, so
-- a spot ahead wins over one as near behind (S001: travel_to kept stepping
-- back E out of the fire, undoing the way on).
N.ESCAPE_MAX_EXPOSURE_M = 4.0

N.inner.activeChar, N.inner.movementLeft = activeChar, movementLeft
