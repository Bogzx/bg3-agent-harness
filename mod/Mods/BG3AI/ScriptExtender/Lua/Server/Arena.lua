-- Two teams in one fight (server context): the teams, whose turn it is, and the harness's arena.* ops.
-- Shared/Arena.lua has the switch (BG3AI_ARENA=1) and Ar.active(). Every op here is refused without the switch and
-- is harness-only (never an agent tool: bridge/tests/test_referee.py HIDDEN_OPS). Setup calls (factions, the party,
-- placing) go to Osiris directly, never through Guard's G.osi, whose blocklist is for agent actions.
--
-- Turn ownership comes from the game's own TurnStarted events (listener time: the team tables, not Ctx.side). When
-- the game puts members of both teams in one open turn group (an initiative tie), the side listed first in the
-- group acts and the other side's members count as not acting until the first side's members have all ended.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local Ar = BG3AI.Arena
local truthy = U.truthy

Ar.SIDES = { "A", "B" }

local function osi(fn, ...) return V.osi(fn, ...) end
local function now_ms() return Ext.Utils.MonotonicTime() end

local function other(side) return side == "A" and "B" or "A" end
Ar.other = other

-- The summon's owner (IsSummon.Owner / Summoner), nil for anyone else.
function Ar.owner(uuid)
    local e = V.entity(uuid)
    local sc = e and U.comp(e, "IsSummon")
    if not sc then return nil end
    local ow
    pcall(function() ow = V.entityUuid(sc.Owner) or V.entityUuid(sc.Summoner) end)
    return ow
end

-- The side of a team member or of a summon a team member owns.
function Ar.side_of(uuid)
    local s = Ar.member_side(uuid)
    if s then return s end
    local ow = uuid and Ar.owner(uuid)
    return ow and Ar.member_side(ow) or nil
end

-- A side's team: its members, then their summons (from DB_PartyMembers, where the game keeps summons). Returns the
-- list and the uuid -> true set, as V.party() does.
function Ar.team(side)
    local out, seen = {}, {}
    for _, u in ipairs(Ar.state.teams[side] or {}) do
        if not seen[u] then seen[u] = true; out[#out + 1] = u end
    end
    local ok, rows = pcall(function() return Osi.DB_PartyMembers:Get(nil) end)
    for _, row in ipairs(ok and rows or {}) do
        local u = V.uuid(row[1])
        if u and not seen[u] and Ar.member_side(Ar.owner(u)) == side then
            seen[u] = true
            out[#out + 1] = u
        end
    end
    return out, seen
end

------------------------------------------------------------------------
-- Whose turn
------------------------------------------------------------------------

-- A member's turn is open: the game's flags say active and not ended, and no end of ours is pending.
function Ar.open(uuid)
    local G = BG3AI.Guard
    local active, req = G.tb_flags(uuid)
    return active == true and not req and not G.end_pending(uuid)
end

-- A side's own "selected" character (the HUD's selection and the host belong to no side): its member whose turn
-- is open, else its first living member, else its first.
function Ar.lead(side)
    local team = Ar.state.teams[side]
    for _, u in ipairs(team) do
        if Ar.open(u) then return u end
    end
    for _, u in ipairs(team) do
        if not truthy(osi(Osi.IsDead, u)) then return u end
    end
    return team[1]
end

-- The side listed first in the turn order's first group that holds an open member.
function Ar.first_open_side(open)
    local found
    pcall(function()
        local any = Ar.state.teams.A[1]
        local to = V.entity(any).CombatParticipant.CombatHandle.TurnOrder
        for _, g in ipairs(to.CurrentGroups) do
            for _, m in ipairs(g.Members) do
                local u = V.entityUuid(m.Entity)
                if u and open[u] then
                    found = Ar.side_of(u)
                    return
                end
            end
        end
    end)
    return found
end

-- The side acting now: the side with an open member; with open members on both (one turn group across the teams),
-- the side listed first; with none, the side of the last team member whose turn started.
function Ar.acting_side()
    local st = Ar.state
    local open, by = {}, {}
    for _, s in ipairs(Ar.SIDES) do
        for _, u in ipairs(st.teams[s]) do
            if Ar.open(u) then open[u] = true; by[s] = true end
        end
    end
    if by.A and by.B then return Ar.first_open_side(open) or (st.turn and st.turn.side) or "A" end
    if by.A then return "A" end
    if by.B then return "B" end
    return st.turn and st.turn.side or nil
end

-- A team member whose turn flags are open while the other side acts (a turn group across the teams): its turn
-- waits (Guard.turn_moved_on), as if it had not come yet.
function Ar.held(uuid)
    if not Ar.active() then return false end
    local s = Ar.side_of(uuid)
    if not s or not Ar.open(uuid) then return false end
    local acting = Ar.acting_side()
    return acting ~= nil and acting ~= s
end

------------------------------------------------------------------------
-- The match: start, turns, end
------------------------------------------------------------------------

local function hpLeft(side)
    local hp, max = 0, 0
    for _, u in ipairs(Ar.state.teams[side]) do
        local e = V.entity(u)
        local h = e and U.comp(e, "Health")
        if h then
            pcall(function()
                hp, max = hp + math.max(0, h.Hp), max + h.MaxHp
            end)
        end
    end
    return max > 0 and U.round(hp / max, 3) or 0
end

-- Every member of the side downed or dead.
function Ar.wiped(side)
    for _, u in ipairs(Ar.state.teams[side]) do
        local dead = truthy(osi(Osi.IsDead, u))
        local downed = truthy(osi(Osi.HasActiveStatus, u, "DOWNED"))
        if not dead and not downed then
            local e = V.entity(u)
            local h = e and U.comp(e, "Health")
            local ok, hp = pcall(function() return h.Hp end)
            if not (ok and hp == 0) then return false end
        end
    end
    return true
end

local function record(ev, data)
    local R = BG3AI.Referee
    if R and R.record then pcall(R.record, ev, data) end
end

function Ar.finish(winner, reason)
    local st = Ar.state
    if st.ended then return st.ended end
    st.ended = { winner = winner, reason = reason, rounds = st.round, hp_left_A = hpLeft("A"), hp_left_B = hpLeft("B"),
        t = now_ms() }
    record("arena_end", { winner = winner, reason = reason, rounds = st.round, hp_left_A = st.ended.hp_left_A,
        hp_left_B = st.ended.hp_left_B })
    return st.ended
end

-- A turn boundary: a side all downed or dead loses (both: a draw).
function Ar.check_end()
    local st = Ar.state
    if st.ended then return end
    local a, b = Ar.wiped("A"), Ar.wiped("B")
    if not (a or b) then return end
    local winner -- both at once: no winner
    if not (a and b) then winner = a and "B" or "A" end
    Ar.finish(winner, "wipe")
end

function Ar.turn_started(char)
    if not Ar.active() then return end
    local u = V.uuid(char)
    local s = u and Ar.side_of(u)
    if not s then return end
    local st = Ar.state
    st.turn = { side = s, uuid = u, round = st.round }
    record("arena_turn", { side = s, character = u, round = st.round })
    Ar.check_end()
end

if not Ar.listening then
    Ar.listening = true
    for name, arity in pairs({ TurnStarted = 1, CombatRoundStarted = 2 }) do
        local ok, err = pcall(Ext.Osiris.RegisterListener, name, arity, "after", function(a, b)
            local ok2, err2 = pcall(function()
                if name == "TurnStarted" then
                    Ar.turn_started(a)
                elseif Ar.active() then
                    Ar.state.round = tonumber(b) or Ar.state.round
                end
            end)
            if not ok2 then Ext.Utils.PrintWarning("[BG3AI] arena " .. name .. ": " .. tostring(err2)) end
        end)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] arena cannot listen to " .. name .. ": " .. tostring(err)) end
    end
