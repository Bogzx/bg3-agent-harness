-- Part of test_act_lua.lua (split for the 1000-line cap): live bug S001 (melee range, shared turn groups), leftover Osiris tasks and the loop watchdog.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- ---- live bug S001 (claude-run-2): melee "range 0.0 m", shared turn groups -----

test("cast: melee keyword wins over the stats' Range 0 (live: Target_MainHandAttack Range=0)", function()
    combat_setup(9) -- the range refusal (out of combat the cast walks into range first)
    spell("Target_MainHandAttack", { TargetRadius = "MeleeMainWeaponRange", Range = 0, SpellType = "Target",
        UseCosts = "ActionPoint:1", Level = 0 })
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = "HAMMER"
    osiv[key("GetStatString", "HAMMER")] = "WPN_Warhammer"
    stats.WPN_Warhammer = { WeaponRange = 150 }
    world[GOB].comps.Bound = { Bound = { AIBounds = { Stand = { Radius = 0.49 } } } } -- an imp
    for _, d in ipairs({ 1.8, 2.2, 2.45 }) do -- the live refusals were at 1.8 m and 2.2 m
        world[GOB].pos = { d, 0, 0 }
        local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
        eq(j.status, "running", d .. " m: " .. tostring(j.error))
    end
    world[GOB].pos = { 8.8, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "status"); assert(j.error:find("range 1.5 m"), j.error)
    local r, melee = G.spell_range(stats.Target_MainHandAttack, P1)
    eq(r, 1.5, "range"); eq(melee, true, "melee")
end)

test("spell_range: numeric TargetRadius, Range 0 ignored, ranged keyword, unknown -> nil", function()
    eq(G.spell_range({ TargetRadius = "18", Range = 0 }, P1), 18, "numeric")
    eq(select(2, G.spell_range({ TargetRadius = 1.5, Range = 0 }, P1)), true, "shove-like reach is melee")
    eq(G.spell_range({ TargetRadius = "RangedMainWeaponRange", Range = 0 }, P1), G.DEFAULT_RANGED_M, "ranged default")
    eq(G.spell_range({ TargetRadius = "", Range = 0 }, P1), nil, "unknown")
    eq(G.spell_range({ TargetRadius = "MeleeMainWeaponRange", Range = 0 }, P1), 1.5, "unarmed default")
end)

local P2 = "ffffffff-0000-0000-0000-000000000006"
local function group_setup()
    -- no pending ends from an earlier test (a leftover request now stays pending END_DROP_MS)
    G.end_requested, G.end_alone, G.turn_over = {}, {}, {}
    G.turn_start, G.foe_start = {}, {}
    world[P2] = { pos = { -1, 0, 0 }, comps = {} }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("IsOnStage", P2)] = 1
    osiv[key("IsInCombat", P1)] = 1
    osiv[key("IsInCombat", P2)] = 1
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    world[P2].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    local saved = Osi.DB_PartyMembers
    Osi.DB_PartyMembers = { Get = function() return { { "S_Player_" .. P1 }, { "S_Player_" .. P2 } } end }
    return function() Osi.DB_PartyMembers = saved end
end

