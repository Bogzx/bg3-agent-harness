-- Navigation layer (server context): pathfinding previews, the party's own
-- breadcrumbs (what a player has explored, persisted in the save), and the
-- "frontier" of perceived leads (doors, ladders, unvisited things).
--
-- Fairness: breadcrumbs are the party's own positions; the frontier only uses
-- entities Visibility.lua says the party perceives right now. The engine's map
-- shroud (esv::ShroudManager) is not exposed to Lua, so it is not used.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local N = BG3AI.Nav or {}
BG3AI.Nav = N

local truthy = U.truthy
local osi = V.osi
local round = U.round

N.CELL_XZ = 4.0     -- breadcrumb cell size (m), horizontal
N.CELL_Y = 8.0      -- vertical bucket so stacked floors stay apart
N.PLACE_GAP = 15.0  -- a new "place" once the active character is this far from the last one
N.PLACES_CAP = 60
N.RECORD_MS = 1000
N.PATH_MAX_M = 80
N.ROUTE_MAX_NODES = 400

------------------------------------------------------------------------
-- Persistence (PersistentVars travel with the savegame under bg3le/bg3se)
------------------------------------------------------------------------

-- A file mirror keyed by the host character's uuid (unique per playthrough)
-- keeps breadcrumbs across Ext.Debug.Reset(), which drops in-session
-- PersistentVars that were never written into a save.
local function mirrorPath()
    local host = osi(Osi.GetHostCharacter)
    local u = host and V.uuid(host)
    return u and ("BG3AI/nav_" .. u .. ".json") or nil
end

local function store()
    PersistentVars = PersistentVars or {}
    local pv = PersistentVars
    if not pv.nav then
        local path = mirrorPath()
        local ok, txt = pcall(Ext.IO.LoadFile, path)
        if path and ok and txt and txt ~= "" then
            local okp, data = pcall(Ext.Json.Parse, txt)
            if okp and type(data) == "table" and data.levels then pv.nav = data end
        end
        pv.nav = pv.nav or { levels = {} }
    end
    return pv.nav
end

N.MIRROR_MS = 30000
N.lastMirror = 0
local function mirror()
    local path = mirrorPath()
    if not path or not PersistentVars or not PersistentVars.nav then return end
    pcall(Ext.IO.SaveFile, path, Ext.Json.Stringify(PersistentVars.nav, { Beautify = false }))
end

local function levelStore(level)
    local s = store()
    local l = s.levels[level]
    if not l then
        l = { cells = {}, n = 0, places = {}, bbox = nil }
        s.levels[level] = l
    end
    return l
end

local function cellOf(p)
    return math.floor(p[1] / N.CELL_XZ), math.floor(p[2] / N.CELL_Y), math.floor(p[3] / N.CELL_XZ)
end

local function cellKey(cx, cy, cz) return cx .. ":" .. cy .. ":" .. cz end

local function currentLevel()
    local host = osi(Osi.GetHostCharacter)
    return host and osi(Osi.GetRegion, host) or nil
end

------------------------------------------------------------------------
-- Waypoint display names (TranslatedStringKeys "WAYP_*" in the game's
-- Localization/Waypointshrines*.lsf)
------------------------------------------------------------------------

N.waypointNameCache = N.waypointNameCache or {}
function N.waypointName(id)
    local c = N.waypointNameCache[id]
    if c ~= nil then return c or nil end
    local name
    local ok, h = pcall(Ext.Loca.GetTranslatedStringKey, id)
    if ok and h then name = U.loca(tostring(h)) end
    N.waypointNameCache[id] = name or false
    return name
end

