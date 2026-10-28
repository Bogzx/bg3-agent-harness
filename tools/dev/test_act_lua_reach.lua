-- Part of test_act_lua.lua (split for the 1000-line cap): move_to range, the walk into a spell's reach (live N10-N13).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- Live N10-N13 (heights 0 here): Claude (-42.0,-44.7), Wyll (-38.5,-53.5), 9.4 m; Longstrider's touch reach 2.5 m
-- (centres, act.spell_reach). Each walk to the point 1.5 m from Wyll ended 1.8 m (N10) or 2.4 m (N13) short of it.
local CLAUDE, WYLL = { -42.0, 0, -44.7 }, { -38.5, 0, -53.5 }

-- The game's walk: straight toward the ordered point in 0.5 m steps, ending `short_m` before it (EntityEvent).
local function engine_walk(j, short_m)
    local mv = called("CharacterMoveToPosition")
    local from, to = world[P1].pos, { mv[#mv][3], mv[#mv][4], mv[#mv][5] }
    local len = U.dist(from, to) - short_m
    for s = 0.5, len, 0.5 do
        local f = s / U.dist(from, to)
        world[P1].pos = { from[1] + (to[1] - from[1]) * f, 0, from[3] + (to[3] - from[3]) * f }
        tick()
        if j.status ~= "running" then return end
    end
    A.dispatch("EntityEvent", { P1, j.walk.ev })
end

test("move_to range: a walk the game ends 1.8-2.4 m short of its goal still stops inside the reach (N10-N13)",
    function()
    for _, short_m in ipairs({ 1.8, 2.4 }) do
        reset()
        world[P1].pos, world[NPC].pos = { CLAUDE[1], 0, CLAUDE[3] }, WYLL
        local j = job(op("move_to", { char = P1, target = NPC, range = 2.5 }))
        eq(j.status, "running", "walking " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "one walk")
        engine_walk(j, short_m)
        eq(j.status, "done", short_m .. " m short: " .. tostring(j.error))
        eq(j.result.in_reach, true, short_m .. " m short: in reach")
        assert(U.dist({ mv[1][3], 0, mv[1][5] }, CLAUDE) > U.dist(WYLL, CLAUDE), "the goal lies past Wyll")
        assert(U.dist(world[P1].pos, WYLL) <= 1.5, "stopped on the way in: " .. U.dist(world[P1].pos, WYLL))
        eq(#called("PurgeOsirisQueue"), 1, "the walker stopped the walk itself")
        eq(#called("CharacterMoveToPosition"), 1, "no second leg")
    end
end)

test("move_to range: a walk that ends out of the range fails with the distance, not done (N10's 'done, 3.3 m')",
    function()
    world[P1].pos, world[NPC].pos = { CLAUDE[1], 0, CLAUDE[3] }, WYLL
    local j = job(op("move_to", { char = P1, target = NPC, range = 2.5 }))
    engine_walk(j, 9.0) -- the game ends it after 2.5 m, 7.0 m from Wyll
    eq(j.status, "failed", "status")
    assert(j.error:find("the walk ended 7.0 m from the target, beyond the 2.5 m range", 1, true), j.error)
    eq(j.result.in_reach, false, "in_reach")
end)

test("move_to range: no walkable goal past Wyll (or only a way round) -> the stop point itself, as before", function()
    for _, past in ipairs({ "unreachable", "round" }) do
        reset()
        world[P1].pos, world[NPC].pos = { CLAUDE[1], 0, CLAUDE[3] }, WYLL
        nav.fn = function(_, pos)
            local from = world[P1].pos
            local beyond = U.dist(from, pos) > U.dist(from, WYLL)
            if beyond and past == "unreachable" then return { reachable = false } end
            local len = U.dist(from, pos) + (beyond and 3.5 or 0) -- "round": 3.5 m more than the line
            return { reachable = true, length_m = len, nodes = 1, points = { from, pos } }
        end
        local j = job(op("move_to", { char = P1, target = NPC, range = 2.5 }))
        eq(j.status, "running", past .. ": " .. tostring(j.error))
        local mv = called("CharacterMoveToPosition")
        near(U.dist({ mv[1][3], 0, mv[1][5] }, WYLL), 1.5, past .. ": the point 1.5 m from Wyll")
        assert(U.dist({ mv[1][3], 0, mv[1][5] }, CLAUDE) < U.dist(WYLL, CLAUDE), past .. ": on Claude's side")
    end
end)
