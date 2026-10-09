-- Offline test: ui.cc_name / ui.cc_finish and cc_apply's name guard in Client/Progression.lua, over a fake
-- character creation page (no game). Run: tools/dev/test_cc_name.sh
local L = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local fails = 0
local function check(cond, what) if not cond then fails = fails + 1 print("FAIL " .. what) else print("ok   " .. what) end end

-- Fake Noesis objects: tables with props and SetProperty/GetProperty.
local function obj(type_, props, onset)
    local o = { _t = type_, _p = props or {}, _onset = onset }
    function o.GetProperty(self, k) return self._p[k] end
    function o.SetProperty(self, k, v)
        if self._onset then self._onset(self, k, v) else self._p[k] = v end
    end
    return o
end

local page = { name = "Tav", writeWorks = false, finished = false }
local dc = obj("ls.DCCharacterCreation", { CanEditName = true, IsCharacterComplete = true,
    IsCharacterCompleteExceptName = true, Validity = nil })
setmetatable(dc._p, { __index = function(_, k) if k == "CharacterName" then return page.name end end })
local box = obj("ls.LSTextBox", { Text = "Tav", IsFocused = false, IsKeyboardFocused = false, IsMouseOver = false },
    function(self, k, v)
        if type(v) ~= "string" then error("bad value") end
        self._p[k] = v
        if page.writeWorks then page.name = v end
    end)
local frame = obj("Grid", { IsMouseOver = false })
local flow = obj("ListBox", { SelectedIndex = 0 })
local namingPage = obj("Control", {})
local widget = obj("ls.UIWidget", {})
widget.DataContext = dc

BG3AI = {}
local handlers, jobs, now = {}, {}, 0
BG3AI.RPC = {
    register = function(n, f) handlers[n] = f end,
    now_ms = function() return now end,
    start_job = function(kind) local j = { id = #jobs + 1, kind = kind } jobs[#jobs + 1] = j return j end,
    finish_job = function(j, st, res, err) j.status, j.result, j.error = st, res, err end,
}
local named = { namingPage = namingPage, flowPage = flow }
BG3AI.UI = {
    prop = function(o, k) if o == nil then return nil end return o:GetProperty(k) end,
    props = function(o) return o and o._p or {} end,
    typeName = function(o) return type(o) == "table" and o._t or nil end,
    text = function(o) return nil end,
    clean = function(s) return s end,
    widgets = function() return { { name = "CharacterCreation", el = widget } } end,
    widget = function() return widget end,
    isVisible = function() return true end,
    cachedByName = function(_, n) return named[n] end,
    byName = function(el, n)
        if el ~= namingPage then return nil end
        if n == "characterName" then return box end
        if n == "textInputBox" then return frame end
    end,
    command = function(o, n) if n == "FinishCreating" then page.finished = true end return true end,
    items = function() return function() return nil end end,
    count = function() return 0 end,
    path = function() return nil end,
    findAll = function() return {} end,
}
dofile(L .. "/Client/Progression.lua")

local function call(op, args)
    local ok, r = pcall(handlers[op], args or {})
    return ok, r
end

-- 1. a fresh page: not confirmed, writing the box doesn't change the character
local ok, v = call("ui.cc_name", { name = "Claude" })
check(ok and v.name == "Tav" and v.confirmed == false and v.ok == false and v.box, "view: Tav, not confirmed")
ok, v = call("ui.cc_name", { name = "Claude", set = true })
check(ok and v.set == true and v.text == "Claude" and v.confirmed == false and v.ok == false,
    "set: box shows Claude, character still Tav -> not ok")
ok, v = call("ui.cc_finish", { name = "Claude" })
check(not ok and tostring(v):find("name check failed"), "cc_finish refuses under the wrong name")
check(not page.finished, "not finished")

-- 2. page move
ok, v = call("ui.cc_name", { page = true })
check(ok and v.moved and flow._p.SelectedIndex == 3 and v.on_naming_page, "page -> naming (flowPage 3)")
frame._p.IsMouseOver, box._p.IsMouseOver, box._p.IsFocused = true, true, true
ok, v = call("ui.cc_name", {})
check(ok and v.hover_box and v.hover_frame and v.focused, "hover/focus flags")

-- 3. typed: the character takes the name -> cc_finish runs
page.name = "Claude"
ok, v = call("ui.cc_finish", { name = "Claude" })
check(ok and v.finished and page.finished, "cc_finish after the name checks out")

-- 4. cc_apply with finish: the name guard fails the job and never finishes
page.name, page.finished, box._p.Text, page.writeWorks = "Tav", false, "Tav", false
local function runjob(res)
    local j = jobs[res.job]
    for _ = 1, 400 do
        if j.status then break end
        now = now + 1000
        j.on_tick(j)
    end
    return j
end
ok, v = call("ui.cc_apply", { plan = { name = "Claude" } })
local j = runjob(v)
check(j.status == "failed" and j.result.needs_name == "Claude" and tostring(j.error):find("not finishing"),
    "cc_apply: name not confirmed -> failed with needs_name")
check(not page.finished, "cc_apply did not finish")

-- 5. cc_apply where the box write reaches the character: finishes
page.writeWorks = true
ok, v = call("ui.cc_apply", { plan = { name = "Claude" } })
j = runjob(v)
check(j.status == "done" and j.result.finished and not j.result.name_pending and page.finished,
    "cc_apply: name confirmed by the box write -> finished")

-- 6. finish=false: reports name_pending, no failure
page.writeWorks, page.name, page.finished = false, "Tav", false
ok, v = call("ui.cc_apply", { plan = { name = "Claude" }, finish = false })
j = runjob(v)
check(j.status == "done" and j.result.name_pending == "Claude" and not page.finished, "finish=false: name_pending only")

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
