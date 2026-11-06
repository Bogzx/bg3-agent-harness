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

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
