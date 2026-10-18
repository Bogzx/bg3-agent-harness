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

-- Osiris query wrapper: never throws, returns nil on error. Queries only: Osi[name] runs any call or DB insert
-- (G.q("Unlock", ...) would have unlocked), so a name must read like a query and never be a blocked or allowlisted
-- action (those go through G.osi). Audit ACT-13.
G.QUERY_PREFIXES = { "^Is", "^Has", "^Get", "^Can", "^InSamePartyGroup$", "^DialogIs", "^SpeakerGet", "^CombatGet" }
G.q_denied = G.q_denied or {}
function G.query_ok(name)
    name = tostring(name)
    if name:sub(1, 3) == "DB_" or (G.BLOCK and G.BLOCK[name]) or (G.ALLOW and G.ALLOW[name]) then return false end
    for _, p in ipairs(G.QUERY_PREFIXES) do if name:find(p) then return true end end
    return false
end
function G.q(name, ...)
    if not G.query_ok(name) then
        if not G.q_denied[name] then
            G.q_denied[name] = true
            Ext.Utils.PrintWarning("[BG3AI] guard: G.q('" .. tostring(name) .. "') is not a query; refused")
        end
        return nil
    end
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

-- Is this character held in a conversation the player would see?
-- Signals, strongest first:
--   * ServerCharacter.InDialog: the game's own lock flag (the bridge's metrics
--     and state read it too).
--   * live dialog instances (Ext.Utils.GetDialogManager) that this character
--     is in: DialogState.DialogId's instance, or one listing it in Speakers.
--     An AutomatedDialog instance (bark, party banter) never blocks (live S002).
--     When InDialog is readable and false, world/behaviour dialogs (ambient
--     scenes, combat chatter) don't block either.
--   * SpeakerGetDialog alone is NOT trusted when InDialog reads false. Live
--     S005: Claude's combat_turn was refused "in a conversation" for a whole
--     turn while InDialog was false on every 1 s sample and nothing was on
--     screen (a combat bark with no matching instance is the likely cause).
--     It only counts when neither InDialog nor the manager can be read.
-- DialogState.DialogId keeps the last instance id after the dialogue ends
-- (seen live: 4, 202, 203 with no dialogue running), so it only counts while
-- that instance still exists.
-- Returns blocked, why ("dialog 12 res=..." / "InDialog" / "SpeakerGetDialog").
local function inst_field(inst, k)
    local ok, v = pcall(function() return inst[k] end)
    return ok and v or nil
end

local function speaker_is(sp, g)
    local ok, u = pcall(function() return sp.Uuid.EntityUuid end)
    return ok and type(u) == "string" and u:lower() == g
end

