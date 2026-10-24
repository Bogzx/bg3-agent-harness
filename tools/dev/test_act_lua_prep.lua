-- Part of test_act_lua.lua (split for the 1000-line cap): keys and lockpicking, turn-based mode outside combat, spell preparation (A5, A7), Gather Your Allies (A27).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- ---- locked doors: keys and lockpicking --------------------------------------

-- `who` carries KEY (in a keychain bag) that fits DOOR's lock.
local function keyed_door(who, door_key, key_name)
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = door_key or "PriestessKey" }
    local key_e = { Key = { Key = key_name or "PriestessKey" }, Uuid = { EntityUuid = KEY } }
    local chain = { Uuid = { EntityUuid = "12121212-0000-0000-0000-000000000008" },
        InventoryOwner = { PrimaryInventory = { InventoryContainer = { Items = { { Item = key_e } } } } } }
    local junk = { Uuid = { EntityUuid = GEM } }
    world[who].comps.InventoryOwner = { PrimaryInventory = { InventoryContainer = {
        Items = { { Item = junk }, { Item = chain } } } } }
end

local function add_p2(pos)
    party_rows = { P1, P2 }
    world[P2] = { pos = pos or { 1, 0, 0 }, comps = {} }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("IsOnStage", P2)] = 1
end

test("interact locked: nobody carries the key -> clicked like a player's, 'still locked', no Unlock", function()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "PriestessKey" }
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use in place")
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(6)
    eq(j.status, "failed", "status"); eq(j.error, A.STILL_LOCKED, "only what the game shows")
    eq(#called("Unlock"), 0, "Unlock")
end)

-- ACT-6: a story lock (the Temple of Bhaal door the amulet opens) is clicked anyway. Audit C13: no lock is told
-- apart by its hidden lockpick-difficulty id any more (a pickable id here changes nothing).
local function story_door()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "NO_KEY", LockDifficultyClassID = "0d9484eb-0000-4000-8000-000000000001", LockDC = 0 }
end

test("interact story lock, no key: walks and clicks it; the game's script opens it -> done", function()
    story_door()
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Use"), 1, "Use"); eq(#called("Unlock"), 0, "never Unlock")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    osiv[key("IsLocked", DOOR)] = nil
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
    eq(j.result.unlocked_with, nil, "no key reported")
end)

test("interact story lock: the click changes nothing -> failed, 'still locked' only", function()
    story_door()
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use in place")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    tick(30)
    eq(j.status, "failed", "status " .. tostring(j.status)); eq(j.error, A.STILL_LOCKED, "no lever/puzzle hint")
end)

test("interact story lock: the Use fails -> the story-lock reason", function()
    story_door()
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(6) -- the failure waits A.USE_FAIL_GRACE_MS for a success
    eq(j.status, "failed", "status"); eq(j.error, A.STILL_LOCKED, "error")
end)

