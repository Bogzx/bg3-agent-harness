-- Part of test_arena_lua.lua (split for the 1000-line cap): what each side reads. test_arena_lua.lua runs it after its
-- stubs and helpers, which it shares as globals.

test("a sneaker the other team doesn't see: its cast is its own side's (spell id too), not the other's", function()
    begin()
    osiv[key("HasActiveStatus", B1, "SNEAKING")] = 1 -- no CanSee from A1/A2: A's sight doesn't reach it
    local s0 = RPC.eventSeq
    fire("CastedSpell", B1, "Projectile_FireBolt", "Projectile", "Fire", 1)
    local a = kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "cast")
    local b = kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "cast")
    local h = kinds(rpc("events.since", "{seq=" .. s0 .. "}"), "cast")
    eq(#a, 0, "side A's casts")
    eq(#b, 1, "side B's casts")
    eq(b[1].data.spell, "Projectile_FireBolt", "B's own cast: the spell id")
    eq(b[1].data.caster.uuid, B1, "caster")
    eq(#h, 1, "the harness's (no side) casts")
end)

test("a cast the other team sees: there by display name only (no spell id)", function()
    begin()
    osiv[key("CanSee", A1, B1)] = 1
    local s0 = RPC.eventSeq
    fire("CastedSpell", B1, "Projectile_FireBolt", "Projectile", "Fire", 1)
    local a = kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "cast")
    eq(#a, 1, "side A's casts")
    eq(a[1].data.name, "Fire Bolt", "name")
    eq(a[1].data.spell, nil, "the other team's spell id")
end)

test("an event pushed by a side's op is that side's alone", function()
    begin()
    local s0 = RPC.eventSeq
    as("A", RPC.push, "note", { x = 1 })
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "note"), 1, "A")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "note"), 0, "B")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}"), "note"), 0, "no side")
end)

test("a job is its side's: the other side's poll/notify/cancel says 'not your job'", function()
    begin()
    local ja = as("A", RPC.start_job, "wait", { timeout_ms = 1e9 })
    local j0 = RPC.start_job("wait", { timeout_ms = 1e9 })
    eq(ja.side, "A", "job.side")
    for _, op in ipairs({ "job.poll", "job.notify", "job.cancel" }) do
        local r = rpc(op, "{id=" .. ja.id .. "}", "B")
        eq(r.ok, false, op .. " by B")
        eq(r.error, "not your job", op .. " by B: error")
    end
    eq(rpc("job.poll", "{id=" .. ja.id .. "}", "A").ok, true, "A polls its own")
    eq(rpc("job.poll", "{id=" .. ja.id .. "}").ok, true, "the harness polls any")
    eq(rpc("job.poll", "{id=" .. j0.id .. "}", "A").error, "not your job", "a no-side job is no side's")
    eq(ja.status, "running", "B's cancel left it running")
end)

test("a job's callbacks run as its side: what its on_tick pushes is its side's", function()
    begin()
    local ja = as("A", RPC.start_job, "wait", { timeout_ms = 1e9, on_tick = function(j)
        RPC.push("tick_side", { side = RPC.side() })
        RPC.finish_job(j, "done")
    end })
    local s0 = RPC.eventSeq
    for _, f in ipairs(tickers) do f() end
    eq(ja.status, "done", "status")
    local a = kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "tick_side")
    eq(#a, 1, "A's")
    eq(a[1].data.side, "A", "Ctx.side in on_tick")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "tick_side"), 0, "B's")
end)

