-- An order queued behind a held attack, and what is its own. Loaded by test_act_lua.lua.
local PAUSED = "ls::anubis::PausedComponent"

-- s5-rx-3 312.12 and 467.14: Action 0/1 and the Extra Attack status read before the held attack's CastedSpell, about
-- 0.5 s ahead. An order in that gap passes the gate, and took the held attack's landing as its own.
local function extra_in_the_gap(seen)
    combat_setup(9)
    world[NPC].pos = { 1.5, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1",
        Level = 0, SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = NPC }))
    tick(15)
    world[P1].comp_names = { PAUSED }
    tick(20)
    eq(j.status, "interrupted", "held " .. tostring(j.error))
    world[P1].comp_names = nil
    tick(2)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 0
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    cast_seen = seen
    local k = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = NPC }))
    eq(k.status, "running", "free, and the charge lifted the gate " .. tostring(k.error))
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil
    return k
end
local function attack_event(event) A.dispatch(event, { P1, "Target_MainHandAttack", "Target", "None", 0 }) end
local function hit(amount) A.dispatch("AttackedBy", { NPC, P1, P1, "Slashing", amount, "Attack", 0 }) end

test("held cast: its landing isn't the order queued behind it: that one reads its own swing, rolled", function()
    local k = extra_in_the_gap("none")
    hit(7) -- the held attack's damage
    attack_event("CastedSpell")
    eq(k.last_hit, nil, "the wait for its own arrow isn't cut short by the held hit's time")
    tick(10)
    eq(k.status, "running", "still waiting for its own swing")
    create_hooks.SpellCastState({ SpellCastState = last_cast })
    attack_event("CastSpell")
    hit(9)
    attack_event("CastedSpell")
    tick(10)
    eq(k.status, "done", "own landing " .. tostring(k.error))
    eq(k.result.unrolled, nil, "it rolled")
    eq(k.result.no_damage, nil, "it hit")
    eq(#k.result.damage, 1, "only its own hit")
    eq(k.result.damage[1].amount, 9, "its own amount")
    eq(k.result.extra_attack, true, "the free attack")
end)

test("held cast: a queued order that never shows a state still reports its own landing, unrolled", function()
    local k = extra_in_the_gap("none")
    attack_event("CastedSpell") -- the held attack's
    tick(10)
    attack_event("CastedSpell") -- its own, no state seen: truly no roll
    tick(10)
    eq(k.status, "done", "reported " .. tostring(k.error))
    assert(tostring(k.result.unrolled):find("no cast state", 1, true), tostring(k.result.unrolled))
end)

test("held cast: a straggler after a landing with no CastSpell is its own hit", function()
    local k = extra_in_the_gap("none")
    attack_event("CastedSpell") -- the held attack's
    tick(10)
    attack_event("CastedSpell") -- its own, no CastSpell seen
    hit(5)
    tick(10)
    eq(k.status, "done", "reported " .. tostring(k.error))
    eq(k.result.no_damage, nil, "it hit")
    eq(k.result.damage[1].amount, 5, "the straggler counts")
end)

test("held cast: a held cast that never lands can't take the next order's landing from it", function()
    local k = extra_in_the_gap("state") -- the held swing was lost: the next order's state comes at once
    attack_event("CastSpell")
    hit(9)
    attack_event("CastedSpell")
    tick(10)
    eq(k.status, "done", "its own state, so its own landing " .. tostring(k.error))
    eq(k.result.unrolled, nil, "it rolled")
    eq(k.result.damage[1].amount, 9, "its hit")
end)

-- Live, held-claim read gapranged3 (live.md): held arrow CastedSpell +150, hit +351; own CastSpell +1081, CastedSpell
-- +1418, hit +1584; the result read damage=[6; 6], total 12, its own being 6. AttackedBy carries no cast id. What tells
-- them apart is that the order hasn't started at the first hit.
test("held cast: a held arrow's late hit isn't the queued order's", function()
    local k = extra_in_the_gap("none")
    attack_event("CastedSpell")
    hit(6)
    eq(#k.hits, 0, "not this order's")
    eq(k.last_hit, nil, "its own arrow's wait isn't cut short")
    create_hooks.SpellCastState({ SpellCastState = last_cast })
    attack_event("CastSpell")
    attack_event("CastedSpell")
    hit(4)
    tick(30)
    eq(k.status, "done", "own landing " .. tostring(k.error))
    eq(#k.result.damage, 1, "only its own arrow")
    eq(k.result.damage[1].amount, 4, "its own amount")
end)

test("held cast: a held arrow's hit after the queued order's state, before its CastSpell, isn't its", function()
    local k = extra_in_the_gap("none")
    attack_event("CastedSpell")
    create_hooks.SpellCastState({ SpellCastState = last_cast }) -- about +151 by the reads, before the hit at +351
    hit(6)
    eq(#k.hits, 0, "not this order's")
    eq(k.last_hit, nil, "its own arrow's wait isn't cut short")
    attack_event("CastSpell")
    attack_event("CastedSpell")
    hit(4)
    tick(30)
    eq(k.status, "done", "own landing " .. tostring(k.error))
    eq(#k.result.damage, 1, "only its own arrow")
    eq(k.result.damage[1].amount, 4, "its own amount")
end)

local function plain_hit(entries)
    combat_setup(9)
    world[NPC].pos = { 1.5, 0, 0 }
    spell("Target_Plain", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful" } })
    A.held_audits = entries
    local j = job(op("cast", { char = P1, spell = "Target_Plain", target = NPC }))
    eq(j.status, "running", "started " .. tostring(j.error))
    A.dispatch("AttackedBy", { NPC, P1, P1, "Slashing", 5, "", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Plain", "Target", "None", 0 })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(j.result.total_damage, 5, "its own")
end

test("held cast: an order behind no held cast keeps a hit that comes before its CastSpell", function()
    plain_hit({})
end)

test("held cast: another character's held cast doesn't make an order unstarted", function()
    plain_hit({ { char = P2, matches = function() return false end, free_ms = RPC.now_ms() } })
end)

test("held cast: a status before the queued order's CastSpell isn't its own, its state or not; after, it is", function()
    local saved = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statusInfo = function(id) return { name = id } end }, { __index = saved })
    local k = extra_in_the_gap("none")
    A.dispatch("StatusApplied", { NPC, "THE_HELD_ATTACKS", P1, 0 })
    eq(#k.statuses, 0, "the held attack's")
    attack_event("CastedSpell")
    create_hooks.SpellCastState({ SpellCastState = last_cast })
    A.dispatch("StatusApplied", { NPC, "ALSO_THE_HELD_ATTACKS", P1, 0 })
    eq(#k.statuses, 0, "its state alone: still the held attack's")
    attack_event("CastSpell")
    A.dispatch("StatusApplied", { NPC, "ITS_OWN", P1, 0 })
    BG3AI.Visibility = saved
    eq(#k.statuses, 1, "its own")
    eq(k.statuses[1].status, "ITS_OWN", "which")
end)

test("held cast: with no state seen, a hit after its own CastSpell is still its own", function()
    local k = extra_in_the_gap("none")
    attack_event("CastedSpell") -- the held attack's
    attack_event("CastSpell") -- its own start
    hit(5)
    attack_event("CastedSpell")
    tick(10)
    eq(k.status, "done", "reported " .. tostring(k.error))
    eq(k.result.damage[1].amount, 5, "its own hit")
    assert(tostring(k.result.unrolled):find("no cast state", 1, true), tostring(k.result.unrolled))
end)

-- a30-read c1b-1,2,3 and c2-1b (live.md): Gale's Haste countered by an enemy. By hotbar Action and L3 both went by
-- +5993. By Osiris Action went by +5761 and L3 stayed 3 through +15 s: the game doesn't charge an Osiris cast's slot.
local function countered_slot(outcome)
    combat_setup(9)
    spell("Target_SlotBolt", { TargetRadius = "18", SpellType = "Target", Level = 3,
        UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:3" })
    local res = world[P1].comps.ActionResources.Resources
    res.SpellSlot = { { Level = 3, Amount = 3 } }
    res.ActionPoint[1].Amount = 2 -- a double charge of the action would show
    world[NPC].pos = { 1.5, 0, 0 }
    cast_outcome = outcome
    local j = job(op("cast", { char = P1, spell = "Target_SlotBolt", target = NPC }))
    eq(j.status, "running", "started " .. tostring(j.error))
    return res, j
end
local function fail_cast(j)
    A.dispatch("CastSpellFailed", { P1, "Target_SlotBolt", "Target", "None", 0 })
    cast_outcome = nil
    eq(j.status, "failed", "ended " .. tostring(j.error))
end

test("countered: an Osiris cast pays its slot once, after the game's own charge of the action", function()
    local res, j = countered_slot("Countered")
    fail_cast(j)
    tick(30)
    eq(res.SpellSlot[1].Amount, 3, "nothing charged before the game's charge")
    res.ActionPoint[1].Amount = 1 -- the game's charge
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "the slot is paid")
    eq(res.ActionPoint[1].Amount, 1, "the action only by the game")
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "once")
    eq(#A.held_audits, 0, "no entry left")
end)

test("countered: no double charge if the game takes the slot first", function()
    local res, j = countered_slot("Countered")
    fail_cast(j)
    res.SpellSlot[1].Amount = 2 -- the game's, with the action still to come
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "the slot once")
    eq(res.ActionPoint[1].Amount, 2, "the action is the game's to take, even later than the settle")
    res.ActionPoint[1].Amount = 1
    tick(30)
    eq(res.ActionPoint[1].Amount, 1, "the action once")
    eq(res.SpellSlot[1].Amount, 2, "the slot still once")
end)

test("countered: no double charge if the game takes the slot and the action together", function()
    local res, j = countered_slot("Countered")
    fail_cast(j)
    res.SpellSlot[1].Amount, res.ActionPoint[1].Amount = 2, 1 -- the hotbar's shape: same read
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "together: the slot once")
    eq(res.ActionPoint[1].Amount, 1, "together: the action once")
end)

test("countered: a slot the game takes inside the settle isn't charged again", function()
    local res, j = countered_slot("Countered")
    fail_cast(j)
    res.ActionPoint[1].Amount = 1
    tick(2) -- under CAST_SETTLE_MS
    eq(res.SpellSlot[1].Amount, 3, "still settling")
    res.SpellSlot[1].Amount = 2 -- the game's
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "once")
end)

test("countered: with no cost read by the bound nothing is charged, and a later charge finds no entry", function()
    local res, j = countered_slot("Countered")
    fail_cast(j)
    tick(math.ceil(A.HELD_RESOLVE_MS / 60) + 5)
    eq(res.SpellSlot[1].Amount, 3, "no cost read: not paid")
    eq(#A.held_audits, 0, "the entry has gone")
    res.ActionPoint[1].Amount = 1
    tick(30)
    eq(res.SpellSlot[1].Amount, 3, "nothing waiting to charge")
end)

test("countered: a Canceled cast stays uncharged: only the outcome Countered pays its slot here", function()
    local res, j = countered_slot("Canceled")
    res.ActionPoint[1].Amount = 1 -- it went: the game took the action, as a Riposte's Canceled read
    fail_cast(j)
    tick(30)
    eq(res.SpellSlot[1].Amount, 3, "unread on the player's route, so not charged")
    eq(#A.held_audits, 0, "no entry")
end)

test("countered: an order inside the settle is refused, so the countered cast and its own each pay a slot", function()
    local res, j = countered_slot("Countered")
    fail_cast(j)
    res.ActionPoint[1].Amount = 1 -- the game's charge; a second action is left
    tick(1) -- the cost read, the audit not run yet
    eq(res.SpellSlot[1].Amount, 3, "still settling")
    local k = job(op("cast", { char = P1, spell = "Target_SlotBolt", target = NPC }))
    eq(k.status, "failed", "refused " .. tostring(k.error))
    assert(k.error:find("still resolving", 1, true), k.error)
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "the countered cast's")
    eq(#A.held_audits, 0, "can't wedge: the entry has gone")
    local l = job(op("cast", { char = P1, spell = "Target_SlotBolt", target = NPC }))
    eq(l.status, "running", "free now " .. tostring(l.error))
    A.dispatch("CastedSpell", { P1, "Target_SlotBolt", "Target", "None", 0 })
    tick(10)
    eq(l.status, "done", "landed " .. tostring(l.error))
    eq(res.SpellSlot[1].Amount, 1, "its own too")
end)

test("countered: a bonus-action spell leaves the bonus action to the game when the slot reads first", function()
    combat_setup(9)
    spell("Target_BonusBolt", { TargetRadius = "18", SpellType = "Target", Level = 3,
        UseCosts = "BonusActionPoint:1;SpellSlotsGroup:1:1:3" })
    local res = world[P1].comps.ActionResources.Resources
    res.SpellSlot, res.BonusActionPoint = { { Level = 3, Amount = 3 } }, { { Level = 0, Amount = 2 } }
    world[NPC].pos = { 1.5, 0, 0 }
    cast_outcome = "Countered"
    local j = job(op("cast", { char = P1, spell = "Target_BonusBolt", target = NPC }))
    eq(j.status, "running", "started " .. tostring(j.error))
    A.dispatch("CastSpellFailed", { P1, "Target_BonusBolt", "Target", "None", 0 })
    cast_outcome = nil
    res.SpellSlot[1].Amount = 2 -- the game's
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "the slot once")
    eq(res.BonusActionPoint[1].Amount, 2, "the bonus action is the game's")
end)

-- countered-read part 1: job `interrupted` at the prompt, the answer, then the failed: the outcome reads only at it.
local function prompt_countered(outcome)
    local res = countered_slot(nil)
    tick(15)
    world[P1].comp_names = { PAUSED }
    tick(20)
    world[P1].comp_names = nil
    tick(2)
    return res, function()
        local e = A.held_audits[1].entity
        e.SpellCastOutcome = { Result = outcome }
        A.dispatch("CastSpellFailed", { P1, "Target_SlotBolt", "Target", "None", 0 })
        e.SpellCastOutcome = nil
    end
end

test("countered on a prompt: the slot once, after the game's charge, read only at the failed", function()
    local res, failed = prompt_countered("Countered")
    eq(#A.held_audits, 1, "held")
    failed()
    tick(30)
    eq(res.SpellSlot[1].Amount, 3, "nothing before the game's charge")
    res.ActionPoint[1].Amount = 1
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "the slot is paid")
    eq(res.ActionPoint[1].Amount, 1, "the action only by the game")
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "once")
    eq(#A.held_audits, 0, "no entry left")
end)

test("countered on a prompt: no double charge if the game takes the slot first", function()
    local res, failed = prompt_countered("Countered")
    failed()
    res.SpellSlot[1].Amount = 2
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "the slot once")
    eq(res.ActionPoint[1].Amount, 2, "the action is the game's")
end)

test("countered on a prompt: an order inside the settle is refused", function()
    local res, failed = prompt_countered("Countered")
    failed()
    res.ActionPoint[1].Amount = 1
    tick(1)
    local k = job(op("cast", { char = P1, spell = "Target_SlotBolt", target = NPC }))
    eq(k.status, "failed", "refused " .. tostring(k.error))
    assert(k.error:find("still resolving", 1, true), k.error)
end)

test("held cast on a prompt: a Canceled failed, or the cost gone with no failed, charges nothing", function()
    local res, failed = prompt_countered("Canceled")
    failed()
    res.ActionPoint[1].Amount = 1
    tick(math.ceil(A.HELD_RESOLVE_MS / 60) + 5)
    eq(res.SpellSlot[1].Amount, 3, "Canceled stays uncharged")
    res = prompt_countered("Countered") -- no failed comes
    res.ActionPoint[1].Amount = 1
    tick(math.ceil(A.HELD_RESOLVE_MS / 60) + 5)
    eq(res.SpellSlot[1].Amount, 3, "not read as countered")
end)

test("held cast on a prompt: a declined cast that lands still pays through its landing, with no failed", function()
    local res = prompt_countered("Countered")
    res.ActionPoint[1].Amount = 1
    A.dispatch("CastedSpell", { P1, "Target_SlotBolt", "Target", "None", 0 })
    tick(30)
    eq(res.SpellSlot[1].Amount, 2, "paid once")
    eq(res.ActionPoint[1].Amount, 1, "action once")
end)

test("held cast on a prompt: another spell's failed by the caster isn't this cast's", function()
    local res = prompt_countered("Countered")
    local e = A.held_audits[1].entity
    e.SpellCastOutcome = { Result = "Countered" }
    A.dispatch("CastSpellFailed", { P1, "Target_MainHandAttack", "Target", "None", 0 })
    e.SpellCastOutcome = nil
    res.ActionPoint[1].Amount = 1
    tick(math.ceil(A.HELD_RESOLVE_MS / 60) + 5)
    eq(res.SpellSlot[1].Amount, 3, "not this cast's")
end)

-- live.md 05:50 and S5 s5-l12-m1: a swing rolled and missed, a Riposte, Canceled, the action gone. Players see a miss.
local function dice(total, dc, nat, kind)
    return { { type = kind or "MeleeWeaponAttack", natural = nat or 5, total = total, dc = dc } }
end
local function canceled_swing(rolls)
    local saved = BG3AI.RollLog -- the read itself is RollLog's own suite's
    local entity = { SpellCastOutcome = { Result = "Canceled" } }
    BG3AI.RollLog = { castDice = function(e)
        eq(e, entity, "this cast's dice")
        return rolls or {}
    end }
    local res, j = countered_slot("Canceled")
    res.ActionPoint[1].Amount = 1 -- the game's
    j.cast_entity = entity
    A.dispatch("CastSpellFailed", { P1, "Target_SlotBolt", "Target", "None", 0 })
    cast_outcome = nil
    BG3AI.RollLog = saved
    return j, res
end

test("Riposted miss: reads as a miss, done and no_damage, and charges nothing", function()
    local j, res = canceled_swing(dice(16, 18))
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(j.result.no_damage, true, "no_damage")
    eq(j.result.cast, "Target_SlotBolt", "its cast")
    eq(j.result.target.uuid, NPC, "its target")
    tick(30)
    eq(res.SpellSlot[1].Amount, 3, "Canceled stays uncharged")
    eq(#A.held_audits, 0, "no entry")
end)

test("Riposted miss: a die at its dc, a natural 20 below it, or no die read, isn't called a miss", function()
    for _, rolls in ipairs({ dice(18, 18), dice(16, 18, 20), false }) do
        local j = canceled_swing(rolls or nil)
        eq(j.status, "failed", "not a miss")
        eq(j.error, string.format(A.CAST_CANCELED_SPENT, "action"), "the cost's words")
        assert(not j.error:find("events", 1, true), "promises nothing: " .. j.error)
        reset()
    end
end)

test("Riposted miss: a save die below its dc isn't a miss, alone or beside an attack die below its own", function()
    local save = dice(10, 18, nil, "SavingThrow")[1]
    for _, rolls in ipairs({ { save }, { dice(16, 18)[1], save } }) do
        local j = canceled_swing(rolls)
        eq(j.status, "failed", "not a miss")
        eq(j.error, string.format(A.CAST_CANCELED_SPENT, "action"), "the cost's words")
        reset()
    end
end)

test("Riposted miss: a free attack's says so, done and no_damage", function()
    combat_setup(9)
    world[NPC].pos = { 1.5, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1",
        Level = 0, SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 0
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    local k = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = NPC }))
    eq(k.status, "running", "started " .. tostring(k.error))
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil -- the game's, as the swing went
    local saved, entity = BG3AI.RollLog, { SpellCastOutcome = { Result = "Canceled" } }
    BG3AI.RollLog = { castDice = function() return dice(16, 18) end }
    k.cast_entity, cast_outcome = entity, "Canceled"
    attack_event("CastSpellFailed")
    cast_outcome, BG3AI.RollLog = nil, saved
    eq(k.status, "done", "done " .. tostring(k.error))
    eq(k.result.no_damage, true, "no_damage")
    eq(k.result.extra_attack, true, "the free attack")
end)

test("extra attack: a bonus-cost weapon attack isn't the free attack: its result doesn't say so", function()
    combat_setup(9)
    world[NPC].pos = { 1.5, 0, 0 }
    local flags = { "IsMelee", "IsHarmful" }
    local roll = { Default = "Attack(AttackType.MeleeWeaponAttack)" }
    spell("Target_PommelStrike", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "BonusActionPoint:1",
        Level = 0, SpellFlags = flags, SpellRoll = roll })
    spell("Target_Both", { UseCosts = "ActionPoint:1;BonusActionPoint:1", SpellFlags = flags, SpellRoll = roll })
    local res = world[P1].comps.ActionResources.Resources
    res.ActionPoint[1].Amount, res.BonusActionPoint = 0, { { Level = 0, Amount = 1 } }
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    local j = job(op("cast", { char = P1, spell = "Target_PommelStrike", target = NPC }))
    eq(j.status, "running", "started " .. tostring(j.error))
    A.dispatch("CastedSpell", { P1, "Target_PommelStrike" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(j.result.extra_attack, nil, "not the free attack")
    eq(G.extra_attack_spell("Target_Both", Ext.Stats.Get("Target_Both")), true, "an Action among its costs is")
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil
end)

-- The game's statuses with ExtraAttackCheck's free boost (Status_BOOST.txt). The L12 Fighter has EXTRA_ATTACK_2 first.
local function free_attack_setup()
    combat_setup(9)
    world[NPC].pos = { 1.5, 0, 0 }
    local melee = { Default = "Attack(AttackType.MeleeWeaponAttack)" }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1",
        Level = 0, SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" }, SpellRoll = melee })
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 0
end
local function swing() return job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = NPC })) end

test("extra attack: every free status of the game's frees the swing, and the result says so", function()
    for _, status in ipairs({ "EXTRA_ATTACK_2", "EXTRA_ATTACK", "MAG_MARTIAL_EXERTION", "WILDSTRIKE_EXTRA_ATTACK",
        "STALKERS_FLURRY", "EXTRA_ATTACK_THIRSTING_BLADE", "WILDSTRIKE_2_EXTRA_ATTACK" }) do
        reset()
        free_attack_setup()
        osiv[key("HasActiveStatus", P1, status)] = 1
        local j = swing()
        eq(j.status, "running", status .. " " .. tostring(j.error))
        A.dispatch("CastedSpell", { P1, "Target_MainHandAttack" })
        tick(10)
        eq(j.result.extra_attack, true, status)
        eq(j.result.charged_by_guard, nil, status .. ": no action charged")
        osiv[key("HasActiveStatus", P1, status)] = nil
    end
end)

test("extra attack: a status that makes the attack cost a bonus action isn't free", function()
    for _, status in ipairs({ "EXTRA_ATTACK_WAR_MAGIC", "EXTRA_ATTACK_WAR_PRIEST" }) do
        reset()
        free_attack_setup()
        osiv[key("HasActiveStatus", P1, status)] = 1
        local j = swing()
        eq(j.status, "failed", status)
        assert(j.error:find("not enough action", 1, true), j.error)
        osiv[key("HasActiveStatus", P1, status)] = nil
    end
end)

test("extra attack: EXTRA_ATTACK_2's Canceled swing spends that status, though EXTRA_ATTACK follows it", function()
    cast_outcome = "Canceled"
    free_attack_setup()
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK_2")] = 1
    local j = swing()
    eq(j.status, "running", "ordered " .. tostring(j.error))
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK_2")] = nil
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1 -- its OnRemoveFunctors, the third attack
    attack_event("CastSpellFailed")
    cast_outcome = nil
    eq(j.error, string.format(A.CAST_CANCELED_SPENT, "extra attack"), "the status it spent")
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil
end)

test("extra attack: what the game's check takes is free: an unarmed attack, a throw, its named spells", function()
    free_attack_setup()
    local function spell_of(name, roll, costs)
        spell(name, { UseCosts = costs or "ActionPoint:1", SpellFlags = {}, SpellRoll = roll and { Default = roll } })
        return G.extra_attack_spell(name, Ext.Stats.Get(name))
    end
    eq(spell_of("Target_UnarmedAttack", "Attack(AttackType.MeleeUnarmedAttack)"), true, "unarmed")
    eq(spell_of("Throw_Throw", "Attack(AttackType.RangedUnarmedAttack)"), true, "a throw")
    eq(spell_of("Projectile_ArrowOfSmokepowder", nil), true, "named")
    eq(spell_of("Projectile_FireBolt", "Attack(AttackType.RangedSpellAttack)"), false, "a spell attack")
    eq(spell_of("Target_Offhand", "Attack(AttackType.MeleeWeaponAttack)", "BonusActionPoint:1"), false, "no Action")
    -- live.md D290: Hamstring Shot, past the guard: no Action, EXTRA_ATTACK and its cooldown taken
    spell("Projectile_HamstringShot", { UseCosts = "ActionPoint:1", SpellFlags = { "IsDefaultWeaponAction" },
        SpellRoll = { Default = "Attack(AttackType.RangedWeaponAttack)" } })
    eq(G.extra_attack_spell("Projectile_HamstringShot", Ext.Stats.Get("Projectile_HamstringShot")), true, "an action")
end)

-- live.md Stray row read, t6 and t7: the client's hover preview, on the caster, cancelled as an order starts.
local PREVIEW_OPTS = { "IsPreview", "IsHoverPreview", "FromClient", "NoUnsheath" }
local function preview_state(sp)
    return { SpellCastState = { Caster = { Uuid = { EntityUuid = P1 } }, SpellId = { OriginatorPrototype = sp },
        CastOptions = PREVIEW_OPTS } }
end

test("hover preview: its cast state isn't the order's, which takes its own", function()
    free_attack_setup()
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    cast_seen = "none"
    local j = swing()
    eq(j.status, "running", "ordered " .. tostring(j.error))
    create_hooks.SpellCastState(preview_state("Target_MainHandAttack"))
    eq(j.cast_entity, nil, "not its entity")
    eq(j.roll.state, "pending", "still waiting for its own")
    eq(j.state_ms, nil, "not started")
    local own = { SpellCastState = last_cast }
    create_hooks.SpellCastState(own)
    eq(j.cast_entity, own, "its own")
    eq(j.roll.state, "cleared", "its own read")
end)

test("stray failed: a preview's, same id, own cast alive with no outcome: no end, no purge, the swing lands", function()
    free_attack_setup()
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    local j = swing()
    eq(j.status, "running", "ordered " .. tostring(j.error))
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = { { Type = "Spell" } } } }
    attack_event("CastSpellFailed")
    eq(j.status, "running", "not its own")
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
    attack_event("CastSpell")
    tick(math.floor(A.CAST_NO_START_COMBAT_MS / 60) + 4)
    eq(j.status, "running", "its CastSpell came: the bound is off")
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil
    attack_event("CastedSpell")
    tick(10)
    eq(j.status, "done", "landed " .. tostring(j.error))
    eq(j.result.extra_attack, true, "the free attack")
end)

test("stray failed: with no CastSpell after it, the job ends at the bound in the same words", function()
    free_attack_setup()
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    local j = swing()
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = { { Type = "Spell" } } } }
    attack_event("CastSpellFailed")
    tick(math.floor(A.CAST_NO_START_COMBAT_MS / 60) - 2)
    eq(j.status, "running", "inside the bound")
    tick(4)
    eq(j.status, "failed", "at the bound")
    assert(j.error:find("no reason given", 1, true), j.error)
    eq(#called("PurgeOsirisQueue"), 1, "its purge, as any failed cast")
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil
end)

local function stray_swing()
    free_attack_setup()
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    local j = swing()
    eq(j.status, "running", "ordered " .. tostring(j.error))
    world[P1].comps.ServerCharacter = { OsirisController = { Tasks = { { Type = "Spell" } } } }
    attack_event("CastSpellFailed")
    return j
end

test("stray failed: with the game paused, the job reads held, not refused, and nothing is purged", function()
    local j = stray_swing()
    world[P1].comp_names = { PAUSED }
    tick(math.ceil(A.CAST_PAUSED_MS / 60) + 2)
    eq(j.status, "interrupted", "held " .. tostring(j.error))
    eq(j.result.paused, true, "paused")
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
    world[P1].comp_names = nil
end)

test("stray failed: a pause that starts late in the bound still reads held", function()
    local j = stray_swing()
    local bound = math.floor(A.CAST_NO_START_COMBAT_MS / 60)
    tick(bound - 5)
    eq(j.status, "running", "inside the bound")
    world[P1].comp_names = { PAUSED }
    tick(8)
    eq(j.status, "running", "past the bound, paused: not failed " .. tostring(j.error))
    tick(math.ceil(A.CAST_PAUSED_MS / 60))
    eq(j.status, "interrupted", "held " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "no purge")
    world[P1].comp_names = nil
end)

test("stray failed: a second stray doesn't restart the bound", function()
    local j = stray_swing()
    local bound = math.floor(A.CAST_NO_START_COMBAT_MS / 60)
    tick(bound - 8)
    attack_event("CastSpellFailed")
    eq(j.status, "running", "still inside the bound")
    tick(12)
    eq(j.status, "failed", "on the first one's time")
end)

test("stray failed: after its CastSpell, with an outcome or with no entity, a failed ends the job at once", function()
    free_attack_setup()
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    local j = swing()
    eq(j.status, "running", "ordered " .. tostring(j.error))
    attack_event("CastSpell")
    attack_event("CastSpellFailed")
    eq(j.status, "failed", "begun")
    reset()
    free_attack_setup()
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    cast_outcome = "CantCast"
    j = swing()
    attack_event("CastSpellFailed")
    cast_outcome = nil
    eq(j.status, "failed", "an outcome")
    reset()
    free_attack_setup()
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    cast_seen = "none"
    j = swing()
    attack_event("CastSpellFailed")
    eq(j.status, "failed", "no entity")
end)

-- GuardCast's linked box branch: a call-allies box reads every group in the game data when nothing of it is in the
-- character's own book or containers, which is also so for one who doesn't hold it. Same flaw as Shout_FireShield's.
local BOX = "Shout_END_AllyAbilities_MultipleSummon"
local function allies_box(book, container)
    combat_setup(9)
    spell(BOX, { SpellType = "Shout", UseCosts = "", SpellFlags = { "IsLinkedSpellContainer", "CallAlliesSpell" } })
    spell("Target_Group", { SpellType = "Target", UseCosts = "ActionPoint:1", SpellContainerID = BOX })
    G.boxData[BOX] = { "Target_Group" }
    world[P1].comps.SpellBook, world[P1].comps.SpellContainer = { Spells = book }, { Spells = container or {} }
    return job(op("cast", { char = P1, spell = BOX }))
end

test("linked box: a call-allies box the character doesn't hold reads does not have that action", function()
    local j = allies_box({})
    G.boxData = {}
    eq(j.error, "the character does not have that action", "not its groups")
end)

test("linked box: a holder through its container alone, with no book row, gets its groups, not the refusal", function()
    local j = allies_box({}, { { ContainerSpell = BOX, SpellId = { OriginatorPrototype = "Target_Group" } } })
    G.boxData = {}
    assert(j.error:find("casts nothing by itself", 1, true), j.error)
    assert(not j.error:find("from the game data", 1, true), j.error)
end)

test("linked box: a holder whose groups don't read still gets the game data's list", function()
    local j = allies_box({ { Id = { Prototype = BOX } } })
    G.boxData = {}
    assert(j.error:find("casts nothing by itself", 1, true), j.error)
    assert(j.error:find("from the game data", 1, true), j.error)
end)

-- live.md "L12 on main 807dce16", rows 389, 871: the held swing charged, its free status not on yet.
local function held_swing_charged(held)
    combat_setup(9)
    world[NPC].pos = { 1.5, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1",
        Level = 0, SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Target_Bolt", { TargetRadius = "18", SpellType = "Target", Level = 0, UseCosts = "ActionPoint:1" })
    spell("Target_BonusSwing", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "BonusActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    local res = world[P1].comps.ActionResources.Resources
    res.BonusActionPoint = { { Level = 0, Amount = 1 } }
    local j = job(op("cast", { char = P1, spell = held or "Target_MainHandAttack", target = NPC }))
    tick(15)
    world[P1].comp_names = { PAUSED }
    tick(20)
    eq(j.status, "interrupted", "held " .. tostring(j.error))
    world[P1].comp_names = nil
    tick(2)
    res.ActionPoint[1].Amount, res.BonusActionPoint[1].Amount = 0, 0
    return j
end
local function gap_swing() return op("cast", { char = P1, spell = "Target_MainHandAttack", target = NPC }) end
local function refused_with(r, pat)
    local e = r.error or (r.job and r.job.error) or ""
    assert(e:find(pat, 1, true), tostring(e))
end

test("held cast: a weapon attack before its free status reads as still resolving, not out of action", function()
    held_swing_charged()
    refused_with(gap_swing(), A.CAST_UNLANDED)
    assert(A.CAST_UNLANDED:find("not enough action yet", 1, true), A.CAST_UNLANDED)
    local plain = op("cast", { char = P1, spell = "Target_Bolt", target = NPC })
    eq(plain.error or (plain.job and plain.job.error), "not enough action", "no free attack pays a plain Action spell")
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    eq(job(gap_swing()).status, "running", "the free status on: it passes, landed or not, as #120")
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = nil
end)

test("held cast: after its landing, no status means no free attack: the plain refusal", function()
    held_swing_charged()
    A.dispatch("CastedSpell", { P1, "Target_MainHandAttack", "Target", "None", 0 })
    local r = gap_swing()
    local e = r.error or (r.job and r.job.error) or ""
    eq(e:find("yet", 1, true), nil, e)
    assert(e:find("not enough action", 1, true), e)
end)

test("held cast: a failed with an outcome ends the wait; no outcome (a preview's) doesn't", function()
    local j = held_swing_charged()
    A.dispatch("CastSpellFailed", { P1, "Target_MainHandAttack", "Target", "None", 0 })
    refused_with(gap_swing(), A.CAST_UNLANDED)
    j.cast_entity.SpellCastOutcome = { Result = "Canceled" }
    A.dispatch("CastSpellFailed", { P1, "Target_MainHandAttack", "Target", "None", 0 })
    j.cast_entity.SpellCastOutcome = nil
    local r = gap_swing()
    local e = r.error or (r.job and r.job.error) or ""
    eq(e:find("yet", 1, true), nil, e)
end)

test("state.actions: a weapon attack in that gap lists still resolving, a plain Action spell no action", function()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ entity = entity, osi = function(f, ...)
        local okf, v = pcall(f, ...)
        if okf then return v end
    end }, { __index = savedV })
    held_swing_charged()
    stats.Shout_Plain = { SpellType = "Shout", UseCosts = "ActionPoint:1", SpellFlags = {} }
    stats.Target_BonusWeapon = { SpellType = "Target", UseCosts = "ActionPoint:1;BonusActionPoint:1", SpellFlags = {},
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } }
    local function book(id) return { PrepareType = "Unknown", Id = { Prototype = id, OriginatorPrototype = id } } end
    world[P1].comps.SpellBook = { Spells = { book("Target_MainHandAttack"), book("Shout_Plain"),
        book("Target_BonusWeapon") } }
    local res = world[P1].comps.ActionResources.Resources
    local function read()
        BG3AI.State.spellCache = {}
        local ok, list = pcall(BG3AI.State.actions, P1)
        eq(ok, true, "actions read: " .. tostring(list))
        local by = {}
        for _, a in ipairs(list) do by[a.spell] = a end
        return by
    end
    local by = read()
    eq(by.Target_MainHandAttack.available, false, "not free yet")
    eq(by.Target_MainHandAttack.reason, "last cast still resolving", "its reason")
    eq(by.Shout_Plain.reason, "no action", "a plain Action spell")
    res.ActionPoint[1].Amount, res.BonusActionPoint = 1, { { Level = 0, Amount = 0 } }
    eq(read().Target_BonusWeapon.reason, "no bonus action", "another resource short isn't the gap")
    BG3AI.Visibility = savedV
    world[P1].comps.SpellBook = nil
end)

