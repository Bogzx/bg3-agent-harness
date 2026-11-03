-- Part of test_act_lua.lua (split for the 1000-line cap): forced hits (ops/logs/arena-dice.md "H4 cast path").
-- Osiris UseSpell / UseSpellAtPosition queue a task with IgnoreSpellRolls (0x4) set, so the attack roll was skipped
-- (live 2026-11-03: 30 of 30 hits, 2 of 2 at AC 37). Every order clears it on the queued task and reads it back; a
-- task missing or keeping the bit purges the queue and fails the cast. The stubs' UseSpell queues the task
-- (task_opts, task_sticky in test_act_lua.lua).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local function task_of(char, sp)
    local sc = world[char].comps.ServerCharacter
    for _, t in ipairs(sc and sc.OsirisController.Tasks or {}) do
        if t.Spell.OriginatorPrototype == sp then return t end
    end
end

test("cast rolls: UseSpell's task loses IgnoreSpellRolls, its other options stay (H4)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(task_of(P1, "Projectile_Bolt").SpellCastOptions, 3, "0x4 cleared, 0x1|0x2 kept")
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("cast rolls: a task without the bit is left as it is", function()
    task_opts = 0x10
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(task_of(P1, "Projectile_Bolt").SpellCastOptions, 0x10, "unchanged")
end)

test("cast rolls: UseSpellAtPosition's task too", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(task_of(P1, "Target_Thing").SpellCastOptions, 3, "0x4 cleared")
end)

test("cast rolls: no queued task -> purged and failed, saying the game would skip the roll", function()
    task_opts = nil
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = {} } }
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "failed", "status")
    assert(j.error:find("without its roll", 1, true) and j.error:find("no queued spell task", 1, true), j.error)
    eq(#called("PurgeOsirisQueue") >= 1, true, "the queue purged")
end)

test("cast rolls: a bit that won't clear -> purged and failed", function()
    task_sticky = true
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "failed", "status")
    assert(j.error:find("IgnoreSpellRolls is still set (7)", 1, true), j.error)
    eq(#called("PurgeOsirisQueue") >= 1, true, "the queue purged")
end)

test("cast rolls: after the walk into range, the order clears the bit too", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    local mv = called("CharacterMoveToPosition")
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    eq(#called("UseSpell"), 1, "cast after arriving")
    eq(task_of(P1, "Target_Help").SpellCastOptions, 3, "0x4 cleared")
    eq(j.status, "running", "status " .. tostring(j.error))
end)

test("cast rolls: after the walk, a task keeping the bit fails the cast", function()
    task_sticky = true
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    local mv = called("CharacterMoveToPosition")
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    eq(j.status, "failed", "status"); assert(j.error:find("without its roll", 1, true), j.error)
end)

test("cast rolls: sneak's Hide clears the bit; a task keeping it fails the sneak", function()
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    local j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(task_of(P1, "Shout_Hide").SpellCastOptions, 3, "0x4 cleared")
    reset()
    task_sticky = true
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "failed", "status"); assert(j.error:find("without its roll", 1, true), j.error)
end)