test("V.context in one frame: each side gets its own party (its team and its summons)", function()
    begin()
    local ca = as("A", V.context)
    local cb = as("B", V.context)
    eq(table.concat(ca.party, ","), table.concat({ A1, A2, SA }, ","), "A's party")
    eq(table.concat(cb.party, ","), table.concat({ B1, B2 }, ","), "B's party")
    eq(cb.is_party[A1], nil, "A1 in B's party")
    eq(as("A", V.context), ca, "A's again in the frame: cached")
    eq(#V.context().party, 5, "no side: DB_PartyMembers")
end)

test("the summon is its owner's side; the other team's summons stay out", function()
    begin()
    eq(Ar.side_of(SA), "A", "side_of")
    local _, setB = Ar.team("B")
    eq(setB[SA], nil, "in B's team")
    eq(as("A", G.is_party, SA), true, "A's party member")
    eq(as("B", G.is_party, SA), false, "B's party member")
end)

test("a sighting is remembered by the side that saw it: the other side gets no last-seen hint", function()
    begin()
    as("A", V.remember, GOB, "Goblin", { 5, 0, 0 }, "hostile")
    eq(as("A", G.last_seen_hint, GOB):find("last seen", 1, true) ~= nil, true, "A's hint")
    eq(as("B", G.last_seen_hint, GOB), "", "B's hint")
    eq(as("B", V.lastSeen, GOB), nil, "B's lastSeen")
    eq(V.lastSeen(GOB), nil, "the no-side memory")
    eq(#as("B", V.recall, {}), 0, "B's recall")
end)

test("known ground: a point one side saw stays known to it, not to the other", function()
    begin()
    local seen = V.pointSeen
    V.pointSeen = function() return true end
    local p = { 40, 0, 40 }
    eq(as("A", N.known, nil, p), true, "A sees it now")
    V.pointSeen = function() return false end
    eq(as("A", N.known, nil, p), true, "A remembers it")
    eq(as("B", N.known, nil, p), false, "B never saw it")
    eq(N.known(nil, p), false, "the no-side memory")
    V.pointSeen = seen
end)

test("listener state per side: one side's combat announcement doesn't use up the other's", function()
    begin()
    local s0 = RPC.eventSeq
    fire("CombatStarted", "combat1")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "combat_started"), 1, "A")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "combat_started"), 1, "B")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}"), "combat_started"), 1, "no side")
    s0 = RPC.eventSeq
    fire("CombatEnded", "combat1")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "combat_ended"), 1, "B's end")
end)

