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
-- window 7: the check flags go too (CAST_DROP_CHECKS); IgnoreHasSpell stays
local KEPT = "IgnoreHasSpell,NoMovement,AvoidDangerousAuras"
local function opts() return table.concat(last_cast.CastOptions, ",") end

test("cast rolls: the cast state loses IgnoreSpellRolls and the check flags, its other flags stay (H4, w7)",
    function()
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

test("cast rolls: a flag that won't clear -> purged; no cast after it: failed as stopped (audit S2 b)", function()
    cast_sticky = true
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(#called("PurgeOsirisQueue"), 1, "the queue purged at once")
    eq(j.status, "running", "waits to see whether the purge stopped it")
    tick(math.ceil(A.ROLL_STOP_MS / 60) - 2)
    eq(j.status, "running", "still waiting")
    tick(3)
    eq(j.status, "failed", "status")
    assert(j.error:find("would have cast it without its roll (IgnoreSpellRolls stayed set: ", 1, true), j.error)
    assert(j.error:find("stopped, nothing cast", 1, true), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "no second purge")
end)

test("cast rolls: a flag that won't clear and the cast resolves anyway -> done, marked unrolled (F7)", function()
    cast_sticky = true
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    tick(10)
    A.dispatch("CastSpell", { P1, "Projectile_Bolt" })
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(math.ceil(A.PROJECTILE_SETTLE_MS / 60) + 5)
    eq(j.status, "done", "status " .. tostring(j.error))
    local why = tostring(j.result.unrolled)
    assert(why:find("without its roll (IgnoreSpellRolls stayed set", 1, true), why)
end)

test("cast rolls: CastOptions that don't read as names fail the cast", function()
    cast_opts = nil
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    local st = { Caster = { Uuid = { EntityUuid = P1 } }, CastOptions = 7 }
    create_hooks.SpellCastState({ SpellCastState = st })
    tick(math.ceil(A.ROLL_STOP_MS / 60) + 1)
    eq(j.status, "failed", "status"); assert(j.error:find("don't read as flag names", 1, true), j.error)
end)

test("cast rolls: CastedSpell with no cast state seen -> done, marked unrolled (F7)", function()
    cast_seen = "none"
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.roll.state, "pending", "pending")
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(math.ceil(A.PROJECTILE_SETTLE_MS / 60) + 5)
    eq(j.status, "done", "status " .. tostring(j.error))
    assert(tostring(j.result.unrolled):find("saw no cast state for it", 1, true), j.result.unrolled)
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
    eq(j.status, "running", "waits to see whether the purge stopped it")
    tick(math.ceil(A.ROLL_STOP_MS / 60) + 1)
    eq(j.status, "failed", "status"); assert(j.error:find("stopped, nothing cast", 1, true), j.error)
    -- the hide went through anyway: SNEAKING came after the purge
    reset()
    cast_sticky = true
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    j = job(op("sneak", { char = P1, on = true }))
    osiv[key("HasActiveStatus", P1, "SNEAKING")] = 1
    tick(1)
    eq(j.status, "done", "sneaking anyway")
    assert(tostring(j.result.unrolled):find("without its roll", 1, true), j.result.unrolled)
    osiv[key("HasActiveStatus", P1, "SNEAKING")] = nil
end)

-- Live window 5 (ops/logs/live.md, melee-swap A/B): the cast state was made at 70-270 ms, then Karlach's own
-- reaction prompt (Reckless Attack) paused the cast; the 3 s no-start check said "never started" and its purge
-- cancelled the cast. The state's creation is the start; a pause held CAST_PAUSED_MS is reported, nothing purged.
local PAUSED = "ls::anubis::PausedComponent"
local function swing()
    combat_setup(9)
    spell("Target_Swing", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    world[NPC].pos = { 1.5, 0, 0 }
    return job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
end

test("cast start: a cast whose state was made is not 'never started' after 3 s (window 5)", function()
    local j = swing()
    eq(j.status, "running", "started " .. tostring(j.error))
    tick(60) -- 3.6 s: no CastSpell, no step, no pause read
    eq(j.status, "running", "still running " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "not purged")
    A.dispatch("CastSpell", { P1, "Target_Swing", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Swing", "Target", "None", 0 })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error))
end)

test("cast start: a cast held paused for a reaction is reported as paused, its task kept (window 5)", function()
    local j = swing()
    tick(15) -- 0.9 s: the interrupt pauses it
    world[P1].comp_names = { "eoc::interrupt::ZoneParticipantComponent", PAUSED }
    tick(10) -- 0.6 s paused: an enemy's quick reaction looks the same, wait
    eq(j.status, "running", "short pause " .. tostring(j.error))
    tick(10) -- 1.2 s paused
    eq(j.status, "interrupted", "status " .. tostring(j.error))
    eq(j.error, A.CAST_PAUSED, "says paused")
    eq(j.result.began, true, "began"); eq(j.result.paused, true, "paused")
    eq(#called("PurgeOsirisQueue"), 0, "the held cast is not purged")
    world[P1].comp_names = nil
end)

test("cast start: a short pause that clears lets the cast finish as usual", function()
    local j = swing()
    world[P1].comp_names = { PAUSED }
    tick(12) -- 0.72 s
    world[P1].comp_names = nil
    tick(8)
    world[P1].comp_names = { PAUSED } -- a second short pause: the clock starts again
    tick(12)
    world[P1].comp_names = nil
    eq(j.status, "running", "not reported " .. tostring(j.error))
    A.dispatch("CastedSpell", { P1, "Target_Swing", "Target", "None", 0 })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error))
end)

-- REVIEW-5 F2: the order's stamp outlives its job; every Osiris cast state of the caster loses IgnoreSpellRolls; only
-- the awaited spell marks the job; a late or second cast is a late_cast event.
local pushed_events -- RPC.push calls since a test cleared it
local real_push = RPC.push
RPC.push = function(kind, data)
    if pushed_events then pushed_events[#pushed_events + 1] = { kind = kind, data = data } end
    return real_push(kind, data)
end
local function pushes(kind)
    local out = {}
    for _, p in ipairs(pushed_events or {}) do if p.kind == kind then out[#out + 1] = p.data end end
    return out
end
local function state_of(caster, sp, names)
    local st = { Caster = { Uuid = { EntityUuid = caster } }, SpellId = { Prototype = sp }, CastOptions = names }
    create_hooks.SpellCastState({ SpellCastState = st })
    return st
end

test("late cast: a state after the job gave up is cleared and told as late_cast (F2)", function()
    cast_seen = "none"
    combat_setup(9)
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    tick(60) -- 3.6 s: never started
    eq(j.status, "failed", "gave up " .. tostring(j.error))
    pushed_events = {}
    local st = state_of(P1, "Projectile_Bolt", { "IgnoreSpellRolls", "NoMovement" })
    eq(table.concat(st.CastOptions, ","), "NoMovement", "cleared")
    local late = pushes("late_cast")
    eq(#late, 1, "late_cast"); eq(late[1].spell, "Projectile_Bolt", "spell")
    assert(late[1].note:find("an earlier order went off", 1, true), late[1].note)
end)

test("late cast: two states for one order are both cleared; the second is a late_cast (F2)", function()
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.roll.state, "cleared", "first cleared")
    pushed_events = {}
    local st = state_of(P1, "Projectile_Bolt", { "IgnoreSpellRolls" })
    eq(#st.CastOptions, 0, "second cleared")
    local late = pushes("late_cast")
    eq(#late, 1, "late_cast"); assert(late[1].note:find("a second cast for one order", 1, true), late[1].note)
end)

test("late cast: another spell's state leaves the job pending; the player's own cast is left alone (F2)", function()
    cast_seen = "none"
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    pushed_events = {}
    local react = state_of(P1, "Target_OpportunityAttack", { "IsReaction", "IsInterrupt" })
    eq(j.roll.state, "pending", "still pending"); eq(#react.CastOptions, 2, "untouched")
    local other = state_of(P1, "Target_Other", { "IgnoreSpellRolls" })
    eq(#other.CastOptions, 0, "an Osiris cast of another spell still loses the flag")
    eq(j.roll.state, "pending", "pending")
    eq(#pushes("late_cast"), 0, "no late_cast for another spell")
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" }) -- end the job
    tick(40)
    pushed_events = {}
    local hotbar = state_of(P1, "Projectile_Bolt", { "FromClient", "NoUnsheath" })
    eq(#hotbar.CastOptions, 2, "the hotbar cast untouched"); eq(#pushes("late_cast"), 0, "no late_cast for it")
end)

test("late cast: the stamp expires after ROLL_STAMP_MS (F2)", function()
    bolt()
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    clock = clock + A.ROLL_STAMP_MS + 1
    local st = state_of(P1, "Projectile_Bolt", { "IgnoreSpellRolls" })
    eq(#st.CastOptions, 1, "not touched"); eq(A.roll_wait[G.guid(P1)], nil, "stamp gone")
end)

test("check flags (window 7): dropped by default; the probe switch picks them; a reaction keeps its flags", function()
    bolt()
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(opts(), KEPT, "default: IgnoreCastChecks and IgnoreTargetChecks dropped with IgnoreSpellRolls")
    local react = state_of(P1, "Target_OpportunityAttack", { "IgnoreCastChecks", "IsReaction" })
    eq(#react.CastOptions, 2, "a reaction keeps its flags")
    reset()
    A.CAST_DROP_EXTRA = false -- probe "off": the old flags
    bolt()
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(opts(), "IgnoreHasSpell,IgnoreCastChecks,NoMovement,IgnoreTargetChecks,AvoidDangerousAuras", "off")
    reset()
    A.CAST_DROP_EXTRA = true
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(opts(), "NoMovement,AvoidDangerousAuras", "all three"); eq(j.roll.state, "cleared", "ok")
    reset()
    A.CAST_DROP_EXTRA = { "IgnoreTargetChecks" }
    bolt()
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(opts(), "IgnoreHasSpell,IgnoreCastChecks,NoMovement,AvoidDangerousAuras", "only IgnoreTargetChecks dropped")
end)

test("check flags (audit S2-1): one that stays set is a done cast marked unchecked + a cast_unchecked row", function()
    local warned, rows = {}, {}
    local pw, ref = Ext.Utils.PrintWarning, BG3AI.Referee
    Ext.Utils.PrintWarning = function(m) warned[#warned + 1] = m end
    BG3AI.Referee = { record = function(ev, d) rows[#rows + 1] = { ev = ev, d = d } end }
    G.rules_unread = {}
    cast_sticky = { "IgnoreTargetChecks" }
    spell("Projectile_Odd", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0,
        TargetConditions = "MysteryTerm()" })
    local j = job(op("cast", { char = P1, spell = "Projectile_Odd", target = NPC }))
    eq(j.roll.state, "cleared", "the roll flag went: not failed"); eq(#called("PurgeOsirisQueue"), 0, "no purge")
    eq(opts(), "IgnoreHasSpell,NoMovement,AvoidDangerousAuras,IgnoreTargetChecks", "IgnoreTargetChecks stayed")
    A.dispatch("CastedSpell", { P1, "Projectile_Odd" })
    tick(math.ceil(A.PROJECTILE_SETTLE_MS / 60) + 5)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.unchecked, "the game cast it without checking its own rules (IgnoreTargetChecks stayed set)", "field")
    eq(#rows, 1, "one referee row"); eq(rows[1].ev, "cast_unchecked", "event")
    eq(rows[1].d.uuid, G.guid(P1), "uuid"); eq(rows[1].d.spell, "Projectile_Odd", "spell")
    eq(rows[1].d.flags, "IgnoreTargetChecks", "flags")
    -- the guard leaves TargetConditions to the engine (window 9 R3): with IgnoreTargetChecks kept, nothing checked
    -- them; the row carries the guard's reading of them at that target
    eq(rows[1].d.rules_unread, nil, "no caster terms unread")
    eq(rows[1].d.target_rules, "Projectile_Odd needs, at this target: MysteryTerm() (not read)", "target_rules")
    assert(warned[#warned]:find("IgnoreTargetChecks stayed set", 1, true), warned[#warned])
    -- a late cast whose flags won't clear: both rows
    rows = {}
    local late = setmetatable({ Caster = { Uuid = { EntityUuid = P1 } }, SpellId = { Prototype = "Projectile_Odd" } },
        { __index = function(_, k)
            if k == "CastOptions" then return { "IgnoreSpellRolls", "IgnoreTargetChecks" } end
        end, __newindex = function() end })
    create_hooks.SpellCastState({ SpellCastState = late })
    Ext.Utils.PrintWarning, BG3AI.Referee = pw, ref
    eq(#rows, 2, "rows"); eq(rows[1].ev, "cast_unchecked", "late unchecked"); eq(rows[2].ev, "cast_unrolled", "late")
end)

test("engine refusal (window 7): CastSpellFailed says the game's own reason when the cast entity has one", function()
    cast_outcome = "CantCast"
    bolt()
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastSpellFailed", { P1, "Projectile_Bolt" })
    eq(j.status, "failed", "status")
    assert(j.error:find("the game refused the cast: it can't be cast now (its requirements aren't met) (the game's"
        .. " CantCast)", 1, true), j.error)
    reset()
    cast_outcome = "None" -- nothing read: the old words
    bolt()
    j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastSpellFailed", { P1, "Projectile_Bolt" })
    assert(j.error:find("the game refused the cast (no reason given)", 1, true), j.error)
end)

test("self-check: a hook that doesn't register is said loudly at load (REVIEW-5)", function()
    local said = {}
    local onc, pe, pw, hook = Ext.Entity.OnCreate, Ext.Utils.PrintError, Ext.Utils.PrintWarning, A.roll_hook
    Ext.Entity.OnCreate = function() error("no OnCreate on this build") end
    Ext.Utils.PrintError = function(m) said[#said + 1] = m end
    A.roll_hook = nil
    dofile(LUA .. "/Server/ActionsRolls.lua")
    Ext.Entity.OnCreate, Ext.Utils.PrintError, Ext.Utils.PrintWarning = onc, pe, pw
    eq(A.roll_hook, false, "not registered")
    eq(#said, 1, "said once"); assert(said[1]:find("SELF-CHECK FAILED", 1, true), said[1])
    A.roll_hook = hook
end)

test("engine refusal (audit S3-2): at a ground point, target reasons are said neutrally", function()
    spell("Target_Spot", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    for _, r in ipairs({ "MissingTarget", "TooFewTargets", "InvalidTarget" }) do
        reset()
        spell("Target_Spot", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
        cast_outcome = r
        local j = job(op("cast", { char = P1, spell = "Target_Spot", pos = { 5, 0, 0 } }))
        A.dispatch("CastSpellFailed", { P1, "Target_Spot" })
        eq(j.status, "failed", "status")
        assert(j.error:find("the game refused the cast: it can't be cast at that spot", 1, true), j.error)
        assert(not j.error:find(r, 1, true), "no engine code: " .. j.error)
    end
    -- a creature target keeps the game's reason (the guard checked it is in view first)
    reset()
    bolt()
    cast_outcome = "InvalidTarget"
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastSpellFailed", { P1, "Projectile_Bolt" })
    assert(j.error:find("the target isn't a valid target for it (the game's InvalidTarget)", 1, true), j.error)
    -- other reasons at a point stay
    reset()
    spell("Target_Spot", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    cast_outcome = "CantCast"
    j = job(op("cast", { char = P1, spell = "Target_Spot", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpellFailed", { P1, "Target_Spot" })
    assert(j.error:find("(the game's CantCast)", 1, true), j.error)
end)