-- Waypoint shrine items of the current level with real names and positions.
N.waypointItems = N.waypointItems or nil
function N.waypointsHere()
    local lvl = currentLevel()
    if N.waypointItems and N.waypointItems.level == lvl then return N.waypointItems.list end
    local list = {}
    local ok, rows = pcall(function() return Osi.DB_WaypointInfo:Get(nil, nil, nil, nil) end)
    if ok and rows then
        for _, r in ipairs(rows) do
            local item = V.uuid(r[3])
            local e = item and V.entity(item)
            local p = e and V.pos(e)
            local okl, l = pcall(function() return e.Level.LevelName end)
            if p and okl and l == lvl then
                list[#list + 1] = { id = r[2], name = N.waypointName(r[2]) or r[2], uuid = item, pos = p }
            end
        end
    end
    N.waypointItems = { level = lvl, list = list }
    return list
end

------------------------------------------------------------------------
-- Breadcrumb recording (Tick, ~1 Hz)
------------------------------------------------------------------------

N.subregion = N.subregion or nil
N.lastRecord = 0

local function record()
    local t = Ext.Utils.MonotonicTime()
    if t - N.lastRecord < N.RECORD_MS then return end
    N.lastRecord = t
    if Ext.Utils.GetGameState and tostring(Ext.Utils.GetGameState()) ~= "Running" then return end
    local lvl = currentLevel()
    if not lvl or lvl:find("^SYS_") then return end
    local party = V.party()
    if #party == 0 then return end
    local l = levelStore(lvl)
    local now = Ext.Utils.GameTime and Ext.Utils.GameTime() or nil
    for _, u in ipairs(party) do
        if truthy(osi(Osi.IsOnStage, u)) then
            local e = V.entity(u)
            local p = e and V.pos(e)
            if p then
                local k = cellKey(cellOf(p))
                if not l.cells[k] then
                    l.cells[k] = 1
                    l.n = l.n + 1
                    local b = l.bbox
                    if not b then
                        l.bbox = { p[1], p[3], p[1], p[3] }
                    else
                        b[1] = math.min(b[1], p[1]); b[2] = math.min(b[2], p[3])
                        b[3] = math.max(b[3], p[1]); b[4] = math.max(b[4], p[3])
                    end
                end
            end
        end
    end
    -- Places: follow the first on-stage party member (the leader in practice).
    local lead = V.entity(party[1])
    local lp = lead and V.pos(lead)
    if lp then
        local last = l.places[#l.places]
        if not last or U.dist({ last.x, last.y, last.z }, lp) >= N.PLACE_GAP then
            l.places[#l.places + 1] = { x = round(lp[1], 1), y = round(lp[2], 1), z = round(lp[3], 1),
                sub = N.subregion, gt = now }
            while #l.places > N.PLACES_CAP do table.remove(l.places, 1) end
        end
    end
end

if not N.tickSubscribed then
    N.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        N.frame = (N.frame or 0) + 1
        local ok, err = pcall(record)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] nav record: " .. tostring(err)) end
        local t = Ext.Utils.MonotonicTime()
        if t - N.lastMirror > N.MIRROR_MS then N.lastMirror = t; pcall(mirror) end
    end)
end

function N.visited(level, p, slack)
    local l = store().levels[level]
    if not l then return false end
    local cx, cy, cz = cellOf(p)
    local r = slack or 1
    for dx = -r, r do
        for dz = -r, r do
            for dy = -1, 1 do
                if l.cells[cellKey(cx + dx, cy + dy, cz + dz)] then return true end
            end
        end
    end
    return false
end

------------------------------------------------------------------------
-- Directions
------------------------------------------------------------------------

-- BG3 world: +X east, +Z north (UNVERIFIED against the in-game compass;
-- dx/dz are returned too so the bridge can correct it in one place).
local DIRS = { "E", "NE", "N", "NW", "W", "SW", "S", "SE" }
function N.direction(from, to)
    local dx, dz = to[1] - from[1], to[3] - from[3]
    if math.abs(dx) < 0.5 and math.abs(dz) < 0.5 then return "here" end
    local a = math.atan(dz, dx) -- radians, 0 = +X
    local i = math.floor(((a / (2 * math.pi)) * 8 + 0.5)) % 8
    return DIRS[i + 1]
end

