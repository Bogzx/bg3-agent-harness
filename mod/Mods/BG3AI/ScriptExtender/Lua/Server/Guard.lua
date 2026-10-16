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
local function now_ms()
    local ok, t = pcall(function() return Ext.Utils.MonotonicTime() end)
    return ok and t or os.clock() * 1000
end
G.now_ms = now_ms

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

function G.end_pending(uuid)
    local g = G.guid(uuid) or uuid
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
        if not others then
            G.end_requested[g] = nil
            return false
        end
    end
    return true
end

function G.is_my_turn(uuid)
    local e = G.entity(uuid)
    local tb = e and U.comp(e, "TurnBased")
    if not tb then return false end
    local ok, v = pcall(function() return tb.IsActiveCombatTurn == true and tb.RequestedEndTurn ~= true end)
    return ok and v == true and not G.end_pending(uuid)
end

-- Has this character ended its turn while its turn group is still active?
function G.turn_requested_end(uuid)
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

-- Point targets: within reach of the actor, and (if Visibility offers it)
-- on known/explored ground.
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
            return r.reachable, { length_m = r.length_m, needs_jump = r.needs_jump, reason = r.error_cause or r.reason,
                hidden_floor = r.hidden_floor, drop = r.drop, budget = r.budget }
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
function G.wedged_hint(char)
    local N = BG3AI.Nav
    if type(N) ~= "table" or type(N.wedged) ~= "function" then return "" end
    local ok, w = pcall(N.wedged, char)
    if not ok or type(w) ~= "table" then return "" end
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
    if not ok or type(c) ~= "table" or not c.pos then return G.wedged_hint(char) end
    local up = ""
    if type(c.dy_to_goal) == "number" and math.abs(c.dy_to_goal) >= 1.5 then
        up = string.format(", it is %.1fm %s that spot", math.abs(c.dy_to_goal), c.dy_to_goal > 0 and "above" or "below")
    end
    return string.format("; closest walkable spot (%.1f,%.1f,%.1f) is %.1fm from it (walk %.0fm%s): move_to that, then a ladder/jump_to",
        c.pos[1], c.pos[2], c.pos[3], c.to_goal_m or 0, c.path_m or 0, up)
end

