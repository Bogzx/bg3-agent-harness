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

-- In a dialogue (ServerCharacter.InDialog). Not Osi.IsInDialog: no such
-- Osiris call in this build, and every lookup of a missing name costs
-- ~1-10 ms (live 2026-10-10: nav.activity spent ~31 ms of its 1 Hz sample on
-- it, and the Events passive-roll filter never saw a dialogue).
function V.inDialog(uuid, e)
    e = e or V.entity(uuid)
    local ok, d = pcall(function() return e.ServerCharacter.InDialog end)
    return ok and d == true
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

-- No closure per call: V.pos runs for every character in a scan (900+ live).
local function translate(e) return e.Transform.Transform.Translate end
function V.pos(e)
    local ok, p = pcall(translate, e)
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
-- Cached for the current game frame (Nav.frame, bumped every tick): every op
-- in one eval (look = 6 server ops) shares it; V.rules alone costs ~0.5 ms
-- live. Callers treat it as read-only.
function V.frame() return BG3AI.Nav and BG3AI.Nav.frame or nil end
function V.context()
    local f = V.frame()
    if f and V.ctxFrame == f and V.ctxCache then return V.ctxCache end
    local party, isParty = V.party()
    local ctx = {
        party = party,
        is_party = isParty,
        observers = V.observers(party),
        rules = V.rules(),
    }
    V.ctxCache, V.ctxFrame = ctx, f
    return ctx
end

