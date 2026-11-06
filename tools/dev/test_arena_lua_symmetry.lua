-- Part of test_arena_lua.lua (split for the 1000-line cap): arena.symmetry {pairs}, the mirror-team check (each pair
-- the same character in gameplay terms; the save authoring tool refuses to save on any problem). The characters'
-- components are faked with the paths the mod reads live (State.lua, GuardSpells.lua, Guard.resources).
-- test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local SLOTS = { [0] = "Helmet", [3] = "Breast", [5] = "MeleeMainHand" }
local SKILLS = { [0] = "Deception", "Intimidation", "Performance", "Persuasion", "Acrobatics", "SleightOfHand",
    "Stealth", "Arcana", "History", "Investigation", "Nature", "Religion", "Athletics", "AnimalHandling", "Insight",
    "Medicine", "Perception", "Survival" }

-- One character's components: a level-4 fighter-like profile; over = fields to change.
local function body(u, over)
    over = over or {}
    local items = {}
    for slot, tpl in pairs(over.equipment or { [3] = "ARM_Chain", [5] = "WPN_Longsword" }) do
        local iu = u .. "-item-" .. slot
        osiv[key("GetTemplate", iu)] = tpl
        items[slot] = { Item = { Uuid = { EntityUuid = iu } } }
    end
    local bag = {}
    for i, tpl in ipairs(over.inventory or { "CONS_Potion", "CONS_Potion" }) do
        local iu = u .. "-bag-" .. i
        osiv[key("GetTemplate", iu)] = tpl
        bag[i] = { Item = { Uuid = { EntityUuid = iu } } }
    end
    local spells = {}
    for i, id in ipairs(over.spells or { "Target_MainHandAttack", "Shout_SecondWind" }) do
        spells[i] = { Id = { Prototype = id }, SpellCastingAbility = (over.cast or {})[id] or "None",
            PrepareType = (over.prep or {})[id] or "AlwaysPrepared" }
    end
    local passives = {}
    for i, id in ipairs(over.passives or { "ExtraAttack", "FightingStyle_Defense" }) do
        passives[i] = { Passive = { PassiveId = id, ToggledOn = (over.toggled or {})[id] or false, Disabled = false } }
    end
    -- the main hand: a weapon item with its own Weapon component (MELEE 0x1000, FINESSE 0x4)
    local mh = u .. "-item-5"
    world[mh] = { pos = { 0, 0, 0 }, comps = { Weapon = { WeaponProperties = over.weapon_props or 0x1000,
        AttackRollBonus = {} } } }
    osiv[key("GetEquippedItem", u, "MeleeMainHand")] = mh
    local c = {
        ServerCharacter = {},
        Race = { Race = over.race or "race-human" },
        EocLevel = { Level = 4 },
        Classes = { Classes = { { ClassUUID = "class-fighter", SubClassUUID = "sub-battlemaster", Level = 4 } } },
        Stats = { Abilities = { 0, 16, 14, over.con or 15, 10, 12, 8 },
            AbilityModifiers = over.mods or { 0, 3, 2, 2, 0, 1, -1 },
            ProficiencyBonus = 2, InitiativeBonus = over.initiative or 2, SpellCastingAbility = "Intelligence" },
        DifficultyCheck = { SpellSaveDCBoost = 0, WeaponActionDC = 12 },
        Health = { Hp = 40, MaxHp = 40 },
        Resistances = { AC = 18 },
        InventoryOwner = { Inventories = {
            { InventoryData = { Type = "Equipment" }, InventoryContainer = { Items = items } },
            { InventoryData = { Type = "Inventory" }, InventoryContainer = { Items = bag } } } },
        SpellBook = { Spells = spells },
        SpellBookPrepares = { PreparedSpells = {} },
        PassiveContainer = { Passives = passives },
        StatusContainer = { Statuses = { h1 = "LONGREST_RECOVERY" } },
        ActionResources = { Resources = { ["res-action"] = { { Level = 0, Amount = 1, MaxAmount = over.action_max or 1,
            ReplenishType = { "Turn" } } } } },
        Proficiency = { Flags = over.proficiencies or { "LightArmor", "MediumArmor", "MartialWeapons" } },
        BoostsContainer = { Boosts = { { Type = "ProficiencyBonus", Boosts = {} } } },
    }
    c.Stats.Skills = {}
    for i = 1, 18 do c.Stats.Skills[i] = (over.skills or {})[i] or 0 end
    for i, prm in ipairs(over.boosts or { "Skill,Athletics", "SavingThrow,Strength" }) do
        c.BoostsContainer.Boosts[1].Boosts[i] = { BoostInfo = { Params = { Boost = "ProficiencyBonus", Params = prm,
            Params2 = "" } } }
    end
    for k, v in pairs(over.comps or {}) do c[k] = v or nil end
    world[u] = { pos = { 0, 0, 0 }, comps = c }
    osiv[key("GetTemplate", u)] = over.template or "Hireling_Fighter_tpl"
