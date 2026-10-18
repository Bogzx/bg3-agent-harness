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
    local host = osi(Osi.GetHostCharacter)
    local u = host and V.uuid(host)
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
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
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
local floorAt -- defined below

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
    -- S035 19:38: walk_toward W in the Faith-Leap room ran Claude onto the
    -- trial's invisible-floor layer (the engine mesh is one flat floor over
    -- the whole chasm; only the hidden platforms hold) and Claude fell to
    -- death. Marked here (hidden_floor, winds = the engine's line bends off
    -- the straight one); the automatic walks (reachToward, directLeg) never
    -- take it, Guard refuses a winding one; a straight move_to the agent
    -- picked from the mosaic, or a short step, is the player's own risk.
    if r.reachable and r.points and N.hiddenFloor and (r.length_m or 0) > N.HIDDEN_STEP_M then
        local h = N.hiddenFloor(r.points)
        if h then
            r.hidden_floor = h
            if r.length_m > U.dist(from, r.points[#r.points]) + 1.0 then h.winds = true end
        end
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
    local r0 = N.path(char, target)
    if not r0 then error("no path", 0) end
    local r = {}
    for k, v in pairs(r0) do if k ~= "points" and k ~= "portals" then r[k] = v end end
    if r0.points then r.hazard = N.routeHazard(r0.points) end
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
    local hidden -- a sample refused for crossing the invisible-floor layer (N.hiddenFloor)
    local cut = false
    local function good(i)
        if memo[i] ~= nil then return memo[i] end
        if deadline and Ext.Utils.MonotonicTime() > deadline then cut = true; return false end
        local c, dd = pts[i], pts[i].dd
        tried = tried + 1
        local ok = false
        local r = N.path(charUuid, c.pos)
        if r and r.hidden_floor and (not hidden or c.dd < hidden.dd) then hidden = { pos = r.hidden_floor.pos, at_m = r.hidden_floor.at_m, dd = c.dd } end
        if r and r.reachable and not r.hidden_floor and not r.drop and not r.partial and r.length_m <= math.max(N.DETOUR * dd, dd + 2.0) then
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
            if dd >= 2.0 then
                if memo[i] == nil then tried = tried + 1 end
                n = n + 1
                local r = N.path(charUuid, c.pos)
                if r and r.reachable and not r.partial and not r.needs_jump and not r.hidden_floor and not r.drop
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
    if hidden then hidden.dd = nil end
    if not best then
        if cut then return { reachable = nil, unfinished = true, samples = tried, hazard = firstHazard, hidden_floor = hidden } end
        if avoided == 0 then return { reachable = false, samples = tried, hidden_floor = hidden } end
        return { reachable = false, samples = tried, hazard = firstHazard, hidden_floor = hidden }
    end
    local out = { pos = U.vec(best.pos, 1), path_m = best.path_m, straight_m = best.straight_m,
        needs_jump = best.needs_jump, reachable = true, samples = tried, dir = N.direction(from, best.pos),
        searched = true, winding = winding or nil, unfinished = cut or nil }
    if avoided > 0 then out.avoided = avoided; out.hazard_beyond = firstHazard end
    if hidden then out.hidden_beyond = hidden end
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
-- closer, and lie >= ALT_EXCLUDE_M from `bad` (the leg end that failed).
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
    local goal = { toward[1], floorAt(toward[1], toward[3], toward[2]) or toward[2], toward[3] }
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
            if U.dist(from, pts[i]) <= N.DIRECT_LEG_M then idx, walked = i, acc end
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
    if walk > N.HIDDEN_STEP_M and N.hiddenFloor(legPts) then return nil, "hidden floor" end
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
        out.exposure = risky
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
-- walkable; its score is progress - CROSS_COST x metres of harmful surface
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
    local function try(x, z)
        local fy = floorAt(x, z, from[2])
        if not fy then return end
        local p = { x, fy, z }
        local progress = d0 - U.dist(p, toward)
        if progress < 1.0 or N.surfaceNear(p, 0.5) then return end
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
    local okh, hs = pcall(Ext.Level.GetHeightsAt, pos[1], pos[3])
    local fl = {}
    if okh and type(hs) == "table" then
        for _, h in ipairs(hs) do if type(h) == "number" then fl[#fl + 1] = round(h, 1) end end
    end
    local why = bad(pos)
    if not why and #fl == 0 and okh then
        -- S038 20:48 / S039 21:53: a column with no AI-grid floor at all (the Shadowfell's void) was "safe", and the
        -- jump cast there froze the engine (13 GB, OOM) both times. Never a landing.
        why, out.void = "no_floor", true
    elseif not why and #fl > 0 and not floorAt(pos[1], pos[3], pos[2]) then
        why = "no_floor"
        table.sort(fl, function(a, b2) return math.abs(a - pos[2]) < math.abs(b2 - pos[2]) end)
        out.floors = fl
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
        if r < 1.5 or r > reach + 0.05 or bad(p) then return end
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
        if bad(p) then return end
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
                        if not bad(sp) then stands[#stands + 1] = { p = sp, d = hdist(from, sp), jump = rad } end
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
-- room. Probes the nearest cell, then cells spread over the patch. Without a
-- usable ray API (nil) the patch is kept: walks need it, and the game draws
-- surfaces on the path preview anyway.
local function patchSeen(ctx, members, near)
    if not ctx then return true end
    local k = #members
    local picks = { near }
    local nS = math.min(N.PATCH_SAMPLES - 1, k)
    for s = 1, nS do picks[#picks + 1] = members[math.max(1, math.floor(s * k / nS))].p end
    for _, p in ipairs(picks) do
        local v = V.pointSeen(ctx, p)
        if v ~= false then return true end
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
-- climbs/jumps/drops on it, burning stretches, the floor levels at both ends;
-- with no path: the walkable spot nearest the goal and the ladders/doors in
-- view, ranked by how much they help.
------------------------------------------------------------------------

N.ROUTE_LEG_M = 10       -- a leg ends after this many metres...
N.ROUTE_TURN_DEG = 35    -- ...or at a turn this sharp
N.ROUTE_MAX_LEGS = 8
N.LINK_DY_M = 1.0        -- a step this high and steeper than 1:1 is a climb/drop
N.LEVEL_RANGE_M = 15     -- floors reported within +-15 m
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

-- Floor heights at (x, z) relative to y, ascending and de-duplicated: {-6.1,
-- 0, 9.2} = a floor 6 m below, this one, a ledge 9 m up. nil if unknown.
function N.levelsAt(x, z, y)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
    if not ok or type(hs) ~= "table" then return nil end
    local out = {}
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - y) <= N.LEVEL_RANGE_M then
            local d = round(h - y, 1)
            local dup = false
            for _, o in ipairs(out) do if math.abs(o - d) < 0.5 then dup = true end end
            if not dup then out[#out + 1] = d end
        end
    end
    table.sort(out)
    return out
end

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
        local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
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
        if not seen[key] then
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
                        local okh, hs = pcall(Ext.Level.GetHeightsAt, x, z)
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
-- clear floors LOWER_MIN_M..LOWER_MAX_M below; for each lower floor the nearest ledge cell. Best per compass
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
                local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
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
        -- per direction: toward the goal first, then the narrowest gap
        if not cur or (toward and not cur.toward) or (toward == cur.toward and ad < cur.gap) then
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
-- levels_here, levels_goal, reachable, length_m?, needs_jump?, legs?, links?,
-- exposure?, straight_hazard?, closest?{..., legs, links}, connectors?,
-- standing_in?, far? (beyond PATH_MAX_M: probed, no detail), too_far? (beyond
-- ROUTE_FAR_M: not probed), waypoints? and visited_near? (far or no path)}.
-- check_all FAIL 03:50 (Lower City → the Bhaal Temple far below, no walk): nav.route 258 ms = closestReach (rings +
-- the walk toward it) ~90 + lowerGround ~115 + connectors ~55. Route gives closestReach ROUTE_CLOSEST_BUDGET_MS and
-- skips the lower-ground scan once ROUTE_BUDGET_MS is spent (lower_skipped).
N.ROUTE_CLOSEST_BUDGET_MS = 80
N.ROUTE_BUDGET_MS = 110          -- the whole route (check_route: < 150 ms in the mod)
N.ROUTE_CONNECT_RESERVE_MS = 25  -- kept for the connectors' path probes after the lower-ground scan
N.ROUTE_JUMPX_BUDGET_MS = 40     -- NAV-5: jumpAcross's grid scan + its (expected-failing) landing searches
function N.routeAhead(ctx, charUuid, goal, withConnectors)
    local t0 = Ext.Utils.MonotonicTime()
    local e = V.entity(charUuid)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    local out = { from = U.vec(from, 1), goal = U.vec(goal, 1), straight_m = round(U.dist(from, goal), 1),
        dir = N.direction(from, goal), dy = round(goal[2] - from[2], 1) }
    out.levels_here = N.levelsAt(from[1], from[3], from[2])
    if U.dist(from, goal) > N.ROUTE_FAR_M then
        out.too_far = true
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
        -- Floors at the goal only within path range (what panning the camera
        -- there shows), never for a far, unexplored spot.
        out.levels_goal = N.levelsAt(goal[1], goal[3], from[2])
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
            out.exposure = N.routeExposure(r.points)
        elseif r and r.reachable == false then
            out.reachable = false
            out.straight_hazard = N.routeExposure({ from, goal })
            local okj, jx = pcall(N.jumpAcross, charUuid, from, goal, t0 + N.ROUTE_JUMPX_BUDGET_MS)
            if okj and jx then out.jump = jx end
            local c = N.closestReach(charUuid, goal, N.ROUTE_CLOSEST_BUDGET_MS)
            if c then
                local rc = N.path(charUuid, c.pos)
                if rc and rc.reachable then
                    c.legs, c.links = N.routeLegs(rc.points, rc.portals)
                    c.exposure = N.routeExposure(rc.points)
                end
                out.closest = c
            else
                local okw, w = pcall(N.wedged, charUuid, nil, goal)
                if okw and w then out.wedged = w end
            end
        end
    end
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
    if withConnectors or not out.reachable then
        out.connectors = N.connectors(ctx, charUuid, goal, N.CONNECT_M, N.CONNECT_MAX,
            math.max(10, t0 + N.ROUTE_BUDGET_MS - Ext.Utils.MonotonicTime()))
    end
    if out.far or out.too_far or out.reachable == false then
        local wps = N.unlockedWaypointsHere(goal, from)
        while #wps > N.WAYPOINTS_SHOWN do table.remove(wps) end
        if #wps > 0 then out.waypoints = wps end
        out.visited_near = N.nearestVisited(currentLevel(), goal)
    end
    local under = N.harmfulUnder(charUuid)
    if under then out.standing_in = under end
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
    -- true (open), false, or nil (noSearch: no answer yet)
    local function open(v, dd)
        local x, z = round(from[1] + v[1] * dd, 1), round(from[3] + v[2] * dd, 1)
        local fy = floorAt(x, z, from[2])
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
            if ok == false then
                m = near
                ok, dy = open(v, near)
                if ok == false then m = 0 end
            end
            if ok == nil then goto nextDir end
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
        if w then out[#out + 1] = { dir = w.dir, open_m = w.open_m, dy = w.dy, jump = w.jump } end
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
        local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
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
function N.exitName(name)
    local l = type(name) == "string" and name:lower() or ""
    for _, w in ipairs(N.EXIT_NAME) do
        if l:find("%f[%a]" .. w) then return true end
    end
    return false
end

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
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tp))
        if not visible then error("not visible", 0) end
        toward = tp
    end
    local maxM = math.min(tonumber(args.max_m) or 30, N.PATH_MAX_M)
    local step = math.max(tonumber(args.step) or 2, 1)
    local t0 = Ext.Utils.MonotonicTime()
    -- direct=true (travel_to): the engine's own route in one long leg when the
    -- whole way is plain walking; else the sampled legs below.
    local riskyDirect = nil
    if args.direct and toward and not args.alt then
        local d, why, risky = N.directLeg(char, toward, args.avoid ~= false)
        riskyDirect = risky
        if d then
            local under = N.harmfulUnder(char)
            if under then d.standing_in = under end
            d.ms = Ext.Utils.MonotonicTime() - t0
            return d
        end
        args.direct_why = why
    end
    local r, err = N.reachToward(char, args.dir, toward, maxM, step, args.avoid ~= false, args.loose == true)
    if not r then error(err, 0) end
    -- alts=true (walk_toward by direction): on failure, which directions do work
    if args.alts and args.dir and not r.reachable then
        r.others = N.otherDirections(char, tostring(args.dir):upper(), math.min(maxM, 20), args.avoid ~= false)
    end
    -- avoid="least" (travel_to): when no clear walk gets >= 1 m closer, look
    -- around (detours) and, failing that, take the least-exposure crossing.
    if args.avoid == "least" and toward then
        local e = V.entity(char)
        local from = e and V.pos(e)
        local gain = (r.reachable and r.pos and from) and (U.dist(from, toward) - U.dist(r.pos, toward)) or 0
        if gain < 1.0 or (r.hazard_beyond and gain < 0.5 * maxM) then
            local c = N.crossing(char, toward, maxM)
            if c and c.reachable and (gain < 1.0 or c.score > gain + 0.5) then
                c.straight = { reachable = r.reachable, hazard = r.hazard or r.hazard_beyond }
                r = c
            elseif c then
                r.cross_samples = c.samples
            end
        end
        -- Still nothing that gets on: the engine's own route across the surface
        -- (the bridge weighs the exposure: crossing_ok by policy and HP).
        if riskyDirect then
            local g2 = (r.reachable and r.pos and from) and (U.dist(from, toward) - U.dist(r.pos, toward)) or 0
            if g2 < 1.0 then
                riskyDirect.straight = { reachable = r.reachable, hazard = r.hazard or r.hazard_beyond }
                r = riskyDirect
            end
        end
    end
    -- travel_to: verify the exact leg end we hand out; when it fails, or the
    -- bridge asks for another leg after a failed walk (alt=true, exclude=that
    -- leg's end), take a verified alternative (N.altLeg).
    if toward and not (r.direct and r.verified) and (args.alt or (r.reachable and r.pos)) then
        local fine = nil
        -- reachToward searched this exact (0.1 m) point this frame: no second search
        if not args.alt then fine = r.searched or N.verifyPoint(char, r.pos) end
        if fine == true then
            r.verified = true
        elseif args.alt or fine == false then
            local bad = type(args.exclude) == "table" and args.exclude or (not args.alt and r.pos) or nil
            local a = N.altLeg(char, toward, maxM, bad, args.avoid ~= false)
            if a then
                if fine == false then a.replaced = r.pos end
                r = a
            elseif fine == false then
                r = { reachable = false, samples = r.samples, unverified = r.pos }
            else
                r.alt_failed = true
                if r.reachable then r = { reachable = false, samples = r.samples, alt_failed = true } end
            end
        end
    end
    local under = N.harmfulUnder(char)
    if under then r.standing_in = under end
    r.searched = nil
    r.direct_why = args.direct_why
    if r.reachable == false then
        local ce = V.entity(char)
        r.opened = N.geoNote(ce and V.pos(ce))
    end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.approach {char?, target|pos, within?=1.5, budget_m?, avoid?=true}: where
-- to walk along the real route to get within `within` m (see N.approach).
-- budget_m defaults to the movement left (in combat) minus 0.2.
RPC.register("nav.approach", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local target, tpos = nil, type(args.pos) == "table" and args.pos or nil
    if args.target then
        local te = V.entity(args.target)
        tpos = te and V.pos(te)
        if not tpos then error("unknown target", 0) end
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tpos)
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tpos))
        if not visible then error("not visible", 0) end
        target = args.target
    end
    if type(tpos) ~= "table" or #tpos < 3 then error("need pos [x,y,z] or target", 0) end
    local ce = V.entity(char)
    local cp = ce and V.pos(ce)
    if cp and U.dist(cp, tpos) > N.PATH_MAX_M then
        return { reachable = false, reason = "too far", straight_m = round(U.dist(cp, tpos), 1) }
    end
    local mv = movementLeft(char)
    local budget = tonumber(args.budget_m)
    if mv and Osi.IsInCombat and truthy(osi(Osi.IsInCombat, char)) then
        budget = math.min(budget or math.huge, math.max(0, mv - 0.2))
    end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.approach(char, target or tpos, tpos, math.max(0, tonumber(args.within) or N.APPROACH_M),
        budget, args.avoid ~= false)
    if not r then error(err, 0) end
    if mv then r.movement_left = round(mv, 1) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

N.LOS_MARGIN_M = 0.25
N.LOS_MARGIN_WIDE_M = 0.5
N.LOS_DISAGREE_MIN_M = 1.5
-- nav.los_spot {char?, target, range, budget_m?, avoid?=true}: where to walk
-- to have the target in sight and within range (see N.losSpot). Also says
-- whether the engine sees it from where the character stands now
-- (los_now). budget_m defaults to the movement left (in combat) minus 0.2.
RPC.register("nav.los_spot", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local te = args.target and V.entity(args.target)
    local tpos = te and V.pos(te)
    if not tpos then error("unknown target", 0) end
    local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tpos)
        or (U.comp(te, "ServerItem") and V.itemVisible(ctx, args.target, te, tpos))
    if not visible then error("not visible", 0) end
    local range = tonumber(args.range)
    if not range or range <= 0 then error("need range (m)", 0) end
    local mv = movementLeft(char)
    local budget = tonumber(args.budget_m)
    if mv and Osi.IsInCombat and truthy(osi(Osi.IsInCombat, char)) then
        budget = math.min(budget or math.huge, math.max(0, mv - 0.2))
    end
    if budget == math.huge then budget = nil end
    local t0 = Ext.Utils.MonotonicTime()
    -- Spots with a sideways ray margin; a wider one, and no spot within
    -- LOS_DISAGREE_MIN_M of here, when the rays already see the target from
    -- where the engine says there is no line of sight (the model is off here).
    local losNow = truthy(osi(Osi.HasLineOfSight, char, args.target))
    local ce = V.entity(char)
    local here = ce and V.pos(ce)
    local margin, minStep = N.LOS_MARGIN_M, nil
    if not losNow and here and V.losFrom(ctx, args.target, te, tpos, here) == true then
        margin, minStep = N.LOS_MARGIN_WIDE_M, N.LOS_DISAGREE_MIN_M
    end
    local r, err = N.losSpot(char, args.target, tpos, range, budget, args.avoid ~= false,
        function(feet)
            if minStep and here and U.dist(feet, here) < minStep then return false end
            return V.losFrom(ctx, args.target, te, tpos, feet, margin)
        end)
    if not r then error(err, 0) end
    r.los_now = losNow
    if minStep then r.model_off_here = true end
    if mv then r.movement_left = round(mv, 1) end
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
    local toward = type(args.pos) == "table" and #args.pos >= 3 and args.pos or nil
    local r, err = N.safeSpot(char, math.min(tonumber(args.max_m) or 8, 12), toward)
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.landing {char?, pos}: is a jump_to landing clear? {safe, dist_m,
-- reliable_m, max_m?, surface?|blocked?, short_pos?, suggest?, suggest_off_m?,
-- suggest_dist_m?} (see N.landing).
RPC.register("nav.landing", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    if type(args.pos) ~= "table" or #args.pos < 3 then error("need pos [x,y,z]", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    -- Breadcrumb (S038: the server hung in here and the log said nothing of where): one line per jump_to check,
    -- and its time once it took long.
    local p = args.pos
    Ext.Utils.Print(string.format("[BG3AI] nav.landing (%s,%s,%s)", tostring(p[1]), tostring(p[2]), tostring(p[3])))
    local r, err = N.landing(char, args.pos)
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    if r.ms > 100 then Ext.Utils.Print(string.format("[BG3AI] nav.landing took %.0f ms", r.ms)) end
    return r
end)

-- nav.level_guard {clear?: true|level, kill?: level, why?}: the levels the kill switch turned off ({slow = {level =
-- {why, ms}}}, warm = async warm-up counters); clear=true clears all, clear=<level> one; kill=<level> turns one off
-- by hand (a level known to freeze the engine's searches, e.g. SCL_Main_A for tests on Act 2 saves).
RPC.register("nav.level_guard", function(args)
    local g = N.guardState()
    if type(args.kill) == "string" then N.killLevel(args.kill, args.why or "turned off by hand") end
    if args.clear == true then g.slow, g.strikes, N.slowHits = {}, {}, {}
    elseif type(args.clear) == "string" then
        g.slow[args.clear] = nil
        if g.strikes then g.strikes[args.clear] = nil end
        N.slowHits[args.clear] = nil
    end
    if args.clear ~= nil then pcall(Ext.IO.SaveFile, N.guardFile(), Ext.Json.Stringify(g, { Beautify = false })) end
    local wa = N.warmAsync
    return { slow = g.slow, level = currentLevel(), here = N.levelSlow(), file = N.guardFile(),
        slow_hits = N.slowHits[currentLevel() or ""], ttl_s = N.KILL_TTL_S,
        warm = { ok = wa.ok, timeouts = wa.timeouts, broken = wa.broken } }
end)

-- nav.hazards {char?, radius?, limit?}: harmful patches the party sees nearby.
RPC.register("nav.hazards", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local list, err = N.hazardPatches(char, math.min(tonumber(args.radius) or N.HAZARD_SCAN_M, 20))
    if not list then error(err, 0) end
    local limit = tonumber(args.limit) or 6
    local out = { hazards = {} }
    for i, h in ipairs(list) do
        if i > limit then out.more = #list - limit; break end
        out.hazards[i] = h
    end
    out.ms = Ext.Utils.MonotonicTime() - t0
    return out
end)

------------------------------------------------------------------------
-- Local layout: the walls, openings and floor around a character as a text
-- grid, what the overhead camera shows (S030 Gauntlet of Shar trials: 11 min
-- and 16 screenshots in the Soft-Step maze and the Faith-Leap room).
-- Cells from the AI grid tiles: '#' wall (WalkBlock), '.' floor at our level,
-- '^'/'v' a floor > 1.2 m above/below, ':' inside a darkness cloud, '~' a
-- harmful surface, ' ' no floor a player can see (drop, chasm).
-- Fairness: the Faith-Leap safe path is a walkway layer of invisible-platform
-- helpers (V.helperItem); that layer is never drawn as floor. What a player
-- does see of it is the floor mosaic by the bowl, which depicts the safe
-- lines ([wiki] "the full map of the safe lines is depicted on the mosaic"):
-- N.mosaic draws that picture, only when the character is in that room.
------------------------------------------------------------------------

N.LAYOUT_R = 12
N.LAYOUT_MAX_R = 20
N.HELPER_LAYER_M = 0.6   -- a floor this close to a helper's top is the hidden walkway
N.HELPER_MARGIN_M = 3.0  -- around the helpers' bounding box
N.MOSAIC_NEAR_M = 20     -- the mosaic is shown from this close to the helpers' box
N.MOSAIC_LINK_M = 2.3    -- helpers this close are one line of the picture
N.helperCache = N.helperCache or {}

-- Invisible-platform helper positions on this level (one scan per level).
function N.helpers()
    local lvl = currentLevel()
    local c = N.helperCache
    if c.level == lvl and c.list then return c.list end
    local list = {}
    local ok, all = pcall(function() return Ext.Entity.GetAllEntitiesWithComponent("ServerItem") end)
    for _, e in ipairs(ok and all or {}) do
        local u = e.Uuid and e.Uuid.EntityUuid
        if u and V.helperItem and V.helperItem(u) then
            local p = V.pos(e)
            if p then list[#list + 1] = p end
        end
    end
    c.level, c.list = lvl, list
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

-- First point of a route (points from rawPath) on the invisible-floor layer:
-- inside the helpers' box (+ HIDDEN_BOX_M, about half a platform) at the
-- walkway height. A visible statue/platform ~1 m above that layer is not it.
-- Returns {pos, at_m} or nil. Levels without helpers: one cached empty scan.
N.HIDDEN_STEP_M = 3.0   -- a walk this long or shorter may step onto it (one platform)
N.HIDDEN_BOX_M = 1.0
function N.hiddenFloor(points)
    if type(points) ~= "table" or #points < 2 or #N.helpers() == 0 then return nil end
    local box = helperBox(points[1], 40)
    if not box then return nil end
    local layer = box.y + 0.2
    local walked = 0
    for i = 2, #points do
        local a, b = points[i - 1], points[i]
        local seg = U.dist(a, b)
        local n = math.max(1, math.ceil(seg / 0.5))
        for k = 1, n do
            local f = k / n
            local x, y, z = a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f
            if inBox(box, x, z, N.HIDDEN_BOX_M) and math.abs(y - layer) <= N.HELPER_LAYER_M then
                return { pos = U.vec({ x, y, z }, 1), at_m = round(walked + seg * f, 1) }
            end
        end
        walked = walked + seg
    end
    return nil
end

local function tileFlags(t)
    local f = {}
    pcall(function() for _, v in ipairs(t.Flags or {}) do f[tostring(v)] = true end end)
    return f
end

-- A floor this far below with nothing nearer is a drop, drawn "_" (S032 17:32: the Gauntlet's broken staircase
-- drew blank like the chasm beside it while route said the floor lay 9.4 m below; the agent tried two jumps into
-- the chasm and parked Yurgir). Deeper than DROP_MAX_M stays blank (the Faith-Leap chasm bottom is ~11.7 m down).
N.DROP_MIN_M, N.DROP_MAX_M = 4.0, 10.0
-- Deeper still, "," = deep ground DEEP_MIN_M..DEEP_MAX_M below (S039, Nightsong's Prison: the islands of the way down
-- lie 20-30 m below the arrival ledge and drew blank, like the void). From 14 m, so the Faith-Leap chasm bottom
-- (~11.7 m) stays blank.
N.DEEP_MIN_M, N.DEEP_MAX_M = 14.0, 120.0
function N.layoutCell(x, z, y0, box)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
    if not ok or type(hs) ~= "table" then return " " end
    local best, drop, deep
    local helperHere = box and inBox(box, x, z, N.HELPER_MARGIN_M)
    for _, h in ipairs(hs) do
        if type(h) == "number" and not (helperHere and math.abs(h - (box.y + 0.2)) <= N.HELPER_LAYER_M) then
            if math.abs(h - y0) <= N.DROP_MIN_M then
                if not best or math.abs(h - y0) < math.abs(best - y0) then best = h end
            elseif y0 - h <= N.DROP_MAX_M and (not drop or h > drop) then
                drop = h
            elseif y0 - h >= N.DEEP_MIN_M and y0 - h <= N.DEEP_MAX_M and (not deep or h > deep) then
                deep = h
            end
        end
    end
    local low, mark = drop, "_"
    if not drop and deep then low, mark = deep, "," end
    if not best and low and not helperHere then
        local okt, t = pcall(Ext.Level.GetTileDebugInfo, { x, low, z })
        if not (okt and t) then return " " end
        local f = tileFlags(t)
        if f.WalkBlock then return " " end
        local g, cl = "", ""
        pcall(function() g, cl = tostring(t.GroundSurface), tostring(t.CloudSurface) end)
        if N.HARMFUL[g] or N.HARMFUL[cl] then return " " end
        return mark
    end
    if not best then return " " end
    local okt, t = pcall(Ext.Level.GetTileDebugInfo, { x, best, z })
    if okt and t then
        local f = tileFlags(t)
        if f.WalkBlock then return "#" end
        local g, cl = "", ""
        pcall(function() g, cl = tostring(t.GroundSurface), tostring(t.CloudSurface) end)
        if N.HARMFUL[g] or N.HARMFUL[cl] then return "~" end
        if cl == "DarknessCloud" or g == "Darkness" then return ":" end
    end
    local dy = best - y0
    if dy > 1.2 then return "^" end
    if dy < -1.2 then return "v" end
    return "."
end

-- The helpers' picture (the Faith-Leap mosaic): helpers linked when <= 2.3 m
-- apart, drawn at 1 m per character, N up. Returns {rows, x1, z2} or nil.
function N.mosaic(box)
    if not box or #box.pts < 4 then return nil end
    local x1, z2 = math.floor(box.x1), math.ceil(box.z2)
    local w, h = math.ceil(box.x2) - x1 + 1, z2 - math.floor(box.z1) + 1
    local grid = {}
    for r = 1, h do grid[r] = {} for c = 1, w do grid[r][c] = " " end end
    local function put(x, z)
        local c, r = math.floor(x - x1 + 0.5) + 1, math.floor(z2 - z + 0.5) + 1
        if grid[r] and grid[r][c] then grid[r][c] = "#" end
    end
    local pts = box.pts
    for i = 1, #pts do
        put(pts[i][1], pts[i][3])
        for j = i + 1, #pts do
            local d = U.dist({ pts[i][1], 0, pts[i][3] }, { pts[j][1], 0, pts[j][3] })
            if d <= N.MOSAIC_LINK_M then
                for k = 1, math.ceil(d / 0.5) do
                    local f = k / math.ceil(d / 0.5)
                    put(pts[i][1] + (pts[j][1] - pts[i][1]) * f, pts[i][3] + (pts[j][3] - pts[i][3]) * f)
                end
            end
        end
    end
    -- The statue platforms (visible floor ~1 m above the walkway layer, in
    -- the chasm): the boxes on the mosaic, seen across the chasm too.
    for r = 1, h do
        for c = 1, w do
            if grid[r][c] == " " then
                local ok, hs = pcall(Ext.Level.GetHeightsAt, x1 + c - 1, z2 - r + 1)
                for _, hh in ipairs(ok and type(hs) == "table" and hs or {}) do
                    if type(hh) == "number" and hh - box.y >= 0.6 and hh - box.y <= 1.6 then grid[r][c] = "O" break end
                end
            end
        end
    end
    local rows = {}
    for r = 1, h do rows[r] = (table.concat(grid[r]):gsub("%s+$", "")) end
    return { rows = rows, x1 = x1, z2 = z2, cell = 1, y = round(box.y + 0.2, 1) } -- y: the walkway height (its ledge's)
end

function N.layout(char, radius, cell)
    local e = V.entity(char)
    local from = e and V.pos(e)
    if not from then return nil, "no position" end
    radius = math.max(4, math.min(tonumber(radius) or N.LAYOUT_R, N.LAYOUT_MAX_R))
    cell = math.max(1, math.min(tonumber(cell) or 1.5, 3))
    local n = math.floor(radius / cell + 0.5)
    local box = helperBox(from, radius)
    local rows = {}
    for iz = n, -n, -1 do
        local row = {}
        for ix = -n, n do
            row[#row + 1] = N.layoutCell(from[1] + ix * cell, from[3] + iz * cell, from[2], box)
        end
        rows[#rows + 1] = table.concat(row)
    end
    local out = { rows = rows, cell = cell, n = n, center = U.vec(from, 1) }
    if box and inBox(box, from[1], from[3], N.MOSAIC_NEAR_M) and math.abs(from[2] - box.y) <= 6 then
        out.mosaic = N.mosaic(box)
    end
    return out
end

-- nav.layout {char?, radius?=12, cell?=1.5}: N.layout.
RPC.register("nav.layout", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.layout(char, args.radius, args.cell)
    if not r then error(err, 0) end
    r.ms = Ext.Utils.MonotonicTime() - t0
    return r
end)

-- nav.route {char?, pos | target, connectors?}: the route ahead (N.routeAhead).
-- A target must be visible; a pos is the agent's own goal (objective marker,
-- map marker, coordinates it read). Ladders/doors are listed when there is no
-- path, or always with connectors=true.
-- nav.wedged {char?, goal?}: N.wedged for walk_toward/travel_to failures (route carries it as r.wedged).
RPC.register("nav.wedged", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local goal = type(args.goal) == "table" and #args.goal == 3 and args.goal or nil
    return N.wedged(char, nil, goal) or { wedged = false }
end)

RPC.register("nav.route", function(args)
    local ctx = V.context()
    local char = args.char or activeChar(ctx)
    if not ctx.is_party[char] then error("not a party member", 0) end
    local goal = type(args.pos) == "table" and #args.pos >= 3 and args.pos or nil
    if not goal and args.target then
        local te = V.entity(args.target)
        local tp = te and V.pos(te)
        if not tp then error("unknown target", 0) end
        local visible = U.comp(te, "ServerCharacter") and V.characterVisible(ctx, args.target, te, tp)
            or (U.comp(te, "ServerItem") and (V.itemKnown or V.itemVisible)(ctx, args.target, te, tp))
        if not visible then error("not visible", 0) end
        local ce = V.entity(char)
        goal = U.comp(te, "ServerItem") and N.ladderTop(ce and V.pos(ce), args.target, te) or tp
    end
    if not goal then error("need pos [x,y,z] or target", 0) end
    local t0 = Ext.Utils.MonotonicTime()
    local r, err = N.routeAhead(ctx, char, goal, args.connectors == true)
    if not r then error(err, 0) end
    -- No path: the open directions too, as get_frontier gives them (S023 06:23:
    -- 8 guessed route/wiki/zoom calls across a chasm while the way on, a stone
    -- bridge, lay SW).
    if r.reachable == false and args.ways ~= false then
        local ok, ways = pcall(N.ways, char, 20, 8, 80)
        if ok and ways and #ways > 0 then r.ways = ways end
    end
    if r.reachable == false then
        local ce = V.entity(char)
        r.opened = N.geoNote(ce and V.pos(ce))
    end
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
            if truthy(osi(Osi.IsInCombat, u)) then
                out.in_combat = true
                -- party_turn: a party member can act now (false: enemies' turns, the agent can only wait)
                local G = BG3AI.Guard
                if not out.party_turn and G and G.is_my_turn then
                    local ok, mine = pcall(G.is_my_turn, u)
                    out.party_turn = ok and mine == true
                end
            end
            if not out.in_dialogue and e then out.in_dialogue = V.inDialog(u, e) end
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
