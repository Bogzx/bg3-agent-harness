-- Offline test: ui.level_up in Client/Progression.lua over a fake CharacterLevelUp page (no game).
-- Covers the subclass level (Wizard 2: Gale), the feat level (4: Ability Improvement +2 / +1+1, a named feat,
-- the defaults), refusals (no subclass in the plan, feat not available, no level up available).
-- Run: tools/dev/test_level_up.sh
local L = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local fails = 0
local function check(cond, what) if not cond then fails = fails + 1 print("FAIL " .. what) else print("ok   " .. what) end end

-- Fake Noesis objects: tables with props and GetProperty/SetProperty.
local function obj(type_, props)
    local o = { _t = type_, _p = props or {} }
    function o.GetProperty(self, k) return self._p[k] end
    function o.SetProperty(self, k, v) self._p[k] = v end
    return o
end
local function item(name, extra)
    local p = { Name = name }
    for k, v in pairs(extra or {}) do p[k] = v end
    return obj("VMItem", p)
end

local ABIL = { "Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma" }

-- Static data the mod names spells and passives from (Ext.StaticData / Ext.Stats / Ext.Loca fakes).
STATS = {
    Zone_BurningHands = { Level = 1, SpellSchool = "Evocation", Name = "Burning Hands" },
    Target_CharmPerson = { Level = 1, SpellSchool = "Enchantment", Name = "Charm Person" },
    Projectile_MagicMissile = { Level = 1, SpellSchool = "Evocation", Name = "Magic Missile" },
    Shout_Shield_Wizard = { Level = 1, SpellSchool = "Abjuration", Name = "Shield" },
    Target_Sleep = { Level = 1, SpellSchool = "Enchantment", Name = "Sleep" },
    Target_Hex = { Level = 1, SpellSchool = "Enchantment", Name = "Hex" },
    Shout_HellishRebuke = { Level = 1, SpellSchool = "Evocation", Name = "Hellish Rebuke" },
    Target_Bless = { Level = 1, SpellSchool = "Enchantment", Name = "Bless" },
    AgonizingBlast = { Name = "Agonising Blast" }, ArmorOfShadows = { Name = "Armour of Shadows" },
    RepellingBlast = { Name = "Repelling Blast" }, DevilsSight = { Name = "Devil's Sight" },
    FS_Archery = { Name = "Archery" }, FS_Defence = { Name = "Defence" }, FS_Duelling = { Name = "Duelling" },
    FS_Protection = { Name = "Protection" },
}
SPELL_LISTS = {
    wizard1 = { "Zone_BurningHands", "Target_CharmPerson", "Projectile_MagicMissile", "Shout_Shield_Wizard", "Target_Sleep" },
    -- same size, different school sequence: must not match the wizard page
    decoy5 = { "Target_CharmPerson", "Zone_BurningHands", "Projectile_MagicMissile", "Shout_Shield_Wizard", "Target_Sleep" },
    warlock1 = { "Target_CharmPerson", "Shout_HellishRebuke", "Target_Hex" },
    -- the same level/school sequence as warlock1 with other spells: a page of 3 is ambiguous between these two
    other3 = { "Target_Bless", "Shout_HellishRebuke", "Target_Sleep" },
}
PASSIVE_LISTS = {
    invocations = { Name = "Warlock invocations", Passives = { "AgonizingBlast", "ArmorOfShadows", "RepellingBlast", "DevilsSight" } },
    styles = { Name = "Fighting styles", Passives = { "FS_Archery", "FS_Defence", "FS_Duelling", "FS_Protection" } },
}
local PROGRESSIONS = {
    warlock2 = { TableUUID = "tblWarlock", Level = 2, SelectPassives = { { UUID = "invocations" } } },
    fighter1 = { TableUUID = "tblFighter", Level = 1, SelectPassives = { { UUID = "styles" } } },
}
local CLASSES = { clsWarlock = { ProgressionTableUUID = "tblWarlock" }, clsWizard = { ProgressionTableUUID = "tblWizard" } }
local function keys(t) local o = {} for k in pairs(t) do o[#o + 1] = k end table.sort(o) return o end
local widget, dc, page        -- the open level-up page (nil = closed)

-- page spec: {class, subclasses?, preselect?, feat_level?, primary?, scores?}
local function openPage(spec)
    page = { spec = spec, finished = false, points = 0 }
    local p = {
        LevelUpStep = "Overview", IsMulticlassEnabled = true,
        SelectableMultiClasses = { item(spec.class) },
        SelectableSubClasses = {}, SelectableDeities = {}, CanSelectFeat = spec.feat_level == true,
        SelectableFeats = {}, FeatDetails = obj("FeatDetails", { AbilitySelection = {} }),
    }
    p.SelectedMultiClass = p.SelectableMultiClasses[1]
    for _, n in ipairs(spec.subclasses or {}) do p.SelectableSubClasses[#p.SelectableSubClasses + 1] = item(n) end
    if spec.preselect then p.SelectedSubClass = p.SelectableSubClasses[spec.preselect] end
    if spec.feat_level then
        p.SelectableFeats = { item("Ability Improvement"), item("Great Weapon Master"),
            item("Heavy Armour Master", { IsRequirementsMet = false }) }
    end
    -- Spell selector (spec.spells = {list = spell list id, known = {index = true}, open = n}): VMs with no
    -- readable name, like the live page; only level and school.
    local cpd = { SpellSelectors = {}, NotSubPassiveSelectors = {} }
    if spec.spells then
        local avail, adds = {}, {}
        for i, sid in ipairs(SPELL_LISTS[spec.spells.list]) do
            local st = STATS[sid]
            avail[i] = obj("ls.VMSpellReference", { NotAvailable = (spec.spells.known or {})[i] == true, Selected = false,
                Spell = obj("ls.VMCharacterCreationSpell", { Level = st.Level, SpellSchool = st.SpellSchool }) })
        end
        for i = 1, spec.spells.open do adds[i] = obj("ls.VMSpellReference", {}) end
        local sel = obj("ls.VMSpellSelector", { Available = avail, Additions = adds, IsCantrips = false })
        function sel.GetProperty(self, k)
            if k == "IsComplete" then
                for _, a in ipairs(self._p.Additions) do if a._p.Spell == nil then return false end end
                return true
            end
            return self._p[k]
        end
        cpd.SpellSelectors = { sel }
        page.spellSel = sel
    end
    -- Passive selector (spec.passives = {list = passive list id, max = n}): VMs with Value 0/1, no names.
    if spec.passives then
        local items = {}
        for i = 1, #PASSIVE_LISTS[spec.passives.list].Passives do items[i] = obj("VMPassive", { Value = 0.0, Enabled = true, Blocked = false }) end
        local ps = obj("VMPassiveSelector", { Passives = items, MaxSelectedPassiveCount = spec.passives.max })
        function ps.GetProperty(self, k)
            local n = 0
            for _, it in ipairs(self._p.Passives) do if it._p.Value > 0 then n = n + 1 end end
            if k == "SelectedPassiveCount" then return n end
            if k == "IsComplete" then return n >= self._p.MaxSelectedPassiveCount end
            return self._p[k]
        end
        cpd.NotSubPassiveSelectors = { ps }
        page.passiveSel = ps
    end
    p.ClassProgressionDetails = obj("VMProgressionDetails", cpd)
    dc = obj("ls.DCCharacterLevelUp", p)
    -- Selecting a feat fills FeatDetails.AbilitySelection (only Ability Improvement asks for abilities here).
    function dc.SetProperty(self, k, v)
        self._p[k] = v
        if k == "SelectedFeat" then
            local sel = {}
            if v and v._p.Name == "Ability Improvement" then
                for _, a in ipairs(ABIL) do
                    sel[#sel + 1] = obj("VMAbility", { Ability = a, Value = (spec.scores or {})[a] or 14,
                        Improvement = 0, IsPrimary = a == spec.primary })
                end
            end
            page.points = 0
            self._p.FeatDetails = obj("FeatDetails", { AbilitySelection = sel })
        end
    end
    function dc.GetProperty(self, k)
        if k == "IsLevelUpComplete" then
            if #(self._p.SelectableSubClasses) > 0 and self._p.SelectedSubClass == nil then return false end
            if page.spellSel and not page.spellSel:GetProperty("IsComplete") then return false end
            if page.passiveSel and not page.passiveSel:GetProperty("IsComplete") then return false end
            if self._p.CanSelectFeat then
                if self._p.SelectedFeat == nil then return false end
                if #self._p.FeatDetails._p.AbilitySelection > 0 and page.points < 2 then return false end
            end
            return true
        end
        return self._p[k]
    end
    widget = obj("ls.UIWidget", {})
    widget.DataContext = dc
end

local function closePage() widget, dc = nil, nil end

-- HotBar with one party portrait VM (for the open-it-yourself path).
local portrait = obj("VMCharacter", {})
local stats = { Health = obj("H", { Value = 16, Max = 16 }), Level = obj("Lv", { Value = 1, LevelUpAvailable = true }) }
portrait._p.Stats = obj("Stats", stats)
local hotbar = obj("ls.DCHotBar", { CurrentPlayer = obj("Player", {
    PartyGroups = { obj("Group", { Characters = { portrait } }) }, SelectedCharacter = portrait }) })
local opened = 0

local CLASS_OF = { gale = "clsWizard", wyll = "clsWarlock" }
Ext = { Entity = { Get = function(u) return { ClientControl = true, Health = { Hp = 16, MaxHp = 16 }, EocLevel = { Level = 1 },
                                               Classes = { Classes = { { ClassUUID = CLASS_OF[u], Level = 1 } } } } end },
        Input = { InjectKeyPress = function() end },
        Stats = { Get = function(id) local st = STATS[id] if not st then return nil end
                      return { Level = st.Level, SpellSchool = st.SpellSchool, DisplayName = "h:" .. id } end },
        Loca = { GetTranslatedString = function(h) local st = STATS[h:sub(3)] return st and st.Name or "" end },
        StaticData = {
            GetAll = function(kind)
                if kind == "SpellList" then return keys(SPELL_LISTS) end
                if kind == "Progression" then return keys(PROGRESSIONS) end
                return {}
            end,
            Get = function(id, kind)
                if kind == "SpellList" then return SPELL_LISTS[id] and { Spells = SPELL_LISTS[id] } end
                if kind == "PassiveList" then return PASSIVE_LISTS[id] end
                if kind == "Progression" then return PROGRESSIONS[id] end
                if kind == "ClassDescription" then return CLASSES[id] end
            end,
        } }

BG3AI = {}
local handlers, jobs, now = {}, {}, 0
BG3AI.RPC = {
    register = function(n, f) handlers[n] = f end,
    now_ms = function() return now end,
    start_job = function(kind) local j = { id = #jobs + 1, kind = kind } jobs[#jobs + 1] = j return j end,
    finish_job = function(j, st, res, err) j.status, j.result, j.error = st, res, err end,
}

local function walk(o, path)
    for part in path:gmatch("[^%.]+") do
        if o == nil then return nil end
        o = o:GetProperty(part)
    end
    return o
end

BG3AI.UI = {
    prop = function(o, k) if o == nil then return nil end return o:GetProperty(k) end,
    props = function(o) return o and o._p or {} end,
    path = walk,
    typeName = function(o) return type(o) == "table" and o._t or nil end,
    text = function() return nil end,
    clean = function(s) return s end,
    widgets = function() return widget and { { name = "CharacterLevelUp", el = widget } } or {} end,
    widget = function() return widget end,
    isVisible = function() return true end,
    cachedByName = function() return obj("Tab", {}) end,
    byName = function() return nil end,
    findAll = function() return {} end,
    items = function(coll)
        local i = 0
        return function()
            if type(coll) ~= "table" or coll._t then return nil end
            i = i + 1
            if coll[i] == nil then return nil end
            return i, coll[i]
        end
    end,
    count = function(coll) return type(coll) == "table" and not coll._t and #coll or 0 end,
    dc = function(name) if name == "HotBar" then return hotbar end end,
    command = function(o, name, arg)
        if o == hotbar and name == "StartLevelUp" then
            opened = opened + 1
            openPage(page and page.spec or { class = "Wizard" })
            return true
        end
        if o ~= dc then return false, "unexpected command target" end
        if name == "SelectAbility" then
            if page.points >= 2 then return true end                  -- the page ignores a third point
            arg._p.Improvement = arg._p.Improvement + 1
            page.points = page.points + 1
            return true
        elseif name == "DeselectAbility" then
            if arg._p.Improvement > 0 then arg._p.Improvement = arg._p.Improvement - 1 page.points = page.points - 1 end
            return true
        elseif name == "FinishLevelUp" then
            page.finished = true
            closePage()
            return true
        elseif name == "TogglePassive" then
            arg._p.Value = arg._p.Value > 0 and 0.0 or 1.0
            return true
        elseif name == "SelectFirstUnusedClass" then
            return true
        end
        return false, "unknown command " .. name
    end,
}
dofile(L .. "/Client/Progression.lua")

local function run(args)
    local ok, r = pcall(handlers["ui.level_up"], args)
    if not ok then return { status = "error", error = r } end
    local j = jobs[r.job]
    for _ = 1, 2000 do
        if j.status then break end
        now = now + 100
        j.on_tick(j)
    end
    return j
end

local GALE = { class = "Wizard", subclasses = { "Abjuration School", "Evocation School", "Divination School" } }

-- 1. Gale, Wizard 1 -> 2, no subclass in the plan: refused with the choices, nothing finished
openPage(GALE)
local j = run({ uuid = "gale", plan = { spells = nil } })
check(j.status == "failed" and tostring(j.error):find("pass plan.subclass"), "subclass level without plan.subclass fails")
check(j.result.needs == "subclass" and #j.result.subclasses == 3 and j.result.subclasses[2] == "Evocation School",
    "result lists the subclasses")
check(dc ~= nil and dc._p.SelectedSubClass == nil and not page.finished, "nothing chosen, page still open")

-- 2. Gale with the planned subclass: Evocation, finished
j = run({ uuid = "gale", plan = { subclass = "Evocation" } })
check(j.status == "done" and j.result.finished, "subclass level with plan.subclass finishes")
check(j.result.set.subclass == "Evocation School" and #j.result.problems == 0, "Evocation chosen (prefix match), no problems")
check(page.finished and widget == nil, "FinishLevelUp ran, page closed")

-- 3. the page already shows a subclass (preselected): no plan needed, it is kept and reported
openPage({ class = "Wizard", subclasses = GALE.subclasses, preselect = 3 })
j = run({ uuid = "gale", plan = {} })
check(j.status == "done" and j.result.set.subclass == "Divination School", "preselected subclass kept")

-- 4. allow_defaults: first selectable, reported as defaulted
openPage(GALE)
j = run({ uuid = "gale", plan = { allow_defaults = true } })
local defaulted = table.concat(j.result.defaulted or {}, ",")
check(j.status == "done" and j.result.set.subclass == "Abjuration School" and defaulted:find("subclass"),
    "allow_defaults takes the first subclass and says so")

-- 5. Claude, Paladin 3 -> 4: Ability Improvement +2 Strength
local PAL = { class = "Paladin", feat_level = true, primary = "Strength", scores = { Strength = 17, Charisma = 16 } }
openPage(PAL)
j = run({ uuid = "claude", plan = { feat = "Ability Improvement", abilities = { Strength = 2 } } })
check(j.status == "done" and j.result.finished, "feat level: Ability Improvement finishes")
check(j.result.set.feat == "Ability Improvement", "feat set")
check(j.result.set.abilities and j.result.set.abilities.Strength == 2 and not j.result.set.abilities.Charisma,
    "abilities: Strength +2")

-- 6. +1/+1 with short ability names
openPage(PAL)
j = run({ uuid = "claude", plan = { feat = "Ability Improvement", abilities = { str = 1, Cha = 1 } } })
check(j.status == "done" and j.result.set.abilities.Strength == 1 and j.result.set.abilities.Charisma == 1,
    "abilities: +1 Strength +1 Charisma (short names)")

-- 7. a named feat without ability choices
openPage(PAL)
j = run({ uuid = "claude", plan = { feat = "Great Weapon" } })
check(j.status == "done" and j.result.set.feat == "Great Weapon Master" and j.result.set.abilities == nil,
    "named feat (prefix) chosen, no ability points")

-- 8. no plan at a feat level: Ability Improvement on the primary ability, reported as defaults
openPage(PAL)
j = run({ uuid = "claude", plan = {} })
defaulted = table.concat(j.result.defaulted or {}, ",")
check(j.status == "done" and j.result.set.feat == "Ability Improvement" and j.result.set.abilities.Strength == 2
    and defaulted:find("feat") and defaulted:find("abilities"), "defaults: Ability Improvement, primary +2, reported")

-- 9. a feat whose requirements aren't met: refused, not finished
openPage(PAL)
j = run({ uuid = "claude", plan = { feat = "Heavy Armour Master" } })
check(j.status == "failed" and tostring(j.error):find("not available") and not page.finished, "unavailable feat refused")

-- 10. the page is closed: ui.level_up opens it through the hotbar (portrait shows level up available)
closePage()
page = { spec = GALE }
opened = 0
j = run({ uuid = "gale", plan = { subclass = "Evocation School" } })
check(opened == 1 and j.status == "done" and j.result.set.subclass == "Evocation School", "opens the page itself, then levels")

-- 11. no level up available: error before anything opens
closePage()
stats.Level._p.LevelUpAvailable = false
opened = 0
j = run({ uuid = "gale", plan = { subclass = "Evocation" } })
check(j.status == "error" and tostring(j.error):find("no level up available") and opened == 0, "refused when not available")

-- 12. ui.level_up_options on a closed page reports availability
stats.Level._p.LevelUpAvailable = true
local ok, o = pcall(handlers["ui.level_up_options"], { uuid = "gale" })
check(ok and o.open == false and o.available == true, "level_up_options: closed page, available")

-- A spell pick as the bridge's click does it: the spell moves into an open Additions slot.
local function clickSpell(name)
    for i, sid in ipairs(SPELL_LISTS[page.spec.spells.list]) do
        if STATS[sid].Name == name then
            local it = page.spellSel._p.Available[i]
            for _, a in ipairs(page.spellSel._p.Additions) do
                if a._p.Spell == nil then a._p.Spell = it._p.Spell it._p.Selected = true return true end
            end
        end
    end
    return false
end
local function has(list, x) for _, v in ipairs(list or {}) do if v == x then return true end end return false end
local function anyProblem(r, pat) for _, p in ipairs(r.problems or {}) do if p:find(pat, 1, true) then return p end end end

-- 13. Gale, Wizard 2 with spells: names come from the matching SpellList (not the decoy); the known one is not
--     offered; the picks are left for the bridge's clicks and the page is not finished
local GALE_SP = { class = "Wizard", subclasses = GALE.subclasses, spells = { list = "wizard1", known = { [5] = true }, open = 2 } }
openPage(GALE_SP)
j = run({ uuid = "gale", plan = { subclass = "Evocation", spells = { "Magic Missile", "Shield" } } })
local nc = j.result.needs_clicks or {}
check(j.status == "failed" and tostring(j.error):find("need mouse clicks") and #nc == 2 and nc[1].name == "Magic Missile"
    and nc[2].name == "Shield" and not page.finished, "spells: needs_clicks for Magic Missile and Shield, not finished")
check(dc._p.SelectedSubClass ~= nil, "spells: the subclass was still chosen")
local ok13, o13 = pcall(handlers["ui.level_up_options"], { uuid = "gale" })
local sp13 = ok13 and o13.spells and o13.spells[1] or {}
check(sp13.open == 2 and has(sp13.can_pick, "Burning Hands") and has(sp13.known, "Sleep") and not has(sp13.can_pick, "Sleep"),
    "level_up_options: spell names, open slots, known spells kept apart")

-- 14. a known spell and an unknown one are problems, not clicks
j = run({ uuid = "gale", plan = { subclass = "Evocation", spells = { "Sleep", "Fireball" } } })
check(anyProblem(j.result, "spell Sleep: already known") and anyProblem(j.result, "spell Fireball: not offered")
    and j.result.needs_clicks == nil, "spells: known / not offered reported")
check(anyProblem(j.result, "needs 2 more spells in plan.spells; can pick: Burning Hands, Charm Person, Magic Missile, Shield"),
    "spells: the error lists what can be picked")

-- 15. after the clicks the same plan finishes
check(clickSpell("Magic Missile") and clickSpell("Shield"), "fake clicks placed")
j = run({ uuid = "gale", plan = { subclass = "Evocation", spells = { "Magic Missile", "Shield" } } })
check(j.status == "done" and j.result.finished and j.result.needs_clicks == nil
    and has(j.result.set.spells[1].picked, "Magic Missile") and has(j.result.set.spells[1].picked, "Shield"),
    "spells: placed picks finish the level up")

-- 16. the probe op the bridge clicks with: placed spells say so; an unknown name is an error
openPage(GALE_SP)
clickSpell("Shield")
local okp, pr = pcall(handlers["ui.level_up_spell_probe"], { name = "Shield" })
check(okp and pr.placed == true, "spell_probe: placed")
okp, pr = pcall(handlers["ui.level_up_spell_probe"], { name = "Magic Missile" })
check(okp and pr.name == "Magic Missile" and pr.open == 1 and has(pr.picked, "Shield"), "spell_probe: names the target and the picks")
okp, pr = pcall(handlers["ui.level_up_spell_probe"], { name = "Fireball" })
check(not okp and tostring(pr):find("not offered"), "spell_probe: unknown spell refused")

-- 17. Wyll, Warlock 2: no plan → fails listing the invocations (named from his class progression's list) and the
--     spells; nothing defaulted
local WYLL = { class = "Warlock", spells = { list = "warlock1", open = 1 }, passives = { list = "invocations", max = 2 } }
openPage(WYLL)
j = run({ uuid = "wyll", plan = {} })
check(j.status == "failed" and not page.finished and j.result.defaulted == nil, "warlock: no plan, nothing defaulted, not finished")
local inv = j.result.options and j.result.options["Warlock invocations"]
check(inv and #inv == 4 and inv[1] == "Agonising Blast",
    "warlock: invocation options named from the PassiveList")
-- warlock1 and other3 share the level/school sequence: the page's 3 spells stay unnamed rather than guessed
check(anyProblem(j.result, "needs 1 more spell") and anyProblem(j.result, "#1 (level 1 Enchantment)"),
    "warlock: ambiguous spell list not guessed")

-- 18. a plan with invocations (US spelling) toggles them; the ambiguous spell list can't be picked by name
SPELL_LISTS.other3 = nil
BG3AI.Progression.clearNameCaches()
openPage(WYLL)
j = run({ uuid = "wyll", plan = { passives = { "Agonizing Blast", "Repelling Blast" }, spells = { "Hex" } } })
local pv = page.passiveSel._p.Passives
check(pv[1]._p.Value == 1 and pv[3]._p.Value == 1 and pv[2]._p.Value == 0, "warlock: Agonizing (= Agonising) and Repelling Blast toggled")
check(j.result.needs_clicks and j.result.needs_clicks[1].name == "Hex", "warlock: Hex left for a click")
clickSpell("Hex")
j = run({ uuid = "wyll", plan = { passives = { "Agonizing Blast", "Repelling Blast" }, spells = { "Hex" } } })
check(j.status == "done" and j.result.finished and pv[1]._p.Value == 1 and pv[3]._p.Value == 1,
    "warlock: rerun keeps the invocations on (no toggling off) and finishes")
check(j.result.set.passives and j.result.set.passives[1].title == "Warlock invocations", "warlock: passives reported with the list's title")

-- 19. allow_defaults fills an invocation selector in list order
openPage(WYLL)
j = run({ uuid = "wyll", plan = { allow_defaults = true, spells = { "Hex" } } })
check(page.passiveSel._p.Passives[1]._p.Value == 1 and page.passiveSel._p.Passives[2]._p.Value == 1
    and table.concat(j.result.defaulted or {}, ","):find("Agonising Blast"), "allow_defaults: first invocations, reported")

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
