-- Fair visibility layer (server context). See research/02-visibility.md.
--
-- Decides whether a human player could perceive an entity right now, and
-- provides the shared, allowlisting building blocks State.lua projects with.
-- Nothing in here returns raw components; callers build fresh tables.
--
-- Verified live (bg3le, Patch 8):
--   * Osi.CanSee(viewer, target) == membership of target's SightUuid in the
--     viewer's SightEntityViewshed. It respects the 90 deg FOV cone, sight
--     range (Sight.Sight, ~18.5 m) and occlusion, and covers items too.
--   * Osi.HasLineOfSight is FOV-independent. A human sees around the party via
--     the camera, so "perceived" = CanSee by any party member, OR line of sight
--     from a party member within that member's sight range.
--   * Undiscovered traps: IsTrap=1, IsTrapDiscovered=0 (trapped chests,
--     buttons and books look like normal objects; standalone tripwires are
--     invisible/off stage until found).

BG3AI = BG3AI or {}
local U = BG3AI.Util
local V = BG3AI.Visibility or {}
BG3AI.Visibility = V

local truthy = U.truthy

-- Ruleset modifiers that change what the UI shows (RulesetModifiers.lsx).
V.RULE = {
    HIDE_NPC_HP = "9b349f94-e520-4c49-83e1-c0a8e0543710",
    DIALOGUE_DC_HIDE = "743a3998-636a-4b39-951b-f742a56b07fc",
    DIALOGUE_DC_PREVIEW = "1e5d2feb-3c81-4c0e-86d8-37337603234a",
    HIDE_FAILED_PERCEPTION_ROLLS = "19e33ff1-76fc-4447-a5ff-09a001e9544f",
    DIALOGUE_PASSIVE_ROLL_HIDE = "38fc0d10-ee64-40d6-b1d9-35f8de95b972",
}

-- Statuses on a party member that reveal invisible creatures, with range (m).
V.REVEAL_INVISIBLE = {
    SEE_INVISIBILITY = 9.0,
    MAG_SEE_INVISIBILITY_HIDDEN_IGNORE_RESTING = 9.0,
    TRUESIGHT = 18.0,
    MOD_Generic_Truesight = 18.0,
}
-- Statuses on the target that outline it for everyone (Faerie Fire etc.).
V.OUTLINED = { FAERIE_FIRE = true }

V.DEFAULT_SIGHT = 18.5

------------------------------------------------------------------------
-- Small helpers
------------------------------------------------------------------------

-- Osiris strings like "S_Player_Laezel_58a6...12" -> bare GUID.
function V.uuid(s)
    if type(s) ~= "string" then return nil end
    return s:match("%x+%-%x+%-%x+%-%x+%-%x+$") or s
end

function V.osi(fn, ...)
    local ok, r = pcall(fn, ...)
    if ok then return r end
    return nil
end

function V.entity(uuid)
    if not uuid then return nil end
    local ok, e = pcall(Ext.Entity.Get, uuid)
    if ok then return e end
    return nil
end

function V.entityUuid(e)
    local ok, u = pcall(function() return e.Uuid.EntityUuid end)
    if ok then return u end
    return nil
end

function V.pos(e)
    local ok, p = pcall(function() return e.Transform.Transform.Translate end)
    if ok and p then return { p[1], p[2], p[3] } end
    return nil
end

-- The name the UI shows (story overrides and disguises applied).
function V.name(uuid)
    local key = V.osi(Osi.GetDisplayName, uuid)
    return U.loca(key) or "Unknown"
end

------------------------------------------------------------------------
-- Party and rules
------------------------------------------------------------------------

function V.party()
    local out, seen = {}, {}
    local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end)
    if ok and rows then
        for _, row in ipairs(rows) do
            local u = V.uuid(row[1])
            if u and not seen[u] then
                seen[u] = true
                out[#out + 1] = u
            end
        end
    end
    return out, seen
end

-- Party members that are in the world and can perceive (on stage, not dead).
function V.observers(party)
    local out = {}
    for _, u in ipairs(party) do
        if truthy(V.osi(Osi.IsOnStage, u)) and not truthy(V.osi(Osi.IsDead, u)) then
            local e = V.entity(u)
            out[#out + 1] = {
                uuid = u,
                pos = e and V.pos(e),
                sight = (e and U.comp(e, "Sight") and e.Sight.Sight) or V.DEFAULT_SIGHT,
            }
        end
    end
    return out
end

function V.rules()
    local r = {}
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "RulesetModifiers")
    local mods = ok and list and list[1] and U.comp(list[1], "RulesetModifiers")
    for k, guid in pairs(V.RULE) do
        local v = false
        if mods then
            local ok2, x = pcall(function() return mods.Modifiers[guid] end)
            v = ok2 and x == true
        end
        r[k] = v
    end
    return r
