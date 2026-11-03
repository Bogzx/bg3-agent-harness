-- Part of test_act_lua.lua (split for the 1000-line cap): forced hits (ops/logs/arena-dice.md "H4 cast path").
-- A cast an Osiris UseSpell / UseSpellAtPosition starts carries IgnoreSpellRolls (0x4), so its attack roll or save
-- was skipped (live 2026-11-03: 8/8 hits at AC 35, Sacred Flame never saved; the hotbar route missed 9/9). The order
-- marks the caster; the bit is dropped from the cast's CastStartRequest (pre-update of ServerCastRequest), else from
-- its SpellCastState, and read back. A write that doesn't take, or a CastedSpell with neither seen, fails the cast.
-- The stubs' UseSpell plays the game's side (cast_opts, cast_sticky, cast_seen, last_cast in test_act_lua.lua).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local function bolt()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
end

test("cast rolls: the cast request loses IgnoreSpellRolls before the cast is made; other options stay (H4)", function()
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(last_cast.request.CastOptions.__Value, 3, "request: 0x4 cleared, 0x1|0x2 kept")
    eq(last_cast.state.CastOptions.__Value, 3, "the cast made from it")
    eq(j.roll.state, "cleared", "cleared"); eq(j.roll.where, "request", "at the request")
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(math.ceil(A.PROJECTILE_SETTLE_MS / 60) + 5) -- a projectile's hits may land after CastedSpell
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
end)

test("cast rolls: a request without the bit is left as it is", function()
    cast_opts = 0x10
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(last_cast.request.CastOptions.__Value, 0x10, "unchanged"); eq(j.roll.state, "cleared", "nothing to clear")
end)

test("cast rolls: UseSpellAtPosition's cast too", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(last_cast.request.CastOptions.__Value, 3, "0x4 cleared")
end)

test("cast rolls: a request the hook missed -> cleared on the cast state", function()
    cast_seen = "state"
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(last_cast.request.CastOptions.__Value, 7, "request not touched")
    eq(last_cast.state.CastOptions.__Value, 3, "state cleared"); eq(j.roll.where, "cast state", "where")
    eq(j.status, "running", "status " .. tostring(j.error))
end)

test("cast rolls: a bit that won't clear -> purged and failed, saying it went without its roll", function()
    cast_sticky = true
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "failed", "status")
    assert(j.error:find("without its roll", 1, true), j.error)
    assert(j.error:find("IgnoreSpellRolls stayed set on its request: 7", 1, true), j.error)
    eq(#called("PurgeOsirisQueue") >= 1, true, "the queue purged")
end)

test("cast rolls: CastedSpell with neither the request nor the state seen fails the cast", function()
    cast_seen = "none"
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.roll.state, "pending", "pending")
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    eq(j.status, "failed", "status")
    assert(j.error:find("saw neither its cast request nor its cast state", 1, true), j.error)
end)

test("cast rolls: another caster's request is not touched", function()
    bolt()
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    local other = setmetatable({ Caster = { Uuid = { EntityUuid = GOB } } }, {})
    other.CastOptions = 7
    A.roll_wait[G.guid(P1)] = nil
    cast_requests = { other }
    sys_hooks.ServerCastRequest()
    cast_requests = {}
    eq(other.CastOptions, 7, "untouched")
end)

test("cast rolls: after the walk into range, the order's cast is cleared too", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    eq(last_cast, nil, "no cast before arriving")
    local mv = called("CharacterMoveToPosition")
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    eq(last_cast.request.CastOptions.__Value, 3, "0x4 cleared"); eq(j.roll.state, "cleared", "cleared")
end)

test("cast rolls: sneak's Hide is cleared; a bit that won't clear fails the sneak", function()
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    local j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(last_cast.request.CastOptions.__Value, 3, "0x4 cleared")
    reset()
    cast_sticky = true
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "failed", "status"); assert(j.error:find("without its roll", 1, true), j.error)
end)
