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

-- The avatar's uuid (DB_Avatars), the key of this playthrough's files; nil before an avatar exists. Not
-- GetHostCharacter: that is the character the player controls now, a companion after a party switch or on its
-- combat turn, and a companion's uuid is its template's, the same in every playthrough (Window C 2026-10-24:
-- Vessa's map went into nav_<Shadowheart/Lae'zel/Gale>.json over other playthroughs' maps).
local function avatarUuid()
    local ok, rows = pcall(function() return Osi.DB_Avatars:Get(nil) end)
    local a = ok and type(rows) == "table" and rows[1] and rows[1][1]
    return a and V.uuid(a) or nil
end

-- A file mirror keyed by the avatar's uuid (unique per playthrough)
-- keeps breadcrumbs across Ext.Debug.Reset(), which drops in-session
-- PersistentVars that were never written into a save.
local function mirrorPath()
    local u = avatarUuid()
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

-- Ids (WAYP_*) of the waypoints the party has unlocked (the map's list; the
-- same PartyWaypoints set State.map reads). Empty if unreadable: fail closed.
function N.unlockedWaypointIds()
    local set = {}
    local ok, list = pcall(function() return Ext.Entity.GetAllEntitiesWithComponent("PartyWaypoints") end)
    if ok and list and list[1] then
        pcall(function()
            for _, w in pairs(list[1].PartyWaypoints.Waypoints) do set[tostring(w.Name)] = true end
        end)
    end
    return set
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
        if N.inflightExpire then pcall(N.inflightExpire, t) end
        if N.linkWatch then pcall(N.linkWatch, t) end
        if N.waysWarm then
            local okw, errw = pcall(N.waysWarm, t)
            if not okw and not N.waysWarmWarned then
                N.waysWarmWarned = true
                Ext.Utils.PrintWarning("[BG3AI] nav ways warm: " .. tostring(errw))
            end
        end
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

-- Known ground (FAIRNESS.md: what a player saw or remembers seeing). A point is known when a party member stood in
-- its breadcrumb cell (the 4 m cell itself, same height band), or a party member sees it now (V.pointSeen: within
-- sight range, a clear ray from the eye to 0.5 m above it). A point once seen stays known this session (1 m cells,
-- 2 m height bands). Fail closed: no ray answer (uncalibrated, broken) is not seen. Nav answers about a goal that is
-- not known only with its straight distance and direction; walks end only on known ground.
N.SEEN_MAX = 20000
N.seenPts = N.seenPts or {}
N.seenPtsN = N.seenPtsN or 0
function N.known(ctx, p)
    if type(p) ~= "table" then return false end
    local lvl = currentLevel() or "?"
    local l = store().levels[lvl]
    if l and l.cells[cellKey(cellOf(p))] then return true end
    local k = string.format("%s|%d:%d:%d", lvl, math.floor(p[1]), math.floor(p[2] / 2), math.floor(p[3]))
    if N.seenPts[k] then return true end
    local ok, s = pcall(V.pointSeen, ctx or V.context(), p)
    if not (ok and s == true) then return false end
    if N.seenPtsN >= N.SEEN_MAX then N.seenPts, N.seenPtsN = {}, 0 end
    N.seenPts[k], N.seenPtsN = true, N.seenPtsN + 1
    return true
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

-- Walls, doors and blockers the game opens, closes or takes away (V13, S071 Wyrmway: a Stone Wall opened by a use,
-- an illusory wall opened by lightning on two torches; the story also does it with Open(), SetOnStage and
-- PlatformMoveTo). Every path answer kept across frames (the async warm-up answers, the slow-search memo, the ways
-- per spot, the connector failures) may then describe the old geometry, so a change within GEO_NEAR_M of the party
-- drops them all (geoGen). A sliding wall's walk grid can lag its Opened event: for GEO_SETTLE_MS after it a "no
-- path" is not kept (searched again next time), and no-path replies carry `opened` (a door/wall the party can see,
-- nothing else: a hidden blocker going off stage only empties the caches) so the agent knows to try again.
N.GEO_NEAR_M = 40
N.GEO_SETTLE_MS = 6000
N.GEO_NOTE_MS = 20000
N.geoGen = N.geoGen or 0
function N.geoSettling(now)
    local g = N.geoLast
    return g ~= nil and (now or Ext.Utils.MonotonicTime()) - g.t < N.GEO_SETTLE_MS
end
function N.geometryChanged(uuid, how)
    local e = uuid and V.entity(uuid)
    local p = e and V.pos(e)
    if not p then return false end
    local near = false
    for _, o in ipairs(V.context().observers or {}) do
        if o.pos and U.dist(o.pos, p) <= N.GEO_NEAR_M then near = true end
    end
    if not near then return false end
    N.geoGen = N.geoGen + 1
    local shown = (how == "opened" or how == "closed") and V.itemKind(uuid, e) == "door"
        and V.itemVisible(V.context(), uuid, e, p)
    N.geoLast = { uuid = uuid, how = how, t = Ext.Utils.MonotonicTime(), pos = p, shown = shown or nil }
    N.pathCache, N.asyncPaths, N.asyncPathsN = {}, {}, 0
    N.slowPaths, N.slowPathsN = {}, 0
    N.waysCache, N.waysCacheN = {}, 0
    N.connectFail = {}
    return true
