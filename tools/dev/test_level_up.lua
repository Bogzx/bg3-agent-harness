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

Ext = { Entity = { Get = function(u) return { ClientControl = true, Health = { Hp = 16, MaxHp = 16 }, EocLevel = { Level = 1 } } end },
        Input = { InjectKeyPress = function() end } }

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

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
