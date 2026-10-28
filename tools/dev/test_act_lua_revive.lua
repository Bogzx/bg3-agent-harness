-- Part of test_act_lua.lua (split for the 1000-line cap): a revived character's stand-up (live N2/N4 2026-10-28,
-- bench B3 job 13). test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- ---- revive stand-up -----------------------------------------------------------------------------------------

local RESURRECT = "esv::unsheath::ResurrectComponent" -- the name GetAllComponentNames gave in N2/N4

-- P1 as N4 left it: an unfinished action state on layer 1; `standing` = the revive's ResurrectComponent still there
local function revived(standing)
    local names = { "eoc::HealthComponent" }
    if standing then names[#names + 1] = RESURRECT end
    world[P1].comps.GetAllComponentNames = function() return names end
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = {} },
        AiActionMachine = { Layers = { { State = { Finished = false, TransactionId = 55 } }, {}, {} } } }
    return names
end

test("end_standup: a revived member still standing up walks 3 m toward the reader until the stand-up ends (N4)",
    function()
    local names = revived(true)
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    eq(j.status, "running", "started " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "one walk"); eq(mv[1][2], P1, "who"); near(mv[1][3], 3, "3 m toward the reader (x)")
    near(mv[1][5], 0, "z")
    tick(10)
    eq(j.status, "running", "still standing up")
    world[P1].pos = { 1.5, 0, 0 }
    names[2] = nil -- the walk ended the stand-up (N2: ResurrectComponent and the action state gone)
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.ended, true, "ended")
    eq(j.result.stepped_m, 1.5, "stepped")
end)

test("end_standup: the step is 3 m to a reachable point, never the reader's own spot 1.4 m off (live N7 C1)", function()
    revived(true)
    world[GOB].pos = { 1.4, 0, 0 } -- N7: Claude at (265,0.2,205.8), Shadowheart reading at (263.7,0.3,205.3)
    op("end_standup", { char = P1, toward = GOB })
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "one walk"); near(mv[1][3], 3, "x: past the reader, 3 m out"); near(mv[1][5], 0, "z")
    -- toward the reader is a chasm: the next direction, 45 degrees off
    reset()
    revived(true)
    world[GOB].pos = { 1.4, 0, 0 }
    nav.fn = function(_, pos)
        if math.abs(pos[3]) < 0.1 then return { reachable = false } end
        return { reachable = true, length_m = U.dist(world[P1].pos, pos) }
    end
    op("end_standup", { char = P1, toward = GOB })
    mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "one walk"); near(mv[1][3], 2.12, "x 45 degrees off"); near(math.abs(mv[1][5]), 2.12, "z")
    -- nowhere to step: refused with the fact, no walk
    reset()
    revived(true)
    nav.fn = function() return { reachable = false } end
    local ok, r = pcall(op, "end_standup", { char = P1, toward = GOB })
    local msg = (not ok) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
    eq(msg, "the character still stands up from the revive: no point 3 m away a walk reaches", "the fact")
    eq(#called("CharacterMoveToPosition"), 0, "no walk")
end)

