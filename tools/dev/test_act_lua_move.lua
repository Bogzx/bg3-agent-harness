-- Part of test_act_lua.lua (split for the 1000-line cap): approach points, move_to, combat movement, interact and pickup.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

test("within: a distance that rounds to the max shows one decimal (S028 15:53 '40m ..., max 40m')", function()
    world[NPC].pos = { 40.3, 0, 0 }
    refused(function() G.within(P1, NPC, 40, "target") end, "(40.3m from this character, max 40m)")
    world[NPC].pos = { 47, 0, 0 }
    refused(function() G.within(P1, NPC, 40, "target") end, "(47m from this character, max 40m)")
end)

-- ---- G.approach_point --------------------------------------------------------

test("approach: already within reach -> here", function()
    world[NPC].pos = { 2, 0, 0 }
    local p = G.approach_point(P1, NPC, 2.5)
    eq(p.here, true, "here")
end)

test("approach: ring point on the actor's side, shortest walk first", function()
    local p = G.approach_point(P1, NPC, 2.5) -- radii 1.0/1.5/2.2 fit in reach 2.5
    near(p.pos[1], 7.8, "x"); near(p.pos[3], 0, "z")
    near(p.length_m, 7.8, "length")
end)

test("approach: skips unreachable candidates", function()
    nav.fn = function(_, pos)
        if pos[3] < -0.5 then return { reachable = true, length_m = 12 } end
        return { reachable = false }
    end
    local p = G.approach_point(P1, NPC, 2.5)
    assert(p.pos[3] < -0.5, "picked an unreachable point")
    near(p.length_m, 12, "length")
end)

test("pickpocket: an NPC behind a counter is reached from the 2.2 m ring (setup 14:xx, Last Light Inn)", function()
    local npc = world[NPC].pos
    nav.fn = function(_, pos)  -- the counter: nothing walkable within 2 m of the NPC
        if U.dist(pos, npc) >= 2.0 then return { reachable = true, length_m = U.dist(world[P1].pos, pos) } end
        return { reachable = false }
    end
    local j = job(op("pickpocket", { char = P1, npc = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.phase, "approach", "phase")
end)

test("approach: nothing reachable -> refused", function()
    nav.fn = function() return { reachable = false } end
    refused(function() G.approach_point(P1, NPC, 2.5) end, "no walkable path")
end)

test("point moves (audit C1): a spot not in view and never walked is refused, one in view is not", function()
    nav.unseen = function(p) return p[1] > 4 end
    refused(function() G.point_ok(P1, { 5, 0, 0 }) end, "not in view and the party hasn't walked there")
    G.point_ok(P1, { 3, 0, 0 })
end)

test("point moves (audit C2): over the Faith-Leap chasm at the walkway's height nothing is proven or refused", function()
    nav.fn = function() return { reachable = false } end
    BG3AI.Nav.helperZone = function(p) return p[1] < 0 end
    local ok, err = pcall(function()
        local info = G.point_reachable(P1, { -5, 0, 0 })
        eq(info.length_m, nil, "no path claimed")
        refused(function() G.point_reachable(P1, { 5, 0, 0 }) end, "no walkable path there")
    end)
    BG3AI.Nav.helperZone = nil
    if not ok then error(err, 0) end
end)

test("approach / point move: no path -> names the closest walkable spot (S021)", function()
    nav.fn = function() return { reachable = false } end
    BG3AI.Nav.closestReach = function(_, goal)
        return { pos = { goal[1] - 3, goal[2] - 4.6, goal[3] }, path_m = 21, to_goal_m = 5.2, dy_to_goal = 4.6 }
    end
    refused(function() G.approach_point(P1, NPC, 2.5) end, "closest walkable spot (")
    refused(function() G.point_reachable(P1, { 5, 0, 0 }) end, "4.6m above that spot")
    BG3AI.Nav.closestReach = nil
    refused(function() G.point_reachable(P1, { 5, 0, 0 }) end, "may need a jump or another route)")
end)

test("approach: a big target is used from its rim (S044 23:50 the Hollow Tower: closest spot 3.0 m from it)", function()
    nav.fn = function() return { reachable = false } end -- every ring point lies over the hole
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ bounds = function() return { radius = 2.0, height = 1 } end }, { __index = savedV })
    BG3AI.Nav.closestReach = function(_, goal)
        return { pos = { goal[1] - 3, goal[2], goal[3] }, path_m = 6, to_goal_m = 3.0, dy_to_goal = 0 }
    end
    local ok, err = pcall(function()
        local p = G.approach_point(P1, NPC, 1.5)
        eq(p.rim, true, "rim"); near(p.length_m, 6, "walk")
        BG3AI.Nav.closestReach = function(_, goal)  -- far from the rim: still refused
            return { pos = { goal[1] - 8, goal[2], goal[3] }, path_m = 6, to_goal_m = 8.0, dy_to_goal = 0 }
        end
        refused(function() G.approach_point(P1, NPC, 1.5) end, "no walkable path")
    end)
    BG3AI.Visibility, BG3AI.Nav.closestReach = savedV, nil
    if not ok then error(err, 0) end
end)

test("approach: only with a jump -> refused, says jump", function()
    nav.fn = function() return { reachable = true, length_m = 5, needs_jump = true } end
    refused(function() G.approach_point(P1, NPC, 2.5) end, "needs a jump")
end)

test("approach: pathfinding unknown, close and in sight -> unverified", function()
    nav.fn = function() return { reachable = nil } end
    world[NPC].pos = { 6, 0, 0 }
    local p = G.approach_point(P1, NPC, 2.5)
    eq(p.unverified, true, "unverified")
end)

