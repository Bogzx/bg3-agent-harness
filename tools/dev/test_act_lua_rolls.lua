-- Part of test_act_lua.lua (split for the 1000-line cap): forced hits (ops/logs/arena-dice.md "H4 cast path").
-- A cast an Osiris UseSpell / UseSpellAtPosition starts carries IgnoreSpellRolls in its SpellCastState.CastOptions,
-- so its attack roll or save was skipped (live 2026-11-03: 8/8 hits at AC 35, Sacred Flame never saved). The order
-- marks the caster; at the cast state's OnCreate the flag is dropped from the name table and read back (live rerun:
-- 6/6 misses at AC 35, 2/6 saves made). A write that doesn't take, or a CastedSpell with no state seen, fails it.
-- The stubs' UseSpell plays the game's side (cast_opts, cast_sticky, cast_seen, last_cast in test_act_lua.lua).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local function bolt()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
end
local KEPT = "IgnoreHasSpell,IgnoreCastChecks,NoMovement,IgnoreTargetChecks,AvoidDangerousAuras"
local function opts() return table.concat(last_cast.CastOptions, ",") end

test("cast rolls: the cast state loses IgnoreSpellRolls, its other flags stay (H4)", function()
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(opts(), KEPT, "flags"); eq(j.roll.state, "cleared", "cleared")
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(math.ceil(A.PROJECTILE_SETTLE_MS / 60) + 5) -- a projectile's hits may land after CastedSpell
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("cast rolls: a cast without the flag is left as it is", function()
    cast_opts = { "NoMovement" }
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(opts(), "NoMovement", "unchanged"); eq(j.roll.state, "cleared", "nothing to clear")
end)

test("cast rolls: UseSpellAtPosition's cast too", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(opts(), KEPT, "flags")
end)

test("cast rolls: a flag that won't clear -> purged and failed, saying it went without its roll", function()
    cast_sticky = true
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "failed", "status")
    assert(j.error:find("without its roll (IgnoreSpellRolls stayed set: ", 1, true), j.error)
    eq(#called("PurgeOsirisQueue") >= 1, true, "the queue purged")
end)

test("cast rolls: CastOptions that don't read as names fail the cast", function()
    cast_opts = nil
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    local st = { Caster = { Uuid = { EntityUuid = P1 } }, CastOptions = 7 }
    create_hooks.SpellCastState({ SpellCastState = st })
    eq(j.status, "failed", "status"); assert(j.error:find("don't read as flag names", 1, true), j.error)
end)

test("cast rolls: CastedSpell with no cast state seen fails the cast", function()
    cast_seen = "none"
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.roll.state, "pending", "pending")
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    eq(j.status, "failed", "status"); assert(j.error:find("saw no cast state for it", 1, true), j.error)
end)

test("cast rolls: another caster's cast state is not touched", function()
    bolt()
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    cast_opts = USESPELL_OPTS
    local other = { Caster = { Uuid = { EntityUuid = GOB } }, CastOptions = { "IgnoreSpellRolls" } }
    A.roll_wait[G.guid(GOB)] = nil
    create_hooks.SpellCastState({ SpellCastState = other })
    eq(other.CastOptions[1], "IgnoreSpellRolls", "untouched")
end)

test("cast rolls: after the walk into range, the order's cast is cleared too", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    eq(last_cast, nil, "no cast before arriving")
    local mv = called("CharacterMoveToPosition")
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    eq(opts(), KEPT, "flags"); eq(j.roll.state, "cleared", "cleared")
end)

test("cast rolls: sneak's Hide is cleared; a flag that won't clear fails the sneak", function()
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    local j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(opts(), KEPT, "flags")
    reset()
    cast_sticky = true
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "failed", "status"); assert(j.error:find("without its roll", 1, true), j.error)
end)