function G.dialogue_info(uuid)
    local g = G.guid(uuid) or uuid
    local e = G.entity(g)
    local flag
    pcall(function()
        local sc = e and U.comp(e, "ServerCharacter")
        if sc then flag = sc.InDialog == true end
    end)
    local dm
    pcall(function() dm = Ext.Utils.GetDialogManager() end)
    local dialogs
    pcall(function() dialogs = dm and dm.Dialogs end)
    local insts = {}
    if dialogs then
        local ds = e and U.comp(e, "DialogState")
        local ok, id = pcall(function() return ds and ds.DialogId end)
        if ok and id ~= nil and id ~= 0 then
            local okd, inst = pcall(function() return dialogs[id] end)
            if okd and inst ~= nil then insts[#insts + 1] = inst end
        end
        pcall(function()
            for _, inst in pairs(dialogs) do
                local sps = inst_field(inst, "Speakers")
                if sps then
                    for _, sp in pairs(sps) do
                        if speaker_is(sp, g) then insts[#insts + 1] = inst; break end
                    end
                end
            end
        end)
    end
    local live = #insts > 0
    for _, inst in ipairs(insts) do
        local ambient = inst_field(inst, "AutomatedDialog") == true
        if flag == false and not ambient then
            ambient = inst_field(inst, "IsWorld") == true or inst_field(inst, "IsBehaviour") == true
        end
        if not ambient then
            return true, ("dialog %s res=%s"):format(tostring(inst_field(inst, "DialogId")),
                tostring(inst_field(inst, "DialogResourceUUID")))
        end
    end
    if flag == true then return true, "InDialog" end
    if live or flag == false or dialogs ~= nil then return false end
    if G.q("SpeakerGetDialog", g, 1) ~= nil then return true, "SpeakerGetDialog" end
    return false
end

function G.in_dialogue(uuid)
    return (G.dialogue_info(uuid))
end

-- Is it this character's turn? Uses the replicated TurnBased component.
-- Party members with the same initiative share one turn group: all of them
-- have IsActiveCombatTurn until every member has ended. A member that ended
-- (Osiris EndTurn / the End Turn button) only gets RequestedEndTurn = true
-- and the game hands control to the next member (live, Nautiloid: Claude +
-- Lae'zel at initiative 4), so it may not act again this round.
-- Ended, as far as we know: in a 3-member group (live S002) Osiris EndTurn on a
-- member did NOT set RequestedEndTurn until the last member ended; then the
-- requests applied one by one, ~2 s apart. act.end_turn records its request
-- here so that member counts as ended (no acting again, no 10 s wait). Cleared
-- on TurnEnded (Actions dispatch), out of combat, or after END_REQUEST_TTL_MS.
G.end_requested = G.end_requested or {}
G.END_REQUEST_TTL_MS = 120000
-- A pending end that looks dropped (end_pending): g -> {req = its end_requested time, since = first seen so}
G.end_alone = G.end_alone or {}
-- S087 05:57:40-05:57:58: 18 s from the group's last end (Claude's) to the game's TurnEnded for all three (e4, e1, e2);
-- S084 04:36:53 >= 16 s. 15 s forgot the earlier members' ends before the game was done.
G.END_DROP_MS = 30000
-- An end the game itself showed (our end_turn finished because its turn flags changed, not on our say-so): g -> the
-- end_requested stamp it confirmed. Never dropped as "alone" (the S052 rule is for requests the game never took).
G.end_confirmed = G.end_confirmed or {}
-- vis V17: the os.clock() fallback this had would itself raise in the game (bg3le has no os); the same clock as
-- RPC.now_ms, and an error from it surfaces as the caller's (fail closed)
local function now_ms()
    return Ext.Utils.MonotonicTime()
end
G.now_ms = now_ms

-- A25 (S088 High Hall Courtyard: 22 empty combat_turn/end_turn calls, ~205 s): whose turn it is, from the game's own
-- turn events. Every turn (a shared group's too) begins with Osiris TurnStarted. Once a combatant outside the party
-- (enemy or NPC ally: not in DB_PartyMembers, which holds our summons too) starts a turn in the same combat after a
-- member's own TurnStarted, that member's turn is over, whatever its TurnBased flags show while the game applies a
-- group's ends (S084/S087/S088: an ended member flagged open again). Until its next TurnStarted. No record (a save
-- loaded mid-turn, an unknown combat): the flags decide, as before.
G.turn_start = G.turn_start or {} -- party g -> { t = its own TurnStarted (ms), combat = combat guid }
G.foe_start = G.foe_start or {}   -- combat guid -> the latest TurnStarted (ms) of a combatant outside the party
G.TURN_BURST_MS = 1000            -- starts this close together are one turn (never "after")
-- Fail-safe: a member's own TurnStarted missed (never seen live; turn_over relies on it too) must not hold its next
-- turn shut for good: this long after the other side's start, the flags decide again.
G.TURN_MOVED_TTL_MS = 60000

function G.note_turn_started(char)
    local g = G.guid(char)
    if not g then return end
    local c = G.q("CombatGetGuidFor", g)
    if not c then return end
    if G.is_party(g) then G.turn_start[g] = { t = now_ms(), combat = c } else G.foe_start[c] = now_ms() end
end

function G.turn_moved_on(uuid)
    local g = G.guid(uuid) or uuid
    local s = g and G.turn_start[g]
    local f = s and G.foe_start[s.combat]
    return f ~= nil and f - s.t > G.TURN_BURST_MS and now_ms() - f < G.TURN_MOVED_TTL_MS
end

-- The game's own turn flags, without our pending bookkeeping.
local function tb_flags(uuid)
    local e = G.entity(uuid)
    local tb = e and U.comp(e, "TurnBased")
    if not tb then return nil end
    local ok, active, req = pcall(function() return tb.IsActiveCombatTurn == true, tb.RequestedEndTurn == true end)
    if not ok then return nil end
    return active, req
end
G.tb_flags = tb_flags

-- Live window 2026-10-16 (combat, S027-start): the group's TurnEnded had fired (Karlach + Claude), yet for a moment
-- the game still showed Claude's turn open (IsActiveCombatTurn, no RequestedEndTurn) and end_requested was already
-- cleared, so a 9 m move passed the guard out of turn (the next round started with 0.4 m). After TurnEnded a turn is
-- over until that character's own TurnStarted, or until the game shows it inactive, or TURN_OVER_TTL_MS.
G.turn_over = G.turn_over or {}
G.TURN_OVER_TTL_MS = 90000
function G.turn_is_over(uuid)
    local g = G.guid(uuid) or uuid
    local t = g and G.turn_over[g]
    if not t then return false end
    local active = G.tb_flags(g)
    if not active or now_ms() - t > G.TURN_OVER_TTL_MS or not G.in_combat(g) then
        G.turn_over[g] = nil
        return false
    end
    return true
end

function G.end_pending(uuid)
    local g = G.guid(uuid) or uuid
    if g and G.turn_is_over(g) then return true end
    local t = g and G.end_requested[g]
    if not t then return false end
    if now_ms() - t > G.END_REQUEST_TTL_MS or not G.in_combat(g) then
        G.end_requested[g] = nil
        return false
    end
    -- S052-S055 "stuck shared turn" (Titan fight every round; 11 save+reloads): an end requested inside a turn group
    -- is only ours to remember while the rest of the group still acts. When nobody else in the group is acting and
    -- the game still shows this turn open (active, no RequestedEndTurn), the game dropped the request: forget it, or
    -- the guard refuses both acting ("not this character's turn") and ending ("already ended") forever.
    local active, req = tb_flags(g)
    if active and not req then
        local others = false
        for _, m in ipairs(G.party()) do
            if not G.same(m, g) and not G.end_requested[G.guid(m) or m] then
                local a2, r2 = tb_flags(m)
                if a2 and not r2 then others = true break end
            end
        end
        -- S084 (7x in one fight): this is also how a group's end looks while the game applies the members' requests
        -- one by one (~2 s apart, the group's end-of-turn work up to ~10 s after the last member's EndTurn): forgetting
        -- then showed an ended member as acting and the next combat_turn was refused. Forget only when it lasts.
        if not others then
            if G.end_confirmed[g] == t then return true end
            local a = G.end_alone[g]
            if not a or a.req ~= t then
                G.end_alone[g] = { req = t, since = now_ms() }
                return true
            end
            if now_ms() - a.since < G.END_DROP_MS then return true end
            G.end_requested[g] = nil
            G.end_alone[g] = nil
            return false
        end
    end
    G.end_alone[g] = nil
    return true
end

function G.is_my_turn(uuid)
    local e = G.entity(uuid)
    local tb = e and U.comp(e, "TurnBased")
    if not tb then return false end
    local ok, v = pcall(function() return tb.IsActiveCombatTurn == true and tb.RequestedEndTurn ~= true end)
    return ok and v == true and not G.end_pending(uuid) and not G.turn_moved_on(uuid)
end

-- Has this character ended its turn while its turn group is still active?
function G.turn_requested_end(uuid)
    if G.turn_moved_on(uuid) then return false end -- the turn passed on: not its turn at all
    local e = G.entity(uuid)
    local tb = e and U.comp(e, "TurnBased")
    local ok, v = pcall(function() return tb and tb.IsActiveCombatTurn == true end)
    if not (ok and v == true) then return false end
    local okr, r = pcall(function() return tb.RequestedEndTurn == true end)
    return (okr and r == true) or G.end_pending(uuid)
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
    local blocked, why = G.dialogue_info(char)
    if blocked then G.refuse("character is in a conversation (" .. tostring(why) .. ")") end
end

------------------------------------------------------------------------
-- Visibility. Worker "vis" provides BG3AI.Visibility; we accept any of
-- these functions (first found wins):
--   V.is_visible(uuid) -> bool        (preferred)
--   V.IsVisible(uuid) -> bool
--   V.CharacterVisible(party, uuid) / V.ItemVisible(party, uuid)
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
                    -- itemKnown: also a static thing seen moments ago on this level (Visibility.lua)
                    return ((V.itemKnown or V.itemVisible)(ctx, g, e)) == true
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

-- Out of sight but seen before (S026 09:02: talk_to Mattis, last seen 669 s
-- earlier 40 m away: refused, then get_nearby, then a walk by hand): say where
-- the party last saw it, so the next call is the walk there. "" otherwise.
function G.last_seen_hint(target)
    local V = BG3AI.Visibility
    if type(V) ~= "table" or type(V.lastSeen) ~= "function" then return "" end
    local okv, gone, mv = pcall(V.vanished or function() return false end, G.guid(target))
    if okv and gone and type(mv) == "table" and type(mv.pos) == "table" then
        return string.format("; it turned invisible %ds ago (last seen @(%.1f,%.1f,%.1f)): reveal it (See Invisibility, "
            .. "Faerie Fire, or an area attack on that spot) or wait for it to show itself when it attacks", mv.ago_s or 0,
            mv.pos[1], mv.pos[2], mv.pos[3])
    end
    local ok, m = pcall(V.lastSeen, G.guid(target))
    if not ok or type(m) ~= "table" or type(m.pos) ~= "table" then return "" end
    return string.format("; last seen @(%.1f,%.1f,%.1f) %ds ago: travel_to that spot first",
        m.pos[1], m.pos[2], m.pos[3], m.ago_s or 0)
end

function G.target_visible(target)
    if not G.visible(target) then G.refuse("target is not visible" .. G.last_seen_hint(target)) end
end

-- Point targets: within reach of the actor, and on ground the party has seen
-- or walked (Navigation.lua N.known), as a click needs ground on screen.
function G.point_ok(char, pos)
    if type(pos) ~= "table" or type(pos[1]) ~= "number" or type(pos[2]) ~= "number"
        or type(pos[3]) ~= "number" then
        G.refuse("bad position")
    end
    local p = G.pos(char)
    if not p then G.refuse("cannot locate character") end
    local dd = U.dist(p, pos)
    if dd > G.MAX_MOVE_DIST then
        G.refuse(string.format("destination is too far away (%.0fm from this character; one walk reaches %.0fm: use travel_to)",
            dd, G.MAX_MOVE_DIST))
    end
    local N = BG3AI.Nav
    if type(N) == "table" and type(N.known) == "function" then
        local ok, r = pcall(N.known, nil, pos)
        if not (ok and r == true) then
            G.refuse("that spot is not in view and the party hasn't walked there: pick ground you can see")
        end
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
            local info = { length_m = r.length_m, needs_jump = r.needs_jump, reason = r.error_cause or r.reason,
                drop = r.drop, budget = r.budget }
            -- A found path that ends short of the point (N.path's partial: end_off > 2 m) does not reach it (live L3
            -- helm probe 2026-10-17: a ring point on the floor above a ladder's foot read "reachable 1.8 m"; the
            -- engine then walked there up the ladder, and the interact's own Use climbed back down). N.verifyPoint
            -- already counts it so.
            if r.reachable and r.partial then
                info.partial, info.end_off, info.end_pos = true, r.end_off, r.end_pos
                return false, info
            end
            return r.reachable, info
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
-- A route with a jump/climb link is fine out of combat (a click auto-jumps it
-- too, and the teleport detector accepts steps along the route); in combat
-- the jump is its own action, so it stays refused there.
-- The concrete way on after a "no walkable path" (S021: 6 such refusals left
-- the agent guessing for 2-5 model turns): the reachable spot nearest the
-- goal (BG3AI.Nav.closestReach: rings around the goal, else the farthest
-- walk toward it). "" when there is none or Nav is missing.
-- No walkable spot toward the goal at all: maybe the walker is wedged in a nook (Navigation.lua N.wedged, S058 08:00).
-- goal (optional): no jump next to a member on a floor farther from the goal (naut2 run 3: back down the ladder just
-- climbed); that case says so instead.
function G.wedged_hint(char, goal)
    local N = BG3AI.Nav
    if type(N) ~= "table" or type(N.wedged) ~= "function" then return "" end
    local ok, w = pcall(N.wedged, char, nil, goal)
    if not ok or type(w) ~= "table" then return "" end
    if not w.to and w.away then
        return string.format("; no walkable ground reads around you, and the only landing (next to %s) is %.1fm %s,"
            .. " away from the goal: don't jump there; try again in a moment (right after a climb the ground can read"
            .. " blocked), or route", tostring(w.away), math.abs(w.away_dy or 0), (w.away_dy or 0) < 0 and "below" or "above")
    end
    if not w.to then return "" end -- nobody to land next to: maybe a small balcony whose way out is its ladder
    return string.format("; you are wedged: no walkable ground leads out from where you stand: jump_to (%.1f,%.1f,%.1f)"
        .. " next to %s (%.1fm)%s, then try again", w.to[1], w.to[2], w.to[3], tostring(w.near), w.dist_m or 0,
        w.in_reach == false and " if it is in reach" or "")
end

-- A ladder pulled up out of reach, used from below (Navigation.lua N.ladderRaised, S059 08:56): say so instead of
-- "closest walkable spot … then a ladder/jump_to", which sent the agent under it and into Dimension Door tries.
function G.raised_ladder_refuse(target, from)
    local N, V = BG3AI.Nav, BG3AI.Visibility
    if type(N) ~= "table" or type(N.ladderRaised) ~= "function" or type(V) ~= "table" or not V.itemKind then return end
    local okk, kind = pcall(V.itemKind, target, G.entity(target))
    if not okk or kind ~= "ladder" then return end
    local p = G.pos(target)
    if not p or not from or from[2] > p[2] + 1.0 then return end -- on top of it: not this case
    local okr, gap = pcall(N.ladderRaised, p)
    if not okr or type(gap) ~= "number" then return end
    G.refuse(string.format("this ladder is pulled up: its foot hangs %.1fm above the floor, out of reach from below"
        .. " (a player can't climb it from here; it is lowered from above, often by a lever up there): find another"
        .. " way up (route connectors=true, get_frontier)", gap))
end

function G.closest_hint(char, goal)
    local N = BG3AI.Nav
    if type(N) ~= "table" or type(N.closestReach) ~= "function" or not goal then return "" end
    local ok, c = pcall(N.closestReach, char, goal)
    if not ok or type(c) ~= "table" or not c.pos then return G.wedged_hint(char, goal) end
    local up = ""
    if type(c.dy_to_goal) == "number" and math.abs(c.dy_to_goal) >= 1.5 then
        up = string.format(", it is %.1fm %s that spot", math.abs(c.dy_to_goal), c.dy_to_goal > 0 and "above" or "below")
    end
    return string.format("; closest walkable spot (%.1f,%.1f,%.1f) is %.1fm from it (walk %.0fm%s): move_to that, then a ladder/jump_to",
        c.pos[1], c.pos[2], c.pos[3], c.to_goal_m or 0, c.path_m or 0, up)
end

function G.point_reachable(char, pos)
    -- Over the Faith-Leap chasm at the walkway's height (Navigation.lua N.helperZone): nav reads no floor there, so
    -- no path is proven or refused; the game decides (a misstep: teleported back, or the fall). User 2026-10-18.
    local N = BG3AI.Nav
    if type(N) == "table" and type(N.helperZone) == "function" then
        local okz, zone = pcall(N.helperZone, pos)
        if okz and zone then return {} end
    end
    local r, info = G.reachable(char, pos)
    if info.needs_jump and G.in_combat(char) then G.refuse("needs a jump: use jump_to") end
    if r == true and info.drop then
        -- Navigation.lua N.path: a walk never takes a fall that hurts (S035 Faith-Leap death)
        local d = info.drop.pos or {}
        G.refuse(string.format("the way there drops %.1fm at (%.1f,%.1f,%.1f) (fall damage): find a ladder or stairs"
            .. " (route), or jump_to the landing on purpose", info.drop.m or 0, d[1] or 0, d[2] or 0, d[3] or 0))
    end
    if r ~= true and info.partial then
        local e = info.end_pos or {}
        G.refuse(string.format("no walkable path there: the walkable ground ends %.1fm short of it, at (%.1f,%.1f,%.1f)"
            .. " (the spot is off the walkable ground, or on another floor: a ladder/jump_to)", info.end_off or 0,
            e[1] or 0, e[2] or 0, e[3] or 0))
    end
    if r ~= true then
        G.refuse("no walkable path there (may need a jump or another route)" .. G.closest_hint(char, pos))
    end
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
G.RIM_SLACK_M = 0.5         -- a big target used from its rim: reach + bounds radius + this
G.RIM_DY_M = 2.5            -- ... with the rim spot at most this far above/below the target's position

-- approach_point(char, target, reach) -> plan
--   plan.here = true            already within `reach` of the target (no walk)
--   plan.pos, plan.length_m     walk there (path length in metres)
--   plan.unverified = true      pathfinding unavailable; only allowed for short,
--                               in-sight approaches (the walker's teleport
--                               detector still applies)
--   plan.dist                   straight distance actor -> target now
-- Refuses when no candidate is reachable.
-- A ladder seen from above is approached at its top end (BG3AI.Nav.ladderTop;
-- its position is its foot, S025 08:03). The top landing can sit a few metres
-- off the foot's x/z, so the rings reach out to 3 m there (the walker uses a
-- target that did not move from the verified point, Actions walk_to_plan).
G.LADDER_TOP_RADII = { 1.0, 1.5, 2.2, 3.0 }

local function ladder_top(a, target)
    local N = BG3AI.Nav
    if type(N) ~= "table" or type(N.ladderTop) ~= "function" then return nil end
    local ok, top = pcall(N.ladderTop, a, target)
    return ok and type(top) == "table" and top or nil
end

-- A ladder is used from the floor of the end it is approached at: a ring point on the other floor (the landing above
-- its foot) is a walk the engine makes up the ladder itself (live L3 helm probe 2026-10-17).
G.LADDER_FLOOR_DY = 1.5

function G.approach_point(char, target, reach)
    local a, tpos = G.pos(char), G.pos(target)
    if not a or not tpos then G.refuse("cannot locate target") end
    local top = ladder_top(a, target)
    local t = top or tpos -- where it is used from
    local is_ladder = false
    pcall(function() is_ladder = G.entity(target).ServerItem.IsLadder or false end)
    local d = U.dist(a, t)
    if d <= reach then return { here = true, dist = d, length_m = 0, tpos = tpos } end
    local base = math.atan(a[3] - t[3], a[1] - t[1]) -- angle from target toward actor
    local cands = {}
    for _, r in ipairs(top and G.LADDER_TOP_RADII or G.APPROACH_RADII) do
        if top or r <= reach - 0.2 then
            for i = 0, 7 do
                local ang = base + i * math.pi / 4
                local x, z = t[1] + r * math.cos(ang), t[3] + r * math.sin(ang)
                cands[#cands + 1] = { x, z, (x - a[1]) ^ 2 + (z - a[3]) ^ 2 }
            end
        end
    end
    table.sort(cands, function(p, q) return p[3] < q[3] end)
    local tries, jump_only, known, busy = 0, false, false, false
    for _, c in ipairs(cands) do
        if tries >= G.APPROACH_MAX_TRIES then break end
        local y = G.floor_at(c[1], c[2], t[2])
        if y and is_ladder and math.abs(y - t[2]) >= G.LADDER_FLOOR_DY then y = nil end
        if y then
            tries = tries + 1
            local p = { c[1], y, c[2] }
            local r, info = G.reachable(char, p)
            if r ~= nil then known = true end
            if r == nil and info and info.budget then busy = true end
            if r == true and not info.needs_jump then
                return { pos = p, length_m = info.length_m or U.dist(a, p), dist = d, tpos = tpos,
                    use_y = is_ladder and t[2] or nil }
            end
            if r == true and info.needs_jump then jump_only = true end
        end
    end
    -- A big target (a hole, a wide hatch) is used from its rim: the rings within `reach` of its centre all lie over
    -- it (S044 23:50: interact on the Hollow Tower refused "no walkable path … closest walkable spot … 3.0m from it
    -- (walk 1m)"; a click on it worked at once). The closest walkable spot counts when it is within reach of the
    -- object's own edge (AI bounds radius) on about the same floor.
    if not top and not jump_only then
        local N, V = BG3AI.Nav, BG3AI.Visibility
        local rad = 0
        if type(V) == "table" and V.bounds then
            local okb, bb = pcall(V.bounds, G.entity(target))
            if okb and type(bb) == "table" and type(bb.radius) == "number" then rad = bb.radius end
        end
        if rad > 0 and type(N) == "table" and type(N.closestReach) == "function" then
            local okc, c = pcall(N.closestReach, char, t)
            if okc and type(c) == "table" and c.pos and (c.to_goal_m or 1e9) <= reach + rad + G.RIM_SLACK_M
                and math.abs(c.dy_to_goal or 0) <= G.RIM_DY_M then
                return { pos = c.pos, length_m = c.path_m or U.dist(a, c.pos), dist = d, tpos = tpos, rim = true }
            end
        end
    end
    if jump_only then G.refuse("needs a jump: use jump_to") end
    G.raised_ladder_refuse(target, a)
    -- NAV-8: "no answer" because this frame's path budget was spent is not "no pathfinding": no walk without proof
    if not known and busy then G.refuse("path searches are busy this moment: send it again") end
    if not known and d <= G.UNVERIFIED_APPROACH_M and b(G.q("HasLineOfSight", char, target)) then
        -- No pathfinding answer at all (Nav missing, or nothing floor-like
        -- around a small target): a short walk toward something in plain
        -- sight is what a click would do; the teleport detector still guards.
        return { pos = t, length_m = d, dist = d, unverified = true, tpos = tpos }
    end
    G.refuse("no walkable path there (may need a jump or another route)" .. G.closest_hint(char, t))
end

function G.within(char, target, maxd, what)
    local a, t = G.pos(char), G.pos(target)
    if not a or not t then G.refuse("cannot locate " .. (what or "target")) end
    local d = U.dist(a, t)
    if d > maxd then
        -- S028 15:53: "40m from this character, max 40m" read as a contradiction; one decimal when it rounds to the max
        local fmt = (math.floor(d + 0.5) <= maxd) and "%s is too far away (%.1fm from this character, max %.0fm)"
            or "%s is too far away (%.0fm from this character, max %.0fm)"
        G.refuse(string.format(fmt, what or "target", d, maxd))
    end
    return d
end

------------------------------------------------------------------------
-- Osiris gateway: the cheat blocklist. Action code calls game-changing
-- Osiris functions ONLY through G.osi(); anything not allowlisted is refused.
------------------------------------------------------------------------

G.ALLOW = {
    CharacterMoveToPosition = true, PurgeOsirisQueue = true,
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
    CharacterMoveTo = true, -- the target-entity form walks with no path check and teleports when no path (ACT-13)
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

------------------------------------------------------------------------
-- Keys. A locked door/chest carries eoc::lock::LockComponent.Key_M; a key
-- item carries eoc::lock::KeyComponent.Key with the same name. In the game,
-- clicking a locked door with its key in your bags opens it (the engine raises
-- Unlocked(item, char, key)). The player knows their own inventory, so the
-- match is fair; the key name itself is never returned to the agent.
------------------------------------------------------------------------

function G.lock_key(item)
    local k
    pcall(function() k = G.entity(item).Lock.Key_M end)
    k = k and tostring(k) or nil
    if k == "" or k == "None" then return nil end
    return k
end

local function item_key(ie)
    local k
    pcall(function() k = ie.Key.Key end)
    k = k and tostring(k) or nil
    if k == "" or k == "None" then return nil end
    return k
end

local function find_in_inventory(inv, want, depth)
    local found
    pcall(function()
        for _, it in pairs(inv.InventoryContainer.Items) do
            local ie = it.Item
            if item_key(ie) == want then
                found = ie.Uuid.EntityUuid
                return
            end
            if depth > 0 then
                local io
                pcall(function() io = ie.InventoryOwner end)
                if io and io.PrimaryInventory then
                    found = find_in_inventory(io.PrimaryInventory, want, depth - 1)
                    if found then return end
                end
            end
        end
    end)
    return found
end

-- Uuid of the key in `char`'s bags (keychain and other bags included) that opens `item`, or nil.
function G.key_in(char, item)
    local want = G.lock_key(item)
    if not want then return nil end
    local inv
    pcall(function() inv = G.entity(char).InventoryOwner.PrimaryInventory end)
    if not inv then return nil end
    return G.guid(find_in_inventory(inv, want, 3))
end

-- The party member carrying `item`'s key, actor first: holder, key (or nil).
function G.party_key(item, actor)
    if not G.lock_key(item) then return nil end
    local order = { actor }
    for _, p in ipairs(G.party()) do
        if not G.same(p, actor) then order[#order + 1] = p end
    end
    for _, p in ipairs(order) do
        local k = p and G.key_in(p, item)
        if k then return p, k end
    end
    return nil
end

-- The one sanctioned Unlock: `char` stands at `item` with its key in their bags,
-- which is exactly when the game unlocks it on a click. Rechecks everything.
function G.unlock_with_key(item, char, reach)
    local k = G.key_in(char, item)
    if not k then error("guard: no matching key carried; Unlock is not permitted", 0) end
    local a, t = G.pos(char), G.pos(item)
    if not a or not t or U.dist(a, t) > (reach or 2.5) then
        error("guard: not next to the lock; Unlock is not permitted", 0)
    end
    Osi.Unlock(item, char)
    return k
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

-- Prepared spells (A4, 2026-10-17). A prepared caster's SpellBook holds its whole class list: S065's action list
-- showed Shadowheart every level-1 cleric spell. SpellBookPrepares.PreparedSpells (bg3se SpellMetaId:
-- OriginatorPrototype, SourceType, no Prototype) names the prepared ones, and each SpellBook entry says whether it
-- needs preparing (PrepareType AlwaysPrepared / RequiresPreparation / Unknown). The old check read p.Prototype
-- first: bg3se raises "Property does not exist" there, the pcall swallowed it, and every class spell was castable
-- (Claude's Bless in S065, with no Bless on his hotbar).
-- The set of prepared OriginatorPrototypes, or false when the component can't be read.
function G.prepared_ids(e)
    local prep = e and U.comp(e, "SpellBookPrepares")
    if not prep then return false end
    local set = {}
    local ok = pcall(function()
        for _, p in ipairs(prep.PreparedSpells) do set[tostring(p.OriginatorPrototype)] = true end
    end)
    return ok and set
end

-- The spell a SpellBook entry stands for: its OriginatorPrototype (an upcast entry "Target_Bless_2" has Prototype
-- Target_Bless_2 and OriginatorPrototype Target_Bless; live L4: Claude's book holds level-1 spells x3, level-2 x2),
-- else its Prototype. nil when neither reads.
function G.entry_id(entry)
    local ok, id = pcall(function() return entry.Id.OriginatorPrototype end)
    if not ok or id == nil or tostring(id) == "" then ok, id = pcall(function() return entry.Id.Prototype end) end
    return ok and id ~= nil and tostring(id) or nil
end

-- The container a spell is a variant of (Target_Command_Halt -> Target_Command_Container), else nil. Live L4: each
-- variant sits in the book as its own RequiresPreparation entry, while SpellBookPrepares lists only the container.
function G.container_of(id)
    local f = G.spell_facts(id)
    return f and f.box or nil
end

-- A SpellBook entry that needs preparing and isn't prepared (prepared = G.prepared_ids). A container's variant
-- counts as prepared with its container. An unreadable prepared list counts as nothing prepared: refuse rather than
-- cast a spell the player could not.
function G.unprepared(entry, prepared)
    local ok, kind = pcall(function() return tostring(entry.PrepareType) end)
    if not ok or kind ~= "RequiresPreparation" then return false end
    if not prepared then return true end
    local id = G.entry_id(entry)
    if id == nil then return true end
    if prepared[id] then return false end
    local box = G.container_of(id)
    return not (box and prepared[box])
end

-- Spell preparation (A5, 2026-10-17). What the spellbook's prepare page shows: the class spells that need preparing
-- (prepared or not), the always-prepared ones (domain/oath) and the limit. One row per spell (G.entry_id: upcast
-- entries merge; a container's variants are not rows). The limit (A7, live L4): Stats.MaxPreparedSpells read 9 for
-- Claude while his spellbook page said PreparedSpellsStat 12/12 with 12 prepared. The page's number is per class
-- book; the server keeps per-class counts in SpellBookPrepares.ClassPreparedSpellCount (bg3se field_30,
-- HashMap<class Guid, int>), so that sum is the limit, MaxPreparedSpells only when it doesn't read (GUESS P1:
-- the map holds the page's Max; acceptance act "prepare_spells limit = the page's (A7, read-only)" checks it).

-- What the spellbook page's cell shows for a spell (Client/Spellbook.lua matches cells by it: live L4, a cell has no
-- Name and its ActionId reads "Sentinel"): the stats Icon, level, school, concentration and the main cost as the VM
-- spells it (MainCost Action / BonusAction / BothActions).
local function cell_look(st)
    local look = {}
    local function get(k)
        local ok, v = pcall(function() return st[k] end)
        return ok and v ~= nil and v or nil
    end
    look.icon = get("Icon") and tostring(get("Icon")) or nil
    look.school = get("SpellSchool") and tostring(get("SpellSchool")) or nil
    local flags = get("SpellFlags")
    local conc = false
    if type(flags) == "table" then
        for _, f in ipairs(flags) do if f == "IsConcentration" then conc = true end end
    elseif flags then
        conc = tostring(flags):find("IsConcentration", 1, true) ~= nil
    end
    look.conc = conc
    local costs = tostring(get("UseCosts") or "")
    local act = costs:find("^ActionPoint:") or costs:find("[;%s]ActionPoint:")
    local bonus = costs:find("BonusActionPoint:", 1, true)
    look.cost = (act and bonus) and "BothActions" or act and "Action" or bonus and "BonusAction" or nil
    return look
end

-- What prep_info and the cast checks read from a spell's stats entry, per spell id: its container (SpellContainerID),
-- shown name, level and page look (cell_look). nil when the entry doesn't read (not kept). A stats entry doesn't
-- change in a running game, and bg3le's Ext.Stats.Get is Lua (a fresh proxy per call, each attribute a lookup plus a
-- decode): act.prepared rides in every get_status and walked every caster's whole book through ~2 gets and ~8
-- attribute reads per spell each time (V12: act.prepared alone 338 ms median in S068-S070).
G.spellFacts = {}
function G.spell_facts(id)
    local f = G.spellFacts[id]
    if f then return f end
    local ok, st = pcall(Ext.Stats.Get, id)
    if not ok or not st then return nil end
    local okc, c = pcall(function() return st.SpellContainerID end)
    c = okc and c and tostring(c) or ""
    local okn, h = pcall(function() return st.DisplayName end)
    local okl, l = pcall(function() return st.Level end)
    f = { box = c ~= "" and c or nil, name = (okn and U.loca(h)) or id, level = okl and tonumber(l) or nil,
          look = cell_look(st) }
    G.spellFacts[id] = f
    return f
end

local function class_limit(e)
    local prep = e and U.comp(e, "SpellBookPrepares")
    local sum, n = 0, 0
    pcall(function()
        for _, v in pairs(prep.ClassPreparedSpellCount) do
            if tonumber(v) then sum, n = sum + tonumber(v), n + 1 end
        end
    end)
    return n > 0 and sum or nil
end

local function class_names(e)
    local out = {}
    local c = e and U.comp(e, "Classes")
    pcall(function()
        for _, cl in ipairs(c.Classes) do
            local ok, r = pcall(Ext.StaticData.Get, cl.ClassUUID, "ClassDescription")
            local okd, h = pcall(function() return r.DisplayName.Handle.Handle end)
            local n = (okd and U.loca(h)) or (ok and r and r.Name)
            if n then out[#out + 1] = tostring(n) end
        end
    end)
    return out
end
G.class_names = class_names

-- { prepares, read, max, count, prepared, unprepared, always = { {id, name, level} }, classes }
function G.prep_info(char)
    local entries, e = spell_entries(char)
    local set = G.prepared_ids(e)
    local info = { prepared = {}, unprepared = {}, always = {}, classes = class_names(e), read = set ~= false }
    local seen = {}
    for _, s in ipairs(entries) do
        local okk, kind = pcall(function() return tostring(s.PrepareType) end)
        local id = G.entry_id(s)
        if okk and id and not seen[id] and (kind == "RequiresPreparation" or kind == "AlwaysPrepared")
                and not G.container_of(id) then
            seen[id] = true
            local f = G.spell_facts(id)
            local level = f and f.level
            local row = { id = id, name = f and f.name or id, level = level, look = f and f.look }
            if kind == "AlwaysPrepared" then
                if (level or 0) > 0 then info.always[#info.always + 1] = row end
            elseif G.unprepared(s, set) then
                info.unprepared[#info.unprepared + 1] = row
            else
                info.prepared[#info.prepared + 1] = row
            end
        end
    end
    local function order(a, b)
        if (a.level or 0) ~= (b.level or 0) then return (a.level or 0) < (b.level or 0) end
        return a.name < b.name
    end
    table.sort(info.prepared, order)
    table.sort(info.unprepared, order)
    table.sort(info.always, order)
    local okm, max = pcall(function() return e.Stats.MaxPreparedSpells end)
    info.stats_max = okm and tonumber(max) or nil
    info.class_max = class_limit(e)
    info.max = info.class_max or info.stats_max
    info.count = #info.prepared
    info.prepares = #info.prepared + #info.unprepared > 0
    return info
end

-- A spell named by id or shown name (any case) in prep_info: row, "prepared" | "unprepared" | "always".
function G.prep_find(info, want)
    local w = tostring(want or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
    for _, kind in ipairs({ "prepared", "unprepared", "always" }) do
        for _, r in ipairs(info[kind]) do
            if r.id:lower() == w or r.name:lower() == w then return r, kind end
        end
    end
    return nil
end

local function names(rows)
    local out = {}
    for _, r in ipairs(rows) do out[#out + 1] = r.name end
    return table.concat(out, ", ")
end

-- The change the spellbook will make, checked as the UI checks it, or a refusal: in combat, a class that doesn't
-- prepare, a spell not in the book, over the limit (naming what is prepared now).
function G.prep_plan(char, add, remove)
    char = G.actor(char)
    local who = U.loca(G.q("GetDisplayName", char)) or "this character"
    if G.in_combat(char) then
        G.refuse("spells are prepared out of combat (the spellbook locks preparing during a fight); after the fight,"
            .. " or after a long rest")
    end
    G.not_in_dialogue(char)
    local info = G.prep_info(char)
    local cls = table.concat(info.classes, "/")
    if not info.prepares then
        G.refuse(who .. (cls ~= "" and (" (" .. cls .. ")") or "") .. " doesn't prepare spells: sorcerers, warlocks,"
            .. " bards and rangers know a fixed list that changes only at level up (level_up); clerics, druids,"
            .. " paladins and wizards prepare")
    end
    if not info.read then G.refuse("the prepared spell list didn't read; nothing was changed") end
    local adds, removes, notes, taken = {}, {}, {}, {}
    for _, want in ipairs(remove or {}) do
        local r, kind = G.prep_find(info, want)
        if not r then
            G.refuse(tostring(want) .. " is not in " .. who .. "'s spellbook (nothing to unprepare)")
        elseif kind == "always" then
            notes[#notes + 1] = r.name .. " is always prepared (it can't be unprepared)"
        elseif kind == "unprepared" then
            notes[#notes + 1] = r.name .. " was not prepared"
        elseif not taken[r.id] then
            taken[r.id] = true
            removes[#removes + 1] = r
        end
    end
    for _, want in ipairs(add or {}) do
        local r, kind = G.prep_find(info, want)
        if not r then
            G.refuse(who .. " doesn't know " .. tostring(want) .. ": not in their spellbook (only "
                .. (cls ~= "" and cls or "their class's") .. " spells of a level they can cast)")
        elseif kind == "always" then
            notes[#notes + 1] = r.name .. " is always prepared already"
        elseif kind == "prepared" and not taken[r.id] then
            notes[#notes + 1] = r.name .. " is prepared already"
        elseif not taken["+" .. r.id] then
            taken["+" .. r.id] = true
            adds[#adds + 1] = r
        end
    end
    -- How many other book spells the page shows alike (same level, school, concentration, cost and prepared state):
    -- with no readable icon the page can tell a spell's cell apart only when that is 0 (Client/Spellbook.lua).
    local function look_key(r)
        local l = r.look or {}
        return table.concat({ tostring(r.level), tostring(l.school), tostring(l.conc), tostring(l.cost) }, "|")
    end
    local function twins(r, kind)
        local n = 0
        for _, o in ipairs(info[kind]) do
            if o.id ~= r.id and look_key(o) == look_key(r) then n = n + 1 end
        end
        return n
    end
    for _, r in ipairs(adds) do r.twins = twins(r, "unprepared") end
    for _, r in ipairs(removes) do r.twins = twins(r, "prepared") end
    local after = info.count - #removes + #adds
    if info.max and after > info.max and #adds > 0 then
        G.refuse(string.format("over the limit: %s can prepare %d spells and has %d prepared (%s); remove %d of them"
            .. " in the same call (remove=[...])", who, info.max, info.count, names(info.prepared), after - info.max))
    end
    return { char = char, name = who, classes = info.classes, max = info.max, count = info.count, after = after,
             stats_max = info.stats_max, class_max = info.class_max,
             prepared = info.prepared, always = info.always, add = adds, remove = removes, notes = notes }
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
    if not found then
        -- S031 17:04: Gale's Projectile_MainHandAttack "does not have that action": a ranged weapon attack exists
        -- only with a ranged weapon equipped (a melee weapon attack only with a melee one): say which is missing
        if spell == "Projectile_MainHandAttack" or spell == "Projectile_OffhandAttack" then
            G.refuse("the character does not have that action: no ranged weapon equipped (Ranged Attack needs a bow"
                .. " or crossbow in the ranged slot); use a cantrip, Throw, or the melee attack")
        end
        G.refuse("the character does not have that action")
    end

    if G.unprepared(found, G.prepared_ids(e)) then G.refuse("that spell is not prepared") end

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
    local key = G.item_cd_key(char, spell)
    if key and G.item_uses()[key] then
        G.refuse("that action is used up until the next " .. (G.item_uses()[key] == "short" and "short or long rest"
            or "long rest") .. " (its item allows it once per rest)")
    end
    return found
end

-- A27 (S090, the Netherbrain): Gather Your Allies (Shout_END_AllyAbilities_MultipleSummon) is a linked spell
-- container with no ContainerSpells: its ally groups (Target_END_AllyAbilities_WatchSummon …) name it as their
-- SpellContainerID, and only a group's own cast at a ground point summons anyone. UseSpell on the container "cast" it
-- twice and nobody came; the agent picked the group and the ground by hand (31 clicks over 3 tries).
local function spell_flags(st)
    local f = {}
    pcall(function() for _, x in ipairs(st.SpellFlags) do f[x] = true end end)
    return f
end

-- A container with variants but no ContainerSpells list: flagged IsLinkedSpellContainer, in no container itself.
function G.linked_box(st)
    if not st or tostring(st.ContainerSpells or "") ~= "" or tostring(st.SpellContainerID or "") ~= "" then
        return false
    end
    return spell_flags(st).IsLinkedSpellContainer == true
end

-- The variants of `box` char can pick, in book order: ContainerSpells when the stats list them, else char's SpellBook
-- entries whose stats name `box` as their container and its SpellContainer entries filed under it (the field the DD
-- probe read live). A call-allies box with none read falls back to every group in the game data (from = "game data").
function G.box_variants(char, box)
    local st = Ext.Stats.Get(box)
    local out, seen = {}, {}
    local function add(id)
        id = id and tostring(id) or ""
        if id ~= "" and id ~= box and not seen[id] then seen[id] = true; out[#out + 1] = id end
    end
    for id in tostring(st and st.ContainerSpells or ""):gmatch("[^;]+") do add(id) end
    if #out > 0 then return out end
    local e = G.entity(char)
    pcall(function()
        for _, s in ipairs(e.SpellBook.Spells) do
            local id = s.Id.Prototype
            local f = G.spell_facts(id)
            if f and f.box == box then add(id) end
        end
    end)
    pcall(function()
        for _, m in ipairs(e.SpellContainer.Spells) do
            if tostring(m.ContainerSpell) == box then add(m.SpellId.OriginatorPrototype) end
        end
    end)
    if #out == 0 and st and spell_flags(st).CallAlliesSpell then
        local all = G.boxData[box]
        if not all then
            all = {}
            pcall(function()
                for _, id in ipairs(Ext.Stats.GetStats("SpellData")) do
                    local s = Ext.Stats.Get(id)
                    if s and tostring(s.SpellContainerID or "") == box then all[#all + 1] = id end
                end
            end)
            G.boxData[box] = all -- the game data doesn't change in a running game: one walk of every spell
        end
        for _, id in ipairs(all) do add(id) end
        return out, "game data"
    end
    return out
end
G.boxData = {}

-- The statuses a spell's RequirementConditions forbid, when that is all they say ("not HasStatus('X') and not
-- HasStatus('Y')": an ally group's called/consumed marks); nil otherwise.
function G.forbidden_statuses(st)
    local rc = tostring(st and st.RequirementConditions or "")
    local out = {}
    local rest = rc:gsub("not%s+HasStatus%(%s*'([%w_]+)'%s*%)", function(s) out[#out + 1] = s; return "" end)
    if #out == 0 or rest:gsub("and", ""):match("%S") then return nil end
    return out
end

-- The forbidding status char has for `spell` (the group was already called), else nil.
function G.variant_used(char, spell)
    for _, s in ipairs(G.forbidden_statuses(Ext.Stats.Get(spell)) or {}) do
        if b(G.q("HasActiveStatus", char, s)) then return s end
    end
    return nil
end

-- "The Watch, Gur Huntwardens [already called]" for a refusal; the usable ones first. Names only, as the game shows
-- them (S093 fairness leak: the spell and status ids); the id only for a variant the game gives no name.
function G.variant_list(char, ids)
    local free, used = {}, {}
    for _, id in ipairs(ids) do
        local f = G.spell_facts(id)
        local shown = f and f.name or id
        if G.variant_used(char, id) then used[#used + 1] = shown .. " [already called]" else free[#free + 1] = shown end
    end
    for _, x in ipairs(used) do free[#free + 1] = x end
    return table.concat(free, ", ")
end

-- act.cast {which}: the variant of `box` named `which` (its id, its shown name, or a part of the name, any case).
function G.pick_variant(char, box, which)
    local ids, from = G.box_variants(char, box)
    local want = tostring(which):lower():gsub("^%s+", ""):gsub("%s+$", "")
    local exact, part = {}, {}
    for _, id in ipairs(ids) do
        local f = G.spell_facts(id)
        local name = (f and f.name or id):lower()
        if id:lower() == want or name == want then exact[#exact + 1] = id
        elseif #want >= 3 and name:find(want, 1, true) then part[#part + 1] = id end
    end
    local hit = #exact == 1 and exact or (#exact == 0 and part or exact)
    local shown = G.spell_facts(box)
    shown = shown and shown.name or box
    if #ids == 0 then G.refuse(shown .. " has no variants to pick for this character") end
    if #hit ~= 1 then
        G.refuse((#hit == 0 and "no variant '" or "more than one variant matches '") .. tostring(which) .. "' in "
            .. shown .. ": " .. G.variant_list(char, ids) .. (from and " (from the " .. from .. ")" or ""))
    end
    local used = G.variant_used(char, hit[1])
    if used then
        G.refuse(G.spell_facts(hit[1]).name .. " was already called: pick another of "
            .. G.variant_list(char, ids))
    end
    return hit[1]
end

-- Item spells with a once-per-rest-per-item cooldown (Spellcrux Amulet's Spell Slot Restoration): Osi.UseSpell
-- casts them without their item, so the engine records no cooldown. S030: Gale restored spell slots 3 times in a
-- row for free (the agent flagged it as a game bug and stopped). The guard keeps the ledger itself, in
-- PersistentVars so it travels with the save; a rest clears it (Events.lua).
function G.item_cd_key(char, spell)
    local st = type(spell) == "string" and Ext.Stats.Get(spell)
    local cd = st and tostring(st.Cooldown or "") or ""
    if not cd:find("Rest") or not cd:find("PerItem$") then return nil end
    local root = tostring(st.SpellContainerID or "")
    if root == "" then root = spell end
    local u = G.guid(char)
    return u and (u .. ":" .. root) or nil, cd:find("ShortRest") and "short" or "long"
end

function G.item_uses()
    PersistentVars = PersistentVars or {}
    PersistentVars.item_uses = PersistentVars.item_uses or {}
    return PersistentVars.item_uses
end

function G.item_used(char, spell)
    local key, kind = G.item_cd_key(char, spell)
    if key then G.item_uses()[key] = kind end
end

-- kind "long": every entry; "short": the short-rest ones (of char, when given)
function G.item_rest(kind, char)
    local uses = G.item_uses()
    local u = char and G.guid(char)
    for k, v in pairs(uses) do
        if kind == "long" or (v == "short" and (not u or k:sub(1, #u) == u)) then uses[k] = nil end
    end
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
-- Live (bg3le): Target_MainHandAttack/Target_OffhandAttack/Target_ConcussiveSmash
-- have TargetRadius "MeleeMainWeaponRange" and Range 0 (Range is 0 on every
-- spell probed, never a real range), Projectile_MainHandAttack
-- "RangedMainWeaponRange", Target_Shove 1.5, Target_HolyRebuke 18;
-- WPN_Warhammer/WPN_Longsword WeaponRange 150, WPN_Shortbow 1800.
G.DEFAULT_RANGED_M = 18.0 -- bows/crossbows when the weapon's stats are unreadable
G.DEFAULT_MELEE_M = 1.5   -- unarmed / unreadable melee weapon
function G.spell_range(st, char)
    if not st then return nil end
    local tr = tostring(st.TargetRadius or "")
    if tr:find("Melee") then
        local slot = tr:find("OffHand") and "Melee Offhand Weapon" or "Melee Main Weapon"
        return weapon_range(char, slot) or weapon_range(char, "Melee Main Weapon") or G.DEFAULT_MELEE_M, true
    end
    if tr:find("Ranged") then
        local slot = tr:find("OffHand") and "Ranged Offhand Weapon" or "Ranged Main Weapon"
        return weapon_range(char, slot) or weapon_range(char, "Ranged Main Weapon") or G.DEFAULT_RANGED_M, false
    end
    local r = tonumber(st.TargetRadius)
    if r and r > 0 then return r, r <= 3 end
    r = tonumber(st.Range)
    if r and r > 0 then return r, r <= 3 end
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

-- Melee reach is edge to edge: the furthest centre distance at which a weapon
-- (or Shove) with `range` m reaches `target` (the check_cast test below).
function G.melee_reach(char, target, range)
    return range + G.radius(char) + G.radius(target)
end

-- Jump distance (Projectile_Jump has no numeric range, so the guard caps it).
-- Its stats TargetRadius is 4.5 (live, bg3le). BG3: about half the Strength
-- score in metres; Enhance Leap triples it, Step of the Wind doubles it.
-- UNVERIFIED formula and status ids: tune here.
G.JUMP_SPELL = "Projectile_Jump"
G.JUMP_MIN_M = 4.0
G.JUMP_SLACK_M = 0.5
G.JUMP_MULTIPLIERS = { ENHANCE_LEAP = 3, STEP_OF_THE_WIND = 2, STEP_OF_THE_WIND_DASH = 2 }
-- Statuses that change a jump through their stats Boosts: JumpMaxDistanceMultiplier(k), IgnoreFallDamage(). S086/S087
-- (the Astral Prism, END_Main): every party member has Astral Gravity ("Immune to Falling damage and its Jump distance
-- is tripled", bg3.wiki); jump_to refused the island hops at "this character can jump 9.5 m" and route/ways never
-- offered them (a 4.5 m gap, 4 m down), while the jumps the agent then made by hand landed (15.2 m out, 6.7 m down).
-- Its status id and Boosts string are UNVERIFIED, so its display name (live: "Astral Gravity") counts too.
G.JUMP_STATUS_NAMES = { ["Astral Gravity"] = { mult = 3, no_fall = true } }
G._jumpBoosts = G._jumpBoosts or {} -- status id -> {mult, no_fall} or false (stats don't change in a session)
local function jumpBoosts(id)
    local c = G._jumpBoosts[id]
    if c ~= nil then return c end
    c = false
    local ok, s = pcall(function() return tostring(Ext.Stats.Get(id).Boosts or "") end)
    if ok then
        local k = tonumber(s:match("JumpMaxDistanceMultiplier%(%s*([%d%.]+)%s*%)"))
        local nf = s:find("IgnoreFallDamage", 1, true) ~= nil
        if (k and k > 1) or nf then c = { mult = k and k > 1 and k or 1, no_fall = nf or nil } end
    end
    G._jumpBoosts[id] = c
    return c
end
-- -> jump multiplier (1 = none), no fall damage (true/false) from char's active statuses
function G.jump_status(char)
    local V = BG3AI.Visibility
    local e = V and V.statuses and G.entity(char)
    if not e then return 1, false end
    local mult, noFall = 1, false
    for _, s in ipairs(V.statuses(e) or {}) do
        local j = G.JUMP_STATUS_NAMES[s.name] or jumpBoosts(s.id)
        if j then
            if (j.mult or 1) > mult then mult = j.mult end
            noFall = noFall or j.no_fall == true
        end
    end
    return mult, noFall
end
function G.jump_max(char)
    local str = tonumber((G.q("GetAbility", char, "Strength"))) or 10
    local m = math.max(G.JUMP_MIN_M, str / 2)
    local mult = 1
    for status, k in pairs(G.JUMP_MULTIPLIERS) do
        if b(G.q("HasActiveStatus", char, status)) and k > mult then mult = k end
    end
    local ok, k = pcall(G.jump_status, char)
    if ok and type(k) == "number" and k > mult then mult = k end
    return m * mult
end

-- Jumping down carries further: Projectile_Jump has the HasHighGroundRangeExtension flag, and the game's Data.txt
-- has HighGroundThreshold 2.3 / HighGroundRangeMultiplier 1, so the reach grows by the drop. PROVED S040 (22:02-22:10,
-- the game's own jump, Z + click): (-443.7,-6.9,-1416.5) -> ~(-470,-30,-1405) = 28 m out, 23 m down, Str 17 (flat
-- 8.5); (-489.2,-38.6,-1383.7) -> ~(-507,-56.5,-1385) = 18 m out, 18 m down. Our 8.5 m cap refused both ("too far to
-- jump (18.1 m, this character can jump 8.5 m)"), and the agent hand-clicked 33 jumps. (S039's "Target is too far!"
-- cursor reading over the same gap was a hover at the void.) jump_floor_ok still refuses void/air landings.
G.JUMP_UP_LIMIT_M = 6.0 -- generous (N.JUMP_UP_MAX_M 2.5 is the planner's UNVERIFIED climb); only absurd climbs refused
G.HIGH_GROUND_THRESHOLD_M = 2.3
G.HIGH_GROUND_JUMP_MULT = 1
function G.jump_reach(char, from, pos)
    local m = G.jump_max(char)
    if from and pos then
        local drop = from[2] - pos[2]
        if drop > G.HIGH_GROUND_THRESHOLD_M then return m + drop * G.HIGH_GROUND_JUMP_MULT, m end
    end
    return m, m
end

-- A jump landing must have ground under it near its height, even with force: S038 20:48 and S039 21:53 the game
-- froze (an engine allocation to 13 GB, OOM-killed) on a jump cast at a column with no AI-grid floor at all (the
-- Shadowfell's void, (-408.5,1,-1437) and (-414,20,-1447)); a forced jump into the air over a Chasm was a fatal fall
-- (S035/S038). The player's cursor refuses both.
G.JUMP_FLOOR_SLACK_M = 4.0
function G.jump_floor_ok(pos)
    local ok, hs = pcall(Ext.Level.GetHeightsAt, pos[1], pos[3])
    if not ok or type(hs) ~= "table" then return end -- can't tell
    local near
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - pos[2]) <= G.JUMP_FLOOR_SLACK_M then near = true end
    end
    if #hs == 0 then
        G.refuse(string.format("no ground at all under (%.1f,%.1f,%.1f) (the void): a jump there froze the game"
            .. " twice; pick a landing on a floor you can see (get_layout / route)", pos[1], pos[2], pos[3]))
    elseif not near then
        G.refuse(string.format("the landing (%.1f,%.1f,%.1f) is in the air: no floor within %.0f m of that height;"
            .. " give the floor's own y (get_layout / route)", pos[1], pos[2], pos[3], G.JUMP_FLOOR_SLACK_M))
    end
end

-- HasLineOfSight(char, item) is false for a door, chest or barricade the
-- character stands next to (its own collision blocks the ray: S024/S025 Knock
-- on 3 locked doors from 1.5-2 m, attacks on a barricade and a door, 10 casts
-- refused). An item that passed G.visible this close is in plain sight.
G.ITEM_LOS_NEAR_M = 4.0

-- Farther away the same holds (S083: Knock on e284 Threshold of Loss from 6.3 m, its position 5.9 m up at the
-- door's top; Eldritch Blast on the e703 sewer Wall; Knock on e720 from ~4 m: all refused "no line of sight" while
-- the game's cursor showed 100 %, ~48 hand inputs). HasLineOfSight aims at the item's position, which its own
-- collision or frame hides. An item is in sight from the caster when a physics ray from the caster's eye to a
-- point on a column through its position hits the item itself first (nothing stands between them), or reaches a
-- point inside its AI bounds unblocked (Visibility's large-object rule). Characters keep the engine's check.
-- nil = no usable ray API (uncalibrated / broken): the refusal stands.
G.ITEM_RAY_FRACS = { 0.0, 0.5, 0.85, -0.5, -1.0 } -- x the bounds height from the position; < 0 counts self hits only
G.ITEM_RAY_SLACK = 0.3

local function ray_hit_uuid(hit)
    local ok, u = pcall(function() return hit.Shape.PhysicsObject.Entity.Uuid.EntityUuid end)
    return ok and type(u) == "string" and u:lower() or nil
end

local function ray_hit_dist(hit, from)
    local ok, d = pcall(function() local p = hit.Position; return U.dist(from, { p[1], p[2], p[3] }) end)
    if ok and type(d) == "number" then return d end
    local okd, dd = pcall(function() return hit.Distance end)
    return okd and type(dd) == "number" and dd or nil
end

function G.item_ray_los(char, target)
    local V = BG3AI.Visibility
    local cast = Ext.Level and Ext.Level.RaycastClosest
    if type(V) ~= "table" or V.rayBroken or type(V.rayCalibrated) ~= "function" or not cast then return nil end
    local p, t, uuid = G.pos(char), G.pos(target), G.guid(target)
    if not (p and t and uuid) then return nil end
    local okc, cal = pcall(V.rayCalibrated, { pos = p })
    if not (okc and cal) then return nil end
    local okb, bd = pcall(function() return type(V.bounds) == "function" and V.bounds(G.entity(target)) or nil end)
    bd = okb and bd or nil
    local h = math.max(bd and bd.height or 2.0, 0.5)
    local r = math.max(bd and bd.radius or 0.5, 0.3)
    local eye = { p[1], p[2] + (V.EYE_H or 1.6), p[3] }
    local pts = {}
    for _, f in ipairs(G.ITEM_RAY_FRACS) do pts[#pts + 1] = { { t[1], t[2] + h * f, t[3] }, f >= 0 } end
    local dx, dz = t[1] - eye[1], t[3] - eye[3]
    local len = math.sqrt(dx * dx + dz * dz)
    if len > 0.01 then
        local ux, uz, side, pull, mid = dx / len, dz / len, math.min(r * 0.8, 1.5), math.min(r, 0.75), t[2] + h * 0.5
        pts[#pts + 1] = { { t[1] - uz * side, mid, t[3] + ux * side }, true }
        pts[#pts + 1] = { { t[1] + uz * side, mid, t[3] - ux * side }, true }
        pts[#pts + 1] = { { t[1] - ux * pull, mid, t[3] - uz * pull }, true }
    end
    local ok, res = pcall(function()
        for _, s in ipairs(pts) do
            local pt, inside = s[1], s[2]
            local hit = cast(eye, pt, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
            if hit == nil then
                if inside then return true end
            elseif ray_hit_uuid(hit) == uuid then
                return true
            elseif inside then
                local d = ray_hit_dist(hit, eye)
                if d and d >= U.dist(eye, pt) - G.ITEM_RAY_SLACK then return true end
            end
        end
        return false
    end)
    if not ok then return nil end
    return res
end

-- Line of sight from char to target for a cast: the engine's, else (items only) arm's length or the rays above.
function G.los_ok(char, target, d)
    if b(G.q("HasLineOfSight", char, target)) then return true end
    if b(G.q("IsCharacter", target)) then return false end
    if not d then
        local p, t = G.pos(char), G.pos(target)
        d = p and t and U.dist(p, t)
    end
    if d and d <= G.ITEM_LOS_NEAR_M then return true end
    return G.item_ray_los(char, target) == true
end

-- S085 05:03:37: Gale's Fireball at a point ~10 m below (-27,17,736) from the Guildhall's upper floor hit the ledge
-- and burst on e606 Lady Croup, a neutral Guild ally on the caster's floor (listed in get_combat_state): killed. A
-- player sees the aim preview before the click: the path, where it bursts, who stands in the ring. A harmful area
-- spell (radius = ExplodeRadius / AreaRadius) is refused, saying where it bursts and who is in the blast there, when
-- the blast would catch a party member, ally or neutral the party sees, or when a projectile aimed at a point bursts
-- farther than its radius from it (the straight eye ray to the point hits something first). force=true casts anyway.
-- The path is the straight line from the caster's eye; the game's projectile may arc. No usable ray API: no path
-- check (never refused for that), the ring at the point is still checked.
G.BLAST_TYPES = { Projectile = true, Target = true }
G.BLAST_LIFT_M = 0.5  -- the path ray ends this far above the point (a floor point grazes its own floor)
G.BLAST_SLACK_M = 1.0 -- a hit this close to the ray's end is the point itself

function G.blast_radius(st)
    local r = 0
    for _, f in ipairs({ "ExplodeRadius", "AreaRadius" }) do
        local ok, v = pcall(function() return tonumber(st[f]) end)
        if ok and v and v > r then r = v end
    end
    return r
end

-- Where a projectile aimed at `aim` bursts: aim (clear way or no ray API), else the eye ray's first hit.
-- Returns the point, blocked, the hit entity's uuid.
function G.blast_path(char, aim)
    local V = BG3AI.Visibility
    local cast = Ext.Level and Ext.Level.RaycastClosest
    local p = G.pos(char)
    if type(V) ~= "table" or V.rayBroken or type(V.rayCalibrated) ~= "function" or not cast or not p then
        return aim, false
    end
    local okc, cal = pcall(V.rayCalibrated, { pos = p })
    if not (okc and cal) then return aim, false end
    local eye = { p[1], p[2] + (V.EYE_H or 1.6), p[3] }
    local tgt = { aim[1], aim[2] + G.BLAST_LIFT_M, aim[3] }
    local ok, hit = pcall(cast, eye, tgt, V.RAY_TYPE, V.RAY_INCLUDE, V.RAY_EXCLUDE, 0)
    if not ok or hit == nil then return aim, false end
    local len, d = U.dist(eye, tgt), ray_hit_dist(hit, eye)
    if not d or d >= len - G.BLAST_SLACK_M then return aim, false end
    local okp, hp = pcall(function() local q = hit.Position; return { q[1], q[2], q[3] } end)
    if not (okp and type(hp[1]) == "number") then
        local f = d / len
        hp = { eye[1] + (tgt[1] - eye[1]) * f, eye[2] + (tgt[2] - eye[2]) * f, eye[3] + (tgt[3] - eye[3]) * f }
    end
    return hp, true, ray_hit_uuid(hit)
end

-- Living characters the party sees within r of pt (skip: the cast's own target), nearest first:
-- {uuid, name, side = party/ally/neutral/hostile, d}. nil without Visibility.
function G.in_blast(char, pt, r, skip)
    local V = BG3AI.Visibility
    if type(V) ~= "table" or type(V.charactersNear) ~= "function" then return nil end
    local ctx = V.context()
    local out = {}
    for _, row in ipairs(V.charactersNear(pt, r)) do
        local u = G.guid(V.entityUuid(row[1]) or "")
        if u and u ~= skip and not G.is_dead(u)
            and (G.is_party(u) or V.characterVisible(ctx, u, row[1], row[2])) then
            local side = (G.is_party(u) and "party") or (b(G.q("IsEnemy", char, u)) and "hostile")
                or (V.isAlly(ctx, u) and "ally") or "neutral"
            out[#out + 1] = { uuid = u, name = V.name(u), side = side, d = row[3] }
        end
    end
    table.sort(out, function(x, y) return x.d < y.d end)
    return out
end

local function fmt_pos(p) return string.format("(%.1f,%.1f,%.1f)", p[1], p[2], p[3]) end

function G.blast_risk(char, st, flags, spell, target, pos)
    local kind = tostring(st.SpellType or "")
    if not flags.IsHarmful or not G.BLAST_TYPES[kind] then return end
    local r = G.blast_radius(st)
    local aim = pos or (target and not G.same(char, target) and G.pos(target))
    if r <= 0 or not aim then return end
    local at, blocked, what = aim, false, nil
    if pos and kind == "Projectile" then at, blocked, what = G.blast_path(char, pos) end
    local short = blocked and U.dist(at, aim) > r
    local hit = G.in_blast(char, at, r, target and G.guid(target)) or {}
    local friends, hostile = {}, 0
    for _, h in ipairs(hit) do
        if h.side == "hostile" then hostile = hostile + 1
        else friends[#friends + 1] = string.format("%s %s (%s, %.1f m)", h.uuid, h.name, h.side, h.d) end
    end
    if #friends == 0 and not short then return end
    local name = U.loca(st.DisplayName) or spell
    local head
    if blocked then
        local on = what and b(G.q("IsItem", what)) and BG3AI.Visibility.name(what)
        local p = G.pos(char)
        head = string.format("%s: the way to that point is blocked: it would burst at %s%s, %.1f m from the caster"
            .. " and %.1f m from the point", name, fmt_pos(at), on and on ~= "Unknown" and (" on " .. on) or "",
            p and U.dist(p, at) or 0, U.dist(at, aim))
    else
        head = string.format("%s at %s", name, target and "that target" or "that point")
    end
    local ring = string.format("in its blast (%g m) there: ", r)
    if #friends > 0 then
        ring = ring .. table.concat(friends, ", ") .. (hostile > 0 and (" and " .. hostile .. " hostile") or "")
    else
        ring = ring .. (hostile > 0 and (hostile .. " hostile only") or "nobody")
    end
    G.refuse(head .. "; " .. ring .. ": aim elsewhere or move first, or force=true to cast anyway")
end

-- Extra Attack (live stats, 2026-10-10): after a weapon attack the ExtraAttack passive applies EXTRA_ATTACK,
-- whose boost is UnlockSpellVariant(ExtraAttackCheck(), ModifyUseCosts(Replace,ActionPoint,0,0,ActionPoint))
-- and which goes on the next cast (RemoveEvents OnSpellCast): the next weapon attack costs no action. We
-- checked the raw UseCosts (ActionPoint:1), so every Extra Attack was refused "not enough action" (S023-S028:
-- 0 of 7). A weapon attack here = a Melee/RangedWeaponAttack roll that is not a weapon action (Lacerate,
-- Cleave...): the main-hand attacks, Divine Smite, Reckless Attack.
G.EXTRA_ATTACK_STATUS = "EXTRA_ATTACK"

function G.extra_attack_free(char, st, flags)
    local roll = ""
    pcall(function() roll = tostring(st.SpellRoll and st.SpellRoll.Default or "") end)
    if not roll:find("WeaponAttack") or flags.IsDefaultWeaponAction then return false end
    return b(G.q("HasActiveStatus", char, G.EXTRA_ATTACK_STATUS))
end

-- Full pre-validation for an Osiris-originated cast. Returns
-- {stats, costs, payers} or refuses with the reason the UI would show.

-- The first status on char that stops its orders: {name, type}, or nil. S067 #43/#54: Wyll's Eldritch Blast, ordered
-- while Stunned, went to the game and never started (3 s each): an INCAPACITATED status (Stunned, Paralysed, Asleep)
-- leaves no action to take. S066 #100/#130, S067 #32: Karlach's Rage, the first order of her turn while Fearful (Orin's
-- Piercing Growl: FEARED, StatusType FEAR), never started either (3 of 3), though her move and attacks after it did.
G.BLOCKING_STATUS_TYPES = { INCAPACITATED = true, FEAR = true }
-- INCAPACITATED in the stats but not in play. S072 21:53:33: Karlach's turn started "+Lethargic -Hastened"
-- (HASTE_LETHARGY, `using "STUNNED"`); the guard refused her Rage "(it takes the whole turn)", yet the same turn's
-- move and both attacks worked.
G.ACTING_STATUSES = { HASTE_LETHARGY = true }
-- ...but no Lethargic walk ever started (S074 23:21:13, S089 06:56:41 "never started moving", travelled 0), and its
-- turn was over once the rest of the party's turn group had ended (S091 08:30:24 Wyll ended last, Claude's turn gone
-- by 08:30:34 with no end order; S088 06:19:53): orders sent before that worked (S072 two attacks, S089 Dash). S091
-- showed `DISABLED e5 Claude: Lethargic (1 turn)` beside `Move 9/9m` and the agent planned to walk off the Orb of
-- Negation's platform.
G.LETHARGY_EFFECT = "cannot move; its turn passes once the rest of its turn group has ended (act with it first)"
-- with_acting: those too (a walk that never started names them, S074).
function G.blocking_status(char, with_acting)
    local V = BG3AI.Visibility
    local e = V and V.statuses and G.entity(char)
    if not e then return nil end
    for _, s in ipairs(V.statuses(e) or {}) do
        local ok, typ = pcall(function() return tostring(Ext.Stats.Get(s.id).StatusType) end)
        if ok and G.BLOCKING_STATUS_TYPES[typ] and (with_acting or not G.ACTING_STATUSES[s.id]) then return { name = s.name, type = typ } end
    end
    return nil
end

-- S087 06:02:51-06:05:57: Mind Blast stunned Wyll, Gale and Shadowheart; their open turns read as acting, so Claude's
-- sent-ahead combat_turn was refused "e3 Shadowheart (yours) must act first" (and the agent was told to plan stunned
-- Wyll), while the game passes such a turn by itself. A party member whose INCAPACITATED status (Stunned, Paralysed,
-- Asleep …) takes the whole turn: its turn passes (state.combat `passes`: not acting, never blocking). Its name, or nil.
function G.turn_passes(char)
    local blk = G.blocking_status(char)
    return blk and blk.type == "INCAPACITATED" and blk.name or nil
end

-- Statuses that take a party member out of the fight, listed on every combat read (state.combat `disabled`) with
-- what the game offers against them. S076 00:39:38: Gale's +Sleeping (Chatterteeth's Eyebite) showed once, in a
-- CHANGES line; the agent planned her spells for rounds 4-6 ("Gale is awake now") and nobody woke her.
G.DISABLING_TYPES = { INCAPACITATED = true, FEAR = true, KNOCKED_DOWN = true }
G.DISABLING_GROUPS = { SG_Incapacitated = true, SG_Unconscious = true, SG_Sleeping = true, SG_Paralyzed = true,
    SG_Stunned = true, SG_Petrified = true, SG_Frightened = true, SG_Prone = true, SG_Restrained = true,
    SG_Dominated = true, SG_Possessed = true, SG_Confused = true, SG_Fleeing = true }
-- What Target_Help removes (its SpellProperties in Shared Spell_Target.txt: RemoveStatus of these groups and ids).
G.HELP_GROUPS = { SG_Sleeping = true, SG_Helpable_Condition = true, SG_Prone = true, SG_Restrained = true }
G.HELP_STATUSES = { BURNING = true, PRONE = true, SLEEPING = true, SLEEP = true, ENSNARING_STRIKE = true, WEB = true,
    HYPNOTIC_PATTERN = true }

-- A stats flag list (a table of names, a name->true map or a ';' string) as a set.
local function flagset(v)
    local out = {}
    if type(v) == "table" then
        for k, x in pairs(v) do
            if type(k) == "string" and x then out[k] = true elseif type(x) == "string" then out[x] = true end
        end
    elseif v ~= nil then
        for x in tostring(v):gmatch("[^;%s]+") do out[x] = true end
    end
    return out
end

G._disabling = G._disabling or {}  -- status id -> {effect, fix} or false (stats don't change in a session)

function G.disabling_info(id)
    local c = G._disabling[id]
    if c ~= nil then return c end
    c = false
    local st = Ext.Stats.Get(id)
    if st then
        local function field(k) local ok, v = pcall(function() return st[k] end); if ok then return v end end
        local typ = tostring(field("StatusType") or "")
        local groups = flagset(field("StatusGroups"))
        local hit = G.DISABLING_TYPES[typ]
        for g in pairs(groups) do hit = hit or G.DISABLING_GROUPS[g] end
        if hit and typ ~= "DOWNED" then  -- downed members have their own line (turn.downed; SG_Incapacitated too)
            local effect
            if typ == "KNOCKED_DOWN" or groups.SG_Prone then
                effect = "stands up at its turn start for half its movement"
            elseif typ == "INCAPACITATED" and not G.ACTING_STATUSES[id] then
                effect = "cannot act"
            elseif G.ACTING_STATUSES[id] then
                effect = G.LETHARGY_EFFECT
            elseif tostring(field("Boosts") or ""):find("ActionResourceBlock(Movement)", 1, true) then
                effect = "cannot move"
            end
            local fix = {}
            local help = G.HELP_STATUSES[id]
            for g in pairs(groups) do help = help or G.HELP_GROUPS[g] end
            if help then fix[#fix + 1] = "an adjacent ally's Help (an action) ends it" end
            if flagset(field("RemoveEvents")).OnDamage then fix[#fix + 1] = "any damage ends it" end
            if tostring(field("RemoveConditions") or ""):find("SavingThrow", 1, true) then
                fix[#fix + 1] = "it saves again each turn"
            end
            c = { effect = effect, fix = #fix > 0 and table.concat(fix, "; ") or nil }
        end
    end
    G._disabling[id] = c
    return c
end

-- char's disabling statuses: {{name, turns?, effect?, fix?}, ...} (nil when none).
function G.disabling(char)
    local V = BG3AI.Visibility
    local e = V and V.statuses and G.entity(char)
    if not e then return nil end
    local out
    for _, s in ipairs(V.statuses(e) or {}) do
        local ok, info = pcall(G.disabling_info, s.id)
        if ok and info then
            out = out or {}
            out[#out + 1] = { name = s.name, turns = s.turns, effect = info.effect, fix = info.fix }
        end
    end
    return out
end

-- What keeps char from walking: the name of a disabling status that says "cannot act" or "cannot move …", or nil.
-- The movement figure (State.lua moveView) and the combat walk guard (Actions.lua combat_budget) use it.
function G.move_blocked(char)
    for _, d in ipairs(G.disabling(char) or {}) do
        local eff = d.effect or ""
        if eff == "cannot act" or eff:find("^cannot move") then return d.name end
    end
    return nil
end

-- The fix the game offers for char's status `name` ("" when none), for refusals.
function G.disabling_fix(char, name)
    for _, d in ipairs(G.disabling(char) or {}) do
        if d.name == name and d.fix then return "; " .. d.fix end
    end
    return ""
end

function G.check_cast(char, spell, target, pos, upcast, force)
    if type(spell) ~= "string" or spell == "" then G.refuse("no action given") end
    local st = Ext.Stats.Get(spell)
    if not st then G.refuse("unknown action") end
    local variants = tostring(st.ContainerSpells or "")
    if variants ~= "" then
        G.refuse("choose a variant: " .. variants:gsub(";", ", "))
    end
    if G.linked_box(st) then
        local ids, from = G.box_variants(char, spell)
        local free = {}
        for _, id in ipairs(ids) do if not G.variant_used(char, id) then free[#free + 1] = id end end
        local shown = G.spell_facts(spell)
        shown = shown and shown.name or spell
        if #free > 0 then
            -- by shown name, the usable ones first (combat_turn's quick pick takes the first)
            G.refuse(shown .. " casts nothing by itself: use_ability which=<one of " .. G.variant_list(char, ids) .. ">"
                .. (spell_flags(st).CallAlliesSpell and ", x,y,z = the ground where they appear" or "")
                .. (from and " (from the " .. from .. ")" or ""))
        elseif #ids > 0 then
            G.refuse(shown .. ": every variant was already used: " .. G.variant_list(char, ids))
        elseif spell_flags(st).CallAlliesSpell then
            G.refuse(shown .. " casts nothing by itself and no ally group could be read for this character")
        end
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
    local blk = G.in_combat(char) and G.blocking_status(char)
    if blk and blk.type == "INCAPACITATED" then
        G.refuse("cannot act while " .. tostring(blk.name) .. " (it takes the whole turn): end its turn"
            .. G.disabling_fix(char, blk.name))
    end

    -- Resources.
    local snap = G.resources(char)
    local costs = G.parse_costs(st.UseCosts, upcast)
    local payers = {}
    local in_combat = G.in_combat(char)
    local extra_attack = in_combat and G.extra_attack_free(char, st, flags)
    if extra_attack then
        for i = #costs, 1, -1 do
            if costs[i].name == "ActionPoint" then table.remove(costs, i) end
        end
    end
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
        -- Melee (weapon or a short numeric reach like Shove) is edge to edge:
        -- centres may be both bounding radii farther apart than the reach
        -- (1.5 m warhammer + 0.5 + 0.49 for Claude vs an imp = 2.49 m).
        local reach = range and (melee and G.melee_reach(char, target, range) or range + 0.5)
        if reach and not G.same(char, target) and d > reach then
            G.refuse(string.format("target is out of range (%.1f m, range %.1f m): move closer first", d, range))
        end
        if st_type ~= "Shout" and not G.same(char, target) and not G.los_ok(char, target, d) then
            G.refuse("no line of sight to target")
        end
        if not force then G.blast_risk(char, st, flags, spell, target, nil) end
    elseif pos then
        G.point_ok(char, pos)
        local p = G.pos(char)
        if spell == G.JUMP_SPELL then G.jump_floor_ok(pos) end
        if spell == G.JUMP_SPELL and p then
            local dx, dz = pos[1] - p[1], pos[3] - p[3]
            local d = math.sqrt(dx * dx + dz * dz)
            -- a jump climbs a little, never 20 m (S039 21:53: jump_to (-414,20,-1447), 20 m above the jumper, froze
            -- the engine; the void check catches that one, this catches a floor high above too)
            if pos[2] - p[2] > G.JUMP_UP_LIMIT_M then
                G.refuse(string.format("the landing is %.1f m above you: a jump climbs at most ~%.0f m; find a ladder,"
                    .. " stairs or a ramp (route)", pos[2] - p[2], G.JUMP_UP_LIMIT_M))
            end
            local maxd, flat = G.jump_reach(char, p, pos)
            if d > maxd + G.JUMP_SLACK_M then
                G.refuse(maxd > flat + 0.05
                    and string.format("too far to jump (%.1f m, this character can jump %.1f m, %.1f m down to there)",
                        d, maxd, p[2] - pos[2])
                    or string.format("too far to jump (%.1f m, this character can jump %.1f m)", d, maxd))
            end
        end
        -- Projectile_Jump has TargetRadius 4.5 (live), below a strong
        -- character's jump; jump_max above is its cap instead.
        if range and p and spell ~= G.JUMP_SPELL and U.dist(p, pos) > range + 0.5 then
            G.refuse(string.format("target point is out of range (%.1f m, range %.1f m): move closer first",
                U.dist(p, pos), range))
        end
        if not force and spell ~= G.JUMP_SPELL then G.blast_risk(char, st, flags, spell, nil, pos) end
    end
    return { stats = st, costs = costs, payers = payers, snapshot = snap, level = level, range = range,
        extra_attack = extra_attack or nil }
end

return G
