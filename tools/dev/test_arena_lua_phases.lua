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
