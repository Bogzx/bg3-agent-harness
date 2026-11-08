-- Two teams in one fight (server context): the teams, whose turn it is, and the harness's arena.* ops.
-- Shared/Arena.lua has the switch (BG3AI_ARENA=1) and Ar.active(). Every op here is refused without the switch and
-- is harness-only (never an agent tool: bridge/tests/test_referee.py HIDDEN_OPS). Setup calls (factions, the party,
-- placing) go to Osiris directly, never through Guard's G.osi, whose blocklist is for agent actions.
--
-- Turn ownership comes from the game's own turn flags and turn order (listener time: the team tables, not Ctx.side).
-- The game puts adjacent player characters into one shared turn group whatever their side, so the arena plays every
-- group in phases of one character each, the sides alternating (seat fix, owner D643, Fable REVIEW-8 O1): the round's
-- opening side's first open member in the rolled order, then the other side's, and so on (B|AA|B plays B1|A1|B2|A2).
-- A side with nobody open left plays its members on in order. The arena file's turn_order picks the opening side:
-- "alternate" (default) the side of the round's first living member in the rolled order; "snake" that for the match's
-- first round, then the other side each round. With more than one turn group in a round (a summon, a split), the
-- sides alternate inside each group only: alternate opens each group with its own first living member's side, snake
-- opens every group of the round with the round's side, so A1 | A2 B1 B2 can happen (arena-v0 has played one group
-- every round). Every other open member is held until its phase. One arena_turn record per phase (Ar.phase_check
-- on the server tick), not per TurnStarted, which fires once per member of a group.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local Ar = BG3AI.Arena
local truthy = U.truthy

Ar.SIDES = { "A", "B" }
Ar.TURN_ORDERS = { alternate = true, snake = true }

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
-- the last turn started, else the side the match's seed and round give (A when seed + round is odd, else B: no side
-- is the default every time; REVIEW-5 F9). Recorded once per round and side, so an audit sees every such call.
local function tie_fallback(open)
    local st, G = Ar.state, BG3AI.Guard
    local best, side
    for u in pairs(open) do
        local t = G.turn_start[u] and G.turn_start[u].t
        local s = Ar.side_of(u)
        if t and (best == nil or t < best or (t == best and s < side)) then best, side = t, s end
    end
    side = side or (st.turn and st.turn.side) or (((st.seed or 0) + (st.round or 1)) % 2 == 1 and "A" or "B")
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

-- The round's opening side in a group whose rolled order opens with `first`'s side (turn_order): kept per round and
-- group from the round's first phase on, so a read later in the round gives the same side.
local function openingSide(round, group, first)
    local st = Ar.state
    st.openings = st.openings or {}
    local k = tostring(round) .. "|" .. tostring(group)
    if st.openings[k] then return st.openings[k] end
    local o = first
    if st.turn_order == "snake" then
        if not st.snake then st.snake = { round = round, side = first } end
        o = ((round - st.snake.round) % 2 == 0) and st.snake.side or other(st.snake.side)
    end
    st.openings[k] = o
    return o
end

-- One group's acting phase from its open members ({u, s} in turn order): the last phase's character while its turn
-- is still open; else, after a phase of this round and group, the other side's first open member; else (the group's
-- first phase this round) the opening side's. A side with nobody open: the other side's first open member. rule:
-- "opening" | "alternate" | "only_side_left". first: the side the group's rolled order opens with (Ar.phase).
-- members: all the group's members. The group is the last phase's while it holds that phase's character, whatever
-- its id reads now (a Team that reads nil on one tick gives members[1]: O1 audit S3), and keeps that phase's opening.
local function pick(open, group, shared, round, first, members)
    local st = Ar.state
    local last = st.phase
    if last ~= nil and last.round == round and last.group ~= group then
        for _, u in ipairs(members or {}) do
            if u == last.character then group = last.group; break end
        end
    end
    local same = last ~= nil and last.round == round and last.group == group
    local opening = same and last.opening or openingSide(round, group, first)
    local function phase(x, rule)
        return { side = x.s, character = x.u, members = { x.u }, group = group, shared = shared, rule = rule,
            opening = opening }
    end
    if same then
        for _, x in ipairs(open) do
            if x.u == last.character then return phase(x, last.rule) end
        end
    end
    local want, rule = opening, "opening"
    if same then want, rule = other(last.side), "alternate" end
    for _, x in ipairs(open) do
        if x.s == want then return phase(x, rule) end
    end
    return phase(open[1], "only_side_left")