end

local savedEnums
local function mirror(overB1, overB2)
    reset()
    savedEnums = Ext.Enums
    Ext.Enums = { ItemSlot = SLOTS, SkillId = SKILLS }
    body(A1); body(B1, overB1); body(A2); body(B2, overB2)
end

local function check(pairs_, side)
    local r = rpc("arena.symmetry", "{pairs=" .. pairs_ .. "}", side)
    Ext.Enums = savedEnums
    return r
end

local P = string.format("{{%q,%q},{%q,%q}}", A1, B1, A2, B2)

local function problems(r) return table.concat(r.result.problems, "; ") end

-- Review 3 F7-F9: what ids and current amounts don't show.
test("arena.symmetry F7: a spell cast with another ability, or always prepared vs to prepare, differs (DC, attack)",
    function()
    mirror({ cast = { Shout_SecondWind = "Wisdom" } }, { prep = { Shout_SecondWind = "RequiresPreparation" } })
    local r = check(P)
    eq(problems(r), "pair 1 (Ana, Bo): spellcast differs; pair 2 (Abe, Bea): spellcast differs", "problems")
    local a = r.result.members[A1].spellcast
    eq(enc(a), enc({ "Shout_SecondWind|None|AlwaysPrepared|-|-", "Target_MainHandAttack|None|AlwaysPrepared|-|-" }),
        "A1 as read")
    eq(enc(r.result.members[B1].spellcast[1]), enc("Shout_SecondWind|Wisdom|AlwaysPrepared|11|3"),
        "B1: DC 8 + 2 + Wis 1, attack 2 + 1")
end)

test("arena.symmetry F7: initiative bonus and a finesse main hand (the better of Str/Dex) differ", function()
    mirror({ initiative = 4 }, { weapon_props = 0x1004, mods = { 0, 3, 4, 2, 0, 1, -1 } })
    local r = check(P)
    eq(problems(r), "pair 1 (Ana, Bo): derived differs; pair 2 (Abe, Bea): derived differs", "problems")
    local d = r.result.members[A1].derived
    eq(enc({ d.proficiency_bonus, d.initiative_bonus, d.melee_main_hand, d.ranged_main_hand, d.movement,
        d.spell_casting_ability }), enc({ 2, 2, "4096|Strength|5|0", "none", "none", "Intelligence" }), "A1 as read")
    eq(r.result.members[B2].derived.melee_main_hand, "4100|Dexterity|6|0", "B2's finesse blade: Dex 4 over Str 3")
end)

test("arena.symmetry F8-F9: a resource's maximum with the same current amount, a passive's toggle state differ",
    function()
    mirror({ action_max = 2 }, { toggled = { FightingStyle_Defense = true } })
    local r = check(P)
    eq(problems(r), "pair 1 (Ana, Bo): resource_max differs; pair 2 (Abe, Bea): passive_state differs", "problems")
    eq(enc(r.result.members[A1].resource_max), enc({ "res-action|0|1|Turn" }), "A1's resource as read")
end)

test("arena.symmetry F7-F9: a field these read that doesn't read is a problem (no DifficultyCheck, no MaxAmount)",
    function()
    mirror({ comps = { DifficultyCheck = false } }, nil)
    world[A2].comps.ActionResources.Resources["res-action"][1].MaxAmount = nil
    local r = check(P)
    eq(problems(r), "Abe: resource_max doesn't read; Bo: derived doesn't read; Bo: spellcast doesn't read",
        "problems")
end)

