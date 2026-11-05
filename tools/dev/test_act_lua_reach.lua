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

-- App review of night2/vis: the goal past the target is ground the walker reaches when it does not stop in time.
local function past_goal_skipped(setup, what)
    reset()
    world[P1].pos, world[NPC].pos = { CLAUDE[1], 0, CLAUDE[3] }, WYLL
    local args = setup() or {}
    local j = job(op("move_to", { char = P1, target = NPC, range = 2.5, avoid = args.avoid }))
    eq(j.status, "running", what .. ": " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    near(U.dist({ mv[1][3], 0, mv[1][5] }, WYLL), 1.5, what .. ": the stop point 1.5 m from Wyll is the goal")
    assert(U.dist({ mv[1][3], 0, mv[1][5] }, CLAUDE) < U.dist(WYLL, CLAUDE), what .. ": on Claude's side")
end

test("move_to range: no goal past Wyll on a harmful surface, a discovered trap or a warned area (app review)",
    function()
    local N = BG3AI.Nav
    local function beyond(p) return U.dist(p, CLAUDE) > U.dist(WYLL, CLAUDE) + 0.5 end
    past_goal_skipped(function()
        N.surfaceNear = function(p) return beyond(p) and "Fire" or nil end
    end, "fire past him")
    N.surfaceNear = nil
    past_goal_skipped(function()
        N.routeHazard = function(pts) return beyond(pts[#pts]) and { surface = "Acid", pos = pts[#pts] } or nil end
    end, "acid on the way past him")
    N.routeHazard = nil
    local saved = BG3AI.Events
    past_goal_skipped(function()
        world[GEM].pos = { WYLL[1] + 0.8, 0, WYLL[3] - 2.0 } -- a spotted trap just past Wyll
        BG3AI.Events = { knownTraps = { [GEM] = true } }
    end, "a trap past him")
    BG3AI.Events = saved
    past_goal_skipped(function() -- a warned area 4 m past Wyll, r 3 m: every goal past him is in it, he and the stop
        -- point are not
        return { avoid = { { id = 1, pos = { -37.02, 0, -57.22 }, r = 3.0, name = "the vault" } } }
    end, "a warned area past him")
end)

test("move_to range: a target ~1.6 m above the walker's floor: stops at the closest approach in reach (app review)",
    function()
    world[P1].pos, world[NPC].pos = { CLAUDE[1], 0, CLAUDE[3] }, { WYLL[1], 1.6, WYLL[3] }
    local j = job(op("move_to", { char = P1, target = NPC, range = 2.5 }))
    engine_walk(j, 0)
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.in_reach, true, "in reach")
    local d = U.dist(world[P1].pos, world[NPC].pos)
    assert(d <= 2.5, "in spell_reach's 2.5 m (centres): " .. d)
    -- the closest approach is under him: stopped a tick (0.5 m) after it, on the ground checked past him
    assert(U.dist(world[P1].pos, CLAUDE) <= U.dist(WYLL, CLAUDE) + 0.6, "not walked on past him")
end)

test("move_to range in combat: the budget is the route to the stop point, not to the goal past it (app review)",
    function()
    world[P1].pos, world[NPC].pos = { CLAUDE[1], 0, CLAUDE[3] }, WYLL
    combat_setup(9) -- the stop point is 7.9 m away, the goal past Wyll 11.9 m
    local j = job(op("move_to", { char = P1, target = NPC, range = 2.5 }))
    eq(j.status, "running", "status " .. tostring(j.error))
end)

test("move_to range: a plate detour's first leg stops in reach too, no second leg (app review)", function()
    local PLATE = "a3a3a3a3-0000-0000-0000-00000000000c"
    local savedV, savedR, savedF = BG3AI.Visibility, BG3AI.Nav.route, BG3AI.Nav.routeFrom
    world[NPC].pos = { 10, 0, 0 }
    world[PLATE] = { pos = { 9, 0, 0 }, comps = {} }
    for _, k in ipairs({ "IsItem", "IsOnStage" }) do osiv[key(k, PLATE)] = 1 end
    BG3AI.Visibility = setmetatable({
        PLATE_NAMES = { ["Marble Plate"] = true }, fixtureSeen = { [PLATE] = { t = clock, level = "L" } },
        FIXTURE_REMEMBER_MS = 300000, levelNow = function() return "L" end,
        name = function(u) return u == PLATE and "Marble Plate" or nil end,
        bounds = function() return { radius = 0.6, height = 0.1 } end,
    }, { __index = savedV })
    BG3AI.Nav.route = function(_, to) return { { world[P1].pos[1], 0, world[P1].pos[3] }, to } end
    BG3AI.Nav.routeFrom = function(_, from, to) return { from, to } end
    local ok, err = pcall(function()
        local j = job(op("move_to", { char = P1, target = NPC, range = 2.5 }))
        eq(j.status, "running", "status " .. tostring(j.error))
        eq(#called("CharacterMoveToPosition"), 1, "the way round the plate first")
        engine_walk(j, 0)
        eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.in_reach, true, "in reach")
        eq(#called("CharacterMoveToPosition"), 1, "stopped on the first leg, in reach")
    end)
    BG3AI.Visibility, BG3AI.Nav.route, BG3AI.Nav.routeFrom = savedV, savedR, savedF
    if not ok then error(err, 0) end
end)

test("melee reach, window 9 live: the engine cast at 1.8 and 2.0 m, refused 2.05 m: Move radii (1.998 m)", function()
    -- Karlach's greataxe (WeaponRange 150) on an Apostate Destroyer, both Move 0.249, Hit 0.200, Stand 0.499 (M1).
    -- The Hit radii's 1.9 m refused the 2.0 m swing the engine allowed.
    combat_setup(30)
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = "AXE"
    osiv[key("GetStatString", "AXE")] = "WPN_Greataxe"
    stats.WPN_Greataxe = { WeaponRange = 150 }
    spell("Target_MainHandAttack", { TargetRadius = "MeleeMainWeaponRange", Range = 0, SpellType = "Target",
        UseCosts = "ActionPoint:1", Level = 0 })
    local bound = { Bound = { AIBounds = { Move = { Radius = 0.249 }, Hit = { Radius = 0.2 },
        Stand = { Radius = 0.499 } } } }
    world[P1].comps.Bound, world[GOB].comps.Bound = bound, bound
    osiv[key("IsCharacter", GOB)] = 1
    near(G.melee_reach(P1, GOB, 1.5), 1.998, "reach")
    for _, d in ipairs({ 1.8, 1.99 }) do
        world[GOB].pos = { d, 0, 0 }
        local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
        eq(j.status, "running", d .. " m swing " .. tostring(j.error))
        A.dispatch("CastSpellFailed", { P1, "Target_MainHandAttack" }) -- ends the job; only the guard's verdict counts
    end
    world[GOB].pos = { 2.05, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "2.05 m"); assert(j.error:find("out of range (2.0 m, range 1.5 m)", 1, true), j.error)
end)
