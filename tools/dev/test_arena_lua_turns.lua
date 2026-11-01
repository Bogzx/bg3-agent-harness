-- Part of test_arena_lua.lua (split for the 1000-line cap): whose turn, whose characters, how a match ends.
-- test_arena_lua.lua runs it after its stubs and helpers, which it shares as globals.

test("the other side's character as actor, leader or `to`: '<name> is not on your side'", function()
    begin()
    refused(function() as("A", G.actor, B1) end, "Bo is not on your side")
    refused(function() as("B", G.actor, SA) end, "Aspect is not on your side")
    refused(function() as("A", G.actor, GOB) end, "Goblin is not on your side")
    eq(as("A", G.actor, SA), SA, "A's summon")
    eq(as("A", G.actor, A2), A2, "A's member")
    eq(G.actor(B1), B1, "no side: any party member")
end)

test("in combat, the side not acting: 'not your turn (side A is acting)'", function()
    begin()
    setTurn(A1, true, false)
    refused(function() as("B", G.combat_gate, B1, "turn") end, "not your turn (side A is acting)")
    as("A", G.combat_gate, A1, "turn")
    refused(function() as("A", G.combat_gate, A2, "turn") end, "it is not this character's turn")
end)

test("between turns, the side of the last team member whose turn started acts", function()
    begin()
    fire("TurnStarted", "S_Player_" .. B2)
    eq(Ar.state.turn.side, "B", "turn.side")
    eq(Ar.state.turn.uuid, B2, "turn.uuid")
    eq(Ar.acting_side(), "B", "acting")
    refused(function() as("A", G.combat_gate, A1, "turn") end, "not your turn (side B is acting)")
end)

test("a turn group across the teams: the side listed first acts, the other's members wait their turn", function()
    begin()
    setTurn(A1, true, false)
    setTurn(B1, true, false)
    order = { B1, A1 }
    eq(Ar.acting_side(), "B", "acting: listed first")
    eq(Ar.held(A1), true, "A1 waits")
    eq(Ar.held(B1), false, "B1 acts")
    eq(G.is_my_turn(A1), false, "A1's turn")
    eq(G.is_my_turn(B1), true, "B1's turn")
    refused(function() as("A", G.combat_gate, A1, "turn") end, "not your turn (side B is acting)")
    as("B", G.combat_gate, B1, "turn")
    setTurn(B1, true, true) -- B1 ended: A's turn now
    eq(Ar.acting_side(), "A", "acting after B1's end")
    eq(Ar.held(A1), false, "A1 acts")
    as("A", G.combat_gate, A1, "turn")
end)

test("a TurnStarted of the other team ends a member's turn for its side, though both are player characters", function()
    begin()
    setTurn(A1, true, false)
    G.note_turn_started(A1)
    clock = clock + 2000
    G.note_turn_started(B1) -- the game shows A1 open a moment longer (S084/S087)
    eq(G.turn_moved_on(A1), true, "A1's turn moved on")
    eq(G.is_my_turn(A1), false, "A1's turn")
end)

test("the other team's bag: '<name> is not on your side' for its item", function()
    begin()
    local GEM = "dddddddd-0000-0000-0000-000000000004"
    world[GEM] = { pos = { 10, 0, 0 }, comps = {} }
    osiv[key("IsItem", GEM)] = 1
    osiv[key("IsEquipable", GEM)] = 1
    osiv[key("GetInventoryOwner", GEM)] = B1
    osiv[key("IsInCombat", A1)] = 0
    local r = rpc("act.equip", "{char=" .. string.format("%q", A1) .. ",item=" .. string.format("%q", GEM) .. "}", "A")
    eq(enc(r):find("Bo is not on your side", 1, true) ~= nil, true, "A equips B1's item: " .. enc(r))
end)

test("arena.status: each team's true HP fraction; the summon's HP is not a member's", function()
    begin()
    local r = rpc("arena.status")
    eq(r.result.hp.A, 0.75, "A: 30/40")
    eq(r.result.hp.B, 0.75, "B: 45/60")
    eq(r.result.ended, nil, "ended")
end)

test("a side all downed or dead at a turn boundary ends the match: reason wipe, the other side wins", function()
    begin()
    fire("CombatRoundStarted", "combat1", 3)
    osiv[key("HasActiveStatus", B1, "DOWNED")] = 1
    world[B1].comps.Health.Hp = 0
    osiv[key("IsDead", B2)] = 1
    world[B2].comps.Health.Hp = 0
    fire("TurnStarted", "S_Player_" .. A1)
    local e = rpc("arena.status").result.ended
    eq(e and e.winner, "A", "winner")
    eq(e.reason, "wipe", "reason")
    eq(e.rounds, 3, "rounds")
    eq(e.hp_left_A, 0.75, "hp_left_A")
    eq(e.hp_left_B, 0, "hp_left_B")
    eq(rpc("arena.end", "{reason=\"cap\",winner=\"B\"}").result.ended.winner, "A", "the mod's end is kept")
end)

test("both sides wiped at once: reason wipe, no winner", function()
    begin()
    for _, u in ipairs({ A1, A2, B1, B2 }) do osiv[key("IsDead", u)] = 1 end
    fire("TurnStarted", "S_Player_" .. A1)
    local e = Ar.state.ended
    eq(e and e.reason, "wipe", "reason")
    eq(e.winner, nil, "winner")
end)

test("no wipe while a member stands", function()
    begin()
    osiv[key("IsDead", B1)] = 1
    fire("TurnStarted", "S_Player_" .. A1)
    eq(Ar.state.ended, nil, "ended")
end)

test("arena.end: the runner's reason and winner; a winner other than A/B refused", function()
    begin()
    eq(rpc("arena.end", "{reason=\"cap\",winner=\"C\"}").error, "winner must be A or B", "bad winner")
    eq(Ar.state.ended, nil, "not ended by the refusal")
    local e = rpc("arena.end", "{reason=\"cap\"}").result.ended
    eq(e.reason, "cap", "reason")
    eq(e.winner, nil, "winner")
    eq(rpc("arena.status").result.active, true, "the arena stays active until reset")
end)