end

-- One context per op call: party, observers, rules.
function V.context()
    local party, isParty = V.party()
    return {
        party = party,
        is_party = isParty,
        observers = V.observers(party),
        rules = V.rules(),
    }
end

------------------------------------------------------------------------
-- Perception
------------------------------------------------------------------------

-- Engine sight from any party member, or LOS within that member's sight
-- range (the camera lets a human see around the party, not just its FOV).
function V.perceived(ctx, uuid, pos)
    for _, o in ipairs(ctx.observers) do
        if o.uuid == uuid then return true end
        if truthy(V.osi(Osi.CanSee, o.uuid, uuid)) then return true end
        if pos and o.pos and U.dist(o.pos, pos) <= o.sight
            and truthy(V.osi(Osi.HasLineOfSight, o.uuid, uuid)) then
            return true
        end
    end
    return false
end

local function scriptHidden(uuid)
    return not truthy(V.osi(Osi.IsOnStage, uuid)) or truthy(V.osi(Osi.IsInvisibleByScript, uuid))
end

local function hasAnyStatus(uuid, set)
    for status in pairs(set) do
        if truthy(V.osi(Osi.HasActiveStatus, uuid, status)) then return true end
    end
    return false
end

local function invisibleRevealed(ctx, uuid, pos)
    if hasAnyStatus(uuid, V.OUTLINED) then return true end
    for _, o in ipairs(ctx.observers) do
        for status, range in pairs(V.REVEAL_INVISIBLE) do
            if truthy(V.osi(Osi.HasActiveStatus, o.uuid, status)) and pos and o.pos
                and U.dist(o.pos, pos) <= range
                and truthy(V.osi(Osi.HasLineOfSight, o.uuid, uuid)) then
                return true
            end
        end
    end
    return false
end

-- Hostile-or-neutral sneakers we have explicitly seen being spotted (filled by
-- Events.lua from combat/stealth events). Keyed by uuid -> time.
V.spotted = V.spotted or {}

function V.isAlly(ctx, uuid)
    if ctx.is_party[uuid] then return true end
    local o = ctx.observers[1] or { uuid = ctx.party[1] }
    return o.uuid and truthy(V.osi(Osi.IsAlly, o.uuid, uuid)) or false
end

-- Is character `uuid` (entity `e`) perceivable by the party right now?
function V.characterVisible(ctx, uuid, e, pos)
    if ctx.is_party[uuid] then return true end
    if scriptHidden(uuid) then return false end
    pos = pos or V.pos(e)
    local ally = V.isAlly(ctx, uuid)
    if not ally and truthy(V.osi(Osi.IsInvisible, uuid)) then
        return invisibleRevealed(ctx, uuid, pos)
    end
    if not ally and truthy(V.osi(Osi.HasActiveStatus, uuid, "SNEAKING")) then
        -- Strict: an enemy sneaker is only reported once the engine sight says
        -- a party member actually sees it (spot checks feed the viewshed) or a
        -- visible event revealed it.
        if V.spotted[uuid] then return V.perceived(ctx, uuid, pos) end
        for _, o in ipairs(ctx.observers) do
            if truthy(V.osi(Osi.CanSee, o.uuid, uuid)) then return true end
        end
        return false
    end
    return V.perceived(ctx, uuid, pos)
end

-- Item classification from server flags (no hidden flags are ever exported).
function V.itemKind(uuid, e)
    local si = U.comp(e, "ServerItem")
    if not si then return "item" end
    local ok, r = pcall(function()
        if si.IsDoor then return "door" end
        if si.IsLadder then return "ladder" end
        if si.IsContainer then return "container" end
        if si.CanBePickedUp then return "item" end
        if si.CanUse then return "usable" end
        return "object"
    end)
    return ok and r or "item"
end

