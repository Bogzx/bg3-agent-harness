-- Part of test_act_lua.lua (split for the 1000-line cap): cast.
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

-- ---- cast ---------------------------------------------------------------------

function spell(name, st)
    stats[name] = st
    world[P1].comps.SpellBook = world[P1].comps.SpellBook or { Spells = {} }
    table.insert(world[P1].comps.SpellBook.Spells, { Id = { Prototype = name } })
end

test("cast at position: engine's 6-argument UseSpellAtPosition first (no story-form try)", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    eq(j.status, "running", "status " .. tostring(j.error))
    local c = called("UseSpellAtPosition")
    eq(#c, 1, "calls"); eq(c[1].n, 6, "arity"); eq(c[1][7], 1, "WithoutMove")
    eq(#called("UseSpellAtPosition?"), 1, "attempts")
end)

test("cast at position: falls back to 5 arguments on a build without the 6th", function()
    usp_arity = 5
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    usp_arity = 6
    eq(called("UseSpellAtPosition")[1].n, 5, "arity")
end)

test("cast: a ranged cast out of range is refused out of combat too (no walk, no Osiris approach walk)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    world[NPC].pos = { 30, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("out of range"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no walk"); eq(#called("UseSpell"), 0, "no engine cast")
end)

test("cast: in range uses UseSpell/5 with WithoutMove=1", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    local c = called("UseSpell")
    eq(#c, 1, "calls"); eq(c[1].n, 5, "arity"); eq(c[1][6], 1, "WithoutMove")
end)

test("cast: a projectile's late hit is in the result, not no_damage (acceptance 14:5x Ranged Attack at 18 m)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful" } })
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(8) -- 480 ms: the bolt is still in flight
    eq(j.status, "running", "still waiting for the bolt")
    A.dispatch("AttackedBy", { NPC, P1, P1, "Piercing", 2, "", 0 })
    tick(5)
    eq(j.status, "done", "status")
    eq(j.result.total_damage, 2, "damage"); eq(j.result.no_damage, nil, "no_damage")
    j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC }))
    A.dispatch("CastedSpell", { P1, "Projectile_Bolt" })
    tick(30) -- 1.8 s, no hit: a miss
    eq(j.status, "done", "miss status"); eq(j.result.no_damage, true, "miss")
end)

test("cast: an order the engine silently drops fails after CAST_NO_START_MS (S035 jump_to 25 s x3)", function()
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    tick(100) -- 6 s: still waiting
    eq(j.status, "running", "early")
    tick(40) -- 8.4 s, no CastSpell, no step
    eq(j.status, "failed", "status"); assert(j.error:find("never started"), j.error)
    -- the cast began (CastSpell): waits on
    j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    A.dispatch("CastSpell", { P1, "Target_Thing", "Target", "None", 0 })
    tick(140)
    eq(j.status, "running", "began")
    -- the character stepped (an engine approach / the jump took off): waits on
    j = job(op("cast", { char = P1, spell = "Target_Thing", pos = { 5, 0, 0 } }))
    world[P1].pos = { 1, 0, 0 }; tick(140)
    eq(j.status, "running", "moved")
    world[P1].pos = { 0, 0, 0 }
end)

test("cast in combat (ACT-11): a dropped order fails after CAST_NO_START_COMBAT_MS, not 8 s", function()
    combat_setup(9)
    spell("Target_Thing", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Thing", target = NPC }))
    eq(j.status, "running", "started " .. tostring(j.error))
    tick(40) -- 2.4 s
    eq(j.status, "running", "early")
    tick(12) -- 3.1 s, no CastSpell, no step
    eq(j.status, "failed", "status"); assert(j.error:find("never started it %(3 s"), j.error)
    -- a cast the engine took waits on
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Thing", target = NPC }))
    A.dispatch("CastSpell", { P1, "Target_Thing", "Target", "None", 0 })
    tick(60)
    eq(j.status, "running", "began")
end)

test("cast in combat while Stunned is refused; a dropped order while Fearful names the fear (S066/S067)", function()
    combat_setup(9)
    spell("Projectile_EB", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    spell("STUNNED", { StatusType = "INCAPACITATED" })
    spell("FEARED", { StatusType = "FEAR" })
    spell("BLEEDING", { StatusType = "BOOST" })
    local savedV = BG3AI.Visibility
    local on = {}
    BG3AI.Visibility = setmetatable({ statuses = function() return on end }, { __index = savedV })
    -- S067 #43/#54: Wyll Stunned, Eldritch Blast "never started it" x2: refused before any order now
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "STUNNED", name = "Stunned" } }
    local j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    eq(j.status, "failed", "stunned"); assert(j.error:find("cannot act while Stunned"), j.error)
    eq(#called("UseSpell"), 0, "no order")
    -- S066 #100: Karlach's Rage at a Fearful turn start: ordered (not refused), and the drop says why
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "FEARED", name = "Fearful" } }
    j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    eq(j.status, "running", "ordered " .. tostring(j.error))
    tick(52) -- 3.1 s, no CastSpell, no step
    eq(j.status, "failed", "status")
    assert(j.error:find("never started it %(3 s, no cast, no step%): the caster is Fearful %(a fear status"), j.error)
    -- no such status: the plain message
    on = { { id = "BLEEDING", name = "Bleeding" } }
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    tick(52)
    eq(j.status, "failed", "plain"); assert(not j.error:find("the caster is"), j.error)
    -- S072 21:53:33: Lethargic (HASTE_LETHARGY, INCAPACITATED in the stats) let Karlach move and attack: ordered
    spell("HASTE_LETHARGY", { StatusType = "INCAPACITATED" })
    on = { { id = "HASTE_LETHARGY", name = "Lethargic" } }
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    local n0 = #called("UseSpell")
    j = job(op("cast", { char = P1, spell = "Projectile_EB", target = NPC }))
    eq(j.status, "running", "lethargic ordered " .. tostring(j.error))
    eq(#called("UseSpell"), n0 + 1, "lethargic: ordered once")
    tick(52)
    BG3AI.Visibility = savedV
end)

