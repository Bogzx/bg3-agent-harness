-- Two teams in one fight (server context): the teams, whose turn it is, and the harness's arena.* ops.
-- Shared/Arena.lua has the switch (BG3AI_ARENA=1) and Ar.active(). Every op here is refused without the switch and
-- is harness-only (never an agent tool: bridge/tests/test_referee.py HIDDEN_OPS). Setup calls (factions, the party,
-- placing) go to Osiris directly, never through Guard's G.osi, whose blocklist is for agent actions.
--
-- Turn ownership comes from the game's own turn flags and turn order (listener time: the team tables, not Ctx.side).
-- The game puts adjacent player characters into one shared turn group whatever their side, so the arena plays every
-- group in phases: a phase is one same-side run of the group's open members in turn order (B|AAAA|BBB: 3 phases).
-- Its members act; every other open member is held until the phases before its own have ended. One arena_turn record
-- per phase (Ar.phase_check on the server tick), not per TurnStarted, which fires once per member of a group.

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

local function broadcast(msg)
    local ok, err = pcall(function() Ext.Net.CreateChannel(Ar.NET[1], Ar.NET[2]):Broadcast(msg) end)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] arena teams to the client: " .. tostring(err)) end
end

-- A summon seen for the first time: its name joins its side's names and the client gets them again (a reaction
-- prompt for it is its side's).
local function noteSummon(side, u)
    local st = Ar.state
    st.summons = st.summons or {}
    if st.summons[u] then return end
    st.summons[u] = side
    st.names[side][#st.names[side] + 1] = V.name(u)
    broadcast({ teams = st.teams, names = st.names })
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
            noteSummon(side, u)
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

local function record(ev, data)
    local R = BG3AI.Referee
    if R and R.record then pcall(R.record, ev, data) end
end

-- Without the turn order: the side whose open member's turn started first (the game's own order), else the side of
-- the last turn started, else A. Recorded once per round and side, so an audit sees every such call.
local function tie_fallback(open)
    local st, G = Ar.state, BG3AI.Guard
    local best, side
    for u in pairs(open) do
        local t = G.turn_start[u] and G.turn_start[u].t
        local s = Ar.side_of(u)
        if t and (best == nil or t < best or (t == best and s < side)) then best, side = t, s end
    end
    side = side or (st.turn and st.turn.side) or "A"
    local k = tostring(st.round) .. "|" .. side
    if st.tieFallback ~= k then
        st.tieFallback = k
        local list = {}
        for u in pairs(open) do list[#list + 1] = u end
        table.sort(list)
        record("arena_tie_fallback", { side = side, round = st.round, open = list })
    end
    return side
end

-- The turn order's groups, each {team = its Team guid (nil when unread), members = {uuid...}} in order; nil when no
-- team member's combat turn order reads.
local function turnGroups()
    local out
    for _, s in ipairs(Ar.SIDES) do
        for _, u in ipairs(Ar.state.teams[s]) do
            local ok = pcall(function()
                local to = V.entity(u).CombatParticipant.CombatHandle.TurnOrder
                local gs = {}
                for gi, g in ipairs(to.CurrentGroups) do
                    local mem = {}
                    for _, m in ipairs(g.Members) do mem[#mem + 1] = V.entityUuid(m.Entity) end
                    local okT, t = pcall(function() return g.Team end)
                    gs[gi] = { team = okT and t ~= nil and tostring(t) or nil, members = mem }
                end
                out = gs
            end)
            if ok and out then return out end
        end
    end
    return nil
end

-- The acting phase: the first turn group holding an open team member; its first open member's side; that side's run
-- of open members from there (in turn order, skipping members whose turn is not open). Without such a group in the
-- turn order (it doesn't read): the open members by the team tables, the side tie_fallback picks when both have one,
-- group nil. nil when no team member's turn is open.
function Ar.phase()
    local gs = turnGroups()
    for _, g in ipairs(gs or {}) do
        local open, sides = {}, {}
        for _, u in ipairs(g.members) do
            local s = u and Ar.side_of(u)
            if s then
                sides[s] = true
                if Ar.open(u) then open[#open + 1] = { u = u, s = s } end
            end
        end
        if #open > 0 then
            local side, members = open[1].s, {}
            for _, x in ipairs(open) do
                if x.s ~= side then break end
                members[#members + 1] = x.u
            end
            return { side = side, character = members[1], members = members, group = g.team or g.members[1],
                shared = (sides.A and sides.B) == true }
        end
    end
    local open, by = {}, {}
    for _, s in ipairs(Ar.SIDES) do
        by[s] = {}
        for _, u in ipairs(Ar.state.teams[s]) do
            if Ar.open(u) then open[u] = true; by[s][#by[s] + 1] = u end
        end
    end
    local side = (#by.A > 0 and #by.B > 0) and tie_fallback(open) or (#by.A > 0 and "A") or (#by.B > 0 and "B") or nil
    if not side then return nil end
    return { side = side, character = by[side][1], members = by[side], shared = #by.A > 0 and #by.B > 0 }
end

-- The side acting now: the acting phase's side; with no team member's turn open, the side of the last team member
-- whose turn started.
function Ar.acting_side()
    local p = Ar.phase()
    if p then return p.side end
    local st = Ar.state
    return st.turn and st.turn.side or nil
end

-- A team member whose turn flags are open outside the acting phase (the other side's, or a later run of its own side
-- in the same turn group): its turn waits (Guard.turn_moved_on), as if it had not come yet.
function Ar.held(uuid)
    if not Ar.active() then return false end
    local s = Ar.side_of(uuid)
    if not s or not Ar.open(uuid) then return false end
    for _, m in ipairs(Ar.phase().members) do -- uuid's turn is open: there is a phase
        if m == uuid then return false end
    end
    return true
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

-- The arena's combat: the one a team member is in (a fight elsewhere on the level is not the match).
local function arenaCombat(combat)
    local st = Ar.state
    if st.combat == nil and combat ~= nil then
        for _, s in ipairs(Ar.SIDES) do
            for _, u in ipairs(st.teams[s]) do
                if osi(Osi.CombatGetGuidFor, u) == combat then st.combat = combat end
            end
        end
    end
    return combat ~= nil and combat == st.combat
end

function Ar.turn_started(char)
    if not Ar.active() then return end
    local u = V.uuid(char)
    local s = u and Ar.side_of(u)
    if not s then return end
    local st = Ar.state
    if not Ar.member_side(u) then Ar.team(s) end -- a summon's turn: its name reaches the client
    arenaCombat(osi(Osi.CombatGetGuidFor, u))
    if (st.round or 0) < 1 then st.round = 1 end -- a turn before the first CombatRoundStarted is round 1's
    st.turn = { side = s, uuid = u, round = st.round }
    Ar.check_end()
end

-- The acting phase changed since the last check (another round, group or side): a turn boundary (a wipe ends the
-- match), then one arena_turn {side, character (its first open member), members, round, phase (1-based within the
-- group), group (the group's Team guid, else its first member), shared (the group holds both sides)}. On the server
-- tick, at most every PHASE_EVERY_MS: the per-member TurnStarted burst of a group (all within a few ms) is over by
-- then, and so is any change no listener sees (an end, a downing, a status). A phase shorter than that between two
-- checks is not seen.
Ar.PHASE_EVERY_MS = 250
function Ar.phase_check()
    if not Ar.active() then return end
    local st = Ar.state
    if st.ended then return end
    local t = now_ms()
    if st.phaseAt and t - st.phaseAt < Ar.PHASE_EVERY_MS then return end
    st.phaseAt = t
    local p = Ar.phase()
    if not p then return end
    local round = math.max(st.round or 0, 1)
    local last = st.phase
    if last and last.round == round and last.group == p.group and last.side == p.side then return end
    local n = (last and last.round == round and last.group == p.group) and last.phase + 1 or 1
    st.phase = { round = round, group = p.group, side = p.side, phase = n }
    Ar.check_end()
    if st.ended then return end
    record("arena_turn", { side = p.side, character = p.character, members = p.members, round = round, phase = n,
        group = p.group, shared = p.shared })
end

-- A round of the arena's combat starts: a turn boundary (a wipe ends the match); the start of round cap+1 ends it
-- as a draw (reason round_cap) with rounds = the cap, before anyone acts in it.
function Ar.round_started(combat, round)
    if not Ar.active() then return end
    local st = Ar.state
    if st.ended or not arenaCombat(combat) then return end
    round = tonumber(round)
    if not round then return end
    Ar.check_end()
    if st.ended then return end
    if st.round_cap and round > st.round_cap then
        Ar.finish(nil, "round_cap")
        return
    end
    st.round = round
end

-- The arena's combat ends: a wipe ends the match; any other end is recorded for the runner (it ends the match).
function Ar.combat_ended(combat)
    if not Ar.active() then return end
    local st = Ar.state
    if st.ended or combat == nil or combat ~= st.combat then return end
    Ar.check_end()
    if not st.ended then record("arena_combat_ended", { rounds = st.round }) end
end

if not Ar.listening then
    Ar.listening = true
    for name, arity in pairs({ TurnStarted = 1, CombatRoundStarted = 2, CombatEnded = 1 }) do
        local ok, err = pcall(Ext.Osiris.RegisterListener, name, arity, "after", function(a, b)
            local ok2, err2 = pcall(function()
                if name == "TurnStarted" then
                    Ar.turn_started(a)
                elseif name == "CombatRoundStarted" then
                    Ar.round_started(a, b)
                else
                    Ar.combat_ended(a)
                end
            end)
            if not ok2 then Ext.Utils.PrintWarning("[BG3AI] arena " .. name .. ": " .. tostring(err2)) end
        end)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] arena cannot listen to " .. name .. ": " .. tostring(err)) end
    end
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(Ar.phase_check)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] arena phase: " .. tostring(err)) end
    end)
end

-- Begin a match with these teams ({A = {uuid x4}, B = {uuid x4}}): the setup op calls it once the teams stand. The
-- client gets the team uuids and display names. opts.round_cap: the rounds a match lasts at most (a draw after).
function Ar.begin(teams, opts)
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
    Ar.state = { teams = t, names = names, round = 0, started = now_ms(),
        round_cap = opts and tonumber(opts.round_cap) or nil }
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