test("interact locked: a key with another name does not fit", function()
    keyed_door(P1, "PriestessKey", "PrisonKey")
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "clicked without a key " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(#called("Unlock"), 0, "the other key is not used")
end)

test("interact locked: actor's own key (keychain) -> walk, Unlock, Use, reported", function()
    keyed_door(P1)
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(#called("Unlock"), 0, "no Unlock before arrival")
    arrive(j, { 8.5, 0, 0 })
    local ul = called("Unlock")
    eq(#ul, 1, "Unlock after arrival"); eq(ul[1][2], DOOR, "door"); eq(ul[1][3], P1, "by")
    eq(#called("Use"), 1, "Use")
    osiv[key("IsLocked", DOOR)] = nil
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(j.result.opened, true, "opened"); assert(j.result.unlocked_with, "unlocked_with"); assert(j.result.key_holder, "holder")
end)

test("interact locked: another party member's key -> that member opens it", function()
    add_p2({ 1, 0, 0 })
    keyed_door(P2)
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(j.char, P2, "the key holder acts")
    local mv = called("CharacterMoveToPosition"); local d = mv[#mv]
    for i = 1, 5 do world[P2].pos = { 1 + (d[3] - 1) * i / 5, d[4], d[5] * i / 5 }; tick() end
    A.dispatch("EntityEvent", { P2, j.walk.ev })
    assert(j.status == "running", tostring(j.status) .. " " .. tostring(j.error) .. " " .. tostring(j.phase))
    local ul = called("Unlock")
    eq(#ul, 1, "Unlock"); eq(ul[1][3], P2, "by the holder")
    local use = called("Use"); eq(#use, 1, "Use"); eq(use[1][2], P2, "Use by the holder")
end)

test("interact locked: Use reports failure and it stays locked -> says the key did not open it", function()
    keyed_door(P1)
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Unlock"), 1, "Unlock in place")
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(6) -- the failure waits A.USE_FAIL_GRACE_MS for a success
    eq(j.status, "failed", "status"); assert(j.error:find("still locked"), j.error)
end)

test("interact: a door's UseFinished success then failure is a use; it opens -> done (S085 Sluice Gate, Vault Door)", function()
    -- S085 04:53:03 e455 Vault Door (Knock had unlocked it): EVENTS `used … success` then `used …`, the job said
    -- "could not use that", the door opened a moment later
    world[DOOR].pos = { 1, 0, 0 }
    local j = job(op("interact", { char = P1, target = DOOR }))
    eq(#called("Use"), 1, "Use in place")
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick()
    eq(j.status, "running", "still waiting for the door: " .. tostring(j.error))
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error)); eq(j.result.opened, true, "opened")
    -- the failure first, the success within the grace: still a use
    osiv[key("IsOpened", DOOR)] = nil
    j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(2)
    A.dispatch("UseFinished", { P1, DOOR, 1 })
    tick(6)
    eq(j.status, "running", "success after the failure: " .. tostring(j.error))
    osiv[key("IsOpened", DOOR)] = 1
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    -- a failure alone still fails, after the grace
    osiv[key("IsOpened", DOOR)] = nil
    j = job(op("interact", { char = P1, target = DOOR }))
    A.dispatch("UseFinished", { P1, DOOR, 0 })
    tick(2)
    eq(j.status, "running", "within the grace")
    tick(4)
    eq(j.status, "failed", "status"); eq(j.error, "could not use that", "reason")
end)

test("guard: Unlock stays blocked through G.osi; unlock_with_key needs the key and adjacency", function()
    local refused_or_error = pcall(G.osi, "Unlock", DOOR, P1)
    eq(refused_or_error, false, "G.osi Unlock")
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "PriestessKey" }
    eq(pcall(G.unlock_with_key, DOOR, P1, 2.5), false, "no key")
    keyed_door(P1)
    eq(pcall(G.unlock_with_key, DOOR, P1, 2.5), false, "too far (10 m)")
    world[DOOR].pos = { 1, 0, 0 }
    eq(pcall(G.unlock_with_key, DOOR, P1, 2.5), true, "key + adjacent")
    eq(#called("Unlock"), 1, "one Unlock")
end)

test("guard (ACT-13): CharacterMoveTo is blocked; G.q runs queries only", function()
    eq(pcall(G.osi, "CharacterMoveTo", P1, NPC, "Walk", "ev"), false, "G.osi CharacterMoveTo")
    eq(#called("CharacterMoveTo"), 0, "no CharacterMoveTo")
    osiv[key("IsLocked", DOOR)] = 1
    eq(G.q("IsLocked", DOOR), 1, "a query runs")
    eq(G.q("Unlock", DOOR, P1), nil, "a blocked action through G.q"); eq(#called("Unlock"), 0, "no Unlock")
    eq(G.q("Use", P1, DOOR, "ev"), nil, "an allowlisted action through G.q"); eq(#called("Use"), 0, "no Use")
    eq(G.q("DB_Foo", P1), nil, "a DB insert"); eq(G.q("SetHitpoints", P1, 1), nil, "a non-query name")
    eq(G.query_ok("InSamePartyGroup"), true, "InSamePartyGroup"); eq(G.query_ok("SpeakerGetDialog"), true, "SpeakerGetDialog")
end)

test("disarm: a discovered trap out of sight is approached and right-clicked (S064); unknown refused", function()
    osiv[key("IsTrap", GEM)] = 1
    osiv[key("CanSee", P1, GEM)], osiv[key("HasLineOfSight", P1, GEM)] = nil, nil
    eq(G.visible(GEM), false, "out of sight")
    local j = job(op("disarm", { char = P1, target = GEM }))
    eq(j.status, "failed", "undiscovered"); assert(j.error:find("no known trap there", 1, true), j.error)
    osiv[key("IsTrapDiscovered", GEM)] = 1
    j = job(op("disarm", { char = P1, target = GEM }))
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "discovered " .. tostring(j.error))
    eq(j.result.needs_click, GEM, "needs_click"); eq(j.result.menu, "Disarm", "menu item")
end)

test("lockpick: a party member carries the key -> refused, points to interact", function()
    keyed_door(P1)
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    eq(j.status, "failed", "status"); assert(j.error:find("use interact"), j.error)
end)

-- The click's own request (NETMSG_CHARACTER_LOCKPICK -> LockpickingSystem.PickLockRequests).
local function pick_system()
    Ext.System = { ServerLockpicking = { PickLockRequests = {} } }
    return Ext.System.ServerLockpicking.PickLockRequests
end

test("lockpick (default): no engine request; right-click -> context menu 'Lockpick' (S024 hand route)", function()
    osiv[key("IsLocked", DOOR)] = 1
    local reqs = pick_system()
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    arrive(j, { 8.5, 0, 0 })
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#reqs, 0, "no PickLockRequest (live: never consumed, stale ones pile up)")
    eq(j.result.needs_click, DOOR, "needs_click"); eq(j.result.expect, "roll", "expect")
    eq(j.result.button, "R", "right-click"); eq(j.result.menu, "Lockpick", "menu item")
end)

test("lockpick: a lock with no lockpick difficulty (zero DC id) -> the right-click, as any lock (audit C13)", function()
    -- the game's own menu shows whether Lockpick is offered; the hidden difficulty id is never read
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "NO_KEY", LockDifficultyClassID = "00000000-0000-0000-0000-000000000000", LockDC = 0 }
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    arrive(j, { 8.5, 0, 0 })
    eq(j.result.needs_click, DOOR, "needs_click"); eq(j.result.menu, "Lockpick", "menu")
end)

test("lockpick: a lock with a difficulty id -> proceeds to the right-click, no engine request", function()
    osiv[key("IsLocked", DOOR)] = 1
    world[DOOR].comps.Lock = { Key_M = "NO_KEY", LockDifficultyClassID = "0d9484eb-0000-4000-8000-000000000001", LockDC = 0 }
    local reqs = pick_system()
    local j = job(op("lockpick", { char = P1, target = DOOR }))
    arrive(j, { 8.5, 0, 0 })
    eq(#reqs, 0, "no PickLockRequest (it corrupts the game's array: crash 12:35)")
    eq(j.result.needs_click, DOOR, "needs_click"); eq(j.result.menu, "Lockpick", "menu")
end)

-- ---- turn-based mode outside combat (S024-S026 Shadow Curse) ---------------

test("tb gate: a walk in turn-based mode outside combat is refused at once, no walk", function()
    osiv[key("IsInForceTurnBasedMode", P1)] = 1
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 } }))
    eq(j.status, "failed", "status"); assert(j.error:find("turn-based mode outside combat", 1, true), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no walk")
    j = job(op("interact", { char = P1, target = DOOR }))
    eq(j.status, "failed", "interact status")
end)

test("tb gate: tb_ok (the agent's own turn-based mode) walks; in combat no gate", function()
    osiv[key("IsInForceTurnBasedMode", P1)] = 1
    local j = job(op("move_to", { char = P1, pos = { 5, 0, 0 }, tb_ok = true }))
    eq(j.status, "running", "tb_ok status " .. tostring(j.error))
end)

test("act.turn_based off: ForceTurnBasedMode for every party member still in it", function()
    party_rows = { P1, NPC }
    osiv[key("IsInForceTurnBasedMode", P1)] = 1
    osiv[key("IsInForceTurnBasedMode", NPC)] = 1
    local j = job(op("turn_based", { on = false }))
    tick(2)
    eq(#called("ForceTurnBasedMode"), 2, "both members")
    local m = RPC.ops["act.mode"]({})
    eq(m.ftb, true, "act.mode ftb"); eq(#m.ftb_members, 2, "members")
    eq(j.status, "running", "running until they leave it")
end)

test("interact: in combat a carried potion is drunk on the drinker's turn for its own cost (S031 at 7 HP)", function()
    combat_setup(9)
    local POT = "a0a0a0a0-0000-0000-0000-000000000009"
    world[POT] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "OBJ_Potion_Healing" } } }
    osiv[key("IsItem", POT)] = 1
    osiv[key("GetInventoryOwner", POT)] = P1
    stats.OBJ_Potion_Healing = { ItemUseType = "Potion", UseCosts = "BonusActionPoint:1" }
    world[P1].comps.ActionResources.Resources.BonusActionPoint = { { Level = 0, Amount = 1 } }
    local j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "running", "drinks: " .. tostring(j.error))
    eq(#called("Use"), 1, "one Use")
    A.dispatch("UseFinished", { P1, POT, 1 })
    tick(10)
    eq(j.status, "done", "done"); eq(j.result.drunk, true, "drunk")
    eq(j.result.charged_by_guard[1].resource, "bonus action", "bonus charged")
    eq(world[P1].comps.ActionResources.Resources.BonusActionPoint[1].Amount, 0, "bonus spent")
    -- no bonus left: refused, no Use
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "no bonus"); assert(j.error:find("not enough bonus action", 1, true), j.error)
    -- not this character's turn
    world[P1].comps.ActionResources.Resources.BonusActionPoint[1].Amount = 1
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "not my turn"); assert(j.error:find("not this character's turn", 1, true), j.error)
    -- any other carried item (not a potion or a scroll) stays out of combat
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    stats.OBJ_Potion_Healing.ItemUseType = "Food"
    j = job(op("interact", { char = P1, target = POT }))
    eq(j.status, "failed", "food"); assert(j.error:find("during combat", 1, true), j.error)
    eq(#called("Use"), 1, "still one Use")
end)

test("interact (A7): in combat a carried scroll is read on the reader's turn for an action; settle charges once", function()
    combat_setup(9)
    local SCR = "a0a0a0a0-0000-0000-0000-00000000000a"
    world[SCR] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "OBJ_Scroll_Fear" } } }
    osiv[key("IsItem", SCR)] = 1
    osiv[key("GetInventoryOwner", SCR)] = P1
    osiv[key("GetStackAmount", SCR)] = 2
    stats.OBJ_Scroll_Fear = { ItemUseType = "Scroll", UseCosts = "ActionPoint:1" }
    local j = job(op("interact", { char = P1, target = SCR }))
    eq(j.status, "running", "reads: " .. tostring(j.error))
    eq(#called("Use"), 1, "one Use")
    A.dispatch("UseFinished", { P1, SCR, 1 })
    tick(10)
    eq(j.status, "done", "done"); eq(j.result.combat_scroll, true, "flagged for the target clicks")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 1, "no charge at the Use")
    -- the target not clicked yet: the scroll is still there -> nothing charged, still pending
    local r = op("scroll_settle", { char = P1 })
    eq(r.read, false, "not used up yet"); eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 1, "kept")
    -- the spell went off (stack 2 -> 1) and the engine charged nothing: the guard charges the action
    osiv[key("GetStackAmount", SCR)] = 1
    r = op("scroll_settle", { char = P1 })
    eq(r.read, true, "read"); eq(r.charged[1].resource, "action", "action charged")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 0, "action spent")
    eq(op("scroll_settle", { char = P1 }).none, true, "settled once")
    -- no action left: refused, no Use
    j = job(op("interact", { char = P1, target = SCR }))
    eq(j.status, "failed", "no action"); assert(j.error:find("not enough action to read it", 1, true), j.error)
    eq(#called("Use"), 1, "still one Use")
    -- not the reader's turn
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    world[P1].comps.TurnBased.IsActiveCombatTurn = false
    j = job(op("interact", { char = P1, target = SCR }))
    eq(j.status, "failed", "not my turn"); assert(j.error:find("not this character's turn", 1, true), j.error)
    -- the engine charged the action itself (Use took it): settle charges nothing more
    world[P1].comps.TurnBased.IsActiveCombatTurn = true
    j = job(op("interact", { char = P1, target = SCR }))
    A.dispatch("UseFinished", { P1, SCR, 1 })
    tick(10)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 0
    osiv[key("IsItem", SCR)] = 0   -- the last scroll is gone
    r = op("scroll_settle", { char = P1 })
    eq(r.read, true, "read (gone)"); eq(#r.charged, 0, "nothing more charged")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 0, "not below 0")
end)

test("cast: a Devotion paladin's killing blow on a Temporarily Hostile or neutral target is the player's call (user, 2026-10-18)", function()
    combat_setup(9)
    world[GOB].pos = { 1.2, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" }, SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    world[P1].comps.PassiveContainer = { Passives = { { Passive = { PassiveId = "Oath_Devotion_Tenents" } },
        { Passive = { PassiveId = "NonLethal", Disabled = true } } } }
    world[GOB].comps.Health = { Hp = 2, MaxHp = 67 }
    osiv[key("HasActiveStatus", GOB, "TEMPORARILY_HOSTILE")] = 1
    osiv[key("IsEnemy", P1, GOB)] = 1
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ rules = function() return { HIDE_NPC_HP = false } end,
        statusInfo = function(id) return ({ TEMPORARILY_HOSTILE = { name = "Temporarily Hostile" } })[id] end },
        { __index = savedV })
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "running", "temporarily hostile at 2 HP: " .. tostring(j.error))
    RPC.jobs[j.id] = nil; A.active[P1] = nil
    -- a neutral at 2 HP: struck too
    osiv[key("IsEnemy", P1, GOB)] = nil
    osiv[key("HasActiveStatus", GOB, "TEMPORARILY_HOSTILE")] = nil
    j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "running", "neutral at 2 HP: " .. tostring(j.error))
    RPC.jobs[j.id] = nil; A.active[P1] = nil
    BG3AI.Visibility = savedV
    eq(G.oath_risk, nil, "no oath guard")
