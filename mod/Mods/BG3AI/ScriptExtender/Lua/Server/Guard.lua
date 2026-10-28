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
-- An instance counts only while its own DialogId is the id the manager lists
-- it under. Bench B3 (act3_gold_max r2, 13:32:24/27): 2-5 s after Oliver
-- Tefoco's dialogue ended, with InDialog false on every party member, the
-- manager still listed an instance reading DialogId 0 and a building mesh as
-- its resource; it blocked Wyll's unequip twice "in a conversation".
-- Returns blocked, why ("dialog 12 res=..." / "InDialog" / "SpeakerGetDialog");
-- why is for the game log only: it names engine resources a player can't see.
local function inst_field(inst, k)
    local ok, v = pcall(function() return inst[k] end)
    return ok and v or nil
end

local function live_inst(inst, id)
    local d = inst_field(inst, "DialogId")
    return d ~= nil and d ~= 0 and d == id
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
            if okd and inst ~= nil and live_inst(inst, id) then insts[#insts + 1] = inst end
        end
        pcall(function()
            for k, inst in pairs(dialogs) do
                local sps = live_inst(inst, k) and inst_field(inst, "Speakers")
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
    if blocked then
        Ext.Utils.Print("[BG3AI] guard: in a conversation: " .. tostring(why))
        G.refuse("character is in a conversation")
    end
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
            -- live N11 step 2: a "found" path with no nodes (it ends on the walker's own spot, 0.0 m) to a point 1.8 m
            -- off read "reachable"; a normal path's last node is ~1 m from its goal (Navigation.lua readResult)
            info.no_nodes = r.reachable and r.nodes == 0 or nil
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

-- File-local helpers the other parts use.
G.inner = { b = b }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads G and the
-- helpers in G.inner.
for _, part in ipairs({ "Server/GuardGate.lua", "Server/GuardSpells.lua", "Server/GuardCast.lua" }) do
    Ext.Require(part)
end

return G
