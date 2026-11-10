-- Part of test_act_lua.lua (split for the 1000-line cap): pick_up on a target nobody can walk to.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- path_preview's read (nav.path, NavigationReach.lua) as the game answered it for Omotola's Chest Key
-- (v011-day-opus act3_gold_max 2026-11-10): a found path that ends 4 m short after the free spots were checked.
local KEY_SHORT = { reachable = true, length_m = 70.2, partial = true, end_pos = { -164.0, 24.0, -63.0 },
    end_off = 4.0 }

local function with_nav_path(res, body)
    local asked = {}
    RPC.ops["nav.path"] = function(args) asked[#asked + 1] = args return res end
    local ok, err = pcall(body, asked)
    RPC.ops["nav.path"] = nil
    if not ok then error(err, 0) end
end

test("pickup: a target whose walkable path ends short is refused before the start (act3_gold_max Chest Key)",
    function()
    with_nav_path(KEY_SHORT, function(asked)
        local j = job(op("pickup", { char = P1, item = GEM }))
        eq(j.status, "failed", "refused")
        eq(j.error, "can't reach it: the walkable ground ends 4.0m short of it, at (-164.0,24.0,-63.0)")
        eq(#called("Pickup"), 0, "no order sent")
        eq(#asked, 1, "one read"); eq(asked[1].char, P1, "the actor's path"); eq(asked[1].target, GEM, "to the item")
    end)
    -- the ring cut by its deadline is no proof, and a path that reaches is the usual walk
    for _, res in ipairs({ { reachable = true, partial = true, end_off = 4.0, free_spots_unchecked = true },
        { reachable = true, length_m = 10 }, { reachable = nil } }) do
        reset()
        with_nav_path(res, function()
            local j = job(op("pickup", { char = P1, item = GEM }))
            eq(j.status, "running", "started " .. tostring(j.error))
        end)
    end
    -- standing within reach: no walk, no read
    reset()
    world[P1].pos = { 9, 0, 0 }
    with_nav_path(KEY_SHORT, function(asked)
        eq(job(op("pickup", { char = P1, item = GEM })).status, "running", "next to it")
        eq(#asked, 0, "not read")
    end)
end)

test("pickup: the second re-send gone with nothing picked fails then, not at the 40 s timeout (Chest Key)", function()
    world[P1].pos = { 9, 0, 0 }
    local j = job(op("pickup", { char = P1, item = GEM }))
    clock = clock + A.PICKUP_REORDER_MS tick()
    clock = clock + A.PICKUP_REORDER_MS tick()
    eq(#called("Pickup"), 3, "sent, then re-sent twice")
    eq(j.status, "running", "the second re-send gets its time")
    clock = clock + A.PICKUP_REORDER_MS tick()
    eq(j.status, "failed", "status")
    eq(j.error, "the game didn't run the pickup: sent 3 times, each order left the queue with nothing picked up and"
        .. " no failure from the game (1.0m from it)")
    eq(#called("Pickup"), 3, "no 4th order")
end)

test("pickup: a big target used from its rim, or a walk with no path answer, is not read (S044 the Hollow Tower)",
    function()
    -- rim: every ring spot lies over it, the closest walkable spot is within reach of its edge (approach_point)
    nav.fn = function() return { reachable = false } end
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ bounds = function() return { radius = 2.0, height = 1 } end },
        { __index = savedV })
    BG3AI.Nav.closestReach = function(_, goal)
        return { pos = { goal[1] - 3, goal[2], goal[3] }, path_m = 6, to_goal_m = 3.0, dy_to_goal = 0 }
    end
    local ok, err = pcall(with_nav_path, KEY_SHORT, function(asked)
        local j = job(op("pickup", { char = P1, item = GEM }))
        eq(j.status, "running", "from the rim " .. tostring(j.error))
        eq(#asked, 0, "rim: not read")
    end)
    BG3AI.Visibility, BG3AI.Nav.closestReach = savedV, nil
    if not ok then error(err, 0) end
    -- unverified: no path answer, close and in sight
    reset()
    nav.fn = function() return { reachable = nil } end
    world[P1].pos = { 4, 0, 0 }
    with_nav_path(KEY_SHORT, function(asked)
        local j = job(op("pickup", { char = P1, item = GEM }))
        eq(j.status, "running", "unverified " .. tostring(j.error))
        eq(#asked, 0, "unverified: not read")
    end)
end)