end)

test("go_to_camp: a dead selected character doesn't block the party's camp button (S035 19:38)", function()
    Osi.TeleportToFromCamp = record("TeleportToFromCamp")
    add_p2()
    osiv[key("GetHostCharacter")] = P1
    osiv[key("IsDead", P1)] = 1
    local j = job(op("go_to_camp", {}))
    eq(j.status, "running", "status: " .. tostring(j.error))
    local tp = called("TeleportToFromCamp")
    eq(#tp, 1, "teleport"); eq(tp[1][2], P2, "the living member presses it")
    RPC.jobs[j.id] = nil; A.active[P2] = nil
    -- an explicit char is still checked
    j = job(op("go_to_camp", { char = P1 }))
    eq(j.status, "failed", "explicit dead char"); assert(j.error:find("character is dead", 1, true), j.error)
end)

test("interact ladder: chained followers settle, are held for the climb and re-chained after (acceptance 04:31)", function()
    add_p2({ 1, 0, 0 })
    osiv[key("InSamePartyGroup", P1, P2)] = 1
    world[P1].pos = { 0, 0, 0 }
    local j = job(op("interact", { char = P1, target = LADDER }))
    world[P1].pos = { 9.3, 0, 0 }
    A.dispatch("EntityEvent", { P1, j.walk.ev })
    tick()
    for i = 1, 5 do world[P2].pos = { 8.0 + i * 0.1, 0, 0 } tick() end   -- the follower still arriving
    clock = clock + A.LADDER_SETTLE_MS + 1
    world[P2].pos = { 8.6, 0, 0 } tick()
    eq(#called("Use"), 0, "no Use while a follower moves")
    tick() clock = clock + A.LADDER_SETTLE_MS + 1 tick()
    eq(#called("Use"), 1, "Use once everyone stands")
    local d = called("DetachFromPartyGroup")
    eq(#d, 1, "the follower detached for the climb"); eq(d[1][2], P2, "who")
    A.dispatch("UseFinished", { P1, LADDER, 1 })
    osiv[key("InSamePartyGroup", P1, P2)] = 0
    world[P1].pos = { 9.6, -3, 0 } tick()
    world[P1].pos = { 9.7, -5.8, 0 } tick()
    world[P1].pos = { 10.2, -5.8, 0.4 } tick() tick()
    eq(j.status, "done", "status " .. tostring(j.error))
    local a = called("AttachToPartyGroup")
    eq(#a, 1, "re-chained"); eq(a[1][2], P2, "follower"); eq(a[1][3], P1, "to the climber")
end)

test("interact on a pulled-up ladder from below: says it hangs out of reach (S059 08:56 e683)", function()
    world[LADDER].pos = { 10, 5.7, 0 }
    nav.fn = function() return { reachable = false } end
    local savedV, savedR = BG3AI.Visibility, BG3AI.Nav.ladderRaised
    BG3AI.Visibility = { itemKind = function(u) return u == LADDER and "ladder" or "item" end }
    BG3AI.Nav.ladderRaised = function(p) return p[2] > 2.5 and 5.7 or nil end
    local ok, err = pcall(G.approach_point, P1, LADDER, 1.5)
    local ok2, err2 = pcall(G.approach_point, P1, DOOR, 1.5)
    world[P1].pos = { 10, 12, 0 }
    local ok3 = pcall(G.raised_ladder_refuse, LADDER, world[P1].pos)
    BG3AI.Visibility, BG3AI.Nav.ladderRaised = savedV, savedR
    eq(ok, false, "refused")
    eq(tostring(err):find("pulled up: its foot hangs 5.7m", 1, true) ~= nil, true, "names the raised foot: " .. tostring(err))
    eq(ok2, false, "door refused too"); eq(tostring(err2):find("pulled up", 1, true), nil, "the door: no ladder text")
    eq(ok3, true, "from above: no refusal")
end)

test("state.actions: lists only prepared class spells (A4: S065 showed Shadowheart every level-1 cleric spell)", function()
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ entity = entity, osi = function(f, ...)
        local okf, v = pcall(f, ...)
        if okf then return v end
    end }, { __index = savedV })
    if not BG3AI.State or not BG3AI.State.actions then dofile(LUA .. "/Server/State.lua") end
    for _, id in ipairs({ "Target_Bless", "Target_CureWounds", "Target_Sanctuary" }) do
        stats[id] = { Level = 1, SpellType = "Target", UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:1" }
    end
    local function book(id, kind) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = kind } end
    world[P1].comps.SpellBook = { Spells = { book("Target_Bless", "RequiresPreparation"),
        book("Target_CureWounds", "RequiresPreparation"), book("Target_Sanctuary", "AlwaysPrepared") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds") } }
    BG3AI.State.spellCache = {}
    local ok, list = pcall(BG3AI.State.actions, P1)
    BG3AI.Visibility = savedV
    world[P1].comps.SpellBook, world[P1].comps.SpellBookPrepares = nil, nil
    eq(ok, true, "actions read: " .. tostring(list))
    local ids = {}
    for _, a in ipairs(list) do ids[#ids + 1] = a.spell end
    eq(table.concat(ids, ","), "Target_CureWounds,Target_Sanctuary", "no unprepared Bless")
end)

test("state.combat hp_pct: a member at 1/218 HP reads 1 %, not 0 (S076 Karlach called downed)", function()
    if not BG3AI.State or not BG3AI.State.hp_pct then dofile(LUA .. "/Server/State.lua") end
    eq(BG3AI.State.hp_pct(1, 218), 1, "1/218")
    eq(BG3AI.State.hp_pct(0, 218), 0, "0/218")
    eq(BG3AI.State.hp_pct(109, 218), 50, "half")
    eq(BG3AI.State.hp_pct(218, 218), 100, "full")
end)

-- ---- A5: prepare_spells plan (Guard.prep_info / prep_plan, act.prepare_plan, act.prepared) -------------------

-- Claude the Paladin: Bless, Cure Wounds, Command, Shield of Faith need preparing (Cure Wounds and Shield of Faith
-- prepared), Sanctuary is the oath's, limit 3.
local function paladin(max)
    G.spellFacts = {}  -- each helper redefines the stats; the game's never change (V12 memo)
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h end
    local function book(id, kind) return { Id = { Prototype = id, OriginatorPrototype = id }, PrepareType = kind } end
    local shown = { Target_Bless = "Bless", Target_CureWounds = "Cure Wounds", Target_Command_Container = "Command",
                    Target_ShieldOfFaith = "Shield of Faith", Target_Sanctuary = "Sanctuary",
                    Target_MainHandAttack = "Attack" }
    for id, name in pairs(shown) do stats[id] = { DisplayName = name, Level = id == "Target_MainHandAttack" and 0 or 1 } end
    world[P1].comps.SpellBook = { Spells = { book("Target_Bless", "RequiresPreparation"),
        book("Target_CureWounds", "RequiresPreparation"), book("Target_Command_Container", "RequiresPreparation"),
        book("Target_ShieldOfFaith", "RequiresPreparation"), book("Target_Sanctuary", "AlwaysPrepared"),
        book("Target_MainHandAttack", "Unknown") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds"),
        meta_id("Target_ShieldOfFaith"), meta_id("Target_Sanctuary") } }
    world[P1].comps.Stats = { MaxPreparedSpells = max or 3 }
    osiv[key("GetDisplayName", P1)] = "Claude"
    return function() Ext.Loca.GetTranslatedString = loca end
end

test("prep_info: prepared / unprepared / always-prepared rows and the game's limit", function()
    local undo = paladin()
    local info = G.prep_info(P1)
    undo()
    local function ids(rows) local o = {} for _, r in ipairs(rows) do o[#o + 1] = r.name end return table.concat(o, ",") end
    eq(ids(info.prepared), "Cure Wounds,Shield of Faith", "prepared")
    eq(ids(info.unprepared), "Bless,Command", "unprepared")
    eq(ids(info.always), "Sanctuary", "always (no weapon actions)")
    eq(info.max, 3, "limit = Stats.MaxPreparedSpells"); eq(info.count, 2, "count"); eq(info.prepares, true, "prepares")
end)

test("prep_plan: add within the limit, names or ids in any case; notes for no-ops", function()
    local undo = paladin()
    local p = G.prep_plan(P1, { "bless", "Target_ShieldOfFaith", "Sanctuary" }, { "Command" })
    undo()
    eq(#p.add, 1, "one add"); eq(p.add[1].id, "Target_Bless", "Bless by name")
    eq(#p.remove, 0, "Command was not prepared: nothing to remove")
    eq(p.after, 3, "2 + 1"); eq(p.name, "Claude", "who")
    local notes = table.concat(p.notes, "|")
    eq(notes:find("Command was not prepared", 1, true) ~= nil, true, notes)
    eq(notes:find("Shield of Faith is prepared already", 1, true) ~= nil, true, notes)
    eq(notes:find("Sanctuary is always prepared already", 1, true) ~= nil, true, notes)
end)

test("prep_plan: over the limit names what is prepared; a swap in the same call fits", function()
    local undo = paladin(2)
    refused(function() G.prep_plan(P1, { "Bless" }, {}) end,
        "over the limit: Claude can prepare 2 spells and has 2 prepared (Cure Wounds, Shield of Faith); remove 1")
    local p = G.prep_plan(P1, { "Bless" }, { "Cure Wounds" })
    undo()
    eq(p.add[1].name, "Bless", "add"); eq(p.remove[1].name, "Cure Wounds", "remove"); eq(p.after, 2, "after")
end)

test("prep_plan refusals: in combat, not in the book, a class that doesn't prepare, an unreadable list", function()
    local undo = paladin()
    refused(function() G.prep_plan(P1, { "Fireball" }, {}) end, "Claude doesn't know Fireball: not in their spellbook")
    refused(function() G.prep_plan(P1, {}, { "Fireball" }) end, "Fireball is not in Claude's spellbook")
    osiv[key("IsInCombat", P1)] = 1
    refused(function() G.prep_plan(P1, { "Bless" }, {}) end, "spells are prepared out of combat")
    osiv[key("IsInCombat", P1)] = 0
    world[P1].comps.SpellBookPrepares = setmetatable({}, { __index = function() error("bad component") end })
    refused(function() G.prep_plan(P1, { "Bless" }, {}) end, "the prepared spell list didn't read")
    -- a warlock: every spell is Unknown (learned at level up), nothing to prepare
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = "Projectile_EldritchBlast" }, PrepareType = "Unknown" },
        { Id = { Prototype = "Target_HexSpell" }, PrepareType = "Unknown" } } }
    refused(function() G.prep_plan(P1, { "Hex" }, {}) end, "Claude doesn't prepare spells: sorcerers, warlocks")
    undo()
end)

test("act.prepare_plan returns {refused}; act.prepared lists only preparing casters", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 1, 0, 0 }, comps = {} }
    local undo = paladin()
    local r = op("prepare_plan", { char = P1, add = { "Fireball" } })
    local ok_plan = op("prepare_plan", { char = P1, add = { "Bless" } })
    local list = op("prepared", {})
    undo()
    eq(r.refused ~= nil and r.refused:find("doesn't know Fireball", 1, true) ~= nil, true, tostring(r.refused))
    eq(ok_plan.add[1].id, "Target_Bless", "plan")
    eq(#list.casters, 1, "only P1 prepares"); eq(list.casters[1].uuid, P1, "uuid")
    eq(list.casters[1].max, 3, "max"); eq(#list.casters[1].prepared, 2, "prepared rows")
end)

-- ---- A7: live L4 shapes (Claude S065-end: "31 prepared, limit 9"; page 12/12) -----------------------------------

-- Claude's book as bg3se gives it: upcast entries (_2/_3) with the base as OriginatorPrototype, Command's variants as
-- their own RequiresPreparation entries (stats SpellContainerID), SpellBookPrepares listing only the container.
local function paladin_l4(class_max)
    G.spellFacts = {}  -- each helper redefines the stats; the game's never change (V12 memo)
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h end
    local function book(id, orig, kind) return { Id = { Prototype = id, OriginatorPrototype = orig },
                                                 PrepareType = kind or "RequiresPreparation" } end
    local function sp(id, name, school, flags, costs, extra)
        stats[id] = { DisplayName = name, Level = 1, SpellSchool = school, SpellFlags = flags, UseCosts = costs,
                      Icon = "Spell_" .. name:gsub("%W", "") }
        for k, v in pairs(extra or {}) do stats[id][k] = v end
    end
    sp("Target_Bless", "Bless", "Enchantment", { "IsConcentration", "IsSpell" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Heroism", "Heroism", "Enchantment", { "IsConcentration" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Bane", "Bane", "Enchantment", { "IsConcentration" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_CureWounds", "Cure Wounds", "Evocation", { "IsSpell" }, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Smite_Wrathful", "Wrathful Smite", "Evocation", { "IsConcentration" },
        "ActionPoint:1;BonusActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Command_Container", "Command", "Enchantment", {}, "ActionPoint:1;SpellSlotsGroup:1:1:1")
    sp("Target_Command_Halt", "Command: Halt", "Enchantment", {}, "ActionPoint:1",
        { SpellContainerID = "Target_Command_Container" })
    world[P1].comps.SpellBook = { Spells = {
        book("Target_Bless", "Target_Bless"), book("Target_Bless_2", "Target_Bless"),
        book("Target_Heroism", "Target_Heroism"), book("Target_Heroism_2", "Target_Heroism"),
        book("Target_CureWounds", "Target_CureWounds"), book("Target_CureWounds_2", "Target_CureWounds"),
        book("Target_CureWounds_3", "Target_CureWounds"), book("Target_Smite_Wrathful", "Target_Smite_Wrathful"),
        book("Target_Command_Container", "Target_Command_Container"),
        book("Target_Command_Halt", "Target_Command_Halt"), book("Target_Command_Halt_2", "Target_Command_Halt") } }
    world[P1].comps.SpellBookPrepares = { PreparedSpells = { meta_id("Target_CureWounds"), meta_id("Target_Heroism"),
        meta_id("Target_Smite_Wrathful"), meta_id("Target_Command_Container") },
        ClassPreparedSpellCount = class_max and { ["c0de0000-0000-0000-0000-00000000000a"] = class_max } or nil }
    world[P1].comps.Stats = { MaxPreparedSpells = 3 }
    osiv[key("GetDisplayName", P1)] = "Claude"
    return function() Ext.Loca.GetTranslatedString = loca end
end

test("prep_info (A7, live L4): one row per spell (upcasts merged, container variants no rows); the class limit", function()
    local undo = paladin_l4(12)
    local info = G.prep_info(P1)
    undo()
    local function ids(rows) local o = {} for _, r in ipairs(rows) do o[#o + 1] = r.name end return table.concat(o, ",") end
    eq(ids(info.prepared), "Command,Cure Wounds,Heroism,Wrathful Smite", "prepared: 4, once each")
    eq(ids(info.unprepared), "Bless", "unprepared: Bless once, no Command: Halt")
    eq(info.count, 4, "count 4 (not 9 entries)")
    eq(info.max, 12, "limit = SpellBookPrepares.ClassPreparedSpellCount"); eq(info.stats_max, 3, "stats max kept")
    eq(info.prepared[4].look.cost, "BothActions", "Wrathful Smite: BothActions")
    eq(info.unprepared[1].look.icon, "Spell_Bless", "icon"); eq(info.unprepared[1].look.conc, true, "conc")
    -- no class counts: Stats.MaxPreparedSpells
    undo = paladin_l4(nil)
    info = G.prep_info(P1)
    undo()
    eq(info.max, 3, "no class map: Stats.MaxPreparedSpells")
end)

test("unprepared (A7): a container's variant casts with its container prepared", function()
    local undo = paladin_l4(12)
    local set = G.prepared_ids(G.entity(P1))
    local spells = world[P1].comps.SpellBook.Spells
    eq(G.unprepared(spells[10], set), false, "Command: Halt castable (Command prepared)")
    eq(G.unprepared(spells[11], set), false, "Halt's upcast too")
    eq(G.unprepared(spells[2], set), true, "Bless_2 not prepared")
    world[P1].comps.SpellBookPrepares.PreparedSpells = { meta_id("Target_CureWounds") }
    eq(G.unprepared(spells[10], G.prepared_ids(G.entity(P1))), true, "Halt refused once Command isn't prepared")
    undo()
end)

test("prep_plan (A7): no over-limit refusal under the page's limit; each change carries its page look + twins", function()
    local undo = paladin_l4(12)
    local p = G.prep_plan(P1, { "Bless" }, { "Heroism" })
    eq(p.add[1].twins, 0, "Bless: no unprepared look-alike")
    eq(p.remove[1].twins, 0, "Heroism: no prepared look-alike")
    eq(p.add[1].look.school, "Enchantment", "look rides along"); eq(p.add[1].level, 1, "level")
    eq(p.class_max, 12, "class_max"); eq(p.stats_max, 3, "stats_max")
    -- Bane in the book unprepared and alike: Bless now has a twin
    table.insert(world[P1].comps.SpellBook.Spells, { Id = { Prototype = "Target_Bane", OriginatorPrototype = "Target_Bane" },
                                                     PrepareType = "RequiresPreparation" })
    p = G.prep_plan(P1, { "Bless" }, {})
    eq(p.add[1].twins, 1, "Bless and Bane look the same on the page")
    local list = op("prepared", { uuids = { P1 } })
    undo()
    eq(list.casters[1].prepared[1].look, nil, "act.prepared rows carry no look"); eq(list.casters[1].max, 12, "max")
end)

test("act.prepared (V12): a second read gets no stats entry again; same rows", function()
    local undo = paladin_l4(12)
    local get, n = Ext.Stats.Get, 0
    Ext.Stats.Get = function(id) n = n + 1; return get(id) end
    local first = op("prepared", { uuids = { P1 } })
    local cold = n
    n = 0
    local again = op("prepared", { uuids = { P1 } })
    Ext.Stats.Get = get
    undo()
    local function names(l) local o = {} for _, r in ipairs(l.casters[1].prepared) do o[#o + 1] = r.name .. r.level end
        return table.concat(o, ",") end
    eq(cold > 0, true, "the first read gets the entries")
    eq(n, 0, "the second read gets none")
    eq(names(again), names(first), "same rows"); eq(names(first), "Command1,Cure Wounds1,Heroism1,Wrathful Smite1", "rows")
end)

-- ---- A27: Gather Your Allies (S090 Netherbrain: 31 hand clicks over 3 tries) ----------------------------------

local GYA = "Shout_END_AllyAbilities_MultipleSummon"
local WATCH, KEENE, GUR = "Target_END_AllyAbilities_WatchSummon", "Target_END_AllyAbilities_KeeneSummon",
    "Target_END_AllyAbilities_GurSummon"
-- S090's game data (GustavDev Spell_Shout/Spell_Target.txt), the fields the route reads; names by loca handle
local function allies_setup(where)
    G.spellFacts, G.boxData = {}, {}
    local loca = { hGYA = "Gather Your Allies", hW = "The Watch", hK = "Guildmaster Keene's Fixers", hG = "Gur Huntwardens" }
    Ext.Loca.GetTranslatedString = function(h) return loca[h] end
    stats[GYA] = { SpellType = "Shout", DisplayName = "hGYA;4", UseCosts = "",
        SpellFlags = { "IsLinkedSpellContainer", "Temporary", "Wildshape", "CallAlliesSpell" } }
    local function group(id, h, mark)
        stats[id] = { SpellType = "Target", DisplayName = h .. ";2", TargetRadius = "18", Level = 0,
            UseCosts = "ActionPoint:1", SpellContainerID = GYA, Requirements = "Combat",
            RequirementConditions = "not HasStatus('" .. mark .. "INTERDICTED') and not HasStatus('" .. mark .. "CONSUMED')",
            SpellFlags = { "HasVerbalComponent", "CannotTargetItems", "CannotTargetCharacter", "IsLinkedSpellContainer" } }
    end
    group(WATCH, "hW", "END_ALLYABILITIES_WATCHALLIES"); group(KEENE, "hK", "END_ALLYABILTIES_KEENEALLIES")
    group(GUR, "hG", "END_ALLYABILITIES_GURALLIES")
    world[P1].comps.SpellBook = { Spells = { { Id = { Prototype = GYA } } } }
    local kids = {}
    for _, id in ipairs({ WATCH, KEENE, GUR }) do
        if where == "book" then table.insert(world[P1].comps.SpellBook.Spells, { Id = { Prototype = id } })
        else kids[#kids + 1] = { ContainerSpell = GYA, SpellId = { OriginatorPrototype = id } } end
    end
    world[P1].comps.SpellContainer = { Spells = kids }
    combat_setup(9)
    return function() Ext.Loca.GetTranslatedString = function() return nil end; G.spellFacts, G.boxData = {}, {} end
end

-- S093 fairness leak: no spell or status id in what the agent reads (the game shows the group names)
local function no_ids(text)
    for _, pat in ipairs({ "Target_", "Shout_", "END_ALLY", "END_ALLYABILTIES" }) do
        assert(not text:find(pat, 1, true), "internal id in: " .. text)
    end
end

test("A27 Gather Your Allies alone is refused before any cost, listing the groups by name (S090 x2 empty casts)", function()
    for _, where in ipairs({ "book", "container" }) do
        local undo = allies_setup(where)
        osiv[key("HasActiveStatus", P1, "END_ALLYABILITIES_WATCHALLIESINTERDICTED")] = 1 -- Shadowheart called The Watch
        local j = job(op("cast", { char = P1, spell = GYA }))
        undo()
        eq(j.status, "failed", where .. " status")
        -- the usable ones first: combat_turn's quick pick (_WHICH_VARIANTS) takes Keene's, not the used Watch
        assert(j.error:find("Gather Your Allies casts nothing by itself: use_ability which=<one of Guildmaster Keene's"
            .. " Fixers, Gur Huntwardens, The Watch [already called]>", 1, true), j.error)
        assert(j.error:find("x,y,z = the ground where they appear", 1, true), j.error)
        no_ids(j.error)
        eq(#called("UseSpell"), 0, where .. " no cast")
    end
end)

test("A27 which='The Watch' casts that group at the ground point (UseSpellAtPosition, the variant's id)", function()
    local undo = allies_setup("container")
    local j = job(op("cast", { char = P1, spell = GYA, which = "the watch", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local c = called("UseSpellAtPosition")
    eq(#c, 1, "one cast"); eq(c[1][3], WATCH, "the group's own spell")
    -- part of a name, any case
    j = job(op("cast", { char = P1, spell = GYA, which = "keene's", pos = { 5, 0, 0 } }))
    eq(called("UseSpellAtPosition")[2][3], KEENE, "Keene by part of the name")
    undo()
end)

test("A27 which: an unknown or used group is refused with the list; no point = 'needs x,y,z'; not in a fight", function()
    local undo = allies_setup("book")
    local function fails(args, pat)
        local j = job(op("cast", args))
        eq(j.status, "failed", pat)
        assert(j.error:find(pat, 1, true), j.error)
        no_ids(j.error)
    end
    fails({ char = P1, spell = GYA, which = "Harpers", pos = { 5, 0, 0 } },
        "no variant 'Harpers' in Gather Your Allies: The Watch, Guildmaster Keene's Fixers, Gur Huntwardens")
    osiv[key("HasActiveStatus", P1, "END_ALLYABILITIES_GURALLIESCONSUMED")] = 1
    fails({ char = P1, spell = GYA, which = "Gur", pos = { 5, 0, 0 } },
        "Gur Huntwardens was already called: pick another of The Watch, Guildmaster Keene's Fixers, Gur Huntwardens"
        .. " [already called]")
    -- S093 09:38 (fairness leak): the Watch called and consumed; every refusal and the all-used one, names only
    osiv[key("HasActiveStatus", P1, "END_ALLYABILITIES_WATCHALLIESCONSUMED")] = 1
    fails({ char = P1, spell = GYA, which = "The Watch", pos = { 5, 0, 0 } }, "The Watch was already called")
    osiv[key("HasActiveStatus", P1, "END_ALLYABILTIES_KEENEALLIESCONSUMED")] = 1
    fails({ char = P1, spell = GYA }, "Gather Your Allies: every variant was already used: The Watch [already called]")
    osiv[key("HasActiveStatus", P1, "END_ALLYABILITIES_WATCHALLIESCONSUMED")] = nil
    fails({ char = P1, spell = GYA, which = "The Watch" }, "needs x,y,z: a ground point")
    osiv[key("IsInCombat", P1)] = nil
    fails({ char = P1, spell = GYA, which = "The Watch", pos = { 5, 0, 0 } }, "called only in a fight")
    undo()
    eq(#called("UseSpellAtPosition"), 0, "nothing cast")
end)

test("A27 the result names who arrived at the point (after the camera wait), or says nobody did", function()
    local undo = allies_setup("container")
    local LADY = "12121212-0000-0000-0000-000000000009"
    local rows = { { "e-npc", { 6, 0, 0 } } }
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({
        charactersNear = function(p, r)
            local out = {}
            for _, row in ipairs(rows) do if U.dist(row[2], p) <= r then out[#out + 1] = { row[1], row[2], 0 } end end
            return out
        end,
        entityUuid = function(e) return e == "e-npc" and NPC or e == "e-lady" and LADY or nil end,
        name = function(u) return u == LADY and "Lady Lockjaw" or "x" end,
    }, { __index = savedV })
    local j = job(op("cast", { char = P1, spell = GYA, which = "The Watch", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpell", { P1, WATCH, "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, WATCH })
    tick(20) -- 1.2 s: the camera wait, nobody yet
    eq(j.status, "running", "waits for them")
    rows[#rows + 1] = { "e-lady", { 7, 0, 1 } }
    tick(2)
    eq(j.status, "done", "status " .. tostring(j.error))
    eq(#j.result.allies, 1, "one newcomer (the NPC was already there)"); eq(j.result.allies[1].name, "Lady Lockjaw", "name")
    eq(j.result.no_allies, nil, "no warning")
    -- nobody comes: done after ALLY_CALL_WAIT_MS with the warning
    rows = { { "e-npc", { 6, 0, 0 } } }
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = GYA, which = "Keene", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpell", { P1, KEENE, "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, KEENE })
    tick(math.ceil(A.ALLY_CALL_WAIT_MS / 60) + 2)
    BG3AI.Visibility = savedV
    undo()
    eq(j.status, "done", "status"); eq(j.result.allies, nil, "nobody")
    assert(tostring(j.result.no_allies):find("nobody arrived within 9 m of the point 6 s after the cast", 1, true),
        tostring(j.result.no_allies))
end)

test("A27 a linked container with no variants (Shout_Shield_Wizard shape) still casts; forbidden_statuses parse", function()
    G.spellFacts = {}
    spell("Shout_Shield_Wizard", { SpellType = "Shout", UseCosts = "ReactionActionPoint:1", Level = 1,
        SpellFlags = { "IsLinkedSpellContainer" } })
    local j = job(op("cast", { char = P1, spell = "Shout_Shield_Wizard" }))
    eq(j.status, "running", "cast " .. tostring(j.error))
    local f = G.forbidden_statuses({ RequirementConditions = "not HasStatus('A_X') and not HasStatus( 'B_Y' )" })
    eq(#f, 2, "two"); eq(f[2], "B_Y", "second")
    eq(G.forbidden_statuses({ RequirementConditions = "not HasStatus('A') and Combat()" }), nil, "other terms: nil")
    eq(G.forbidden_statuses({}), nil, "none")
end)
