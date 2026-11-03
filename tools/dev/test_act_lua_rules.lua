-- Part of test_act_lua.lua (split for the 1000-line cap): REVIEW-5 F1/F3, a spell's own rules the harness cast skips
-- (IgnoreCastChecks / IgnoreTargetChecks stay: window 6, dropping them refused every cast). Server/GuardRules.lua
-- reads Requirements, RequirementConditions and TargetConditions and refuses what a greyed hotbar or the cursor
-- refuses; a term it can't read is logged, never refused. test_act_lua.lua runs it after its stubs and helpers.

local function rules(spell_name, st, target)
    spell(spell_name, st)
    return function() G.check_rules(P1, spell_name, Ext.Stats.Get(spell_name), target) end
end
local function passes(fn)
    local ok, err = pcall(fn)
    if not ok then error("refused: " .. tostring(err.reason or err), 2) end
end

test("rules: Requirements Combat / !Combat (Rage, Action Surge out of combat; Arcane Recovery in a fight)", function()
    refused(rules("Shout_Rage", { SpellType = "Shout", Requirements = "Combat" }), "can only be used in combat")
    local j = job(op("cast", { char = P1, spell = "Shout_Rage" }))
    eq(j.status, "failed", "refused through act.cast")
    assert(j.error:find("can only be used in combat", 1, true), j.error)
    eq(#called("UseSpell"), 0, "no order")
    G.RULES_OFF = true -- the probe's switch: the engine's own verdict is measured
    passes(rules("Shout_Rage", { SpellType = "Shout", Requirements = "Combat" }))
    G.RULES_OFF = false
    combat_setup(9)
    passes(rules("Shout_Rage", { SpellType = "Shout", Requirements = "Combat" }))
    refused(rules("Shout_ArcaneRecovery", { SpellType = "Shout", Requirements = "!Combat " }),
        "can't be used in combat")
    -- bg3se's list form
    refused(rules("Shout_X", { SpellType = "Shout", Requirements = { { Requirement = "Combat", Not = true } } }),
        "can't be used in combat")
end)

test("rules: Requirements !Immobile refuse Jump while Ensnared (window 6: it went through live)", function()
    stats["ENSNARED"] = { StatusType = "BOOST", StatusGroups = { "SG_Condition", "SG_Restrained" },
        Boosts = "ActionResourceBlock(Movement)" }
    G._disabling = {}
    local savedV = BG3AI.Visibility
    BG3AI.Visibility = setmetatable({ statuses = function() return { { id = "ENSNARED", name = "Ensnared" } } end },
        { __index = savedV })
    local ok, err = pcall(rules("Projectile_Jump", { SpellType = "Projectile", Requirements = "!Immobile" }))
    BG3AI.Visibility = savedV
    G._disabling = {}
    eq(ok, false, "refused"); eq(err.reason, "Projectile_Jump can't be used while the caster can't move (Ensnared)")
    passes(rules("Projectile_Jump", { SpellType = "Projectile", Requirements = "!Immobile" }))
end)

test("rules: RequirementConditions on the caster (Rage twice; a Rage-only action)", function()
    osiv[key("HasActiveStatus", P1, "RAGE")] = 1
    osiv[key("HasActiveStatus", P1, "CALM_EMOTIONS")] = 0
    refused(rules("Shout_Rage", { SpellType = "Shout",
        RequirementConditions = "not HasStatus('RAGE') and not HasStatus('CALM_EMOTIONS')" }),
        "Shout_Rage can't be used now: the caster has the Rage status")
    osiv[key("HasActiveStatus", P1, "RAGE")] = 0
    passes(rules("Shout_Rage", { SpellType = "Shout",
        RequirementConditions = "not HasStatus('RAGE') and not HasStatus('CALM_EMOTIONS')" }))
    refused(rules("Target_Frenzy", { SpellType = "Target", RequirementConditions = "HasStatus('RAGE')" }, NPC),
        "the caster doesn't have the Rage status")
end)

test("rules: TargetConditions (Hold Person on an undead, a heal on a dead target, an item)", function()
    Ext.StaticData = { GetAll = function() return { "t-hum", "t-und" } end,
        Get = function(id) return { Name = ({ ["t-hum"] = "HUMANOID", ["t-und"] = "UNDEAD" })[id] } end }
    G._tag_ids = nil
    local hold = { SpellType = "Target",
        TargetConditions = "Character() and not Self() and Tagged('HUMANOID') and not Tagged('UNDEAD')" }
    osiv[key("IsCharacter", NPC)] = 1
    osiv[key("IsTagged", NPC, "t-und")] = 1
    refused(rules("Target_HoldPerson", hold, NPC), "Target_HoldPerson can't target that: the target is undead")
    -- a humanoid: HUMANOID read true, UNDEAD false -> allowed
    osiv[key("IsTagged", NPC, "t-und")] = 0
    osiv[key("IsTagged", NPC, "t-hum")] = 1
    passes(rules("Target_HoldPerson", hold, NPC))
    -- tags unreadable (no answer): not refused
    osiv[key("IsTagged", NPC, "t-hum")] = nil
    osiv[key("IsTagged", NPC, "t-und")] = nil
    passes(rules("Target_HoldPerson", hold, NPC))
    osiv[key("IsDead", NPC)] = 1
    refused(rules("Target_HealingWord", { SpellType = "Target", TargetConditions = "not Dead() and Character()" }, NPC),
        "the target is dead")
    osiv[key("IsCharacter", NPC)] = 0
    passes(rules("Target_X", { SpellType = "Target", TargetConditions = "Character() or Item()" }, NPC)) -- Item unread
    osiv[key("IsItem", NPC)] = 0
    refused(rules("Target_X", { SpellType = "Target", TargetConditions = "Character() or Item()" }, NPC),
        "it is not a character and it is not an item")
    G._tag_ids = nil
end)

test("rules: Shove weight (CanShoveWeight: Strength x 12 kg >= the target's weight)", function()
    local shove = { SpellType = "Target", TargetConditions = "not Self() and CanShoveWeight()" }
    osiv[key("GetAbility", P1, "Strength")] = 10
    world[NPC].comps.Data = { Weight = 200000 } -- 200 kg > 120
    refused(rules("Target_Shove", shove, NPC), "Target_Shove can't target that: it is too heavy to shove")
    world[NPC].comps.Data = { Weight = 80000 }
    passes(rules("Target_Shove", shove, NPC))
    osiv[key("HasActiveStatus", P1, "ENLARGE")] = 1 -- x 28.2 = 282 kg
    world[NPC].comps.Data = { Weight = 200000 }
    passes(rules("Target_Shove", shove, NPC))
end)

test("rules: an unread term is logged once, never refused; a false known term still refuses", function()
    local warned = {}
    local pw = Ext.Utils.PrintWarning
    Ext.Utils.PrintWarning = function(m) warned[#warned + 1] = m end
    G.rules_unread = {}
    local st = { SpellType = "Target", TargetConditions = "Player(context.Source) and MysteryTerm() or Dead()" }
    osiv[key("IsDead", NPC)] = 0
    passes(rules("Target_Odd", st, NPC))
    passes(rules("Target_Odd", st, NPC))
    eq(#warned, 2, "Player and MysteryTerm, once each")
    assert(warned[1]:find("Target_Odd TargetConditions: term Player not read", 1, true), warned[1])
    passes(rules("Target_Ops", { SpellType = "Target", TargetConditions = "HasHPPercentageLessThan(50) >= 1" }, NPC))
    -- unknown AND false = false
    osiv[key("IsCharacter", NPC)] = 0
    refused(rules("Target_Y", { SpellType = "Target", TargetConditions = "MysteryTerm() and Character()" }, NPC),
        "it is not a character")
    Ext.Utils.PrintWarning = pw
    -- a two-step spell checks its first pick against OriginTargetConditions
    osiv[key("IsCharacter", NPC)] = 1
    passes(rules("Teleportation_Revivify", { SpellType = "Teleportation", OriginTargetConditions = "Character()",
        TargetConditions = "not Character()" }, NPC))
end)

test("rules: a Shout, Zone or area spell's TargetConditions filter who it reaches, not the pick (launch-v1 replay)",
    function()
    Ext.StaticData = { GetAll = function() return { "t-und" } end, Get = function() return { Name = "UNDEAD" } end }
    G._tag_ids = nil
    osiv[key("IsTagged", P1, "t-und")] = 0
    osiv[key("IsCharacter", P1)] = 1
    osiv[key("HasActiveStatus", P1, "LOW_GHOST_POSSESSED")] = 0
    -- Turn Undead: the harness passes the caster as a Shout's target; the caster is not undead
    passes(rules("Shout_TurnUndead", { SpellType = "Shout", AreaRadius = 9,
        TargetConditions = "Character() and Tagged('UNDEAD') or HasStatus('LOW_GHOST_POSSESSED')" }, P1))
    -- Aid (Self()) with an ally named as the target
    passes(rules("Shout_MAG_Aid_Self", { SpellType = "Shout", TargetConditions = "Self()" }, NPC))
    osiv[key("IsDead", NPC)] = 1
    passes(rules("Zone_Thunderwave", { SpellType = "Zone", TargetConditions = "not Dead()" }, NPC))
    passes(rules("Target_Shatter", { SpellType = "Target", AreaRadius = 3, TargetConditions = "not Dead()" }, NPC))
    passes(rules("Projectile_Fireball", { SpellType = "Projectile", ExplodeRadius = 4,
        TargetConditions = "not Dead()" }, NPC))
    -- the Requirements still hold for a Shout; a single-target spell still checks its pick
    combat_setup(9)
    refused(rules("Shout_PrayerOfHealing", { SpellType = "Shout", AreaRadius = 9, Requirements = "!Combat" }, P1),
        "can't be used in combat")
    refused(rules("Target_MainHandAttack", { SpellType = "Target", TargetConditions = "not Dead()" }, NPC),
        "the target is dead")
end)

test("rules: Hide is refused while FLANKED, through act.cast and sneak; the guard's rules are on by default (w8)",
    function()
    -- window 8: with the guard's rules off the engine let a flanked Hide through (both check flags dropped): the
    -- engine doesn't re-check RequirementConditions on a UseSpell cast, the guard is what enforces them
    eq(G.RULES_OFF, false, "on by default")
    local hide = { SpellType = "Shout", UseCosts = "BonusActionPoint:1", Level = 0,
        RequirementConditions = "not HasStatus('FLANKED')" }
    spell("Shout_Hide", hide)
    osiv[key("HasActiveStatus", P1, "FLANKED")] = 1
    local j = job(op("cast", { char = P1, spell = "Shout_Hide" }))
    eq(j.status, "failed", "act.cast")
    eq(j.error, "Shout_Hide can't be used now: the caster has the Flanked status", "the caster, not 'the target'")
    j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "failed", "sneak")
    eq(j.error, "Shout_Hide can't be used now: the caster has the Flanked status", "sneak text")
    eq(#called("UseSpell"), 0, "no order")
    osiv[key("HasActiveStatus", P1, "FLANKED")] = 0
    spell("Shout_Hide", hide)
    j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "running", "not flanked: hides " .. tostring(j.error))
end)

test("rules: the helper terms read from the game (Sleep HP, Help, Knock, Mobile Shooting, proficiency, Grease)",
    function()
    local function tr(spell_name, cond, target)
        return rules(spell_name, { SpellType = "Target", TargetConditions = cond }, target)
    end
    -- Sleep: not HasHPMoreThan(24) (thoth: entity.HP > value, the target by default)
    osiv[key("GetHitpoints", NPC)] = 30
    refused(tr("Target_Sleep", "not HasHPMoreThan(24)", NPC), "can't target that: the target has more than 24 HP")
    osiv[key("GetHitpoints", NPC)] = 20
    passes(tr("Target_Sleep", "not HasHPMoreThan(24)", NPC))
    -- a player caster: IsCrowdControlled doesn't matter (Sleep's "not (not Player(context.Source) and ...)")
    osiv[key("IsPlayer", P1)] = 1
    passes(tr("Target_Sleep", "not (not Player(context.Source) and IsCrowdControlled(context.Target))", NPC))
    -- Help: HasHelpableCondition (the khn status list)
    stats["PRONE"] = { StatusGroups = { "SG_Prone" } }
    world[NPC].comps.ServerCharacter = { StatusManager = { Statuses = { { StatusId = "BLESS" } } } }
    for _, id in ipairs(G.HELPABLE) do
        if id:sub(1, 3) ~= "SG_" then osiv[key("HasActiveStatus", NPC, id)] = 0 end
    end
    eq(G.rule_has_status(NPC, "SG_Prone"), false, "a status group reads as off (was always unknown)")
    refused(tr("Target_Help", "HasHelpableCondition()", NPC), "it has nothing to help with")
    osiv[key("HasActiveStatus", NPC, "PRONE")] = 1
    passes(tr("Target_Help", "HasHelpableCondition()", NPC))
    osiv[key("HasActiveStatus", NPC, "PRONE")] = 0
    world[NPC].comps.ServerCharacter = { StatusManager = { Statuses = { { StatusId = "PRONE" } } } }
    eq(G.rule_has_status(NPC, "SG_Prone"), true, "in the group")
    world[NPC].comps.ServerCharacter = { StatusManager = { Statuses = {} } }
    eq(G.rule_has_status(NPC, "SG_Prone"), false, "no status at all: in no group")
    -- Knock: Locked()
    osiv[key("IsLocked", NPC)] = 0
    refused(tr("Target_Knock", "Locked()", NPC), "it is not locked")
    osiv[key("IsLocked", NPC)] = 1
    passes(tr("Target_Knock", "Locked()", NPC))
    -- HasPassive, IsImmuneToStatus (listed = immune; not listed = unknown, never refused)
    osiv[key("HasPassive", P1, "SculptSpells")] = 1
    refused(tr("Target_X", "not HasPassive('SculptSpells', context.Source)", NPC),
        "the caster has the passive Sculptspells")
    world[NPC].comps.StatusImmunities = { PersonalStatusImmunities = { SLEEP = {} } }
    refused(tr("Target_Sleep", "not IsImmuneToStatus('SLEEP')", NPC), "the target is immune to the Sleep status")
    world[NPC].comps.StatusImmunities = { PersonalStatusImmunities = {} }
    passes(tr("Target_Sleep", "not IsImmuneToStatus('SLEEP')", NPC))
    -- Grease: "not Item();" (a trailing ';' made the whole condition unread)
    osiv[key("IsItem", NPC)] = 1
    refused(tr("Target_Grease", "not Item();", NPC), "it is an item")
    -- Mobile Shooting's RequirementConditions (the engine doesn't check them, window 8)
    local mobile = { SpellType = "Projectile", RequirementConditions = "CanUseWeaponActions() and"
        .. " IsProficientWithEquippedWeapon(context.Source, true, false) and MobileShootingCasterCondition()" }
    world[P1].comps.Proficiency = { Flags = { "Longbows", "MartialWeapons" } }
    osiv[key("GetEquippedItem", P1, "Ranged Main Weapon")] = "BOW"
    world.BOW = { pos = { 0, 0, 0 }, comps = { ProficiencyGroup = { Flags = { "Longbows" } } } }
    osiv[key("HasActiveStatus", P1, "DASH")] = 0
    osiv[key("HasActiveStatus", P1, "DISENGAGE")] = 0
    refused(rules("Projectile_MobileShooting", mobile, NPC), "the caster has neither dashed nor disengaged")
    osiv[key("HasActiveStatus", P1, "DASH")] = 1
    passes(rules("Projectile_MobileShooting", mobile, NPC))
    world.BOW.comps.ProficiencyGroup = { Flags = { "HeavyCrossbows" } }
    refused(rules("Projectile_MobileShooting", mobile, NPC), "the caster is not proficient with the weapon held")
    world.BOW.comps.ProficiencyGroup = nil -- unreadable: unknown, not refused
    passes(rules("Projectile_MobileShooting", mobile, NPC))
    world.BOW = nil
end)

