-- BG3AI action guard (server context).
--
-- Every action op goes through here before touching the game. The guard
-- enforces what the in-game UI would enforce for a human player:
--   * the actor is a controllable party member (alive, not downed, on stage)
--   * targets are things the party can see (BG3AI.Visibility, with a fallback)
--   * combat gating: in combat, only the character whose turn it is may act
--   * the cheat blocklist: Osiris calls are only made through G.osi(), which
--     refuses anything that is not on the action allowlist
--   * spells: known, prepared/available, off cooldown, resources available,
--     in range and in line of sight; and no Ignore*/Forced cast options
-- See research/03-actions.md §7.

BG3AI = BG3AI or {}
local U = BG3AI.Util
local G = {}
BG3AI.Guard = G

G.MAX_MOVE_DIST = 60.0      -- metres; farther than a click on screen could reach
G.MAX_INTERACT_DIST = 40.0  -- metres from the actor for interact/pickup/talk targets
G.TALK_RANGE = 2.5          -- how close the actor walks before requesting a dialogue

------------------------------------------------------------------------
-- Identity helpers
------------------------------------------------------------------------

local GUID_PAT = "(%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x)$"

-- "S_Player_Laezel_58a69333-..." or a bare GUID -> bare lowercase GUID (or nil).
function G.guid(s)
    if type(s) ~= "string" then return nil end
    local g = s:match(GUID_PAT)
    return g and g:lower() or nil
end

function G.same(a, b)
    local ga, gb = G.guid(a), G.guid(b)
    return ga ~= nil and ga == gb
end

local function b(x) return U.truthy(x) end
G.b = b

-- Osiris query wrapper: never throws, returns nil on error.
function G.q(name, ...)
    local fn = Osi[name]
    local ok, r1, r2, r3 = pcall(fn, ...)
    if ok then return r1, r2, r3 end
    return nil
end

function G.pos(uuid)
    local ok, x, y, z = pcall(Osi.GetPosition, uuid)
    if ok and x then return { x, y, z } end
    return nil
end

function G.entity(uuid)
    local ok, e = pcall(Ext.Entity.Get, uuid)
    if ok then return e end
    return nil
end

