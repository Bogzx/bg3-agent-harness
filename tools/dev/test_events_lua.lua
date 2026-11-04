-- Offline tests for Server/Events.lua fairness/correctness (audit 2026-10-13):
-- ACT-14 the `cast` event carries the engine spell id only for party casters,
-- ACT-15 approval changes are reported from the first one after a load (the
-- poll seeds E.approval from GetApprovalRating).
-- Ext, Osi, RPC and Visibility are stubbed; Shared/Util.lua is real.
-- Run: tools/dev/test_events_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local HOST = "aaaaaaaa-0000-0000-0000-000000000001"
local SH = "aaaaaaaa-0000-0000-0000-000000000002" -- companion in the party
local GOB = "cccccccc-0000-0000-0000-000000000009" -- a visible enemy

local listeners, tick = {}, nil
local pushed = {}
local ratings = { [SH .. "|" .. HOST] = 10 }

Ext = {
    Osiris = { RegisterListener = function(name, _, _, fn) listeners[name] = fn end },
    Utils = { MonotonicTime = function() return 100000 end, PrintWarning = function(m) print("WARN " .. m) end,
        Print = function() end, GetGameState = function() return "Running" end },
    Events = { Tick = { Subscribe = function(_, fn) tick = fn end } },
    Stats = { Get = function(id) return ({
        Target_FireBolt = { DisplayName = "hFire", SpellRoll = { Default = "Attack(AttackType.RangedSpellAttack)" } },
        Projectile_GOB_SecretHint = { DisplayName = "hScimitar", SpellRoll = { Default = "" } },
        -- Cloudkill's stats entry (SharedDev Spell_Target.txt), its loca handles stubbed
        Target_Cloudkill = { DisplayName = "hCK;2", Description = "hCKd;7",
            DescriptionParams = "DealDamage(5d8,Poison)", ExtraDescription = "hCKx;5", Level = 5,
            TargetRadius = "18", AreaRadius = "6",
            TooltipAttackSave = "Constitution", TooltipDamageList = "DealDamage(5d8,Poison)",
            TooltipStatusApply = "ApplyStatus(CLOUDKILL,100,10)", SpellRoll = { Default = "" },
            SpellFlags = { "IsSpell", "IsHarmful", "IsConcentration" } },
        Projectile_Unnamed = { DisplayName = "", SpellRoll = { Default = "" } },
    })[id] end },
    Loca = { GetTranslatedString = function(h) return ({ hFire = "Fire Bolt", hScimitar = "Scimitar Slash",
        hCK = "Cloudkill",
        hCKd = "Create a cloud of poison that deals [1] each turn to creatures inside it.<br>It moves away from you.",
        hCKx = "The cloud moves <LSTag Type=\"Tooltip\" Tooltip=\"Distance\">3m</LSTag> each turn." })[h] end },
}
Osi = {
    GetHostCharacter = function() return HOST end,
    GetApprovalRating = function(owner, rated) return ratings[owner .. "|" .. rated] end,
    PartyGetGold = function() return 50 end,
    CombatGetGuidFor = function() return nil end,
}