-- Is item `uuid` perceivable, and is it something worth listing?
-- Returns visible:boolean, kind:string.
function V.itemVisible(ctx, uuid, e, pos)
    if scriptHidden(uuid) or truthy(V.osi(Osi.IsInvisible, uuid)) then return false end
    local si = U.comp(e, "ServerItem")
    if si then
        local ok, hidden = pcall(function() return si.Invisible or si.Invisible2 or si.Destroyed or si.OffStage end)
        if ok and hidden then return false end
    end
    if U.comp(e, "InventoryMember") then return false end -- carried / inside a container
    local kind = V.itemKind(uuid, e)
    local interact = truthy(V.osi(Osi.GetCanInteract, uuid))
    local isTrap = truthy(V.osi(Osi.IsTrap, uuid))
    if isTrap and not truthy(V.osi(Osi.IsTrapDiscovered, uuid)) then
        -- Trapped chests/doors/books look normal: list them, never the trap.
        -- Standalone traps (plates, tripwires) stay hidden until discovered.
        if not interact or (kind ~= "container" and kind ~= "door" and kind ~= "usable" and kind ~= "item") then
            return false
        end
    end
    local secret = si and select(2, pcall(function() return si.IsSecretDoor end))
    if secret == true and not interact then return false end
    if not interact and kind ~= "door" and kind ~= "container" and kind ~= "ladder" and not isTrap then
        return false -- scenery: the game doesn't highlight it either
    end
    if not V.perceived(ctx, uuid, pos) and not V.largePerceived(ctx, uuid, e, pos, kind) then return false end
    return true, kind
end

------------------------------------------------------------------------
-- Large-object perception (doors, ladders, big interactables).
--
-- HasLineOfSight to a door is often false because the door is its own
-- occluder, and CanSee is FOV-coned, so doors used to appear only up close.
-- For these objects only, a party member also perceives the object if a
-- physics ray from its eye reaches one of a few sample points on the object's
-- bounds unobstructed (or the first thing it hits is the object itself).
-- Characters never use this path: they stay on CanSee/LOS above.
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
V.rayCache = V.rayCache or {}
V.rayStats = V.rayStats or { ray = 0, blocked = 0, out_of_range = 0, errors = 0, uncalibrated = 0 }

-- {height, radius} from the entity's AI bounds (Hit preferred), or nil.
function V.bounds(e)
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

-- Is a large object perceived by an unobstructed ray from a party member?
function V.largePerceived(ctx, uuid, e, pos, kind)
    if not pos or V.rayBroken then return false end
    local bd = V.bounds(e)
    local large = V.LARGE_KINDS[kind] or (bd and (bd.height >= V.LARGE_H or bd.radius >= V.LARGE_R))
    if not large then return false end
    local now = Ext.Utils.MonotonicTime()
    local c = V.rayCache[uuid]
    if c and now - c.t < V.RAY_CACHE_MS then return c.v end
    local o1 = ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return false end
    local h = math.max(bd and bd.height or 2.0, 1.0)
    local r = math.max(bd and bd.radius or 0.75, 0.3)
    local v, inRange = false, false
    for _, o in ipairs(ctx.observers) do
        if o.pos and U.dist(o.pos, pos) <= o.sight then
            inRange = true
            local eye = { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }
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
    V.rayCacheN = (V.rayCacheN or 0) + 1
    if V.rayCacheN > 4000 then V.rayCache, V.rayCacheN = {}, 0 end
    V.rayCache[uuid] = { t = now, v = v }
    return v
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
V.INDEX_CHUNK = 400
V.INDEX_REFRESH_MS = 15000

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
end

local function indexStep(budget)
    local b = V.index.building
    if not b then return end
    local list, n = b.list, #b.list
    local stop = math.min(n, b.i + budget - 1)
    for i = b.i, stop do
        local e = list[i]
        local ok, p, u = pcall(function()
            if e.InventoryMember then return nil end
            return e.Transform.Transform.Translate, e.Uuid.EntityUuid
        end)
        if ok and p and u then
            local k = cellKey(p[1], p[3])
            local cell = b.cells[k]
            if not cell then cell = {}; b.cells[k] = cell end
            cell[#cell + 1] = u
        end
    end
    b.i = stop + 1
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
        indexStep(V.INDEX_CHUNK)
        return
    end
    local t = Ext.Utils.MonotonicTime()
    local lvl = currentLevel()
    if not V.index.ready or t - V.index.built > V.INDEX_REFRESH_MS or lvl ~= V.index.level then
        V.indexStart()
    end
end

-- Synchronous build (first call only, when no index exists yet).
function V.indexEnsure()
    if V.index.ready then return end
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

return V
