-- Offline tests for confirmation boxes (worker se): UIActions ui.confirm /
-- pickConfirm / the ui.command "Yes" fallback, and Services' long rest
-- answering "End the day?" + "Someone wants to talk, rest anyway?" (live S005).
-- Ext, RPC, UI and UIState are stubbed.
-- Run: tools/dev/test_confirm.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local clock = 1000
local keys, commands = {}, {}
local widgets = {}      -- name -> {name, visible, buttons={{label,name,onPress}}, texts={}}
local inCamp = true

local function W(name, texts, buttons)
    local w = { name = name, visible = true, texts = texts or {}, buttons = {} }
    for i, b in ipairs(buttons or {}) do
        w.buttons[i] = { isButton = true, label = b[1], name = b[2], onPress = b[3], parent = w }
    end
    widgets[name] = w
    return w
end

Ext = {
    Utils = { MonotonicTime = function() return clock end, GetGameState = function() return "Running" end },
    Input = { InjectKeyPress = function(k) keys[#keys + 1] = k end },
    IO = { LoadFile = function() return nil end },
    Json = { Parse = function() return {} end },
}

local jobs = {}
local ops = {}
local nextId = 0
BG3AI = {
    RPC = {
        now_ms = function() return clock end,
        register = function(name, fn) ops[name] = fn end,
        push = function() end,
        start_job = function(kind, spec)
            nextId = nextId + 1
            local job = spec or {}
            job.id, job.kind, job.started, job.status = nextId, kind, clock, "running"
            jobs[nextId] = job
            return job
        end,
        finish_job = function(job, status, result, err)
            if job.status ~= "running" then return end
            job.status, job.result, job.error = status, result, err
        end,
    },
    UI = {
        widgets = function()
            local out, names = {}, {}
            for n in pairs(widgets) do names[#names + 1] = n end
            table.sort(names)
            for _, n in ipairs(names) do out[#out + 1] = { name = n, el = widgets[n] } end
            return out
        end,
        widget = function(n) return widgets[n] end,
        isVisible = function(el) return el.visible ~= false end,
        isButton = function(e) return e.isButton == true end,
        findAll = function(el, pred)
            local out = {}
            for _, b in ipairs(el.buttons or {}) do if pred(b) then out[#out + 1] = b end end
            return out
        end,
        allText = function(e) return e.label end,
        name = function(e) return e.name end,
        press = function(el, dry)
            if dry then return true end
            if el.onPress then el.onPress(el) end
            return true
        end,
        props = function() return {} end,
        prop = function() return nil end,
        path = function(dc, p)
            if p == "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.IsInCamp" then return inCamp end
            return nil
        end,
        dc = function(n) return widgets[n] and { dc = n } or { dc = "HotBar" } end,
        anyDC = function() return { dc = "any" } end,
        command = function(dc, name, param, dry)
            if dc.dc == "any" or dc.dc == "HotBar" then
                if name == "CampTravel" then
                    commands[#commands + 1] = name
                    if not dry then
                        -- "End the day?" then "Someone wants to talk" then the rest screen
                        W("Dialog_box", { "[ForceUpdate]" }, {
                            { "Yes", "YesBtn", function(b)
                                W("Dialog_box", { "[ForceUpdate]", "Someone wants to talk. Rest anyway?" }, {
                                    { "Yes", nil, function() widgets.Dialog_box = nil W("MakeCamp", {}, {}) end },
                                    { "No", nil, function() widgets.Dialog_box = nil end } })
                            end },
                            { "No", "NoBtn", function() widgets.Dialog_box = nil end } })
                    end
                    return true
                end
            end
            return false, "no command '" .. tostring(name) .. "' on ls.Widget"
        end,
        byName = function() return nil end,
        findButton = function() return nil end,
        execute = function() return true end,
    },
    UIState = {
        popup = function(el)
            local p = {}
            if el.texts[1] then p.title = el.texts[1] end
            if el.texts[2] then p.text = el.texts[2] end
            local b = {}
            for _, x in ipairs(el.buttons) do b[#b + 1] = x.label end
            if #b > 0 then p.buttons = b end
            return p
        end,
        SCREENS = {},
    },
    Util = { comp = function() return nil end, dist = function() return 0 end },
}

dofile(LUA .. "/Client/UIActions.lua")
dofile(LUA .. "/Client/Services.lua")
local A = BG3AI.UIActions

local fails, passes = 0, 0
local function ok(cond, msg)
    if cond then passes = passes + 1 else fails = fails + 1 print("FAIL " .. msg) end
end
local function run(id, ms)
    local job = jobs[id]
    for _ = 1, (ms or 5000) / 50 do
        clock = clock + 50
        if job.status ~= "running" then break end
        if clock > job.deadline_at then job.status = "timeout" break end
        job.on_tick(job)
    end
    return job
end
local _start = BG3AI.RPC.start_job
BG3AI.RPC.start_job = function(kind, spec)
    local j = _start(kind, spec)
    j.deadline_at = clock + (j.timeout_ms or 20000)
    return j
end

-- pickConfirm
local B = function(...) local t = {} for i, l in ipairs({ ... }) do t[i] = { label = l } end return t end
ok(A.pickConfirm(B("Yes", "No"), "yes").label == "Yes", "yes -> Yes")
ok(A.pickConfirm(B("Yes", "No"), "no").label == "No", "no -> No")
ok(A.pickConfirm(B("Yes", "No"), nil).label == "Yes", "default -> Yes")
ok(A.pickConfirm(B("OK"), "yes").label == "OK", "yes -> OK")
ok(A.pickConfirm(B("Confirm", "Cancel"), "no").label == "Cancel", "no -> Cancel")
ok(A.pickConfirm(B("Not now", "Now"), "no") ~= nil, "no -> something")
ok(A.pickConfirm(B("End the Day", "Cancel"), "yes").label == "End the Day", "yes -> first (affirmative) button")
ok(A.pickConfirm(B("Leave them", "Stay"), "stay").label == "Stay", "label answer")
ok(A.pickConfirm(B("A", "B", "C"), "2").label == "B", "numeric answer")
ok(A.pickConfirm({ { label = nil, name = "YesBtn" }, { name = "NoBtn" } }, "no").name == "NoBtn", "name match")
ok(A.pickConfirm(B("Overwrite"), "maybe") == nil, "no match -> nil")

-- ui.confirm: no box
local okc, err = pcall(ops["ui.confirm"], { answer = "yes" })
ok(not okc and tostring(err):find("no confirmation box"), "no box -> error")

-- ui.confirm: Yes closes the box
local pressed
W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() pressed = "yes" widgets.Dialog_box = nil end },
    { "No", nil, function() pressed = "no" widgets.Dialog_box = nil end } })
local r = ops["ui.confirm"]({ answer = "yes" })
local j = run(r.job)
ok(pressed == "yes" and j.status == "done" and j.result.pressed == "Yes", "ui.confirm yes: " .. tostring(j.status))

-- ui.confirm no
W("Dialog_box", { "Quit?" }, { { "Yes", nil, function() pressed = "yes" widgets.Dialog_box = nil end },
    { "No", nil, function() pressed = "no" widgets.Dialog_box = nil end } })
j = run(ops["ui.confirm"]({ answer = "no" }).job)
ok(pressed == "no" and j.result.pressed == "No", "ui.confirm no")

-- ui.command fallback (live S005: ui_action Yes; ui_action Btn param=Yes)
W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() pressed = "cmd-yes" widgets.Dialog_box = nil end },
    { "No", nil, function() widgets.Dialog_box = nil end } })
local okk, res = pcall(ops["ui.command"], { command = "Yes" })
ok(okk and pressed == "cmd-yes" and res.pressed == "Yes", "ui.command Yes falls back to the box: " .. tostring(res))
W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() pressed = "param-yes" widgets.Dialog_box = nil end },
    { "No", nil, function() widgets.Dialog_box = nil end } })