test("end_standup: never steps into a harmful surface; all of them burning is refused with the fact (#29 review)",
    function()
    local saved = BG3AI.Nav.routeHazard
    local burning = function(q) return q[1] > 2.5 end -- a fire 2.5 m on toward the reader (+x)
    BG3AI.Nav.routeHazard = function(pts)
        local q = pts[#pts]
        if burning(q) then return { surface = "Fire", pos = q, at_m = 3 } end
    end
    local ok, err = pcall(function()
        revived(true)
        op("end_standup", { char = P1, toward = GOB })
        local mv = called("CharacterMoveToPosition")
        eq(#mv, 1, "one walk"); assert(mv[1][3] < 2.5, "not into the fire: x=" .. mv[1][3])
        near(mv[1][3], 2.12, "45 degrees off, clear of it")
        reset()
        revived(true)
        burning = function() return true end
        local okr, r = pcall(op, "end_standup", { char = P1, toward = GOB })
        local msg = (not okr) and (G.is_refusal(r) and r.reason or tostring(r)) or tostring(job(r).error)
        eq(msg, "the character still stands up from the revive: every point 3 m away a walk reaches is in or behind"
            .. " a harmful surface (Fire)", "the fact")
        eq(#called("CharacterMoveToPosition"), 0, "no walk")
    end)
    BG3AI.Nav.routeHazard = saved
    if not ok then error(err, 0) end
end)

test("end_standup: nothing pending, or in combat, orders nothing; a stand-up that outlasts the walk fails", function()
    revived(false)
    local r = op("end_standup", { char = P1, toward = GOB })
    eq(r.pending, false, "nothing pending"); eq(#called("CharacterMoveToPosition"), 0, "no walk")
    revived(true)
    osiv[key("IsInCombat", P1)] = 1
    r = op("end_standup", { char = P1, toward = GOB })
    osiv[key("IsInCombat", P1)] = nil
    eq(r.pending, true, "pending"); eq(r.in_combat, true, "in combat: its own turn's order ends it")
    eq(#called("CharacterMoveToPosition"), 0, "no walk in combat")
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    tick(170) -- 10.2 s, never moving
    eq(j.status, "failed", "status")
    eq(j.error, "the character still stands up from the revive 10 s after the first walk order (ResurrectComponent"
        .. " kept): 7 walk order(s), none of them started (it never moved)", "the facts")
    eq(j.result.walked, false, "walked")
end)

test("end_standup: a walk that never starts is ordered again until one walks (live N8 A: osi=1 for 4 s)", function()
    local names = revived(true)
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    tick(20) -- 1.2 s: still the first order
    eq(#called("CharacterMoveToPosition"), 1, "one order so far")
    tick(10) -- 1.8 s: never moved -> purged and ordered again
    eq(#called("CharacterMoveToPosition"), 2, "ordered again"); assert(#called("PurgeOsirisQueue") >= 1, "purged first")
    tick(25) -- 3.3 s: a third order; this one walks (N8 B: a raw walk ~7 s after the revive walked 2.98 m)
    eq(#called("CharacterMoveToPosition"), 3, "third order")
    world[P1].pos = { 1, 0, 0 }
    tick()
    tick(30)
    eq(#called("CharacterMoveToPosition"), 3, "walking: no more orders")
    world[P1].pos = { 2.98, 0, 0 }
    names[2] = nil
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.ended, true, "ended")
    eq(j.result.orders, 3, "orders"); eq(j.result.walked, true, "walked"); eq(j.result.stepped_m, 3, "stepped")
end)

test("end_standup: a member who dies again is not 'stood up' (app review of #34)", function()
    local names = revived(true)
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    tick(5)
    osiv[key("IsDead", P1)] = 1 -- the death takes the ResurrectComponent and the action state with it
    names[2] = nil
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
    tick()
    eq(j.status, "failed", "status"); eq(j.result.ended, nil, "not ended")
    eq(j.error, "the character died again during the stand-up (1 walk order(s) sent)", "the facts")
end)

test("end_standup: combat starting mid-job stops the orders and purges at once (app review of #34)", function()
    revived(true)
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    tick(5)
    local purges = #called("PurgeOsirisQueue")
    osiv[key("IsInCombat", P1)] = 1
    tick()
    eq(j.status, "interrupted", "status")
    eq(j.error, "combat started: no more stand-up walk orders or queue purges for the character (1 sent)", "facts")
    tick(40)
    eq(#called("CharacterMoveToPosition"), 1, "no more orders"); eq(#called("PurgeOsirisQueue"), purges, "no purge")
end)

test("a walk the game never starts names the unfinished action holding the character (N4), not a bare no path",
    function()
    revived(false)
    local j = job(op("move_to", { char = P1, pos = { 8, 0, 0 } }))
    tick(90) -- 5.4 s standing still
    eq(j.status, "failed", "status")
    eq(j.error, "no path to the destination: the game holds the character in an unfinished action (layer 1 of its"
        .. " action machine, TransactionId 55, no stand-up pending): none of its orders starts", "the fact")
    -- the revive's own stand-up (ResurrectComponent still there) is not that: a walk ends it
    reset()
    revived(true)
    j = job(op("move_to", { char = P1, pos = { 8, 0, 0 } }))
    tick(90)
    eq(j.error, "no path to the destination", "standing up: no held-action fact")
end)

test("a carried Use the game never starts names the unfinished action holding the character (N4)", function()
    revived(false)
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("CanSee", P1, GEM)] = nil
    world[GEM].pos = { 0, 0, 0 }
    world[P1].comps.ServerCharacter.OsirisController.Tasks = { {} } -- the Use, queued
    local j = job(op("interact", { char = P1, target = GEM }))
    tick(135) -- 8.1 s
    eq(j.status, "failed", "status")
    assert(j.error:find("nothing used): the game holds the character in an unfinished action", 1, true), j.error)
end)