test("NAV-8: approach with this frame's path budget spent -> refused (no walk without a path proof)", function()
    nav.fn = function() return { reachable = nil, budget = true, unfinished = true } end
    world[NPC].pos = { 6, 0, 0 }
    refused(function() G.approach_point(P1, NPC, 2.5) end, "busy")
end)

test("approach: pathfinding unknown and far -> refused", function()
    nav.fn = function() return { reachable = nil } end
    world[NPC].pos = { 20, 0, 0 }
    refused(function() G.approach_point(P1, NPC, 2.5) end, "no walkable path")
end)

test("approach: a ladder seen from above is approached at its top (S025 08:03)", function()
    world[P1].pos = { 10, 15, 0 }
    world[NPC].pos = { 0, 0, 0 } -- the ladder's foot
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = function() return { 0.0, 15.0 } end
    BG3AI.Nav.ladderTop = function(from, u) if u == NPC and from[2] > 2.5 then return { 0, 15, 0 } end end
    nav.fn = function(_, pos) return { reachable = pos[2] > 10, length_m = 9 } end
    local ok, err = pcall(function()
        local p = G.approach_point(P1, NPC, 1.8)
        near(p.pos[2], 15, "on the top floor")
        near(p.tpos[2], 0, "tpos stays the foot (the walker's did-not-move check)")
        world[P1].pos = { 1, 15, 0 }
        eq(G.approach_point(P1, NPC, 1.8).here, true, "at the top: here")
        world[P1].pos = { 10, 0, 0 } -- from below: the foot, as before
        refused(function() G.approach_point(P1, NPC, 1.8) end, "no walkable path")
    end)
    Ext.Level.GetHeightsAt, BG3AI.Nav.ladderTop = heights, nil
    if not ok then error(err, 0) end
end)

-- ---- move_to -------------------------------------------------------------

test("move_to entity: walks to a verified point, never CharacterMoveTo", function()
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "status")
    eq(#called("CharacterMoveTo"), 0, "CharacterMoveTo calls")
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "move calls"); near(mv[1][3], 8.5, "dest x")
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    near(j.result.dist, 1.5, "dist to target"); eq(j.result.target, NPC, "target")
end)

test("move_to entity: unreachable -> failed job, no movement", function()
    nav.fn = function() return { reachable = false } end
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("no walkable path"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "moves")
end)

test("move_to entity: already there -> done without walking", function()
    world[NPC].pos = { 1.5, 0, 0 }
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "done", "status"); eq(j.result.already_there, true, "already_there")
    eq(#called("CharacterMoveToPosition"), 0, "moves")
end)

test("move_to entity within: stops inside the edge-to-edge melee reach (S025 Spiritual Weapon 2.4 m)", function()
    world[NPC].pos = { 2.0, 0, 0 }
    world[NPC].comps.Bound = { Bound = { AIBounds = { Stand = { Radius = 0.25 } } } }
    -- 2.0 m is 'already there' for a plain move (2.2 m centres) but outside 1.5 + 0.5 + 0.25 - 0.4 = 1.85 m.
    local j = job(op("move_to", { char = P1, target = NPC, within = 1.5 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "move calls"); near(mv[1][3], 0.5, "dest x (ring 1.5 m on the actor's side)")
    arrive(j, { 0.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.in_reach, true, "in_reach")
    world[NPC].comps.Bound = nil
end)

test("move_to entity within: already in reach -> done, in_reach", function()
    world[NPC].pos = { 1.8, 0, 0 }
    local j = job(op("move_to", { char = P1, target = NPC, within = 1.5 }))
    eq(j.status, "done", "status"); eq(j.result.already_there, true, "already_there")
    eq(j.result.in_reach, true, "in_reach")
end)

-- A jump that lands where the walk was going waits A.TELEPORT_SETTLE_MS for a story signal (teleport_check).
local function settle() tick(math.ceil(A.TELEPORT_SETTLE_MS / 60) + 1) end
local function last_event(kind, since)
    for i = #RPC.events, 1, -1 do
        local ev = RPC.events[i]
        if ev.seq <= (since or 0) then return nil end
        if ev.kind == kind then return ev end
    end
end

test("move_to: teleport is detected and purged", function()
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 30, 0, 0 }
    tick()
    eq(j.status, "running", "settling"); assert(#called("PurgeOsirisQueue") >= 1, "purged at once")
    settle()
    eq(j.status, "failed", "status"); assert(j.error:find("engine teleported"), j.error)
    eq(j.result.teleported, true, "teleported"); eq(j.result.by_game, nil, "ours")
    local ev = last_event("teleport_blocked")
    assert(ev and ev.data.m == 30, "teleport_blocked m")
end)

test("V9 S066: a jump onto the walk's spot + the walker pulled into a conversation = the game moved it", function()
    local seq = RPC.eventSeq
    local j = job(op("move_to", { char = P1, pos = { 21.7, -1, 0 } }))
    tick()
    world[P1].pos = { 21.7, -1.3, 0.4 } -- Orin's arena: 21.7 m in one tick, 0.5 m from where the walk was going
    tick()
    eq(j.status, "running", "settling")
    A.dispatch("DialogActorJoined", { "ORIN_DLG", 7, P1, 0 })
    eq(j.status, "interrupted", "status"); eq(j.result.by_game, true, "by_game")
    assert(j.error:find("the game moved", 1, true) and j.error:find("a conversation started", 1, true), j.error)
    near(j.result.m, 21.7, "m"); eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
    eq(last_event("teleported_by_game", seq).data.why, "a conversation started", "event")
end)

test("V9 S066: a quest step unlocking right after the jump is the story's too", function()
    local j = job(op("move_to", { char = P1, pos = { 21.7, -1, 0 } }))
    tick()
    world[P1].pos = { 21.7, -1.3, 0.4 }
    tick(5)
    A.dispatch("QuestUpdateUnlocked", { P1, "CTY_OrinNetherstone", "Step" })
    eq(j.status, "interrupted", "status"); eq(j.result.why, "a quest updated", "why")
end)

test("V9: a conversation another member joins while the jump settles is no signal", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 50, 0, 0 }, comps = {} }
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 30, 0, 0 }
    tick()
    A.dispatch("DialogActorJoined", { "OTHER_DLG", 8, P2, 0 })
    eq(j.status, "running", "still settling")
    settle()
    eq(j.status, "failed", "the engine's")
end)