end

local function broadcast(msg)
    local ok, err = pcall(function() Ext.Net.CreateChannel(Ar.NET[1], Ar.NET[2]):Broadcast(msg) end)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] arena teams to the client: " .. tostring(err)) end
end

-- Begin a match with these teams ({A = {uuid x4}, B = {uuid x4}}): the setup op calls it once the teams stand. The
-- client gets the team uuids and display names.
function Ar.begin(teams)
    if not Ar.enabled() then error("arena ops need BG3AI_ARENA=1 in the game's environment", 0) end
    local t, names = {}, {}
    for _, s in ipairs(Ar.SIDES) do
        t[s], names[s] = {}, {}
        for _, u in ipairs(teams[s] or {}) do
            local g = (V.uuid(u) or u):lower()
            t[s][#t[s] + 1] = g
            names[s][#names[s] + 1] = V.name(g)
        end
        if #t[s] == 0 then error("team " .. s .. " is empty", 0) end
    end
    RPC.clear_sides()
    Ar.state = { teams = t, names = names, round = 0, started = now_ms() }
    broadcast({ teams = t, names = names })
    record("arena_start", { teams = t })
    return Ar.status()
end

function Ar.status()
    local st = Ar.state
    if not st then return { active = false, teams = nil, turn = nil, hp = nil, ended = nil } end
    -- hp: each team's summed current HP over its summed max (the true values: harness only)
    return { active = Ar.active(), teams = st.teams, turn = st.turn, hp = { A = hpLeft("A"), B = hpLeft("B") },
        ended = st.ended }
end

function Ar.reset()
    Ar.state = nil
    RPC.clear_sides()
    broadcast({})
    return Ar.status()
end

local function op(name, fn)
    RPC.register(name, function(args)
        if not Ar.enabled() then error("arena ops need BG3AI_ARENA=1 in the game's environment", 0) end
        if RPC.Ctx.side then error("arena ops are the harness's (no side)", 0) end
        return fn(args or {})
    end)
end

op("arena.status", function() return Ar.status() end)

-- The runner's end (caps, forfeits, faults): {reason, winner?}; a match the mod already ended keeps that end.
op("arena.end", function(args)
    if not Ar.state then error("no arena", 0) end
    local w = args.winner
    if w ~= nil and w ~= "A" and w ~= "B" then error("winner must be A or B", 0) end
    Ar.finish(w, tostring(args.reason or "aborted"))
    return Ar.status()
end)

op("arena.reset", function() return Ar.reset() end)

return Ar