function G.point_reachable(char, pos)
    local r, info = G.reachable(char, pos)
    if info.needs_jump and G.in_combat(char) then G.refuse("needs a jump: use jump_to") end
    if r == true and info.drop then
        -- Navigation.lua N.path: a walk never takes a fall that hurts (S035 Faith-Leap death)
        local d = info.drop.pos or {}
        G.refuse(string.format("the way there drops %.1fm at (%.1f,%.1f,%.1f) (fall damage): find a ladder or stairs"
            .. " (route), or jump_to the landing on purpose", info.drop.m or 0, d[1] or 0, d[2] or 0, d[3] or 0))
    end
    if r == true and info.hidden_floor and info.hidden_floor.winds then
        -- Navigation.lua N.path (S035: a walk onto the Faith-Leap chasm's invisible floor killed Claude)
        local h = info.hidden_floor.pos or {}
        G.refuse(string.format("the engine's way there bends across the chasm where no floor shows (from (%.1f,%.1f,%.1f), %.1fm along):"
            .. " on an invisible-floor trial walk straight lines along the mosaic's lines (get_layout LINES), corner to corner",
            h[1] or 0, h[2] or 0, h[3] or 0, info.hidden_floor.at_m or 0))
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

function G.approach_point(char, target, reach)
    local a, tpos = G.pos(char), G.pos(target)
    if not a or not tpos then G.refuse("cannot locate target") end
    local top = ladder_top(a, target)
    local t = top or tpos -- where it is used from
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
        if y then
            tries = tries + 1
            local p = { c[1], y, c[2] }
            local r, info = G.reachable(char, p)
            if r ~= nil then known = true end
            if r == nil and info and info.budget then busy = true end
            if r == true and not info.needs_jump then
                return { pos = p, length_m = info.length_m or U.dist(a, p), dist = d, tpos = tpos }
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

-- false when `item`'s lock has no lockpick difficulty (Lock.LockDifficultyClassID is the zero GUID:
-- story locks, puzzle walls; the game answers a click with "locked" and never a roll), true when it has
-- one, nil when there is no Lock component to read. Live probe S026 (SCL): the Mausoleum "Stone Wall"
-- doors and every STORY_LOCK have the zero id; LockDC itself reads 0 on every lock.
function G.lock_pickable(item)
    local id
    pcall(function() id = G.entity(item).Lock.LockDifficultyClassID end)
    if id == nil then return nil end
    id = tostring(id)
    if id == "" or id:match("^[0%-]+$") then return false end
    return true
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
    local key = G.item_cd_key(char, spell)
    if key and G.item_uses()[key] then
        G.refuse("that action is used up until the next " .. (G.item_uses()[key] == "short" and "short or long rest"
            or "long rest") .. " (its item allows it once per rest)")
    end
    return found
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
function G.jump_max(char)
    local str = tonumber((G.q("GetAbility", char, "Strength"))) or 10
    local m = math.max(G.JUMP_MIN_M, str / 2)
    local mult = 1
    for status, k in pairs(G.JUMP_MULTIPLIERS) do
        if b(G.q("HasActiveStatus", char, status)) and k > mult then mult = k end
    end
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
-- Oath of Devotion: a killing blow on a Temporarily Hostile (guards turned on you mid-scene) or a neutral
-- character breaks the oath (PALADIN_OATH_BROKEN; restoring it costs 2000 gold at camp). S028 12:44: Claude's bow
-- finished The Warden (Temporarily Hostile, 2 HP) → Oath Broken, the 2nd time (S026 too). A player knows the
-- tenets and leaves the kill to someone else: a harmful cast by a Devotion paladin on such a target that is low
-- enough to die is refused unless force (or Non-Lethal Attacks is on, or the oath is already broken).
G.OATH_LOW_HP = 20       -- target HP at or below this...
G.OATH_LOW_FRAC = 0.3    -- ...or at or below this share of its max: one hit may kill it
-- The character's passive entry `id` (its .Disabled is a toggle's off state, read live: NonLethal), or nil.
local function passive(char, id)
    local found
    pcall(function()
        for _, pe in ipairs(G.entity(char).PassiveContainer.Passives) do
            if pe.Passive and pe.Passive.PassiveId == id then found = pe.Passive end
        end
    end)
    return found
end
function G.oath_risk(char, st, flags, target)
    if not target or G.same(char, target) or G.is_party(target) or not b(G.q("IsCharacter", target)) then return end
    if not (flags.IsHarmful or tostring((st.SpellRoll or {}).Default or ""):find("Attack%(")) then return end
    if not passive(char, "Oath_Devotion_Tenents") or b(G.q("HasActiveStatus", char, "PALADIN_OATH_BROKEN")) then
        return
    end
    local nl = passive(char, "NonLethal")
    if nl and nl.Disabled == false then return end -- Non-Lethal Attacks on: the blow knocks out
    local temp = b(G.q("HasActiveStatus", target, "TEMPORARILY_HOSTILE"))
    if not temp and b(G.q("IsEnemy", char, target)) then return end
    local hp, max
    pcall(function() local h = G.entity(target).Health; hp, max = h.Hp, h.MaxHp end)
    if not hp or hp <= 0 then return end
    if hp > G.OATH_LOW_HP and not (max and max > 0 and hp <= max * G.OATH_LOW_FRAC) then return end
    G.refuse(string.format("Oath of Devotion: killing this %s character (%d HP left) would break the oath "
        .. "(restoring it costs 2000 gold): let another party member finish it, or force=true to strike anyway",
        temp and "Temporarily Hostile" or "neutral", hp))
end

function G.check_cast(char, spell, target, pos, upcast, force)
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
        if not force then G.oath_risk(char, st, flags, target) end
        if st_type ~= "Shout" and not G.same(char, target)
            and not b(G.q("HasLineOfSight", char, target))
            and not (d <= G.ITEM_LOS_NEAR_M and not b(G.q("IsCharacter", target))) then
            G.refuse("no line of sight to target")
        end
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
    end
    return { stats = st, costs = costs, payers = payers, snapshot = snap, level = level, range = range,
        extra_attack = extra_attack or nil }
end

return G
