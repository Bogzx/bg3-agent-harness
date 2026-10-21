-- Part of test_nav_lua.lua (split for the 1000-line cap): travel_to leg ends on known ground.
-- E2E run 1 (ovh-e2e e2e-naut-pi, 2026-10-22): 9 of 50 travel_to legs were refused by Guard's point_ok "that spot is
-- not in view and the party hasn't walked there". Each refused leg with a known start ended 8/17/20/23/26/29 m from it:
-- N.crossing's 2 m + 3 m steps (reachToward samples every 2 m), so the planner handed out unseen spots the guard
-- refuses. The guard's check is N.known(nil, pos) on the exact point; these tests ask that of every leg end.

local function knownWithin(cx, cz, r)
    return function(p) return (p[1] - cx) ^ 2 + (p[3] - cz) ^ 2 <= r * r end
end

local function guardOk(pos, what)
    assert(pos, what .. ": a leg end")
    assert(N.known(nil, pos), string.format("%s: Guard's point_ok refuses (%.1f,%.1f,%.1f): not in view, not walked",
        what, pos[1], pos[2], pos[3]))
end

test("legs (run 1 #61): the sampled way blocked -> the crossing fallback ends on known ground", function()
    -- leg 2 of 'travel_to objective Escape Route': from (5.3,17.5,-423.5), refused at (-17.7,17.9,-424.6), 23.0 m W
    world[P1] = { 5.3, 0, -423.5 }
    local goal = { -24.7, 0, -424.6 }
    seen = knownWithin(5.3, -423.5, 12)
    blocked = function(x, z) return x < 4 and math.abs(z - (-423.5 + (x - 5.3) * 1.1 / 30)) < 1.0 end
    local r = RPC.ops["nav.reach_toward"]({ pos = goal, max_m = 30, direct = true, avoid = "least" })
    eq(r.reachable, true, "reachable")
    guardOk(r.pos, "crossing leg")
    assert(U.dist(r.pos, goal) < U.dist(world[P1], goal) - 1.0, "gets closer")
end)

test("legs (run 1 #995): 'around Fire (heading W)' ends on known ground", function()
    -- leg 2 of 'travel_to (-47.4,13.2,-426)': from (-23.1,16.5,-432.4), refused at (-42.4,13.0,-427.3), 20.0 m W
    world[P1] = { -23.1, 0, -432.4 }
    local goal = { -47.4, 0, -426.0 }
    seen = knownWithin(-23.1, -432.4, 8)
    fire = function(x, z) return x < -25 and x > -30 and math.abs(z - (-431.5)) < 2.5 end
    local r = RPC.ops["nav.reach_toward"]({ pos = goal, max_m = 30, direct = true, avoid = "least" })
    eq(r.reachable, true, "reachable")
    eq(r.straight and r.straight.hazard and r.straight.hazard.surface, "Fire", "the straight way's Fire (the note)")
    guardOk(r.pos, "detour leg")
end)

test("legs: no known spot gets on -> the crossing says not reachable (no unseen leg)", function()
    seen = knownWithin(0, 0, 1.5) -- every candidate (2 m and further) unseen; the ground itself all walkable
    local c = N.crossing(P1, { -30, 0, 0 }, 20)
    eq(c.reachable, false, "reachable")
end)

test("legs (run 1 #1060): altLeg 'another way (along the route)' only to a known route point", function()
    -- 'travel_to (-31.4,18.7,-354.8)': the alternative leg ended on (-31.6,18.16,-355.74) and was refused
    seen = function(p) return p[1] > -10 end
    nodesFor = function(_, t)
        local pts = {}
        for x = -1, math.floor(t[1]), -1 do pts[#pts + 1] = { x, 0, 0 } end
        pts[#pts + 1] = { t[1], t[2], t[3] }
        return pts
    end
    local r = RPC.ops["nav.reach_toward"]({ pos = { -30, 0, 0 }, max_m = 30, direct = true, avoid = "least",
        alt = true, exclude = { -20, 0, 0 } })
    eq(r.reachable, true, "reachable")
    guardOk(r.pos, "alternative leg")
end)

test("legs: directLeg checks the 0.1 m point it hands out (a route node at 9.96 is 10.0, another 1 m key)", function()
    seen = function(p) return p[1] < 10 or p[1] > 50 end -- 9.96 in view, 10.0 not; the goal 60 m E in view
    nodesFor = function() return { { 9.96, 0, 0 }, { 30, 0, 0 }, { 60, 0, 0 } } end
    local saved = N.verifyPoint
    N.verifyPoint = function() return true end
    local d = N.directLeg(P1, { 60, 0, 0 }, false)
    N.verifyPoint = saved
    if d then guardOk(d.pos, "direct leg") end
end)