test("engine refusal, window 8 m2: Sneak Attack's InvalidTarget at 1.0 m says the game's reason and the unread"
    .. " target conditions, no 'step closer' guess", function()
    -- A Cedar's Sneak Attack (Melee) at 1.0 m: refused InvalidTarget twice; the old words guessed the engine's reach
    -- ("step closer (within 1 m) and swing again"), move_to said already_there, the retry was refused again.
    combat_setup(30)
    osiv[key("GetEquippedItem", P1, "Melee Main Weapon")] = "RAPIER"
    osiv[key("GetStatString", "RAPIER")] = "WPN_Rapier"
    stats.WPN_Rapier = { WeaponRange = 150 }
    spell("Target_SneakAttack", { TargetRadius = "MeleeMainWeaponRange", Range = 0, SpellType = "Target",
        UseCosts = "ActionPoint:1", Level = 0,
        TargetConditions = "Character() and not Self() and not Dead() and ((HasAdvantage(AttackType.MeleeWeaponAttack)"
            .. " or HasAllyWithinRange('SG_Incapacitated',1.5)) and not HasDisadvantage(AttackType.MeleeWeaponAttack))"
            .. " and not Tagged('INANIMATE')" })
    local bound = { Bound = { AIBounds = { Move = { Radius = 0.25 }, Hit = { Radius = 0.2 },
        Stand = { Radius = 0.5 } } } }
    world[P1].comps.Bound, world[GOB].comps.Bound = bound, bound
    osiv[key("IsCharacter", GOB)] = 1
    osiv[key("HasLineOfSight", P1, GOB)] = 1
    osiv[key("IsDead", GOB)] = 0
    local sd = Ext.StaticData
    Ext.StaticData = { GetAll = function() return { "t-inan" } end, Get = function() return { Name = "INANIMATE" } end }
    G._tag_ids = nil
    osiv[key("IsTagged", GOB, "t-inan")] = 0
    world[GOB].pos = { 1.0, 0, 0 }
    cast_outcome = "InvalidTarget"
    local j = job(op("cast", { char = P1, spell = "Target_SneakAttack", target = GOB }))
    eq(j.status, "running", "the guard lets it go (its terms are unread) " .. tostring(j.error))
    A.dispatch("CastSpellFailed", { P1, "Target_SneakAttack" })
    eq(j.status, "failed", "status")
    local e = j.error
    assert(e:find("the game refused the cast: the target isn't a valid target for it (the game's InvalidTarget)",
        1, true), e)
    assert(e:find("needs, at this target: (the caster has advantage on a melee weapon attack (not read) or there is"
        .. " an ally of the caster that is not incapacitated within 1.5 m of the target (not read)) and the caster"
        .. " doesn't have disadvantage on a melee weapon attack (not read)", 1, true), e)
    for _, guess in ipairs({ "step closer", "engine's reach", "swing again", "path is blocked", "try another",
        "clearer spot" }) do
        assert(not e:find(guess, 1, true), guess .. ": " .. e)
    end
    Ext.StaticData, G._tag_ids = sd, nil
    -- a term read as false is said as the fact
    reset()
    spell("Target_Heal", { TargetRadius = "18", SpellType = "Target", UseCosts = "ActionPoint:1", Level = 0,
        TargetConditions = "Character() and not Dead()" })
    osiv[key("IsCharacter", GOB)], osiv[key("IsDead", GOB)] = 0, 0
    eq(G.explain_target(P1, "Target_Heal", GOB), "Target_Heal needs, at this target: it is a character (no: it is"
        .. " not a character)", "false term")
    osiv[key("IsCharacter", GOB)] = 1
    eq(G.explain_target(P1, "Target_Heal", GOB), "Target_Heal's target conditions all read as met here", "all met")
end)

