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
    return V.perceived(ctx, uuid, pos) or V.bigPerceived(ctx, uuid, e, pos)
end

------------------------------------------------------------------------
-- Big creatures. S091 (the Netherbrain's psyche, 08:19-08:29): the brain, a huge body filling the screen, had no
-- id until it took damage (get_nearby "Nothing visible in range", combat state `unseen_combatants`): its anchor
-- point is 8 m below the party's platform and 20-22.5 m from every member, past the 18.5 m sight range; it listed
-- at 12 m. A player sees a body that size from wherever any part of it is in sight. So a Huge/Gargantuan creature
-- (or one with AI bounds this wide) is within a member's sight range when the nearest point of its body is
-- (V.bodyDist on a cylinder from its bounds or its size), and seen there by LOS or a clear ray to its body.
------------------------------------------------------------------------

V.BIG_R = 1.5                        -- AI bounds radius (m) from which a creature counts as big
V.SIZE_REACH = { [4] = 2.25, [5] = 3.0 } -- ObjectSize Huge, Gargantuan: half the 5e space (15 ft, 20 ft), m

-- Distance from `from` to a cylinder of radius r and height h standing on `pos` (pure; tested offline).
function V.bodyDist(from, pos, r, h)
    local dx, dz = from[1] - pos[1], from[3] - pos[3]
    local horiz = math.max(0, math.sqrt(dx * dx + dz * dz) - r)
    local y, dy = from[2], 0
    if y < pos[2] then dy = pos[2] - y elseif y > pos[2] + h then dy = y - pos[2] - h end
    return math.sqrt(horiz * horiz + dy * dy)
end

-- {r, h} of a big creature's body, or nil. Medium and smaller sizes skip the bounds read (~0.25 ms live).
function V.bigBody(e)
    local okS, size = pcall(function() return e.ObjectSize.Size end)
    size = okS and tonumber(size) or nil
    if size and size <= 2 then return nil end
    local bd = V.bounds(e)
    local r = math.max(bd and bd.radius or 0, V.SIZE_REACH[size] or 0)
    if r < V.BIG_R then return nil end
    return { r = r, h = math.max(bd and bd.height or 0, 2 * r) }
end

-- uuid fights in the fight a party member is in (the game's combat guid).
function V.inPartyFight(ctx, uuid)
    local g = V.osi(Osi.CombatGetGuidFor, uuid)
    if not g then return false end
    for _, u in ipairs(ctx.party) do
        if V.osi(Osi.CombatGetGuidFor, u) == g then return true end
    end
    return false
end

-- V31, S093 (the psyche): the platforms don't answer the downward calibration ray (check_all "calibration ray missed
-- the floor: rule is off"), so the ray branch is off exactly there. While the rays can't answer at this spot
-- (uncalibrated; a broken or switched-off ray API stays strict), a big creature in the party's own fight with its
-- body within a member's sight range is seen: it is in the fight's turn order and a body that size is on screen.
function V.bigPerceived(ctx, uuid, e, pos)
    local body = pos and V.bigBody(e)
    if not body then return false end
    local near = false
    for _, o in ipairs(ctx.observers) do
        if o.pos and V.bodyDist(o.pos, pos, body.r, body.h) <= o.sight then
            if truthy(V.osi(Osi.HasLineOfSight, o.uuid, uuid)) then return true end
            near = true
        end
    end
    if not near then return false end
    if V.largePerceived(ctx, uuid, e, pos, nil, body) then return true end
    return not V.rayBroken and not V.calib.ok and V.inPartyFight(ctx, uuid)
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

-- The item an active objective's marker sits on, with a party member within sight of it: the game draws the quest
-- marker over it on screen and highlights it. S094 09:53-10:08, the High Hall: o1 "Netherbrain Stem" (marker
-- END_BrainStem, an item) routed to 5.7 m, yet none of the rules here listed the Brain Stem (a wall of flesh) from
-- the spot travel_to(o1) stopped at, so get_nearby missed it and interact had no id: 7 hand clicks and 9
-- screenshots over two tries. Walls don't hide it from this rule (its spot is on the map already); called
-- after itemVisible's trap/secret/scenery filters.
function V.objectivePerceived(ctx, uuid, pos)
    local N = BG3AI.Nav
    if not (pos and N and N.objectiveTargets) then return false end
    local ok, set = pcall(N.objectiveTargets, ctx)
    if not (ok and type(set) == "table" and set[uuid]) then return false end
    for _, o in ipairs(ctx.observers or {}) do
        if o.pos and U.dist(o.pos, pos) <= (o.sight or V.DEFAULT_SIGHT) then return true end
    end
    return false
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
        or V.fixturePerceived(ctx, uuid, e, pos, kind) or V.unboundedPerceived(ctx, uuid, e, pos, kind)
        or V.objectivePerceived(ctx, uuid, pos) then
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
-- Floor plates (a subset of the named fixtures): stepping on one presses it. Walks go round the ones the party has
-- seen (Actions.lua A.known_plates, vis V25).
V.PLATE_NAMES = { ["Pressure Plate"] = true, ["Marble Plate"] = true }
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

-- The rest of the module (split for the 1000-line cap).
for _, part in ipairs({ "Server/VisibilityObjects.lua" }) do
    Ext.Require(part)
end

return V
