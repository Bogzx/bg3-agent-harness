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