------------------------------------------------------------------------
-- Pathfinding
------------------------------------------------------------------------

-- Per-frame cache: Guard.lua checks every move, often several times per tick.
N.frame = N.frame or 0
N.pathCache = N.pathCache or {}
N.pathCacheFrame = -1
local function cacheGet(key)
    if N.pathCacheFrame ~= N.frame then N.pathCache = {}; N.pathCacheFrame = N.frame; return nil end
    return N.pathCache[key]
end
local function cachePut(key, v)
    if N.pathCacheFrame ~= N.frame then N.pathCache = {}; N.pathCacheFrame = N.frame end
    N.pathCache[key] = v
end

local function rawPath(e, from, target)
    local t0 = Ext.Utils.MonotonicTime()
    local ok, path = pcall(Ext.Level.BeginPathfindingImmediate, e, target)
    if not ok or not path then
        return { reachable = nil, estimate = true, length_m = round(U.dist(from, target), 1), reason = "pathfinding unavailable" }
    end
    local res
    local okf, found = pcall(Ext.Level.FindPath, path)
    if okf and found then
        local len, prev, jump = 0, from, false
        local okn = pcall(function()
            for _, n in ipairs(path.Nodes) do
                local p = { n.Position[1], n.Position[2], n.Position[3] }
                len = len + U.dist(prev, p)
                prev = p
                -- A node with a portal is a jump/climb/ladder link the pathfinder uses.
                if n.Portal and Ext.Utils.IsValidHandle and Ext.Utils.IsValidHandle(n.Portal) then jump = true end
            end
        end)
        res = { reachable = true, length_m = round(len, 1), nodes = okn and #path.Nodes or nil }
        if jump then res.needs_jump = true end
        -- The route itself (node positions): hazard checks along it, and the
        -- teleport detector accepts big steps that stay on it.
        local pts = { U.vec(from, 2) }
        pcall(function()
            for i, n in ipairs(path.Nodes) do
                if i > N.ROUTE_MAX_NODES then break end
                pts[#pts + 1] = { round(n.Position[1], 2), round(n.Position[2], 2), round(n.Position[3], 2) }
            end
        end)
        res.points = pts
    else
        local cause
        pcall(function() cause = path.ErrorCause end)
        res = { reachable = false, straight_m = round(U.dist(from, target), 1), error_cause = cause }
    end
    pcall(Ext.Level.ReleasePath, path)
    res.ms = Ext.Utils.MonotonicTime() - t0
    return res
end

-- path(char, target): target is a pos {x,y,z} or an entity uuid. For an entity
-- the path may end anywhere within APPROACH_M of it (characters and doors
-- occupy their own tiles). Returns {reachable=true|false|nil, length_m?,
-- needs_jump?, nodes?, approach_pos?, straight_m?, error_cause?, estimate?, ms}.
N.APPROACH_M = 1.5
local RING = { { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 }, { 0.7071, 0.7071 }, { -0.7071, 0.7071 }, { 0.7071, -0.7071 }, { -0.7071, -0.7071 } }
local floorAt -- defined below

function N.path(charUuid, target)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local tpos, isEntity = target, false
    if type(target) == "string" then
        local te = V.entity(target)
        tpos = te and V.pos(te)
        if not tpos then return nil, "unknown target" end
        isEntity = true
    end
    local key = charUuid .. "|" .. (isEntity and target or string.format("%.1f:%.1f:%.1f", tpos[1], tpos[2], tpos[3]))
        .. "|" .. string.format("%.1f:%.1f", from[1], from[3])
    local c = cacheGet(key)
    if c then return c end
    local r = rawPath(e, from, tpos)
    if isEntity and r.reachable == false then
        local t0 = Ext.Utils.MonotonicTime()
        for _, d in ipairs(RING) do
            local x, z = tpos[1] + d[1] * N.APPROACH_M, tpos[3] + d[2] * N.APPROACH_M
            local fy = floorAt and floorAt(x, z, tpos[2]) or tpos[2]
            if fy then
                local r2 = rawPath(e, from, { x, fy, z })
                if r2.reachable then
                    r = r2
                    r.approach_pos = U.vec({ x, fy, z }, 1)
                    break
                end
            end
        end
        r.ms = (r.ms or 0) + (Ext.Utils.MonotonicTime() - t0)
    end
    cachePut(key, r)
    return r
end

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
            or (U.comp(te, "ServerItem") and V.itemVisible(ctx, args.target, te, tp))
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
    local r0 = N.path(char, target)
    if not r0 then error("no path", 0) end
    local r = {}
    for k, v in pairs(r0) do if k ~= "points" then r[k] = v end end
    if r0.points then r.hazard = N.routeHazard(r0.points) end
    local mv = movementLeft(char)
    if mv and r.reachable then r.within_movement = r.length_m <= mv + 0.05; r.movement_left = round(mv, 1) end
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
    local wps = N.waypointsHere()
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

    local function add(kind, uuid, p, extra)
        local d = U.dist(center, p)
        local o = { kind = kind, uuid = uuid, name = V.name(uuid), dist = round(d, 1),
            dir = N.direction(center, p), pos = U.vec(p, 1) }
        if not N.visited(lvl, p, 1) then o.unvisited = true end
        if extra then for k, v in pairs(extra) do o[k] = v end end
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
            if vis then
                if wpSet[u] then
                    add("waypoint", u, p, { waypoint = wpSet[u].name })
                elseif kind == "door" then
                    add("door", u, p, { locked = truthy(osi(Osi.IsLocked, u)) or nil })
                elseif kind == "ladder" then
                    add("ladder", u, p)
                elseif kind == "container" and not U.comp(e, "HasOpened") then
                    add("container", u, p, { locked = truthy(osi(Osi.IsLocked, u)) or nil })
                elseif not N.visited(lvl, p, 1) and (kind == "usable" or kind == "item") then
                    add(kind, u, p)
                end
            end
        end
    end
    -- Characters the party perceives but hasn't been near.
    local ok, chars = pcall(Ext.Entity.GetAllEntitiesWithComponent, "ServerCharacter")
    if ok and chars then
        for _, e in ipairs(chars) do
            local p = V.pos(e)
            if p and math.abs(p[1] - center[1]) <= radius and math.abs(p[3] - center[3]) <= radius then
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
    -- Rank: unvisited first, then distance.
    table.sort(leads, function(a, b)
        if (a.unvisited or false) ~= (b.unvisited or false) then return a.unvisited == true end
        return a.dist < b.dist
    end)
    local out = { level = lvl, leads = {} }
    for i, o in ipairs(leads) do
        if i > limit then out.more = #leads - limit; break end
        out.leads[#out.leads + 1] = o
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

local DIR_VEC = {
    E = { 1, 0 }, NE = { 0.7071, 0.7071 }, N = { 0, 1 }, NW = { -0.7071, 0.7071 },
    W = { -1, 0 }, SW = { -0.7071, -0.7071 }, S = { 0, -1 }, SE = { 0.7071, -0.7071 },
}

-- Floor height at (x, z) closest to y (multi-floor aware); nil if no floor.
floorAt = function(x, z, y)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
    if not ok or type(hs) ~= "table" then return nil end
    local best
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - y) <= 4.0 and (not best or math.abs(h - y) < math.abs(best - y)) then best = h end
    end
    return best
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
function N.surfaceAt(pos)
    if not pos then return nil end
    local ok, t = pcall(Ext.Level.GetTileDebugInfo, { pos[1], pos[2], pos[3] })
    if not ok or not t then return nil end
    local g, c
    pcall(function() g = tostring(t.GroundSurface) end)
    pcall(function() c = tostring(t.CloudSurface) end)
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

-- Farthest reachable floor point along a compass direction (or toward pos):
-- the "walk west as far as you can" primitive. Samples every `step` m up to
-- max_m; a sample counts only if pathable and the path is direct
-- (length <= DETOUR x straight distance), i.e. floor a player sees that way.
-- avoid (default true): skip samples standing in, or reached through, a
-- harmful surface; the farthest safe sample wins. If every sample is unsafe
-- the result is {reachable=false, hazard={surface,pos,at_m}}.
N.DETOUR = 1.5
function N.reachToward(charUuid, dir, toward, maxM, step, avoid)
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
    local cands, tried = {}, 0
    local y = from[2]
    local d = step
    -- the destination itself is a sample too (not just multiples of step)
    local function sample(dd)
        local x, z = from[1] + vx * dd, from[3] + vz * dd
        local fy = floorAt(x, z, y)
        if fy then
            tried = tried + 1
            local r = N.path(charUuid, { x, fy, z })
            if r and r.reachable and r.length_m <= math.max(N.DETOUR * dd, dd + 2.0) then
                cands[#cands + 1] = { pos = { x, fy, z }, path_m = r.length_m, straight_m = round(dd, 1),
                    needs_jump = r.needs_jump, points = r.points }
                y = fy -- follow ramps and stairs
            end
        end
    end
    while d <= maxM + 0.01 do
        sample(d)
        d = d + step
    end
    if maxM - (d - step) > 0.5 then sample(maxM) end
    if #cands == 0 then return { reachable = false, samples = tried } end
    local best, firstHazard, avoided = nil, nil, 0
    for i = #cands, 1, -1 do
        local c = cands[i]
        local hz = nil
        if avoid then
            local s = N.surfaceNear(c.pos)
            hz = s and { surface = s, pos = U.vec(c.pos, 1), at_m = c.path_m } or N.routeHazard(c.points)
        end
        if not hz then best = c; break end
        avoided = avoided + 1
        firstHazard = firstHazard or hz
    end
    if not best then return { reachable = false, samples = tried, hazard = firstHazard } end
    local out = { pos = U.vec(best.pos, 1), path_m = best.path_m, straight_m = best.straight_m,
        needs_jump = best.needs_jump, reachable = true, samples = tried, dir = N.direction(from, best.pos) }
    if avoided > 0 then out.avoided = avoided; out.hazard_beyond = firstHazard end
    return out
end

-- Nearest safe floor out of a harmful surface: rings of 8 directions at
-- 1.5..max_m; the shortest path whose end and route (after the first metre)
-- are clear. nil when the character is not in a harmful surface.
function N.safeSpot(charUuid, maxM)
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local under = N.surfaceAt(from)
    if not under then return { safe = true } end
    maxM = maxM or 8
    local best
    for _, rad in ipairs({ 1.5, 3, 5, 8, 12 }) do
        if rad > maxM + 0.01 then break end
        for _, dname in ipairs(DIRS) do
            local v = DIR_VEC[dname]
            local x, z = from[1] + v[1] * rad, from[3] + v[2] * rad
            local fy = floorAt(x, z, from[2])
            if fy and not N.surfaceNear({ x, fy, z }) then
                local r = N.path(charUuid, { x, fy, z })
                if r and r.reachable and r.length_m <= rad * 2 + 2 then
                    -- the start is burning anyway: only the rest must be clear
                    local hz = N.routeHazard(r.points, math.min(rad, 2.0))
                    if not hz and (not best or r.length_m < best.path_m) then
                        best = { pos = U.vec({ x, fy, z }, 1), path_m = r.length_m, dir = dname }
                    end
                end
            end
        end
        if best then break end
    end
    if not best then return { safe = false, standing_in = under, found = false } end
    best.safe = false
    best.standing_in = under
    best.found = true
    return best
end

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
            local q = { id = qid, title = block:match('id="QuestTitle" type="TranslatedString" handle="([^"]*)"'), steps = {} }
            for st in block:gmatch('<node id="QuestStep">(.-)</node>') do
                q.steps[#q.steps + 1] = {
                    id = st:match('id="ID" type="FixedString" value="([^"]*)"'),
                    objective = st:match('id="Objective" type="FixedString" value="([^"]*)"'),
                }
            end
            if qid then quests[#quests + 1] = q end
            pos = qe
        end
    end
    N.stepObjectives = quests
    return quests
end

-- Accepted, open quests and the objective of their latest unlocked step.
-- Walking every quest's steps costs a few hundred Osiris calls, and get_status
-- asks every turn, so the list is reused for MARKER_CACHE_MS.
N.MARKER_CACHE_MS = 2000
N.activeCache = N.activeCache or nil
local function activeObjectives(who)
    local now = Ext.Utils.MonotonicTime()
    local c = N.activeCache
    if c and c.who == who and now - c.t < N.MARKER_CACHE_MS then return c.list end
    local list = {}
    for _, q in ipairs(stepObjectives()) do
        if truthy(osi(Osi.QuestIsAccepted, who, q.id)) and not truthy(osi(Osi.QuestIsClosed, q.id)) then
            local current
            for _, st in ipairs(q.steps) do
                if st.id and truthy(osi(Osi.QuestUpdateIsUnlocked, who, q.id, st.id)) then current = st end
            end
            if current and current.objective then list[#list + 1] = { quest = q, objective = current.objective } end
        end
    end
    N.activeCache = { who = who, t = now, list = list }
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

RPC.register("nav.markers", function(args)
    local t0 = Ext.Utils.MonotonicTime()
    local list, err = N.objectiveMarkers(V.context())
    return { markers = list, error = err, ms = Ext.Utils.MonotonicTime() - t0 }
end)

RPC.register("nav.reach_toward", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local toward = type(args.pos) == "table" and args.pos or nil
    if args.target then
        local te = V.entity(args.target)
        local tp = te and V.pos(te)
        if not tp then error("unknown target", 0) end
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tp)
            or (U.comp(te, "ServerItem") and V.itemVisible(ctx, args.target, te, tp))
        if not visible then error("not visible", 0) end
        toward = tp
    end
    local maxM = math.min(tonumber(args.max_m) or 30, N.PATH_MAX_M)
    local step = math.max(tonumber(args.step) or 2, 1)
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.reachToward(char, args.dir, toward, maxM, step, args.avoid ~= false)
    if not r then error(err, 0) end
    local under = N.harmfulUnder(char)
    if under then r.standing_in = under end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.safe_spot {char?, max_m?}: where to step out of a harmful surface.
-- {safe=true} when not standing in one; else {standing_in, found, pos?, path_m?, dir?}.
RPC.register("nav.safe_spot", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.safeSpot(char, math.min(tonumber(args.max_m) or 8, 12))
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- Cheap activity sample for latency metrics (the bridge polls it ~1/s on its
-- own connection): party positions (the bridge diffs them to detect movement),
-- running jobs, dialogue and combat flags. Party-only data, nothing hidden.
RPC.register("nav.activity", function(args)
    local party = V.party()
    local out = { pos = {}, jobs = 0, job_kinds = {}, in_dialogue = false, in_combat = false }
    for _, u in ipairs(party) do
        if truthy(osi(Osi.IsOnStage, u)) then
            local e = V.entity(u)
            local p = e and V.pos(e)
            if p then
                out.pos[u] = U.vec(p, 2)
                local s = N.surfaceAt(p)
                if s then out.surfaces = out.surfaces or {}; out.surfaces[u] = s end
            end
            if truthy(osi(Osi.IsInCombat, u)) then out.in_combat = true end
            if not out.in_dialogue then
                local d = false
                pcall(function() d = e.ServerCharacter.InDialog == true end)
                out.in_dialogue = d or truthy(osi(Osi.IsInDialog, u))
            end
        end
    end
    for _, j in pairs(RPC.jobs or {}) do
        if j.status == "running" then
            out.jobs = out.jobs + 1
            out.job_kinds[#out.job_kinds + 1] = j.kind
        end
    end
    return out
end)

return N