test("V9 S063: a jump that lands far from where the walk was going is the game's at once", function()
    local j = job(op("move_to", { char = P1, pos = { 3, 0, 0 } }))
    tick()
    world[P1].pos = { 0.5, -22, 0 } -- the ritual drop: 22 m straight down, the walk was going 3 m E
    tick()
    eq(j.status, "interrupted", "status"); eq(j.result.by_game, true, "by_game")
    assert(j.error:find("landed 22 m from where it was walking", 1, true), j.error)
end)

test("V9 S036 19:52:10: the Faith-Leap trial's put-back mid-walk is the game's, not a blocked teleport", function()
    -- runtime/actions.jsonl: move_to (-780,-28.3,-727.2) from (-773.7,-28.3,-727.4) (no drop first) put back at
    -- (-758.9,-25.9,-727.1): it read "engine teleported the character (not allowed)"
    local seq = RPC.eventSeq
    world[P1].pos = { -772, -28.3, -727.2 }
    local j = job(op("move_to", { char = P1, pos = { -780, -28.3, -727.2 } }))
    world[P1].pos = { -773.7, -28.3, -727.4 }
    tick()
    world[P1].pos = { -758.9, -25.9, -727.1 }
    tick()
    eq(j.status, "interrupted", "status " .. tostring(j.error)); eq(j.result.by_game, true, "by_game")
    eq(j.error, "the game moved the character (it landed 21 m from where it was walking), not our walk", "facts")
    eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
    world[P1].pos = { 0, 0, 0 }
end)

