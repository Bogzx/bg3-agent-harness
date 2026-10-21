-- Visibility, part 2 of 2 (Visibility.lua loads the parts in order; split for the 1000-line cap):
-- large-object perception, the status filter, the item index, memory and the dev check.
local U = BG3AI.Util
local V = BG3AI.Visibility

local truthy = U.truthy

------------------------------------------------------------------------
-- Large-object perception (doors, ladders, big interactables).
--
-- HasLineOfSight to a door is often false because the door is its own
-- occluder, and CanSee is FOV-coned, so doors used to appear only up close.
-- For these objects only, a party member also perceives the object if a
-- physics ray from its eye reaches one of a few sample points on the object's
-- bounds unobstructed (or the first thing it hits is the object itself).
-- Characters use it only when big (V.bigPerceived); the others stay on CanSee/LOS above.
-- Called only after the trap/secret-door/scenery filters, so it can never
-- reveal an undiscovered standalone trap or a hidden secret door.
------------------------------------------------------------------------

V.EYE_H = 1.6               -- eye height above the feet (m)
V.LARGE_H = 2.0             -- bounds this tall, or this wide, count as "large"
V.LARGE_R = 1.0
V.LARGE_KINDS = { door = true, ladder = true }
V.RAY_TYPE = 0x05          -- PhysicsType Static | Dynamic
V.RAY_INCLUDE = 0xFFFFFFFF -- all PhysicsGroupFlags...
V.RAY_EXCLUDE = 0x820200   -- ...except Character | CanSeeThrough | CanSeeThrough2
V.RAY_CACHE_MS = 250
-- A verdict is reused this long while no observer has moved (positions to
-- 0.5 m): look, get_frontier and the delta after each action re-test the same
-- items while the party stands still (S025/S026 live: ~0.85 ms of rays per
-- unseen door/large item, 155 of them in a Shattered Sanctum frontier).
V.RAY_STILL_MS = 2000
-- Party members standing this close share an eye: their rays are not cast twice.
V.OBSERVER_SAME_M = 0.75
V.rayCache = V.rayCache or {}
V.rayStats = V.rayStats or { ray = 0, blocked = 0, out_of_range = 0, errors = 0, uncalibrated = 0 }

-- {height, radius} from the entity's AI bounds (Hit preferred), or nil.
-- Cached per entity uuid for BOUNDS_TTL_MS: reading AIBounds costs ~0.2-0.27 ms
-- live, and every item in a look/frontier scan reads it twice (large and
-- fixture checks): 286 items at camp = ~130 of a 140 ms frontier (2026-10-10).
V.BOUNDS_TTL_MS = 30000
V.boundsCache = V.boundsCache or {}
V.boundsCacheN = V.boundsCacheN or 0
local function readBounds(e)
    local b = U.comp(e, "Bound")
    if not b then return nil end
    local best
    pcall(function()
        for k, d in pairs(b.Bound.AIBounds) do
            local h = d.ActualHeight and d.ActualHeight > 0 and d.ActualHeight or d.Height
            local r = d.Radius
            if type(h) == "number" and type(r) == "number" then
                local hit = (k == "Hit" or k == 1)
                if not best or hit then best = { height = h, radius = r } end
            end
        end
    end)
    return best
end

function V.bounds(e)
    local u = e and V.entityUuid(e)
    if not u then return readBounds(e) end
    local now = Ext.Utils.MonotonicTime()
    local c = V.boundsCache[u]
    if c and now - c.t < V.BOUNDS_TTL_MS then return c.v or nil end
    local v = readBounds(e)
    if not c then
        V.boundsCacheN = V.boundsCacheN + 1
        if V.boundsCacheN > 5000 then V.boundsCache, V.boundsCacheN = {}, 1 end
    end
    V.boundsCache[u] = { t = now, v = v or false }
    return v
end

