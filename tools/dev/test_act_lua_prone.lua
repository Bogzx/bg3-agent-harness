-- Part of test_act_lua.lua (split for the 1000-line cap): prone (REVIEW-5 F4, REVIEW-12 (a)). BG3 lets a prone
-- creature do nothing until it stands up at its turn start; the guard refused nothing while prone.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local function prone_setup()
    combat_setup(9)
    G._disabling = {}
    spell("Projectile_PR", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    stats["PRONE_GREASE"] = { StatusType = "KNOCKED_DOWN", StatusGroups = { "SG_Prone" }, RemoveEvents = { "OnTurn" } }
    stats["BLEEDING"] = { StatusType = "BOOST", StatusGroups = { "SG_Condition" } }
end

test("prone in combat: a cast is refused before any order, with the game's rule and fix", function()
    prone_setup()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function()
        return { { id = "BLEEDING", name = "Bleeding" }, { id = "PRONE_GREASE", name = "Prone" } }
    end }, { __index = savedV })
    local n0 = #called("UseSpell")
    local j = job(op("cast", { char = P1, spell = "Projectile_PR", target = NPC }))
    BG3AI.Visibility = savedV
    G._disabling = {}
    eq(j.status, "failed", "refused " .. tostring(j.error))
    eq(j.error, "cannot act while Prone (it stands up at its turn start for half its movement, if it has that "
        .. "much); an adjacent ally's Help (an action) ends it")
    eq(#called("UseSpell"), n0, "no order")
end)

test("prone in combat: a walk is refused before any order; G.prone lists the status ids", function()
    prone_setup()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function() return { { id = "PRONE_GREASE", name = "Prone" } } end },
        { __index = savedV })
    local p = G.prone(P1)
    local n0 = #called("CharacterMoveToPosition")
    local j = job(op("move_to", { char = P1, target = NPC }))
    BG3AI.Visibility = savedV
    G._disabling = {}
    eq(p and #p, 1, "one prone status"); eq(p[1].id, "PRONE_GREASE", "its id")
    eq(j.status, "failed", "refused " .. tostring(j.error))
    eq(j.error, "cannot move while Prone (the status keeps it in place this turn)")
    eq(#called("CharacterMoveToPosition"), n0, "no walk ordered")
end)

test("G.movement reads Movement's amount and max", function()
    combat_setup(9)
    world[P1].comps.ActionResources.Resources.Movement[1].MaxAmount = 12
    local a, m = G.movement(P1)
    eq(a, 9, "amount"); eq(m, 12, "max")
    world[P1].comps.ActionResources = nil
    eq(G.movement(P1), nil, "unread")
end)