-- V15 S073 22:33:39: Lower City Central Wall reached at 23509.9 (bg3le log clock), the walk from the waypoint began 0.5 s later;
-- at 23518.86, 19 m along it, Claude jumped 29.8 m (7.8 m down) onto the walk's own end, no story signal with it.
local S073_FROM, S073_END = { -48.5, 27.9, -73.5 }, { -47.7, 20.1, -102.2 }
local function s073_jump(after_ms, land)
    op("trip_arrived", {})
    world[P1].pos = { S073_FROM[1], S073_FROM[2], S073_FROM[3] }
    local j = job(op("move_to", { char = P1, pos = S073_END }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local sway = 0.05
    while clock - A.trip_t < after_ms do -- 5 cm to and fro a tick: not standing still (the 5 s stuck check)
        sway = -sway
        world[P1].pos = { S073_FROM[1] + sway, S073_FROM[2], S073_FROM[3] }
        tick()
    end
    world[P1].pos = land or S073_END
    tick()
    return j
end

test("V15 S073: a jump onto the walk's end 9 s after a fast travel is the game's: the walk is done", function()
    local seq = RPC.eventSeq
    local j = s073_jump(9000)
    eq(j.status, "running", "settling (a story signal may still come)")
    settle()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.by_trip, true, "by_trip")
    near(j.result.m, 29.8, "m"); near(j.result.dist, 0, "at the walk's end")
    assert(j.result.after_trip_s >= 9 and j.result.after_trip_s < 9.2, "after_trip_s " .. j.result.after_trip_s)
    eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
    eq(last_event("teleported_by_game", seq), nil, "no story move (it stops the member's queue)")
    near(last_event("moved_after_trip", seq).data.m, 29.8, "event")
end)

test("V15: the same jump 16 s after the arrival is still the engine's teleport", function()
    local j = s073_jump(16000)
    settle()
    eq(j.status, "failed", "status"); assert(j.error:find("engine teleported"), j.error)
    eq(j.result.by_trip, nil, "by_trip")
end)

test("V15: a jump off the walk's end right after a fast travel is the game's move, not ours", function()
    local seq = RPC.eventSeq
    local j = s073_jump(3000, { -48.0, 27.9, -78.5 }) -- 5 m along, 24 m short of the end
    settle()
    eq(j.status, "interrupted", "status"); eq(j.result.by_game, true, "by_game")
    assert(j.error:find("3 s after the fast travel", 1, true), j.error)
    eq(last_event("teleport_blocked", seq), nil, "not a blocked teleport")
end)

test("V15: an approach put on its spot right after a fast travel does not count its action as done", function()
    op("trip_arrived", {})
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.phase, "approach", "phase")
    tick()
    world[P1].pos = { j.walk.pos[1], j.walk.pos[2], j.walk.pos[3] }
    tick()
    settle()
    eq(j.status, "interrupted", "status " .. tostring(j.error)); eq(#called("Use"), 0, "no Use")
end)

test("V9 S066: the members walking to him keep walking when his conversation starts", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 0, 0, 5 }, comps = {} }
    osiv[key("IsCharacter", P2)] = 1; osiv[key("IsOnStage", P2)] = 1
    local j = job(op("move_to", { char = P2, pos = { 20, 0, 5 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    A.dispatch("DialogActorJoined", { "ORIN_DLG", 7, P1, 0 })
    eq(j.status, "running", "another member's conversation")
    A.dispatch("DialogActorJoined", { "ORIN_DLG", 7, P2, 0 })
    eq(j.status, "interrupted", "its own"); eq(j.error, "a conversation started", "why")
end)

-- vis V25, S084 04:16-04:22 (Counting House vault, 9 Marble Plates, code 1-3-5-6): every walk crossed other plates
-- and reset the code. Plates the party has seen (V.fixtureSeen) are walked round; move_to(target=plate) stands on it.
-- Nav.route/routeFrom are straight lines here (the plates' geometry is the test).
local PLATE_A = "a1a1a1a1-0000-0000-0000-00000000000a"
local PLATE_T = "a2a2a2a2-0000-0000-0000-00000000000b"
local function with_plates(plates, seen, fn)
    local savedV, savedR, savedF = BG3AI.Visibility, BG3AI.Nav.route, BG3AI.Nav.routeFrom
    local names, fixtureSeen = {}, {}
    for u, p in pairs(plates) do
        world[u] = { pos = p, comps = {} }
        names[u] = "Marble Plate"
        osiv[key("IsItem", u)] = 1
        osiv[key("IsOnStage", u)] = 1
        osiv[key("CanSee", P1, u)] = 1
        osiv[key("HasLineOfSight", P1, u)] = 1
    end
    for _, u in ipairs(seen) do fixtureSeen[u] = { t = clock, level = "CTY_Main_A" } end
    BG3AI.Visibility = setmetatable({
        PLATE_NAMES = { ["Marble Plate"] = true }, fixtureSeen = fixtureSeen, FIXTURE_REMEMBER_MS = 300000,
        levelNow = function() return "CTY_Main_A" end, name = function(u) return names[u] end,
        bounds = function() return { radius = 0.6, height = 0.1 } end,
    }, { __index = savedV })
    BG3AI.Nav.route = function(_, to) return { { world[P1].pos[1], world[P1].pos[2], world[P1].pos[3] }, to } end
    BG3AI.Nav.routeFrom = function(_, from, to) return { from, to } end
    local ok, err = pcall(fn)
    BG3AI.Visibility, BG3AI.Nav.route, BG3AI.Nav.routeFrom = savedV, savedR, savedF
    if not ok then error(err, 0) end
end

test("move_to V25: a seen plate on the straight way is walked round via a point beside it", function()
    with_plates({ [PLATE_A] = { 5, 0, 0 } }, { PLATE_A }, function()
        local j = job(op("move_to", { char = P1, pos = { 10, 0, 0 } }))
        eq(j.status, "running", "status " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "one walk at a time")
        near(mv[1][3], 5, "via x"); near(math.abs(mv[1][5]), 1.6, "via z: beside the plate")
        arrive(j, { mv[1][3], 0, mv[1][5] })
        mv = called("CharacterMoveToPosition")
        eq(#mv, 2, "then the destination"); near(mv[2][3], 10, "dest x"); near(mv[2][5], 0, "dest z")
        arrive(j, { 10, 0, 0 })
        eq(j.status, "done", "status " .. tostring(j.error))
        near(j.result.round_plates[1], 5, "round_plates")
        eq(j.result.over_plates, nil, "over_plates")
    end)
end)

test("move_to V25: no clear way round -> walks as before and names the plate it crosses", function()
    with_plates({ [PLATE_A] = { 5, 0, 0 } }, { PLATE_A }, function()
        BG3AI.Nav.routeFrom = function() return nil end
        local j = job(op("move_to", { char = P1, pos = { 10, 0, 0 } }))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "moves"); near(mv[1][3], 10, "straight to the destination")
        arrive(j, { 10, 0, 0 })
        eq(j.status, "done", "status " .. tostring(j.error))
        eq(j.result.over_plates and j.result.over_plates[1], PLATE_A, "over_plates")
    end)
end)

test("move_to V25: a plate the party has not seen is not avoided (fair), nor the one it stands on", function()
    with_plates({ [PLATE_A] = { 5, 0, 0 }, [PLATE_T] = { 0.3, 0, 0 } }, { PLATE_T }, function()
        local j = job(op("move_to", { char = P1, pos = { 10, 0, 0 } }))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "moves"); near(mv[1][3], 10, "straight"); near(mv[1][5], 0, "straight z")
        arrive(j, { 10, 0, 0 })
        eq(j.result.round_plates, nil, "round_plates"); eq(j.result.over_plates, nil, "over_plates")
    end)
end)

test("move_to V25: target=<plate> walks onto its centre (not the ring beside it), round the other plates", function()
    with_plates({ [PLATE_A] = { 3, 0, 0 }, [PLATE_T] = { 6, 0, 0 } }, { PLATE_A, PLATE_T }, function()
        local j = job(op("move_to", { char = P1, target = PLATE_T }))
        eq(j.status, "running", "status " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        near(math.abs(mv[1][5]), 1.6, "via beside plate A")
        arrive(j, { mv[1][3], 0, mv[1][5] })
        mv = called("CharacterMoveToPosition")
        near(mv[2][3], 6, "onto the plate x"); near(mv[2][5], 0, "onto the plate z")
        arrive(j, { 6, 0, 0.2 })
        eq(j.status, "done", "status " .. tostring(j.error))
        eq(j.result.on_plate, PLATE_T, "on_plate")
    end)
end)

test("move_to V25: a plate walk that stops beside the plate fails, saying so", function()
    with_plates({ [PLATE_T] = { 6, 0, 0 } }, { PLATE_T }, function()
        local j = job(op("move_to", { char = P1, target = PLATE_T }))
        arrive(j, { 4.8, 0, 0 })
        eq(j.status, "failed", "status")
        assert(j.error:find("not on it (1.2 m from its centre)", 1, true), j.error)
    end)
end)

test("move_to V25: already on the plate -> done, no walk", function()
    with_plates({ [PLATE_T] = { 0.2, 0, 0 } }, { PLATE_T }, function()
        local j = job(op("move_to", { char = P1, target = PLATE_T }))
        eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.on_plate, PLATE_T, "on_plate")
        eq(#called("CharacterMoveToPosition"), 0, "moves")
    end)
end)

-- vis V26, S084 04:09-04:12 (the Counting House): let off "with a warning. This time.", then travel_to e578 walked
-- into the vault guards' off-limits area (Dadger's talk at (-111,4,-177)) and the second offence started a fight.
-- The bridge passes the areas the game warned about as avoid; walks go round them, never into them.
local DADGER = { id = 2, pos = { -111, 4, -177 }, r = 6, level = "CTY_Main_A",
    name = "the off-limits area by Cashguard Artillerist Dadger (-111,4,-177)" }
local function move_refused(args, pat)
    local r = op("move_to", args)
    eq(r.status, "failed", "status")
    assert(r.error:find(pat, 1, true), r.error)
end

test("move_to V26: a destination inside a warned off-limits area is refused, naming it (S084 travel_to e578)", function()
    with_plates({}, {}, function()
        world[P1].pos = { -92.2, 5.7, -188.2 } -- the east office, after the loot
        world[DOOR].pos = { -112, 4, -176 }    -- e578 Grated Iron Doors, by the guards
        move_refused({ char = P1, target = DOOR, avoid = { DADGER } },
            "the destination is inside the off-limits area by Cashguard Artillerist Dadger (-111,4,-177) you were warned about")
        eq(#called("CharacterMoveToPosition"), 0, "no walk")
        move_refused({ char = P1, pos = { -110, 4, -178 }, avoid = { DADGER } }, "trespass=true")
        local j = job(op("move_to", { char = P1, pos = { -110, 4, -178 }, avoid = { DADGER }, trespass = true }))
        eq(j.status, "running", "trespass=true walks in: " .. tostring(j.error))
    end)
end)

test("move_to V26: a way through a warned area goes round it and says so", function()
    with_plates({}, {}, function()
        world[P1].pos = { -100, 4, -177 }
        local j = job(op("move_to", { char = P1, pos = { -122, 4, -177 }, avoid = { DADGER } }))
        eq(j.status, "running", "status " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "via first")
        local via = { mv[1][3], mv[1][4], mv[1][5] }
        assert(math.abs(via[3] + 177) > 6, "via clear of the area: z=" .. via[3])
        arrive(j, via)
        arrive(j, { -122, 4, -177 })
        eq(j.status, "done", "status " .. tostring(j.error))
        assert(j.result.avoiding and j.result.avoiding:find("Dadger", 1, true), tostring(j.result.avoiding))
        assert(j.result.avoiding:find("you were warned about", 1, true), j.result.avoiding)
    end)
end)

test("move_to V26: no way round a warned area -> refused, never walked through", function()
    with_plates({}, {}, function()
        world[P1].pos = { -100, 4, -177 }
        BG3AI.Nav.routeFrom = function() return nil end
        move_refused({ char = P1, pos = { -122, 4, -177 }, avoid = { DADGER } },
            "crosses the off-limits area by Cashguard Artillerist Dadger (-111,4,-177) you were warned about and no way round")
        eq(#called("CharacterMoveToPosition"), 0, "no walk")
    end)
end)

test("move_to V26: no warning, another level, another floor, or standing in it -> walks as before", function()
    with_plates({}, {}, function()
        world[P1].pos = { -100, 4, -177 }
        local function straight(avoid)
            calls = {}
            local j = job(op("move_to", { char = P1, pos = { -122, 4, -177 }, avoid = avoid }))
            local mv = called("CharacterMoveToPosition")
            eq(#mv, 1, "one walk"); near(mv[1][3], -122, "straight to the destination")
            j.status = "done"
            A.active = {}
        end
        straight(nil)
        straight({ { id = 1, pos = DADGER.pos, r = 6, level = "SCL_Main_A", name = "x" } })
        straight({ { id = 1, pos = { -111, 12.5, -177 }, r = 6, level = "CTY_Main_A", name = "upstairs" } })
        world[P1].pos = { -108, 4, -177 } -- inside it already: walking out is right
        calls = {}
        local j = job(op("move_to", { char = P1, pos = { -122, 4, -177 }, avoid = { DADGER } }))
        eq(j.status, "running", "status " .. tostring(j.error))
        near(called("CharacterMoveToPosition")[1][3], -122, "straight out")
    end)
end)

-- S001 leg 5: a 7 m single-tick step along the engine's own route (a jump link
-- or a frame hitch) is the walk, not a teleport. Nav.route/onRoute are stubbed
-- here; their geometry is tested in tools/dev/test_nav_lua.lua.
local function with_route(on)
    BG3AI.Nav.route = function() return { { 0, 0, 0 }, { 30, 0, 0 } } end
    BG3AI.Nav.onRoute = function() return on end
end
local function without_route() BG3AI.Nav.route, BG3AI.Nav.onRoute = nil, nil end

test("move_to: a big step along the walk's own route is not a teleport", function()
    with_route(true)
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 7, 0, 0 }
    tick()
    without_route()
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "purged")
end)

test("move_to: a big step off the route is still a teleport", function()
    with_route(false)
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 7, 0, 9 }
    tick()
    without_route()
    eq(j.status, "interrupted", "status"); eq(j.result.teleported, true, "teleported") -- 23 m off the walk's end
end)

test("NAV-8: a 3 m jump within one 60 ms job tick is a teleport (both old tests passed it)", function()
    A.TELEPORT_SMALL_M = 1.5
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    world[P1].pos = { 0.3, 0, 0 }
    tick()
    eq(j.status, "running", "a walk's step")
    world[P1].pos = { 3.3, 0, 0 } -- jobs tick every TICK_MS 50 ms: 60 ms here
    tick()
    settle() -- a 3 m jump is never the story's at once
    eq(j.status, "failed", "status"); assert(j.error:find("teleported"), j.error)
end)

test("NAV-8: a walk's step after a frame hitch (2 m in 300 ms) is not a teleport", function()
    A.TELEPORT_SMALL_M = 1.5
    local j = job(op("move_to", { char = P1, pos = { 30, 0, 0 } }))
    tick()
    clock = clock + 240 -- a 300 ms tick
    world[P1].pos = { 2, 0, 0 }
    tick()
    eq(j.status, "running", "status " .. tostring(j.error))
end)

-- Bench act3_gold_max 12:50:50: a 1 m leg ran 31.8 m round the others at the spot until its 9.3 s deadline (TIMEOUT).
local function run_round(n, centre, r)
    for _ = 1, n do -- 0.3 m per 60 ms tick on a circle of radius r round centre: moving, never closer
        local p = world[P1].pos
        local a = math.atan(p[3] - centre[3], p[1] - centre[1]) + 0.3 / r
        world[P1].pos = { centre[1] + r * math.cos(a), 0, centre[3] + r * math.sin(a) }
        tick()
    end
end

test("move_to: a walker that keeps running round the spot fails once it ran 2x its route + 10 m", function()
    world[P1].pos = { -3, 0, 0 }
    nav.fn = function() return { reachable = true, length_m = 1.0 } end -- the engine's route: 1 m
    local j = job(op("move_to", { char = P1, pos = { 1, 0, 0 } }))
    run_round(36, { 1, 0, 0 }, 4) -- 10.8 m
    eq(j.status, "running", "under the cap " .. tostring(j.error))
    run_round(6, { 1, 0, 0 }, 4) -- 12.6 m: past 2 x 1 + 10, 2.5 s into a 9.25 s deadline
    eq(j.status, "failed", "status")
    assert(j.error:find("could not get closer", 1, true) and j.error:find("on a 1.0 m route", 1, true), j.error)
    assert(#called("PurgeOsirisQueue") >= 1, "purged")
    reset()
    local j2 = job(op("move_to", { char = P1, pos = { 20, 0, 0 } })) -- a 20 m route walked round something: 1.5x
    run_round(100, { 20, 0, 0 }, 20)
    eq(j2.status, "running", "within the cap " .. tostring(j2.error))
end)

-- ---- combat movement -------------------------------------------------------

function combat_setup(mv)
    osiv[key("IsInCombat", P1)] = 1
    world[P1].comps.TurnBased = { IsActiveCombatTurn = true }
    world[P1].comps.ActionResources = { Resources = { Movement = { { Level = 0, Amount = mv } },
        ActionPoint = { { Level = 0, Amount = 1 } } } }
end

test("combat move_to entity: path beyond movement -> refused", function()
    combat_setup(6)
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("beyond remaining movement"), j.error)
end)

test("combat move_to entity: within movement, guard charges what the engine did not", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    near(j.result.movement_charged_by_guard, 8.5, "charged")
    near(j.result.after.movement, 3.5, "movement left")
end)

test("combat move_to: a walk that never starts fails after 2.5 s (S074 r2)", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    tick(40) -- 2.4 s standing still: the old walk waited 5 s for "no path to the destination"
    eq(j.status, "running", "before the window")
    tick(3)
    eq(j.status, "failed", "status")
    assert(j.error:find("never started moving (2.5 s, the route was there)", 1, true), j.error)
    assert(not j.error:find("not this character's turn", 1, true), "the turn goes on: " .. j.error)
    assert(#called("PurgeOsirisQueue") >= 1, "the order is purged")
end)

test("combat move_to: a Lethargic walker is refused at once (S091 08:30; S074 r2 / S089 stood still 2.5-5 s)", function()
    combat_setup(12)
    stats["HASTE_LETHARGY"] = { StatusType = "INCAPACITATED" }
    G._disabling = {}
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function() return { { id = "HASTE_LETHARGY", name = "Lethargic" } } end },
        { __index = savedV })
    local n0 = #called("CharacterMoveToPosition")
    local j = job(op("move_to", { char = P1, target = NPC }))
    BG3AI.Visibility = savedV
    eq(j.status, "failed", "refused " .. tostring(j.error))
    eq(j.error, "cannot move while Lethargic (the status keeps it in place this turn)")
    eq(#called("CharacterMoveToPosition"), n0, "no walk ordered")
    G._disabling = {}
end)

test("combat move_to: the turn passing mid-walk stops it at once with the fatal turn text (S074 r2)", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    world[P1].pos = { 1, 0, 0 }
    tick()
    world[P1].comps.TurnBased.IsActiveCombatTurn = false -- the game ended the turn (now Cazador's)
    tick(2)
    eq(j.status, "running", "a flag seen for 2 ticks is not a stop yet")
    tick()
    eq(j.status, "failed", "status")
    assert(j.error:find("not this character's turn any more: the game passed the turn on during the walk (travelled 1.0 m)",
        1, true), j.error)
end)

test("combat move_to: the flag dropping because the fight ended is not a stop; no start window out of combat", function()
    combat_setup(12)
    local j = job(op("move_to", { char = P1, target = NPC }))
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    osiv[key("IsInCombat", P1)] = nil -- the last enemy died: out of combat, the walk goes on
    tick(60) -- 3.6 s, past the combat no-start window too: the walk began in combat, but the fight is over
    eq(j.status, "running", "walks on " .. tostring(j.error))
    reset()
    j = job(op("move_to", { char = P1, target = NPC }))
    tick(60)
    eq(j.status, "running", "out of combat: the old 5 s rule only")
    tick(30)
    eq(j.status, "failed", "5 s"); assert(j.error:find("no path to the destination", 1, true), j.error)
end)

test("combat loot approach: budget enforced (click ops walk in combat too)", function()
    combat_setup(4)
    osiv[key("IsDead", NPC)] = 1
    local j = job(op("loot_corpse", { char = P1, target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("beyond remaining movement"), j.error)
end)

-- ---- interact / pickup -----------------------------------------------------

test("interact: approach first, Use only after arriving", function()
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 0, "Use before arrival")
    eq(j.phase, "approach", "phase")
    local mv = called("CharacterMoveToPosition")
    near(mv[1][3], 8.5, "dest x") -- reach 1.8: radii 1.0/1.5
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Use"), 1, "Use after arrival")
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
end)

-- V24, S083 03:55:56-58: Knock unlocked e720 (look: "unlocked"), 2 s later IsLocked read true again and interact
-- refused "nobody in the party carries its key" while EVENTS had door_opened e720.
local function knocked_door()
    world[DOOR].comps.Lock = { Key_M = "" }
    osiv[key("IsLocked", DOOR)] = 1
end
local function interact_refused(pat)
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status")
    assert(j.error:find(pat, 1, true), j.error)
end

test("interact V24: a door the game unlocked 2 s ago that reads locked again is clicked, not refused", function()
    knocked_door()
    A.dispatch("Unlocked", { DOOR, P2, "NULL_00000000-0000-0000-0000-000000000000" })
    clock = clock + 2000
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Use"), 1, "the click is sent")
    osiv[key("IsLocked", DOOR)] = nil -- the game finishes unlocking it; the Use opens it
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
end)

test("interact V24: still locked after that click says the game locked it again, not 'no lock to pick'", function()
    knocked_door()
    A.dispatch("Opened", { DOOR })
    local j = job(op("interact", { char = P1, target = DOOR }))
    arrive(j, { 8.5, 0, 0 })
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    clock = clock + 1600 tick()
    eq(j.status, "failed", "status"); eq(j.error, A.RELOCKED, "error")
end)

test("interact V24: a door opened moments ago that still reads locked: 'it is open now', no Use", function()
    knocked_door()
    A.dispatch("Opened", { DOOR })
    osiv[key("IsOpened", DOOR)] = 1
    clock = clock + 2000
    interact_refused("it is open now (the game unlocked or opened it 2 s ago)")
    eq(#called("Use"), 0, "Use")
end)

test("interact V24: an unlock older than LOCK_FRESH_MS no longer counts: still locked, not 'locked again'", function()
    knocked_door()
    world[DOOR].pos = { 1, 0, 0 }
    A.dispatch("Unlocked", { DOOR, P2, "" })
    clock = clock + A.LOCK_FRESH_MS + 1
    local j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(6)
    eq(j.status, "failed", "status"); eq(j.error, A.STILL_LOCKED, "error")
end)

-- Live L3 helm probe 2026-10-17 (e39 at the Nautiloid helm): the ring point facing the actor lies under the upper
-- landing; GetHeightsAt there gives only the upper floor, and N.path to it is a partial path (ends 4.2 m short).
local function helm_heights(x) if x < 9.2 then return { -12.0, 3.1 } end return { 0.0, 3.1 } end

test("G.reachable: a partial path (ends short of the point) is not reachable; a point move names where it ends", function()
    nav.fn = function() return { reachable = true, partial = true, end_off = 4.2, end_pos = { 6.8, 0, 1.4 }, length_m = 1.8 } end
    local r, info = G.reachable(P1, { 8.2, 3.1, 0 })
    eq(r, false, "reachable"); eq(info.partial, true, "partial"); eq(info.end_off, 4.2, "end_off")
    refused(function() G.point_reachable(P1, { 8.2, 3.1, 0 }) end, "the walkable ground ends 4.2m short of it, at (6.8,0.0,1.4)")
end)

test("G.point_reachable: a path over the Faith-Leap's hidden floor is not refused (user, 2026-10-18: a misstep is the game's)", function()
    nav.fn = function() return { reachable = true, length_m = 9.0, hidden_floor = { pos = { -9, 0, 0 }, at_m = 9.0, winds = true } } end
    local r, info = G.reachable(P1, { -16, 0, 0 })
    eq(r, true, "reachable"); eq(info.hidden_floor, nil, "not passed on")
    G.point_reachable(P1, { -16, 0, 0 })
end)

test("approach: a ladder's foot is approached on the foot's floor, not under its upper landing (live L3 helm probe)", function()
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = helm_heights
    nav.fn = function(_, pos)
        if pos[2] > 1.5 then return { reachable = true, partial = true, end_off = 4.2, length_m = 1.8 } end
        return { reachable = true, length_m = 6.7 }
    end
    local ok, err = pcall(function()
        local p = G.approach_point(P1, LADDER, A.USE_REACH)
        near(p.pos[2], 0, "on the foot's floor"); near(p.length_m, 6.7, "the full path")
        near(p.use_y, 0, "use_y = the foot")
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("approach: a ladder ring point on the other floor is skipped even when it reads reachable", function()
    local heights = Ext.Level.GetHeightsAt
    Ext.Level.GetHeightsAt = helm_heights
    nav.fn = function(_, pos) return { reachable = true, length_m = pos[2] > 1.5 and 1.8 or 6.7 } end
    local ok, err = pcall(function()
        near(G.approach_point(P1, LADDER, A.USE_REACH).pos[2], 0, "ladder: on the foot's floor")
        near(G.approach_point(P1, GEM, A.USE_REACH).pos[2], 3.1, "not a ladder: the nearest floor point, as before")
    end)
    Ext.Level.GetHeightsAt = heights
    if not ok then error(err, 0) end
end)

test("interact ladder: the approach walk already climbed it -> done climbed=up, no Use (live L3 helm probe)", function()
    local j = job(op("interact", { char = P1, target = LADDER }))
    eq(j.phase, "approach", "walks first")
    arrive(j, { 9.6, 2.4, 0 }) -- the engine's walk went up the ladder and stepped off at the top
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.climbed, "up", "climbed"); eq(j.result.climbed_on_walk, true, "on the walk")
    eq(#called("Use"), 0, "no Use (it would climb back down)")
end)

test("interact ladder: a walk that ends on the foot's floor still settles and Uses", function()
    local j = job(op("interact", { char = P1, target = LADDER }))
    arrive(j, { 9.0, 0, 0 })
    eq(j.status, "running", "status " .. tostring(j.error))
    tick() tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use")
end)

test("interact in combat names the tool a player uses: click_on <target> (naut3 x3: '(use the UI)')", function()
    osiv[key("IsInCombat", P1)] = 1
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.status, "failed", "status")
    assert(j.error:find("click_on " .. GEM, 1, true), tostring(j.error))
    eq(#called("Use"), 0, "no Use")
end)

test("interact: a ladder-like jump next to the item is not a teleport", function()
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use right away")
    tick()
    world[P1].pos = { 9, 8, 0 }
    tick()
    eq(j.status, "running", "status " .. tostring(j.error))
end)

test("interact: a ladder used from its top landing (item position 3.2 m below) uses, no re-plan loop", function()
    world[GEM].pos = { 10, -3, 0 }
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.phase, "approach", "phase")
    j.approach.plan.pos = { 10, 0.2, 0 } -- the verified landing next to the ladder's top
    arrive(j, { 10, 0.2, 0 })
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(j.phase, "act", "phase"); eq(#called("Use"), 1, "Use"); eq(j.approach.replans, 0, "replans")
end)

test("interact: Use's own walk teleporting from afar is caught", function()
    nav.fn = function() return { reachable = nil } end -- unverified short approach
    world[DOOR].pos = { 6, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    -- the walk ends 2.7 m short (within reach + slack): Use must walk the rest
    world[P1].pos = { 3.3, 0, 0 }; tick()
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    eq(#called("Use"), 1, "Use")
    tick()
    world[P1].pos = { 3.3, 0, 9 }; tick()
    settle()
    eq(j.status, "failed", "status"); assert(j.error:find("teleported"), tostring(j.error))
end)

test("interact: an item in the actor's own bag is used in place", function()
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("CanSee", P1, GEM)] = nil -- carried items are not "visible" in the world
    world[GEM].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("Use"), 1, "Use"); eq(#called("CharacterMoveToPosition"), 0, "moves")
end)

local function carried_use()
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("CanSee", P1, GEM)] = nil
    world[GEM].pos = { 0, 0, 0 }
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = { {} } } } -- the Use, queued
    return job(op("interact", { char = P1, target = GEM }))
end

test("interact: a carried Use the game never takes from the queue fails at 8 s, not a 38 s TIMEOUT (B3)", function()
    local j = carried_use()
    tick(130) -- 7.8 s
    eq(j.status, "running", "before 8 s")
    tick(5) -- 8.1 s
    eq(j.status, "failed", "status " .. tostring(j.error))
    assert(j.error:find("the game never started it (8 s, the Use still queued", 1, true), j.error)
    eq(j.result.purged_tasks, 1, "the queued Use purged")
end)

test("interact: a carried Use the game ran (UseFinished) is done; one being run (queue empty) waits", function()
    local j = carried_use()
    tick(5)
    A.dispatch("UseFinished", { P1, GEM, 1 })
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.used, true, "used")
    reset()
    j = carried_use()
    world[P1].comps.ServerCharacter.OsirisController.Tasks = {}
    tick(140) -- 8.4 s
    eq(j.status, "running", "taken from the queue: not 'never started'")
end)

test("job.notify (ACT-10): a waited job that outlived the wait announces its end as job_finished", function()
    local r = op("interact", { char = P1, target = DOOR })
    local j = job(r)
    local function finished()
        for _, ev in ipairs(RPC.events) do
            if ev.kind == "job_finished" and ev.data.job == "j" .. tostring(j.id) then return ev end
        end
    end
    eq(finished(), nil, "no event before")
    local v = RPC.ops["job.notify"]({ id = j.id })
    eq(v.status, "running", "view")
    RPC.finish_job(j, "done", { used = true })
    assert(finished(), "job_finished pushed")
    eq(RPC.ops["job.notify"]({ id = j.id }).status, "done", "an ended job's view")
end)

test("interact: an item in another character's bag is refused", function()
    osiv[key("GetInventoryOwner", GEM)] = NPC
    local j = job(op("interact", { char = P1, target = GEM }))
    eq(j.status, "failed", "status"); assert(j.error:find("another character"), j.error)
end)

test("pickup: approach first, then Pickup without force", function()
    local j = job(op("pickup", { char = P1, item = GEM }))
    eq(#called("Pickup"), 0, "Pickup before arrival")
    arrive(j, { 9, 0, 0 })
    local pk = called("Pickup")
    eq(#pk, 1, "Pickup after arrival"); eq(pk[1][5], 0, "ForcePickUpOnFailure")
    osiv[key("GetInventoryOwner", GEM)] = P1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
end)

test("talk: approach point within talk range, then DialogStartRequested", function()
    local j = job(op("talk", { char = P1, npc = NPC }))
    eq(#called("CharacterMoveTo"), 0, "CharacterMoveTo")
    arrive(j, { 9, 0, 0 })
    eq(j.phase, "requested", "phase")
end)