test("disabling statuses carry the game's fix; the Sleeping refusal names it (S076 Gale, vis V18)", function()
    combat_setup(9)
    G._disabling = {}
    spell("Projectile_SR", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    -- the stats as the game resolves them (Shared Status_*.txt, `using` inherited)
    spell("SLEEPING_EYEBITE", { StatusType = "INCAPACITATED", RemoveEvents = { "OnDamage", "OnStatusApplied" },
        StatusGroups = { "SG_Unconscious", "SG_Condition", "SG_Sleeping", "SG_Sleeping_Magical" } })
    spell("HOLD_PERSON", { StatusType = "INCAPACITATED", RemoveEvents = { "OnTurn" },
        RemoveConditions = "SavingThrow(Ability.Wisdom, SourceSpellDC())" })
    spell("PRONE", { StatusType = "KNOCKED_DOWN", RemoveEvents = { "OnTurn" } })
    spell("FRIGHTENED", { StatusType = "BOOST", StatusGroups = { "SG_Frightened", "SG_Condition" },
        Boosts = "Disadvantage(AllAbilities);Disadvantage(AttackRoll);ActionResourceBlock(Movement)" })
    spell("HASTE_LETHARGY", { StatusType = "INCAPACITATED" })
    spell("FEARED", { StatusType = "FEAR", StatusGroups = "SG_Frightened;SG_Condition;SG_Fleeing",
        RemoveEvents = "OnTurn", RemoveConditions = "FearStatusSavingCheck()",
        Boosts = "Disadvantage(AllAbilities);Disadvantage(AttackRoll)" })
    spell("BLEEDING", { StatusType = "BOOST", StatusGroups = { "SG_Condition" } })
    spell("DOWNED", { StatusType = "DOWNED", StatusGroups = "SG_Unconscious;SG_Incapacitated" })
    local savedV = BG3AI.Visibility
    local on = {}
    BG3AI.Visibility = setmetatable({ statuses = function() return on end }, { __index = savedV })
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "DOWNED", name = "Downed" } }
    eq(G.disabling(P1), nil, "bleeding + downed (its own line)")
    on = { { id = "BLEEDING", name = "Bleeding" }, { id = "SLEEPING_EYEBITE", name = "Sleeping", turns = 7 },
        { id = "HOLD_PERSON", name = "Held" }, { id = "PRONE", name = "Prone" },
        { id = "FRIGHTENED", name = "Frightened", turns = 2 }, { id = "HASTE_LETHARGY", name = "Lethargic" } }
    local d = G.disabling(P1)
    eq(#d, 5, "five")
    eq(d[1].name, "Sleeping"); eq(d[1].turns, 7); eq(d[1].effect, "cannot act")
    eq(d[1].fix, "an adjacent ally's Help (an action) ends it; any damage ends it", "sleep fix")
    eq(d[2].effect, "cannot act"); eq(d[2].fix, "it saves again each turn", "hold fix")
    eq(d[3].effect, "stands up at its turn start for half its movement")
    eq(d[3].fix, "an adjacent ally's Help (an action) ends it", "prone fix")
    eq(d[4].effect, "cannot move"); eq(d[4].fix, nil, "fear: no fix")
    eq(d[5].name, "Lethargic")
    eq(d[5].effect, "cannot move; its turn passes once the rest of its turn group has ended (act with it first)",
        "lethargic: acts (S072) but never walks, its turn goes with the group's (S074/S089/S091)")
    -- the movement figure / walk guard: whatever "cannot act" or "cannot move"
    eq(G.move_blocked(P1), "Sleeping", "move_blocked: the first that stops a walk")
    on = { { id = "PRONE", name = "Prone" }, { id = "BLEEDING", name = "Bleeding" } }
    eq(G.move_blocked(P1), nil, "prone stands up and walks")
    on = { { id = "HASTE_LETHARGY", name = "Lethargic" } }
    eq(G.move_blocked(P1), "Lethargic", "lethargic")
    -- S065 11:51: Claude's FEARED turn: the game moved him and ended it while end_turn said "now e4's turn"
    on = { { id = "FEARED", name = "Fearful", turns = 2 } }
    d = G.disabling(P1)
    assert(tostring(d[1].effect):find("the game may move it away by itself at its turn start", 1, true), d[1].effect)
    eq(d[1].fix, "it saves again each turn", "fear: FearStatusSavingCheck at the turn start")
    eq(G.move_blocked(P1), nil, "a fearful member may still walk (S066 Karlach)")
    on = { { id = "FRIGHTENED", name = "Frightened" } }
    eq(G.move_blocked(P1), "Frightened", "movement blocked by a boost")
    -- S076 00:40:39: Gale's Scorching Ray while asleep: the refusal says how to wake her
    on = { { id = "SLEEPING_EYEBITE", name = "Sleeping" } }
    local j = job(op("cast", { char = P1, spell = "Projectile_SR", target = NPC }))
    eq(j.status, "failed", "asleep")
    eq(j.error, "cannot act while Sleeping (it takes the whole turn): end its turn; an adjacent ally's Help "
        .. "(an action) ends it; any damage ends it")
    BG3AI.Visibility = savedV
end)

test("cast in combat: a dropped order right after a hit on the caster is ordered once more (S066-S069 x6)", function()
    combat_setup(9)
    spell("Target_Swing", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    world[NPC].pos = { 1.5, 0, 0 }
    -- S069 r3 Karlach: an opportunity attack at her during the walk, then the swing never started; the bonus
    -- ordered 3 s later worked. A hit 1 s before the order: the first order is dropped, the second one starts.
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 0, "Attack", 0 })
    clock = clock + 1000
    local j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    eq(#called("UseSpell"), 1, "first order")
    tick(52) -- 3.1 s, no CastSpell, no step
    eq(j.status, "running", "ordered again " .. tostring(j.error))
    eq(#called("UseSpell"), 2, "second order"); eq(#called("PurgeOsirisQueue"), 1, "the dropped one purged first")
    A.dispatch("CastSpell", { P1, "Target_Swing", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Swing", "Target", "None", 0 })
    tick(10)
    eq(j.status, "done", "the second order cast")
    -- dropped twice: fails, naming the hit and the second order
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 2, "", 0 }) -- a Bleeding tick at the turn start
    clock = clock + 500
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "running", "second order")
    tick(52)
    eq(j.status, "failed", "dropped twice")
    assert(j.error:find("never started it %(3 s, no cast, no step%) twice: ordered again after a hit on the caster"
        .. " 0%.5 s before the first order"), j.error)
    eq(#called("UseSpell"), 4, "two orders, no third")
    -- no hit near the order (one 3 s before it): fails at 3 s as before, one order
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 2, "", 0 })
    clock = clock + 3000
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "failed", "no hit: plain failure"); assert(not j.error:find("twice"), j.error)
    eq(#called("UseSpell"), 5, "one order")
    -- a hit after the order (during the 3 s) counts too
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(10)
    A.dispatch("AttackedBy", { P1, NPC, NPC, "Slashing", 0, "Attack", 0 })
    tick(42)
    eq(j.status, "running", "hit after the order: ordered again")
    eq(#called("UseSpell"), 7, "second order")
    -- S070 Guiding Bolt: no hit, Prone ended at the turn start (shown statuses only, by name)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statusInfo = function(id)
        return ({ PRONE = { name = "Prone" } })[id] end }, { __index = savedV })
    clock = clock + 3000
    A.dispatch("StatusRemoved", { P1, "SOME_TECHNICAL_STATUS", "", 0 })
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "failed", "a technical status ending is no reason")
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    A.dispatch("StatusRemoved", { P1, "PRONE", "", 0 })
    j = job(op("cast", { char = P1, spell = "Target_Swing", target = NPC }))
    tick(52)
    eq(j.status, "running", "Prone ended: ordered again")
    tick(52)
    assert(j.error:find("twice: ordered again after the caster's Prone ending"), j.error)
    BG3AI.Visibility = savedV