function G.party()
    local out = {}
    local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end)
    if ok and rows then
        for _, row in ipairs(rows) do
            local g = G.guid(row[1])
            if g then out[#out + 1] = g end
        end
    end
    return out
end

function G.is_party(uuid)
    local g = G.guid(uuid)
    if not g then return false end
    for _, p in ipairs(G.party()) do
        if p == g then return true end
    end
    return false
end

function G.host()
    return G.guid(G.q("GetHostCharacter"))
end

------------------------------------------------------------------------
-- Refusals. A refusal is an error() with a table so ops can turn it into
-- a player-facing reason; G.check() catches it.
------------------------------------------------------------------------

local Refusal = {}
Refusal.__index = Refusal
Refusal.__tostring = function(r) return r.reason end

function G.refuse(reason)
    error(setmetatable({ reason = reason }, Refusal), 0)
end

function G.is_refusal(e)
    return getmetatable(e) == Refusal
end

------------------------------------------------------------------------
-- Actor checks
------------------------------------------------------------------------

function G.is_dead(uuid) return b(G.q("IsDead", uuid)) end

function G.is_downed(uuid)
    return b(G.q("HasActiveStatus", uuid, "DOWNED"))
end

function G.in_combat(uuid) return b(G.q("IsInCombat", uuid)) end

-- bg3le has no IsInDialog; SpeakerGetDialog(speaker, 1) returns the dialog
-- resource while the speaker is in one, and DialogState.DialogId is non-zero.
function G.in_dialogue(uuid)
    if G.q("SpeakerGetDialog", uuid, 1) ~= nil then return true end
    local e = G.entity(uuid)
    local ds = e and U.comp(e, "DialogState")
    local ok, id = pcall(function() return ds and ds.DialogId end)
    if not ok or id == nil or id == 0 then return false end
    -- DialogState.DialogId keeps the last instance id after the dialogue
    -- ends (seen live: 4 with no dialogue running), so only trust it while
    -- that instance still exists in the dialog manager.
    local ok2, live = pcall(function()
        local dm = Ext.Utils.GetDialogManager()
        return dm and dm.Dialogs[id] ~= nil
    end)
    return ok2 and live == true
end

-- Is it this character's turn? Uses the replicated TurnBased component.
function G.is_my_turn(uuid)
    local e = G.entity(uuid)
    local tb = e and U.comp(e, "TurnBased")
    if not tb then return false end
    local ok, v = pcall(function() return tb.IsActiveCombatTurn end)
    return ok and v == true
end

-- The actor must be a living, standing, on-stage party member.
function G.actor(char)
    local g = G.guid(char)
    if not g then G.refuse("no character given") end
    if not G.is_party(g) then G.refuse("not a party member") end
    if not b(G.q("IsOnStage", g)) then G.refuse("character is not present") end
    if G.is_dead(g) then G.refuse("character is dead") end
    if G.is_downed(g) then G.refuse("character is downed") end
    return g
end

-- Combat gating.
--   mode "turn":      in combat, the actor must be the active combatant
--   mode "no_combat": refused while the actor is in combat (e.g. Osiris item
--                     moves that would skip action costs; use the UI path)
--   mode "any":       always allowed (selection, chaining)
function G.combat_gate(char, mode)
    if mode == "any" then return end
    if not G.in_combat(char) then return end
    if mode == "no_combat" then
        G.refuse("not available during combat through this action (use the UI)")
    end
    if not G.is_my_turn(char) then G.refuse("it is not this character's turn") end
end

function G.not_in_dialogue(char)
    if G.in_dialogue(char) then G.refuse("character is in a conversation") end
end

------------------------------------------------------------------------
-- Visibility. Worker "vis" provides BG3AI.Visibility; we accept any of
-- these functions (first found wins):
--   V.is_visible(uuid) -> bool        (preferred)
--   V.IsVisible(uuid) -> bool
--   V.CharacterVisible(party, uuid) / V.ItemVisible(party, uuid)
--   V.position_known(pos) -> bool     (optional; for point targets)
-- Fallback: some party member CanSee the target, and it is on stage and not
-- invisible or sneaking. That is strict, never generous.
------------------------------------------------------------------------

local function fallback_visible(target)
    if not b(G.q("IsOnStage", target)) then return false end
    if b(G.q("IsInvisible", target)) then return false end
    local is_char = b(G.q("IsCharacter", target))
    if is_char and not G.is_party(target) and b(G.q("HasActiveStatus", target, "SNEAKING")) then
        return false
    end
    for _, p in ipairs(G.party()) do
        if b(G.q("CanSee", p, target)) then return true end
    end
    return false
end

function G.visible(target)
    local g = G.guid(target)
    if not g then return false end
    if G.is_party(g) then return true end
    local V = BG3AI.Visibility
    if type(V) == "table" then
        local ok, r
        if type(V.is_visible) == "function" then
            ok, r = pcall(V.is_visible, g)
        elseif type(V.IsVisible) == "function" then
            ok, r = pcall(V.IsVisible, g)
        elseif type(V.context) == "function" and type(V.characterVisible) == "function" then
            -- worker vis's API: context() + characterVisible/itemVisible(ctx, uuid, e)
            ok, r = pcall(function()
                local ctx = V.context()
                local e = G.entity(g)
                if b(Osi.IsCharacter(g)) then return V.characterVisible(ctx, g, e) end
                if type(V.itemVisible) == "function" then
                    return (V.itemVisible(ctx, g, e)) == true
                end
                return fallback_visible(g)
            end)
        elseif type(V.CharacterVisible) == "function" and type(V.Party) == "function" then
            ok, r = pcall(function()
                local P = V.Party()
                if b(Osi.IsCharacter(g)) then return V.CharacterVisible(P, g) end
                if type(V.ItemVisible) == "function" then return V.ItemVisible(P, g) end
                return fallback_visible(g)
            end)
        end
        if ok then return r == true end
    end
    return fallback_visible(g)
end

function G.target_visible(target)
    if not G.visible(target) then G.refuse("target is not visible") end
end

-- Point targets: within reach of the actor, and (if Visibility offers it)
-- on known/explored ground.
function G.point_ok(char, pos)
    if type(pos) ~= "table" or type(pos[1]) ~= "number" or type(pos[2]) ~= "number"
        or type(pos[3]) ~= "number" then
        G.refuse("bad position")
    end
    local p = G.pos(char)
    if not p then G.refuse("cannot locate character") end
    if U.dist(p, pos) > G.MAX_MOVE_DIST then G.refuse("destination is too far away") end
    local V = BG3AI.Visibility
    if type(V) == "table" and type(V.position_known) == "function" then
        local ok, r = pcall(V.position_known, pos)
        if ok and r == false then G.refuse("destination is not explored") end
    end
end

-- Can `char` walk to `pos`? Uses worker vis's BG3AI.Nav (Navigation.lua):
--   N.reachable(char, pos) -> bool|nil[, reason]      (preferred, if present)
--   N.path(char, pos) -> {reachable=true|false|nil, length_m, needs_jump, error_cause}
--   N.pathLength(char, pos) -> metres | nil(=unreachable)  (last resort)
-- Returns reachable (true/false/nil=unknown), info {length_m?, needs_jump?, reason?}.
-- nil (pathfinding unavailable) never blocks a move.
function G.reachable(char, pos)
    local N = BG3AI.Nav
    if type(N) ~= "table" then return nil, {} end
    if type(N.path) == "function" then
        local ok, r = pcall(N.path, char, pos)
        if ok and type(r) == "table" then
            return r.reachable, { length_m = r.length_m, needs_jump = r.needs_jump, reason = r.error_cause or r.reason }
        end
    end
    if type(N.reachable) == "function" then
        local ok, r, why = pcall(N.reachable, char, pos)
        if ok and (r == true or r == false) then return r, { reason = why } end
    end
    if type(N.pathLength) == "function" then
        local ok, len = pcall(N.pathLength, char, pos)
        if ok then
            if len == nil then return false, {} end
            return true, { length_m = len }
        end
    end
    return nil, {}
end

-- Refuse points nobody could walk to (the "black void" S001 hit).
-- Strict (after S001's teleports): a point move needs a PROVEN path. Unknown
-- (pathfinding unavailable / no Nav) is refused too, because Osiris move tasks
-- teleport the character when no path exists.
function G.point_reachable(char, pos)
    local r, info = G.reachable(char, pos)
    if info.needs_jump then G.refuse("needs a jump: use jump_to") end
    if r ~= true then G.refuse("no walkable path there (may need a jump or another route)") end
    return info
end

-- Floor height at (x, z) closest to y (multi-floor aware), or nil.
function G.floor_at(x, z, y)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, x, z)
    if not ok or type(hs) ~= "table" then return nil end
    local best
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - y) <= 4.0
            and (not best or math.abs(h - y) < math.abs(best - y)) then best = h end
    end
    return best
