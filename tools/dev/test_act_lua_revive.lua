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
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {} -- no action state held either
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
    eq(j.error, "the character took no step 10 s after the walk order (it never moved; ResurrectComponent kept)",
        "the facts")
    eq(j.result.walked, false, "walked"); eq(j.result.orders, 1, "one order")
end)

-- Live N9 (C1 replay, a0e598b): walk ordered at once and re-ordered with purges while Claude still had the revive's
-- RegisteredForAutomatedDialog: 0.0 m in 3 orders, the ResurrectComponent went by itself, the layer-1 ActionState
-- stayed, and the reply said "stood up". N8 B: one raw walk ~8 s after the revive, no automated dialog, walked 2.98 m.
test("end_standup: no order while the revive's automated dialog is up, one walk after it, never purged (N9)", function()
    local names = revived(true)
    world[P1].comps.ServerCharacter.Flags = { "IsPlayer", "RegisteredForAutomatedDialog" }
    local j = job(op("end_standup", { char = P1, toward = GOB }))
    tick(40) -- 2.4 s of the dialog
    eq(#called("CharacterMoveToPosition"), 0, "no order during the dialog")
    world[P1].comps.ServerCharacter.Flags = { "IsPlayer" }
    tick()
    eq(#called("CharacterMoveToPosition"), 1, "one walk once the dialog is over")
    near(j.dialog_s, 2.5, "waited for the dialog")
    names[2] = nil -- N9: the component goes by itself, no step: not stood up
    tick(30)
    eq(j.status, "running", "no step yet: not done"); eq(#called("PurgeOsirisQueue"), 0, "no purge while it runs")
    tick(140)
    eq(j.status, "failed", "status")
    eq(j.error, "the character took no step 10 s after the walk order (it never moved; ResurrectComponent gone;"
        .. " layer-1 action unfinished, TransactionId 55)", "the facts")
    eq(#called("CharacterMoveToPosition"), 1, "one order"); eq(j.result.walked, false, "walked")
end)

test("end_standup: a dialog that never ends gets the walk after STANDUP_DIALOG_MS; N9's held action is walked too",
    function()
        revived(true)
        world[P1].comps.ServerCharacter.Flags = { "RegisteredForAutomatedDialog" }
        op("end_standup", { char = P1, toward = GOB })
        tick(math.floor(A.STANDUP_DIALOG_MS / 60) - 2)
        eq(#called("CharacterMoveToPosition"), 0, "still waiting")
        tick(3)
        eq(#called("CharacterMoveToPosition"), 1, "ordered at the cap")
        reset()
        revived(false) -- N9 after the reply: no ResurrectComponent, layer 1 held (TransactionId 55)
        local j = job(op("end_standup", { char = P1, toward = GOB }))
        eq(j.status, "running", "pending: a held action is walked out of too")
        eq(#called("CharacterMoveToPosition"), 1, "walk")
        world[P1].pos = { 2.98, 0, 0 }
        world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
        tick()
        eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.walked, true, "walked")
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

-- Live N10 (plain C1 replay, 616e02f): a walk_toward of Claude arrived while end_standup ran; "superseded by a new
-- action (move_to)" left the stand-up's layer-1 action unfinished and no order of Claude's started again.
test("an order for a member whose stand-up job runs waits for it, then starts, and says it waited (N10)", function()
    local names = revived(true)
    world[P1].comps.ServerCharacter.Flags = { "RegisteredForAutomatedDialog" }
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    tick(5)
    local j2 = job(op("move_to", { char = P1, pos = { 6, 0, 0 } }))
    eq(js.status, "running", "the stand-up is not superseded"); eq(j2.status, "running", "the order waits")
    tick(10)
    eq(#called("CharacterMoveToPosition"), 0, "nothing ordered: the dialog is up, the move waits")
    world[P1].comps.ServerCharacter.Flags = {}
    tick()
    eq(#called("CharacterMoveToPosition"), 1, "the stand-up's own walk only")
    world[P1].pos = { 2.98, 0, 0 } -- the step ended the stand-up
    names[2] = nil
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
    tick()
    eq(js.status, "done", "stand-up " .. tostring(js.error))
    tick()
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 2, "the waiting move started"); near(mv[2][3], 6, "to its own point")
    eq(#called("PurgeOsirisQueue"), 0, "no purge while it waited")
    arrive(A.active[P1], { 6, 0, 0 }) -- the move run again after the stand-up (#38 review)
    tick()
    eq(j2.status, "done", "move " .. tostring(j2.error))
    assert(j2.result.waited_for_standup_s >= 0.6, "waited_for_standup_s " .. tostring(j2.result.waited_for_standup_s))
end)

test("a waiting order is dropped by a newer one or act.stop, unstarted and unpurged", function()
    revived(true)
    world[P1].comps.ServerCharacter.Flags = { "RegisteredForAutomatedDialog" }
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    local j1 = job(op("move_to", { char = P1, pos = { 6, 0, 0 } }))
    local j2 = job(op("move_to", { char = P1, pos = { 0, 0, 6 } }))
    eq(j1.status, "interrupted", "the older waiting order"); eq(j1.error, "superseded by a new action (move_to)", "why")
    eq(j2.status, "running", "the newer one waits"); eq(js.status, "running", "stand-up")
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
    op("stop", { char = P1 })
    eq(j2.status, "interrupted", "stopped"); eq(j2.error, "stopped", "why"); eq(js.status, "interrupted", "stand-up")
    tick(3)
    eq(#called("CharacterMoveToPosition"), 0, "neither ever started")
end)

-- #38 review: the waiting order runs again from scratch after the stand-up (its op: gates, plan, 'already there',
-- supersede rules), never on its request-time plan.
local function stood_up(names)
    world[P1].pos = { 2.98, 0, 0 } -- the step ended the stand-up
    names[2] = nil
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1] = {}
end

test("a fight that starts while an order waits for the stand-up gates it then: no off-turn walk (#38 review)",
    function()
    revived(true)
    world[P1].comps.ServerCharacter.Flags = { "RegisteredForAutomatedDialog" }
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    local j2 = job(op("move_to", { char = P1, pos = { 6, 0, 0 } }))
    osiv[key("IsInCombat", P1)] = 1 -- not this member's turn (no TurnBased component)
    tick(3)
    eq(js.status, "interrupted", "the stand-up stops for the fight")
    tick()
    eq(j2.status, "failed", "the waiting move " .. tostring(j2.error))
    eq(j2.error, "it is not this character's turn", "the turn gate, at the time it would have walked")
    eq(#called("CharacterMoveToPosition"), 0, "no walk")
    osiv[key("IsInCombat", P1)] = nil
end)

test("'already there' for a waiting order is decided after the stand-up's step (#38 review)", function()
    local names = revived(true)
    world[NPC].pos = { 4.5, 0, 0 } -- 4.5 m off at the request, 1.52 m after the 3 m step toward the reader
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    local j2 = job(op("move_to", { char = P1, target = NPC }))
    eq(j2.status, "running", "waits")
    stood_up(names)
    tick()
    eq(js.status, "done", "stand-up " .. tostring(js.error))
    tick()
    eq(j2.status, "done", "move " .. tostring(j2.error)); eq(j2.result.already_there, true, "already there")
    assert(j2.result.waited_for_standup_s, "waited_for_standup_s")
    eq(#called("CharacterMoveToPosition"), 1, "the stand-up's own walk only")
end)

test("an order given after the stand-up ended wins over one still waiting to run again (#38 review)", function()
    revived(true)
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    local j1 = job(op("move_to", { char = P1, pos = { 6, 0, 0 } }))
    RPC.ops["job.cancel"]({ id = js.id }) -- the stand-up ends; j1 has not ticked since
    local j2 = job(op("move_to", { char = P1, pos = { 0, 0, 6 } }))
    eq(j1.status, "interrupted", "the older order"); eq(j1.error, "superseded by a new action (move_to)", "why")
    eq(A.active[P1], j2, "the newer order is the member's action")
    tick(3)
    eq(j2.status, "running", "still walking " .. tostring(j2.error))
    local mv = called("CharacterMoveToPosition")
    near(mv[#mv][5], 6, "the last walk ordered is the newer one's")
    for _, c in ipairs(mv) do assert(c[3] ~= 6, "the older order never walked") end
end)

test("a hotbar cast (ui) for a member still standing up is refused at once, not parked (#38 re-review)", function()
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    revived(true)
    world[P1].comps.ServerCharacter.Flags = { "RegisteredForAutomatedDialog" }
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    local j = job(op("cast", { char = P1, spell = "Target_Bless", target = GOB, ui = true }))
    eq(j.status, "failed", "status")
    assert(j.error:find("is still standing up from the revive: not cast", 1, true), j.error)
    eq(js.status, "running", "the stand-up goes on")
end)

-- act9 (N11 step 1): the bridge's short_rest asks which stand-ups run; a failed stand-up prints its state trail
test("act.standups names a running stand-up; a failed one prints what it went through (N11)", function()
    revived(false) -- N11: ResurrectComponent gone, the layer-1 action unfinished
    world[P1].comps.ServerCharacter.AiActionMachine.Layers[1].State.TransactionId = 2
    eq(#RPC.ops["act.standups"]({}).running, 0, "none before")
    local js = job(op("end_standup", { char = P1, toward = GOB }))
    local up = RPC.ops["act.standups"]({}).running
    eq(#up, 1, "one"); eq(up[1].uuid, P1, "who")
    local printed = {}
    local print0 = Ext.Utils.Print
    Ext.Utils.Print = function(s) printed[#printed + 1] = s end
    tick(math.ceil(A.STANDUP_END_MS / 60) + 2)
    Ext.Utils.Print = print0
    eq(js.status, "failed", "status " .. tostring(js.error))
    local line
    for _, s in ipairs(printed) do if s:find("standup trail", 1, true) then line = s end end
    assert(line and line:find("layer1=2 moved=0.0", 1, true), tostring(line))
    eq(#RPC.ops["act.standups"]({}).running, 0, "none after")
end)

-- night2 (live N4, N7-N12 vs N10): every stuck plain C1 replay had Shadowheart's Cure Wounds on the revived Claude
-- ~5 s into his stand-up; N10's probe, with nothing aimed at him, stood up at 8.3 s.
local function healer()
    party_rows = { P1, P2 }
    world[P2] = { pos = { -1.5, 0, 0 }, comps = {} }
    world[P2].comps.SpellBook = { Spells = { { Id = { Prototype = "Target_Cure" } } } }
    stats["Target_Cure"] = { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("IsOnStage", P2)] = 1
    osiv[key("CanSee", P2, P1)] = 1
    osiv[key("HasLineOfSight", P2, P1)] = 1
end

test("a spell another member aims at one whose stand-up job runs waits for it, then is cast (night2)", function()
    local names = revived(true)
    healer()
    local js = job(op("end_standup", { char = P1, toward = P2 }))
    local j = job(op("cast", { char = P2, spell = "Target_Cure", target = P1 }))
    eq(j.status, "running", "waits " .. tostring(j.error))
    tick(3)
    eq(#called("UseSpell"), 0, "not cast during the stand-up")
    eq(js.status, "running", "the stand-up goes on")
    stood_up(names)
    tick()
    eq(js.status, "done", "stand-up " .. tostring(js.error))
    tick()
    local c = called("UseSpell")
    eq(#c, 1, "cast after it"); eq(c[1][2], P2, "by the healer"); eq(c[1][4], P1, "on the member")
    A.dispatch("CastedSpell", { P2, "Target_Cure" })
    tick(30)
    eq(j.status, "done", "cast " .. tostring(j.error))
    assert(j.result.waited_for_standup_s, "waited_for_standup_s")
end)

test("a hotbar cast (ui) aimed at a member still standing up is refused at once; on oneself it is not (night2)",
    function()
    revived(true)
    healer()
    local js = job(op("end_standup", { char = P1, toward = P2 }))
    local j = job(op("cast", { char = P2, spell = "Target_Cure", target = P1, ui = true }))
    eq(j.status, "failed", "status")
    eq(j.error, "the character is still standing up from the revive: not cast on them", "the fact")
    eq(js.status, "running", "the stand-up goes on")
    local self = job(op("cast", { char = P2, spell = "Target_Cure", target = P2 }))
    eq(self.status, "running", "a cast on the caster itself " .. tostring(self.error))
    eq(#called("UseSpell"), 1, "cast at once")
end)