end)

test("cast in combat: an enemy's reaction cast holds the no-start, then the order goes once more (A30, S094)", function()
    combat_setup(9)
    A.react_seen = nil
    spell("Target_Keene", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    spell("Target_Counterspell", { DisplayName = "Counterspell" })
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h end
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ is_visible = function() return false end }, { __index = savedV })
    local AP = world[P1].comps.ActionResources.Resources.ActionPoint[1]
    -- S094 09:55:40 Shadowheart's Keene's Fixers: Pactsplitter's Psionic Backlash came with it, nothing was cast,
    -- nothing spent; the same call later worked. The reaction plays past 3 s: no purge while it does.
    local j = job(op("cast", { char = P1, spell = "Target_Keene", pos = { 3, 0, 0 } }))
    eq(#called("UseSpellAtPosition"), 1, "first order")
    tick(8)
    A.dispatch("CastSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    tick(44) -- 3.1 s after the order, the enemy's cast not over
    eq(j.status, "running", "held while the reaction plays " .. tostring(j.error))
    eq(#called("PurgeOsirisQueue"), 0, "not purged while held")
    tick(16)
    A.dispatch("CastedSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    tick(5)
    eq(j.status, "running", "settle after the reaction")
    tick(5)
    eq(#called("UseSpellAtPosition"), 2, "ordered once more after the reaction")
    eq(#called("PurgeOsirisQueue"), 1, "the dropped order purged first")
    A.dispatch("CastSpell", { P1, "Target_Keene", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Keene", "Target", "None", 0 })
    tick(10)
    eq(j.status, "done", "the second order cast " .. tostring(j.error))
    -- dropped twice: names the reaction as a player sees it (an unseen caster unnamed)
    AP.Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Keene", pos = { 3, 0, 0 } }))
    A.dispatch("CastedSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    tick(60)
    eq(j.status, "running", "second order")
    tick(60)
    eq(j.status, "failed", "dropped twice")
    assert(j.error:find("never started it %(3 s, no cast, no step%) twice: ordered again after someone's reaction"
        .. " cast"), j.error) -- audit C23: unseen, so neither named nor called an enemy
    -- a seen caster is named; the game took the action: countered, not ordered again
    BG3AI.Visibility = setmetatable({ is_visible = function() return true end,
        name = function() return "Spellplague" end }, { __index = savedV })
    AP.Amount = 1
    local n = #called("UseSpellAtPosition")
    j = job(op("cast", { char = P1, spell = "Target_Keene", pos = { 3, 0, 0 } }))
    A.dispatch("CastSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    AP.Amount = 0
    A.dispatch("CastedSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    tick(60)
    eq(j.status, "failed", "spent: no second order")
    eq(#called("UseSpellAtPosition"), n + 1, "one order")
    assert(j.error:find("Spellplague's Counterspell %(an enemy reaction%) came just then and the game took the"
        .. " action: most likely countered"), j.error)
    -- a reaction that never ends: held at most CAST_REACT_HOLD_MS, then ordered again
    AP.Amount = 1
    n = #called("UseSpellAtPosition")
    j = job(op("cast", { char = P1, spell = "Target_Keene", pos = { 3, 0, 0 } }))
    A.dispatch("CastSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    tick(130) -- 7.8 s
    eq(#called("UseSpellAtPosition"), n + 1, "held to the cap")
    tick(5)
    eq(#called("UseSpellAtPosition"), n + 2, "ordered again at the cap")
    -- an enemy cast well before the order (not a reaction to it): the plain 3 s failure, one order
    AP.Amount = 1
    A.react_seen = nil
    A.dispatch("CastedSpell", { NPC, "Target_Counterspell", "Target", "None", 0 })
    clock = clock + 3000
    n = #called("UseSpellAtPosition")
    j = job(op("cast", { char = P1, spell = "Target_Keene", pos = { 3, 0, 0 } }))
    tick(52)
    eq(j.status, "failed", "old enemy cast: plain failure"); assert(not j.error:find("twice"), j.error)
    eq(#called("UseSpellAtPosition"), n + 1, "one order")
    -- the party's own casts are no reaction
    A.react_seen = nil
    A.dispatch("CastSpell", { P2, "Target_Counterspell", "Target", "None", 0 })
    party_rows = { P1, P2 }
    A.dispatch("CastSpell", { P2, "Target_Counterspell", "Target", "None", 0 })
    eq(A.react_seen and A.react_seen.caster, P2, "outside the party then")
    A.react_seen = nil
    A.dispatch("CastSpell", { P2, "Target_Counterspell", "Target", "None", 0 })
    eq(A.react_seen, nil, "a party member's cast")
    BG3AI.Visibility = savedV
    Ext.Loca.GetTranslatedString = loca
    A.react_seen = nil
end)

test("select: done once the character holds ClientControl; a no-op order resyncs via another member (S069)", function()
    party_rows = { P1, P2 }
    world[P2] = { pos = { 3, 0, 0 }, comps = { ClientControl = {} } }
    osiv[key("IsCharacter", P2)] = 1
    osiv[key("GetReservedUserID", P1)] = 1
    osiv[key("GetCurrentCharacter", 1)] = P1 -- the server's current character is already P1, the client controls P2
    local orders, follow = {}, nil
    Osi.MakePlayerActive = function(u)
        orders[#orders + 1] = u
        if follow then follow(u) end
    end
    -- S069 20:45:31: the old post-condition (GetCurrentCharacter) finished at once; now it waits for ClientControl
    local j = job(op("select", { char = P1 }))
    tick(5) -- 300 ms
    eq(j.status, "running", "P2 still holds ClientControl")
    eq(#orders, 1, "one order so far")
    follow = function(u) -- the game follows a real change of selection
        for _, w in ipairs({ P1, P2 }) do world[w].comps.ClientControl = (w == u) and {} or nil end
    end
    tick(6) -- 660 ms: resync, P2 first, then P1 on the next tick
    tick(2)
    eq(orders[2], P2, "via another member"); eq(orders[3], P1, "then the character")
    eq(j.status, "done", "selected " .. tostring(j.error)); eq(j.result.resynced, true, "resynced")
    -- already holding it: done on the first tick, one order
    orders = {}
    j = job(op("select", { char = P1 }))
    tick()
    eq(j.status, "done", "already selected"); eq(#orders, 1, "one order")
    -- the client never follows: failed at 2.5 s, naming who holds it (not a 3 s timeout)
    follow = nil
    world[P1].comps.ClientControl, world[P2].comps.ClientControl = nil, {}
    j = job(op("select", { char = P1 }))
    tick(43) -- 2.58 s
    eq(j.status, "failed", "kept"); assert(j.error:find("the game kept .* selected"), j.error)
    eq(j.result.holder, P2, "holder")
    -- nobody holds ClientControl (the host fallback): GetCurrentCharacter decides, as before
    world[P2].comps.ClientControl = nil
    j = job(op("select", { char = P1 }))
    tick()
    eq(j.status, "done", "host fallback")
    Osi.MakePlayerActive = nil
end)

test("select is not an action: a walk running for the member goes on (L8 helm ladder 'superseded by select')", function()
    osiv[key("GetReservedUserID", P1)] = 1
    osiv[key("GetCurrentCharacter", 1)] = P1
    Osi.MakePlayerActive = function() end
    local w = job(op("move_to", { char = P1, target = NPC }))
    eq(w.status, "running", "walk started")
    local s = job(op("select", { char = P1 }))
    tick()
    eq(s.status, "done", "selected " .. tostring(s.error))
    eq(w.status, "running", "the walk was not superseded: " .. tostring(w.error))
    eq(A.active[P1], w, "the walk stays the member's job")
    arrive(w, { 8.5, 0, 0 })
    eq(w.status, "done", "walk " .. tostring(w.error))
    Osi.MakePlayerActive = nil
end)

test("cast ui=true (ACT-8 hotbar route): 3 targets checked, no Osiris order, CastedSpell finishes, nothing charged", function()
    combat_setup(9)
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    -- without ui: still refused over 2
    local j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB } }))
    eq(j.status, "failed", "osiris >2"); assert(j.error:find("more than two"), j.error)
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    eq(j.status, "running", "ui started: " .. tostring(j.error))
    eq(#called("UseSpell"), 0, "no UseSpell: the bridge clicks the hotbar")
    tick(60) -- 3.6 s: past the combat no-start (3 s), the clicks are still going
    eq(j.status, "running", "ui no-start is longer")
    A.dispatch("CastSpell", { P1, "Target_Bless", "Target", "None", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Bless" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error))
    eq(j.result.via, "hotbar", "via"); eq(j.result.targets_n, 3, "targets_n")
    eq(j.result.charged_by_guard, nil, "the game charged it (the action point stays as the game left it)")
    eq(world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount, 1, "no extra charge")
end)

test("cast ui=true: a CastSpellFailed before the clicks are done is held (L8 DD upcast picker), after them it fails", function()
    combat_setup(9)
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    -- the picker's CastSpellFailed mid-clicks, then the real cast: done
    local j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    A.dispatch("CastSpellFailed", { P1, "Target_Bless", "Target", "None", 0 })
    tick(5)
    eq(j.status, "running", "held while clicking: " .. tostring(j.error))
    A.dispatch("CastSpell", { P1, "Target_Bless", "Target", "None", 0 })
    RPC.ops["act.ui_clicked"]({ id = j.id })
    A.dispatch("CastedSpell", { P1, "Target_Bless" })
    tick(10)
    eq(j.status, "done", "the cast went off " .. tostring(j.error))
    -- held, clicks done, nothing starts: failed CAST_UI_REFUSED_MS later with the game's reason
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    A.dispatch("CastSpellFailed", { P1, "Target_Bless", "Target", "None", 0 })
    local r = RPC.ops["act.ui_clicked"]({ id = j.id })
    assert(r.held and r.held:find("the game refused the cast"), tostring(r.held))
    tick(10) -- 0.6 s
    eq(j.status, "running", "not yet")
    tick(20) -- 1.8 s
    eq(j.status, "failed", "refused"); assert(j.error:find("the game refused the cast"), j.error)
    -- after the clicks: at once
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    RPC.ops["act.ui_clicked"]({ id = j.id })
    A.dispatch("CastSpellFailed", { P1, "Target_Bless", "Target", "None", 0 })
    eq(j.status, "failed", "refused after the clicks")
end)

test("cast ui=true cancelled: after CastSpell it says began (no Osiris redo), before it a plain cancel; no purge", function()
    combat_setup(9)
    spell("Target_Bless", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 1 })
    local j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    eq(j.status, "running", "ui started: " .. tostring(j.error))
    local purged = #called("PurgeOsirisQueue")
    RPC.ops["job.cancel"]({ id = j.id })
    eq(j.status, "interrupted", "cancelled"); eq(j.result and j.result.began, nil, "nothing began")
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Target_Bless", targets = { P1, NPC, GOB }, ui = true }))
    A.dispatch("CastSpell", { P1, "Target_Bless", "Target", "None", 0 })
    RPC.ops["job.cancel"]({ id = j.id })
    eq(j.status, "interrupted", "late cancel"); eq(j.error, "cancelled", "why")
    eq(j.result and j.result.began, true, "began reported")
    eq(#called("PurgeOsirisQueue"), purged, "a ui cast is never purged on cancel")
end)

test("cast ui=true: every later target is checked; a repeat is a 2nd dart; no position / upcast; no-start backstop", function()
    combat_setup(9)
    spell("Projectile_MagicMissile", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 1 })
    world[GOB].pos = { 30, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC, NPC, GOB }, ui = true }))
    eq(j.status, "failed", "3rd out of range"); assert(j.error:find("target 3 is out of range"), j.error)
    world[GOB].pos = { 2.4, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC, NPC, GOB }, ui = true }))
    eq(j.status, "running", "darts: " .. tostring(j.error))
    tick(260) -- 15.6 s, nothing cast: the backstop
    eq(j.status, "failed", "backstop"); assert(j.error:find("never started it %(15 s"), j.error)
    world[P1].comps.ActionResources.Resources.ActionPoint[1].Amount = 1
    j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC }, pos = { 5, 0, 0 }, ui = true }))
    eq(j.status, "failed", "pos"); assert(j.error:find("not a position"), j.error)
    j = job(op("cast", { char = P1, spell = "Projectile_MagicMissile", targets = { NPC }, upcast = 2, ui = true }))
    eq(j.status, "failed", "upcast"); assert(j.error:find("level picker"), j.error)
end)

test("cast: a once-per-rest item spell is refused after one use until a rest (S030 Spell Slot Restoration x3)", function()
    PersistentVars = nil
    spell("Shout_MAG_Restore", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        Cooldown = "OncePerRestPerItem", ContainerSpells = "Shout_MAG_Restore_1;Shout_MAG_Restore_2" })
    spell("Shout_MAG_Restore_1", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        Cooldown = "OncePerRestPerItem", SpellContainerID = "Shout_MAG_Restore" })
    spell("Shout_MAG_Restore_2", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        Cooldown = "OncePerRestPerItem", SpellContainerID = "Shout_MAG_Restore" })
    local j = job(op("cast", { char = P1, spell = "Shout_MAG_Restore_2" }))
    eq(j.status, "running", "first use " .. tostring(j.error))
    A.dispatch("CastedSpell", { P1, "Shout_MAG_Restore_2" })
    tick(10)
    j = job(op("cast", { char = P1, spell = "Shout_MAG_Restore_1" }))
    eq(j.status, "failed", "second use (another variant)"); assert(j.error:find("used up until the next long rest"), j.error)
    BG3AI.Guard.item_rest("short", P1)
    eq(job(op("cast", { char = P1, spell = "Shout_MAG_Restore_1" })).status, "failed", "a short rest keeps it used")
    BG3AI.Guard.item_rest("long")
    eq(job(op("cast", { char = P1, spell = "Shout_MAG_Restore_1" })).status, "running", "after a long rest")
    PersistentVars = nil
end)

test("cast: a melee hit still reports at 250 ms", function()
    spell("Target_Hit", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful" } })
    world[NPC].pos = { 1.5, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Hit", target = NPC }))
    A.dispatch("AttackedBy", { NPC, P1, P1, "Slashing", 5, "", 0 })
    A.dispatch("CastedSpell", { P1, "Target_Hit" })
    tick(5)
    eq(j.status, "done", "status"); eq(j.result.total_damage, 5, "damage")
    j = job(op("cast", { char = P1, spell = "Target_Hit", target = NPC }))
    A.dispatch("CastedSpell", { P1, "Target_Hit" })
    tick(5)
    eq(j.status, "done", "miss status"); eq(j.result.no_damage, true, "miss")
end)

test("cast: Extra Attack (EXTRA_ATTACK status) makes the next weapon attack cost no action (S023-S028 0 of 7)", function()
    combat_setup(9)
    world[P1].comps.ActionResources.Resources.ActionPoint = { { Level = 0, Amount = 0 } } -- the first attack spent it
    world[GOB].pos = { 1.2, 0, 0 }
    spell("Target_MainHandAttack", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsAttack" }, SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Target_Smite_Divine", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful" }, SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Target_Slash_New", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsMelee", "IsHarmful", "IsDefaultWeaponAction" },
        SpellRoll = { Default = "Attack(AttackType.MeleeWeaponAttack)" } })
    spell("Projectile_FireBolt", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = { "IsHarmful", "IsSpell" }, SpellRoll = { Default = "Attack(AttackType.RangedSpellAttack)" } })
    local j = job(op("cast", { char = P1, spell = "Target_MainHandAttack", target = GOB }))
    eq(j.status, "failed", "no status: refused"); assert(j.error:find("not enough action"), j.error)
    osiv[key("HasActiveStatus", P1, "EXTRA_ATTACK")] = 1
    for _, sp in ipairs({ "Target_MainHandAttack", "Target_Smite_Divine" }) do
        j = job(op("cast", { char = P1, spell = sp, target = GOB }))
        eq(j.status, "running", sp .. ": " .. tostring(j.error))
        A.dispatch("CastedSpell", { P1, sp })
        tick(10)
        eq(j.status, "done", sp .. " done"); eq(j.result.extra_attack, true, sp .. " extra_attack")
        eq(j.result.charged_by_guard, nil, sp .. ": no action charged")
    end
    for _, sp in ipairs({ "Target_Slash_New", "Projectile_FireBolt" }) do -- a weapon action, a spell attack
        j = job(op("cast", { char = P1, spell = sp, target = GOB }))
        eq(j.status, "failed", sp); assert(j.error:find("not enough action"), j.error)
    end
end)

test("cast out of combat, out of range: walks into range on our walker, then casts (S028 Help at 5 m)", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    eq(j.status, "running", "status " .. tostring(j.error)); eq(j.phase, "approach", "phase")
    eq(#called("UseSpell"), 0, "no cast before arriving")
    local mv = called("CharacterMoveToPosition")
    eq(#mv, 1, "one walk")
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    local us = called("UseSpell")
    eq(#us, 1, "cast after arriving"); eq(us[1][6], 1, "WithoutMove")
    A.dispatch("CastedSpell", { P1, "Target_Help" })
    tick(10)
    eq(j.status, "done", "done " .. tostring(j.error)); assert(j.result.walked_into_range, "walked_into_range")
end)

test("cast out of range in combat (or a point target): refused as before, no walk", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    combat_setup(9)
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    eq(j.status, "failed", "status"); assert(j.error:find("move closer first"), j.error)
    eq(#called("CharacterMoveToPosition"), 0, "no walk")
end)

test("cast out of combat: still out of reach after the walk -> fails with the guard's reason", function()
    spell("Target_Help", { TargetRadius = "1.5", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        SpellFlags = {} })
    world[NPC].pos = { 6, 0, 0 }
    local j = job(op("cast", { char = P1, spell = "Target_Help", target = NPC }))
    local mv = called("CharacterMoveToPosition")
    world[NPC].pos = { 30, 0, 0 } -- it walked off: the approach re-plans or fails; never a cast from afar
    arrive(j, { mv[1][3], mv[1][4], mv[1][5] })
    tick(3)
    eq(#called("UseSpell"), 0, "no cast out of reach")
end)

test("transfer: GetStackAmount's extra return values don't break tonumber (S018-S024 give/camp crash)", function()
    party_rows = { P1, NPC }
    osiv[key("GetInventoryOwner", GEM)] = P1
    local real = rawget(Osi, "GetStackAmount")
    rawset(Osi, "GetStackAmount", function() return 3, 3 end)
    local ok, r = pcall(op, "transfer", { item = GEM, to = NPC })
    rawset(Osi, "GetStackAmount", real)
    assert(ok, tostring(r))
    eq(job(r).status, "running", "status " .. tostring(job(r).error))
    eq(#called("ToInventory"), 1, "ToInventory calls")
end)

test("transfer: a stack that arrives as new items finishes the give (S031 Potion of Healing e64 timeout)", function()
    party_rows = { P1, NPC }
    local NEW = "15151515-0000-0000-0000-00000000000b"
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("GetTemplate", GEM)] = "POT_Healing_tpl"
    osiv[key("GetTemplate", NEW)] = "POT_Healing_tpl"
    local j = job(op("transfer", { item = GEM, to = NPC }))
    tick()
    eq(j.status, "running", "still with the giver")
    A.dispatch("AddedTo", { "16161616-0000-0000-0000-00000000000c", NPC, "" }) -- another kind: not ours
    eq(j.status, "running", "other template ignored")
    A.dispatch("AddedTo", { NEW, NPC, "" })
    eq(j.status, "done", "status " .. tostring(j.error))
end)

test("transfer: the whole stack merged away (original gone from the giver) is done on the tick", function()
    party_rows = { P1, NPC }
    osiv[key("GetInventoryOwner", GEM)] = P1
    local j = job(op("transfer", { item = GEM, to = NPC }))
    tick()
    eq(j.status, "running", "still held")
    osiv[key("GetInventoryOwner", GEM)] = nil -- merged into the receiver's stack and destroyed
    tick()
    eq(j.status, "done", "status " .. tostring(j.error))
end)

test("equip: a failed equip says why (S025: two-handed Sword of Justice; the Moonlantern does not block a shield)", function()
    local LANTERN, SHIELD = "12121212-0000-0000-0000-000000000008", "13131313-0000-0000-0000-000000000009"
    local SWORD = "14141414-0000-0000-0000-00000000000a"
    world[LANTERN] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "Quest_GLO_Moonlantern" } } }
    world[SWORD] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "WPN_Greatsword_Justice" } } }
    world[SHIELD] = { pos = { 0, 0, 0 }, comps = { Data = { StatsId = "ARM_Shield_Sentinel" } } }
    stats.Quest_GLO_Moonlantern = { ["Weapon Properties"] = { "Torch", "Light", "Melee", "Unstowable", "Versatile" } }
    stats.WPN_Greatsword_Justice = { ["Weapon Properties"] = { "Twohanded", "Heavy", "Melee" } }
    stats.ARM_Shield_Sentinel = { Slot = "Melee Offhand Weapon" }
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = LANTERN
    osiv[key("GetInventoryOwner", SHIELD)] = P1
    osiv[key("IsCharacter", P1)] = 1
    osiv[key("IsProficientWith", P1, SHIELD)] = 1
    eq(A.equip_why(P1, SHIELD), "cannot equip that", "live: a shield goes on beside the Moonlantern")
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = SWORD
    local why = A.equip_why(P1, SHIELD)
    assert(why:find("two%-handed") and why:find("one%-handed weapon first"), why)
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = nil
end)

test("cast: an item next to the caster needs no engine line of sight (S024 Knock on a door at 2 m)", function()
    spell("Target_Knock", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    world[DOOR].pos = { 2.0, 0, 0 }
    osiv[key("HasLineOfSight", P1, DOOR)] = 0
    local j = job(op("cast", { char = P1, spell = "Target_Knock", target = DOOR }))
    eq(j.status, "running", "status " .. tostring(j.error))
    world[DOOR].pos = { 10, 0, 0 } -- far: the engine's check still decides
    j = job(op("cast", { char = P1, spell = "Target_Knock", target = DOOR }))
    eq(j.status, "failed", "far status"); assert(j.error:find("line of sight"), j.error)
    world[GOB].pos = { 2.0, 0, 0 } -- a character this close still needs it
    osiv[key("HasLineOfSight", P1, GOB)] = 0
    j = job(op("cast", { char = P1, spell = "Target_Knock", target = GOB }))
    eq(j.status, "failed", "char status"); assert(j.error:find("line of sight"), j.error)
end)

test("cast: a door/wall past arm's length is in sight when an eye ray reaches its body (S083 e284/e703/e720)", function()
    spell("Target_Knock", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local WALL = "cdcdcdcd-0000-0000-0000-00000000000b"
    local function hit(u, p) return { Position = p, Shape = { PhysicsObject = { Entity = { Uuid = { EntityUuid = u } } } } } end
    local rays, rayfn = {}, nil
    Ext.Level.RaycastClosest = function(a, b) rays[#rays + 1] = b; return rayfn(a, b) end
    BG3AI.Visibility = { rayCalibrated = function() return true end, EYE_H = 1.6, RAY_TYPE = 5, RAY_INCLUDE = 0xFFFFFFFF,
        RAY_EXCLUDE = 0x820200, bounds = function() return { height = 3.0, radius = 1.0 } end }
    local function knock(t)
        return job(op("cast", { char = P1, spell = "Target_Knock", target = t }))
    end
    local ok, err = pcall(function()
        -- e284 Threshold of Loss: 6.3 m, its position 5.9 m up at the door's top inside the frame. The ray to the
        -- position hits the frame (another object); one lower down the door hits the door itself.
        world[DOOR].pos = { 2.2, 5.9, 0 }
        osiv[key("HasLineOfSight", P1, DOOR)] = 0
        rayfn = function(_, b)
            if b[2] < 5.9 then return hit(DOOR, { 2.0, b[2], 0 }) end
            return hit(WALL, { 1.9, 5.5, 0 })
        end
        local j = knock(DOOR)
        eq(j.status, "running", "e284 status " .. tostring(j.error))
        -- the same door behind a wall: every ray stops at the wall (fail: no line of sight)
        rays, rayfn = {}, function() return hit(WALL, { 1.0, 3.0, 0 }) end
        j = knock(DOOR)
        eq(j.status, "failed", "walled status"); assert(j.error:find("line of sight"), j.error)
        assert(#rays >= 8, "every sample tried: " .. #rays)
        -- e703 sewer Wall (hidden destructible wall) 7 m away: the ray to its position ends on the wall itself
        world[DOOR].pos = { 7.0, 0, 0 }
        rayfn = function(_, b) return hit(DOOR, { 6.6, b[2], 0 }) end
        j = knock(DOOR)
        eq(j.status, "running", "e703 status " .. tostring(j.error))
        -- e720 No Entry - No Exit, caster ~4.5 m back: nothing hit on the way to its middle (no hit = clear)
        world[DOOR].pos = { 4.5, 0, 0 }
        rayfn = function(_, b) if b[2] > 1.0 then return nil end return hit(WALL, { 4.4, 0, 0 }) end
        j = knock(DOOR)
        eq(j.status, "running", "e720 status " .. tostring(j.error))
        -- a clear ray only below the position (outside the bounds) proves nothing: refused
        world[DOOR].pos = { 2.2, 5.9, 0 }
        rayfn = function(_, b) if b[2] < 5.9 then return nil end return hit(WALL, { 1.9, 5.5, 0 }) end
        j = knock(DOOR)
        eq(j.status, "failed", "air below status"); assert(j.error:find("line of sight"), j.error)
        -- a character keeps the engine's check, whatever the rays say
        world[GOB].pos = { 6.0, 0, 0 }
        osiv[key("HasLineOfSight", P1, GOB)] = 0
        rayfn = function(_, b) return hit(GOB, b) end
        j = knock(GOB)
        eq(j.status, "failed", "char status"); assert(j.error:find("line of sight"), j.error)
        -- rays uncalibrated (the filter flags or the API wrong): fail closed
        world[DOOR].pos = { 7.0, 0, 0 }
        rayfn = function(_, b) return hit(DOOR, b) end
        BG3AI.Visibility.rayCalibrated = function() return false end
        j = knock(DOOR)
        eq(j.status, "failed", "uncalibrated status"); assert(j.error:find("line of sight"), j.error)
        -- the ray API throwing: fail closed
        BG3AI.Visibility.rayCalibrated = function() return true end
        rayfn = function() error("bad ray") end
        j = knock(DOOR)
        eq(j.status, "failed", "ray error status"); assert(j.error:find("line of sight"), j.error)
    end)
    BG3AI.Visibility, Ext.Level.RaycastClosest = nil, nil
    if not ok then error(err, 0) end
end)

test("cast: a harmful area spell says where it bursts and who is in the blast before it is cast (S085 Fireball)", function()
    -- S085 05:03:37: Gale @(-22,27,748) on the Guildhall's upper floor, Fireball at (-27,17,736) ~10 m below; it hit
    -- the ledge and burst on e606 Lady Croup @(-26,27,745), a neutral Guild ally: killed.
    spell("Projectile_Fireball", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1",
        Level = 0, ExplodeRadius = "4", SpellFlags = { "IsHarmful" } })
    local function hit(p) return { Position = p, Shape = { PhysicsObject = { Entity = { Uuid = { EntityUuid = "" } } } } } end
    local rays, rayfn = {}, nil
    Ext.Level.RaycastClosest = function(a, b) rays[#rays + 1] = { a, b }; return rayfn(a, b) end
    local seen = { [P1] = true, [NPC] = true, [GOB] = true }
    BG3AI.Visibility = { rayCalibrated = function() return true end, EYE_H = 1.6, RAY_TYPE = 5, RAY_INCLUDE = 0xFFFFFFFF,
        RAY_EXCLUDE = 0x820200, context = function() return { is_party = { [P1] = true }, observers = {} } end,
        entityUuid = function(e) return e.uuid end,
        charactersNear = function(c, r)
            local out = {}
            for _, u in ipairs({ P1, NPC, GOB }) do
                local d = U.dist(world[u].pos, c)
                if d <= r then out[#out + 1] = { { uuid = u }, world[u].pos, d } end
            end
            return out
        end,
        characterVisible = function(_, u) return seen[u] end,
        isAlly = function() return false end,
        name = function(u) return ({ [NPC] = "Lady Croup", [GOB] = "Fishbait Arno", [P1] = "Gale" })[u] or "Unknown" end }
    osiv[key("IsEnemy", P1, GOB)] = 1
    local aim = { -27, 17, 736 }
    local function fireball(extra)
        local a = { char = P1, spell = "Projectile_Fireball", pos = aim }
        for k, v in pairs(extra or {}) do a[k] = v end
        return job(op("cast", a))
    end
    local ok, err = pcall(function()
        world[P1].pos = { -22, 27, 748 }
        world[NPC].pos = { -26, 27, 745 }
        world[GOB].pos = { -27, 17, 736.5 }
        -- the eye ray to the point stops on the ledge, ~6.7 m out on the caster's floor
        rayfn = function() return hit({ -24.5, 27, 742 }) end
        local j = fireball()
        eq(j.status, "failed", "S085 status")
        assert(j.error:find("the way to that point is blocked: it would burst at %(%-24%.5,27%.0,742%.0%)"), j.error)
        assert(j.error:find(NPC:gsub("%-", "%%-") .. " Lady Croup %(neutral, 3%.4 m%)"), j.error)
        assert(j.error:find("blast %(4 m%)"), j.error); assert(j.error:find("force=true to cast anyway"), j.error)
        assert(not j.error:find(GOB, 1, true), "the hostile below is out of that blast: " .. j.error)
        eq(#called("UseSpellAtPosition"), 0, "nothing cast")
        local r = rays[#rays]
        near(r[1][2], 28.6, "eye height"); near(r[2][2], 17.5, "the ray ends 0.5 m above the point")
        -- force=true: cast as asked
        j = fireball({ force = true })
        eq(j.status, "running", "forced " .. tostring(j.error)); eq(#called("UseSpellAtPosition"), 1, "cast")
        -- the way clear (no hit, or a hit at the point itself), only the hostile in the blast: cast, no word
        rayfn = function() return nil end
        j = fireball()
        eq(j.status, "running", "clear " .. tostring(j.error))
        rayfn = function(_, b) return hit({ b[1], b[2] - 0.4, b[3] + 0.2 }) end
        j = fireball()
        eq(j.status, "running", "hit at the point " .. tostring(j.error))
        -- the way clear, but the neutral stands in the ring at the point
        world[NPC].pos = { -25, 17, 737 }
        rayfn = function() return nil end
        j = fireball()
        eq(j.status, "failed", "neutral in the ring")
        assert(j.error:find("Projectile_Fireball at that point; in its blast %(4 m%) there: " .. NPC:gsub("%-", "%%-")
            .. " Lady Croup %(neutral, 2%.2 m%) and 1 hostile"), j.error)
        -- a neutral the party does not see is not named (fair)
        seen[NPC] = false
        j = fireball()
        eq(j.status, "running", "unseen neutral " .. tostring(j.error))
        seen[NPC] = true
        -- blocked far short with nobody there: says so (the spell would be wasted)
        world[NPC].pos = { 30, 0, 0 }
        rayfn = function(a, b) -- a wall half way there
            return hit({ a[1] + (b[1] - a[1]) * 0.5, a[2] + (b[2] - a[2]) * 0.5, a[3] + (b[3] - a[3]) * 0.5 })
        end
        world[P1].pos = { -14, 27, 752 } -- nobody (the caster neither) within 4 m of the ledge point
        aim = { -20, 17, 745 }
        j = fireball()
        eq(j.status, "failed", "wasted"); assert(j.error:find("in its blast %(4 m%) there: nobody"), j.error)
        -- blocked just before the point (within the radius), only the hostile in the blast: cast
        world[P1].pos = { -22, 27, 748 }
        aim = { -27, 17, 736 }
        rayfn = function() return hit({ -26.5, 18.5, 737 }) end
        j = fireball()
        eq(j.status, "running", "short but on target " .. tostring(j.error))
        -- at a creature: who stands beside it (the target itself is not listed)
        world[NPC].pos = { -26, 17, 736.5 }
        j = job(op("cast", { char = P1, spell = "Projectile_Fireball", target = GOB }))
        eq(j.status, "failed", "at a target"); assert(j.error:find("Projectile_Fireball at that target; in its blast"), j.error)
        assert(j.error:find("Lady Croup"), j.error); assert(not j.error:find("hostile"), j.error)
        -- a harmless area spell is not checked
        spell("Projectile_Blessing", { TargetRadius = "18", SpellType = "Projectile", UseCosts = "ActionPoint:1",
            Level = 0, ExplodeRadius = "4" })
        j = job(op("cast", { char = P1, spell = "Projectile_Blessing", pos = aim }))
        eq(j.status, "running", "harmless " .. tostring(j.error))
        -- no ray API: the ring at the point is still checked, the path is not
        BG3AI.Visibility.rayCalibrated = function() return false end
        j = fireball()
        eq(j.status, "failed", "uncalibrated ring"); assert(j.error:find("at that point"), j.error)
    end)
    BG3AI.Visibility, Ext.Level.RaycastClosest = nil, nil
    if not ok then error(err, 0) end
end)

test("cast: melee reach is edge to edge (weapon 1.5 m + AIBounds.Stand radii)", function()
    combat_setup(9) -- the range refusal (out of combat the cast walks into range first)
    eq(G.radius(P1), 0.5, "fallback radius")
    world[GOB].comps.Bound = { Bound = { AIBounds = { Move = { Radius = 0.4 }, Hit = { Radius = 1.28 }, Stand = { Radius = 0.7 } } } }
    eq(G.radius(GOB), 0.7, "Stand radius")
    world[GOB].comps.Bound = { Bound = { AIBounds = { Move = { Radius = 0.25 } } } }
    eq(G.radius(GOB), 0.25, "Move fallback")
    world[GOB].comps.Bound = nil
    spell("Target_Slash", { TargetRadius = "MeleeMainWeaponRange", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0 })
    local j = job(op("cast", { char = P1, spell = "Target_Slash", target = GOB })) -- 2.4 m
    eq(j.status, "running", "status " .. tostring(j.error))
    world[GOB].pos = { 2.7, 0, 0 }
    j = job(op("cast", { char = P1, spell = "Target_Slash", target = GOB }))
    eq(j.status, "failed", "status"); assert(j.error:find("out of range"), j.error)
end)

test("cast: ranged weapon range from the equipped weapon's stats", function()
    combat_setup(9) -- the range refusal (out of combat the cast walks into range first)
    spell("Projectile_RangedAttack", { TargetRadius = "RangedMainWeaponRange", SpellType = "Projectile", UseCosts = "ActionPoint:1", Level = 0 })
    osiv[key("GetEquippedItem", P1, "Ranged Main Weapon")] = "BOW"
    osiv[key("GetStatString", "BOW")] = "WPN_HandCrossbow"
    stats.WPN_HandCrossbow = { WeaponRange = 900 }
    local j = job(op("cast", { char = P1, spell = "Projectile_RangedAttack", target = NPC })) -- 10 m > 9 m
    eq(j.status, "failed", "status"); assert(j.error:find("out of range"), j.error)
end)

test("cast: upcast checks slots at the upcast level", function()
    spell("Target_Heal", { TargetRadius = "18", SpellType = "Target", Level = 1,
        UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:1" })
    stats.Target_Heal_2 = { RootSpellID = "Target_Heal", UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:2" }
    world[P1].comps.ActionResources = { Resources = { SpellSlot = { { Level = 1, Amount = 0 }, { Level = 2, Amount = 1 } } } }
    local j = job(op("cast", { char = P1, spell = "Target_Heal", target = NPC, upcast = 2 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(called("UseSpell")[1][3], "Target_Heal_2", "variant")
end)

test("cast: an upcast with no variant in the game data casts at the base level (S030 Guiding Bolt)", function()
    spell("Projectile_Bolt", { TargetRadius = "18", SpellType = "Projectile", Level = 1,
        UseCosts = "ActionPoint:1;SpellSlotsGroup:1:1:1" })
    world[P1].comps.ActionResources = { Resources = { SpellSlot = { { Level = 1, Amount = 1 }, { Level = 2, Amount = 1 } } } }
    local j = job(op("cast", { char = P1, spell = "Projectile_Bolt", target = NPC, upcast = 2 }))
    eq(j.status, "running", "status " .. tostring(j.error))
    eq(called("UseSpell")[1][3], "Projectile_Bolt", "base spell")
end)

test("sneak: UseSpell with the full 5 arguments", function()
    spell("Shout_Hide", { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0 })
    job(op("sneak", { char = P1, on = true }))
    eq(called("UseSpell")[1].n, 5, "arity")
end)
