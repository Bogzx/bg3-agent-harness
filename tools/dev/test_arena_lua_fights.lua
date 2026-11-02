-- Part of test_arena_lua.lua (split for the 1000-line cap): the fight as each side reads it (allies, the turn order,
-- a pending end, a blast's bystanders). test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

test("the engine calling the other team allies: still no sneaker or invisible character seen through it", function()
    begin()
    osiv[key("IsAlly", A1, B1)] = 1
    osiv[key("HasActiveStatus", B1, "SNEAKING")] = 1
    local e = V.entity(B1)
    eq(as("A", function() return V.characterVisible(V.context(), B1, e) end), false, "a sneaking B1")
    eq(as("A", function() return V.isAlly(V.context(), B1) end), false, "isAlly")
    osiv[key("HasActiveStatus", B1, "SNEAKING")] = nil
    osiv[key("IsInvisible", B1)] = 1
    eq(as("A", function() return V.characterVisible(V.context(), B1, e) end), false, "an invisible B1")
end)

test("a blast's bystanders: the other team is hostile, whatever the engine's relation", function()
    begin()
    osiv[key("CanSee", A1, B1)] = 1
    osiv[key("IsAlly", A1, B1)] = 1
    local near = as("A", G.in_blast, A1, { 10, 0, 0 }, 3)
    local side
    for _, r in ipairs(near or {}) do if r.uuid == B1 then side = r.side end end
    eq(side, "hostile", "B1's side")
end)

-- S084 shape: A1 ended (end_requested), the game still shows its turn open; B1's turn is open. A asks at once, then
-- someone asks again once END_DROP_MS has passed.
local function pendingEndAsked(by)
    reset()
    begin()
    setTurn(A1, true, false)
    setTurn(B1, true, false)
    G.end_requested[A1] = clock
    order = { { A1, B1 } }
    local first = as("A", Ar.acting_side)
    clock = clock + G.END_DROP_MS + 1000
    return first, by and as(by, Ar.acting_side) or Ar.acting_side()
end

test("a pending end is judged in the character's own team: who asks never changes who acts", function()
    local first, a = pendingEndAsked("A")
    eq(first, "B", "at once: A1's end is kept")
    local _, b = pendingEndAsked("B")
    local _, h = pendingEndAsked(nil)
    eq(b, a, "B asking vs A asking")
    eq(h, a, "the harness asking vs A asking")
end)

local function combatAs(side)
    local r = rpc("state.combat", "{}", side)
    if not r.ok then error("state.combat: " .. tostring(r.error), 2) end
    local rows = {}
    for _, o in ipairs(r.result.order) do rows[o.uuid] = o end
    return r.result, rows
end

test("state.combat per side: own team party, the other hostile, unseen rows left out", function()
    begin()
    setTurn(A1, true, false)
    order = { A1, B1, A2, B2 }
    osiv[key("CanSee", A1, B1)] = 1
    osiv[key("HasActiveStatus", B2, "SNEAKING")] = 1
    local c, rows = combatAs("A")
    eq(rows[A1].relation, "party", "A1")
    eq(rows[B1].relation, "hostile", "B1")
    eq(rows[B2], nil, "the sneaking B2")
    eq(c.current, A1, "current")
    osiv[key("CanSee", B1, A1)] = 1
    local _, rb = combatAs("B")
    eq(rb[A1] and rb[A1].relation, "hostile", "B reads A1")
end)

test("state.combat: the acting combatant unseen: current null, active true", function()
    begin()
    setTurn(B2, true, false)
    order = { B2, A1 }
    osiv[key("HasActiveStatus", B2, "SNEAKING")] = 1
    local c = combatAs("A")
    eq(c.active, true, "active")
    eq(c.current, nil, "current")
end)

test("state.combat, a turn group across the teams: the waiting side's member is not current for either side", function()
    begin()
    setTurn(A1, true, false)
    setTurn(B1, true, false)
    order = { { B1, A1 } }
    osiv[key("CanSee", B1, A1)] = 1
    osiv[key("CanSee", A1, B1)] = 1
    local cb, rb = combatAs("B")
    eq(rb[A1].is_current, nil, "B reads A1")
    eq(cb.current, B1, "B: current")
    eq(table.concat(cb.turn_group, ","), B1, "B: turn_group")
    local ca, ra = combatAs("A")
    eq(ra[A1].is_current, nil, "A reads A1")
    eq(ca.current, B1, "A: current")
end)

-- Live check 8 (2026-11-02): side B's rows showed a downed A member without `downed` (party members only had it).
test("state.combat: the other team's downed member reads downed (the status); an NPC at 0 HP without it does not",
    function()
    begin()
    order = { A1, B1, GOB }
    osiv[key("CanSee", B1, A1)], osiv[key("CanSee", B1, GOB)] = 1, 1
    osiv[key("HasActiveStatus", A1, "DOWNED")] = 1
    world[A1].comps.Health.Hp = 0
    world[GOB].comps.Health = { Hp = 0, MaxHp = 10 }
    local _, rows = combatAs("B")
    eq(rows[A1] and rows[A1].downed, true, "B reads A1 downed")
    eq(rows[GOB] and rows[GOB].relation ~= nil, true, "B sees the goblin")
    eq(rows[GOB].downed, nil, "the goblin at 0 HP, no DOWNED: not downed")
    local _, ra = combatAs("A")
    eq(ra[A1].downed, true, "A reads its own A1 downed")
end)