end
-- {uuid, name, how, ago_s, settling?} for a no-path reply from `from` within GEO_NOTE_MS of a shown change, else nil
function N.geoNote(from)
    local g = N.geoLast
    local now = Ext.Utils.MonotonicTime()
    if not g or not g.shown or now - g.t > N.GEO_NOTE_MS or (from and U.dist(from, g.pos) > N.GEO_NEAR_M) then
        return nil
    end
    return { uuid = g.uuid, name = V.name(g.uuid), how = g.how, ago_s = round((now - g.t) / 1000, 0),
             settling = N.geoSettling(now) or nil }
end
if Ext.Osiris and not N.geoListening then
    N.geoListening = true
    for _, ev in ipairs({ { "Opened", 1, "opened" }, { "Closed", 1, "closed" }, { "WentOnStage", 2, "stage" },
        { "DestroyedBy", 4, "destroyed" } }) do
        pcall(Ext.Osiris.RegisterListener, ev[1], ev[2], "after", function(obj)
            local u = V.uuid(obj)
            if u and truthy(osi(Osi.IsItem, u)) then pcall(N.geometryChanged, u, ev[3]) end
        end)
    end
end

-- The engine's path search has a per-frame budget: in one frame only the
-- first 3-4 immediate searches run, the rest come back GoalFound=false with
-- SearchComplete=false (live, WLD_Main_A: 8 identical searches -> 4 ok, 4
-- "no path"). So a 17-sample reachToward said "no walkable ground" on open
-- beach. With IgnoreEntities set, bg3le runs the search through the grid's
-- own step, which resets that budget (10/10 ok, ~5.6 ms each). Ignoring the
-- source itself changes nothing (its own tile is WalkBlockCharacter).
-- A found path can also end short of the target (the goal is on a rock or
-- off the mesh: the last node 3+ m from it, where a normal path ends ~1 m
-- from it); with endTol such a path is flagged partial (end_pos, end_off).
------------------------------------------------------------------------
-- The Faith-Leap walkway (user 2026-10-18, FAIRNESS.md): the safe path over
-- that trial's chasm is a layer of invisible-platform helpers (V.helperItem),
-- and the engine's floor there is that layer. A player sees no floor there,
-- so nav never answers from it: its heights are no floor (N.floorHeights)
-- and a found path that steps on it is no path (readResult), the same answer
-- as for the chasm around it. A move_to the agent aims there itself is the
-- player's own risk: Guard asks N.helperZone and lets the game decide.
------------------------------------------------------------------------

N.HELPER_LAYER_M = 0.6   -- a floor this close to a helper's top is the hidden walkway
N.HELPER_MARGIN_M = 3.0  -- around the helpers' bounding box (the layout's blank band)
N.HELPER_PATH_M = 1.0    -- around the box for paths and floor heights (about half a platform)
N.HELPER_ZONE_DY_M = 2.0 -- Guard's zone: this near the walkway's height
N.helperCache = N.helperCache or {}

