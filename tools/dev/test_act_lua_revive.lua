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

test("end_standup: a revived member still standing up walks toward the reader until the stand-up ends (N4)", function()
    local names = revived(true)
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    eq(j.status, "running", "started " .. tostring(j.error))
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "one walk"); eq(mv[1][2], P1, "who"); eq(mv[1][3], 2.4, "toward the reader (x)")
    tick(10)
    eq(j.status, "running", "still standing up")
    world[P1].pos = { 1.5, 0, 0 }
    names[2] = nil -- the walk ended the stand-up (N2: ResurrectComponent and the action state gone)
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.ended, true, "ended")
    eq(j.result.stepped_m, 1.5, "stepped")
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
    tick(70) -- 4.2 s
    eq(j.status, "failed", "status"); assert(j.error:find("still stands up from the revive 4 s after a walk", 1, true),
        j.error)
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