test("end_turn: an EndTurn the game ignores fails after END_IGNORED_MS (S044/S045 summon, 10 s TIMEOUT x9)", function()
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    osiv[key("IsInCombat", P1)] = 1
    local j = job(op("end_turn", { char = P1 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    clock = clock + A.END_IGNORED_MS tick()
    eq(j.status, "failed", "status"); eq(j.error, A.END_IGNORED, "error")
end)

test("stuck shared turn (S052-S055): a dropped group end is forgotten once nobody else acts, and re-sent", function()
    local restore = group_setup()
    osiv[key("IsInCombat", P1)] = 1
    osiv[key("IsInCombat", P2)] = 1
    -- P1 ends while P2 still acts: requested (the guard remembers it)
    local j = job(op("end_turn", { char = P1 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "requested " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    eq(G.is_my_turn(P1), false, "pending while P2 acts")
    -- P2 ends; the game dropped P1's request (P1 still active, no RequestedEndTurn)
    local j2 = job(op("end_turn", { char = P2 }))
    world[P2].comps.TurnBased.RequestedEndTurn = true
    A.dispatch("TurnEnded", { P2 })
    eq(j2.status, "done", "last member " .. tostring(j2.error))
    local ends = called("EndTurn")
    eq(ends[#ends][2], P1, "P1's EndTurn sent again by the last member")
    -- nobody else acting: still ended while the game may be applying the group's requests (S084), then forgotten
    eq(G.is_my_turn(P1), false, "not forgotten at once")
    clock = clock + G.END_DROP_MS - 100
    eq(G.is_my_turn(P1), false, "still pending just short of END_DROP_MS")
    clock = clock + 200
    -- it lasted: the guard no longer blocks P1 (it can act or end its turn)
    eq(G.is_my_turn(P1), true, "the game's open turn is P1's again")
    eq(G.turn_requested_end(P1), false, "not 'already ended'")
    restore()
end)

test("S084 shared turn end: a member ended earlier stays ended while the game applies the group's requests", function()
    local restore = group_setup()
    -- P1 ended first (requested, the game hasn't set RequestedEndTurn: live S002)
    local j = job(op("end_turn", { char = P1 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "requested " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    -- P2, the last member: its EndTurn starts the group's end (ignored by Lua at 3 s; the bridge presses the HUD)
    local j2 = job(op("end_turn", { char = P2 }))
    clock = clock + A.END_IGNORED_MS tick()
    eq(j2.status, "failed", "ignored " .. tostring(j2.error))
    -- the game applies P2's request first; P1's comes ~2 s later (one by one)
    world[P2].comps.TurnBased.RequestedEndTurn = true
    for _, dt in ipairs({ 0, 1000, 2500, 6000 }) do
        clock = clock + dt
        eq(G.end_pending(P1), true, "P1 still counts as ended +" .. dt .. " ms (was: forgotten, shown acting)")
        eq(G.is_my_turn(P1), false, "P1 may not act +" .. dt .. " ms")
    end
    world[P1].comps.TurnBased.RequestedEndTurn = true
    A.dispatch("TurnEnded", { P2 })
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), false, "group over")
    -- the next round: a fresh request in the same alone shape is timed from its own start, not the old one
    A.dispatch("TurnStarted", { "S_Player_" .. P1 })
    world[P1].comps.TurnBased.RequestedEndTurn = false
    clock = clock + G.END_DROP_MS * 2
    G.end_requested[G.guid(P1)] = G.now_ms()
    eq(G.end_pending(P1), true, "a new request is not forgotten at once")
    restore()
end)

test("S087 group end: the last member's end the game showed stays ended while the game applies the group (18 s)", function()
    local restore = group_setup()
    G.end_confirmed = {}
    local ok, err = pcall(function()
    -- 05:57:40 High Hall Courtyard: Wyll (P2 here) ended earlier (requested); Claude (P1), the group's last, ends
    local j = job(op("end_turn", { char = P2 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "P2 requested " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    local j1 = job(op("end_turn", { char = P1 }))
    world[P1].comps.TurnBased.RequestedEndTurn = true -- the game shows Claude's end
    tick()
    eq(j1.status, "done", "P1 " .. tostring(j1.error)); eq(j1.result.requested, nil, "ended by the game's flags")
    -- then, applying the group's ends one by one, it shows both turns open again (active, no RequestedEndTurn) until
    -- TurnEnded for all 18 s later
    world[P1].comps.TurnBased.RequestedEndTurn = false
    for _, dt in ipairs({ 0, 2000, 16000 }) do
        clock = clock + dt
        eq(G.is_my_turn(P1), false, "Claude may not act +" .. dt .. " ms (was: shown acting, 'e4 Claude must act first')")
        eq(G.is_my_turn(P2), false, "Wyll may not act +" .. dt .. " ms")
    end
    A.dispatch("TurnEnded", { P1 })
    A.dispatch("TurnEnded", { P2 })
    eq(G.end_requested[G.guid(P1)], nil, "TurnEnded clears the pending end")
    -- next round: TurnStarted, and the turn is P1's again
    A.dispatch("TurnStarted", { P1 })
    eq(G.is_my_turn(P1), true, "P1's next turn is open")
    end)
    restore()
    if not ok then error(err, 0) end
end)

test("S087 stunned: a party member's INCAPACITATED status makes its turn pass (Guard.turn_passes)", function()
    local st = {}
    BG3AI.Visibility = { statuses = function() return st end }
    stats.STUNNED = { StatusType = "INCAPACITATED" }
    stats.FEAR = { StatusType = "FEAR" }
    local ok, err = pcall(function()
        st = { { id = "STUNNED", name = "Stunned" } }
        eq(G.turn_passes(P1), "Stunned", "stunned")
        st = { { id = "FEAR", name = "Frightened" } }
        eq(G.turn_passes(P1), nil, "frightened still acts (moves away)")
        st = {}
        eq(G.turn_passes(P1), nil, "nothing")
    end)
    BG3AI.Visibility = nil
    if not ok then error(err, 0) end
end)

test("end_turn in a shared turn group: done on RequestedEndTurn, lists who is still to act", function()
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("EndTurn"), 1, "EndTurn called")
    world[P1].comps.TurnBased.RequestedEndTurn = true -- the group stays active (live)
    tick(3)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.ended, true, "ended")
    eq(j.result.still_to_act and j.result.still_to_act[1], P2, "Lae'zel still to act")
    eq(G.is_my_turn(P1), false, "an ended member may not act again")
    eq(G.is_my_turn(P2), true, "the other member may")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "second end_turn"); assert(j2.error:find("already ended its turn"), j2.error)
    restore()
end)

test("end_turn in a group whose EndTurn stays pending: done {requested} after END_REQUEST_AFTER_MS, counts as ended", function()
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    tick(math.floor(A.END_REQUEST_AFTER_MS / 60) - 1) -- just short of it: still waiting for RequestedEndTurn
    eq(j.status, "running", "status " .. tostring(j.error))
    tick(3) -- past A.END_REQUEST_AFTER_MS; RequestedEndTurn never set (live S002)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.requested, true, "requested")
    eq(j.result.still_to_act and j.result.still_to_act[1], P2, "Lae'zel still to act")
    eq(G.is_my_turn(P1), false, "a pending member may not act again")
    eq(G.turn_requested_end(P1), true, "counts as ended")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "second end_turn refused at once")
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), false, "TurnEnded: the turn is over (until the next TurnStarted; live window 2026-10-16)")
    A.dispatch("TurnStarted", { "S_Player_" .. P1 })
    eq(G.is_my_turn(P1), true, "the next round's TurnStarted: P1 acts again")
    restore()
end)

test("latency: a shared-group end is reported within 0.5 s (was 1.5 s; naut 12 x 1.6 s)", function()
    assert(A.END_REQUEST_AFTER_MS <= 500, "END_REQUEST_AFTER_MS " .. A.END_REQUEST_AFTER_MS)
    assert(A.END_REQUEST_AFTER_MS < A.END_IGNORED_MS, "a request is reported before the ignored-end fallback")
    local restore = group_setup()
    local j = job(op("end_turn", { char = P1 }))
    tick(9) -- 0.54 s
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.requested, true, "requested")
    restore()
end)

test("end_turn: done when the turn passes (IsActiveCombatTurn drops), no group left", function()
    local restore = group_setup()
    world[P2].comps.TurnBased.IsActiveCombatTurn = false
    local j = job(op("end_turn", { char = P1 }))
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    tick(3)
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.still_to_act, nil, "nobody left")
    restore()
end)

test("turn over (live window 2026-10-16): after TurnEnded the stale open-turn flags don't let a move through", function()
    local restore = group_setup()
    -- both members ended; the game fired TurnEnded for the group but still shows the turn open for a moment
    A.dispatch("TurnEnded", { "S_Player_" .. P1 })
    A.dispatch("TurnEnded", { P2 })
    eq(G.is_my_turn(P1), false, "turn over until P1's next TurnStarted")
    eq(G.end_pending(P1), true, "counts as ended (state.combat marks it ended)")
    local j = job(op("move_to", { char = P1, pos = { 3, 0, 0 } }))
    eq(j.status, "failed", "move refused"); assert(j.error:find("not this character's turn"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no move order sent")
    -- the game shows the turn inactive: the marker goes (the next active flag is a new turn)
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    eq(G.turn_is_over(P1), false, "cleared once inactive")
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    eq(G.is_my_turn(P1), true, "the next turn is P1's")
    -- TurnStarted clears it too (a turn that never showed inactive between the two)
    A.dispatch("TurnEnded", { P2 })
    eq(G.is_my_turn(P2), false, "P2 over")
    A.dispatch("TurnStarted", { P2 })
    eq(G.is_my_turn(P2), true, "P2's TurnStarted: its turn")
    -- the TTL is the last safety
    A.dispatch("TurnEnded", { P2 })
    clock = clock + G.TURN_OVER_TTL_MS + 1
    eq(G.is_my_turn(P2), true, "TTL")
    restore()
end)

test("S088 whose turn: another side's TurnStarted after the group's ends the members' turns (Guard.turn_moved_on)", function()
    local restore = group_setup()
    local ok, err = pcall(function()
    local C = "COMBAT_HIGHHALL"
    for _, u in ipairs({ P1, P2, NPC }) do osiv[key("CombatGetGuidFor", u)] = C end
    -- round 4: the group's turn starts (one burst for both members)
    A.dispatch("TurnStarted", { "S_Player_" .. P1 })
    clock = clock + 50
    A.dispatch("TurnStarted", { P2 })
    eq(G.turn_moved_on(P1), false, "own turn")
    eq(G.is_my_turn(P1), true, "P1 acts")
    -- 06:19:16 Claude (P1) ends first: requested; 06:19:30 Shadowheart (P2), the last, ends through the HUD (the Lua
    -- job gave up at 3 s: nothing recorded for her)
    local j = job(op("end_turn", { char = P1 }))
    clock = clock + A.END_REQUEST_AFTER_MS + 1 tick()
    eq(j.status, "done", "P1 requested " .. tostring(j.error))
    world[P2].comps.TurnBased.RequestedEndTurn = true
    -- the game applies the group's ends: P1 flagged open again, and its pending end long forgotten (the S052 rule)
    clock = clock + 14000
    G.end_requested[G.guid(P1)] = nil
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true, RequestedEndTurn = false }
    eq(G.is_my_turn(P1), true, "on the flags alone P1 reads open (the S088 'e6 Claude (yours) must act first')")
    -- the Inquisitor's turn starts (e402): the game's turn has moved on, whatever P1's flags show
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), true, "moved on")
    eq(G.is_my_turn(P1), false, "P1 may not act")
    eq(G.is_my_turn(P2), false, "P2 may not act")
    eq(G.turn_requested_end(P1), false, "not 'already ended (the rest of its group still acting)': not its turn")
    local j2 = job(op("end_turn", { char = P1 }))
    eq(j2.status, "failed", "end_turn refused"); assert(j2.error:find("not this character's turn"), j2.error)
    eq(#called("EndTurn"), 1, "no EndTurn order sent for P1 again")
    -- round 5: P1's own TurnStarted opens its next turn
    clock = clock + 20000
    A.dispatch("TurnStarted", { P1 })
    eq(G.turn_moved_on(P1), false, "own TurnStarted")
    eq(G.is_my_turn(P1), true, "P1's next turn")
    -- a start outside the party in the same burst as ours (an NPC ally sharing the group) is not "after"
    clock = clock + 300
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), false, "same burst")
    -- another combat's turns say nothing about this one
    clock = clock + 5000
    osiv[key("CombatGetGuidFor", NPC)] = "COMBAT_ELSEWHERE"
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), false, "another combat")
    -- fail-safe: a missed TurnStarted of ours never holds a turn shut for good
    osiv[key("CombatGetGuidFor", NPC)] = C
    A.dispatch("TurnStarted", { NPC })
    eq(G.is_my_turn(P1), false, "moved on")
    clock = clock + G.TURN_MOVED_TTL_MS
    eq(G.is_my_turn(P1), true, "the flags decide again after TURN_MOVED_TTL_MS")
    -- no record (a save loaded mid-turn: no TurnStarted seen): the flags decide
    G.turn_start = {}
    A.dispatch("TurnStarted", { NPC })
    eq(G.turn_moved_on(P1), false, "no record")
    end)
    restore()
    if not ok then error(err, 0) end
end)