-- Invisible-platform helper positions on this level (one scan per level; the level is read once per frame, as
-- every floor-height read asks: N.floorHeights).
function N.helpers()
    local c = N.helperCache
    if c.list and c.frame == N.frame then return c.list end
    local lvl = currentLevel()
    if c.level == lvl and c.list then c.frame = N.frame; return c.list end
    local list = {}
    local ok, all = pcall(function() return Ext.Entity.GetAllEntitiesWithComponent("ServerItem") end)
    for _, e in ipairs(ok and all or {}) do
        local u = e.Uuid and e.Uuid.EntityUuid
        if u and V.helperItem and V.helperItem(u) then
            local p = V.pos(e)
            if p then list[#list + 1] = p end
        end
    end
    c.level, c.list, c.frame = lvl, list, N.frame
    return list
end

-- Bounding boxes of helper clusters near `from` (within `near` m), with the
-- walkway layer height.
local function helperBox(from, near)
    local box
    for _, p in ipairs(N.helpers()) do
        if math.abs(p[1] - from[1]) <= near + 40 and math.abs(p[3] - from[3]) <= near + 40 then
            if not box then box = { x1 = p[1], x2 = p[1], z1 = p[3], z2 = p[3], y = p[2], pts = {} } end
            box.x1, box.x2 = math.min(box.x1, p[1]), math.max(box.x2, p[1])
            box.z1, box.z2 = math.min(box.z1, p[3]), math.max(box.z2, p[3])
            box.pts[#box.pts + 1] = p
        end
    end
    return box
end

local function inBox(b, x, z, m)
    return b and x >= b.x1 - m and x <= b.x2 + m and z >= b.z1 - m and z <= b.z2 + m
end


local function onLayer(box, x, y, z, m)
    return inBox(box, x, z, m or N.HELPER_PATH_M) and math.abs(y - (box.y + 0.2)) <= N.HELPER_LAYER_M
end

local function layerBox(x, z)
    if #N.helpers() == 0 then return nil end
    return helperBox({ x, 0, z }, 0)
end

-- Ext.Level.GetHeightsAt without the walkway layer, pcall-shaped (ok, heights).
function N.floorHeights(x, z)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
    if not ok or type(hs) ~= "table" then return ok, hs end
    local box = layerBox(x, z)
    if not box then return ok, hs end
    local out = {}
    for _, h in ipairs(hs) do
        if not (type(h) == "number" and onLayer(box, x, h, z)) then out[#out + 1] = h end
    end
    return true, out
end

-- Over the chasm at the walkway's height (Guard: a move there is not proven or refused).
function N.helperZone(p)
    local box = type(p) == "table" and layerBox(p[1], p[3])
    return box and inBox(box, p[1], p[3], N.HELPER_MARGIN_M) and math.abs(p[2] - (box.y + 0.2)) <= N.HELPER_ZONE_DY_M
        or false
end

-- Does a route (points) step on the walkway layer? Sampled every 0.5 m.
local function onWalkway(points)
    if type(points) ~= "table" or #points < 1 or #N.helpers() == 0 then return false end
    local box = helperBox(points[1], 40)
    if not box then return false end
    if onLayer(box, points[1][1], points[1][2], points[1][3]) then return true end
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local n = math.max(1, math.ceil(U.dist(a, b) / 0.5))
        for k = 1, n do
            local f = k / n
            if onLayer(box, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f) then
                return true
            end
        end
    end
    return false
end

N.PATH_MAX_NODES = 5000 -- more is not a real route (S038: runaway node walk); ~2.5 km tile by tile
N.PATH_SLOW_MS = 1000
N.PATH_END_TOL_M = 2.0 -- the walker's own "close enough" (Actions walk_close_enough)
-- A finished search's result (the sync FindPath here, or an async request's callback, N.waysWarm).
local function readResult(path, found, from, target, endTol)
    -- S038 20:48 (Shadowfell): the server hung inside nav.landing and the game's RSS went 5 -> 13 GB in <40 s
    -- (OOM kill). The node walk was the one loop here without a bound (ipairs over the engine's array, a proxy per
    -- node and per Position read): a found path whose node count is garbage or runaway allocated without end. The
    -- count is read once and checked; a path longer than PATH_MAX_NODES is not trusted (reachable unknown).
    local nn
    if found then
        local okc, c = pcall(function() return #path.Nodes end)
        nn = okc and tonumber(c) or nil
        if not nn or nn > N.PATH_MAX_NODES then
            if nn then
                Ext.Utils.Print(string.format("[BG3AI] path: %s nodes to (%.1f,%.1f,%.1f) from (%.1f,%.1f,%.1f), not read",
                    tostring(nn), target[1], target[2], target[3], from[1], from[2], from[3]))
            end
            return { reachable = nil, estimate = true, length_m = round(U.dist(from, target), 1),
                reason = "the engine's path is unreadable (" .. tostring(nn) .. " nodes)" }
        end
    end
    local res
    if found then
        local len, prev, jump, portals = 0, from, false, {}
        -- The route itself (node positions): hazard checks along it, and the
        -- teleport detector accepts big steps that stay on it.
        local pts = { U.vec(from, 2) }
        local okn = pcall(function()
            local nodes = path.Nodes
            for i = 1, nn do
                local n = nodes[i]
                if n == nil then break end
                local q = n.Position
                local p = { q[1], q[2], q[3] }
                len = len + U.dist(prev, p)
                prev = p
                if i <= N.ROUTE_MAX_NODES then pts[#pts + 1] = { round(p[1], 2), round(p[2], 2), round(p[3], 2) } end
                -- A node with a portal is a jump/climb/ladder link the pathfinder uses.
                if n.Portal and Ext.Utils.IsValidHandle and Ext.Utils.IsValidHandle(n.Portal) then
                    jump = true
                    if i <= N.ROUTE_MAX_NODES then portals[#portals + 1] = i + 1 end -- index into points
                end
            end
        end)
        res = { reachable = true, length_m = round(len, 1), nodes = okn and nn or nil }
        if jump then res.needs_jump = true end
        if #portals > 0 then res.portals = portals end
        res.points = pts
        -- the Faith-Leap walkway is no floor a player sees: a path on it is no path, as over the chasm beside it
        if onWalkway(pts) then return { reachable = false, straight_m = round(U.dist(from, target), 1) } end
        local last = pts[#pts]
        if endTol and #pts >= 2 and U.dist(last, target) > endTol then
            res.partial, res.end_pos, res.end_off = true, U.vec(last, 1), round(U.dist(last, target), 1)
        end
    else
        local cause
        pcall(function() cause = path.ErrorCause end)
        res = { reachable = false, straight_m = round(U.dist(from, target), 1), error_cause = cause }
    end
    return res
end

local function rawPath(e, from, target, endTol)
    local t0 = Ext.Utils.MonotonicTime()
    local ok, path = pcall(Ext.Level.BeginPathfindingImmediate, e, target)
    if not ok or not path then
        return { reachable = nil, estimate = true, length_m = round(U.dist(from, target), 1), reason = "pathfinding unavailable" }
    end
    pcall(function() path.IgnoreEntities = { e } end)
    if N.PATH_HORIZON then pcall(function() path.SearchHorizon = N.PATH_HORIZON end) end
    local okf, found = pcall(Ext.Level.FindPath, path)
    local res = readResult(path, okf and found, from, target, endTol)
    pcall(Ext.Level.ReleasePath, path)
    res.ms = Ext.Utils.MonotonicTime() - t0
    -- a FindPath that freezes the game (S025: 1.6-2.3 s no-path searches; S038: one that never returned and took
    -- the RSS to 13 GB) is named in the log with its endpoints
    if res.ms > N.PATH_SLOW_MS then
        Ext.Utils.PrintWarning(string.format("[BG3AI] slow FindPath %d ms (%.1f,%.1f,%.1f)->(%.1f,%.1f,%.1f) found=%s",
            math.floor(res.ms), from[1], from[2], from[3], target[1], target[2], target[3], tostring(res.reachable)))
    end
    return res
end

-- Per-level kill switch (se's S038 verdict: the engine's FindPath never ended on the Shadowfell grid). A level where
-- searches took over PATH_LEVEL_KILL_MS SLOW_SEARCH_STRIKES times since the load, where two async warm-up searches
-- timed out, or where the game died while background/jump-planning searches were in flight (the in-flight marker
-- below is still in the file at the next load) gets no warm-ups, no jump planning and no jumpGap/jumpAcross probes;
-- replies say so (plan_skipped). Cleared with nav.level_guard {clear=true}.
-- NAV-3 (audit 2026-10-13): the list was permanent and machine-global, so one slow search or one unrelated crash
-- took jumps away for every later playthrough and benchmark run. Now: one file per playthrough (the host
-- character's uuid, like the breadcrumb mirror; the machine-wide GUARD_FILE only before a host exists), a kill
-- expires after KILL_TTL_S of wall time, one slow search is a strike (SLOW_SEARCH_STRIKES of them kill), and a jump
-- marker counts only for a death within JUMP_MARK_MAX_MS of the cast (the Tick drops an older one: the game lived).
N.GUARD_FILE = "BG3AI/nav_guard.json"
N.PATH_LEVEL_KILL_MS = 800
N.SLOW_SEARCH_STRIKES = 2
N.WARM_DEATH_STRIKES = 2
N.KILL_TTL_S = 6 * 3600
N.JUMP_MARK_MAX_MS = 15000
-- wall clock (s); tests replace it. Not os.time: the SE sandbox has no `os` (bg3le sets it to nil), so every kill
-- was saved undated and never expired (L8 2026-10-18: CTY_Main_A "async path searches timed out 2x", no t).
N.nowS = function() return Ext.Timer and Ext.Timer.ClockEpoch and Ext.Timer.ClockEpoch() or nil end
N.slowHits = N.slowHits or {}
local guard, guardPath
function N.guardFile()
    local u = avatarUuid()
    return u and ("BG3AI/nav_guard_" .. u .. ".json") or N.GUARD_FILE
end
local function guardSave()
    pcall(Ext.IO.SaveFile, guardPath or N.GUARD_FILE, Ext.Json.Stringify(guard, { Beautify = false }))
end
-- a kill older than KILL_TTL_S is dropped (with its strikes); a kill with no time (an older file) is dated now
local function expireKills(g)
    local now, changed = N.nowS(), false
    if not now then return false end
    for lv, v in pairs(g.slow) do
        if type(v) ~= "table" then v = { why = tostring(v) }; g.slow[lv] = v; changed = true end
        if not tonumber(v.t) then v.t = now; changed = true
        elseif now - v.t > N.KILL_TTL_S then
            g.slow[lv] = nil
            if g.strikes then g.strikes[lv] = nil end
            changed = true
            Ext.Utils.PrintWarning("[BG3AI] nav guard: " .. tostring(lv) .. " back on (its kill expired)")
        end
    end
    return changed
end
local function guardState()
    local path = N.guardFile()
    if guard and guardPath == path then return guard end
    guard, guardPath = { slow = {}, strikes = {} }, path
    local ok, txt = pcall(Ext.IO.LoadFile, path)
    if ok and type(txt) == "string" and txt ~= "" then
        local okp, d = pcall(Ext.Json.Parse, txt)
        if okp and type(d) == "table" then
            guard.slow = type(d.slow) == "table" and d.slow or {}
            guard.strikes = type(d.strikes) == "table" and d.strikes or {}
            -- kills by the old rule (any 2 stops during a warm-up, stuck or not: BGO_Main_A 2026-10-11) are dropped;
            -- a stuck warm-up's kill reads "a stuck ways warm"
            for lv, v in pairs(guard.slow) do
                if type(v) == "table" and v.why == "the game died during ways warm" then
                    guard.slow[lv], guard.strikes[lv] = nil, nil
                    Ext.Utils.PrintWarning("[BG3AI] nav guard: " .. tostring(lv) .. " back on (an old warm-up strike rule)")
                end
            end
            local dirty = expireKills(guard)
            -- NAV-7: one marker per op (inflights[op]); an older file has the single `inflight`
            local marks = {}
            if type(d.inflight) == "table" then marks[#marks + 1] = d.inflight end
            if type(d.inflights) == "table" then
                for _, f in pairs(d.inflights) do if type(f) == "table" then marks[#marks + 1] = f end end
            end
            for _, f in ipairs(marks) do
            if type(f) == "table" and f.level then
                -- A background warm-up request is in flight most of the time the party stands still, so a planned
                -- game kill (the runner's 2 h relaunch, 2026-10-11 04:39) left its marker and the next start put
                -- the whole Lower City on the kill list. Warm-ups need N.WARM_DEATH_STRIKES deaths on a level; a
                -- jump cast / jump plan (the real S038/S039 OOMs) still kills it at once.
                -- 2026-10-11: a 2 h relaunch (09:08) + S060's crash in a debug-port eval (09:11, se) were 2 strikes
                -- and put all of Act 3's BGO_Main_A off (S058 08:04 "No jump plan … the game died during ways warm").
                -- A warm-up only strikes when its request was STUCK (unanswered past N.WARM_STUCK_MS, marked by the
                -- Tick): a fresh request (answered in ~ms) out when the game was killed or crashed elsewhere is no clue.
                local n = tonumber(guard.strikes[f.level]) or 0
                local counts = f.op ~= "ways warm" or f.stuck == true
                if counts then n = n + 1 end
                guard.strikes[f.level] = n > 0 and n or nil
                if not counts then
                    Ext.Utils.PrintWarning(string.format("[BG3AI] nav guard: the game stopped during a fresh %s on %s"
                        .. " (a kill or a crash elsewhere): no strike", tostring(f.op), tostring(f.level)))
                elseif f.op ~= "ways warm" or n >= N.WARM_DEATH_STRIKES then
                    guard.slow[f.level] = guard.slow[f.level] or { why = "the game died during "
                        .. (f.op == "ways warm" and "a stuck ways warm" or tostring(f.op)), ms = -1, t = N.nowS() }
                    Ext.Utils.PrintError(string.format("[BG3AI] nav guard: the game died during %s on %s @%s: warm-ups"
                        .. " and jump planning are off on that level", tostring(f.op), tostring(f.level), tostring(f.at)))
                else
                    Ext.Utils.PrintWarning(string.format("[BG3AI] nav guard: the game stopped during %s on %s (strike %d of"
                        .. " %d; a relaunch also does this)", tostring(f.op), tostring(f.level), n, N.WARM_DEATH_STRIKES))
                end
                dirty = true
            end
            end
            if dirty then guardSave() end
        end
    end
    return guard
end
N.guardState = function() return guardState() end
N.guardReset = function() guard, guardPath = nil, nil end -- tests
function N.levelSlow(level)
    level = level or currentLevel()
    if not level then return nil end
    local g = guardState()
    local v = g.slow[level]
    -- a kill can expire while the game runs (a long session)
    if v and tonumber(v.t) and N.nowS() and N.nowS() - v.t > N.KILL_TTL_S then
        if expireKills(g) then guardSave() end
        v = g.slow[level]
    end
    return v
end
function N.killLevel(level, why, ms)
    level = level or currentLevel()
    if not level then return end
    local g = guardState()
    if g.slow[level] then return end
    g.slow[level] = { why = why, ms = ms and math.floor(ms) or nil, t = N.nowS() }
    Ext.Utils.PrintError(string.format("[BG3AI] nav guard: %s on %s: warm-ups and jump planning off on this level",
        tostring(why), tostring(level)))
    guardSave()
end
-- A sync search slower than PATH_LEVEL_KILL_MS (S025: 1.6-2.3 s no-path searches on ordinary big levels): a strike;
-- SLOW_SEARCH_STRIKES of them on a level since this load kill it.
function N.slowSearch(ms, level)
    level = level or currentLevel()
    if not level then return end
    local n = (N.slowHits[level] or 0) + 1
    N.slowHits[level] = n
    if n >= N.SLOW_SEARCH_STRIKES then
        N.killLevel(level, string.format("%d path searches took over %d ms (the last %d ms)", n, N.PATH_LEVEL_KILL_MS,
            math.floor(ms)), ms)
    else
        Ext.Utils.PrintWarning(string.format("[BG3AI] nav guard: a path search took %d ms on %s (strike %d of %d)",
            math.floor(ms), tostring(level), n, N.SLOW_SEARCH_STRIKES))
    end
end
-- In-flight markers around background and jump-planning searches: a crash inside one leaves it in the file.
-- NAV-7: one per op (inflights[op]): a warm-up answer used to clear the one shared slot and erase a jump cast's
-- marker set ~1 s earlier, so the guard missed the very crash it exists for. N.inflight(op, at) sets op's marker,
-- N.inflightDone(op) clears it (N.inflight(nil) clears all: older callers).
function N.inflight(op, at)
    local g = guardState()
    if op then
        g.inflights = g.inflights or {}
        g.inflights[op] = { level = currentLevel(), op = op, at = at and U.vec(at, 1) and table.concat(U.vec(at, 1), ","),
            mt = Ext.Utils.MonotonicTime() }
    else
        g.inflights = nil
    end
    guardSave()
end
function N.inflightDone(op)
    local g = guardState()
    if g.inflights and g.inflights[op] then
        g.inflights[op] = nil
        if next(g.inflights) == nil then g.inflights = nil end
        guardSave()
    end
end
function N.inflightOf(op) local g = guardState(); return g.inflights and g.inflights[op] or nil end
-- Tick (NAV-3): a jump marker older than JUMP_MARK_MAX_MS with the game still running was not a jump that froze the
-- engine (a job that never finished, an error path): dropped, so a later unrelated death doesn't kill the level.
function N.inflightExpire(now)
    local f = guard and guard.inflights and guard.inflights.jump
    if f and f.mt and now - f.mt > N.JUMP_MARK_MAX_MS then N.inflightDone("jump") end
end
-- The in-flight warm-up is stuck (unanswered past N.WARM_STUCK_MS): a death from here on is a strike.
N.WARM_STUCK_MS = 1000
function N.inflightStuck()
    local f = N.inflightOf("ways warm")
    if f and not f.stuck then f.stuck = true; guardSave() end
end
-- Answered warm-ups on a level since this load; N.WARM_CLEAN_RESET of them clear its old strikes (minutes of
-- healthy searches: an old strike was a kill, not this level's searches).
N.WARM_CLEAN_RESET = 100
N.warmClean = N.warmClean or {}
function N.warmAnswered(level)
    if not level then return end
    local n = (N.warmClean[level] or 0) + 1
    N.warmClean[level] = n
    if n == N.WARM_CLEAN_RESET then
        local g = guardState()
        if g.strikes and g.strikes[level] and not g.slow[level] then g.strikes[level] = nil; guardSave() end
    end
end

-- Cheap checks before a search (se's S038 point 1): no engine search for a goal it can't walk to. A goal column with
-- no AI-grid tile, or none within PATH_FLOOR_SLACK_M of the goal's height (the void over a chasm, a guessed y in the
-- air), or a Chasm tile, is reachable=false at once; a start with no floor near the character's feet (mid-jump, a
-- fall) or a goal farther than PATH_STRAIGHT_MAX_M is unknown (nil).
N.PATH_STRAIGHT_MAX_M = 450
N.PATH_FLOOR_SLACK_M = 5.0
N.PATH_START_SLACK_M = 3.0
local function nearFloor(x, z, y, slack)
    local ok, hs = N.floorHeights(x, z)
    if not ok or type(hs) ~= "table" then return nil end -- can't tell: search
    for _, h in ipairs(hs) do if type(h) == "number" and math.abs(h - y) <= slack then return true end end
    return false, #hs
end
local function precheck(from, target, isEntity)
    local d = U.dist(from, target)
    if d > N.PATH_STRAIGHT_MAX_M then
        return { reachable = nil, estimate = true, too_far = true, length_m = round(d, 1),
            reason = "too far to search a walk (straight " .. math.floor(d) .. " m)", ms = 0 }
    end
    if nearFloor(from[1], from[3], from[2], N.PATH_START_SLACK_M) == false then
        return { reachable = nil, midair = true, reason = "no floor under the character (mid-jump or falling)", ms = 0 }
    end
    if isEntity then return nil end
    local okF, n = nearFloor(target[1], target[3], target[2], N.PATH_FLOOR_SLACK_M)
    if okF == false then
        return { reachable = false, no_floor = true, straight_m = round(d, 1), ms = 0,
            reason = n == 0 and "no walkable ground at the target" or "no floor near that height at the target" }
    end
    if N.surfaceAt and N.surfaceAt(target) == "Chasm" then
        return { reachable = false, chasm = true, straight_m = round(d, 1), ms = 0, reason = "the target is in a Chasm" }
    end
    return nil
end

-- A goal in the air over ground the party knows (live9 acceptance: route to a point 60 m above the party said "not
-- seen or walked yet"): no floor near the goal's height while a floor of that column is known ground (N.known), so a
-- player sees there is nothing to stand on. The reason, else nil; a column with no known floor stays unknown.
function N.airGoal(ctx, goal)
    if nearFloor(goal[1], goal[3], goal[2], N.PATH_FLOOR_SLACK_M) ~= false then return nil end
    local _, hs = N.floorHeights(goal[1], goal[3])
    local best
    for _, h in ipairs(hs) do
        if type(h) == "number" and (not best or math.abs(h - goal[2]) < math.abs(best - goal[2]))
            and N.known(ctx, { goal[1], h, goal[3] }) then best = h end
    end
    if not best then return nil end
    return string.format("no floor near that height at the target: the ground there is %.1f m %s",
        math.abs(goal[2] - best), best < goal[2] and "below" or "above")
end

-- Async results (N.waysWarm): N.path answers from here before searching. key = N.path's own key.
N.ASYNC_TTL_MS = 30000
N.ASYNC_MAX = 256
N.asyncPaths = N.asyncPaths or {}
N.asyncPathsN = N.asyncPathsN or 0
-- The start's height is in the key (naut2 runs 2/3, Nautiloid helm): a ladder's foot and top share x/z to 0.1 m, so a
-- warm-up answer from the deck below was the answer for 30 s after climbing it.
local function pathKey(charUuid, tposOrUuid, from)
    return charUuid .. "|" .. (type(tposOrUuid) == "string" and tposOrUuid
        or string.format("%.1f:%.1f:%.1f", tposOrUuid[1], tposOrUuid[2], tposOrUuid[3]))
        .. "|" .. string.format("%.1f:%.1f:%.1f", from[1], from[2], from[3])
end

-- Bounds over every caller at once (S038 20:48: the server froze in a FindPath on the Shadowfell and the RSS went
-- 5 -> 13 GB; S025: 1.6-2.3 s no-path searches over a big level). Each caller has its own budget (jumpPlan, ways,
-- closestReach), but a request chains several (route = closestReach + connectors + ways + jumpGap, walk_toward =
-- reachToward + otherDirections x7): one server frame (one RPC, or one Tick) runs at most PATH_FRAME_MAX searches
-- and PATH_FRAME_BUDGET_MS of them; past that a search answers reachable=nil (budget=true, unknown, not cached),
-- which every caller already reads as "don't know". A search slower than PATH_SLOW_MEMO_MS is remembered per level,
-- start (1 m) and target (0.5 m) for PATH_SLOW_MEMO_TTL_MS and answered from memory, so no frame repeats a search
-- that froze the game once (the cross-frame path cache only lives one frame).
-- PATH_HORIZON (nil = the engine's default SearchHorizon 32000) caps the engine's own search when set (UNVERIFIED
-- units: set it live with luadebug `BG3AI.Nav.PATH_HORIZON = 8000` and compare a long route's result first).
N.PATH_FRAME_MAX = 150
N.PATH_FRAME_BUDGET_MS = 1500
N.PATH_SLOW_MEMO_MS = 250
N.PATH_SLOW_MEMO_TTL_MS = 60000
N.PATH_SLOW_MEMO_MAX = 64
N.PATH_HORIZON = N.PATH_HORIZON or nil
N.pathFrame = N.pathFrame or { frame = -1, n = 0, ms = 0, skipped = 0 }
N.slowPaths = N.slowPaths or {}
N.slowPathsN = N.slowPathsN or 0
local function slowKey(from, target)
    return string.format("%s|%d:%d:%d|%d:%d:%d", tostring(currentLevel()), math.floor(from[1] + 0.5),
        math.floor(from[2] + 0.5), math.floor(from[3] + 0.5), math.floor(target[1] * 2 + 0.5),
        math.floor(target[2] * 2 + 0.5), math.floor(target[3] * 2 + 0.5))
end
local function gatedPath(e, from, target, endTol)
    local pf = N.pathFrame
    if pf.frame ~= N.frame then
        if pf.skipped > 0 then
            Ext.Utils.PrintWarning(string.format("[BG3AI] path budget: %d searches skipped in one frame (%d run, %d ms)",
                pf.skipped, pf.n, math.floor(pf.ms)))
        end
        pf.frame, pf.n, pf.ms, pf.skipped = N.frame, 0, 0, 0
    end
    local key = slowKey(from, target)
    local s = N.slowPaths[key]
    local now = Ext.Utils.MonotonicTime()
    if s then
        if now - s.t <= N.PATH_SLOW_MEMO_TTL_MS then
            local r = {}
            for k, v in pairs(s.r) do r[k] = v end
            r.memo, r.ms = true, 0
            return r
        end
        N.slowPaths[key] = nil
    end
    if pf.n >= N.PATH_FRAME_MAX or pf.ms >= N.PATH_FRAME_BUDGET_MS then
        pf.skipped = pf.skipped + 1
        return { reachable = nil, budget = true, unfinished = true, reason = "path search budget for this frame spent",
            straight_m = round(U.dist(from, target), 1), ms = 0 }
    end
    local r = rawPath(e, from, target, endTol)
    pf.n = pf.n + 1
    pf.ms = pf.ms + (r.ms or 0)
    if (r.ms or 0) > N.PATH_LEVEL_KILL_MS then N.slowSearch(r.ms) end
    if (r.ms or 0) > N.PATH_SLOW_MEMO_MS and r.reachable ~= nil and not (r.reachable == false and N.geoSettling()) then
        if N.slowPathsN >= N.PATH_SLOW_MEMO_MAX then N.slowPaths, N.slowPathsN = {}, 0 end
        N.slowPaths[key] = { t = now, r = r }
        N.slowPathsN = N.slowPathsN + 1
    end
    return r
end

-- path(char, target): target is a pos {x,y,z} or an entity uuid. For an entity
-- the path may end anywhere within APPROACH_M of it (characters and doors
-- occupy their own tiles). Returns {reachable=true|false|nil, length_m?,
-- needs_jump?, nodes?, approach_pos?, straight_m?, error_cause?, estimate?, ms}.
N.APPROACH_M = 1.5
local RING = { { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 }, { 0.7071, 0.7071 }, { -0.7071, 0.7071 }, { 0.7071, -0.7071 }, { -0.7071, -0.7071 } }
local DIR_VEC = {
    E = { 1, 0 }, NE = { 0.7071, 0.7071 }, N = { 0, 1 }, NW = { -0.7071, 0.7071 },
    W = { -1, 0 }, SW = { -0.7071, -0.7071 }, S = { 0, -1 }, SE = { 0.7071, -0.7071 },
}

-- Floor height at (x, z) closest to y (multi-floor aware); nil if no floor.
local function floorAt(x, z, y)
    local ok, hs = N.floorHeights(x, z)
    if not ok or type(hs) ~= "table" then return nil end
    local best
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - y) <= 4.0 and (not best or math.abs(h - y) < math.abs(best - y)) then best = h end
    end
    return best
end

-- deadline (MonotonicTime ms, optional): the approach ring stops there; the
-- result is then r.unfinished (reachable unknown) and not cached.
-- noSearch (the ways warm-up): answer only from the frame cache, the prechecks and a fresh async answer; never an
-- engine search on the server thread -> nil, "no async answer" otherwise.
function N.path(charUuid, target, deadline, noSearch)
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
    local key = pathKey(charUuid, isEntity and target or tpos, from)
    local c = cacheGet(key)
    if c then return c end
    local r = precheck(from, tpos, isEntity)
    if r then
        cachePut(key, r)
        return r
    end
    local a = N.asyncPaths[key]
    if a and Ext.Utils.MonotonicTime() - a.t <= N.ASYNC_TTL_MS then
        r = {}
        for k, v in pairs(a.r) do r[k] = v end
        r.async = true
    elseif noSearch then
        return nil, "no async answer"
    else
        r = gatedPath(e, from, tpos, not isEntity and N.PATH_END_TOL_M or nil)
    end
    if isEntity and r.reachable == false and not noSearch then
        local t0 = Ext.Utils.MonotonicTime()
        for _, d in ipairs(RING) do
            if deadline and Ext.Utils.MonotonicTime() > deadline then
                r = { reachable = nil, unfinished = true, ms = r.ms }
                break
            end
            local x, z = tpos[1] + d[1] * N.APPROACH_M, tpos[3] + d[2] * N.APPROACH_M
            local fy = floorAt and floorAt(x, z, tpos[2]) or tpos[2]
            if fy then
                local r2 = gatedPath(e, from, { x, fy, z })
                if r2.reachable then
                    r = r2
                    r.approach_pos = U.vec({ x, fy, z }, 1)
                    break
                end
            end
        end
        r.ms = (r.ms or 0) + (Ext.Utils.MonotonicTime() - t0)
    end
    -- A walk never takes a fall that hurts (S035: "walks must never path over a lethal drop"): an engine route
    -- whose link drops more than WALK_DROP_MAX_M is marked; reachToward/directLeg skip it, Guard refuses it
    -- (jump_to is the deliberate way down, it checks the landing).
    if r.reachable and r.points and not r.drop then
        local pts = r.points
        for i = 2, #pts do
            local a, b = pts[i - 1], pts[i]
            local dy = a[2] - b[2]
            if dy > N.WALK_DROP_MAX_M and math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2) <= 2 * dy then
                r.drop = { pos = U.vec(a, 1), m = round(dy, 1) }
                break
            end
        end
    end
    if not r.unfinished then cachePut(key, r) end
    return r
end

-- File-local helpers the other parts use.
N.inner = { store = store, mirror = mirror, levelStore = levelStore, currentLevel = currentLevel, DIRS = DIRS,
    DIR_VEC = DIR_VEC, floorAt = floorAt, readResult = readResult, rawPath = rawPath, gatedPath = gatedPath,
    pathKey = pathKey,
    helperBox = helperBox, inBox = inBox }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads N and the
-- helpers in N.inner.
for _, part in ipairs({ "Server/NavigationReach.lua", "Server/NavigationTerrain.lua", "Server/NavigationRoute.lua",
    "Server/NavigationOps.lua", "Server/NavigationDoors.lua" }) do
    Ext.Require(part)
end

return N