test("state.status: a side's party is its team and its active character its own", function()
    begin()
    setTurn(B2, true, false)
    local r = rpc("state.status", "{}", "B")
    eq(r.ok, true, "ok: " .. tostring(r.error))
    local names = {}
    for _, m in ipairs(r.result.party) do names[#names + 1] = m.uuid end
    eq(table.concat(names, ","), B1 .. "," .. B2, "B's party")
    eq(r.result.active, B2, "active: B's member whose turn is open")
    eq(rpc("state.status", "{}", "A").result.active, A1, "A: its first living member")
end)

test("state.entity: the other team reads as hostile, whatever the engine's relation", function()
    begin()
    osiv[key("CanSee", A1, B1)] = 1
    osiv[key("IsAlly", A1, B1)] = 1 -- the game: two player characters
    local r = rpc("state.entity", "{uuid=" .. string.format("%q", B1) .. "}", "A")
    eq(r.ok, true, "ok: " .. tostring(r.error))
    eq(r.result.relation, "hostile", "relation")
end)

test("arena ops: refused without BG3AI_ARENA, refused with a side, the harness's otherwise", function()
    env = {}
    for _, op in ipairs({ "arena.status", "arena.end", "arena.reset" }) do
        eq(rpc(op).error, "arena ops need BG3AI_ARENA=1 in the game's environment", op .. " switch off")
    end
    env = { BG3AI_ARENA = "1" }
    begin()
    for _, op in ipairs({ "arena.status", "arena.end", "arena.reset" }) do
        eq(rpc(op, "{}", "A").error, "arena ops are the harness's (no side)", op .. " with a side")
    end
    eq(rpc("arena.status").result.active, true, "the harness's status")
    eq(rpc("arena.reset").result.active, false, "reset")
    eq(Ar.state, nil, "state after reset")
end)

test("the teams reach the client on begin, and their end on reset", function()
    begin()
    eq(net[1].teams.A[1], A1, "teams")
    eq(net[1].names.B[2], "Bea", "names")
    Ar.reset()
    eq(next(net[2]), nil, "reset: {}")
end)

test("Ar.begin refuses without the switch, so nothing becomes side-aware", function()
    env = {}
    refused(begin, "arena ops need BG3AI_ARENA=1")
    eq(Ar.state, nil, "state")
end)

test("switch off: a request's side changes nothing (same bytes), no event tags, jobs have no side", function()
    env = {}
    RPC.push("x", { n = 1 })
    eq(next(RPC.eventSide), nil, "no tag kept")
    local j = as("A", RPC.start_job, "wait", { timeout_ms = 1e9 })
    eq(j.side, nil, "job.side")
    eq(raw("events.since", "{seq=0}", "A"), raw("events.since", "{seq=0}"), "events.since")
    eq(raw("job.poll", "{id=" .. j.id .. "}", "B"), raw("job.poll", "{id=" .. j.id .. "}"), "job.poll")
    eq(raw("state.status", "{}", "A"), raw("state.status", "{}"), "state.status")
    setTurn(A1, true, false)
    order = { { A1, B1 } }
    eq(raw("state.combat", "{}", "B"), raw("state.combat", "{}"), "state.combat")
    eq(as("A", G.is_party, B1), true, "B1 in the party")
end)

test("an arena begun, then the switch gone: inert again", function()
    begin()
    env = {}
    eq(RPC.side(), nil, "side")
    eq(as("A", RPC.side), nil, "side inside a side's op")
    eq(#as("A", V.party), 5, "party")
end)

test("an end of the arena drops every side's memory and side-tagged event", function()
    begin()
    as("A", V.remember, GOB, "Goblin", { 5, 0, 0 }, "hostile")
    local s0 = RPC.eventSeq
    as("B", RPC.push, "note", {})
    Ar.reset()
    begin()
    eq(as("A", V.lastSeen, GOB), nil, "A's memory")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "note"), 0, "B's events")
end)