test("rules: a status requirement reads as a fact with the game's status name (w8 'the caster has flanked')", function()
    local loca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return h == "h-hunt" and "Hunter's Mark" or nil end
    stats.HUNTERS_MARK = { DisplayName = "h-hunt;1" }
    osiv[key("HasActiveStatus", NPC, "HUNTERS_MARK")] = 0
    refused(rules("Target_X", { SpellType = "Target", TargetConditions = "HasStatus('HUNTERS_MARK')" }, NPC),
        "Target_X can't target that: the target doesn't have the Hunter's Mark status")
    Ext.Loca.GetTranslatedString = loca
    -- a group: what kind of status, not a verb's object
    stats.SLEEPING = { StatusGroups = { "SG_Incapacitated" } }
    world[NPC].comps.ServerCharacter = { StatusManager = { Statuses = {} } }
    local ctx = { source = P1, target = NPC, subject = NPC }
    local v, why = G.rule_eval("HasStatus('SG_Incapacitated')", ctx)
    eq(v, false, "none"); eq(why, "the target has no status of the Incapacitated kind", "group words")
    world[NPC].comps.ServerCharacter = { StatusManager = { Statuses = { { StatusId = "SLEEPING" } } } }
    v, why = G.rule_eval("not HasStatus('SG_Incapacitated')", ctx)
    eq(v, false, "one"); eq(why, "the target has a status of the Incapacitated kind", "group words, not")
    world[NPC].comps.ServerCharacter = nil
end)