end

-- The acting phase: in the first turn group holding an open team member, one character (pick). Without such a group
-- in the turn order (it doesn't read): the open members by the team tables, the side tie_fallback picks first when
-- both have one, group nil. nil when no team member's turn is open. The group's opening side comes from its rolled
-- order (its first team member not dead; a downed one keeps its slot), not from whose turn is open at the read: a
-- read from the side gate, Ar.held or a state read can come between two of the round's TurnStarted events.
function Ar.phase(gs)
    gs = gs or turnGroups()
    local round = Ar.state.round or 1
    for _, g in ipairs(gs or {}) do
        local open, sides, first = {}, {}, nil
        for _, u in ipairs(g.members) do
            local s = u and Ar.side_of(u)
            if s then
                sides[s] = true
                if Ar.open(u) then open[#open + 1] = { u = u, s = s } end
                if not first and not truthy(osi(Osi.IsDead, u)) then first = s end
            end
        end
        if #open > 0 then
            return pick(open, g.team or g.members[1], (sides.A and sides.B) == true, round, first or open[1].s,
                g.members)
        end
    end
    local open, by = {}, {}
    for _, s in ipairs(Ar.SIDES) do
        by[s] = {}
        for _, u in ipairs(Ar.state.teams[s]) do
            if Ar.open(u) then open[u] = true; by[s][#by[s] + 1] = u end
        end
    end
    if #by.A == 0 and #by.B == 0 then return nil end
    local first = (#by.A > 0 and #by.B > 0) and tie_fallback(open) or (#by.A > 0 and "A") or "B"
    local list = {}
    for _, s in ipairs({ first, other(first) }) do
        for _, u in ipairs(by[s]) do list[#list + 1] = { u = u, s = s } end
    end
    local all = {}
    for _, s in ipairs(Ar.SIDES) do
        for _, u in ipairs(Ar.state.teams[s]) do all[#all + 1] = u end
    end
    return pick(list, nil, #by.A > 0 and #by.B > 0, round, first, all) -- an unread tick keeps the last phase's group
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

-- Why a member can't act: dead; downed (the game's DOWNED status: its turn is the death saving throw, nothing to
-- decide); else at 0 HP without either ("zero_hp"); else nil. Live 2026-11-02 (live 10, m1 a2): Lae'zel, downed
-- mid-move, got DownedChanged down then up again at once with her HP still 0; from then on she had no DOWNED status,
-- the game rolled no death saving throw for her and kept her turn open as if she could act (round 2: 4 min until the
-- runner's cap ended it; round 3: 300 s to the stall fault).
local function cantAct(u)
    if truthy(osi(Osi.IsDead, u)) then return "dead" end
    if truthy(osi(Osi.HasActiveStatus, u, "DOWNED")) then return "downed" end
    local e = V.entity(u)
    local h = e and U.comp(e, "Health")
    local ok, hp = pcall(function() return h.Hp end)
    if ok and hp == 0 then return "zero_hp" end
    return nil
end

-- Every member of the side out of the fight (cantAct).
function Ar.wiped(side)
    for _, u in ipairs(Ar.state.teams[side]) do
        if not cantAct(u) then return false end
    end
    return true
end

-- A mirror arena's surfaces read again, report-only (does a cleared fire come back mid-match? cross-review 7828a6f8):
-- the setup's grid out to the clear's reach, recorded as arena_surfaces {when, round, tiles_total, tiles_read,
-- radius, off, tiles, appeared, appeared_tiles}: off/tiles = mismatches setup already read (outside the checked
-- radius), appeared/appeared_tiles = tiles whose mirror reads otherwise only since setup (a fire or grease made in the
-- fight, or one that came back), not a setup mismatch.
function Ar.reread_surfaces(when)
    local st = Ar.state
    local sf = st and st.surfaces
    if not (sf and Ar.Setup) then return end
    local ok, r = pcall(Ar.Setup.check_surfaces, sf.spawns, sf.n, nil, Ar.Setup.surface_reach(sf.spawns))
    if not ok then
        record("arena_surfaces", { when = when, round = st.round, error = tostring(r) })
        return
    end
    local old, new = {}, {}
    for i, t in ipairs(r.off) do
        if (sf.setup_keys or {})[r.off_keys[i]] then old[#old + 1] = t else new[#new + 1] = t end
    end
    record("arena_surfaces", { when = when, round = st.round, tiles_total = r.points, tiles_read = r.read,
        radius = r.radius, off = #old, tiles = { table.unpack(old, 1, 5) }, appeared = #new,
        appeared_tiles = { table.unpack(new, 1, 5) } })
end

function Ar.finish(winner, reason, detail)
    local st = Ar.state
    if st.ended then return st.ended end
    st.ended = { winner = winner, reason = reason, rounds = st.round, hp_left_A = hpLeft("A"), hp_left_B = hpLeft("B"),
        detail = detail, t = now_ms() }
    Ar.reread_surfaces("end") -- before arena_end: the runner's reader takes no arena record after it
    record("arena_end", { winner = winner, reason = reason, rounds = st.round, hp_left_A = st.ended.hp_left_A,
        hp_left_B = st.ended.hp_left_B, detail = detail })
    return st.ended
end

-- A character on no team in the arena's combat ends the match as a fault at once (the turn order's props are not
-- characters). Live 2026-11-02 (live 9): the avatar, sitting out 21 m away, joined ~3 min in when a member came ~18 m
-- from him; the game's turn sat on him, nobody ends a sitter's turn, and the match stalled until the runner faulted it.
-- True if it ended the match.
local function outsider(u)
    if not u or Ar.side_of(u) or not truthy(osi(Osi.IsCharacter, u)) then return false end
    Ar.finish(nil, "fault", tostring(V.name(u)) .. " (not on a team) joined the fight")
    return true
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
    st.round = st.round or 1 -- a turn before the match's first round is known is round 1's
    st.turn = { side = s, uuid = u, round = st.round }
    Ar.check_end()
end

-- The acting phase changed since the last check (another round, group or character): a turn boundary (a wipe ends
-- the match), then one arena_turn {side, character, members (that one character), names (its display name), round,
-- phase (1-based within the group), group (the group's Team guid, else its first member), shared (the group holds
-- both sides), order (turn_order), opening (the round's opening side), rule ("opening" | "alternate" |
-- "only_side_left": why this side)}. On the server
-- tick, at most every PHASE_EVERY_MS: the per-member TurnStarted burst of a group (all within a few ms) is over by
-- then, and so is any change no listener sees (an end, a downing, a status). A phase shorter than that between two
-- checks is not seen.
Ar.PHASE_EVERY_MS = 250

-- The acting phase's members who can't act get their turn ended by the mod, one arena_turn_passed record each, so
-- the phase closes and the next one starts. Live 2026-11-02 (live 7 rerun 3): round 2's phase 1 was A [Gale], downed
-- in round 1; the game kept her turn open (no EndTurn of its own) and her phase held the rest of the group: 29.5
-- min to the wall cap. The end is paired as act.end_turn's: Osiris EndTurn and a pending end (G.end_requested,
-- cleared by its TurnEnded), which counts as ended for the phases and the group's last end. A phase passed whole is
-- the match's phase all the same (its number taken, no arena_turn): live 2026-11-02 (live 7 rerun 2), round 2 B
-- [Karlach] | A [Lae'zel] passed | B [Minthara, Wyll] read as one B phase, and the last one got no record.
local function samePhase(st, round, p) -- p is the phase st.phase holds (its character still acting)
    local last = st.phase
    return last ~= nil and last.round == round and last.group == p.group and last.side == p.side
        and last.character == p.character
end

local function nextPhase(st, round, p)
    local last = st.phase
    return (last and last.round == round and last.group == p.group) and last.phase + 1 or 1
end

-- A round's first phase, once per round: arena_round {round, first (the round's opening side, turn_order's: the
-- first phase's side unless that side had nobody open), how ("turn": it was recorded as an
-- arena_turn; "passed": every member of it couldn't act and was passed)}. The audit for a turn-order rule that sets
-- who moves first each round (seat fix O3/O3a, ops/briefs/arena/seat-fix.md); the first arena_turn of a round alone
-- misses a first phase passed whole (seed-7 m1 r2: Cedar passed, B's arena_turn came first).
local function noteRoundFirst(st, round, side, how)
    st.round_first = st.round_first or {}
    if st.round_first[round] then return end
    st.round_first[round] = side
    record("arena_round", { round = round, first = side, how = how })
end

-- A downed member's death save comes with its turn (window 8 seed 7: arena_turn_passed 146.4 s, the save 146.6 s;
-- 0.2-0.6 s after the pass in every case). A third success removes DOWNED with 1 HP and the game resets the turn
-- (Status_DOWNED OnRemoveFunctors ResetCombatTurn), so a pass sent first can take that turn away. A downed member is
-- passed once a roll of its own is in the roll log after its TurnStarted, or DEATH_SAVE_WAIT_MS after that start
-- (bounded; the pass record says which). Downed later in its turn (its start long past): passed at once.
Ar.DEATH_SAVE_WAIT_MS = 1500

local function saveSince(u, t0)
    local L, G = BG3AI.RollLog, BG3AI.Guard
    local g = G.guid(u) or u
    for i = #(L and L.ring or {}), 1, -1 do
        local r = L.ring[i]
        if r.t < t0 then break end
        if r.roller and (G.guid(r.roller) or r.roller) == g then return r end
    end
    return nil
end

-- "wait" while the downed member's save may still come this turn; else the save record (or false: none).
local function deathSave(u)
    local G = BG3AI.Guard
    local s = G.turn_start[G.guid(u) or u]
    if not s then return false end
    local r = saveSince(u, s.t)
    if r then return r end
    if now_ms() - s.t < Ar.DEATH_SAVE_WAIT_MS then return "wait" end
    return false
end

local function passCantAct(p, round)
    local G = BG3AI.Guard
    local passed, n, last, waiting = false, 0, nil, false
    for _, u in ipairs(p.members) do
        local why = cantAct(u)
        local save
        if why == "downed" then
            save = deathSave(u)
            if save == "wait" then waiting, why = true, nil end
            if save and save ~= "wait" then why = cantAct(u) end -- the save may have stood them up
        end
        if why then
            if not passed then
                Ar.check_end() -- a side all downed or dead: the match is over, nobody's turn to pass
                if Ar.state.ended then return false end
            end
            local ok, err = pcall(Osi.EndTurn, u)
            G.end_requested[G.guid(u) or u] = now_ms()
            record("arena_turn_passed", { side = p.side, character = u, name = V.name(u), why = why, round = round,
                error = (not ok) and tostring(err) or nil,
                death_save = why == "downed" and (save and "rolled" or "none in time") or nil,
                order = Ar.state.turn_order, opening = p.opening, rule = p.rule })
            Ar.state.passed = Ar.state.passed or {}
            Ar.state.passed[G.guid(u) or u] = round
            passed, n, last = true, n + 1, u
        end
    end
    -- The pass is an end the mod sends: the group's last one gets the group's ends written as act.end_turn's would
    -- (Ar.write_group_end: nothing unless every other member's end is the mod's). Live smoke 2026-11-03 (m1 r1, L3):
    -- Ash, the twins' group's last open member, passed with no write; the game never closes that group on its own, so
    -- 30 s later (Guard END_DROP_MS) Birch's requested end read as one the game dropped and his turn reopened.
    if last then Ar.write_group_end(last) end
    local st = Ar.state
    if n == #p.members and not samePhase(st, round, p) then
        st.phase = { round = round, group = p.group, side = p.side, phase = nextPhase(st, round, p),
            character = p.character, members = p.members, rule = p.rule, opening = p.opening }
        noteRoundFirst(st, round, p.opening, "passed")
    end
    return passed, waiting
end

-- A member passed this round that stands up (a third death save success: DOWNED gone, HP > 0) with its turn open
-- again (the game's ResetCombatTurn): the pass is withdrawn so its phase takes it, one arena_turn_readmitted record.
local function readmit(round)
    local G, st = BG3AI.Guard, Ar.state
    for g, r in pairs(st.passed or {}) do
        if r ~= round then
            st.passed[g] = nil
        elseif not cantAct(g) then
            local active, req = G.tb_flags(g)
            if active and not req then
                st.passed[g] = nil
                G.end_requested[g], G.end_alone[g], G.end_confirmed[g] = nil, nil, nil
                record("arena_turn_readmitted", { side = Ar.side_of(g), character = g, name = V.name(g), round = round,
                    why = "stood up after its turn was passed" })
            end
        end
    end
end

-- BG3 stands a prone creature up at the start of its turn for half its movement (it stays down with less). In a
-- shared group every member's turn starts in the round's burst, before any phase, so one knocked down by an earlier
-- phase of the same round would lie through its own phase (REVIEW-12 (a); Guard refuses its casts and walks, F4): the
-- mod stands it up at its phase start instead. PRONE_DEDUCT: the mod takes the half itself, because the Lua removal
-- costs nothing (w10 L6 ST-P5(f): Osi.RemoveStatus left 9/9 at +0.4, +1.4 and +3.5 s), while the game's own stand-up
-- in the round's burst leaves 4.5/9 (ST-P5(e)). A member the burst already stood up is not prone at its phase start:
-- nothing is taken twice. Each stand-up: one arena_stood_up {side, character, name, round, status, movement_before,
-- movement_after, movement_max, deducted (what the mod took), game_took}.
Ar.PRONE_DEDUCT = true

local function standUp(u, round)
    local G = BG3AI.Guard
    local prone = G.in_combat(u) and G.prone(u)
    if not prone then return end
    local amount, max = G.movement(u)
    local half = max and max / 2
    if not (amount and half and amount >= half) then
        Ext.Utils.Print(string.format("[BG3AI] arena: %s stays %s (movement %s of %s)", tostring(V.name(u)),
            tostring(prone[1].name), tostring(amount), tostring(max)))
        return
    end
    for _, d in ipairs(prone) do pcall(Osi.RemoveStatus, u, d.id) end
    local after = G.movement(u)
    local took = after ~= nil and amount - after >= half - 0.01
    local deducted = 0
    if Ar.PRONE_DEDUCT and not took and G.deduct(u, "Movement", 0, half) then deducted = half end
    record("arena_stood_up", { side = Ar.side_of(u), character = u, name = V.name(u), round = round,
        status = prone[1].name, movement_before = amount, movement_after = G.movement(u), movement_max = max,
        deducted = deducted, game_took = took })
end

function Ar.phase_check()
    if not Ar.active() then return end
    local st = Ar.state
    if st.ended then return end
    local t = now_ms()
    if st.phaseAt and t - st.phaseAt < Ar.PHASE_EVERY_MS then return end
    st.phaseAt = t
    local gs = turnGroups()
    for _, g in ipairs(gs or {}) do
        for _, u in ipairs(g.members) do
            if outsider(u) then return end
        end
    end
    local round = st.round or 1
    readmit(round)
    local p = Ar.phase(gs)
    if not p then return end
    while p do -- each pass leaves those members' turns not open
        local passed, waiting = passCantAct(p, round)
        if waiting then return end -- a downed member's death save may still come: the phase is held, not recorded
        if not passed then break end
        p = Ar.phase()
    end
    if not p or st.ended then return end
    if samePhase(st, round, p) then return end
    local n = nextPhase(st, round, p)
    st.phase = { round = round, group = p.group, side = p.side, phase = n, character = p.character,
        members = p.members, rule = p.rule, opening = p.opening }
    if st.forced then st.forced[p.side] = nil end -- its new phase: the forced one is over
    Ar.check_end()
    if st.ended then return end
    local names = {}
    for i, u in ipairs(p.members) do names[i] = V.name(u) end
    noteRoundFirst(st, round, p.opening, "turn")
    record("arena_turn", { side = p.side, character = p.character, members = p.members, names = names, round = round,
        phase = n, group = p.group, shared = p.shared, order = st.turn_order, opening = p.opening, rule = p.rule })
    standUp(p.character, round)
end

-- The runner ended a side's turn at its cap (act.end_turn {forced = which cap}): the side is told once per phase, as
-- a fact, by a turn_forced {uuid, name, why} event of its own, and its out-of-phase refusals name it until its next
-- phase (Guard.side_gate).
function Ar.note_forced(char, why)
    local st, s = Ar.state, Ar.side_of(char)
    if not (st and s) then return end
    st.forced = st.forced or {}
    if st.forced[s] then return end
    st.forced[s] = { name = V.name(char), why = why }
    U.with_side(s, RPC.push, "turn_forced", { uuid = char, name = V.name(char), why = why })
end

-- The game paused for a reaction decision: a member carries the pause component (live: all 8 of them, gone within 0.1 s
-- of the answer).
function Ar.game_held()
    for _, team in pairs(Ar.state and Ar.state.teams or {}) do
        for _, u in ipairs(team) do
            if BG3AI.Actions.cast_paused(u) then return true end
        end
    end
    return false
end

-- A shared turn group's last end that the game ignored (act.end_turn, arena only): live 14 (2026-11-02, 2v2 twins in
-- one engine group) every member had its EndTurn and the game changed no flag (all four active, none
-- RequestedEndTurn) and never closed the group; writing TurnBased.RequestedEndTurn = true + Replicate("TurnBased") on
-- the four closed it at once (round 2 opened in the same burst). Written only when every other member of char's
-- engine turn group is a team member with an end the mod sent or a pass of the mod's (G.end_requested), on each
-- member that still reads active and not requested, char included; recorded as arena_group_end_written. -> the members
-- written, or nil (nothing written: no arena, the group doesn't read, char alone in it, or a member the mod didn't
-- end).
function Ar.write_group_end(char)
    if not Ar.active() then return nil end
    local G, st = BG3AI.Guard, Ar.state
    local c = G.guid(char) or char
    local group
    for _, g in ipairs(turnGroups() or {}) do
        for _, u in ipairs(g.members) do
            if u == c then group = g end
        end
    end
    if not group or #group.members < 2 then return nil end
    for _, u in ipairs(group.members) do
        if u ~= c and not (Ar.side_of(u) and G.end_requested[u]) then return nil end
    end
    local out, names, errors = {}, {}, {}
    for _, u in ipairs(group.members) do
        local active, req = G.tb_flags(u)
        if active and not req then
            local ok, err = pcall(function()
                local e = V.entity(u)
                e.TurnBased.RequestedEndTurn = true
                e:Replicate("TurnBased")
            end)
            out[#out + 1], names[#names + 1] = u, V.name(u)
            if not ok then errors[u] = tostring(err) end
        end
    end
    record("arena_group_end_written", { round = st.round, group = group.team or group.members[1], character = c,
        members = out, names = names, errors = next(errors) and errors or nil, how = "requested_end_write" })
    return out
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
    if round == 2 and not st.reread_round2 then
        st.reread_round2 = true
        Ar.reread_surfaces("round 2")
    end
end

-- The arena's combat ends: a wipe ends the match; any other end is recorded for the runner (it ends the match).
function Ar.combat_ended(combat)
    if not Ar.active() then return end
    local st = Ar.state
    if st.ended or combat == nil or combat ~= st.combat then return end
    Ar.check_end()
    if not st.ended then record("arena_combat_ended", { rounds = st.round }) end
end

-- Someone enters the arena's combat: anyone on no team ends the match (outsider).
function Ar.entered_combat(char, combat)
    if not Ar.active() then return end
    local st = Ar.state
    if st.ended or not arenaCombat(combat) then return end
    outsider(V.uuid(char))
end

if not Ar.listening then
    Ar.listening = true
    for name, arity in pairs({ TurnStarted = 1, CombatRoundStarted = 2, CombatEnded = 1, EnteredCombat = 2 }) do
        local ok, err = pcall(Ext.Osiris.RegisterListener, name, arity, "after", function(a, b)
            local ok2, err2 = pcall(function()
                if name == "TurnStarted" then
                    Ar.turn_started(a)
                elseif name == "CombatRoundStarted" then
                    Ar.round_started(a, b)
                elseif name == "EnteredCombat" then
                    Ar.entered_combat(a, b)
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
        ok, err = pcall(Ar.hud_check)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] arena HUD: " .. tostring(err)) end
    end)
end

-- L5 reads (live.md 01:03:22): a twin's reaction prompt shows only while a twin holds the HUD: with the host selected
-- the game held all 8 and no client showed it; on a portrait click it came up at once, whichever twin that was. So
-- the HUD stays on a living team member. It isn't the reactor's: any twin showed it, and a side's own select still
-- moves it between twins.
Ar.HUD_CHECK_MS = 1000
function Ar.hud_check()
    if not Ar.active() then return end
    local G = BG3AI.Guard
    local now = G.now_ms()
    if Ar.hud_at and now - Ar.hud_at < Ar.HUD_CHECK_MS then return end
    Ar.hud_at = now
    local first
    for _, s in ipairs(Ar.SIDES) do
        for _, u in ipairs(Ar.state.teams[s] or {}) do
            if not G.is_dead(u) then
                local ok, cc = pcall(function() return V.entity(u).ClientControl end)
                if ok and cc ~= nil then return end
                first = first or u
            end
        end
    end
    if first then G.osi("MakePlayerActive", first) end
end

-- The round of the combat u is in now: CombatRoundStarted's (G.round_of), else the engine's turn order; nil out of
-- combat.
local function roundNow(u)
    local r = BG3AI.Guard.round_of(u)
    if r then return r end
    local ok, v = pcall(function() return V.entity(u).CombatParticipant.CombatHandle.TurnOrder.Round end)
    return ok and tonumber(v) or nil
end

-- Begin a match with these teams ({A = {uuids}, B = {uuids}}): the setup op calls it once the teams stand, in
-- combat. The match's round starts as that combat's (live 2026-11-02: setup's combat call fires round 1's
-- CombatRoundStarted and first TurnStarted before this), then follows CombatRoundStarted; nil until known. The
-- client gets the team uuids and display names. opts.round_cap: the rounds a match lasts at most (a draw after).
-- opts.sitters: {uuid = its CanJoinCombat before setup} for the characters setup kept out of the fight; arena.reset
-- gives them that back. opts.positions: {uuid = {x, y, z}} where setup's members stand, recorded in arena_start.
-- opts.surfaces: a mirror arena's {spawns, n} for the report-only surface re-reads (Ar.reread_surfaces).
-- opts.field: a mirror arena's field clean counts (Server/ArenaField.lua), recorded in arena_start.
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
    local combat = osi(Osi.CombatGetGuidFor, t.A[1])
    local order = opts and opts.turn_order or "alternate"
    if not Ar.TURN_ORDERS[order] then error("turn_order must be \"alternate\" or \"snake\"", 0) end
    Ar.state = { teams = t, names = names, combat = combat, round = combat and roundNow(t.A[1]) or nil,
        started = now_ms(), round_cap = opts and tonumber(opts.round_cap) or nil, sitters = opts and opts.sitters,
        seed = opts and tonumber(opts.seed) or nil, surfaces = opts and opts.surfaces or nil, turn_order = order }
    broadcast({ teams = t, names = names })
    record("arena_start", { teams = t, positions = opts and opts.positions or nil, turn_order = order,
        field = opts and opts.field or nil })
    return Ar.status()
end

function Ar.status()
    local st = Ar.state
    if not st then return { active = false, teams = nil, turn = nil, hp = nil, ended = nil } end
    -- hp: each team's summed current HP over its summed max (the true values: harness only); round: the match's
    -- (nil before its combat); turn: the last phase recorded {side, uuid (its first member), members, round, phase},
    -- nil before the first. Not the last TurnStarted: a shared group's burst ends on its last member, whichever side
    -- (live 10 m1 a2: the turn read Wyll, B, while A's Lae'zel phase held the group).
    local ph = st.phase
    local turn = ph and { side = ph.side, uuid = ph.character, members = ph.members, round = ph.round,
        phase = ph.phase } or nil
    -- lost_control: the game runs this phase's character (Guard.lost_control), {name = its status} (the runner's clock)
    if turn then turn.lost_control = BG3AI.Guard.lost_control(ph.character) end
    return { active = Ar.active(), teams = st.teams, round = st.round, turn = turn,
        hp = { A = hpLeft("A"), B = hpLeft("B") }, ended = st.ended }
end

function Ar.reset()
    for u, can in pairs(Ar.state and Ar.state.sitters or {}) do pcall(Osi.SetCanJoinCombat, u, can) end
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

-- The runner's fact for one side (a prompt it left unanswered declined, a turn charged for that): its event alone.
op("arena.notice", function(args)
    if not (args.side and args.text) then error("arena.notice needs side and text", 0) end
    U.with_side(args.side, RPC.push, "arena_notice", { text = tostring(args.text) })
    return { ok = true }
end)

-- The runner's end (caps, forfeits, faults): {reason, winner?}; a match the mod already ended keeps that end, and so
-- does a wipe already there that no turn boundary has checked yet (a side downed mid-phase, then the runner's cap or
-- forfeit: the wipe is the end, not the runner's reason).
op("arena.end", function(args)
    if not Ar.state then error("no arena", 0) end
    local w = args.winner
    if w ~= nil and w ~= "A" and w ~= "B" then error("winner must be A or B", 0) end
    Ar.check_end()
    Ar.finish(w, tostring(args.reason or "aborted"))
    return Ar.status()
end)

op("arena.reset", function() return Ar.reset() end)

-- A side's answer to a box on the one shared screen (the bridge's confirm / dismiss_popups ask first; a side's op, not
-- the harness's): a box names no owner, and only the acting side acts, so a box on screen is the acting side's.
-- Refused with Guard.side_gate's text.
RPC.register("guard.side_gate", function()
    local G = BG3AI.Guard
    local ok, e = pcall(G.side_gate)
    if not ok then error(G.is_refusal(e) and e.reason or e, 0) end
    return { ok = true }
end)

return Ar