test("combat gate: an ended group member cannot cast", function()
    local restore = group_setup()
    world[P1].comps.TurnBased.RequestedEndTurn = true
    spell("Shout_Dodge", { SpellType = "Shout", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Shout_Dodge" }))
    eq(j.status, "failed", "status"); assert(j.error:find("turn"), j.error)
    restore()
end)

-- ---- leftover Osiris tasks and the loop watchdog (se, S001 ladder loop) ----

-- P1 with `n` queued Osiris tasks; PurgeOsirisQueue empties them (and is recorded).
local function with_tasks(n)
    local tasks = {}
    for i = 1, n do tasks[i] = { Type = "UseItem" } end
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = tasks } }
    local orig = Osi.PurgeOsirisQueue
    Osi.PurgeOsirisQueue = function(...)
        orig(...)
        for i = #tasks, 1, -1 do tasks[i] = nil end
    end
    return function() Osi.PurgeOsirisQueue = orig end
end

test("interact: a ladder Use that keeps climbing after UseFinished is purged when the job is done", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(#called("Use"), 1, "Use right away")
    A.dispatch("UseFinished", { P1, GEM, 1 })
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 1, "purged"); eq(j.result.purged_tasks, 1, "purged_tasks")
    restore()
end)

test("interact ladder: the job waits for the climb (no purge at the top), done once down and still", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(#called("Use"), 1, "Use right away")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.status, "running", "still climbing " .. tostring(j.error)); eq(#called("PurgeOsirisQueue"), 0, "no purge yet")
    world[P1].pos = { 9.6, -3, 0 } tick()
    world[P1].pos = { 9.7, -5.8, 0 } tick()
    eq(j.status, "running", "moving")
    world[P1].pos = { 10.2, -5.8, 0.4 } clock = clock + 100 tick() -- stepping off sideways
    world[P1].pos = { 10.6, -5.8, 0.8 } clock = clock + 300 tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    eq(#called("PurgeOsirisQueue"), 1, "purged after the climb")
    restore()
end)

test("interact ladder: a climb loop that turns round at the foot at once is done there (live probe 10-11 01:4x)", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    local lp = world[LADDER].pos
    world[LADDER].pos = { 10, -6, 0 }   -- live: the item stands at the ladder's foot
    for _, y in ipairs({ -0.6, -2.1, -3.6 }) do world[P1].pos = { 9.9, y, 0 } tick() end
    eq(j.status, "running", "climbing")
    eq(#called("PurgeOsirisQueue"), 1, "the Use task purged once the climb began (no re-climb loop)")
    world[P1].pos = { 9.9, -5.6, 0.3 } tick()   -- at the foot, still moving (no stillness before it climbs back)
    world[LADDER].pos = lp
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    eq(#called("PurgeOsirisQueue"), 1, "the loop is purged")
    restore()
end)

test("interact ladder: a UseFinished during the walk to the top (before our Use) is ignored", function()
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "phase")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.status, "running", "status " .. tostring(j.error)); eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("interact ladder: the loop watchdog leaves a climb in progress alone (several UseFinished)", function()
    local restore = with_tasks(1)
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    for _ = 1, 4 do A.dispatch("UseFinished", { P1, LADDER, 1 }) end
    tick()
    eq(j.status, "running", "status " .. tostring(j.error))
    restore()
end)

test("interact ladder: no climb after the Use -> failed, says so", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    world[P1].pos = { 9.0, 0, 0 } clock = clock + A.LADDER_RETRY_MS + 1 tick() -- still walking: no re-Use
    eq(#called("Use"), 1, "no re-Use while the Use walks")
    tick()
    eq(#called("CharacterMoveToPosition"), 1, "stepped back from the ladder first (probe C: walk, then Use)")
    local mv = called("CharacterMoveToPosition")[1]
    near(mv[3], 9.0 - A.LADDER_STEP_M, "1.5 m away from the ladder")
    eq(#called("Use"), 1, "no in-place re-Use (probe B did nothing)")
    tick(2) clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(j.status, "running", "re-Used, not failed yet"); eq(#called("Use"), 2, "a second Use after the settle")
    clock = clock + A.LADDER_CLIMB_MS + 1 tick()
    eq(j.status, "failed", "status"); assert(j.error:find("did not climb"), j.error)
    assert(j.error:find("Used twice", 1, true), j.error)
end)

test("interact ladder: the re-Use starts the climb -> done", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick() tick()
    tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 2, "re-Used")
    world[P1].pos = { 9.6, -3, 0 } tick()
    world[P1].pos = { 9.7, -5.8, 0 } tick()
    world[P1].pos = { 10.2, -5.8, 0.4 } clock = clock + 100 tick()
    world[P1].pos = { 10.6, -5.8, 0.8 } clock = clock + 300 tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    eq(#called("Use"), 2, "one re-Use")
end)

test("NAV-8: the ladder step back walks only with a path proof", function()
    local function steps() local n = 0; for _, c in ipairs(called("CharacterMoveToPosition")) do
        if tostring(c[7]):find("lstep") then n = n + 1 end end; return n end
    world[P1].pos = { 9.5, 0, 0 }
    job(op("interact", { char = P1, target = LADDER }))
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick()
    eq(steps(), 1, "a proven step back")
end)

test("NAV-8: no step back where the engine finds no path (it would be the engine's teleport)", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    nav.fn = function() return { reachable = false } end
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick()
    local n = 0
    for _, c in ipairs(called("CharacterMoveToPosition")) do if tostring(c[7]):find("lstep") then n = n + 1 end end
    eq(n, 0, "no step back")
    eq(j.status, "running", "the re-Use still comes " .. tostring(j.error))
end)

test("interact ladder: after a walk to the top, the Use waits until the actor stands still (acceptance 01:5x)", function()
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "walks first")
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })   -- the walk's arrival
    eq(#called("Use"), 0, "no Use at the walk's arrival")
    world[P1].pos = { 9.4, 0, 0 } tick()            -- still settling
    eq(#called("Use"), 0, "no Use while moving")
    tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use once still")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.status, "running", "climb awaited")
end)

test("interact ladder: a settled Use with no UseFinished still watches the climb (acceptance 10-11 08:37/10:43)", function()
    local restore = with_tasks(1)
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    tick() tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use once still")
    assert(j.used_at, "the climb watch starts at our Use")
    local lp = world[LADDER].pos
    world[LADDER].pos = { 10, -6, 0 }
    for _, y in ipairs({ -0.6, -2.1, -3.6 }) do world[P1].pos = { 9.9, y, 0 } tick() end
    eq(#called("PurgeOsirisQueue"), 1, "the Use purged once the climb began, no UseFinished needed")
    world[P1].pos = { 9.9, -5.6, 0.3 } tick()
    world[LADDER].pos = lp
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.climbed, "down", "climbed")
    restore()
end)

test("interact ladder: LADDER_WATCH_AT_USE=false keeps the old wait for UseFinished", function()
    A.LADDER_WATCH_AT_USE = false
    local ok, err = pcall(function()
        world[P1].pos = { 0, 0, 0 }
        local j = job(op("interact", { char = P1, target = LADDER }))
        world[P1].pos = { 9.3, 0, 0 }
        A.dispatch("EntityEvent", { P1, j.walk.ev })
        tick() tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
        eq(#called("Use"), 1, "Use once still")
        eq(j.used_at, nil, "no watch before UseFinished")
    end)
    A.LADDER_WATCH_AT_USE = true
    if not ok then error(err, 0) end
end)

test("interact ladder: a standing Use with no UseFinished: no-climb retry still comes", function()
    world[P1].pos = { 9.5, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    assert(j.used_at, "watch from the Use")
    tick()
    clock = clock + A.LADDER_RETRY_MS + 1 tick()
    eq(#called("CharacterMoveToPosition"), 1, "stepped back for the re-Use")
end)

test("interact ladder: a UseFinished before our settled Use is not ours", function()
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    tick()
    eq(j.used_at, nil, "not counted"); eq(j.status, "running", "status " .. tostring(j.error))
end)

test("pickup: an order gone from the queue is re-sent, not a 40 s timeout (S043 batch step 2 x3)", function()
    world[P1].pos = { 9, 0, 0 }   -- next to the gem: no walk (the 2nd pick_up of a batch)
    local j = job(op("pickup", { char = P1, item = GEM }))
    eq(#called("Pickup"), 1, "ordered")
    tick()
    eq(#called("Pickup"), 1, "not at once")
    clock = clock + A.PICKUP_REORDER_MS tick()
    eq(#called("Pickup"), 2, "re-sent: no task left, nothing picked")
    osiv[key("GetInventoryOwner", GEM)] = P1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    -- a running task (walking/animating) is left alone
    osiv[key("GetInventoryOwner", GEM)] = nil
    local restore = with_tasks(1)
    j = job(op("pickup", { char = P1, item = GEM }))
    clock = clock + A.PICKUP_REORDER_MS * 3 tick()
    eq(#called("Pickup"), 3, "no re-send while its task runs")
    restore()
end)

test("pickup: the item stacked into a party stack is done at once, not a 45 s timeout (S080 #9 apples)", function()
    local STACK = "17171717-0000-0000-0000-00000000000d"
    party_rows = { P1, P2 }
    osiv[key("GetTemplate", GEM)] = "Apple_tpl"
    osiv[key("GetTemplate", STACK)] = "Apple_tpl"
    osiv[key("GetInventoryOwner", STACK)] = P2
    osiv[key("IsCharacter", P2)] = 1
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("pickup", { char = P1, item = GEM }))
    A.dispatch("AddedTo", { "16161616-0000-0000-0000-00000000000c", P2, "" }) -- another kind: not ours
    tick()
    eq(j.status, "running", "other template ignored")
    A.dispatch("AddedTo", { STACK, P2, "" })   -- the add names the stack; the gem still exists this frame
    eq(j.status, "running", "ours still in the world")
    world[GEM] = nil                           -- merged: it left the world
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.by, P2, "by"); eq(j.result.stacked_into, STACK, "stacked_into")
end)

test("pickup: our item's own AddedTo counts even when it merged at once (no holder left)", function()
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("pickup", { char = P1, item = GEM }))
    A.dispatch("AddedTo", { GEM, P1, "" })
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.by, P1, "by")
end)

test("pickup: an item that left the world mid-walk stops the walk at once (no timeout)", function()
    local restore = with_tasks(1)
    local j = job(op("pickup", { char = P1, item = GEM }))
    eq(j.phase, "approach", "walking")
    tick()
    world[GEM] = nil
    tick()
    eq(j.status, "failed", "status")
    assert(j.error:find("no longer in the world", 1, true), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "walk purged")
    restore()
    local r = job(op("pickup", { char = P1, item = GEM }))   -- already gone when ordered: refused, no walk
    eq(r.status, "failed", "refused"); assert(r.error:find("no longer in the world", 1, true), r.error)
end)

test("move_to: a timed-out walk purges its CharacterMoveToPosition task", function()
    local restore = with_tasks(1)
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status")
    clock = j.deadline + 1
    for _, f in ipairs(tickers) do f() end
    eq(j.status, "timeout", "status")
    eq(#called("PurgeOsirisQueue"), 1, "purged")
    restore()
end)

test("move_to: done with an empty queue purges nothing", function()
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    arrive(j, { 5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("loop watchdog: an orphaned task re-using the same ladder is purged after 3 uses", function()
    local restore = with_tasks(2)
    A.use_streak = {}
    A.dispatch("UseFinished", { P1, DOOR, 1 }); clock = clock + 5000
    A.dispatch("UseFinished", { P1, DOOR, 1 }); clock = clock + 5000
    eq(#called("PurgeOsirisQueue"), 0, "not after 2")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    eq(#called("PurgeOsirisQueue"), 1, "purged after 3")
    restore()
end)

test("loop watchdog: uses far apart, other items or no tasks are not a loop", function()
    A.use_streak = {}
    for _ = 1, 4 do A.dispatch("UseFinished", { P1, DOOR, 1 }) end -- no tasks queued
    eq(#called("PurgeOsirisQueue"), 0, "no tasks")
    local restore = with_tasks(1)
    A.use_streak = {}
    for _ = 1, 3 do A.dispatch("UseFinished", { P1, DOOR, 1 }); clock = clock + 25000 end
    A.dispatch("UseFinished", { P1, GEM, 1 })
    eq(#called("PurgeOsirisQueue"), 0, "gaps > 20 s / another item")
    restore()
end)

test("walk: a 0-damage trap puff doesn't interrupt; damage or a character attacker does", function()
    local NULL = "NULL_00000000-0000-0000-0000-000000000000"
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    A.dispatch("AttackedBy", { P1, NULL, GEM, "None", 0, "", 0 })  -- Mausoleum vent: Blinded, 0 dmg
    eq(j.status, "running", "trap puff")
    A.dispatch("AttackedBy", { P1, NULL, GEM, "Force", 5, "", 0 })
    eq(j.status, "interrupted", "trap damage"); assert(j.error:find("attacked"), j.error)
    local k = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    A.dispatch("AttackedBy", { P1, GOB, GOB, "None", 0, "", 0 })    -- a goblin's miss
    eq(k.status, "interrupted", "character miss")
end)

test("loop watchdog: a running job routed through a ladder loop is interrupted", function()
    local restore = with_tasks(1)
    A.use_streak = {}
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    for _ = 1, 3 do A.dispatch("UseFinished", { P1, DOOR, 1 }) end
    eq(j.status, "interrupted", "status"); assert(j.error:find("stuck"), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "purged once")
    restore()
end)

-- naut S001 16:37 (Nautiloid helm): interact e53 (the 2nd ladder) walked back down e54 (the 1st) and up again 6x
local LADDER2 = "acacacac-0000-0000-0000-000000000018"
test("walk: the approach going up a ladder and back down it stops at the reversal, naming it (naut S001 helm)", function()
    local restore = with_tasks(1)
    A.use_streak = {}
    world[LADDER2] = { pos = { 3, 0, 0 }, comps = { ServerItem = { IsLadder = true } } }
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "walks first")
    world[P1].pos = { 3, 0, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 })   -- the climb starts at the foot
    world[P1].pos = { 3, 1.2, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 }) -- several per climb (live 14:1x)
    world[P1].pos = { 3, 3, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 })   -- at the top
    eq(j.status, "running", "one climb is a route " .. tostring(j.error))
    world[P1].pos = { 3, 3, 0.2 } A.dispatch("UseFinished", { P1, LADDER2, 1 }) -- the same ladder again, from its top
    eq(j.status, "running", "not yet back down")
    world[P1].pos = { 3, 0, 0.2 } A.dispatch("UseFinished", { P1, LADDER2, 1 })
    eq(j.status, "interrupted", "stopped at the reversal")
    assert(j.error:find("the walk took ladder " .. LADDER2 .. " up and then back down", 1, true), j.error)
    assert(j.error:find("(interact " .. LADDER2 .. ")", 1, true), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "purged once (the watchdog does not fire after it)")
    restore()
end)

test("walk: ladder uses that never reverse, the target ladder's own use, and a door are no reversal", function()
    world[LADDER2] = { pos = { 3, 0, 0 }, comps = { ServerItem = { IsLadder = true } } }
    local j = job(op("move_to", { char = P1, pos = { 5, 6, 0 } }))
    world[P1].pos = { 3, 3, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 })
    world[P1].pos = { 3, 6, 0 } A.dispatch("UseFinished", { P1, LADDER2, 1 }) -- a long ladder, still going up
    world[P1].pos = { 3, 0, 0 } A.dispatch("UseFinished", { P1, DOOR, 1 })    -- not a ladder
    eq(j.status, "running", "move_to " .. tostring(j.error))
    world[P1].pos = { 0, 0, 0 }
    local k = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9, 0, 0 } A.dispatch("UseFinished", { P1, LADDER, 1 })  -- the walk to the target's top
    world[P1].pos = { 9, 3, 0 } A.dispatch("UseFinished", { P1, LADDER, 1 })
    world[P1].pos = { 9, 0, 0 } A.dispatch("UseFinished", { P1, LADDER, 1 })
    eq(k.status, "running", "the target itself is the interact's own business " .. tostring(k.error))
end)

test("act.stop: interrupts the running job and purges the queue", function()
    local restore = with_tasks(3)
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    local s = job(op("stop", { char = P1 }))
    eq(j.status, "interrupted", "walk"); eq(s.status, "done", "stop")
    eq(s.result.stopped_job, j.id, "stopped_job"); eq(s.result.purged_tasks, 3, "purged"); eq(s.result.left, 0, "left")
    restore()
end)

test("cast: a finished spell does not purge the queue", function()
    local restore = with_tasks(1)
    spell("Shout_Dash", { SpellType = "Shout", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Shout_Dash" }))
    eq(j.osi_task, "spell", "marked as a spell task")
    RPC.finish_job(j, "done", {})
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
    restore()
end)