test("transfer of a worn item: the fact 'it is equipped' (the bridge's give unequips on these words)", function()
    party_rows = { P1, NPC }
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("IsEquipped", GEM)] = 1
    local j = job(op("transfer", { item = GEM, to = NPC }))
    eq(j.status, "failed", "refused")
    eq(j.error, "it is equipped", "the words bridge/src/bg3ai/tools/inventory.py matches")
end)

test("interact on a living character states what interact does, no 'use talk_to' order", function()
    osiv[key("IsCharacter", NPC)] = 1
    local j = job(op("interact", { char = P1, target = NPC }))
    eq(j.status, "failed", "refused"); eq(j.error, "interact doesn't talk to characters (talk_to does)", "words")
end)

test("tool facts stay facts: loot_corpse loots a corpse, a give in a fight needs the UI tools", function()
    osiv[key("IsCharacter", NPC)], osiv[key("IsDead", NPC)] = 1, 1
    local j = job(op("interact", { char = P1, target = NPC }))
    eq(j.error, "interact doesn't loot corpses (loot_corpse does)", "corpse")
    osiv[key("IsDead", NPC)] = 0
    party_rows = { P1, NPC }
    osiv[key("GetInventoryOwner", GEM)] = P1
    osiv[key("IsInCombat", P1)] = 1
    j = job(op("transfer", { item = GEM, to = NPC }))
    -- arena: G.refuse_ui adds its own note (arena/int's "(use the UI)" without a side)
    assert(j.error:find("not available during combat through this action (", 1, true), j.error)
    osiv[key("IsInCombat", P1)] = 0
    j = job(op("pickup", { char = P1, item = GEM }))
    eq(j.error, "that item is inside a container (loot takes items out of a container)", "in a container")
end)
