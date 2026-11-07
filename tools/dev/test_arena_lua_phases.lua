-- Part of test_arena_lua.lua (split for the 1000-line cap): phases. The game puts adjacent player characters of both
-- teams into one shared turn group (live spike: all 8 in one group); the arena plays it as phases of one character,
-- the sides alternating (seat fix D643), one arena_turn record per phase from the server tick.
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local function openAll(us)
    for _, u in ipairs(us) do setTurn(u, true, false) end
end

-- A death save in the roll log (the Osiris RollResult row of the downed member's turn).
local function deathSave(u, success)
    BG3AI.RollLog.add("osi", { roller = u, subject = u, event = "death save", success = success, active = false })
end
local function burst(us) -- the game's TurnStarted for each member of a group, all at once
    for _, u in ipairs(us) do fire("TurnStarted", "S_Player_" .. u) end
end

local function members(r) return table.concat(r.members, ",") end

test("a group B|AA|B plays B1|A1|B2|A2: one character a phase, sides alternating, one arena_turn each", function()
    begin()
    fire("CombatRoundStarted", "combat1", 2)
    order = { { B1, A1, A2, B2 } }
    openAll({ B1, A1, A2, B2 })
    burst({ B1, A1, A2, B2 })
    eq(#refs("arena_turn"), 0, "no record per TurnStarted")
    tick()
    local r = refs("arena_turn")
    eq(#r, 1, "phase 1 recorded")
    eq(enc({ r[1].side, r[1].character, members(r[1]), r[1].phase, r[1].round, r[1].group, r[1].shared }),
        enc({ "B", B1, B1, 1, 2, B1, true }), "phase 1: B1 alone")
    eq(enc({ r[1].order, r[1].opening, r[1].rule }), enc({ "alternate", "B", "opening" }), "why B")
    local rounds = refs("arena_round") -- seat audit: the round's opening side, once
    eq(#rounds, 1, "one arena_round")
    eq(rounds[1].round .. rounds[1].first .. rounds[1].how, "2Bturn", "round 2 opened by B's phase")
    eq(Ar.held(A1), true, "A1 waits")
    eq(Ar.held(B2), true, "B2 waits")
    refused(function() as("B", G.combat_gate, B2, "turn") end, "it is not this character's turn")
    tick()
    eq(#refs("arena_turn"), 1, "no change, no record")
    setTurn(B1, true, true) -- B1 ended (the game keeps the group open)
    clock = clock + 1 -- inside the throttle: not checked yet
    for _, f in ipairs(tickers) do f() end
    eq(#refs("arena_turn"), 1, "throttled")
    tick()
    r = refs("arena_turn")
    eq(enc({ #r, r[2].side, members(r[2]), r[2].phase, r[2].rule }), enc({ 2, "A", A1, 2, "alternate" }),
        "phase 2: A1, not A1+A2")
    eq(Ar.held(A2), true, "A2 waits for B2")
    refused(function() as("A", G.combat_gate, A2, "turn") end, "it is not this character's turn")
    setTurn(A1, true, true)
    tick()
    r = refs("arena_turn")
    eq(enc({ #r, r[3].side, members(r[3]), r[3].phase }), enc({ 3, "B", B2, 3 }), "phase 3: B2")
    eq(Ar.held(B2), false, "B2 acts")
    as("B", G.combat_gate, B2, "turn")
    setTurn(B2, true, true)
    tick()
    r = refs("arena_turn")
    eq(enc({ #r, r[4].side, members(r[4]), r[4].phase }), enc({ 4, "A", A2, 4 }), "phase 4: A2")
    eq(#refs("arena_round"), 1, "still one arena_round in round 2")
end)

test("a round whose first phase is passed whole: arena_round names that side, how passed; the next round its own",
    function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 2)
        order = { { A1, B1, B2, A2 } }
        openAll({ A1, B1, B2, A2 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1 -- seed-7 m1 r2: Cedar down, his phase passed whole
        world[A1].comps.Health.Hp = 0
        burst({ A1, B1, B2, A2 })
        deathSave(A1, true)
        tick()
        local turns = refs("arena_turn")
        eq(#turns, 1, "B's phase recorded")
        eq(turns[1].side, "B", "the first arena_turn is B's")
        local rounds = refs("arena_round")
        eq(#rounds, 1, "one arena_round")
        eq(rounds[1].round .. rounds[1].first .. rounds[1].how, "2Apassed", "round 2's first phase was A's, passed")
        local p = refs("arena_turn_passed")[1] -- a passed slot counts in the alternation: the facts say where
        eq(enc({ p.character, p.order, p.opening, p.rule }), enc({ A1, "alternate", "A", "opening" }), "passed slot")
        eq(enc({ turns[1].character, turns[1].rule }), enc({ B1, "alternate" }), "B1 after A1's passed slot")
        fire("CombatRoundStarted", "combat1", 3)
        osiv[key("HasActiveStatus", A1, "DOWNED")] = nil
        world[A1].comps.Health.Hp = 5
        openAll({ A1, B1, B2, A2 })
        burst({ A1, B1, B2, A2 })
        tick()
        rounds = refs("arena_round")
        eq(#rounds, 2, "round 3 has its own")
        eq(rounds[2].round .. rounds[2].first .. rounds[2].how, "3Aturn", "round 3 opened by A's phase")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

test("single-side groups: one character a phase, not shared; the next group starts at phase 1", function()
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
    eq(members(r[1]), A1, "A's group: A1 first, alone")
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

-- Window 6 seed-5 m2: B's Sneak Attack on downed Ash ran 9 s after arena_end (and rolled)
test("once the match has ended, every action a side asks is refused as over; the harness's still runs", function()
    begin()
    order = { { B1, A1 } } -- B acting: without the gate its cast would run
    openAll({ A1, B1 })
    Ar.finish("A", "wipe")
    for _, c in ipairs({ { "act.cast", "{char=" .. string.format("%q", B1) .. ",spell=\"Target_SneakAttack\"}" },
        { "act.end_turn", "{char=" .. string.format("%q", B1) .. "}" },
        { "act.move_to", "{char=" .. string.format("%q", B1) .. ",pos={1,0,1}}" } }) do
        local r = rpc(c[1], c[2], "B")
        eq(enc(r):find("the match is over (wipe, side A won)", 1, true) ~= nil, true, c[1] .. ": " .. enc(r))
    end
    refused(function() as("A", G.side_gate) end, "the match is over (wipe, side A won)")
    local r = rpc("act.end_turn", "{char=" .. string.format("%q", B1) .. "}")
    eq(enc(r):find("the match is over", 1, true), nil, "the harness's end_turn: " .. enc(r))
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

test("a phase's names: its character's display name; a summon's turn is a phase of its side", function()
    begin()
    order = { { A1, SA, B1 } }
    openAll({ A1, SA, B1 })
    tick()
    local r = refs("arena_turn")
    eq(enc({ #r, members(r[1]), table.concat(r[1].names, ",") }), enc({ 1, A1, "Ana" }), "A1")
    setTurn(A1, true, true)
    tick()
    setTurn(B1, true, true)
    tick()
    r = refs("arena_turn")
    eq(enc({ #r, r[3].side, members(r[3]), table.concat(r[3].names, ","), r[3].rule }),
        enc({ 3, "A", SA, "Aspect", "alternate" }), "the summon's phase after B1")
end)

test("a member without a display name: 'Unknown' in names, which stays as long as members", function()
    begin()
    NAMES[A1] = nil
    order = { { A1, A2, B1 } }
    openAll({ A1, A2, B1 })
    tick()
    NAMES[A1] = "Ana"
    local r = refs("arena_turn")[1]
    eq(#r.names, #r.members, "as many names as members")
    eq(table.concat(r.names, ","), "Unknown", "names")
end)

-- act.end_turn as a side's bridge (or the harness, side nil) sends it; the job it started.
local function endTurn(u, side)
    rpc("act.end_turn", "{char=" .. string.format("%q", u) .. "}", side)
    return RPC.lastJob
end

local function ticks(n) for _ = 1, n do tick() end end

local function has(list, u)
    for _, x in ipairs(list or {}) do if x == u then return true end end
    return false
end

test("live load2: each end is pending while the rest is still to act; A1|B1|A2|B2, the last end closes", function()
    begin()
    order = { { A1, A2, B1, B2 } }
    openAll({ A1, A2, B1, B2 })
    tick()
    local j = endTurn(A2, "A")
    ticks(2)
    eq(j.status, "failed", "A2 is held: A1 opens") -- one character a phase: A2 after B1
    for _, d in ipairs({ { A1, "A" }, { B1, "B" }, { A2, "A" } }) do
        eq(Ar.acting_side(), d[2], "acting at " .. d[1])
        j = endTurn(d[1], d[2])
        ticks(2)
        eq(j.status, "done", d[1] .. "'s end: " .. tostring(j.error))
        eq(j.result.requested, true, d[1] .. "'s end pending")
        eq(has(j.result.still_to_act, B2), true, "B2 still to act after " .. d[1])
        tick()
    end
    local r = refs("arena_turn")
    local got = {}
    for _, x in ipairs(r) do got[#got + 1] = x.side .. ":" .. members(x) end
    eq(table.concat(got, " "), "A:" .. A1 .. " B:" .. B1 .. " A:" .. A2 .. " B:" .. B2, "the phases")
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
    ticks(math.floor(35000 / Ar.PHASE_EVERY_MS)) -- B's first phase held
    drive(B1, "B")
    drive(A2, "A")                              -- A1's end is now over 120 s old
    eq(G.is_my_turn(A1), false, "A1 stays ended")
    local j = endTurn(B2, "B")                  -- the group's last end
    tick()
    eq(j.status, "running", "B2's end waits for the game")
    local sides = ""
    for _, r in ipairs(refs("arena_turn")) do sides = sides .. r.side end
    eq(sides, "ABAB", "no reopened member's phase")
    for _, u in ipairs({ A1, A2, B1, B2 }) do setTurn(u, false, false) end
    for _, u in ipairs({ A1, A2, B1, B2 }) do fire("TurnEnded", "S_Player_" .. u) end
    tick()
    eq(j.status, "done", "B2's end: " .. tostring(j.error))
end)

-- Live 2026-11-02 phases4 (every round): when the group's last end closes the round the game fires CombatRoundStarted,
-- the new round's TurnStarted for all members, then the old round's TurnEnded for them, in one ~10 ms burst.
test("live round change: new TurnStarted before the old TurnEnded: round 2's first phase opens at once", function()
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    order = { { A1, B1, A2, B2 } }
    openAll({ A1, B1, A2, B2 })
    burst({ A1, B1, A2, B2 })
    tick()
    for _, x in ipairs({ { A1, "A" }, { B1, "B" }, { A2, "A" } }) do
        local j = endTurn(x[1], x[2])
        ticks(3)
        eq(j.result and j.result.requested, true, x[1] .. "'s end pending")
    end
    local j = endTurn(B2, "B") -- the group's last end
    -- live smoke 2026-11-03 (L4): every round waited 18 s for the write; now ~1 s after the last end
    ticks(math.floor(1250 / Ar.PHASE_EVERY_MS))
    eq(#refs("arena_group_end_written"), 1, "the group's ends written within 1.25 s (every other end was the mod's)")
    eq(j.status, "running", "waits for the game to close the group")
    fire("CombatRoundStarted", "combat1", 2)
    openAll({ A1, B1, A2, B2 }) -- the new round's turns open, the written flags cleared (live 14 after the write)
    burst({ A1, B1, A2, B2 })
    clock = clock + 6
    for _, u in ipairs({ A1, B1, A2, B2 }) do fire("TurnEnded", "S_Player_" .. u) end -- the game shows all open again
    tick()
    eq(j.status, "done", "B2's end: " .. tostring(j.error))
    eq(j.result.still_to_act, nil, "round 2's open members are not this group's still to act")
    eq(enc(j.result.round_moved_on), enc({ from = 1, to = 2 }), "round_moved_on")
    eq(#refs("arena_group_end_written"), 1, "one write")
    for _, u in ipairs({ A1, B1, A2, B2 }) do eq(G.turn_is_over(u), false, u .. "'s new turn not over") end
    eq(G.is_my_turn(A1), true, "A1 acts")
    local r = refs("arena_turn")
    eq(r[#r].round, 2, "a round 2 record")
    eq(r[#r].phase, 1, "phase 1")
    eq(members(r[#r]), A1, "A1's phase")
end)

-- Live 2026-11-02 (live 7 rerun 3): round 2's phase 1 = A [Gale], downed in round 1; the game left her turn open (no
-- EndTurn of its own), her phase held everyone else for 29.5 min.
test("a downed member's phase: the mod ends its turn, recorded, and the next phase starts; a dead one's too", function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 2)
        order = { { A1, B1, A2, B2 } }
        openAll({ A1, B1, A2, B2 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
        world[A1].comps.Health.Hp = 0
        osiv[key("IsDead", B1)] = 1
        burst({ A1, B1, A2, B2 })
        deathSave(A1, false) -- its turn's death save (window 8: 0.2-0.6 s after the start)
        tick()
        eq(table.concat(ended, ","), A1 .. "," .. B1, "EndTurn for the downed A1, then the dead B1")
        local p = refs("arena_turn_passed")
        eq(#p, 2, "one record each")
        eq(enc({ p[1].side, p[1].character, p[1].name, p[1].why, p[1].round, p[1].death_save }),
            enc({ "A", A1, "Ana", "downed", 2, "rolled" }), "A1's record")
        eq(enc({ p[2].side, p[2].character, p[2].why }), enc({ "B", B1, "dead" }), "B1's record")
        local r = refs("arena_turn")
        eq(#r, 1, "one phase recorded")
        eq(enc({ r[1].side, members(r[1]), r[1].phase }), enc({ "A", A2, 3 }), "A2's phase is round 2's third")
        eq(G.still_to_act(A1), false, "A1 counts as ended")
        eq(Ar.held(A2), false, "A2 acts")
        eq(Ar.held(B2), true, "B2 waits")
        tick()
        eq(#ended, 2, "no second EndTurn")
        eq(#refs("arena_turn_passed"), 2, "no second record")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

test("a side all downed or dead in the acting phase: a wipe ends the match, no turn passed", function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        order = { { A1, A2, B1, B2 } }
        openAll({ A1, A2, B1, B2 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
        world[A1].comps.Health.Hp = 0
        osiv[key("IsDead", A2)] = 1
        burst({ A1, A2, B1, B2 })
        tick()
        eq(Ar.state.ended and Ar.state.ended.winner, "B", "B wins")
        eq(#ended, 0, "no EndTurn")
        eq(#refs("arena_turn_passed"), 0, "no pass")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

-- Live 2026-11-02 (live 7 rerun 2, round 2; group order Astarion, Gale, Karlach, Lae'zel, Minthara, Wyll): Astarion
-- downed in round 1 passed, p1 A [Gale], p2 B [Karlach] (who downs Lae'zel), Lae'zel's p3 passed, then p4 B [Minthara,
-- Wyll] got no arena_turn: the passed A phase left the last record B's, so B's next phase read as the same one.
test("a phase passed whole between two of the other side's: both recorded, the passed one numbered", function()
    local A3 = "aaaaaaaa-0000-0000-0000-0000000000a3"
    local B3 = "bbbbbbbb-0000-0000-0000-0000000000b3"
    for _, u in ipairs({ A3, B3 }) do
        world[u] = { pos = { 2, 0, 0 }, comps = { ServerCharacter = {}, Health = { Hp = 12, MaxHp = 12 } } }
        osiv[key("IsOnStage", u)], osiv[key("IsCharacter", u)] = 1, 1
        osiv[key("CombatGetGuidFor", u)], osiv[key("IsInCombat", u)] = "combat1", 1
        partyRows[#partyRows + 1] = u
    end
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        Ar.begin({ A = { A1, A2, A3 }, B = { B1, B2, B3 } })
        fire("CombatRoundStarted", "combat1", 2)
        order = { { A1, A2, B1, A3, B2, B3 } }
        openAll({ A1, A2, B1, A3, B2, B3 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1 -- Astarion, downed in round 1
        world[A1].comps.Health.Hp = 0
        burst({ A1, A2, B1, A3, B2, B3 })
        deathSave(A1, false)
        tick() -- A1 passed (phase 1), B1's phase 2
        clock = clock + Ar.DEATH_SAVE_WAIT_MS -- the phases take longer than the save's wait
        setTurn(B1, true, true)
        tick() -- A2's phase 3
        osiv[key("HasActiveStatus", A3, "DOWNED")] = 1 -- Gale downs Lae'zel, then ends
        world[A3].comps.Health.Hp = 0
        setTurn(A2, true, true)
        tick() -- B2's phase 4
        setTurn(B2, true, true)
        tick() -- A3 passed (phase 5), B3's phase 6
        local got = {}
        for _, r in ipairs(refs("arena_turn")) do got[#got + 1] = r.side .. r.phase .. "[" .. members(r) .. "]" end
        eq(table.concat(got, " "), "B2[" .. B1 .. "] A3[" .. A2 .. "] B4[" .. B2 .. "] B6[" .. B3 .. "]", "the phases")
        local passed = {}
        for _, r in ipairs(refs("arena_turn_passed")) do passed[#passed + 1] = r.character end
        eq(table.concat(passed, ","), A1 .. "," .. A3, "Astarion's and Lae'zel's turns passed")
        eq(Ar.held(B3), false, "B3 acts")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

test("a character passed in its own phase: that phase's number taken, the other side's next takes the next", function()
    rawset(Osi, "EndTurn", function() end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 2)
        order = { { A1, A2, B1, B2 } }
        openAll({ A1, A2, B1, B2 })
        burst({ A1, A2, B1, B2 })
        tick() -- A1, phase 1
        setTurn(A1, true, true)
        clock = clock + Ar.DEATH_SAVE_WAIT_MS -- its turn started long before: no save to wait for
        tick() -- B1, phase 2
        setTurn(B1, true, true)
        osiv[key("HasActiveStatus", A2, "DOWNED")] = 1 -- downed before its phase
        world[A2].comps.Health.Hp = 0
        tick() -- A2 passed (phase 3), B2's phase 4
        local r = refs("arena_turn")
        eq(#refs("arena_turn_passed"), 1, "A2 passed")
        eq(enc({ r[3].side, members(r[3]), r[3].phase }), enc({ "B", B2, 4 }), "B2's phase is the 4th")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

-- Live 2026-11-02 (live 10, m1 a2, round 3 group Astarion, Gale, Karlach, Lae'zel, Minthara, Wyll): Lae'zel at 0 HP
-- with no DOWNED status (downed and "revived" at once, mid-move) held her phase: the game kept her turn open, rolled
-- no death saving throw, and nobody ended it until the stall fault.
test("a member at 0 HP without DOWNED: its turn is passed as zero_hp and the next phase starts", function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 3)
        order = { { A1, B1, A2, B2 } }
        openAll({ A1, B1, A2, B2 })
        burst({ A1, B1, A2, B2 })
        tick() -- A [A1]
        setTurn(A1, true, true)
        tick() -- B [B1]
        setTurn(B1, true, true)
        world[A2].comps.Health.Hp = 0 -- no DOWNED, not dead
        tick()
        eq(table.concat(ended, ","), A2, "EndTurn for A2")
        local p = refs("arena_turn_passed")
        eq(enc({ #p, p[1] and p[1].character, p[1] and p[1].why, p[1] and p[1].round }), enc({ 1, A2, "zero_hp", 3 }),
            "A2's pass")
        local r = refs("arena_turn")
        eq(enc({ r[#r].side, members(r[#r]) }), enc({ "B", B2 }), "B2's phase comes")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

-- Live 10 m1 a2 at the stall: arena.status's turn read Wyll (B), the group's last TurnStarted, while the phase was
-- A's Lae'zel.
test("arena.status's turn is the recorded phase, not the burst's last TurnStarted", function()
    begin()
    fire("CombatRoundStarted", "combat1", 3)
    eq(rpc("arena.status").result.turn, nil, "no phase yet")
    order = { { A1, B1, A2, B2 } }
    openAll({ A1, B1, A2, B2 })
    burst({ A1, B1, A2, B2 })
    tick() -- A [A1]
    local t = rpc("arena.status").result.turn
    eq(enc({ t.side, t.uuid, enc(t.members), t.round, t.phase }), enc({ "A", A1, enc({ A1 }), 3, 1 }), "A1's phase")
    setTurn(A1, true, true)
    tick()
    t = rpc("arena.status").result.turn
    eq(enc({ t.side, t.uuid, t.phase }), enc({ "B", B1, 2 }), "B1's phase")
end)

-- live.md "## Goaded read": the game runs a Goaded character's turn; the runner reads it here for its clock.
test("arena.status's turn says when the game runs that phase's character (its LoseControlComponent)", function()
    begin()
    fire("CombatRoundStarted", "combat1", 3)
    order = { { A1, B1, A2, B2 } }
    openAll({ A1, B1, A2, B2 })
    burst({ A1, B1, A2, B2 })
    tick() -- A [A1]
    eq(rpc("arena.status").result.turn.lost_control, nil, "A1 played by its side")
    world[A1].comps.StatusLoseControl = { LoseControl = true }
    eq(type(rpc("arena.status").result.turn.lost_control), "table", "A1's turn is the game's")
    world[A1].comps.StatusLoseControl = nil
end)

-- Live check 12 (2026-11-02, 2v2 twins, setup2v2/drive12.jsonl): the group B1 | A1 | B2 | A2, phases of one, one end
-- every ~15.8 s. Ends 1-3 were pending (the rest still to act); the game changed no flag for them (03-turnflags: all
-- four IsActiveCombatTurn, none RequestedEndTurn) and the last end failed 'ignored' at 20 s: round 1 never closed.
-- Live 14: RequestedEndTurn = true + Replicate on the four closed it at once (round 2 in the same burst, then the old
-- round's TurnEnded).
local LIVE12 = { B1, A1, B2, A2 }
local function live12(gameEnds) -- the first three ends as the mod sends them; gameEnds: B2 ended by the game instead
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    order = { LIVE12 }
    openAll(LIVE12)
    burst(LIVE12)
    tick()
    for _, d in ipairs({ { B1, "B" }, { A1, "A" }, { B2, "B" } }) do
        eq(Ar.phase().side, d[2], "the acting side at " .. d[1] .. "'s end")
        if gameEnds and d[1] == B2 then
            setTurn(B2, true, true) -- its end shows in its flags: not one the mod sent
        else
            local j = endTurn(d[1], d[2])
            ticks(2)
            eq(j.status, "done", d[1] .. "'s end: " .. tostring(j.error))
            eq(j.result.requested, true, d[1] .. "'s end pending")
        end
        ticks(math.floor(15800 / Ar.PHASE_EVERY_MS))
    end
    eq(#replicated + #refs("arena_group_end_written"), 0, "no write mid-phase (the rest of the group still to act)")
    return endTurn(A2, "A") -- the game shows nothing: every member stays active, no RequestedEndTurn
end

local function flags()
    local out = {}
    for _, u in ipairs(LIVE12) do out[#out + 1] = tostring(world[u].comps.TurnBased.RequestedEndTurn) end
    return table.concat(out, ",")
end

local function withEnds(fn)
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(fn, ended)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end

test("live check 12 replay: the ignored last end gets the group's ends written once, recorded; round 2 opens",
    function() withEnds(function(ended)
        local j = live12()
        ticks(math.floor(A.END_WRITE_AFTER_MS / Ar.PHASE_EVERY_MS) + 1)
        eq(flags(), "true,true,true,true", "RequestedEndTurn written on all four")
        local rep = {}
        for _, u in ipairs(LIVE12) do rep[#rep + 1] = u .. ":TurnBased" end
        eq(table.concat(replicated, ","), table.concat(rep, ","), "each replicated")
        local w = refs("arena_group_end_written")
        eq(enc({ #w, w[1].round, w[1].character, w[1].how, w[1].errors }), enc({ 1, 1, A2, "requested_end_write" }),
            "recorded")
        eq(enc(w[1].members), enc(LIVE12), "the members written")
        eq(j.status, "running", "waits for the game to close the group")
        fire("CombatRoundStarted", "combat1", 2) -- the game closes it (live 14's burst order)
        openAll(LIVE12)
        burst(LIVE12)
        for _, u in ipairs(LIVE12) do fire("TurnEnded", "S_Player_" .. u) end
        ticks(2)
        eq(j.status, "done", "A2's end: " .. tostring(j.error))
        eq(enc(j.result.round_moved_on), enc({ from = 1, to = 2 }), "round 2")
        eq(enc(j.result.end_written), enc(LIVE12), "the result says the ends were written")
        eq(table.concat(ended, ","), table.concat(LIVE12, ","), "one EndTurn each, in order")
        eq(#refs("arena_group_end_written"), 1, "one write")
    end) end)

test("the write doesn't close the group: the last end fails 'ignored' at 20 s as before; no second write",
    function() withEnds(function()
        local j = live12()
        ticks(math.floor(A.END_LAST_WAIT_MS / Ar.PHASE_EVERY_MS) + 1)
        eq(enc({ j.status, j.error }), enc({ "failed", A.END_IGNORED }), "A2's end")
        eq(enc(j.result.end_written), enc(LIVE12), "the failure says the ends were written")
        eq(#replicated, 4, "one write per member")
        eq(#refs("arena_group_end_written"), 1, "one record")
    end) end)

test("a group member whose end the mod didn't send: nothing written, the last end 'ignored' at 20 s",
    function() withEnds(function()
        local j = live12(true)
        ticks(math.floor(A.END_LAST_WAIT_MS / Ar.PHASE_EVERY_MS) + 1)
        eq(enc({ j.status, j.error }), enc({ "failed", A.END_IGNORED }), "A2's end")
        eq(flags(), "false,false,true,false", "no flag written")
        eq(#replicated + #refs("arena_group_end_written"), 0, "no write, no record")
    end) end)

test("normal play: a shared group's ignored last end writes nothing and fails at 3 s as before",
    function() withEnds(function()
        setTurn(A1, true, false)
        setTurn(A2, true, false)
        G.end_requested[A2] = clock -- A2 ended earlier in the group
        rpc("act.end_turn", "{char=" .. string.format("%q", A1) .. "}")
        local j = RPC.lastJob
        ticks(math.floor(A.END_IGNORED_MS / Ar.PHASE_EVERY_MS) + 1)
        eq(enc({ j.status, j.error }), enc({ "failed", A.END_IGNORED }), "A1's end")
        eq(tostring(world[A1].comps.TurnBased.RequestedEndTurn) .. "," .. tostring(world[A2].comps.TurnBased
            .RequestedEndTurn), "false,false", "no flag written")
        eq(#replicated + #refs("arena_group_end_written"), 0, "no write, no record")
    end) end)

-- Live smoke 2026-11-03 (m1 r1, L3): the group Cedar | Dune, Birch | Ash; Ash downed in B's phase was passed (EndTurn,
-- no act.end_turn job, so no write); the game never closes the twins' group on its own, so 30 s later (END_DROP_MS)
-- Birch's requested end read as one the game dropped and his turn reopened: a 4th phase with his leftover move and
-- bonus action. The pass of the group's last open member writes the group's ends, as its last end_turn would. (This
-- fake doesn't replay the 30 s reopen itself: its group reads a fifth member, so the drop rule never fires here; the
-- write is what fails without the fix.)
test("the group's last open member passed: the group's ends written; no ended member reopens after END_DROP_MS",
    function() withEnds(function(ended)
        begin()
        fire("CombatRoundStarted", "combat1", 1)
        local grp = { A1, B1, B2, A2 }
        order = { grp }
        openAll(grp)
        burst(grp)
        tick()
        for _, d in ipairs({ { A1, "A" }, { B1, "B" }, { A2, "A" } }) do -- one character a phase: A2 before B2
            local j = endTurn(d[1], d[2])
            ticks(2)
            eq(j.status, "done", d[1] .. "'s end: " .. tostring(j.error))
            eq(j.result.requested, true, d[1] .. "'s end pending")
        end
        local phases, sent = #refs("arena_turn"), #ended -- the three act.end_turn EndTurns
        osiv[key("HasActiveStatus", B2, "DOWNED")] = 1 -- downed in A2's phase
        world[B2].comps.Health.Hp = 0
        tick()
        eq(enc({ #ended - sent, ended[#ended] }), enc({ 1, B2 }), "B2's turn passed")
        ticks(math.floor((G.END_DROP_MS + 2000) / Ar.PHASE_EVERY_MS))
        eq(#refs("arena_turn"), phases, "no phase after the pass (B1/A2 never reopen)")
        for _, u in ipairs({ B1, A2 }) do eq(Ar.open(u), false, u .. " not open") end
        local rep = {}
        for _, u in ipairs(grp) do rep[#rep + 1] = u .. ":TurnBased" end
        eq(table.concat(replicated, ","), table.concat(rep, ","), "the group's ends written once, at the pass")
    end) end)

test("a pass with a member still to act after it writes nothing: that member's turn stays open", function()
    withEnds(function()
        begin()
        fire("CombatRoundStarted", "combat1", 1)
        local grp = { B1, B2, A1, A2 }
        order = { grp }
        openAll(grp)
        burst(grp)
        tick()
        local j = endTurn(B1, "B")
        ticks(2)
        eq(j.result and j.result.requested, true, "B1's end pending")
        clock = clock + Ar.DEATH_SAVE_WAIT_MS
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
        world[A1].comps.Health.Hp = 0
        tick()
        eq(#refs("arena_turn_passed"), 1, "A1 passed")
        eq(#replicated, 0, "no write: B2 and A2 are still to act")
        eq(G.is_my_turn(B2), true, "B2 acts (B after A1's passed turn)")
    end)
end)

-- Window 8 seed 7 (arena-fight-analysis.md S3): the pass came 0.2-0.6 s BEFORE the turn's death save. A third success
-- removes DOWNED with 1 HP and the game resets the turn (Status_DOWNED OnRemoveFunctors ResetCombatTurn).
test("a downed member at its turn start: passed only after its death save; a third success keeps its turn", function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 3)
        order = { { A1, A2, B1, B2 } }
        openAll({ A1, A2, B1, B2 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
        world[A1].comps.Health.Hp = 0
        burst({ A1, A2, B1, B2 })
        tick()
        eq(#ended, 0, "no pass before its save"); eq(#refs("arena_turn"), 0, "the phase is held, not recorded")
        -- the save: the third success, DOWNED gone with 1 HP
        deathSave(A1, true)
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 0
        world[A1].comps.Health.Hp = 1
        tick()
        eq(#ended, 0, "not passed"); eq(#refs("arena_turn_passed"), 0, "no pass record")
        local r = refs("arena_turn")
        eq(enc({ r[1].side, members(r[1]) }), enc({ "A", A1 }), "A1 acts in its phase")
        eq(Ar.held(A1), false, "A1 acts")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

test("a downed member whose save doesn't come in DEATH_SAVE_WAIT_MS: passed, recorded as 'none in time'", function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 3)
        order = { { A1, A2, B1, B2 } }
        openAll({ A1, A2, B1, B2 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
        world[A1].comps.Health.Hp = 0
        burst({ A1, A2, B1, B2 })
        for _ = 1, math.floor(Ar.DEATH_SAVE_WAIT_MS / Ar.PHASE_EVERY_MS) - 1 do tick() end
        eq(#ended, 0, "still waiting")
        tick(); tick()
        eq(table.concat(ended, ","), A1, "passed after the bound")
        eq(refs("arena_turn_passed")[1].death_save, "none in time", "said")
        eq(members(refs("arena_turn")[1]), B1, "B1's phase (B after A1's passed turn)")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)

test("a member passed, then standing up with its turn reset (a late third success): readmitted, A's next phase",
    function()
    local ended = {}
    rawset(Osi, "EndTurn", function(u) ended[#ended + 1] = u end)
    local ok, err = pcall(function()
        begin()
        fire("CombatRoundStarted", "combat1", 3)
        order = { { A1, A2, B1, B2 } }
        openAll({ A1, A2, B1, B2 })
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
        world[A1].comps.Health.Hp = 0
        burst({ A1, A2, B1, B2 })
        deathSave(A1, false) -- a save that doesn't stand them up: passed
        tick()
        eq(table.concat(ended, ","), A1, "passed"); eq(G.still_to_act(A1), false, "ended")
        -- the game then stands A1 up and resets its turn (ResetCombatTurn): active, no end requested
        osiv[key("HasActiveStatus", A1, "DOWNED")] = 0
        world[A1].comps.Health.Hp = 1
        setTurn(A1, true, false)
        tick()
        local rr = refs("arena_turn_readmitted")
        eq(#rr, 1, "one record"); eq(enc({ rr[1].side, rr[1].character, rr[1].round }), enc({ "A", A1, 3 }), "A1's")
        eq(Ar.open(A1), true, "its turn is open again"); eq(Ar.held(A1), true, "held: B1's phase")
        tick()
        eq(#refs("arena_turn_readmitted"), 1, "once")
        setTurn(B1, true, true)
        tick()
        eq(Ar.held(A1), false, "A's next phase is A1's (before A2 in the order)")
    end)
    rawset(Osi, "EndTurn", nil)
    if not ok then error(err, 0) end
end)