-- Every ServerCharacter with its position, scanned once per frame (live: 911
-- characters on WLD_Main_A, ~14 ms): state.nearby, nav.frontier and the
-- other scans in one eval share it. Rows are {e, p}; read-only.
function V.characters()
    local f = V.frame()
    if f and V.charsFrame == f and V.charsCache then return V.charsCache end
    local out = {}
    local ok, chars = pcall(Ext.Entity.GetAllEntitiesWithComponent, "ServerCharacter")
    if ok and chars then
        for _, e in ipairs(chars) do
            local p = V.pos(e)
            if p then out[#out + 1] = { e, p } end
        end
    end
    V.charsCache, V.charsFrame = out, f
    return out
end

-- Characters within radius of any of centers (a pos or a list of pos), from
-- the per-frame V.characters() cache: rows {e, p, d}, d to the nearest center.
-- Not Ext.Entity.GetEntitiesAroundPosition: bg3le implements it as a Lua walk
-- over every Transform entity (~5000), slower than this cached scan.
function V.charactersNear(centers, radius)
    if type(centers[1]) == "number" then centers = { centers } end
    local out = {}
    if #centers == 0 then return out end
    local sqrt = math.sqrt
    for _, row in ipairs(V.characters()) do
        local p = row[2]
        local best
        for _, c in ipairs(centers) do
            local dx, dz = p[1] - c[1], p[3] - c[3]
            if dx <= radius and dx >= -radius and dz <= radius and dz >= -radius then
                local dy = p[2] - c[2]
                local d = sqrt(dx * dx + dy * dy + dz * dz)
                if d <= radius and (not best or d < best) then best = d end
            end
        end
        if best then out[#out + 1] = { row[1], p, best } end
    end
    return out
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

-- A party member is at camp (the story's DB_PlayerInCamp, as A.in_camp reads it). Memoised on the frame's ctx.
function V.partyInCamp(ctx)
    if ctx.in_camp == nil then
        local hit = false
        local ok, rows = pcall(function() return Osi.DB_PlayerInCamp:Get(nil) end)
        if ok and rows then
            for _, r in ipairs(rows) do
                if ctx.is_party[V.uuid(r[1]) or ""] then hit = true break end
            end
        end
        ctx.in_camp = hit
    end
    return ctx.in_camp
end

-- Is character `uuid` (entity `e`) perceivable by the party right now?
function V.characterVisible(ctx, uuid, e, pos)
    if ctx.is_party[uuid] then return true end
    if scriptHidden(uuid) then return false end
    -- A recruited companion waiting at camp (CampPresence, as State.campRoster lists them) while the party is at
    -- camp: each stands at its own spot, often past the 18.5 m sight range and outside the facing cone. S081 03:01:
    -- get_nearby radius 60 at camp listed Withers (16 m) and Scratch, not Karlach, and talk_to Karlach was "not
    -- visible"; swap_party then walked up to her. A player at camp sees the camp and who waits there; the caller's
    -- radius still bounds the scan.
    if U.comp(e, "CampPresence") and V.partyInCamp(ctx) then return true end
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

-- The host's level, once per frame (V.itemVisible notes static things seen for every item a scan shows).
function V.levelNow()
    local f = V.frame()
    if f and V.levelFrame == f then return V.levelCache end
    local host = V.osi(Osi.GetHostCharacter)
    V.levelCache, V.levelFrame = host and V.osi(Osi.GetRegion, host) or nil, f
    return V.levelCache
end

-- Static things the party has seen stay valid targets (V.itemKnown), never listed again by this.
-- "item" too (S032 17:34: a Gilded Chest you can pick up, kind item, listed 12 m away on arrival, then "target is
-- not visible" to interact 4 min later from the next room); V.itemKnown drops one that moved since.
V.STATIC_KINDS = { door = true, container = true, usable = true, ladder = true, object = true, item = true }
V.SEEN_MOVED_M = 1.0
V.staticSeen = V.staticSeen or {}

-- Level-design helpers a player never sees: the Faith-Leap Trial's safe path is
-- ~80 "Cube 1x1x1" items of template UNI_SHA_InvisiblePlatform (probe S031:
-- not flagged Invisible, kind "object", so a name search would have listed the
-- hidden path). Cached per uuid (one Osi.GetTemplate per item ever).
V.HELPER_TEMPLATES = { "InvisiblePlatform" }
V.helperCache = V.helperCache or {}
function V.helperItem(uuid)
    local c = V.helperCache[uuid]
    if c ~= nil then return c end
    local tpl = tostring(V.osi(Osi.GetTemplate, uuid) or "")
    c = false
    for _, pat in ipairs(V.HELPER_TEMPLATES) do
        if tpl:find(pat, 1, true) then c = true break end
    end
    V.helperCache[uuid] = c
    return c
end

-- Is item `uuid` perceivable, and is it something worth listing?
-- Returns visible:boolean, kind:string.
-- pos defaults to the item's own position (S021: Guard called this without
-- one, so the LOS-in-sight and large-object ray checks were skipped and a
-- ladder/gate/bench listed in look at 8-22 m was "not visible" to interact).
function V.itemVisible(ctx, uuid, e, pos)
    e = e or V.entity(uuid)
    pos = pos or (e and V.pos(e))
    if scriptHidden(uuid) or truthy(V.osi(Osi.IsInvisible, uuid)) then return false end
    local si = U.comp(e, "ServerItem")
    if si then
        local ok, hidden = pcall(function() return si.Invisible or si.Invisible2 or si.Destroyed or si.OffStage end)
        if ok and hidden then return false end
    end
    if U.comp(e, "InventoryMember") then return false end -- carried / inside a container
    if V.helperItem(uuid) then return false end
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
    if V.perceived(ctx, uuid, pos) or V.largePerceived(ctx, uuid, e, pos, kind)
        or V.fixturePerceived(ctx, uuid, e, pos, kind) or V.unboundedPerceived(ctx, uuid, e, pos, kind) then
        if (V.FIXTURE_KINDS[kind] and V.isNamedFixture(uuid)) or kind == "ladder" then
            V.fixtureSeen[uuid] = { t = Ext.Utils.MonotonicTime(), level = V.levelNow() }
        end
        if V.STATIC_KINDS[kind] then
            V.staticSeen[uuid] = { t = Ext.Utils.MonotonicTime(), level = V.levelNow(), pos = pos and { pos[1], pos[2], pos[3] } }
        end
        return true, kind
    end
    if (V.FIXTURE_KINDS[kind] or kind == "ladder") and V.fixtureRemembered(ctx, uuid, pos) then return true, kind end
    return false
end

-- A target check for interact/route/travel_to/move_to: visible now, or a static thing (door, container, usable,
-- ladder) the party saw on this level in the last SEEN_STATIC_MS, still there, with a party member within
-- SEEN_STATIC_M. A player remembers the painting 10 m behind them and walks back to it. S025 Grand Mausoleum:
-- route e992 (Moonrise Towers, listed 10.8 m away one call before) and interact e996 (Grief, 2.8 m) refused
-- "not visible" as the party moved; S026: interact e444 the same; each cost a walk back + a retry. Listings
-- (get_nearby, look) keep the strict rule: only V.itemVisible.
V.SEEN_STATIC_MS = 600000
V.SEEN_STATIC_M = 40.0
function V.itemKnown(ctx, uuid, e, pos)
    e = e or V.entity(uuid)
    pos = pos or (e and V.pos(e))
    local vis, kind = V.itemVisible(ctx, uuid, e, pos)
    if vis then return true, kind end
    local m = V.staticSeen[uuid]
    if not (m and pos and e) or Ext.Utils.MonotonicTime() - m.t > V.SEEN_STATIC_MS or m.level ~= V.levelNow() then
        return false
    end
    local host = V.osi(Osi.GetHostCharacter)
    if host and truthy(V.osi(Osi.IsInCombat, host)) then return false end -- a fight targets what is in sight
    -- gone since: picked up, destroyed, made invisible or hidden by a script
    if U.comp(e, "InventoryMember") or scriptHidden(uuid) or truthy(V.osi(Osi.IsInvisible, uuid)) then return false end
    if m.pos and U.dist(m.pos, pos) > V.SEEN_MOVED_M then return false end -- moved since: not where the party saw it
    local si = U.comp(e, "ServerItem")
    if si then
        local ok, gone = pcall(function() return si.Invisible or si.Invisible2 or si.Destroyed or si.OffStage end)
        if ok and gone then return false end
    end
    for _, o in ipairs(ctx.observers or {}) do
        if o.pos and U.dist(o.pos, pos) <= V.SEEN_STATIC_M then return true, V.itemKind(uuid, e) end
    end
    return false
end

-- An item a party member used is known to the whole party (V.itemKnown): they all saw it used. S069 20:36: a hand
-- click used e623 (Statue of Mystra), then `interact e623 who=Gale` from the same spot was "target is not visible".
function V.noteUsed(char, item, success)
    local u = V.uuid(item)
    if not (u and truthy(success) and V.context().is_party[V.uuid(char) or ""]) then return end
    local e = V.entity(u)
    local pos = e and V.pos(e)
    if pos then V.staticSeen[u] = { t = Ext.Utils.MonotonicTime(), level = V.levelNow(), pos = pos } end
end
if Ext.Osiris and not V.useListening then
    V.useListening = true
    pcall(Ext.Osiris.RegisterListener, "UseFinished", 3, "after", function(char, item, success)
        pcall(V.noteUsed, char, item, success)
    end)
end

-- Puzzle fixtures by their shown name (State.FIXTURE_NAMES: Button, Lever, Switch, ...).
function V.isNamedFixture(uuid)
    local S = BG3AI.State
    return type(S) == "table" and type(S.FIXTURE_NAMES) == "table" and S.FIXTURE_NAMES[V.name(uuid) or ""] == true
end

-- A fixture seen moments ago stays known (fixtures don't move; a player remembers the button): live 11:3x, Grand
-- Mausoleum: after pressing the button under Moonrise Towers, the one under Grief (recessed, 12 m along the same
-- wall) was hidden by the plinth and interact refused "target is not visible". Kept FIXTURE_REMEMBER_MS on the same
-- level while a party member is within FIXTURE_REMEMBER_M. Ladders too (se 12:3x, Roadside Cliffs: the ladder 13 m N
-- of a ledge was named in one travel_to stop and missing from the next, depending on where the walk ended).
V.FIXTURE_REMEMBER_MS = 300000
V.FIXTURE_REMEMBER_M = 30.0
V.fixtureSeen = V.fixtureSeen or {}
function V.fixtureRemembered(ctx, uuid, pos)
    local m = V.fixtureSeen[uuid]
    if not (m and pos) or Ext.Utils.MonotonicTime() - m.t > V.FIXTURE_REMEMBER_MS then return false end
    local host = V.osi(Osi.GetHostCharacter)
    if m.level ~= (host and V.osi(Osi.GetRegion, host) or nil) then return false end
    for _, o in ipairs(ctx.observers) do
        if o.pos and U.dist(o.pos, pos) <= V.FIXTURE_REMEMBER_M then return true end
    end
    return false
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
function V.largePerceived(ctx, uuid, e, pos, kind)
    if not pos or V.rayBroken then return false end
    local bd = V.bounds(e)
    local large = V.LARGE_KINDS[kind] or (bd and (bd.height >= V.LARGE_H or bd.radius >= V.LARGE_R))
    if not large then return false end
    local now = Ext.Utils.MonotonicTime()
    local c = rayCacheGet(ctx, uuid, now)
    if c then return c.v end
    local o1 = ctx.observers[1]
    if not (o1 and o1.pos and V.rayCalibrated(o1)) then return false end
    local h = math.max(bd and bd.height or 2.0, 1.0)
    local r = math.max(bd and bd.radius or 0.75, 0.3)
    local v, inRange, tried = false, false, {}
    for _, o in ipairs(ctx.observers) do
        local eye = o.pos and { o.pos[1], o.pos[2] + V.EYE_H, o.pos[3] }
        if eye and U.dist(o.pos, pos) <= o.sight and not eyeTried(tried, eye) then
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
    rayCachePut(ctx, uuid, now, v)
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
-- name in the UI: unnamed EFFECT statuses with a visual effect. Grand Mausoleum mural puzzle
-- (Story Act2_SHA_Mausoleum): a pressed button lights its painting (INPROGRESS) until the 3rd
-- press resolves the order: all glow (RIGHTORDER, walls open) or flash 3 s with the fail sound
-- (WRONGORDER: the vents fire and every painting goes dark = reset). Live 11:40: no way to tell a
-- painting lit by an earlier session's press, so the "right" order hit the trap.
V.ITEM_EFFECTS = {
    SHA_MAUSOLEUM_MURAL_INPROGRESS = "lit",
    SHA_MAUSOLEUM_MURAL_RIGHTORDER = "glowing (solved)",
    SHA_MAUSOLEUM_MURAL_WRONGORDER = "flashing (wrong order: resets)",
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

-- Effect states on item entity e: "lit" / "glowing (solved)" / nil.
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

return V