local party = { HOST, SH }
local isParty = { [HOST] = true, [SH] = true }
BG3AI = {
    RPC = { push = function(kind, data) pushed[#pushed + 1] = { kind = kind, data = data } end,
        register = function() end },
    Visibility = {
        osi = function(fn, ...) local ok, r = pcall(fn, ...) if ok then return r end return nil end,
        uuid = function(x) return type(x) == "string" and x:match("%x+%-%x+%-%x+%-%x+%-%x+$") or nil end,
        entity = function(u) return { ServerCharacter = {}, _u = u } end,
        context = function() return { party = party, is_party = isParty, observers = {}, rules = {} } end,
        characterVisible = function(_, u) return u == GOB end,
        name = function(u) return ({ [HOST] = "Tav", [SH] = "Shadowheart", [GOB] = "Goblin" })[u] end,
        index = { ready = false },
    },
}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/Events.lua")
local E = BG3AI.Events

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end
local function last(kind)
    for i = #pushed, 1, -1 do if pushed[i].kind == kind then return pushed[i].data end end
    return nil
end

-- ACT-14: an enemy's cast shows its display name only; the party's own cast keeps the id.
do
    listeners.CastedSpell("Goblin_" .. GOB, "Projectile_GOB_SecretHint", "", "", 0)
    local d = last("cast")
    check("enemy cast pushed", d ~= nil)
    check("enemy cast: shown name", d and d.name == "Scimitar Slash", d and d.name)
    check("enemy cast: no engine id", d and d.spell == nil, d and d.spell)
    listeners.CastedSpell("S_Player_" .. SH, "Target_FireBolt", "", "", 0)
    d = last("cast")
    check("party cast: id kept", d and d.spell == "Target_FireBolt", d and d.spell)
    check("party cast: roll", d and d.roll == "attack", d and d.roll)
end

-- ACT-15: unseeded, the first change after a load used to be dropped.
do
    E.approval = {}
    tick()
    check("seeded from GetApprovalRating", E.approval[SH .. "|" .. HOST] == 10, E.approval[SH .. "|" .. HOST])
    check("host not seeded against itself", E.approval[HOST .. "|" .. HOST] == nil)
    local n = #pushed
    listeners.ApprovalRatingChanged("S_Player_" .. SH, "Tav_" .. HOST, 12)
    local d = last("approval")
    check("first change after load reported", #pushed == n + 1 and d ~= nil)
    check("first change: direction", d and d.direction == "approves", d and d.direction)
    listeners.ApprovalRatingChanged("S_Player_" .. SH, "Tav_" .. HOST, 7)
    d = last("approval")
    check("second change: direction", d and d.direction == "disapproves", d and d.direction)
    n = #pushed
    listeners.ApprovalRatingChanged("S_Player_" .. SH, "Tav_" .. HOST, 7)
    check("same value: no event", #pushed == n)
    -- the poll never overwrites a value the listener already holds
    ratings[SH .. "|" .. HOST] = 99
    E.lastPoll = 0
    tick()
    check("seed keeps the live value", E.approval[SH .. "|" .. HOST] == 7, E.approval[SH .. "|" .. HOST])
end

-- a member with no rating (a hireling) is asked once, not every poll
do
    local HIRE = "aaaaaaaa-0000-0000-0000-000000000003"
    party[#party + 1] = HIRE; isParty[HIRE] = true
    local asked = 0
    local real = Osi.GetApprovalRating
    Osi.GetApprovalRating = function(o, r) if o == HIRE then asked = asked + 1 end return real(o, r) end
    for _ = 1, 3 do E.lastPoll = 0; tick() end
    check("no rating: asked once", asked == 1, asked)
    check("no rating: nothing seeded", E.approval[HIRE .. "|" .. HOST] == nil)
    Osi.GetApprovalRating = real
end

-- vis V26: dialogue_started says where the party member stood when the talk began (and the level): the bridge's
-- off-limits areas (S084 Counting House trespass talks). Their own character's place: fair.
do
    local V = BG3AI.Visibility
    V.pos = function(e) return e._u == SH and { -111.04, 4.0, -177.02 } or nil end
    V.levelNow = function() return "CTY_Main_A" end
    Osi.DialogGetNumberOfInvolvedNPCs = function() return 1 end
    Osi.DialogGetInvolvedNPC = function() return "Dadger_" .. GOB end
    Osi.DialogGetNumberOfInvolvedPlayers = function() return 1 end
    Osi.DialogGetInvolvedPlayer = function() return "S_Player_" .. SH end
    Osi.DialogIsAutomated = function() return 0 end
    listeners.DialogStarted(nil, 7)
    local d = last("dialogue_started")
    check("dialogue_started: at", d and d.at and d.at[1] == -111 and d.at[3] == -177, d and d.at and d.at[1])
    check("dialogue_started: level", d and d.level == "CTY_Main_A", d and d.level)
end

-- audit C10: quest events only for quests the journal shows, with a localised title and step text
do
    BG3AI.State = { loadQuests = function() return {
        { id = "Q_SHOWN", title = "hQ", visible = true, steps = { { id = "S1", desc = "hS1" }, { id = "S2", desc = "hEmpty" } } },
        { id = "Q_HIDDEN", title = "hQ", visible = false, steps = { { id = "S1", desc = "hS1" } } },
        { id = "Q_NOTITLE", title = "hEmpty", visible = true, steps = { { id = "S1", desc = "hS1" } } },
    } end }
    local realLoca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return ({ hQ = "Find the Relic", hS1 = "A step.", hEmpty = "%%% EMPTY" })[h] end
    local function count(kind) local n = 0 for _, p in ipairs(pushed) do if p.kind == kind then n = n + 1 end end return n end
    local qa, qu = count("quest_added"), count("quest_update")
    listeners.QuestAccepted("S_Player_" .. SH, "Q_HIDDEN")
    listeners.QuestAccepted("S_Player_" .. SH, "Q_NOTITLE")
    listeners.QuestAccepted("S_Player_" .. SH, "Q_UNKNOWN")
    check("hidden/untitled/unknown quest: no quest_added", count("quest_added") == qa, count("quest_added") - qa)
    listeners.QuestAccepted("S_Player_" .. SH, "Q_SHOWN")
    check("shown quest: quest_added with its title", count("quest_added") == qa + 1 and last("quest_added").title == "Find the Relic")
    listeners.QuestUpdateUnlocked("S_Player_" .. SH, "Q_HIDDEN", "S1")
    listeners.QuestUpdateUnlocked("S_Player_" .. SH, "Q_UNKNOWN", "S1")
    listeners.QuestUpdateUnlocked("S_Player_" .. SH, "Q_SHOWN", "S2")
    check("hidden quest / unknown quest / unlocalised step: no quest_update", count("quest_update") == qu, count("quest_update") - qu)
    listeners.QuestUpdateUnlocked("S_Player_" .. SH, "Q_SHOWN", "S1")
    local d = last("quest_update")
    check("shown step: quest_update with title and text", count("quest_update") == qu + 1 and d.title == "Find the Relic"
        and d.text == "A step.")
    Ext.Loca.GetTranslatedString = realLoca
end

-- audit C11: item_effect only for an item the party sees or remembers, never one near it behind a wall
do
    local V = BG3AI.Visibility
    local PAINT = "dddddddd-0000-0000-0000-000000000001"
    local known = false
    V.itemEffect = function(id) return ({ LIT = "lit", FLASH = "flashing" })[id] end
    V.itemKnown = function(_, u) return u == PAINT and known end
    V.statusInfo = function() return nil end
    local realCtx, realPos, realTime = V.context, V.pos, Ext.Utils.MonotonicTime
    V.context = function() local c = realCtx(); c.observers = { { pos = { 1, 0, 0 } } }; return c end
    V.pos = function() return { 3, 0, 0 } end -- 2 m from a member, behind a wall
    Ext.Utils.MonotonicTime = function() return 900000 end -- Events' ctx() cache expires: the context above
    local n = #pushed
    listeners.StatusApplied("Paint_" .. PAINT, "LIT", "", "")
    check("unseen item lighting up 2 m away: no item_effect", #pushed == n)
    V.context, V.pos = realCtx, realPos
    known = true
    listeners.StatusApplied("Paint_" .. PAINT, "LIT", "", "")
    local d = last("item_effect")
    check("seen item: item_effect lit", #pushed == n + 1 and d and d.state == "lit", d and d.state)
    listeners.StatusRemoved("Paint_" .. PAINT, "LIT", "", "")
    d = last("item_effect")
    check("seen item: no longer lit", d and d.state == "no longer lit", d and d.state)    Ext.Utils.MonotonicTime = realTime
end

-- launch-v1 haiku act3_gold_max r2/r5: the night's sleep press was refused while the camp story still wanted a talk
-- (GLO_Camp DB_Camp_RequiredTalks) with Shadowheart (in the party) or Withers (at camp, out of sight). Who: the party
-- member and a perceived character by name, the unseen one counted only; one row per character.
do
    local WITHERS = "eeeeeeee-0000-0000-0000-000000000004"
    local rows = {
        { "S_Player_ShadowHeart_" .. SH, "CAMP_Shadowheart_CRD_Parents_" .. "11111111-0000-0000-0000-000000000001" },
        { "S_GLO_Withers_" .. WITHERS, "CAMP_Withers_Night_" .. "11111111-0000-0000-0000-000000000002" },
        { "Dadger_" .. GOB, "NULL_00000000-0000-0000-0000-000000000000" },
        { "S_Player_ShadowHeart_" .. SH, "NULL_00000000-0000-0000-0000-000000000000" },
    }
    Osi.DB_Camp_RequiredTalks = { Get = function(_, a, b)
        if a ~= nil or b ~= nil then error("filtered read") end
        return rows
    end }
    local t = E.requiredTalks()
    check("required talks: one row per character", t and #t == 3, t and #t)
    check("required talks: the party member named", t and t[1].uuid == SH and t[1].name == "Shadowheart",
        t and t[1].name)
    check("required talks: the unseen one counted, not named", t and t[2].uuid == nil and t[2].name == nil,
        t and t[2].name)
    check("required talks: a perceived one named", t and t[3].uuid == GOB and t[3].name == "Goblin", t and t[3].name)
    rows = {}
    t = E.requiredTalks()
    check("no required talks: empty", t and #t == 0, t and #t)
    Osi.DB_Camp_RequiredTalks = nil
    check("unreadable DB: nil", E.requiredTalks() == nil)
end

-- act2-fights-analysis fix 4: an enemy's spell on first sight carries the tooltip a player reads by hovering it, once
-- per spell per fight; the party's own casts and a repeat in the same fight add nothing.
do
    local combat = "combat-1"
    Osi.CombatGetGuidFor = function() return combat end
    local function infos()
        local out = {}
        for _, p in ipairs(pushed) do if p.kind == "spell_info" then out[#out + 1] = p.data end end
        return out
    end
    local n0 = #infos()
    listeners.CastedSpell("Balthazar_" .. GOB, "Target_Cloudkill", "", "", 0)
    local got = infos()
    check("first sight: one spell_info", #got == n0 + 1, #got - n0)
    local want = "Cloudkill: Create a cloud of poison that deals 5d8 Poison damage each turn to creatures inside it."
        .. " It moves away from you. The cloud moves 3m each turn. (level 5, concentration, range 18m, area 6m,"
        .. " Constitution save, 5d8 Poison damage, lasts 10 turns)"
    check("first sight: the tooltip's text and numbers", got[#got] == want, got[#got])
    check("the cast event itself is unchanged", last("cast").name == "Cloudkill" and last("cast").spell == nil)
    listeners.CastedSpell("Balthazar_" .. GOB, "Target_Cloudkill", "", "", 0)
    check("same fight: not again", #infos() == n0 + 1, #infos() - n0)
    E.note_spell(GOB, "Target_Cloudkill") -- a row showing it concentrating on Cloudkill, same fight
    check("row in the same fight: not again", #infos() == n0 + 1)
    combat = "combat-2"
    E.note_spell(GOB, "Target_Cloudkill") -- the next fight: its row's concentration is its first sight there
    check("next fight: again", #infos() == n0 + 2, #infos() - n0)
    listeners.CastedSpell("S_Player_" .. SH, "Target_FireBolt", "", "", 0)
    check("a party cast: no spell_info", #infos() == n0 + 2)
    E.note_spell(GOB, "Projectile_Unnamed")
    check("a spell with no shown name: no spell_info", #infos() == n0 + 2)
end

-- w10 L2 (Balthazar): damage formulas with the caster's numbers, the common actions left out, a Shout's missing
-- fields, and Balthazar's own Cloudkill entry (GustavDev Spell_Target.txt: using Target_Cloudkill; the engine's
-- stats object carries the inherited fields).
do
    local V = BG3AI.Visibility
    local RANGER = "dddddddd-0000-0000-0000-000000000011" -- a skeleton with a longsword, level 3
    local BARE = "dddddddd-0000-0000-0000-000000000012"   -- nothing equipped
    local SWORD = "ffffffff-0000-0000-0000-000000000001"
    local NOSTATS = "dddddddd-0000-0000-0000-000000000013" -- a longsword, ability scores unreadable
    local RAPIER = "ffffffff-0000-0000-0000-000000000002"
    local FENCER = "dddddddd-0000-0000-0000-000000000014" -- a +1 rapier (finesse): the higher of STR/DEX
    local combat = "combat-L2"
    Osi.CombatGetGuidFor = function() return combat end
    local realGet, realEntity, realPrint = Ext.Stats.Get, V.entity, Ext.Utils.Print
    local printed = {}
    Ext.Utils.Print = function(m) printed[#printed + 1] = m end
    -- a stats object raises on a field its type lacks (Shout_Dash has no TargetRadius), as the engine's does
    local function strict(t) return setmetatable(t, { __index = function(_, k) error("no field " .. k) end }) end
    local entries = {
        Target_SHA_Necromancer_Cloudkill = { DisplayName = "hCK;2", Description = "hCKd;7",
            DescriptionParams = "DealDamage(5d8,Poison)", ExtraDescription = "hCKx;5", Level = 5,
            TargetRadius = "18", AreaRadius = "6", TooltipAttackSave = "Constitution",
            TooltipDamageList = "DealDamage(5d8,Poison)", TooltipStatusApply = "ApplyStatus(CLOUDKILL,100,10)",
            SpellRoll = { Default = "" }, SpellFlags = { "IsSpell", "IsHarmful", "IsConcentration" } },
        Projectile_RayOfFrost = { DisplayName = "hRoF;1", Description = "hRoFd;3", DescriptionParams = "Distance(3)",
            Level = 0, TargetRadius = "18", TooltipAttackSave = "RangedSpellAttack",
            TooltipDamageList = "DealDamage(LevelMapValue(D8Cantrip),Cold)",
            TooltipStatusApply = "ApplyStatus(RAY_OF_FROST,100,1)", SpellRoll = { Default = "Attack(x)" },
            SpellFlags = { "IsSpell", "IsHarmful" } },
        Target_MainHandAttack = { DisplayName = "hMH;1", Description = "hMHd;1", TargetRadius = "MeleeMainWeaponRange",
            TooltipAttackSave = "MeleeWeaponAttack",
            TooltipDamageList = "DealDamage(MainMeleeWeapon, MainMeleeWeaponDamageType)",
            SpellRoll = { Default = "Attack(x)" }, SpellFlags = { "IsAttack", "IsMelee", "IsHarmful" } },
        Target_Skeleton_Cleave = { DisplayName = "hCl;1", Description = "hCld;1",
            TooltipAttackSave = "MeleeWeaponAttack",
            TooltipDamageList = "DealDamage(MainMeleeWeapon, MainMeleeWeaponDamageType)",
            SpellRoll = { Default = "Attack(x)" }, SpellFlags = { "IsAttack", "IsMelee", "IsHarmful" } },
        Shout_Dash = strict({ DisplayName = "hDash;1", Description = "hDashd;1", SpellRoll = { Default = "" },
            SpellFlags = { "IgnoreSilence" } }),
        WPN_Longsword = { Damage = "1d8", ["Damage Type"] = "Slashing" },
        WPN_Rapier_1 = { Damage = "1d8", ["Damage Type"] = "Piercing", ["Weapon Properties"] = { "Finesse", "Light" },
            DefaultBoosts = "WeaponEnchantment(1);WeaponProperty(Magical)" },
    }
    Ext.Stats.Get = function(id) return entries[id] or realGet(id) end
    local loca = { hRoF = "Ray of Frost", hRoFd = "Reduces the target's movement speed by [1].",
        hMH = "Main Hand Attack",
        hMHd = "Make a melee attack with your equipped weapon.", hCl = "Cleave", hCld = "Swing through two foes.",
        hDash = "Dash", hDashd = "Double your movement speed." }
    local realLoca = Ext.Loca.GetTranslatedString
    Ext.Loca.GetTranslatedString = function(h) return loca[h] or realLoca(h) end
    -- the level map as bg3se lays it out (GuidResources.h LevelMap: Name, LevelMaps[level], DiceRoll values)
    local d8 = { Name = "D8Cantrip", FallbackValue = 0, LevelMaps = {} }
    for l = 1, 20 do d8.LevelMaps[l] = { DiceValue = 2, AmountOfDices = l >= 11 and 3 or (l >= 5 and 2 or 1) } end
    Ext.StaticData = { GetAll = function(t) return t == "LevelMap" and { "lm-1", "lm-2" } or {} end,
        Get = function(g, t)
            if t ~= "LevelMap" then return nil end
            return ({ ["lm-1"] = { Name = "D10Cantrip", LevelMaps = {} }, ["lm-2"] = d8 })[g]
        end }
    local book = { { Id = { Prototype = "Target_MainHandAttack" } }, { Id = { Prototype = "Shout_Dash" } } }
    V.entity = function(u)
        -- Abilities: [1] None, Strength 16 (+3), Dexterity 12 (+1)
        return { ServerCharacter = {}, _u = u, EocLevel = { Level = (u == RANGER or u == GOB) and 3 or 1 },
            Stats = u ~= NOSTATS and { Abilities = { 0, 16, 12, 10, 10, 10, 10 } } or nil,
            SpellBook = isParty[u] and { Spells = book } or nil,
            Data = (u == SWORD and { StatsId = "WPN_Longsword" }) or (u == RAPIER and { StatsId = "WPN_Rapier_1" })
                or nil }
    end
    Ext.Entity = { Get = function(u) return V.entity(u) end }
    Osi.GetEquippedItem = function(u, slot)
        if slot ~= "Melee Main Weapon" then return nil end
        return ((u == RANGER or u == NOSTATS) and SWORD) or (u == FENCER and RAPIER) or nil
    end
    local realVisible = V.characterVisible
    V.characterVisible = function(_, u) return u == GOB or u == RANGER or u == BARE or u == NOSTATS or u == FENCER end
    local function infos()
        local out = {}
        for _, p in ipairs(pushed) do if p.kind == "spell_info" then out[#out + 1] = p.data end end
        return out
    end

    local n0 = #infos()
    listeners.CastedSpell("S_SHA_Necromancer_" .. GOB, "Target_SHA_Necromancer_Cloudkill", "target", "Conjuration", 0)
    local got = infos()
    check("Balthazar's Cloudkill: a cast event", last("cast") and last("cast").name == "Cloudkill")
    check("Balthazar's Cloudkill: its spell_info", #got == n0 + 1 and got[#got]:find("^Cloudkill: Create a cloud")
        and got[#got]:find("lasts 10 turns", 1, true), got[#got])
    E.note_spell(GOB, "Target_SHA_Necromancer_Cloudkill") -- his row's concentration=Cloudkill, same fight
    check("Balthazar's Cloudkill: the row's concentration doesn't repeat it", #infos() == n0 + 1)
    combat = "combat-L2b" -- a fight whose cast this side never saw: the row's concentration is its first sight
    E.note_spell(GOB, "Target_SHA_Necromancer_Cloudkill")
    check("concentration alone: spell_info", #infos() == n0 + 2, #infos() - n0)

    listeners.CastedSpell("S_Skeleton_Caster_" .. RANGER, "Projectile_RayOfFrost", "projectile", "Evocation", 0)
    got = infos()
    check("Ray of Frost: LevelMapValue(D8Cantrip) at the caster's level 3",
        got[#got] == "Ray of Frost: Reduces the target's movement speed by 3m. (range 18m, ranged spell attack,"
            .. " 1d8 Cold damage, lasts 1 turn)", got[#got])

    -- the party has a summon (no Main Hand Attack in its own short spellbook): still on every member's hotbar
    local SUM = "aaaaaaaa-0000-0000-0000-0000000000f1"
    party[#party + 1], isParty[SUM] = SUM, true
    local entityNoSummon = V.entity
    V.entity = function(u)
        if u == SUM then return { ServerCharacter = {}, IsSummon = { Owner = HOST }, SpellBook = { Spells = {} } } end
        return entityNoSummon(u)
    end
    Ext.Utils.MonotonicTime = function() return 1900000 end -- Events' ctx() cache: the party with the summon
    local n1, c1 = #infos(), #pushed
    listeners.CastedSpell("S_Skeleton_" .. RANGER, "Target_MainHandAttack", "target", "", 0)
    listeners.CastedSpell("S_Skeleton_" .. RANGER, "Shout_Dash", "shout", "", 0)
    check("Main Hand Attack and Dash (on every party hotbar): no spell_info", #infos() == n1, infos()[#infos()])
    check("... their cast events stay", #pushed == c1 + 2 and last("cast").name == "Dash")
    party[#party], isParty[SUM], V.entity = nil, nil, entityNoSummon

    -- REVIEW-13 item 1: weapon tokens are never resolved (off-hand, versatile two-handed and the caster's own damage
    -- boosts change the tooltip's number): a longsword, a +1 finesse rapier and no weapon all leave the part out
    local p0 = #printed
    for i, who in ipairs({ RANGER, FENCER, NOSTATS, BARE }) do
        combat = "combat-L2w" .. i -- once per spell per fight: each caster in its own fight
        listeners.CastedSpell("S_Skeleton_" .. who, "Target_Skeleton_Cleave", "target", "", 0)
        got = infos()
        check("weapon formula left out (" .. i .. ")",
            got[#got] == "Cleave: Swing through two foes. (melee weapon attack)" and not got[#got]:find("Weapon"),
            got[#got])
    end
    check("... one log line per caster names the token", #printed == p0 + 4
        and printed[#printed]:find("MainMeleeWeapon", 1, true), printed[#printed])

    -- a description whose parameter stays a formula is left out whole (no "[1]" or a cut sentence)
    entries.Target_Skeleton_Chill = { DisplayName = "hRoF;1", Description = "hRoFd;3",
        DescriptionParams = "Distance(MovementSpeed)", SpellRoll = { Default = "" }, SpellFlags = { "IsSpell" } }
    E.spellInfo, E.spellSeen = {}, {}
    E.note_spell(RANGER, "Target_Skeleton_Chill")
    got = infos()
    check("an unresolved description parameter: description left out", got[#got] == "Ray of Frost:", got[#got])
    E.spellInfo, E.spellSeen = {}, {}

    Ext.Stats.Get, V.entity, Ext.Utils.Print, Ext.Loca.GetTranslatedString = realGet, realEntity, realPrint, realLoca
    V.characterVisible = realVisible
    Ext.StaticData, Ext.Entity, Osi.GetEquippedItem = nil, nil, nil
end

-- w11 L1 trade: a +52 g sale gave "item_added Gold count=402" (the stack after the merge), paying 48 g gave
-- "item_added Gold count=48" (the split-off coins). Gold comes from GoldChanged as the change of the party's total.
do
    local gold, realGold = 410, Osi.PartyGetGold
    Osi.PartyGetGold = function() return gold end
    Osi.GetStackAmount = function() return 402 end
    E.lastGold = nil
    local function kinds(from)
        local out = {}
        for i = from, #pushed do out[#out + 1] = pushed[i].kind end
        return table.concat(out, ",")
    end
    local n = #pushed + 1
    listeners.AddedTo("LOOT_Gold_A_dddddddd-0000-0000-0000-000000000001", "S_Player_" .. HOST, "Regular")
    check("a gold stack's AddedTo is no item_added", kinds(n) == "", kinds(n))
    gold = 462
    listeners.GoldChanged("Trader_cccccccc-0000-0000-0000-000000000003", -52) -- the trader's side
    listeners.GoldChanged("S_Player_" .. HOST, 52)
    local d = last("gold_changed")
    check("sale: one gold_changed", kinds(n) == "gold_changed", kinds(n))
    check("sale: +52 -> 462", d and d.gold == 462 and d.delta == 52, d and (tostring(d.gold) .. " " .. tostring(d.delta)))
    n = #pushed + 1
    Osi.GetStackAmount = function() return 48 end
    gold = 414
    listeners.AddedTo("LOOT_Gold_A_dddddddd-0000-0000-0000-000000000002", "S_Player_" .. HOST, "Regular")
    listeners.GoldChanged("S_Player_" .. HOST, -48)
    d = last("gold_changed")
    check("payment: only gold_changed", kinds(n) == "gold_changed", kinds(n))
    check("payment: -48 -> 414", d and d.gold == 414 and d.delta == -48, d and (tostring(d.gold) .. " " .. tostring(d.delta)))
    n = #pushed + 1
    listeners.GoldChanged("S_Player_" .. HOST, -10) -- a move between members: the total stays 414
    listeners.GoldChanged("S_Player_" .. SH, 10)
    check("move between members: nothing", kinds(n) == "", kinds(n))
    Osi.PartyGetGold, Osi.GetStackAmount = realGold, nil
end

print(string.format("%d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