test("a job hears its own side's events only, as that side", function()
    begin()
    local heard = {}
    as("A", RPC.start_job, "wait", { timeout_ms = 1e9, on_event = function(_, ev)
        heard[#heard + 1] = ev.kind .. "/" .. tostring(RPC.side())
    end })
    as("B", RPC.push, "b_note", {})
    RPC.push("harness_note", {})
    as("A", RPC.push, "a_note", {})
    eq(table.concat(heard, ","), "a_note/A", "heard")
end)

test("a side's op that fails leaves no side behind", function()
    begin()
    eq(rpc("job.poll", "{id=999}", "A").ok, false, "the op failed")
    eq(RPC.Ctx.side, nil, "Ctx.side after it")
    eq(rpc("state.status", "{}", "B").ok, true, "a good one")
    eq(RPC.Ctx.side, nil, "Ctx.side after that")
end)

test("a summon's name reaches the client once its side's team finds it", function()
    begin()
    eq(#net, 1, "begin's broadcast")
    eq(#net[1].names.A, 2, "members only")
    as("A", V.party)
    eq(#net, 2, "again with the summon")
    eq(net[2].names.A[3], "Aspect", "the summon's name")
    as("A", V.party)
    as("B", V.party)
    eq(#net, 2, "once")
    eq(Ar.name_of(A2), "Abe", "members keep their names")
end)

test("a summon's turn: its side's, and its name reaches the client", function()
    begin()
    fire("TurnStarted", "S_Player_" .. SA)
    eq(Ar.state.turn.side, "A", "turn.side")
    eq(net[#net].names.A[3], "Aspect", "the summon's name")
end)

-- Walked cells (Navigation.lua): known ground and explored cells per side. arena-v010-merge.md fix 1: the ~1 Hz record
-- ran with no side, so both teams' steps went into one store and N.known answered true for side A on a spot only
-- side B stood on (a move_to there passed "in view or walked", and the answer told A where B went).
local FAR = { 40.0, 0.0, 40.0 }
local function walkB1There()
    PersistentVars = nil
    world[B1].pos = { FAR[1], FAR[2], FAR[3] }
    N.lastRecord = 0
    tick()
end

test("walked cells: a spot only side B stood on is B's known ground, not A's; the harness's view has it", function()
    begin()
    walkB1There()
    eq(as("B", N.known, nil, FAR), true, "B's own step")
    eq(as("A", N.known, nil, FAR), false, "A: only B stood there")
    eq(as("A", N.visited, "ARENA_Level", FAR), false, "A visited")
    eq(as("B", N.visited, "ARENA_Level", FAR), true, "B visited")
    eq(N.known(nil, FAR), true, "no side: every step, as today")
    eq(as("A", N.nearestVisited, "ARENA_Level", FAR).to_goal_m > 40, true, "A's nearest walked cell: its own, not B's")
    eq(as("B", N.nearestVisited, "ARENA_Level", FAR).to_goal_m < 5, true, "B's nearest walked cell")
end)

test("walked cells: a side's memory ends with the match", function()
    begin()
    walkB1There()
    RPC.clear_sides()
    eq(as("B", N.visited, "ARENA_Level", FAR), false, "B after the match's end")
end)

test("walked cells, normal play (no arena): every member's step is known, one store", function()
    env = {}
    walkB1There()
    eq(N.known(nil, FAR), true, "known")
    eq(N.visited("ARENA_Level", FAR), true, "visited")
    eq(as("A", N.known, nil, FAR), true, "a side with no arena active is no side")
end)

-- The camera centre for ground in view (V.pointSeen's CAMERA_M range): a side's context had no leader, so the range
-- centred on the side's first team row wherever the action was (arena-v010-merge.md fix 2).
test("a side's camera centre: its own member whose turn is open, else its first living one", function()
    begin()
    setTurn(A2, true, false)
    local a = as("A", V.context)
    eq(a.leader and a.leader[1], world[A2].pos[1], "A's camera: A2, whose turn is open")
    local b = as("B", V.context)
    eq(b.leader and b.leader[1], world[B1].pos[1], "B's camera: B1, its first living member")
    osiv[key("IsDead", B1)] = 1
    N.frame = N.frame + 1
    b = as("B", V.context)
    eq(b.leader and b.leader[1], world[B2].pos[1], "B's camera with B1 dead: B2")
end)

-- Door links (NavigationDoors.lua): a door that took its user far. They went into the save's level store only, so
-- side B's route named a door only side A went through (round-1503 16:35).
local DOOR = "dddddddd-0000-0000-0000-0000000000d1"
local function aUsesTheDoor()
    PersistentVars = nil
    world[DOOR] = { pos = { 0, 0, 2 }, comps = {} }
    N.uses = {}
    N.noteUse(A1, DOOR, 1)
    world[A1].pos = { 0, 0, 100 } -- the door took A1 98 m
    N.linkWatch(clock)
end

test("door links: B routes through no door only A used; A and the harness have it", function()
    begin()
    aUsesTheDoor()
    local from, goal = { 0, 0, 10 }, { 0, 0, 120 }
    eq(as("B", N.doorLink, "ARENA_Level", from, goal), nil, "B's door link")
    local a = as("A", N.doorLink, "ARENA_Level", from, goal)
    eq(a and a.uuid, DOOR, "A's door link")
    local h = N.doorLink("ARENA_Level", from, goal)
    eq(h and h.uuid, DOOR, "no side: the save's links, as today")
    RPC.clear_sides()
    eq(as("A", N.doorLink, "ARENA_Level", from, goal), nil, "A's link after the match's end")
end)

test("door links, normal play (no arena): one store, as before", function()
    env = {}
    aUsesTheDoor()
    local l = N.doorLink("ARENA_Level", { 0, 0, 10 }, { 0, 0, 120 })
    eq(l and l.uuid, DOOR, "the link")
end)
