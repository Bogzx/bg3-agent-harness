-- Offline test: the CC driver (Client/Progression.lua + ProgressionCC.lua) on the respec page. Osi.StartRespec on a
-- real party member opens "CharacterRespec" (route H: a respecced hireling is an arena build's reference); the driver
-- finds the page through P.CC_PAGES, by the CharacterRespec names (tabs, finish command, complete flag) read from the
-- game's xaml and the route-H page dump. A name unknown fails its op at the call, nothing changed on the page; known,
-- ui.cc_apply drives the respec page with its own names and leaves the race and background alone (respec keeps them).
-- A fake page and clock, no game. Run: tools/dev/test_cc_respec.sh
local L = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local fails = 0
local function check(cond, what)
    if not cond then fails = fails + 1 print("FAIL " .. what) else print("ok   " .. what) end
end

local now = 0
local function obj(type_, props, onset)
    local o = { _t = type_, _p = props or {}, _onset = onset }
    function o.GetProperty(self, k) return self._p[k] end
    function o.SetProperty(self, k, v)
        if self._onset then self._onset(self, k, v) else self._p[k] = v end
    end
    return o
end

local NAMES = {}
local function vms(list_)
    local out = {}
    for i, n in ipairs(list_) do
        out[i] = obj("ls.VM", {})
        NAMES[out[i]] = n
    end
    return out
end
local RACES, CLASSES = vms({ "Human", "Drow" }), vms({ "Wizard", "Fighter" })
local BACKGROUNDS = vms({ "Sage", "Soldier" })
local TAB_LIST = { raceTab = "SelectableRaces", classTab = "SelectableClasses",
    backgroundTab = "SelectableBackgrounds" }

local widgets, dc, page, tabs = {}, nil, nil, {}
-- One open page named `name`, its tabs called by `tabNames` (cc tab name -> element name), complete via `complete`.
local function open(name, tabNames, complete)
    page = { name = name, commands = {}, shown = nil, sets = {} }
    local props = { SelectableClasses = CLASSES, SelectedClass = CLASSES[1], SelectableBackgrounds = BACKGROUNDS,
        SelectedBackground = BACKGROUNDS[1], SelectableSubClasses = {}, SelectableDeities = {},
        SelectedRace = RACES[2] }
    if name == "CharacterCreation" then
        props.SelectableRaces = RACES
        props.SelectableOrigins = { obj("ls.VM", { IsCustom = true }) }
    end
    props[complete] = true
    dc = obj("ls.DC" .. name, props, function(self, k, v)
        page.sets[#page.sets + 1] = k
        self._p[k] = v
    end)
    widgets = { { name = name, el = obj("ls.UIWidget", {}) } }
    widgets[1].el.DataContext = dc
    tabs = {}
    for cc, el in pairs(tabNames) do
        tabs[el] = obj("ListBoxItem", { Visibility = "Visible" }, function(_, k, v)
            if k == "IsSelected" and v then page.shown = cc page.sets[#page.sets + 1] = el end
        end)
    end
    BG3AI.Progression.rendered, BG3AI.Progression.renderedKey = nil, nil
end

-- The shown tab's list items, all rendered.
local function renderedItems()
    local coll = page.shown and TAB_LIST[page.shown] and dc:GetProperty(TAB_LIST[page.shown]) or nil
    local out = {}
    for _, it in ipairs(coll or {}) do
        out[#out + 1] = obj("ListBoxItem", { DataContext = it, _tb = obj("TextBlock", { _text = NAMES[it] }) })
    end
    return out
end

BG3AI = {}
local handlers, jobs = {}, {}
BG3AI.RPC = {
    register = function(n, f) handlers[n] = f end,
    now_ms = function() return now end,
    start_job = function(kind) local j = { id = #jobs + 1, kind = kind } jobs[#jobs + 1] = j return j end,
    finish_job = function(j, st, res, err) j.status, j.result, j.error = st, res, err end,
}
BG3AI.UI = {
    pcall = pcall,
    prop = function(o, k) if o == nil then return nil end return o:GetProperty(k) end,
    props = function(o) return o and o._p or {} end,
    typeName = function(o) return type(o) == "table" and o._t or nil end,
    text = function(o) return o and o._p and o._p._text or nil end,
    clean = function(s) return s end,
    widgets = function() return widgets end,
    widget = function(n) return widgets[1] and widgets[1].name == n and widgets[1].el or nil end,
    isVisible = function() return true end,
    cachedByName = function(w, n) return widgets[1] and widgets[1].name == w and tabs[n] or nil end,
    byName = function() return nil end,
    command = function(_, n)
        page.commands[#page.commands + 1] = n
        return true
    end,
    items = function(coll)
        local i = 0
        return function()
            i = i + 1
            if type(coll) == "table" and coll[i] ~= nil then return i, coll[i] end
            return nil
        end
    end,
    count = function(coll) return type(coll) == "table" and #coll or 0 end,
    path = function() return nil end,
    findAll = function(root, _)
        if widgets[1] and root == widgets[1].el then return renderedItems() end
        if root and root._p and root._p._tb then return { root._p._tb } end
        return {}
    end,
}
Ext = { Require = function(p) return dofile(L .. "/" .. p) end } -- Progression.lua loads its parts
dofile(L .. "/Client/Progression.lua")
local P = BG3AI.Progression
local RESPEC = P.CC_PAGES[2]
local REAL = { tabs = RESPEC.tabs, finish = RESPEC.finish, complete = RESPEC.complete }

local CC_TABS = { originTab = "originTab", raceTab = "raceTab", subRaceTab = "subRaceTab", classTab = "classTab",
    subClassTab = "subClassTab", deityTab = "deityTab", backgroundTab = "backgroundTab", abilityTab = "abilityTab" }
-- Made-up names: the test only needs them to differ from character creation's
local DUMP_TABS = { classTab = "respecClassTab", subClassTab = "respecSubClassTab", deityTab = "respecDeityTab",
    abilityTab = "respecAbilityTab" }
local function filled(on)
    if on then
        RESPEC.tabs, RESPEC.finish, RESPEC.complete = DUMP_TABS, "FinishRespecDump", "IsRespecCompleteDump"
    else
        RESPEC.tabs, RESPEC.finish, RESPEC.complete = nil, nil, nil
    end
end

local function run(op, args)
    local ok, res = pcall(handlers[op], args)
    if not ok then return nil, tostring(res) end
    local j = res and res.job and jobs[res.job]
    for _ = 1, 4000 do
        if not j or j.status then break end
        now = now + 16
        j.on_tick(j)
    end
    return j or res
end

local PLAN = { class = "Fighter", abilities = "recommended" }

-- 1. the names as read live (Game.pak CharacterRespec.xaml, route-H window 3); an unknown name fails at the call
check(REAL.finish == "FinishCreating" and REAL.complete == "IsCharacterCompleteExceptName"
    and REAL.tabs.classTab == "classTab" and REAL.tabs.subClassTab == "subClassTab"
    and REAL.tabs.deityTab == "deityTab" and REAL.tabs.abilityTab == "abilityTab" and REAL.tabs.backgroundTab == nil,
    "CharacterRespec's names")
check(RESPEC.keeps.background == true, "respec keeps the background: the page has no tab for it")
filled(false)
open("CharacterRespec", DUMP_TABS, "IsRespecCompleteDump")
local j, err = run("ui.cc_apply", { plan = PLAN })
check(j == nil and err:find("CharacterRespec: its classTab name is not known yet", 1, true) ~= nil,
    "unknown tabs: cc_apply refused at the call (" .. tostring(err) .. ")")
check(not err:find("TODO", 1, true), "the tab answer is a fact, no note of ours (" .. tostring(err) .. ")")
check(#page.sets == 0 and #page.commands == 0 and #jobs == 0, "nothing set, no command, no job")
RESPEC.tabs = DUMP_TABS
j, err = run("ui.cc_apply", { plan = PLAN })
check(j == nil and err:find("CharacterRespec: its complete name is not known yet", 1, true) ~= nil,
    "unknown complete flag: refused (" .. tostring(err) .. ")")
j, err = run("ui.cc_finish", {})
check(j == nil and err:find("CharacterRespec: its finish name is not known yet", 1, true) ~= nil,
    "unknown finish command: cc_finish refused (" .. tostring(err) .. ")")
check(#page.sets == 0 and #page.commands == 0, "still nothing touched")
check(not err:find("TODO", 1, true), "the answer is a fact, no note of ours (" .. tostring(err) .. ")")
filled(false)

-- 2. names filled in from the dump: cc_apply drives the respec page with its own tabs and finish command
filled(true)
open("CharacterRespec", DUMP_TABS, "IsRespecCompleteDump")
j = run("ui.cc_apply", { plan = PLAN })
check(j and j.status == "done", "respec: cc_apply done (" .. tostring(j and j.error) .. ")")
check(j.result.page == "CharacterRespec", "respec: result names the page")
check(j.result.set.class == "Fighter", "respec: class picked")
local cmds = table.concat(page.commands, ",")
check(cmds:find("FinishRespecDump", 1, true) ~= nil and cmds:find("FinishCreating", 1, true) == nil,
    "respec: its own finish command (" .. cmds .. ")")
local sets = table.concat(page.sets, ",")
check(sets:find("respecClassTab", 1, true) and not sets:find("raceTab", 1, true)
    and not sets:find("SelectedRace", 1, true) and not sets:find("SelectedOrigin", 1, true)
    and not sets:find("SelectedBackground", 1, true),
    "respec: its own tabs, no origin/race/background step (" .. sets .. ")")
check(NAMES[dc:GetProperty("SelectedRace")] == "Drow", "respec: the race kept")

-- 3. a plan that sets what respec keeps, or names the character, is refused at the call
for _, bad in ipairs({ { race = "Human" }, { subrace = "x" }, { origin = "Custom" }, { background = "Sage" } }) do
    local key = next(bad)
    open("CharacterRespec", DUMP_TABS, "IsRespecCompleteDump")
    j, err = run("ui.cc_apply", { plan = bad })
    check(j == nil and err:find("CharacterRespec keeps the " .. key, 1, true) ~= nil, "respec keeps the " .. key)
end
j, err = run("ui.cc_apply", { plan = { class = "Fighter", name = "Red Ash" } })
check(j == nil and err:find("CharacterRespec has no naming page", 1, true) ~= nil, "respec: no naming in a plan")
j, err = run("ui.cc_name", {})
check(j == nil and err:find("CharacterRespec has no naming page", 1, true) ~= nil, "respec: no ui.cc_name")
j = run("ui.cc_finish", {})
check(j and j.finished == true and j.page == "CharacterRespec" and page.commands[1] == "FinishRespecDump",
    "respec: cc_finish runs the page's finish command")
local o = run("ui.cc_options", {})
check(o and o.page == "CharacterRespec" and o.complete == true, "respec: cc_options names the page, reads its flag")
local okp, pe = pcall(handlers["ui.level_up_spell_probe"], { name = "Bless" })
check(not okp and tostring(pe):find("spell Bless: not offered", 1, true) ~= nil,
    "respec: the spell probe reads its page (" .. tostring(pe) .. ")")
filled(false)

-- 4. character creation is unchanged: its own tabs, FinishCreating, IsCharacterComplete
open("CharacterCreation", CC_TABS, "IsCharacterComplete")
j = run("ui.cc_apply", { plan = { race = "Human", class = "Fighter", background = "Soldier" } })
check(j and j.status == "done" and j.result.page == "CharacterCreation", "CC: done (" .. tostring(j and j.error) .. ")")
check(page.commands[#page.commands] == "FinishCreating", "CC: FinishCreating")
check(table.concat(page.sets, ","):find("raceTab", 1, true) ~= nil, "CC: the race tab shown")
check(P.ccPage() == P.CC_PAGES[1], "CC: the page found is character creation")

-- 5. expertise is picked in the expertise group: its skills are proficient ones, so the class group lists them too and
-- a plain search stops there (a Rogue's Perception reads selected, nothing happens; Stealth is toggled as a class pick)
do
    local function skill(name, selected)
        return obj("ls.VM", { Name = name, Selected = selected })
    end
    local class = { skill("Perception", true), skill("Stealth", false) }
    local exp = { skill("Perception", false), skill("Stealth", false) }
    local groups = { ["AllSkills.ClassProficientSkills"] = obj("ls.VM", { Skills = class }),
        ["AllSkills.ExpertiseSkills"] = obj("ls.VM", { Skills = exp }) }
    local toggled, path, command = {}, BG3AI.UI.path, BG3AI.UI.command
    BG3AI.UI.path = function(_, p) return groups[p] end
    BG3AI.UI.command = function(_, n, item)
        if n == "ToggleSkill" then toggled[#toggled + 1] = item end
        return true
    end
    local ctx = { result = { problems = {}, set = {} }, fast = true }
    P.inner.skillsStage(function() return {} end, ctx, { "Perception", "Stealth" }, "expertise")()
    check(#toggled == 2 and toggled[1] == exp[1] and toggled[2] == exp[2] and #ctx.result.problems == 0,
        "expertise: both picked in the expertise group, the class group untouched")
    P.inner.skillsStage(function() return {} end, ctx, { "Athletics" }, "expertise")()
    check(ctx.result.problems[1] == "skill Athletics: not offered", "expertise: one not in its group is a problem")
    local to, wait = P.inner.skillsStage(function() return {} end, ctx, nil, "expertise")()
    check(to == "next" and wait == 0, "expertise: no pick, no settle wait on a normal level-up")
    BG3AI.UI.path, BG3AI.UI.command = path, command
end

-- 6. the point buy on the respec page, from live 2026-11-06 launch 3 (cc-options-fighter-stop.json): a hireling's
-- +2 Str and +1 Con stay through ResetAbilities and sit in Value, RaceBonus reads nothing, Improvement marks the pick.
do
    local NAMES6 = { "Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma" }
    local function cost(n) return n <= 13 and n - 8 or 5 + (n - 13) * 2 end
    local function page6(start, picks, read)
        local p = { abilities = {}, base = {}, cols = {}, refuse = {} }
        for _, n in ipairs(NAMES6) do
            p.base[n] = start[n]
            p.abilities[#p.abilities + 1] = obj("ls.VM", { Ability = n, CanIncrease = true, CanDecrease = true })
        end
        for bonus, on in pairs(picks) do
            local items = {}
            for _, n in ipairs(NAMES6) do
                items[#items + 1] = obj("ls.VM", { Ability = n, Improvement = read and (n == on and 1 or 0) or nil })
            end
            p.cols[#p.cols + 1] = obj("ls.VM", { AbilityBonus = bonus, BonusAbilities = items, _on = on })
        end
        function p.sync()
            local spent = 0
            for _, a in ipairs(p.abilities) do
                local n, add = a._p.Ability, 0
                for _, c in ipairs(p.cols) do
                    if c._p._on == n then add = add + c._p.AbilityBonus end
                    for _, it in ipairs(c._p.BonusAbilities) do
                        if read then it._p.Improvement = it._p.Ability == c._p._on and 1 or 0 end
                    end
                end
                a._p.Value = p.base[n] + add
                spent = spent + cost(p.base[n])
            end
            dc._p.UnusedAbilityPoints = 27 - spent
        end
        return p
    end
    local function apply(p, plan)
        open("CharacterRespec", REAL.tabs, REAL.complete)
        p.sync()
        local path, command = BG3AI.UI.path, BG3AI.UI.command
        BG3AI.UI.path = function(_, k)
            if k == "DummyCharacter.Stats.Abilities" then return p.abilities end
            if k == "RaceProgressionDetails.AbilityBonusSelection" then return p.cols end
            return nil
        end
        BG3AI.UI.command = function(_, n, a)
            if n == "ResetAbilities" then
                for k in pairs(p.base) do p.base[k] = 8 end
            elseif n == "SelectNextAbilityBonus" then
                if p.stuck then return true end -- the live press: ok, and nothing moves
                local at = 0
                for i, x in ipairs(NAMES6) do if x == a._p._on then at = i end end
                a._p._on = NAMES6[at % #NAMES6 + 1]
            elseif n == "IncreaseAbility" or n == "DecreaseAbility" then
                local name = a._p.Ability
                if p.refuse[name] and p.base[name] >= p.refuse[name] then return false, "not allowed" end
                p.base[name] = p.base[name] + (n == "IncreaseAbility" and 1 or -1)
            end
            p.sync()
            return true
        end
        RESPEC.tabs, RESPEC.finish, RESPEC.complete = REAL.tabs, REAL.finish, REAL.complete
        local res = run("ui.cc_apply", { plan = plan, finish = false })
        BG3AI.UI.path, BG3AI.UI.command = path, command
        return res
    end
    local WANT = { Strength = 15, Dexterity = 13, Constitution = 14, Intelligence = 10, Wisdom = 12, Charisma = 8 }
    local STOP = { Strength = 13, Dexterity = 13, Constitution = 13, Intelligence = 10, Wisdom = 12, Charisma = 8 }
    local plan = { class = "Fighter", abilities = WANT }

    local p = page6(STOP, { [2] = "Strength", [1] = "Constitution" }, true)
    local r = apply(p, plan)
    check(r and r.status == "done", "point buy: done (" .. tostring(r and r.error) .. ")")
    check(p.base.Strength == 15 and p.base.Constitution == 14 and p.abilities[1]._p.Value == 17
        and p.abilities[3]._p.Value == 15, "point buy: 15 and 14 bought under the kept +2 and +1, 17 and 15 shown")
    check(dc._p.UnusedAbilityPoints == 0 and #r.result.problems == 0, "point buy: every point spent, no problem")
    local picked = {}
    for _, c in ipairs(r.result.set.abilities.bonus) do picked[c.bonus] = c.picked end
    check(picked[2] == "Strength" and picked[1] == "Constitution", "point buy: the view reads the picks by Improvement")

    p = page6(STOP, { [2] = "Strength", [1] = "Constitution" }, false)
    r = apply(p, plan)
    check(table.concat(r.result.problems, ";"):find("abilities: 6 points left", 1, true) ~= nil,
        "point buy: a pick that doesn't read stops as live did, now named")

    p = page6(STOP, { [2] = "Strength", [1] = "Constitution" }, true)
    p.refuse.Strength = 13
    r = apply(p, plan)
    check(table.concat(r.result.problems, ";"):find("Strength bought 13, wanted 15: not allowed", 1, true) ~= nil,
        "point buy: a press the page refuses is named (" .. table.concat(r.result.problems, ";") .. ")")

    -- the Rogue's page, cc-options-rogue-stop.json: +2 Dexterity stays, the +1 moves off Intelligence
    local ROGUE = { Strength = 8, Dexterity = 15, Constitution = 14, Intelligence = 12, Wisdom = 13, Charisma = 10 }
    local rplan = { class = "Fighter", abilities = ROGUE, bonus = { "Dexterity", "Constitution" } }
    p = page6(ROGUE, { [2] = "Dexterity", [1] = "Intelligence" }, true)
    r = apply(p, rplan)
    check(#r.result.problems == 0 and p.abilities[3]._p.Value == 15 and p.abilities[4]._p.Value == 12,
        "bonus: the +1 moved to Constitution (" .. table.concat(r.result.problems, ";") .. ")")
    local got = {}
    for _, c in ipairs(r.result.set.abilities.bonus) do got[c.bonus] = c.picked end
    check(got[2] == "Dexterity" and got[1] == "Constitution", "bonus: the page reads the plan's picks")
    check(r.result.before.abilities.bonus ~= nil, "cc_apply: keeps what the page opened with")
    p = page6(ROGUE, { [2] = "Dexterity", [1] = "Intelligence" }, true)
    p.stuck = true
    r = apply(p, rplan)
    local probs = table.concat(r.result.problems, ";")
    check(probs:find("bonus Constitution: not picked after 6 presses", 1, true) ~= nil
        and probs:find("bonus Constitution: the page has Intelligence", 1, true) ~= nil,
        "bonus: a column that won't move is named (" .. probs .. ")")

    -- skills and expertise, as that read: every group full of the hireling's picks
    local CLASS = { "Deception", "Intimidation", "Performance", "Acrobatics", "Investigation", "Athletics", "Insight",
        "Perception", "SleightOfHand", "Stealth" }
    local BACKGROUND = { SleightOfHand = true, Stealth = true }
    local function skillPage()
        local q = { own = { class = { Deception = true, Performance = true, Acrobatics = true, Athletics = true },
            race = { Persuasion = true }, expertise = { SleightOfHand = true, Stealth = true } },
            max = { class = 4, race = 1, expertise = 2 }, groups = {} }
        local function group(key, names)
            local items = {}
            for i, n in ipairs(names) do items[i] = obj("ls.VM", { Name = n, _g = key }) end
            q.groups[key] = obj("ls.VM", { Skills = items, MaxSelectedSkillCount = q.max[key] })
        end
        group("class", CLASS)
        group("race", { "Persuasion", "Athletics", "Perception", "Investigation" })
        group("expertise", { "SleightOfHand", "Stealth", "Perception", "Athletics" })
        function q.sync()
            local function has(n) return BACKGROUND[n] or q.own.class[n] or q.own.race[n] end
            for key, g in pairs(q.groups) do
                for _, it in ipairs(g._p.Skills) do
                    local n = it._p.Name
                    if key == "expertise" then
                        it._p.Selected, it._p.Enabled = q.own.expertise[n] == true, has(n) == true
                    else
                        it._p.Selected = has(n) == true
                        local other = key == "class" and "race" or "class"
                        it._p.Enabled = not (BACKGROUND[n] or q.own[other][n])
                    end
                end
            end
        end
        function q.toggle(it)
            local key, n = it._p._g, it._p.Name
            if it._p.Enabled == false or q.stuck then return end
            local count = 0
            for _ in pairs(q.own[key]) do count = count + 1 end
            if q.own[key][n] then q.own[key][n] = nil
            elseif count < q.max[key] then q.own[key][n] = true end -- a full group ignores it, and says nothing
            q.sync()
        end
        q.sync()
        return q
    end
    local function applySkills(q, skillPlan)
        open("CharacterRespec", REAL.tabs, REAL.complete)
        local path, command = BG3AI.UI.path, BG3AI.UI.command
        BG3AI.UI.path = function(_, k)
            if k == "AllSkills.ClassProficientSkills" then return q.groups.class end
            if k == "AllSkills.RaceProficientSkills" then return q.groups.race end
            if k == "AllSkills.ExpertiseSkills" then return q.groups.expertise end
            if k == "ClassProgressionDetails.NotSubPassiveSelectors" then return q.selectors end
            if k == "ClassProgressionDetails.SpellSelectors" then return q.spellSelectors end
            return nil
        end
        BG3AI.UI.command = function(_, n, it)
            if n == "ToggleSkill" then q.toggle(it)
            elseif n == "TogglePassive" and q.passive then q.passive(it) end
            return true
        end
        RESPEC.tabs, RESPEC.finish, RESPEC.complete = REAL.tabs, REAL.finish, REAL.complete
        local res = run("ui.cc_apply", { plan = skillPlan, finish = false })
        BG3AI.UI.path, BG3AI.UI.command = path, command
        return res
    end
    local SKILLS = { class = "Fighter", class_skills = { "Acrobatics", "Perception", "Deception", "Investigation" },
        race_skills = { "Athletics" }, expertise = { "Stealth", "Perception" } }
    local q = skillPage()
    r = applySkills(q, SKILLS)
    check(#r.result.problems == 0, "skills: no problem (" .. table.concat(r.result.problems, ";") .. ")")
    check(q.own.class.Performance == nil and q.own.class.Perception and q.own.class.Investigation
        and q.own.race.Athletics and q.own.race.Persuasion == nil, "skills: class and race picks moved to the plan's")
    check(q.own.expertise.SleightOfHand == nil and q.own.expertise.Perception and q.own.expertise.Stealth,
        "expertise: moved, once Perception is proficient")

    q = skillPage()
    q.stuck = true
    r = applySkills(q, SKILLS)
    probs = table.concat(r.result.problems, ";")
    for _, want in ipairs({ "skill Perception: not on the page (class)", "skill Investigation: not on the page (class)",
        "skill Athletics: not on the page (race)", "skill Perception: not on the page (expertise)" }) do
        check(probs:find(want, 1, true) ~= nil, "end check names " .. want)
    end

    -- a full selector ignores the toggle as a skill group does: the Ranger's favoured enemy
    Ext.StaticData = { Get = function() return nil end, GetAll = function() return {} end }
    q = skillPage()
    local function passive(n, v) return obj("ls.VM", { Name = n, Value = v, Enabled = true }) end
    local items = { passive("Sanctified Stalker", 1), passive("Bounty Hunter", 0) }
    q.selectors = { obj("ls.VM", { Passives = items, MaxSelectedPassiveCount = 1, IsComplete = true }) }
    q.passive = function(it)
        local on = 0
        for _, x in ipairs(items) do on = on + x._p.Value end
        if it._p.Value == 1 then it._p.Value = 0 elseif on < 1 then it._p.Value = 1 end
    end
    r = applySkills(q, { class = "Fighter", passives = { "Bounty Hunter" } })
    check(items[2]._p.Value == 1 and items[1]._p.Value == 0 and #r.result.problems == 0,
        "passives: a selector's pick moved (" .. table.concat(r.result.problems, ";") .. ")")
    q.passive, items[1]._p.Value, items[2]._p.Value = function() end, 1, 0
    r = applySkills(q, { class = "Fighter", passives = { "Bounty Hunter" } })
    check(table.concat(r.result.problems, ";"):find("passive Bounty Hunter: not on the page", 1, true) ~= nil,
        "end check names a passive that didn't land")

    -- a Ranger's page (live 2026-11-06, cc-options-ranger-stop.json): two selectors of 5 unnamed entries, the
    -- hireling's picks #3 and #1. Lists of one length: only the row's ids tell them apart.
    local LISTS = { enemy = { Passives = { "Bounty Hunter", "Keeper", "Mage Breaker", "Knight", "Stalker" } },
        land = { Passives = { "Beast Tamer", "Urban Tracker", "Cold", "Fire", "Poison" } } }
    Ext.StaticData = { Get = function(id) return LISTS[id] end, GetAll = function() return {} end }
    Ext.Stats = { Get = function() return nil end }
    local function ranger(first, second)
        local function sel(on)
            local its = {}
            for i = 1, 5 do its[i] = obj("ls.VM", { Value = i == on and 1 or 0, Enabled = true }) end
            return obj("ls.VM", { Passives = its, MaxSelectedPassiveCount = 1, IsComplete = true })
        end
        local page2 = skillPage()
        page2.selectors = { sel(first), sel(second) }
        page2.passive = function(it)
            for _, s in ipairs(page2.selectors) do
                local on, mine = 0, false
                for _, x in ipairs(s._p.Passives) do on, mine = on + x._p.Value, mine or x == it end
                if mine then
                    if it._p.Value == 1 then it._p.Value = 0 elseif on < 1 then it._p.Value = 1 end
                end
            end
        end
        return page2
    end
    local PICKS = { "Bounty Hunter", "Beast Tamer" }
    P.clearNameCaches()
    q = ranger(3, 1)
    r = applySkills(q, { class = "Fighter", passives = PICKS, passive_lists = { "enemy", "land" } })
    probs = table.concat(r.result.problems, ";")
    local enemy, land = q.selectors[1]._p.Passives, q.selectors[2]._p.Passives
    check(r.status == "done" and enemy[1]._p.Value == 1 and enemy[3]._p.Value == 0 and land[1]._p.Value == 1
        and probs == "",
        "passives: lists of one length, each selector named by the row's list (" .. probs .. ")")
    P.clearNameCaches()
    r = applySkills(ranger(3, 1), { class = "Fighter", passives = PICKS })
    check(table.concat(r.result.problems, ";"):find("passive Bounty Hunter: not offered", 1, true) ~= nil,
        "passives: without the row's lists the same page stays unnamed")
    P.clearNameCaches()
    r = applySkills(ranger(3, 1), { class = "Fighter", passives = PICKS, passive_lists = { "enemy" } })
    check(table.concat(r.result.problems, ";"):find("the page has 2 selectors, the row 1", 1, true) ~= nil,
        "passives: selectors the row doesn't account for are named")
    check(r.result.before and #r.result.before.passives == 2 and r.result.before.passives[1].picked[1] == "#3",
        "before: the page's own passive picks")
    Ext.StaticData, Ext.Stats = { Get = function() return nil end, GetAll = function() return {} end }, nil
    P.clearNameCaches()

    r = applySkills(skillPage(), { class = "Fighter", spells = { "Bless" } })
    check(table.concat(r.result.problems, ";"):find("spell Bless: not on the page", 1, true) ~= nil,
        "end check names a spell that didn't land")

    -- a hireling's own spell picks: Lua can't press either button, so they go to the bridge, off before on
    local function spell(n) return obj("ls.VM", { Name = n, Spell = obj("ls.VM", {}) }) end
    local function selector(picks, offered, cantrips)
        local s = { IsCantrips = cantrips, Additions = {}, Available = {}, IsComplete = true }
        for i, n in ipairs(picks) do s.Additions[i] = spell(n) end
        for i, n in ipairs(offered) do s.Available[i] = spell(n) end
        return obj("ls.VM", s)
    end
    local function needsOf(res)
        local out = {}
        for _, nd in ipairs(res.result.needs_clicks or {}) do
            out[#out + 1] = (nd.remove and "-" or "+") .. nd.name
        end
        return table.concat(out, " ")
    end
    Ext.StaticData = { Get = function() return nil end, GetAll = function() return {} end }
    q = skillPage()
    local book = selector({ "Shield", "Sleep" }, { "Shield", "Sleep", "Magic Missile" }, false)
    q.spellSelectors = { selector({ "Light" }, { "Light", "Fire Bolt" }, true), book }
    r = applySkills(q, { class = "Fighter", spells = { "Shield", "Magic Missile" }, cantrips = { "Fire Bolt" } })
    check(needsOf(r) == "-Light -Sleep +Fire Bolt +Magic Missile", "spells: own picks off first (" .. needsOf(r) .. ")")
    check(table.concat(r.result.problems, ";"):find("not on the page", 1, true) == nil,
        "spells: a pick waiting for its click isn't missing (" .. table.concat(r.result.problems, ";") .. ")")
    r = applySkills(q, { class = "Fighter", spells = { "Shield", "Sleep" } })
    check(needsOf(r) == "", "spells: a kind the plan doesn't name stays, a plan already on the page clicks nothing ("
        .. needsOf(r) .. ")")
    -- a hireling's picks: other VMs than Available's, so no name; Available says what's held (live L5 launch 2)
    local function unnamed() return obj("ls.VM", { Spell = obj("ls.VM", {}) }) end
    local function held(names, offered)
        local sel = selector({}, offered, false)
        sel._p.Additions = {}
        for i, n in ipairs(names) do
            sel._p.Additions[i] = unnamed()
            for _, it in ipairs(sel._p.Available) do
                if it._p.Name == n then it._p.Selected = true end
            end
        end
        sel._p.Additions[#names + 1] = obj("ls.VM", {}) -- an empty slot is no pick
        return sel
    end
    q.spellSelectors = { held({ "Shield", "Sleep" }, { "Shield", "Sleep", "Magic Missile" }) }
    r = applySkills(q, { class = "Fighter", spells = { "Shield", "Magic Missile" } })
    local needs = needsOf(r)
    check(select(2, needs:gsub("%-own pick %d+", "")) == 2 and needs:find("+Magic Missile", 1, true) ~= nil
        and needs:find("-", 1, true) == 1, "spells: every unnamed pick off when one isn't the plan's (" .. needs .. ")")
    check(#r.result.problems == 0, "spells: no problem (" .. table.concat(r.result.problems, ";") .. ")")
    local first = r.result.needs_clicks[1].name
    local _, found = BG3AI.Progression.findPick(obj("ls.DC", {}), first, false)
    check(found == nil, "the made-up name only reads where the page has it")
    local path0 = BG3AI.UI.path
    BG3AI.UI.path = function(_, k) if k == "ClassProgressionDetails.SpellSelectors" then return q.spellSelectors end end
    _, found = BG3AI.Progression.findPick(obj("ls.DC", {}), first, false)
    BG3AI.UI.path = path0
    check(found == q.spellSelectors[1]._p.Additions[1], "the probe finds a pick by its made-up name")
    -- taken off, the slot stays in Additions with Spell nil and the plan's next spell can land in it (live 22:40)
    local slot = found
    slot._p.Spell = nil
    BG3AI.UI.path = function(_, k) if k == "ClassProgressionDetails.SpellSelectors" then return q.spellSelectors end end
    _, found = BG3AI.Progression.findPick(obj("ls.DC", {}), first, false)
    BG3AI.UI.path = path0
    check(found == nil, "a pick that came off reads as off though its slot stays")
    slot._p.Spell = obj("ls.VM", {})
    q.spellSelectors[1]._p.Additions[2]._p.Spell = nil
    for _, it in ipairs(q.spellSelectors[1]._p.Available) do it._p.Selected = it._p.Name == "Magic Missile" end
    r = applySkills(q, { class = "Fighter", spells = { "Shield", "Magic Missile" } })
    check(needsOf(r) == "+Shield", "the next pass leaves a refilled slot on and adds the rest (" .. needsOf(r) .. ")")
    q.spellSelectors = { held({ "Shield", "Sleep" }, { "Shield", "Sleep", "Magic Missile" }) }
    r = applySkills(q, { class = "Fighter", spells = { "Shield", "Sleep" } })
    check(needsOf(r) == "", "spells: unnamed picks that are all the plan's stay (" .. needsOf(r) .. ")")

    local UIm = BG3AI.UI
    local path, findAll, name = UIm.path, UIm.findAll, UIm.name
    q.spellSelectors = { book }
    -- the sub-tab opens its panel and clears its own selection (CCLib_k.xaml): IsSelected never reads true
    local panel = false
    local sub = obj("ListBoxItem", { DataContext = book, IsSelected = false }, function(_, k, v)
        if k == "IsSelected" and v then panel = true end
    end)
    sub.name = "tab"
    local buttons = { list = obj("ls.VM", {}) }
    buttons.list.name = "subTabsSpellsList"
    local function button(n, it)
        local cell = obj("ls.VM", { Content = it, ActualWidth = 88, ActualHeight = 88 })
        local b = obj("ls.VM", { TemplatedParent = cell })
        b.name = n
        return b
    end
    local all = { buttons.list, sub, button("DeselectAddedSpell", book._p.Additions[1]),
        button("DeselectAddedSpell", book._p.Additions[2]), button("SelectAvailableSpell", book._p.Available[3]) }
    UIm.path = function(_, k) if k == "ClassProgressionDetails.SpellSelectors" then return q.spellSelectors end end
    UIm.name = function(e) return e.name end
    UIm.findAll = function(_, pred)
        local out = {}
        for _, e in ipairs(all) do
            local isButton = e ~= sub and e ~= buttons.list
            if e == sub or (e ~= sub and pred(e) and (panel or not isButton)) then out[#out + 1] = e end
        end
        return out
    end
    local probe = handlers["ui.level_up_spell_probe"]
    tabs.classTab._p.IsSelected, page.shown = false, nil
    local p1 = probe({ name = "Sleep", remove = true })
    check(p1.switched == true and page.shown == "classTab", "probe: the class tab first, from the ability tab")
    tabs.classTab._p.IsSelected = true
    local p2 = probe({ name = "Sleep", remove = true })
    check(p2.switched == true and panel, "probe: then the sub-tab")
    p2 = probe({ name = "Sleep", remove = true })
    check(p2.switched == nil and p2.index == 2 and p2.n == 2, "probe: the panel's up, the sub-tab reads unselected: "
        .. "the pick's own button, among the Additions' only (" .. tostring(p2.index) .. "/" .. tostring(p2.n) .. ")")
    book._p.Additions[2] = nil
    check(probe({ name = "Sleep", remove = true }).placed == true, "probe: a pick that's off says so")
    UIm.path, UIm.findAll, UIm.name = path, findAll, name
    filled(false)
end

-- The instrument: an open selector takes its first entry, a full one is left, a new game's page isn't touched
do
    local function apply(name, have)
        local names = name == "CharacterRespec" and REAL.tabs or { classTab = "classTab" }
        open(name, names, name == "CharacterRespec" and REAL.complete or "IsCharacterComplete")
        local lute, flute = obj("ls.VM", {}), obj("ls.VM", {})
        local sel = obj("ls.VMSel", { SelectedEquipmentCount = have, MaxSelectedEquipmentCount = 1,
            Equipments = { lute, flute } })
        local path, command = BG3AI.UI.path, BG3AI.UI.command
        local took = {}
        BG3AI.UI.path = function(_, k)
            if k == "ClassProgressionDetails.NotSubEquipmentSelectors" then return { sel } end
            return nil
        end
        BG3AI.UI.command = function(_, n, it)
            if n == "ToggleEquipment" then took[#took + 1] = it end
            return true
        end
        RESPEC.tabs, RESPEC.finish, RESPEC.complete = REAL.tabs, REAL.finish, REAL.complete
        local res = run("ui.cc_apply", { plan = { class = "Wizard" }, finish = false })
        BG3AI.UI.path, BG3AI.UI.command = path, command
        return took, lute, res
    end
    local took, lute, res = apply("CharacterRespec", 0)
    check(#took == 1 and took[1] == lute, "instrument: an open selector takes its first entry")
    check(res.result.set.equipment.took_first == true, "instrument: the result says so")
    took = apply("CharacterRespec", 1)
    check(#took == 0, "instrument: a selector already full is left alone")
    took = apply("CharacterCreation", 0)
    check(#took == 0, "instrument: a new game's page is left alone")
    filled(false)
end

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