test("held cast: the words need a started weapon attack of this character's, unlanded, not failed", function()
    local function counts(over)
        local h = { char = P1, began = true, gives_free = true }
        for k, v in pairs(over) do h[k] = v end
        A.held_audits = { h }
        return A.cast_unlanded(P1)
    end
    eq(counts({}), true, "the plain case")
    eq(counts({ char = NPC }), false, "another character's")
    eq(counts({ began = false }), false, "not started")
    eq(counts({ gives_free = false }), false, "can't bring the status")
    eq(counts({ landed_ms = 1 }), false, "landed")
    eq(counts({ failed = true }), false, "failed")
    eq(counts({ countered = true }), false, "countered: no free attack comes")
    local now = RPC.now_ms()
    eq(counts({ free_ms = now - A.HELD_LANDS_MS + 100 }), true, "inside the bound")
    eq(counts({ free_ms = now - A.HELD_LANDS_MS }), false, "at the bound")
end)

test("held cast: a held cast that can't bring the status leaves the plain refusal", function()
    for _, held in ipairs({ "Target_Bolt", "Target_BonusSwing" }) do
        held_swing_charged(held)
        local r = gap_swing()
        eq(r.error or (r.job and r.job.error), "not enough action", held)
    end
end)

test("held cast: in the window a weapon attack short of its bonus action says so, not 'yet'", function()
    held_swing_charged()
    spell("Target_BothSwing", { TargetRadius = "1.5", SpellType = "Target", Level = 0,
        UseCosts = "BonusActionPoint:1;ActionPoint:1", SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    local r = op("cast", { char = P1, spell = "Target_BothSwing", target = NPC })
    eq(r.error or (r.job and r.job.error), "not enough bonus action", "the shortage it has")
end)

-- REVIEW-reactions.md F1: a failed reads every matching entry, as before this head: a Canceled doesn't stop the loop.
local function failed_entries(one, two)
    local function entry(outcome)
        return { char = P1, began = true, matches = function() return true end,
            entity = { SpellCastOutcome = { Result = outcome } } }
    end
    local a, b = entry(one), entry(two)
    A.held_audits = { a, b }
    return a, b
end

test("held cast: an older Canceled entry doesn't keep a newer Countered one from being countered", function()
    combat_setup(9)
    local a, b = failed_entries("Canceled", "Countered")
    A.held_cast_failed(P1, "Target_MainHandAttack")
    eq(a.failed, true, "older")
    eq(a.countered, nil, "older isn't countered")
    eq(b.countered, true, "newer")
end)

test("held cast: one failed ends every matching entry with a readable outcome", function()
    combat_setup(9)
    local a, b = failed_entries("Canceled", "Canceled")
    A.held_cast_failed(P1, "Target_MainHandAttack")
    eq(a.failed, true, "older")
    eq(b.failed, true, "newer")
    a.entity.SpellCastOutcome.Result = "Countered"
    A.held_cast_failed(P1, "Target_MainHandAttack")
    eq(a.countered, true, "h.failed only ends the words: what a cast pays reads as it did before")
end)
