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

-- As seen live (Patch 8): CharacterName reads as a handle whatever the box shows, the box's Text reads as
-- nil, its text is only readable through SelectedText.
local HANDLE = "ResStr_3455818087"
local page = { name = HANDLE, text = "Tav", finished = false }
local dc = obj("ls.DCCharacterCreation", { CanEditName = true, IsCharacterComplete = true,
    IsCharacterCompleteExceptName = true, Validity = nil })
setmetatable(dc._p, { __index = function(_, k) if k == "CharacterName" then return page.name end end })
local box = obj("ls.LSTextBox", { IsFocused = false, IsKeyboardFocused = false, IsMouseOver = false,
    SelectionStart = 0, SelectionLength = 0 },
    function(self, k, v)
        if k == "Text" then error("unexpected Text write") end
        if type(v) ~= "number" then error("bad value") end
        if k == "SelectionLength" then v = math.min(v, #page.text - self._p.SelectionStart) end
        self._p[k] = v
    end)
setmetatable(box._p, { __index = function(t, k)
    if k == "SelectedText" then
        local s0 = rawget(t, "SelectionStart") or 0
        return page.text:sub(s0 + 1, s0 + (rawget(t, "SelectionLength") or 0))
    end
end })
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
    pcall = pcall,
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
Ext = { Require = function(p) return dofile(L .. "/" .. p) end } -- Progression.lua loads its parts
dofile(L .. "/Client/Progression.lua")

local function call(op, args)
    local ok, r = pcall(handlers[op], args or {})
    return ok, r
end

-- 1. a fresh page: the box shows Tav, the character's name is unreadable (handle)
local ok, v = call("ui.cc_name", { name = "Claude" })
check(ok and v.name == nil and v.name_handle == HANDLE and v.text == "Tav" and v.ok == false and v.box,
    "view: box Tav, name unreadable (handle), not ok")
check(box._p.SelectionStart == 0 and box._p.SelectionLength == 0, "reading the text restores the selection")
ok, v = call("ui.cc_name", { name = "Claude", set = true })
check(not ok and tostring(v):find("not supported"), "set (Text write) is refused")
ok, v = call("ui.cc_finish", { name = "Claude" })
check(not ok and tostring(v):find("name check failed"), "cc_finish refuses under the wrong name")
check(not page.finished, "not finished")

-- 2. page move
ok, v = call("ui.cc_name", { page = true })
check(ok and v.moved and flow._p.SelectedIndex == 3 and v.on_naming_page, "page -> naming (flowPage 3)")
frame._p.IsMouseOver, box._p.IsMouseOver, box._p.IsFocused = true, true, true
ok, v = call("ui.cc_name", {})
check(ok and v.hover_box and v.hover_frame and v.focused, "hover/focus flags")

-- 3. typed: the box shows the name -> cc_finish runs
box._p.SelectionStart, box._p.SelectionLength = 3, 0
page.text = "Claude"
ok, v = call("ui.cc_name", { name = "Claude" })
check(ok and v.ok and v.text == "Claude" and box._p.SelectionStart == 3, "typed: ok, caret kept")
ok, v = call("ui.cc_finish", { name = "Claude" })
check(ok and v.finished and page.finished, "cc_finish after the name checks out")

-- 3b. a readable character name that differs blocks it
page.name, page.finished = "Tav", false
ok, v = call("ui.cc_finish", { name = "Claude" })
check(not ok and not page.finished, "readable wrong character name blocks cc_finish")
page.name = HANDLE

-- 4. cc_apply with finish: the name guard fails the job and never finishes
page.finished, page.text = false, "Tav"
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
    "cc_apply: name not set -> failed with needs_name")
check(not page.finished, "cc_apply did not finish")
check(j.result.set.name == nil, "cc_apply: handle not reported as the name")

-- 5. cc_apply where the box already shows the name (typed earlier): finishes
page.text = "Claude"
ok, v = call("ui.cc_apply", { plan = { name = "Claude" } })
j = runjob(v)
check(j.status == "done" and j.result.finished and not j.result.name_pending and page.finished,
    "cc_apply: box shows the name -> finished")

-- 6. finish=false: reports name_pending, no failure
page.text, page.finished = "Tav", false
ok, v = call("ui.cc_apply", { plan = { name = "Claude" }, finish = false })
j = runjob(v)
check(j.status == "done" and j.result.name_pending == "Claude" and not page.finished, "finish=false: name_pending only")

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
