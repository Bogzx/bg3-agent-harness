-- Part of test_arena_lua.lua (split for the 1000-line cap): prone at a phase start (REVIEW-12 (a)). Every member's
-- turn starts in the round's burst, before any phase, so the game's stand-up at the turn start comes before a member
-- knocked down by an earlier phase of the same round is reached: the mod stands it up at its own phase start, for
-- half its movement when it has that much, as BG3 does at a turn start.
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local PRONE = { StatusType = "KNOCKED_DOWN", StatusGroups = { "SG_Prone" }, RemoveEvents = { "OnTurn" } }

-- fn(on, removed) with A1 prone: on[u] = u's statuses, removed = the RemoveStatus calls; engine_half: the game's own
-- removal takes half the movement (the open question of w10 ST-P5(f)). Stubs restored after.
local function withProne(amount, fn, engine_half)
    local savedStatuses, savedGet = V.statuses, Ext.Stats.Get
    local on, removed = { [A1] = { { id = "PRONE_GREASE", name = "Prone" } } }, {}
    V.statuses = function(e) return on[e.Uuid.EntityUuid] or {} end
    Ext.Stats.Get = function(n) return n == "PRONE_GREASE" and PRONE or nil end
    rawset(Osi, "RemoveStatus", function(u, id)
        removed[#removed + 1] = u .. ":" .. id
        on[u] = {}
        local r = world[u].comps.ActionResources.Resources.Movement[1]
        if engine_half then r.Amount = r.Amount - r.MaxAmount / 2 end
    end)
    G._disabling = {}
    for _, u in ipairs({ A1, A2, B1, B2 }) do
        world[u].comps.ActionResources = { Resources = { Movement = { { Level = 0, Amount = 9, MaxAmount = 9 } } } }
    end
    world[A1].comps.ActionResources.Resources.Movement[1].Amount = amount
    local ok, err = pcall(fn, on, removed)
    V.statuses, Ext.Stats.Get = savedStatuses, savedGet
    rawset(Osi, "RemoveStatus", nil)
    G._disabling = {}
    Ar.PRONE_DEDUCT = true
    if not ok then error(err, 0) end
end

local function startRound(first)
    begin()
    fire("CombatRoundStarted", "combat1", 2)
    order = { { first, B1, first == A1 and A2 or A1, B2 } }
    for _, u in ipairs(order[1]) do setTurn(u, true, false) end
    for _, u in ipairs(order[1]) do fire("TurnStarted", "S_Player_" .. u) end
end

local function movement(u) return world[u].comps.ActionResources.Resources.Movement[1].Amount end

test("a prone member's phase start stands it up for half its movement; the guard refuses it while prone", function()
    withProne(9, function(_, removed)
        startRound(A2) -- A2 B1 A1 B2: A1 prone while A2 and B1 play
        tick()
        eq(refs("arena_turn")[1].character, A2, "phase 1 A2")
        eq(#removed, 0, "not A1's phase yet")
        eq(G.move_blocked(A1), "Prone", "A1 can't walk while prone")
        setTurn(A2, false, true); tick()
        setTurn(B1, false, true); tick()
        eq(refs("arena_turn")[3].character, A1, "phase 3 A1")
        eq(table.concat(removed, ","), A1 .. ":PRONE_GREASE", "stood up")
        eq(movement(A1), 4.5, "half its movement taken")
        eq(G.move_blocked(A1), nil, "walks now")
        local r = refs("arena_stood_up")
        eq(#r, 1, "one arena_stood_up")
        eq(enc({ r[1].side, r[1].character, r[1].name, r[1].round, r[1].status, r[1].movement_before,
            r[1].movement_after, r[1].movement_max, r[1].deducted, r[1].game_took }),
            enc({ "A", A1, "Ana", 2, "Prone", 9, 4.5, 9, 4.5, false }), "the record")
        tick()
        eq(#removed, 1, "once per phase")
    end)
end)

test("a prone member with less than half its movement stays down at its phase start", function()
    withProne(4, function(_, removed)
        startRound(A1)
        tick()
        eq(refs("arena_turn")[1].character, A1, "phase 1 A1")
        eq(#removed, 0, "stays prone")
        eq(movement(A1), 4, "nothing taken")
        eq(#refs("arena_stood_up"), 0, "no stand-up record")
    end)
end)

test("the game's own removal took half: the mod takes nothing more (no double deduct)", function()
    withProne(9, function(_, removed)
        startRound(A1)
        tick()
        eq(#removed, 1, "stood up")
        eq(movement(A1), 4.5, "half once")
        local r = refs("arena_stood_up")[1]
        eq(enc({ r.movement_after, r.deducted, r.game_took }), enc({ 4.5, 0, true }), "the game took it")
    end, true)
end)

test("PRONE_DEDUCT off: the status goes, the mod deducts nothing", function()
    withProne(9, function(_, removed)
        Ar.PRONE_DEDUCT = false
        startRound(A1)
        tick()
        eq(#removed, 1, "stood up")
        eq(movement(A1), 9, "nothing deducted")
        local r = refs("arena_stood_up")[1]
        eq(enc({ r.movement_after, r.deducted, r.game_took }), enc({ 9, 0, false }), "switch off")
    end)
end)

test("a member not prone at its phase start: nothing removed or deducted", function()
    withProne(9, function(on, removed)
        on[A1] = {}
        startRound(A1)
        tick()
        eq(#removed, 0, "nothing"); eq(movement(A1), 9, "movement as it was")
    end)
end)