-- Sample points on the object as seen from `eye`: middle and upper body, the
-- two side edges (perpendicular to the view), and the middle pulled toward
-- the viewer (a door's near face).
local function samplePoints(eye, pos, h, r)
    local dx, dz = pos[1] - eye[1], pos[3] - eye[3]
    local len = math.sqrt(dx * dx + dz * dz)
    if len < 0.01 then return { { pos[1], pos[2] + h * 0.5, pos[3] } } end
    local ux, uz = dx / len, dz / len
    local side = math.min(r * 0.8, 1.5)
    local pull = math.min(r, 0.75)
    local mid = pos[2] + h * 0.5
    return {
        { pos[1], mid, pos[3] },
        { pos[1], pos[2] + h * 0.85, pos[3] },
        { pos[1] - uz * side, mid, pos[3] + ux * side },
        { pos[1] + uz * side, mid, pos[3] - ux * side },
        { pos[1] - ux * pull, mid, pos[3] - uz * pull },
    }
end

local function hitEntityUuid(hit)
    local ok, u = pcall(function()
        local ent = hit.Shape.PhysicsObject.Entity
        return ent and ent.Uuid and ent.Uuid.EntityUuid
    end)
    return ok and u or nil
end

local function raycast(a, b)
    return Ext.Level.RaycastClosest(a, b, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
end

-- Distance from `from` to the hit, from its position (or its Distance field).
local function hitDist(hit, from)
    local ok, p = pcall(function() return hit.Position end)
    if ok and (type(p) == "table" or type(p) == "userdata") then
        local okp, d = pcall(function() return U.dist(from, { p[1], p[2], p[3] }) end)
        if okp and type(d) == "number" then return d end
    end
    local okd, d = pcall(function() return hit.Distance end)
    return okd and type(d) == "number" and d or nil
end

-- Is the segment eye -> pt clear up to the object? Raises on API failure.
local function rayClear(uuid, eye, pt, slack)
    local hit = raycast(eye, pt)
    if not hit then return true end
    local d = hitDist(hit, eye)
    if d and d >= U.dist(eye, pt) - slack then return true end
    return hitEntityUuid(hit) == uuid
end

-- Fail-closed self-test: a ray from the eye straight down must hit the floor.
-- If it doesn't, the filter flags or the API are wrong and "no hit" would mean
-- nothing, so large-object perception stays off. Re-checked every 5 s.
V.calib = V.calib or { t = -1e9, ok = false }
function V.rayCalibrated(o)
    local now = Ext.Utils.MonotonicTime()
    if now - V.calib.t < 5000 then return V.calib.ok end
    local ok, hit = pcall(raycast, { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }, { o.pos[1], o.pos[2] - 3.0, o.pos[3] })
    V.calib = { t = now, ok = ok and hit ~= nil }
    if not V.calib.ok then V.rayStats.uncalibrated = V.rayStats.uncalibrated + 1 end
    return V.calib.ok
end

-- The observers' positions (0.5 m grid), once per context (= per frame).
local function observerFp(ctx)
    if ctx.rayFp then return ctx.rayFp end
    local parts = {}
    for _, o in ipairs(ctx.observers or {}) do
        if o.pos then
            parts[#parts + 1] = math.floor(o.pos[1] * 2 + 0.5) .. "," .. math.floor(o.pos[2] * 2 + 0.5) .. ","
                .. math.floor(o.pos[3] * 2 + 0.5)
        end
    end
    ctx.rayFp = table.concat(parts, ";")
    return ctx.rayFp
end

local function rayCacheGet(ctx, key, now)
    local c = V.rayCache[key]
    if not c then return nil end
    if now - c.t < V.RAY_CACHE_MS or (c.fp == observerFp(ctx) and now - c.t < V.RAY_STILL_MS) then return c end
    return nil
end

local function rayCachePut(ctx, key, now, v)
    V.rayCacheN = (V.rayCacheN or 0) + 1
    if V.rayCacheN > 4000 then V.rayCache, V.rayCacheN = {}, 0 end
    V.rayCache[key] = { t = now, v = v, fp = observerFp(ctx) }
end

-- Was an eye within OBSERVER_SAME_M of `eye` already tried? (records it if not)
local function eyeTried(tried, eye)
    for _, t in ipairs(tried) do
        if U.dist(t, eye) <= V.OBSERVER_SAME_M then return true end
    end
    tried[#tried + 1] = eye
    return false
end

-- Is a large object perceived by an unobstructed ray from a party member?
-- body = V.bigBody of a big creature: its size and reach from its body (V.bodyDist), not its anchor point.
function V.largePerceived(ctx, uuid, e, pos, kind, body)
    if not pos or V.rayBroken then return false end
    local bd = body and { height = body.h, radius = body.r } or V.bounds(e)
    local large = body or V.LARGE_KINDS[kind] or (bd and (bd.height >= V.LARGE_H or bd.radius >= V.LARGE_R))
    if not large then return false end
    local now = Ext.Utils.MonotonicTime()
    -- a creature can move: its verdict is kept for where it stands
    local key = body and (uuid .. "@" .. math.floor(pos[1] + 0.5) .. "," .. math.floor(pos[3] + 0.5)) or uuid
    local c = rayCacheGet(ctx, key, now)
    if c then return c.v end
    local o1 = ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return false end
    local h = math.max(bd and bd.height or 2.0, 1.0)
    local r = math.max(bd and bd.radius or 0.75, 0.3)
    local v, inRange, tried = false, false, {}
    for _, o in ipairs(ctx.observers) do
        local eye = o.pos and { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }
        local d = o.pos and (body and V.bodyDist(o.pos, pos, r, h) or U.dist(o.pos, pos))
        if eye and d <= o.sight and not eyeTried(tried, eye) then
            inRange = true
            local ok, res = pcall(function()
                for _, pt in ipairs(samplePoints(eye, pos, h, r)) do
                    if rayClear(uuid, eye, pt, math.max(0.5, r)) then return true end
                end
                return false
            end)
            if not ok then
                -- Fail closed: no raycasts, strict perception only.
                V.rayStats.errors = V.rayStats.errors + 1
                V.rayBroken = true
                Ext.Utils.PrintWarning("[BG3AI] large-object raycast disabled: " .. tostring(res))
                return false
            end
            if res then v = true; break end
        end
    end
    local st = V.rayStats
    if v then st.ray = st.ray + 1 elseif inRange then st.blocked = st.blocked + 1 else st.out_of_range = st.out_of_range + 1 end
    rayCachePut(ctx, key, now, v)
    return v
end

-- Small wall/floor fixtures (puzzle buttons, levers, switches): usable, not
-- pickable, not containers. They sit recessed in a wall or plinth, so
-- CanSee/LOS to their center fail, and the ray hits the wall face ~0.3 m short
-- of the center (S025/S026 Grand Mausoleum: the buttons under the paintings
-- Moonrise Towers and Grief never listed; the one under General, flush with
-- the wall, did). Perceived when a party member within FIXTURE_RANGE has an
-- unobstructed ray that ends within FIXTURE_SLACK of the center (the front
-- of the fixture). A wall between them stops the ray earlier.
V.FIXTURE_RANGE = 12.0
V.FIXTURE_SLACK = 0.45
V.FIXTURE_MAX_H = 1.2
V.FIXTURE_MAX_R = 0.6
V.FIXTURE_KINDS = { usable = true, object = true }
function V.fixturePerceived(ctx, uuid, e, pos, kind)
    if not pos or V.rayBroken or not V.FIXTURE_KINDS[kind] then return false end
    local bd = V.bounds(e)
    if not bd or bd.height > V.FIXTURE_MAX_H or bd.radius > V.FIXTURE_MAX_R then return false end
    local now = Ext.Utils.MonotonicTime()
    local key = "fx:" .. uuid
    local c = rayCacheGet(ctx, key, now)
    if c then return c.v end
    local o1 = ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return false end
    local v, tried = false, {}
    for _, o in ipairs(ctx.observers) do
        local eye = o.pos and { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }
        if eye and U.dist(o.pos, pos) <= math.min(o.sight or V.FIXTURE_RANGE, V.FIXTURE_RANGE)
            and not eyeTried(tried, eye) then
            local ok, res = pcall(function()
                for _, pt in ipairs({ { pos[1], pos[2] + bd.height * 0.5, pos[3] }, { pos[1], pos[2] + bd.height, pos[3] } }) do
                    local hit = raycast(eye, pt)
                    if not hit or hitEntityUuid(hit) == uuid then return true end
                    local hp = hit.Position
                    if hp and U.dist({ hp[1], hp[2], hp[3] }, pt) <= V.FIXTURE_SLACK then return true end
                end
                return false
            end)
            if not ok then
                V.rayStats.errors = V.rayStats.errors + 1
                V.rayBroken = true
                Ext.Utils.PrintWarning("[BG3AI] fixture raycast disabled: " .. tostring(res))
                return false
            end
            if res then v = true; break end
        end
    end
    rayCachePut(ctx, key, now, v)
    return v
end

-- Interactables with no AI bounds (V.bounds nil): neither ray rule above applies, and CanSee/LOS fail on an object
-- that occludes itself (live probe L7, S069 Stormshore Tabernacle: the Statue of Mystra 2.8-4.7 m from all four
-- members, see=0 los=0, bounds missing while the Statue of Helm had r 1.05 h 3.5; every ray from the eyes to 0.5/1.0/
-- 1.6 m above its feet hit the statue first, and get_nearby/interact never saw it while a player clicks it). Perceived
-- when a party member within sight has a ray at one of those heights whose first hit is the item itself: nothing
-- stands between the eye and it. Self hits only: a clear ray past a point we can't size proves nothing. Not "item":
-- pickables keep CanSee/LOS (no rays for every loose cup in a scan).
V.UNBOUNDED_KINDS = { usable = true, object = true, container = true }
V.UNBOUNDED_H = { 0.5, 1.0, 1.6 }
function V.unboundedPerceived(ctx, uuid, e, pos, kind)
    if not pos or V.rayBroken or not V.UNBOUNDED_KINDS[kind] or V.bounds(e) then return false end
    local now = Ext.Utils.MonotonicTime()
    local key = "ub:" .. uuid
    local c = rayCacheGet(ctx, key, now)
    if c then return c.v end
    local o1 = ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return false end
    local v, tried = false, {}
    for _, o in ipairs(ctx.observers) do
        local eye = o.pos and { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }
        if eye and U.dist(o.pos, pos) <= o.sight and not eyeTried(tried, eye) then
            local ok, res = pcall(function()
                for _, h in ipairs(V.UNBOUNDED_H) do
                    local hit = raycast(eye, { pos[1], pos[2] + h, pos[3] })
                    if hit and hitEntityUuid(hit) == uuid then return true end
                end
                return false
            end)
            if not ok then
                V.rayStats.errors = V.rayStats.errors + 1
                V.rayBroken = true
                Ext.Utils.PrintWarning("[BG3AI] unbounded-item raycast disabled: " .. tostring(res))
                return false
            end
            if res then v = true; break end
        end
    end
    rayCachePut(ctx, key, now, v)
    return v
end

-- Is a spot on the ground in view: within a party member's sight range, with
-- an unobstructed ray from its eye to `lift` m above the spot (a burning
-- floor, a cloud)? nil when the ray API is unusable (uncalibrated or broken):
-- the caller decides (nav hazards keep the patch: fail-safe for walking).
V.POINT_LIFT = 0.5
function V.pointSeen(ctx, pt, lift)
    if V.rayBroken then return nil end
    local o1 = ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return nil end
    local tgt = { pt[1], pt[2] + (lift or V.POINT_LIFT), pt[3] }
    for _, o in ipairs(ctx.observers) do
        if o.pos and U.dist(o.pos, tgt) <= o.sight then
            local eye = { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }
            local ok, hit = pcall(raycast, eye, tgt)
            if not ok then return nil end
            if not hit then return true end
            local d = hitDist(hit, eye)
            if d and d >= U.dist(eye, tgt) - 0.5 then return true end
        end
    end
    return false
end

-- Would a character standing at `feet` see the entity (uuid, e, pos)?
-- Planning only (combat_turn picking a spot to cast from): the cast itself is
-- still refused by the engine's HasLineOfSight. nil when the ray API is
-- unusable.
-- Characters: centre rays only, to the chest and the head, both clear. The
-- side / near-face samples said "in sight" where the engine's
-- HasLineOfSight did not (S023-S026: 4 of 5 los_spot steps, mostly ~1 m,
-- still "no line of sight"; live probe 2026-10-10: the 5-point rule
-- disagreed with the engine on 2 of 56 character pairs as a false yes, a
-- centre ray at any eye/target height on 0). margin (m): also a parallel ray
-- offset sideways by margin on each side must be clear (a spot that keeps
-- seeing the target when the geometry is a bit off).
-- Items: any of the body samples (middle / top / sides), as before.
-- A prone (downed) body is low: its bounds give the height.
V.LOS_SLACK = 0.3
function V.losFrom(ctx, uuid, e, pos, feet, margin)
    if V.rayBroken or not (feet and pos) then return nil end
    local o1 = ctx.observers and ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return nil end
    local bd = e and V.bounds(e)
    local h = math.max(bd and bd.height or 1.8, 0.4)
    local r = math.max(bd and bd.radius or 0.5, 0.3)
    local eye = { feet[1], feet[2] + V.EYE_H, feet[3] }
    local isChar = e and U.comp(e, "ServerCharacter") ~= nil
    local ok, res = pcall(function()
        if not isChar then
            for _, pt in ipairs(samplePoints(eye, pos, h, r)) do
                if rayClear(uuid, eye, pt, V.LOS_SLACK) then return true end
            end
            return false
        end
        local chest = { pos[1], pos[2] + h * 0.5, pos[3] }
        local head = { pos[1], pos[2] + h * 0.85, pos[3] }
        if not (rayClear(uuid, eye, chest, V.LOS_SLACK) and rayClear(uuid, eye, head, V.LOS_SLACK)) then return false end
        if margin and margin > 0 then
            local dx, dz = pos[1] - eye[1], pos[3] - eye[3]
            local len = math.sqrt(dx * dx + dz * dz)
            if len > 0.01 then
                local px, pz = -dz / len * margin, dx / len * margin
                for _, sgn in ipairs({ 1, -1 }) do
                    local e2 = { eye[1] + sgn * px, eye[2], eye[3] + sgn * pz }
                    local c2 = { chest[1] + sgn * px, chest[2], chest[3] + sgn * pz }
                    if not rayClear(uuid, e2, c2, V.LOS_SLACK) then return false end
                end
            end
        end
        return true
    end)
    if not ok then return nil end
    return res
end

------------------------------------------------------------------------
-- Status filter: only what portraits / Examine show
------------------------------------------------------------------------

V.statusCache = V.statusCache or {}

function V.statusInfo(id)
    local c = V.statusCache[id]
    if c ~= nil then return c or nil end
    local info = false
    local ok, st = pcall(Ext.Stats.Get, id)
    if ok and st then
        local okn, dn = pcall(function() return st.DisplayName end)
        local name = okn and U.loca(dn)
        local hidden = false
        local okf, flags = pcall(function() return st.StatusPropertyFlags end)
        if okf and type(flags) == "table" then
            for _, f in ipairs(flags) do
                if f == "DisablePortraitIndicator" then hidden = true end
            end
        end
        if name and not hidden then info = { name = name } end
    end
    V.statusCache[id] = info
    return info or nil
end

-- Item effect states a player sees on the object itself (a painting lights up) but that have no
-- name in the UI: unnamed EFFECT statuses with a visual effect. Only the look, never what it means
-- (audit C11: "solved" / "wrong order: resets" were the story script's verdicts). Mural paintings:
-- a pressed button lights its painting; later all glow, or they flash for 3 s.
V.ITEM_EFFECTS = {
    SHA_MAUSOLEUM_MURAL_INPROGRESS = "lit",
    SHA_MAUSOLEUM_MURAL_RIGHTORDER = "glowing",
    SHA_MAUSOLEUM_MURAL_WRONGORDER = "flashing",
}
V.itemEffectCache = V.itemEffectCache or {}

-- Shown text of item status `id`, or nil. generic: unnamed EFFECT statuses with a visual effect
-- read "glowing" (only asked for puzzle landmarks, where a glow is the state).
function V.itemEffect(id, generic)
    if V.ITEM_EFFECTS[id] then return V.ITEM_EFFECTS[id] end
    if not generic then return nil end
    local c = V.itemEffectCache[id]
    if c == nil then
        c = false
        local ok, st = pcall(Ext.Stats.Get, id)
        if ok and st then
            local okv, r = pcall(function()
                return st.StatusType == "EFFECT" and st.StatusEffect ~= nil and st.StatusEffect ~= ""
                    and not U.loca(st.DisplayName)
            end)
            if okv and r then c = "glowing" end
        end
        V.itemEffectCache[id] = c
    end
    return c or nil
end

-- Effect states on item entity e: "lit" / "glowing" / nil.
function V.itemEffects(e, generic)
    local sc = U.comp(e, "StatusContainer")
    if not sc then return nil end
    local out, seen = {}, {}
    pcall(function()
        for _, id in pairs(sc.Statuses) do
            local t = V.itemEffect(tostring(id), generic)
            if t and not seen[t] then seen[t] = true; out[#out + 1] = t end
        end
    end)
    if #out == 0 then return nil end
    table.sort(out)
    return table.concat(out, ", ")
end

-- Visible statuses of entity e: [{id, name, turns?}]
function V.statuses(e)
    local out, seen = {}, {}
    local sc = U.comp(e, "StatusContainer")
    if not sc then return out end
    local ok = pcall(function()
        for handle, id in pairs(sc.Statuses) do
            local sid = tostring(id)
            if not seen[sid] then
                local info = V.statusInfo(sid)
                if info then
                    seen[sid] = true
                    local s = { id = sid, name = info.name }
                    local okl, lt = pcall(function() return handle.StatusLifetime and handle.StatusLifetime.Lifetime end)
                    if okl and type(lt) == "number" and lt > 0 then s.turns = math.ceil(lt / 6) end
                    out[#out + 1] = s
                end
            end
        end
    end)
    if not ok then return {} end
    return out
end

------------------------------------------------------------------------
-- Item spatial index (enumerating ~14k items costs ~100 ms, so it is built
-- in small chunks on Tick and refreshed periodically; positions of
-- candidates are re-read live at query time).
------------------------------------------------------------------------

V.CELL = 8.0
-- Built in time slices: at most INDEX_BUDGET_MS of game-thread time per tick
-- (the clock is read every INDEX_CLOCK_EVERY items). Was 400 items per tick
-- through a closure + pcall each: 10-11 ms p50, 141 ms max, ~1.7x/s, 189 s of
-- the game thread per 2 h run, and every RPC queued behind it waited (BACKLOG #5).
-- Rebuilt on a level change, INDEX_DIRTY_MS after items leave an inventory
-- (drops, loot thrown on the floor), else every INDEX_REFRESH_MS; candidates'
-- positions are re-read at query time anyway.
V.INDEX_CHUNK = 400 -- synchronous builds (indexEnsure) only
V.INDEX_BUDGET_MS = 2
V.INDEX_CLOCK_EVERY = 32
V.INDEX_REFRESH_MS = 60000
V.INDEX_DIRTY_MS = 2000
V.INDEX_LEVEL_CHECK_MS = 500

V.index = V.index or { cells = {}, ready = false, built = 0, building = nil, level = nil }

local function cellKey(x, z)
    return math.floor(x / V.CELL) .. ":" .. math.floor(z / V.CELL)
end

local function currentLevel()
    local host = V.osi(Osi.GetHostCharacter)
    return host and V.osi(Osi.GetRegion, host) or nil
end

function V.indexStart()
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "ServerItem")
    if not ok or not list then return end
    V.index.building = { list = list, i = 1, cells = {}, level = currentLevel() }
    V.index.dirty = nil
end

-- Items left an inventory (dropped, thrown, spilled): rebuild soon.
function V.indexDirty()
    V.index.dirty = V.index.dirty or Ext.Utils.MonotonicTime()
end

-- No closure per item: one pcall of a file-level function.
local function itemCell(e)
    if e.InventoryMember then return nil end
    return e.Transform.Transform.Translate, e.Uuid.EntityUuid
end

-- Index up to `budget` items, or until budgetMs have passed (when given).
local function indexStep(budget, budgetMs)
    local b = V.index.building
    if not b then return end
    local list, n = b.list, #b.list
    local stop = math.min(n, b.i + budget - 1)
    local clock, t0 = Ext.Utils.MonotonicTime, budgetMs and Ext.Utils.MonotonicTime()
    local every, cells = V.INDEX_CLOCK_EVERY, b.cells
    local i = b.i
    while i <= stop do
        local ok, p, u = pcall(itemCell, list[i])
        if ok and p and u then
            local k = cellKey(p[1], p[3])
            local cell = cells[k]
            if not cell then cell = {}; cells[k] = cell end
            cell[#cell + 1] = u
        end
        i = i + 1
        if t0 and i % every == 0 and clock() - t0 >= budgetMs then break end
    end
    b.i = i
    if b.i > n then
        V.index.cells = b.cells
        V.index.level = b.level
        V.index.ready = true
        V.index.built = Ext.Utils.MonotonicTime()
        V.index.building = nil
    end
end

function V.indexTick()
    if Ext.Utils.GetGameState and tostring(Ext.Utils.GetGameState()) ~= "Running" then return end
    if V.index.building then
        indexStep(math.huge, V.INDEX_BUDGET_MS)
        return
    end
    local t = Ext.Utils.MonotonicTime()
    local x = V.index
    -- over the Lua heap cap (Shared/Mem.lua) only a missing index or a new level rebuilds
    local paused = BG3AI.Mem and BG3AI.Mem.paused()
    if not x.ready or (not paused and (t - x.built > V.INDEX_REFRESH_MS
        or (x.dirty and t - x.dirty >= V.INDEX_DIRTY_MS))) then
        V.indexStart()
        return
    end
    -- the level (2 Osiris calls) at most every INDEX_LEVEL_CHECK_MS
    if t - (x.levelChecked or 0) >= V.INDEX_LEVEL_CHECK_MS then
        x.levelChecked = t
        if currentLevel() ~= x.level then V.indexStart() end
    end
end

-- Synchronous build (first call only, when no index exists yet).
-- Also finishes a rebuild for a new level at once: the time-sliced build takes
-- seconds, and the old level's cells would answer in between.
function V.indexEnsure()
    local b = V.index.building
    if V.index.ready and not (b and b.level ~= V.index.level) then return end
    if not V.index.building then V.indexStart() end
    while V.index.building do indexStep(100000) end
end

-- Item uuids whose indexed position is within radius of pos (box test).
function V.itemsAround(pos, radius)
    V.indexEnsure()
    local out = {}
    local c0x, c1x = math.floor((pos[1] - radius) / V.CELL), math.floor((pos[1] + radius) / V.CELL)
    local c0z, c1z = math.floor((pos[3] - radius) / V.CELL), math.floor((pos[3] + radius) / V.CELL)
    for cx = c0x, c1x do
        for cz = c0z, c1z do
            local cell = V.index.cells[cx .. ":" .. cz]
            if cell then
                for _, u in ipairs(cell) do out[#out + 1] = u end
            end
        end
    end
    return out
end

if not V.tickSubscribed then
    V.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(V.indexTick)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] visibility index: " .. tostring(err)) end
    end)
end

------------------------------------------------------------------------
-- Memory: last-seen snapshots of characters, never refreshed while unseen.
------------------------------------------------------------------------

V.memory = V.memory or {}
V.MEMORY_CAP = 200

function V.remember(uuid, name, pos, relation)
    V.memory[uuid] = { name = name, pos = pos, relation = relation, t = Ext.Utils.MonotonicTime(), level = currentLevel() }
end

-- The party's last sight of one character on this level: {pos, ago_s} or nil
-- (what get_nearby lists as REMEMBERED; the refusal of an out-of-sight target
-- names it, Guard.target_visible).
function V.lastSeen(uuid)
    local m = uuid and V.memory[uuid]
    if not m or not m.pos or m.level ~= currentLevel() then return nil end
    return { pos = U.vec(m.pos, 1), ago_s = math.floor((Ext.Utils.MonotonicTime() - m.t) / 1000) }
end

-- A character the party saw on this level that is invisible now (not revealed):
-- a player saw it vanish. S030 Soft-Step Trial: a Shadow 1.8 m away answered
-- "target is not visible; last seen 2s ago: travel_to that spot" 3x.
V.INVISIBLE_SEEN_S = 120
function V.vanished(uuid)
    if not uuid or not U.comp(V.entity(uuid), "ServerCharacter") then return false end
    local m = V.lastSeen(uuid)
    if not m or m.ago_s > V.INVISIBLE_SEEN_S then return false end
    return truthy(V.osi(Osi.IsInvisible, uuid)) and not truthy(V.osi(Osi.IsDead, uuid)), m
end

function V.recall(exclude, around, radius)
    local out = {}
    local now = Ext.Utils.MonotonicTime()
    local lvl = currentLevel()
    for u, m in pairs(V.memory) do
        if not exclude[u] and m.level == lvl and m.pos and (not around or U.dist(around, m.pos) <= radius) then
            out[#out + 1] = { uuid = u, name = m.name, relation = m.relation, last_pos = U.vec(m.pos, 1),
                ago_s = math.floor((now - m.t) / 1000) }
        end
    end
    table.sort(out, function(a, b) return a.ago_s < b.ago_s end)
    while #out > 20 do table.remove(out) end
    return out
end

------------------------------------------------------------------------
-- Dev check for the large-object rule (complements dev.fairness_probe).
-- dev.perception_probe{radius=30} -> counts of large objects by how they are
-- perceived, plus the ones only the ray rule shows (shown anyway, so their
-- uuid/name are no leak). Anything hidden is reported as counts only.
------------------------------------------------------------------------

if BG3AI.RPC then
    BG3AI.RPC.register("dev.perception_probe", function(args)
        local ctx = V.context()
        local radius = tonumber(args.radius) or 30
        local center = ctx.observers[1] and ctx.observers[1].pos
        if not center then error("no party member in the world", 0) end
        if args.reset then V.rayBroken = nil; V.calib.t = -1e9 end
        V.rayCache = {}
        local c = { large = 0, by_sight = 0, by_ray = 0, not_perceived = 0, filtered = 0,
            ray_shown_traps = 0, ray_shown_secret_doors = 0 }
        local rayOnly = {}
        for _, u in ipairs(V.itemsAround(center, radius)) do
            local e = V.entity(u)
            local p = e and V.pos(e)
            if p and U.dist(p, center) <= radius then
                local kind = V.itemKind(u, e)
                local bd = V.bounds(e)
                if V.LARGE_KINDS[kind] or (bd and (bd.height >= V.LARGE_H or bd.radius >= V.LARGE_R)) then
                    c.large = c.large + 1
                    local shown = V.itemVisible(ctx, u, e, p)
                    if not shown then
                        if V.perceived(ctx, u, p) or V.largePerceived(ctx, u, e, p, kind) then
                            c.filtered = c.filtered + 1 -- hidden by the trap/secret/scenery rules
                        else
                            c.not_perceived = c.not_perceived + 1
                        end
                    elseif V.perceived(ctx, u, p) then
                        c.by_sight = c.by_sight + 1
                    else
                        c.by_ray = c.by_ray + 1
                        if truthy(V.osi(Osi.IsTrap, u)) and not truthy(V.osi(Osi.IsTrapDiscovered, u)) then
                            c.ray_shown_traps = c.ray_shown_traps + 1
                        end
                        local si = U.comp(e, "ServerItem")
                        local okS, sec = pcall(function() return si and si.IsSecretDoor end)
                        if okS and sec == true then c.ray_shown_secret_doors = c.ray_shown_secret_doors + 1 end
                        if #rayOnly < 25 then
                            rayOnly[#rayOnly + 1] = { uuid = u, name = V.name(u), kind = kind, dist = U.round(U.dist(p, center), 1),
                                height = bd and U.round(bd.height, 1), radius = bd and U.round(bd.radius, 1) }
                        end
                    end
                end
            end
        end
        return { radius = radius, counts = c, ray_only = rayOnly, stats = V.rayStats,
            calibrated = V.calib.ok, ray_broken = V.rayBroken or false }
    end)
end
