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
        "Shout_Rage can't be used now: the caster has rage")
    osiv[key("HasActiveStatus", P1, "RAGE")] = 0
    passes(rules("Shout_Rage", { SpellType = "Shout",
        RequirementConditions = "not HasStatus('RAGE') and not HasStatus('CALM_EMOTIONS')" }))
    refused(rules("Target_Frenzy", { SpellType = "Target", RequirementConditions = "HasStatus('RAGE')" }, NPC),
        "the caster doesn't have rage")
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
    eq(j.error, "Shout_Hide can't be used now: the caster has flanked", "the caster, not 'the target'")
    j = job(op("sneak", { char = P1, on = true }))
    eq(j.status, "failed", "sneak"); eq(j.error, "Shout_Hide can't be used now: the caster has flanked", "sneak text")
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
    refused(tr("Target_X", "not HasPassive('SculptSpells', context.Source)", NPC), "the caster has sculptspells")
    world[NPC].comps.StatusImmunities = { PersonalStatusImmunities = { SLEEP = {} } }
    refused(tr("Target_Sleep", "not IsImmuneToStatus('SLEEP')", NPC), "the target is immune to sleep")
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
