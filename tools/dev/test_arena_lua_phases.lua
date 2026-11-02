-- Part of test_arena_lua.lua (split for the 1000-line cap): team phases. The game puts adjacent player characters of
-- both teams into one shared turn group (live spike: all 8 in one group); the arena plays it as phases, one same-side
-- run of the group's open members each, one arena_turn record per phase from the server tick.
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local function openAll(us)
    for _, u in ipairs(us) do setTurn(u, true, false) end
end

local function burst(us) -- the game's TurnStarted for each member of a group, all at once
    for _, u in ipairs(us) do fire("TurnStarted", "S_Player_" .. u) end
end

local function members(r) return table.concat(r.members, ",") end

test("a group B|AA|B: 3 phases in order, one arena_turn each, none from the TurnStarted burst", function()
    begin()
    fire("CombatRoundStarted", "combat1", 2)
    order = { { B1, A1, A2, B2 } }
    openAll({ B1, A1, A2, B2 })
    burst({ B1, A1, A2, B2 })
    eq(#refs("arena_turn"), 0, "no record per TurnStarted")
    tick()
    local r = refs("arena_turn")
    eq(#r, 1, "phase 1 recorded")
    eq(r[1].side, "B", "phase 1 side")
    eq(r[1].character, B1, "phase 1 character")
    eq(members(r[1]), B1, "phase 1 members")
    eq(r[1].phase, 1, "phase 1 index")
    eq(r[1].round, 2, "round")
    eq(r[1].group, B1, "group: its first member (no Team read)")
    eq(r[1].shared, true, "shared")
    eq(Ar.held(A1), true, "A1 waits")
    eq(Ar.held(B2), true, "B2, a later run of B, waits")
    refused(function() as("B", G.combat_gate, B2, "turn") end, "it is not this character's turn")
    tick()
    eq(#refs("arena_turn"), 1, "no change, no record")
    setTurn(B1, true, true) -- B1 ended (the game keeps the group open)
    clock = clock + 1 -- inside the throttle: not checked yet
    for _, f in ipairs(tickers) do f() end
    eq(#refs("arena_turn"), 1, "throttled")
    tick()
    r = refs("arena_turn")
    eq(#r, 2, "phase 2 recorded")
    eq(r[2].side, "A", "phase 2 side")
    eq(members(r[2]), A1 .. "," .. A2, "phase 2 members")
    eq(r[2].phase, 2, "phase 2 index")
    eq(Ar.held(B2), true, "B2 still waits")
    refused(function() as("B", G.combat_gate, B2, "turn") end, "not your turn (side A is acting)")
    setTurn(A1, true, true)
    setTurn(A2, true, true)
    tick()
    r = refs("arena_turn")
    eq(#r, 3, "phase 3 recorded")
    eq(r[3].side, "B", "phase 3 side")
    eq(members(r[3]), B2, "phase 3 members")
    eq(r[3].phase, 3, "phase 3 index")
    eq(Ar.held(B2), false, "B2 acts")
    as("B", G.combat_gate, B2, "turn")
end)

test("single-side groups: one phase each, not shared; the next group starts at phase 1", function()
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    order = { { A1, A2 }, B1, B2 }
    openAll({ A1, A2 })
    burst({ A1, A2 })
    tick()
    setTurn(A1, false, false)
    setTurn(A2, false, false)
    setTurn(B1, true, false)
    fire("TurnStarted", "S_Player_" .. B1)
    tick()
    local r = refs("arena_turn")
    eq(#r, 2, "two records")
    eq(members(r[1]), A1 .. "," .. A2, "A's group, both members")
    eq(r[1].shared, false, "A's group not shared")
    eq(r[1].phase, 1, "A's phase")
    eq(r[2].side, "B", "B1's group")
    eq(r[2].group, B1, "its own group")
    eq(r[2].phase, 1, "a new group: phase 1")
    eq(r[2].shared, false, "B1's group not shared")
end)

test("side A's ends pending through a B phase over END_DROP_MS: A stays done, B keeps the group", function()
    begin()
    order = { { A1, A2, B1, B2 } }
    openAll({ A1, A2, B1, B2 })
    G.end_requested[A1], G.end_requested[A2] = clock, clock -- act.end_turn's requested ends; the game keeps them open
    eq(Ar.acting_side(), "B", "B acts once A ended")
    clock = clock + G.END_DROP_MS + 1000
    eq(Ar.acting_side(), "B", "B still acts after END_DROP_MS")
    eq(G.end_pending(A1), true, "A1's end kept")
    eq(G.is_my_turn(A1), false, "A1's turn")
    eq(G.is_my_turn(B1), true, "B1's turn")
end)

test("a held member's end_turn under its side: 'not your turn (side A is acting)'", function()
    begin()
    order = { { A1, B1 } }
    openAll({ A1, B1 })
    local r = rpc("act.end_turn", "{char=" .. string.format("%q", B1) .. "}", "B")
    eq(enc(r):find("not your turn (side A is acting)", 1, true) ~= nil, true, "B ends held B1: " .. enc(r))
end)

test("a phase change is a turn boundary: a side all down ends the match, no arena_turn after", function()
    begin()
    order = { { A1, B1 } }
    openAll({ A1, B1 })
    osiv[key("IsDead", B1)], osiv[key("IsDead", B2)] = 1, 1
    tick()
    eq(Ar.state.ended and Ar.state.ended.reason, "wipe", "reason")
    eq(Ar.state.ended.winner, "A", "winner")
    eq(#refs("arena_turn"), 0, "no record after the end")
end)

test("a phase's names: each member's display name in members' order, a summon's included", function()
    begin()
    order = { { A1, SA, B1 } }
    openAll({ A1, SA, B1 })
    tick()
    local r = refs("arena_turn")
    eq(#r, 1, "one record")
    eq(members(r[1]), A1 .. "," .. SA, "A1 and its summon")
    eq(table.concat(r[1].names, ","), "Ana,Aspect", "names")
end)

test("a member without a display name: 'Unknown' in names, which stays as long as members", function()
    begin()
    NAMES[A2] = nil
    order = { { A1, A2, B1 } }
    openAll({ A1, A2, B1 })
    tick()
    NAMES[A2] = "Abe"
    local r = refs("arena_turn")[1]
    eq(#r.names, #r.members, "as many names as members")
    eq(table.concat(r.names, ","), "Ana,Unknown", "names")
end)

-- act.end_turn as a side's bridge (or the harness, side nil) sends it; the job it started.
local function endTurn(u, side)
    rpc("act.end_turn", "{char=" .. string.format("%q", u) .. "}", side)
    return RPC.jobs[RPC.nextJob - 1]
end

local function ticks(n) for _ = 1, n do tick() end end

local function has(list, u)
    for _, x in ipairs(list or {}) do if x == u then return true end end
    return false
end

test("live load2: a phase's last end is pending while the other phase is still to act; B's phase opens", function()
    begin()
    order = { { A1, A2, B1, B2 } }
    openAll({ A1, A2, B1, B2 })
    tick()
    local j = endTurn(A2, "A")
    ticks(2)
    eq(j.status, "done", "A2's end")
    eq(j.result.requested, true, "A2's end pending (A1 still to act)")
    j = endTurn(A1, "A") -- the phase's last member: the game keeps the group open for B
    ticks(2)
    eq(j.status, "done", "A1's end: " .. tostring(j.error))
    eq(j.result.requested, true, "A1's end pending")
    eq(has(j.result.still_to_act, B1) and has(j.result.still_to_act, B2), true, "B still to act")
    tick()
    local r = refs("arena_turn")
    eq(#r, 2, "phase 2 recorded")
    eq(r[2].side, "B", "phase 2 side")
    eq(members(r[2]), B1 .. "," .. B2, "phase 2 members")
    eq(Ar.acting_side(), "B", "acting")
    j = endTurn(B1, "B")
    ticks(2)
    eq(j.result and j.result.requested, true, "B1's end pending (B2 still to act)")
    j = endTurn(B2, "B") -- the group's last end: nobody is still to act, the game closes the group
    tick()
    eq(j.status, "running", "B2's end waits for the game")
    for _, u in ipairs({ A1, A2, B1, B2 }) do setTurn(u, false, false) end
    for _, u in ipairs({ A1, A2, B1, B2 }) do fire("TurnEnded", "S_Player_" .. u) end
    tick()
    eq(j.status, "done", "B2's end: " .. tostring(j.error))
    eq(j.result.ended_again, nil, "nobody's EndTurn sent again")
end)

test("live load1: the harness ends a phase of one (A1 | B1 A2 B2): pending, the next phase opens", function()
    begin()
    order = { { A1, B1, A2, B2 } }
    openAll({ A1, B1, A2, B2 })
    tick()
    local j = endTurn(A1, nil)
    ticks(2)
    eq(j.status, "done", "A1's end: " .. tostring(j.error))
    eq(j.result.requested, true, "pending")
    eq(table.concat(j.result.still_to_act, ","), A2 .. "," .. B1 .. "," .. B2, "still to act: the held members too")
    tick()
    local r = refs("arena_turn")
    eq(r[#r].side, "B", "phase 2 side")
    eq(members(r[#r]), B1, "phase 2: B1")
end)

test("a held member is still to act, not its turn", function()
    begin()
    order = { { A1, B1 } }
    openAll({ A1, B1 })
    eq(G.still_to_act(B1), true, "B1 still to act")
    eq(G.is_my_turn(B1), false, "B1's turn")
    eq(G.is_my_turn(A1), true, "A1's turn")
    G.end_requested[A1] = clock
    eq(G.still_to_act(A1), false, "A1 ended")
end)

test("a team phase over END_REQUEST_TTL_MS: the earlier phase's ends stay pending while the group still has B to act",
    function()
    begin()
    order = { { A1, A2, B1, B2 } }
    openAll({ A1, A2, B1, B2 })
    G.end_requested[A1], G.end_requested[A2] = clock, clock -- A's phase ended; the game keeps the group open
    clock = clock + G.END_REQUEST_TTL_MS + 1000            -- B's phase runs past the TTL
    eq(G.end_pending(A1), true, "A1's end kept")
    eq(Ar.acting_side(), "B", "B still acts")
    eq(G.is_my_turn(A1), false, "A1's turn")
    G.end_requested[B1], G.end_requested[B2] = clock, clock -- B ended too: nobody else is still to act
    eq(G.end_pending(A1), false, "the TTL applies again: A1's old end forgotten")
end)

local A = BG3AI.Actions

-- The group's last end (A1's pending, B1 the last still to act); the job of B1's end_turn.
local function lastEnd()
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    order = { { A1, B1 } }
    openAll({ A1, B1 })
    G.end_requested[A1] = clock
    tick()
    eq(Ar.acting_side(), "B", "B's phase")
    return endTurn(B1, "B")
end

test("the group's last end waits for the game past END_IGNORED_MS and the 10 s job limit; the next round ends it",
    function()
    local j = lastEnd()
    ticks(math.floor(18000 / Ar.PHASE_EVERY_MS)) -- S087: 18 s to the group's TurnEnded
    eq(j.status, "running", "still waiting at 18 s: " .. tostring(j.error))
    fire("CombatRoundStarted", "combat1", 2)
    tick()
    eq(j.status, "done", "B1's end: " .. tostring(j.error))
    eq(j.result.ended_again, nil, "nobody's EndTurn sent again in the new round")
end)

test("the group's last end: its own TurnEnded at 5 s ends it", function()
    local j = lastEnd()
    ticks(20)
    fire("TurnEnded", "S_Player_" .. B1)
    eq(j.status, "done", "TurnEnded at 5 s")
end)

test("the group's last end: no close in END_LAST_WAIT_MS fails 'ignored'", function()
    local j = lastEnd()
    ticks(math.floor(A.END_LAST_WAIT_MS / Ar.PHASE_EVERY_MS) - 1)
    eq(j.status, "running", "just short of END_LAST_WAIT_MS")
    ticks(2)
    eq(j.status, "failed", "status")
    eq(j.error, A.END_IGNORED, "error")
end)

-- Live load2 (2026-11-02, phases3): one act.end_turn at a time, the next ~30.5 s later (the driver waited 30 s per end
-- for an engine flag a shared group never changes), B's first phase held 35 s. The game changes no flag on these
-- ends; the group closes only once every member has ended. A1's end was 120 s old during B's phase and A1 reopened:
-- the driver ended the reopened members again (48 ends) and the group never closed (25 min in round 1).
test("live load2 replay: ends 30.5 s apart, B held 35 s: no ended member reopens and the group's last end closes it",
    function()
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    order = { { A1, A2, B1, B2 } }
    openAll({ A1, A2, B1, B2 })
    burst({ A1, A2, B1, B2 })
    tick()
    local function drive(u, side)
        local p = Ar.phase()
        eq(p and p.side, side, "the acting side at " .. u .. "'s end")
        eq(p.members[1], u, "the phase's first member")
        local j = endTurn(u, side)
        ticks(2)
        eq(j.status, "done", u .. "'s end: " .. tostring(j.error))
        eq(j.result.requested, true, u .. "'s end pending")
        ticks(math.floor(30500 / Ar.PHASE_EVERY_MS))
    end
    drive(A1, "A")
    drive(A2, "A")
    ticks(math.floor(35000 / Ar.PHASE_EVERY_MS)) -- B's first phase held
    drive(B1, "B")                              -- A1's end is now over 120 s old
    eq(G.is_my_turn(A1), false, "A1 stays ended")
    local j = endTurn(B2, "B")                  -- the group's last end
    tick()
    eq(j.status, "running", "B2's end waits for the game")
    for _, r in ipairs(refs("arena_turn")) do
        if r.phase > 2 then eq(r.side, "B", "no phase back to A (phase " .. r.phase .. ")") end
    end
    for _, u in ipairs({ A1, A2, B1, B2 }) do setTurn(u, false, false) end
    for _, u in ipairs({ A1, A2, B1, B2 }) do fire("TurnEnded", "S_Player_" .. u) end
    tick()
    eq(j.status, "done", "B2's end: " .. tostring(j.error))
end)