okk, res = pcall(ops["ui.command"], { command = "Btn", widget = "Dialog_box", param = "Yes" })
ok(okk and pressed == "param-yes", "ui.command Btn param=Yes falls back: " .. tostring(res))
okk, res = pcall(ops["ui.command"], { command = "Yes" })
ok(not okk and tostring(res):find("no command"), "no box: original error kept")

-- long rest: two confirmations, then the rest screen opens
widgets = {}
r = ops["ui.long_rest"]({})
ok(commands[#commands] == "CampTravel", "CampTravel sent")
j = jobs[r.job]
for _ = 1, 60 do
    clock = clock + 50
    j.on_tick(j)
    if widgets.MakeCamp or j.status ~= "running" then break end
end
ok(widgets.MakeCamp ~= nil, "rest screen opened after two Yes answers (status " .. tostring(j.status) .. " " .. tostring(j.error) .. ")")
ok(j.confirms and #j.confirms == 2, "two questions recorded: " .. tostring(j.confirms and #j.confirms))
ok(j.confirms and j.confirms[2]:find("Someone wants to talk"), "question text kept")

-- long rest with confirm=false: stops at the question
widgets = {}
r = ops["ui.long_rest"]({ confirm = false })
j = jobs[r.job]
for _ = 1, 20 do clock = clock + 50 j.on_tick(j) if j.status ~= "running" then break end end
ok(j.status == "failed" and tostring(j.error):find("answer with confirm"), "confirm=false fails with the question: " .. tostring(j.error))
ok(widgets.Dialog_box ~= nil, "box left open")

print(("%d passed, %d failed"):format(passes, fails))
os.exit(fails == 0 and 0 or 1)
