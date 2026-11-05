-- Part of test_act_lua.lua (split for the 1000-line cap): how a walk's end reads.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

test("a walk to walk_toward's own point that stops short says so: how far short, the pathfinder's reason, walked",
    function()
    -- launch-v1 haiku crash_site_grove r3 seq 67-73: "walk_toward: FAILED — could not get closer to the destination —
    -- dist=3.5 ... reason=path blocked" for a point the tool itself picked (N 10 m), after walking 7 m.
    -- REVIEW-13 #2: the text gives the pathfinder's reason, not "the walkable ground ends" (the pick is the engine
    -- path's end, so a short stop is often a creature, door or jump).
    local j = job(op("move_to", { char = P1, pos = { 8, 0, 0 }, picked = "toward E" }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    nav.fn = function() return { reachable = true, needs_jump = true } end
    arrive(j, { 4.7, 0, 0 })
    eq(j.status, "failed", "short")
    eq(j.error, "stopped 3.3 m short of the spot walk_toward picked (the farthest walkable point toward E): the"
        .. " rest of the way needs a jump; walked 4.7 m")
    reset()
    j = job(op("move_to", { char = P1, pos = { 8, 0, 0 }, picked = "toward E" }))
    nav.fn = function() return { reachable = false, error_cause = "Blocked" } end
    arrive(j, { 4.7, 0, 0 })
    eq(j.error, "stopped 3.3 m short of the spot walk_toward picked (the farthest walkable point toward E):"
        .. " destination is not reachable (Blocked); walked 4.7 m")
    -- a point the agent named keeps the plain text
    reset()
    j = job(op("move_to", { char = P1, pos = { 8, 0, 0 } }))
    arrive(j, { 4.7, 0, 0 })
    eq(j.error, "could not get closer to the destination", "the agent's own point")
end)
