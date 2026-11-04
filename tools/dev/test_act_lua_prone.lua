-- Part of test_act_lua.lua (split for the 1000-line cap): knocked down in its own combat turn (w10 L6 ST-P5(d): a
-- harness walk stood a prone character up at no cost beyond the metres walked). The game's PRONE (Shared
-- Status_KNOCKED_DOWN.txt :1-20) keeps it down until its next turn start; the guard refuses walks and casts until then.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local PRONE_RC = "(HasActionResource('Movement', 50, 0, true) and not HasPassive('Athlete_StandUp'))"

-- fn() with P1 in combat, its turn `mine`, carrying PRONE_GREASE (`using "PRONE"`: the stats as the game resolves them)
local function withProne(mine, fn)
    combat_setup(9)
    world[P1].comps.TurnBased.IsActiveCombatTurn = mine
    G._disabling = {}
    spell("Projectile_PG", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    spell("PRONE_GREASE", { StatusType = "KNOCKED_DOWN",
        StatusGroups = { "SG_Prone", "SG_Condition", "SG_Unconscious" }, RemoveEvents = { "OnTurn" },
        RemoveConditions = PRONE_RC })
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function() return { { id = "PRONE_GREASE", name = "Prone" } } end },
        { __index = savedV })
    local ok, err = pcall(fn)
    BG3AI.Visibility = savedV
    G._disabling = {}
    if not ok then error(err, 0) end
end

test("prone in its own turn: a cast is refused before any order, with the game's rule as a fact", function()
    withProne(true, function()
        local n0 = #called("UseSpell")
        local j = job(op("cast", { char = P1, spell = "Projectile_PG", target = NPC }))
        eq(j.status, "failed", "refused " .. tostring(j.error))
        eq(j.error, "cannot act while Prone (it stands up at its turn start for half its movement)")
        eq(#called("UseSpell"), n0, "no order")
    end)
end)

test("prone in its own turn: move_to is refused before any walk", function()
    withProne(true, function()
        local n0 = #called("CharacterMoveToPosition")
        local j = job(op("move_to", { char = P1, target = NPC }))
        eq(j.status, "failed", "refused " .. tostring(j.error))
        eq(j.error, "cannot move while Prone (the status keeps it in place this turn)")
        eq(#called("CharacterMoveToPosition"), n0, "no walk ordered")
    end)
end)

test("prone outside its own turn: nothing changes (the game stands it up at its turn start)", function()
    withProne(false, function()
        eq(G.prone_now(P1), nil, "not its turn")
        eq(G.move_blocked(P1), nil, "movement shown as it is")
    end)
end)

test("the effect comes from the status data: OnTurn + half movement, OnTurn only, no remove event", function()
    G._disabling = {}
    spell("PRONE_T", { StatusType = "KNOCKED_DOWN", RemoveEvents = { "OnTurn" }, RemoveConditions = PRONE_RC })
    spell("LAUGH_T", { StatusType = "KNOCKED_DOWN", RemoveEvents = { "OnDamage", "OnTurn" },
        RemoveConditions = "SavingThrow(Ability.Wisdom, SourceSpellDC())" })
    spell("KD_T", { StatusType = "KNOCKED_DOWN", RemoveEvents = {}, RemoveConditions = "" })
    eq(G.disabling_info("PRONE_T").effect, "stands up at its turn start for half its movement", "PRONE")
    eq(G.disabling_info("LAUGH_T").effect, "may stand up at its turn start", "HIDEOUS_LAUGHTER")
    eq(G.disabling_info("KD_T").effect, "stays down until the status ends", "KNOCKED_DOWN")
    G._disabling = {}
end)
