-- Part of test_nav_lua.lua (split for the 1000-line cap): door links (NavigationDoors.lua, B4 S037 Rescue Volo).
-- test_nav_lua.lua runs it after its stubs and helpers, which it shares as globals.
-- S015 02:43:33: the Sanctum's Heavy Oak Doors at ~(-108,10.8,460) put the party at (297.2,40.3,-78.2); S037: Volo at
-- ~(334,40.8,-6) read 609.6 m SE of the Goblin Camp.

local OAK = "dddddddd-0000-0000-0000-000000000352"
local GATE = "dddddddd-0000-0000-0000-000000000353"
local OUTSIDE, INSIDE = { -108.0, 10.8, 460.0 }, { 297.2, 40.3, -78.2 }
local CAMP, VOLO = { -100.0, 10.0, 440.0 }, { 334.0, 40.8, -6.0 }
local LVL = "WLD_Main_A"
local V = BG3AI.Visibility
V.name = V.name or function(u) return u == OAK and "Heavy Oak Doors" or "Gate" end

local function onLevel(name, fn)
    local h, r = Osi.GetHostCharacter, Osi.GetRegion
    Osi.GetHostCharacter = function() return P1 end
    Osi.GetRegion = function() return name end
    PersistentVars = PersistentVars or {}
    PersistentVars.nav = { levels = {} }
    N.uses = {}
    local ok, err = pcall(fn)
    Osi.GetHostCharacter, Osi.GetRegion = h, r
    world[OAK], world[GATE] = nil, nil
    if not ok then error(err, 0) end
end
local level = LVL
local function links() return (PersistentVars.nav.levels[LVL] or {}).links or {} end
-- P1 uses the item next to it, then stands at `to` `after_ms` later (the tick looks then)
local function use(item, at, to, after_ms, success)
    world[item] = { at[1], at[2], at[3] }
    world[P1] = { at[1] + 1, at[2], at[3] }
    N.noteUse(P1, item, success == nil and 1 or success)
    clock = clock + (after_ms or 400)
    world[P1] = { to[1], to[2], to[3] }
    N.linkWatch(clock)
end

test("door links: the Sanctum doors' use that moved the party 650 m is kept, and leads the route to Volo", function()
    onLevel(LVL, function()
        use(OAK, OUTSIDE, INSIDE)
        eq(#links(), 1, "one link")
        local k = links()[1]
        eq(k.door, OAK, "door"); eq(k.name, "Heavy Oak Doors", "name")
        eq(k.from[3], 460, "from the door"); eq(k.to[1], 297.2, "to the arrival")
        eq(next(N.uses), nil, "the use is done")
        local d = N.doorLink(LVL, CAMP, VOLO)
        assert(d, "a link toward Volo")
        eq(d.uuid, OAK, "the door"); eq(d.dir, "N", "the door is N of the camp spot")
        near(d.door_m, U.dist(CAMP, OUTSIDE), "door_m", 0.1)
        near(d.to_goal_m, 81.0, "arrival to Volo", 0.5)
        -- the same door again replaces its link
        use(OAK, OUTSIDE, INSIDE)
        eq(#links(), 1, "still one link")
        -- nav.route from the camp: too far to preview, no walk, the door named
        world[P1] = { CAMP[1], CAMP[2], CAMP[3] }
        local savedIA = V.itemsAround
        V.itemsAround = function() return {} end
        local ok, r = pcall(N.routeAhead, V.context(), P1, VOLO)
        V.itemsAround = savedIA
        assert(ok, tostring(r))
        eq(r.too_far, true, "too far")
        eq(r.door_link and r.door_link.uuid, OAK, "route names the door")
    end)
end)

test("door links: the teleport already done at UseFinished counts on the first tick", function()
    onLevel(LVL, function()
        world[OAK] = { OUTSIDE[1], OUTSIDE[2], OUTSIDE[3] }
        world[P1] = { INSIDE[1], INSIDE[2], INSIDE[3] }
        N.noteUse(P1, OAK, 1)
        N.linkWatch(clock)
        eq(#links(), 1, "linked")
    end)
end)

test("door links: a plain door, a ladder, a late move, a failed use, a carried item, another level: no link", function()
    onLevel(LVL, function()
        use(GATE, OUTSIDE, { OUTSIDE[1] + 6, OUTSIDE[2], OUTSIDE[3] }) -- walked through: 6 m
        use(GATE, OUTSIDE, { OUTSIDE[1], OUTSIDE[2] + N.LINK_MIN_M - 2, OUTSIDE[3] }) -- a tall ladder: < LINK_MIN_M
        eq(#links(), 0, "short moves")
        clock = clock + N.LINK_WAIT_MS + 1
        N.linkWatch(clock)
        eq(next(N.uses), nil, "an old use expires")
        use(OAK, OUTSIDE, INSIDE, N.LINK_WAIT_MS + 1) -- fast travel well after a use
        use(OAK, OUTSIDE, INSIDE, 400, 0)
        eq(#links(), 0, "late / failed")
        Osi.GetInventoryOwner = function() return P1 end
        local ok, err = pcall(use, OAK, OUTSIDE, INSIDE)
        Osi.GetInventoryOwner = nil
        assert(ok, tostring(err))
        eq(#links(), 0, "a carried item (a scroll) is no door")
        N.noteUse("eeeeeeee-0000-0000-0000-000000000009", OAK, 1)
        eq(next(N.uses), nil, "not a party member")
        world[OAK] = { OUTSIDE[1], OUTSIDE[2], OUTSIDE[3] }
        world[P1] = { OUTSIDE[1] + 1, OUTSIDE[2], OUTSIDE[3] }
        N.noteUse(P1, OAK, 1)
        Osi.GetRegion = function() return "CTY_Main_A" end
        world[P1] = { INSIDE[1], INSIDE[2], INSIDE[3] }
        N.linkWatch(clock)
        eq(#links(), 0, "another level: a load, not a door link")
        eq(next(N.uses), nil, "dropped")
    end)
end)

test("door links: no help -> nil (already inside, a door leading elsewhere, the door farther than the goal)", function()
    onLevel(LVL, function()
        use(OAK, OUTSIDE, INSIDE)
        eq(N.doorLink(LVL, { 300.0, 40.0, -70.0 }, VOLO), nil, "inside already: the walk is short")
        eq(N.doorLink(LVL, CAMP, { -400.0, 10.0, 900.0 }), nil, "a goal the arrival is not near")
        eq(N.doorLink(LVL, { 900.0, 40.0, -6.0 }, VOLO), nil, "the door is farther away than the goal")
        eq(N.doorLink("CTY_Main_A", CAMP, VOLO), nil, "another level's links")
        eq(N.doorLink(level, CAMP, VOLO).uuid, OAK, "this level")
    end)
end)