end

-- Entity targets (move_to, talk, interact, pickup, the click ops): Osiris
-- CharacterMoveTo / Use / Pickup walk with no path check and teleport when no
-- path exists. Instead we walk to a *verified* floor point next to the entity
-- with CharacterMoveToPosition. Candidates are rings around the entity, the
-- side facing the actor first; the first one BG3AI.Nav proves reachable
-- without a jump wins.
G.APPROACH_RADII = { 1.0, 1.5, 2.2 }
G.APPROACH_MAX_TRIES = 16   -- pathfinder queries per resolve (each ~ms)
G.UNVERIFIED_APPROACH_M = 8.0

-- approach_point(char, target, reach) -> plan
--   plan.here = true            already within `reach` of the target (no walk)
--   plan.pos, plan.length_m     walk there (path length in metres)
--   plan.unverified = true      pathfinding unavailable; only allowed for short,
--                               in-sight approaches (the walker's teleport
--                               detector still applies)
--   plan.dist                   straight distance actor -> target now
-- Refuses when no candidate is reachable.
function G.approach_point(char, target, reach)
    local a, t = G.pos(char), G.pos(target)
    if not a or not t then G.refuse("cannot locate target") end
    local d = U.dist(a, t)
    if d <= reach then return { here = true, dist = d, length_m = 0, tpos = t } end
    local base = math.atan(a[3] - t[3], a[1] - t[1]) -- angle from target toward actor
    local cands = {}
    for _, r in ipairs(G.APPROACH_RADII) do
        if r <= reach - 0.2 then
            for i = 0, 7 do
                local ang = base + i * math.pi / 4
                local x, z = t[1] + r * math.cos(ang), t[3] + r * math.sin(ang)
                cands[#cands + 1] = { x, z, (x - a[1]) ^ 2 + (z - a[3]) ^ 2 }
            end
        end
    end
    table.sort(cands, function(p, q) return p[3] < q[3] end)
    local tries, jump_only, known = 0, false, false
    for _, c in ipairs(cands) do
        if tries >= G.APPROACH_MAX_TRIES then break end
        local y = G.floor_at(c[1], c[2], t[2])
        if y then
            tries = tries + 1
            local p = { c[1], y, c[2] }
            local r, info = G.reachable(char, p)
            if r ~= nil then known = true end
            if r == true and not info.needs_jump then
                return { pos = p, length_m = info.length_m or U.dist(a, p), dist = d, tpos = t }
            end
            if r == true and info.needs_jump then jump_only = true end
        end
    end
    if jump_only then G.refuse("needs a jump: use jump_to") end
    if not known and d <= G.UNVERIFIED_APPROACH_M and b(G.q("HasLineOfSight", char, target)) then
        -- No pathfinding answer at all (Nav missing, or nothing floor-like
        -- around a small target): a short walk toward something in plain
        -- sight is what a click would do; the teleport detector still guards.
        return { pos = t, length_m = d, dist = d, unverified = true, tpos = t }
    end
    G.refuse("no walkable path there (may need a jump or another route)")
end

function G.within(char, target, maxd, what)
    local a, t = G.pos(char), G.pos(target)
    if not a or not t then G.refuse("cannot locate " .. (what or "target")) end
    local d = U.dist(a, t)
    if d > maxd then G.refuse((what or "target") .. " is too far away") end
    return d
end

------------------------------------------------------------------------
-- Osiris gateway: the cheat blocklist. Action code calls game-changing
-- Osiris functions ONLY through G.osi(); anything not allowlisted is refused.
------------------------------------------------------------------------

G.ALLOW = {
    CharacterMoveTo = true, CharacterMoveToPosition = true, PurgeOsirisQueue = true,
    Use = true, Pickup = true, ToInventory = true, SendToCampChest = true,
    Equip = true, Unequip = true, UseSpell = true, UseSpellAtPosition = true,
    EndTurn = true, MakePlayerActive = true, AttachToPartyGroup = true,
    DetachFromPartyGroup = true, ForceTurnBasedMode = true,
    TeleportToFromCamp = true,   -- raising the engine's own "camp button" event
    DialogStartRequested = true, -- raising the engine's own "player clicked NPC" event
    RequestPickpocket = true,    -- raising the engine's own "player clicked to pickpocket" event
    OpenCharacterLootUI = true,  -- loot UI on a corpse, only where the story would answer "yes" (see act.loot_corpse)
}

-- Never allowed, whatever the reason (research/03-actions.md §7).
G.BLOCK = {
    TeleportTo = true, TeleportToPosition = true, ApplyStatus = true, RemoveStatus = true,
    RemoveStatusesWithGroup = true, Open = true, Unlock = true, Lock = true,
    AddGold = true, UserAddGold = true, PartyAddGold = true, TemplateAddTo = true,
    AddSpell = true, AddTadpolePower = true, SetFlag = true, ClearFlag = true,
    RequestActiveRoll = true, RequestPassiveRoll = true, ActivateTrade = true,
    StartRespec = true, AddExplorationExperience = true, AddExperience = true,
    SetHitpointsPercentage = true, Resurrect = true, MakePlayer = true,
    SetFaction = true, SetRelation = true, SetRelationTemporaryHostile = true,
    RequestLongRest = true, RequestLongRestConfirmed = true, AutoSave = true,
    QRY_StartDialog_Fixed = true, QRY_StartDialog = true, CharacterMoveToAndTalk = true,
    SetCanJoinCombat = true, RequestGatherAtCamp = true, EndCombat = true, LeaveCombat = true, Die = true,
}

function G.osi(name, ...)
    if G.BLOCK[name] or not G.ALLOW[name] then
        error("guard: Osiris call '" .. tostring(name) .. "' is not permitted", 0)
    end
    if name:sub(1, 3) == "DB_" then error("guard: database writes are not permitted", 0) end
    return Osi[name](...)
end

-- Like G.osi, for calls whose argument count differs between game builds.
-- Tries each argument list in order and only moves on when bg3le rejects the
-- count. Put the engine form first: bg3le routes a count the engine lacks to a
-- story-declared form of that name if one exists (lua_host.cpp), so a short
-- form could run story code instead. Live (bg3le, 2026-10-09): engine forms
-- are UseSpellAtPosition/6, Equip/5, UseSpell/5, Unequip/2.
function G.osi_forms(name, ...)
    local forms = { ... }
    local last
    for _, args in ipairs(forms) do
        local ok, r = pcall(G.osi, name, table.unpack(args, 1, args.n or #args))
        if ok then return r end
        last = r
        if not tostring(r):find("number of IN arguments", 1, true) then break end
    end
    error(last, 0)
end

-- Does a story database have a row whose first column is `uuid`? Reading a
-- DB is only used to choose an execution path, never returned to the agent.
function G.db_has(name, arity, uuid, second)
    local ok, rows = pcall(function()
        local args = {}
        for i = 1, arity do args[i] = nil end
        return Osi[name]:Get(table.unpack(args, 1, arity))
    end)
    if not ok or not rows then return false end
    for _, r in ipairs(rows) do
        if G.same(r[1], uuid) and (second == nil or G.same(r[2], second)) then return true end
    end
    return false
end

-- Engine cast options that must never be set on a cast we originate.
G.FORBIDDEN_CAST_OPTIONS = {
    "IgnoreHasSpell", "IgnoreCastChecks", "IgnoreSpellRolls", "IgnoreTargetChecks", "Forced",
}

-- opts: nil, a list of option names, or a table name->bool.
function G.cast_options_ok(opts)
    if opts == nil then return true end
    for k, v in pairs(opts) do
        local name = (type(k) == "number") and v or (v and k)
        for _, bad in ipairs(G.FORBIDDEN_CAST_OPTIONS) do
            if name == bad then G.refuse("cast option '" .. bad .. "' is not allowed") end
        end
    end
    return true
end

------------------------------------------------------------------------
-- Action resources
------------------------------------------------------------------------

local resource_names = {}  -- resource UUID -> Name (static data), cached
local function resource_name(uuid)
    local key = tostring(uuid)
    local n = resource_names[key]
    if n == nil then
        local ok, d = pcall(Ext.StaticData.Get, key, "ActionResource")
        n = (ok and d and d.Name) or false
        resource_names[key] = n
    end
    return n or nil
end

-- Snapshot: name -> level -> amount (level 0 for unleveled resources).
function G.resources(char)
    local out = {}
    local e = G.entity(char)
    local ar = e and U.comp(e, "ActionResources")
    if not ar then return out end
    pcall(function()
        for uuid, list in pairs(ar.Resources) do
            local name = resource_name(uuid)
            if name then
                out[name] = out[name] or {}
                for _, r in ipairs(list) do
                    out[name][r.Level or 0] = r.Amount
                end
            end
        end
    end)
    return out
end

-- Parse a stats UseCosts string ("ActionPoint:1;SpellSlotsGroup:1:1:2").
-- Returns a list of {name, amount, level}. Spell slot groups become
-- {name="SpellSlotsGroup", amount, level}.
function G.parse_costs(str, upcast)
    local costs = {}
    if type(str) ~= "string" then return costs end
    for part in str:gmatch("[^;]+") do
        local fields = {}
        for f in part:gmatch("[^:]+") do fields[#fields + 1] = f:match("^%s*(.-)%s*$") end
        local name = fields[1]
        if name and name ~= "" then
            if name == "SpellSlotsGroup" or name == "SpellSlot" or name == "WarlockSpellSlot" then
                local amount = tonumber(fields[2]) or 1
                local level = tonumber(fields[4] or fields[3]) or 1
                if upcast and upcast > level then level = upcast end
                costs[#costs + 1] = { name = name, amount = amount, level = level }
            else
                costs[#costs + 1] = { name = name, amount = tonumber(fields[2]) or 1, level = 0 }
            end
        end
    end
    return costs
end

-- Which concrete resource pays for a cost entry, given a snapshot.
-- Returns name, level or nil if nothing can pay.
function G.payer(snap, cost)
    if cost.name == "SpellSlotsGroup" then
        for _, n in ipairs({ "SpellSlot", "WarlockSpellSlot" }) do
            local lv = snap[n]
            if lv then
                if (lv[cost.level] or 0) >= cost.amount then return n, cost.level end
                if n == "WarlockSpellSlot" then
                    -- pact slots are all of one level, at least the spell's level
                    for l, amt in pairs(lv) do
                        if l >= cost.level and amt >= cost.amount then return n, l end
                    end
                end
            end
        end
        return nil
    end
    local lv = snap[cost.name]
    if lv and (lv[cost.level] or lv[0] or 0) >= cost.amount then
        return cost.name, (lv[cost.level] and cost.level or 0)
    end
    return nil
end

local FRIENDLY = {
    ActionPoint = "action", BonusActionPoint = "bonus action", ReactionActionPoint = "reaction",
    Movement = "movement", SpellSlotsGroup = "spell slot", SpellSlot = "spell slot",
    WarlockSpellSlot = "pact slot", KiPoint = "ki point", SorceryPoint = "sorcery point",
    Rage = "rage charge", BardicInspiration = "Bardic Inspiration", ChannelDivinity = "Channel Divinity",
    SuperiorityDie = "superiority die", ArcaneRecoveryPoint = "Arcane Recovery", WildShape = "Wild Shape charge",
}
function G.friendly(name) return FRIENDLY[name] or name end

-- Writes a deduction to the ActionResources component (used only to make an
-- Osiris-originated cast pay what the UI cast would have paid).
function G.deduct(char, name, level, amount)
    local e = G.entity(char)
    local ar = e and U.comp(e, "ActionResources")
    if not ar then return false end
    local done = false
    pcall(function()
        for uuid, list in pairs(ar.Resources) do
            if resource_name(uuid) == name then
                for _, r in ipairs(list) do
                    if (r.Level or 0) == (level or 0) and not done then
                        r.Amount = math.max(0, r.Amount - amount)
                        done = true
                    end
                end
            end
        end
        if done then e:Replicate("ActionResources") end
    end)
    return done
end

------------------------------------------------------------------------
-- Spell validation (for the guarded Osiris cast path)
------------------------------------------------------------------------

local function spell_entries(char)
    local e = G.entity(char)
    local sb = e and U.comp(e, "SpellBook")
    local out = {}
    if not sb then return out end
    pcall(function()
        for _, s in ipairs(sb.Spells) do
            out[#out + 1] = s
        end
    end)
    return out, e
end

-- Is the spell on the character's hotbar/spellbook and usable (prepared if it
-- needs preparing)? Returns the matching SpellBook entry or refuses.
function G.known_spell(char, spell)
    local entries, e = spell_entries(char)
    local function find(name)
        for _, s in ipairs(entries) do
            local ok, proto = pcall(function() return s.Id.Prototype end)
            if ok and proto == name then return s end
        end
    end
    local found = find(spell)
    if not found then
        -- A variant of a container spell (Command -> Halt): the spellbook holds
        -- the container; prepared/cooldown checks apply to the container.
        local st = Ext.Stats.Get(spell)
        local parent = st and st.SpellContainerID
        if parent and parent ~= "" then
            found = find(parent)
            if found then spell = parent end
        end
    end
    if not found then G.refuse("the character does not have that action") end

    -- Prepared check: the SpellBookPrepares component lists what is prepared.
    -- Entries that are always prepared (cantrips, class actions, item spells)
    -- appear there too, so a missing entry means "not prepared".
    local prep = e and U.comp(e, "SpellBookPrepares")
    if prep then
        local ok, inPrep = pcall(function()
            for _, p in ipairs(prep.PreparedSpells) do
                if p.Prototype == spell or (p.OriginatorPrototype == spell) then return true end
            end
            return false
        end)
        if ok and inPrep == false then
            -- Some granted actions (e.g. Jump, Shove) are not "prepared" spells;
            -- trust the spellbook entry if the spell is not a levelled spell.
            local st = Ext.Stats.Get(spell)
            local level = st and tonumber(st.Level) or 0
            if level > 0 then G.refuse("that spell is not prepared") end
        end
    end

    -- Cooldown (once per turn / short rest / long rest)
    local cds = e and U.comp(e, "SpellBookCooldowns")
    if cds then
        local ok, cooling = pcall(function()
            for _, c in ipairs(cds.Cooldowns) do
                if c.SpellId and c.SpellId.Prototype == spell and (c.Cooldown or 0) > 0 then
                    return true
                end
            end
            return false
        end)
        if ok and cooling then G.refuse("that action is on cooldown") end
    end
    return found
end

-- Range (m) of a weapon slot's equipped weapon from its stats (WeaponRange is
-- in cm: 150 = melee 1.5 m, 1800 = longbow 18 m), or nil.
local function weapon_range(char, slot)
    if not char then return nil end
    local item = G.q("GetEquippedItem", char, slot)
    if not item then return nil end
    local name = G.q("GetStatString", item)
    local st = name and Ext.Stats.Get(name)
    local ok, wr = pcall(function() return st and tonumber(st.WeaponRange) end)
    if ok and wr and wr > 0 then return wr / 100 end
    return nil
end

-- Resolve the range of a spell in metres. Returns range, melee (bool), or
-- nil if unknown. Weapon-range spells (attacks) use the equipped weapon.
G.DEFAULT_RANGED_M = 18.0 -- bows/crossbows when the weapon's stats are unreadable
function G.spell_range(st, char)
    if not st then return nil end
    local r = tonumber(st.TargetRadius) or tonumber(st.Range)
    if r then return r, false end
    local tr = tostring(st.TargetRadius or "")
    if tr:find("Melee") then return weapon_range(char, "Melee Main Weapon") or 1.5, true end
    if tr:find("Ranged") then return weapon_range(char, "Ranged Main Weapon") or G.DEFAULT_RANGED_M, false end
    return nil
end

-- Bounding radius of a character (melee reach is edge to edge, positions are
-- centres). bg3le has no Bound.Bound.Radius; the per-type radii live in
-- Bound.Bound.AIBounds.{Move,Hit,Stand}. Stand is the footprint the engine's
-- AiPath uses for bounds (Ai.inl SetSourceEntity). Live (Nautiloid): medium
-- creatures Stand 0.50 (Move 0.25, Hit 0.20), imps/devourers 0.45-0.49, a hell
-- boar 0.70. Falls back to Move, then a medium creature's 0.5 m.
function G.radius(uuid)
    local e = G.entity(uuid)
    local ok, r = pcall(function()
        local b = e.Bound.Bound.AIBounds
        return (b.Stand and b.Stand.Radius) or (b.Move and b.Move.Radius)
    end)
    if ok and type(r) == "number" and r > 0 and r < 5 then return r end
    return 0.5
end

-- Jump distance (Projectile_Jump has no numeric range, so the guard caps it).
-- Its stats TargetRadius is 4.5 (live, bg3le). BG3: about half the Strength
-- score in metres; Enhance Leap triples it, Step of the Wind doubles it.
-- UNVERIFIED formula and status ids: tune here.
G.JUMP_SPELL = "Projectile_Jump"
G.JUMP_MIN_M = 4.0
G.JUMP_SLACK_M = 0.5
G.JUMP_MULTIPLIERS = { ENHANCE_LEAP = 3, STEP_OF_THE_WIND = 2, STEP_OF_THE_WIND_DASH = 2 }
function G.jump_max(char)
    local str = tonumber(G.q("GetAbility", char, "Strength")) or 10
    local m = math.max(G.JUMP_MIN_M, str / 2)
    local mult = 1
    for status, k in pairs(G.JUMP_MULTIPLIERS) do
        if b(G.q("HasActiveStatus", char, status)) and k > mult then mult = k end
    end
    return m * mult
end

-- Full pre-validation for an Osiris-originated cast. Returns
-- {stats, costs, payers} or refuses with the reason the UI would show.
function G.check_cast(char, spell, target, pos, upcast)
    if type(spell) ~= "string" or spell == "" then G.refuse("no action given") end
    local st = Ext.Stats.Get(spell)
    if not st then G.refuse("unknown action") end
    local variants = tostring(st.ContainerSpells or "")
    if variants ~= "" then
        G.refuse("choose a variant: " .. variants:gsub(";", ", "))
    end
    G.known_spell(char, spell)

    local level = tonumber(st.Level) or 0
    if upcast then
        upcast = tonumber(upcast)
        if not upcast or upcast < level or upcast > 9 then G.refuse("bad upcast level") end
        if level == 0 then G.refuse("cantrips cannot be upcast") end
    end

    -- Verbal components while silenced.
    local flags = {}
    pcall(function() for _, f in ipairs(st.SpellFlags) do flags[f] = true end end)
    if flags.HasVerbalComponent and b(G.q("HasActiveStatus", char, "SILENCED")) then
        G.refuse("cannot cast spells with a verbal component while silenced")
    end

    -- Resources.
    local snap = G.resources(char)
    local costs = G.parse_costs(st.UseCosts, upcast)
    local payers = {}
    local in_combat = G.in_combat(char)
    for _, c in ipairs(costs) do
        -- Out of combat, action/bonus/movement points do not limit you.
        local turn_resource = (c.name == "ActionPoint" or c.name == "BonusActionPoint"
            or c.name == "Movement" or c.name == "ReactionActionPoint")
        if in_combat or not turn_resource then
            local n, l = G.payer(snap, c)
            if not n then G.refuse("not enough " .. G.friendly(c.name)) end
            payers[#payers + 1] = { name = n, level = l, amount = c.amount }
        end
    end

    -- Target checks.
    -- Range is enforced in and out of combat: a cast we originate never walks
    -- (WithoutMove=1 whenever the range is known, see act.cast), because the
    -- Osiris spell task's approach walk has no path check (teleport risk), and
    -- an unchecked WithoutMove cast would hit from any distance.
    local st_type = tostring(st.SpellType or "")
    local range, melee = G.spell_range(st, char)
    if target then
        G.target_visible(target)
        local d = G.within(char, target, G.MAX_INTERACT_DIST, "target")
        local reach = range and (melee and range + G.radius(char) + G.radius(target) or range + 0.5)
        if reach and not G.same(char, target) and d > reach then
            G.refuse(string.format("target is out of range (%.1f m, range %.1f m): move closer first", d, range))
        end
        if st_type ~= "Shout" and not G.same(char, target)
            and not b(G.q("HasLineOfSight", char, target)) then
            G.refuse("no line of sight to target")
        end
    elseif pos then
        G.point_ok(char, pos)
        local p = G.pos(char)
        if spell == G.JUMP_SPELL and p then
            local dx, dz = pos[1] - p[1], pos[3] - p[3]
            local d, maxd = math.sqrt(dx * dx + dz * dz), G.jump_max(char)
            if d > maxd + G.JUMP_SLACK_M then
                G.refuse(string.format("too far to jump (%.1f m, this character can jump %.1f m)", d, maxd))
            end
        end
        -- Projectile_Jump has TargetRadius 4.5 (live), below a strong
        -- character's jump; jump_max above is its cap instead.
        if range and p and spell ~= G.JUMP_SPELL and U.dist(p, pos) > range + 0.5 then
            G.refuse(string.format("target point is out of range (%.1f m, range %.1f m): move closer first",
                U.dist(p, pos), range))
        end
    end
    return { stats = st, costs = costs, payers = payers, snapshot = snap, level = level, range = range }
end

return G