test("arena.symmetry: two identical pairs: ok, no problems; templates recorded, not compared", function()
    mirror(nil, { template = "Hireling_Fighter_other_tpl" })
    local r = check(P)
    eq(r.result.ok, true, "ok: " .. enc(r))
    eq(#r.result.problems, 0, "no problems")
    eq(#r.result.pairs, 2, "two pairs")
    eq(r.result.pairs[1].a, A1, "pair 1 a")
    eq(r.result.pairs[1].b, B1, "pair 1 b")
    eq(r.result.members[B2].template, "Hireling_Fighter_other_tpl", "template recorded")
    eq(enc(r.result.members[A1].equipment), enc({ Breast = "ARM_Chain", MeleeMainHand = "WPN_Longsword" }), "equipment")
    eq(enc(r.result.members[A1].abilities), enc({ 16, 14, 15, 10, 12, 8 }), "abilities STR..CHA")
end)

test("arena.symmetry: any gameplay difference is a problem, with both values", function()
    mirror({ con = 14, race = "race-elf", equipment = { [3] = "ARM_Chain", [5] = "WPN_Greatsword" } },
        { spells = { "Target_MainHandAttack" }, passives = { "ExtraAttack" }, inventory = { "CONS_Potion" } })
    local r = check(P)
    eq(r.result.ok, false, "not ok")
    local d1 = {}
    for _, d in ipairs(r.result.pairs[1].diffs) do d1[#d1 + 1] = d.field end
    eq(table.concat(d1, ","), "race,abilities,equipment", "pair 1 fields")
    eq(enc(r.result.pairs[1].diffs[2].b), enc({ 16, 14, 14, 10, 12, 8 }), "B1's abilities")
    local d2 = {}
    for _, d in ipairs(r.result.pairs[2].diffs) do d2[#d2 + 1] = d.field end
    eq(table.concat(d2, ","), "inventory,spells,passives,spellcast,passive_state", "pair 2 fields")
    assert(problems(r):find("pair 1 (Ana, Bo): race differs", 1, true), problems(r))
end)

-- Character creation and level up write the twins' choices into proficiencies, skills and boosts (D289 route 1).
test("arena.symmetry: proficiencies, skills and boosts are compared (a skill choice, expertise, a proficiency)",
    function()
    local sk = {}
    sk[13] = 2 -- Athletics +2 for B1 only
    mirror({ skills = sk, boosts = { "Skill,Acrobatics", "SavingThrow,Strength" } },
        { proficiencies = { "LightArmor", "MediumArmor", "MartialWeapons", "Shields" } })
    local r = check(P)
    eq(r.result.ok, false, "not ok")
    local d1, d2 = {}, {}
    for _, d in ipairs(r.result.pairs[1].diffs) do d1[#d1 + 1] = d.field end
    for _, d in ipairs(r.result.pairs[2].diffs) do d2[#d2 + 1] = d.field end
    eq(table.concat(d1, ","), "skills,boosts", "pair 1 fields")
    eq(table.concat(d2, ","), "proficiencies", "pair 2 fields")
    eq(r.result.members[B1].skills.Athletics, 2, "skills by name")
    eq(enc(r.result.members[A1].boosts), enc({ "ProficiencyBonus|ProficiencyBonus|SavingThrow,Strength|",
        "ProficiencyBonus|ProficiencyBonus|Skill,Athletics|" }), "boosts as type|boost|params|params2, sorted")
    mirror({ comps = { BoostsContainer = false } })
    assert(problems(check(P)):find("Bo: boosts doesn't read", 1, true), "an unread BoostsContainer")
end)

test("arena.symmetry: a field that doesn't read is a problem; two members with one display name too", function()
    mirror({ comps = { Resistances = false } })
    NAMES[B2] = "Abe"
    local r = check(P)
    NAMES[B2] = "Bea"
    eq(r.result.ok, false, "not ok")
    assert(problems(r):find("Bo: ac doesn't read", 1, true), problems(r))
    assert(problems(r):find("Abe: the display name of two members", 1, true), problems(r))
    eq(#r.result.pairs[1].diffs, 0, "an unread field is not a diff")
end)

test("arena.symmetry: no StatusContainer reads as no statuses; a container that doesn't read is a problem",
    function()
    mirror({ comps = { StatusContainer = false } }, { comps = { StatusContainer = false } })
    world[A1].comps.StatusContainer, world[A2].comps.StatusContainer = nil, { Statuses = {} }
    local r = check(P)
    eq(r.result.ok, true, "no statuses on either side of both pairs: " .. enc(r))
    eq(enc(r.result.members[B1].statuses), enc({}), "B1: []")
    mirror({ comps = { StatusContainer = false } }, { comps = { StatusContainer = {} } })
    r = check(P)
    eq(r.result.ok, false, "not ok")
    eq(r.result.pairs[1].diffs[1].field, "statuses", "pair 1: A1's status vs B1's none")
    assert(problems(r):find("Bea: statuses doesn't read", 1, true), problems(r))
end)

test("arena.symmetry refusals: no pairs, a bad pair, an unknown character, one member twice, a side, switch off",
    function()
    mirror()
    assert(enc(check("{}")):find("pairs must list", 1, true), "no pairs")
    mirror()
    assert(enc(check(string.format("{{%q}}", A1))):find("pair 1 must be [uuid, uuid]", 1, true), "bad pair")
    mirror()
    assert(enc(check(string.format("{{%q,%q}}", A1, "dddddddd-0000-0000-0000-000000000000")))
        :find("no character", 1, true), "unknown")
    mirror()
    assert(enc(check(string.format("{{%q,%q},{%q,%q}}", A1, B1, B1, B2))):find("Bo is in two pairs", 1, true),
        "twice")
    mirror()
    assert(enc(check(P, "A")):find("arena ops are the harness's", 1, true), "a side")
    mirror()
    env.BG3AI_ARENA = nil
    assert(enc(check(P)):find("BG3AI_ARENA=1", 1, true), "switch off")
end)

-- Audit S3-8: the start-of-fight check compared max_hp only. A twin hit, fallen or given temp HP between the kit and
-- the fight start passed it. Current and temp HP are compared as the fight starts ({start = true}), not at the kit
-- check (the kit's twins are not restored yet).
test("arena.symmetry S3-8: as the fight starts the current and temp HP are compared too", function()
    mirror()
    world[B1].comps.Health = { Hp = 31, MaxHp = 40, TemporaryHp = 0 }
    world[A1].comps.Health.TemporaryHp, world[A2].comps.Health.TemporaryHp = 0, 0
    world[B2].comps.Health.TemporaryHp = 5
    local pairs_ = { { A1, B1 }, { A2, B2 } }
    local kit = Ar.Symmetry.check(pairs_)
    local start = Ar.Symmetry.check(pairs_, { start = true })
    Ext.Enums = savedEnums
    eq(kit.ok, true, "the kit check: max_hp only: " .. table.concat(kit.problems, "; "))
    eq(start.ok, false, "as the fight starts")
    local all = table.concat(start.problems, "; ")
    assert(all:find("pair 1 (Ana, Bo): hp differs", 1, true), all)
    assert(all:find("temp_hp differs", 1, true), all)
    mirror() -- the fake's Health has no TemporaryHp: a field that doesn't read is a problem, as the fight starts only
    local r = Ar.Symmetry.check({ { A1, B1 } }, { start = true })
    Ext.Enums = savedEnums
    assert(table.concat(r.problems, "; "):find("temp_hp doesn't read", 1, true), table.concat(r.problems, "; "))
end)

-- A twin's profile against its build's reference (website ops/logs/arena-4v4-builds.md G7).
local function refProfile(over)
    local p = {}
    for _, f in ipairs(Ar.Symmetry.FIELDS) do p[f] = {} end
    p.level, p.race, p.max_hp = 5, "elf", 38
    p.passives = { "Darkvision", "DiscipleOfLife" }
    p.inventory = { "potion", "potion" }
    p.skills = { Medicine = 7 }
    for k, v in pairs(over or {}) do p[k] = v end
    return p
end

test("reference: same profile ok; name, template, HP and uuids aren't compared", function()
    local d = Ar.Symmetry.reference_diff(refProfile({ name = "Red Ash", template = "a", hp = 1 }),
        { profile = refProfile({ name = "Lia", template = "b", hp = 38 }) })
    eq(d.ok, true, Ar.Symmetry.diff_text(d))
    eq(#d.diffs + #d.accepted + #d.unread, 0, "nothing listed")
end)

test("reference: a diff names the entries only one side has, in lists, repeats, maps and scalars", function()
    local d = Ar.Symmetry.reference_diff(
        refProfile({ passives = { "Darkvision", "Extra" }, inventory = { "potion" }, skills = { Medicine = 6 },
            max_hp = 30 }),
        { profile = refProfile() })
    eq(d.ok, false, "not ok")
    local by = {}
    for _, x in ipairs(d.diffs) do by[x.field] = x end
    eq(enc(by.passives), enc({ field = "passives", only_reference = { "DiscipleOfLife" }, only_twin = { "Extra" } }),
        "list")
    eq(enc(by.inventory), enc({ field = "inventory", only_reference = { "potion" }, only_twin = {} }), "a repeat")
    eq(enc(by.skills.only_twin), enc({ "Medicine=6" }), "map")
    eq(enc(by.max_hp.only_reference), enc({ "38" }), "scalar")
    assert(Ar.Symmetry.diff_text(d):find("passives: only the twin", 1, true), Ar.Symmetry.diff_text(d))
end)

test("reference: an accepted entry passes and is listed; any other entry in that field still fails", function()
    local accepted = { { field = "classes", only_twin = { "c/null/5" }, only_reference = { "c/life/5" }, why = "P2" } }
    local d = Ar.Symmetry.reference_diff(refProfile({ classes = { "c/null/5" } }),
        { profile = refProfile({ classes = { "c/life/5" } }), accepted = accepted })
    eq(d.ok, true, Ar.Symmetry.diff_text(d))
    eq(enc(d.accepted[1].only_twin), enc({ "c/null/5" }), "listed")
    d = Ar.Symmetry.reference_diff(refProfile({ classes = { "c/null/4" } }),
        { profile = refProfile({ classes = { "c/life/5" } }), accepted = accepted })
    eq(d.ok, false, "a level that differs too isn't what was accepted")
    eq(enc(d.diffs[1].only_twin), enc({ "c/null/4" }), "named")
end)

test("reference: abilities are compared by position: a Strength and a Dexterity swapped is a diff", function()
    local want = { abilities = { 17, 13, 15, 10, 12, 8 } }
    for _, f in ipairs(Ar.Symmetry.FIELDS) do want[f] = want[f] or {} end
    local twin_ = {}
    for k, v in pairs(want) do twin_[k] = v end
    twin_.abilities = { 13, 17, 15, 10, 12, 8 }
    local d = Ar.Symmetry.reference_diff(twin_, { profile = want })
    eq(d.ok, false, "not ok")
    eq(enc(d.diffs), enc({ { field = "abilities", only_twin = { "1=13", "2=17" },
        only_reference = { "1=17", "2=13" } } }), "each score at its place")
    eq(Ar.Symmetry.reference_diff(want, { profile = want }).ok, true, "the same scores pass")
end)

-- The profile is a character's whole sheet: a side reading one would read the other team's.
test("arena.profile and arena.reference_diff: refused with a side and without the switch", function()
    mirror()
    for _, op in ipairs({ "arena.profile", "arena.reference_diff" }) do
        local args = string.format("{uuid=%q,reference={profile={}}}", A1)
        assert(enc(rpc(op, args, "A")):find("arena ops are the harness's", 1, true), op .. ": a side")
        assert(enc(rpc(op, args, "B")):find("arena ops are the harness's", 1, true), op .. ": the other side")
        env.BG3AI_ARENA = nil
        assert(enc(rpc(op, args)):find("BG3AI_ARENA=1", 1, true), op .. ": switch off")
        env.BG3AI_ARENA = "1"
    end
end)

test("reference: a field that doesn't read on either side isn't ok; no profile is refused", function()
    local t = refProfile()
    t.spells = nil
    local r = refProfile()
    r.boosts = nil
    local d = Ar.Symmetry.reference_diff(t, { profile = r })
    eq(d.ok, false, "not ok")
    eq(enc(d.unread), enc({ "spells", "boosts" }), "both named, in FIELDS order")
    eq(pcall(Ar.Symmetry.reference_diff, t, {}), false, "no profile")
end)
