-- Part of test_arena_lua.lua: the seat fix's turn order (owner D643, Fable REVIEW-8). Inside a shared turn group the
-- sides alternate one character at a time by each side's rolled order; turn_order "alternate" opens each round with
-- the side of the group's first open member, "snake" swaps the opening side every round.
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local function openAll(us) for _, u in ipairs(us) do setTurn(u, true, false) end end
local function burst(us) for _, u in ipairs(us) do fire("TurnStarted", "S_Player_" .. u) end end

-- One round of the group `grp` played to its end, each phase's character ending as it comes: "A:<u> B:<u> ..."
local function playRound(round, grp)
    fire("CombatRoundStarted", "combat1", round)
    order = { grp }
    openAll(grp)
    burst(grp)
    local got, n0 = {}, #refs("arena_turn")
    for _ = 1, #grp do
        tick()
        local r = refs("arena_turn")
        local last = r[#r]
        got[#got + 1] = last.side .. ":" .. last.character
        setTurn(last.character, true, true)
    end
    eq(#refs("arena_turn") - n0, #grp, "one phase per character in round " .. round)
    return table.concat(got, " "), refs("arena_turn")[n0 + 1]
end

test("turn_order alternate (default): A|BB|A plays A1|B1|A2|B2 every round; arena_start says alternate", function()
    begin()
    eq(refs("arena_start")[1].turn_order, "alternate", "arena_start")
    local grp = { A1, B1, B2, A2 }
    for round = 1, 3 do
        local seq, first = playRound(round, grp)
        eq(seq, "A:" .. A1 .. " B:" .. B1 .. " A:" .. A2 .. " B:" .. B2, "round " .. round)
        eq(enc({ first.order, first.opening, first.rule }), enc({ "alternate", "A", "opening" }), "round " .. round)
    end
end)

test("turn_order snake: the opening side swaps every round, each side's own order kept", function()
    begin({ turn_order = "snake" })
    eq(refs("arena_start")[1].turn_order, "snake", "arena_start")
    local grp = { A1, B1, B2, A2 }
    local want = {
        "A:" .. A1 .. " B:" .. B1 .. " A:" .. A2 .. " B:" .. B2,
        "B:" .. B1 .. " A:" .. A1 .. " B:" .. B2 .. " A:" .. A2,
        "A:" .. A1 .. " B:" .. B1 .. " A:" .. A2 .. " B:" .. B2,
    }
    for round = 1, 3 do
        local seq, first = playRound(round, grp)
        eq(seq, want[round], "round " .. round)
        eq(enc({ first.order, first.opening }), enc({ "snake", round % 2 == 1 and "A" or "B" }), "opening " .. round)
    end
end)

test("arena_round's first is the round's opening side, also when that side has nobody open (snake)", function()
    begin({ turn_order = "snake" })
    playRound(1, { A1, B1, B2, A2 })
    fire("CombatRoundStarted", "combat1", 2)
    order = { { A1, A2 }, B1, B2 } -- round 2 opens with B (snake), but nobody of B is in A's group
    openAll({ A1, A2 })
    burst({ A1, A2 })
    tick()
    local r = refs("arena_turn")
    eq(enc({ r[#r].side, r[#r].opening, r[#r].rule }), enc({ "A", "B", "only_side_left" }), "A1 acts")
    local rounds = refs("arena_round")
    eq(enc({ #rounds, rounds[2].round, rounds[2].first, rounds[2].how }), enc({ 2, 2, "B", "turn" }), "opening B")
end)

test("a read between two turn starts of the round (B1's in, A1's not yet) doesn't fix the opening on B", function()
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    order = { { A1, B1, B2, A2 } } -- rolled: A1 first
    setTurn(B1, true, false)
    fire("TurnStarted", "S_Player_" .. B1)
    Ar.acting_side() -- the side gate / Ar.held / a state read, before the server tick
    Ar.held(B1)
    for _, u in ipairs({ A1, B2, A2 }) do
        setTurn(u, true, false)
        fire("TurnStarted", "S_Player_" .. u)
    end
    tick()
    local r = refs("arena_turn")
    eq(enc({ #r, r[1].side, r[1].character, r[1].opening, r[1].rule }), enc({ 1, "A", A1, "A", "opening" }),
        "the round opens with A1, the rolled order's first")
    eq(refs("arena_round")[1].first, "A", "arena_round")
end)

test("the group's Team reading nil on a tick mid-round (or no turn order): same group, the alternation goes on",
    function()
    begin({ turn_order = "snake" })
    playRound(1, { A1, B1, B2, A2 })
    fire("CombatRoundStarted", "combat1", 2)
    order = { { A1, B1, B2, A2, team = "T1" } } -- round 2 opens with B (snake): B1, A1, B2, A2
    openAll({ A1, B1, B2, A2 })
    burst({ A1, B1, B2, A2 })
    tick()
    setTurn(B1, true, true)
    order = { { A1, B1, B2, A2 } } -- Team nil: the group's id reads as its first member
    tick()
    local r = refs("arena_turn")
    eq(enc({ r[#r].character, r[#r].phase, r[#r].rule, r[#r].group }), enc({ A1, 2, "alternate", "T1" }), "A1 next")
    setTurn(A1, true, true)
    order = {} -- the turn order doesn't read at all
    tick()
    r = refs("arena_turn")
    eq(enc({ r[#r].character, r[#r].phase, r[#r].rule, r[#r].opening }), enc({ B2, 3, "alternate", "B" }), "B2 next")
end)

-- O1 audit S3 (d): more than one turn group in a round (arena-v0 has had one). Each group alternates on its own;
-- alternate opens each with its own first living member's side, snake every group of the round with the round's.
local function twoGroups(turn_order)
    begin({ turn_order = turn_order })
    playRound(1, { A1, B1, B2, A2 }) -- snake: A opened round 1
    fire("CombatRoundStarted", "combat1", 2)
    order = { { A1, B1 }, { A2, B2 } }
    openAll({ A1, B1, A2, B2 })
    burst({ A1, B1, A2, B2 })
    local got, n0 = {}, #refs("arena_turn")
    for _ = 1, 4 do
        tick()
        local r = refs("arena_turn")
        got[#got + 1] = r[#r].side .. r[#r].phase .. ":" .. r[#r].opening .. ":" .. r[#r].rule
        setTurn(r[#r].character, true, true)
    end
    eq(#refs("arena_turn") - n0, 4, turn_order .. ": four phases")
    return table.concat(got, " ")
end

test("two turn groups in a round: alternate opens each by its own first member, snake both with the round's side",
    function()
    eq(twoGroups("alternate"), "A1:A:opening B2:A:alternate A1:A:opening B2:A:alternate", "alternate")
    reset()
    eq(twoGroups("snake"), "B1:B:opening A2:B:alternate B1:B:opening A2:B:alternate", "snake")
end)

test("a side with nobody open left plays its members on in order (only_side_left)", function()
    begin()
    osiv[key("IsDead", B2)] = 1
    local grp = { A1, B1, A2 }
    local seq = playRound(1, grp)
    eq(seq, "A:" .. A1 .. " B:" .. B1 .. " A:" .. A2, "A2 after B1")
    local r = refs("arena_turn")
    eq(enc({ r[1].rule, r[2].rule, r[3].rule }), enc({ "opening", "alternate", "alternate" }), "rules")
    reset()
    begin()
    osiv[key("IsDead", B2)] = 1
    seq = playRound(1, { A1, A2, B1 })
    eq(seq, "A:" .. A1 .. " B:" .. B1 .. " A:" .. A2, "A2 waits for B1 even when rolled before it")
    reset()
    begin()
    order = { { A1, A2 }, B1, B2 } -- B's turns later, in their own groups: nobody of B open in A's group
    fire("CombatRoundStarted", "combat1", 1)
    openAll({ A1, A2 })
    tick()
    setTurn(A1, true, true)
    tick()
    local rr = refs("arena_turn")
    eq(enc({ #rr, rr[#rr].character, rr[#rr].rule }), enc({ 2, A2, "only_side_left" }), "no B left")
end)

test("an unknown turn_order is refused by Ar.begin", function()
    local ok, err = pcall(begin, { turn_order = "team" })
    eq(ok, false, "refused")
    eq(tostring(err):find('turn_order must be "alternate" or "snake"', 1, true) ~= nil, true, tostring(err))
end)

-- REVIEW-8: one-character phases make the first end of a round wait longest (three later phases of up to ~105 s
-- each, past END_REQUEST_TTL_MS and END_DROP_MS); the arena branch of end_pending keeps it while anyone is still to
-- act.
test("the round's first end stays pending through three later phases of 105 s each; no ended member reopens", function()
    begin()
    fire("CombatRoundStarted", "combat1", 1)
    local grp = { A1, B1, A2, B2 }
    order = { grp }
    openAll(grp)
    burst(grp)
    tick()
    for _, u in ipairs({ A1, B1, A2 }) do
        eq(Ar.phase().character, u, u .. "'s phase")
        G.end_requested[u] = clock -- act.end_turn's requested end; the game changes no flag in a shared group
        for _ = 1, math.floor(105000 / Ar.PHASE_EVERY_MS) do tick() end
    end
    eq(clock - G.end_requested[A1] > G.END_REQUEST_TTL_MS + G.END_DROP_MS, true, "A1's end is past both limits")
    eq(G.end_pending(A1), true, "A1's end kept")
    eq(Ar.phase().character, B2, "B2's phase, nobody reopened")
    local sides = ""
    for _, r in ipairs(refs("arena_turn")) do sides = sides .. r.side end
    eq(sides, "ABAB", "four phases, no repeat")
end)
