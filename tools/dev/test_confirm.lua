-- Offline tests for confirmation boxes (worker se): UIActions ui.confirm /
-- pickConfirm / the ui.command "Yes" fallback, and Services' long rest
-- answering "End the day?" + "Someone wants to talk, rest anyway?" (live S005), and the re-run after
-- the bridge clicks a dead box (live S018: no second CampTravel while the rest screen opens).
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
        prop = function(e, k) if k == "IsMouseOver" and type(e) == "table" then return e.over end return nil end,
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


-- long rest: a box whose Yes ignores its Command (live S013/S014 "[ForceUpdate] Yes/No") -> needs_click
-- after one press, not three; the re-run after the bridge's click opens the rest screen
widgets = {}
local deadPresses = 0
BG3AI.UI.command = (function(orig)
    return function(dc, name, param, dry)
        if name == "CampTravel" and not dry then
            commands[#commands + 1] = name
            W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() deadPresses = deadPresses + 1 end },
                { "No", nil, function() deadPresses = deadPresses + 1 end } })
            return true
        end
        return orig(dc, name, param, dry)
    end
end)(BG3AI.UI.command)
r = ops["ui.long_rest"]({})
j = run(r.job, 5000)
ok(j.status == "failed" and j.result and j.result.needs_click == "yes", "dead box -> needs_click: " .. tostring(j.status) .. " " .. tostring(j.error))
ok(deadPresses == 1, "pressed once before asking for a click: " .. deadPresses)
-- the bridge clicks Yes: the box closes and the rest screen is up; long_rest runs again from there
widgets.Dialog_box = nil
W("MakeCamp", {}, {})
local nCamp = #commands
r = ops["ui.long_rest"]({})
ok(#commands == nCamp, "re-run with the rest screen open doesn't ask CampTravel again")

-- long rest, live S018 x4: the bridge clicked the dead box's Yes, the rest screen was opening (not yet
-- visible), and the re-run 0.5 s later pressed CampTravel again, which cancelled it ("the rest screen
-- didn't open"). Model: Yes -> MakeCamp shows 1 s later unless CampTravel is pressed meanwhile.
local opening, cancelled
local function clickYes() -- what boxclick does: the box closes, the screen starts opening
    widgets.Dialog_box = nil
    opening = clock + 1000
end
local function tickGame()
    if opening and clock >= opening then opening = nil W("MakeCamp", {}, {}).DataContext = { dc = "MakeCamp" } end
end
BG3AI.UI.command = (function(orig)
    return function(dc, name, param, dry)
        if name == "CampTravel" and not dry and opening then
            commands[#commands + 1] = name
            opening, cancelled = nil, true -- a second press while it opens: nothing opens
            return true
        end
        if dc and dc.dc == "MakeCamp" then commands[#commands + 1] = name return true end
        return orig(dc, name, param, dry)
    end
end)(BG3AI.UI.command)
local function runGame(id, ms)
    local job = jobs[id]
    for _ = 1, (ms or 8000) / 50 do
        clock = clock + 50
        tickGame()
        if job.status ~= "running" then break end
        job.on_tick(job)
    end
    return job
end
clock = clock + 60000
widgets, opening, cancelled = {}, nil, nil
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
ok(j.status == "failed" and j.result.needs_click == "yes", "S018: first job asks for a click: " .. tostring(j.error))
clickYes()
clock = clock + 500 -- the bridge sleeps 0.5 s, then runs long_rest again
nCamp = #commands
r = ops["ui.long_rest"]({})
ok(#commands == nCamp, "S018: the re-run after the click doesn't press CampTravel again")
j = runGame(r.job, 3000)
ok(not cancelled and j.status ~= "failed", "S018: the rest screen opens and the rest proceeds: "
    .. tostring(j.status) .. " " .. tostring(j.error))

-- the click closed the box but nothing opened: after 4 s of nothing, CampTravel once more (a fresh box)
clock = clock + 60000
widgets, opening, cancelled = {}, nil, nil
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
widgets.Dialog_box = nil -- clicked, but the game did nothing
clock = clock + 500
nCamp = #commands
r = ops["ui.long_rest"]({})
j = jobs[r.job]
for _ = 1, 100 do clock = clock + 50 j.on_tick(j) if #commands > nCamp then break end end
ok(#commands == nCamp + 1, "nothing after the click: CampTravel pressed again after ~4 s")
j = run(r.job, 3000)
ok(j.status == "failed" and j.result.needs_click == "yes", "the retry's box goes to the bridge again: " .. tostring(j.error))

-- nothing at all opens: the failure names the step and what's on screen
clock = clock + 60000
widgets = { HotBar = { name = "HotBar", visible = true, texts = {}, buttons = {} },
            NotificationWidget = { name = "NotificationWidget", visible = true, texts = {}, buttons = {} } }
BG3AI.UI.command = (function(orig)
    return function(dc, name, param, dry)
        if name == "CampTravel" and not dry then commands[#commands + 1] = name return true end
        return orig(dc, name, param, dry)
    end
end)(BG3AI.UI.command)
nCamp = #commands
r = ops["ui.long_rest"]({})
j = run(r.job, 12000)
local e = tostring(j.error)
ok(j.status == "failed" and e:find("rest screen didn't open", 1, true), "no screen -> failed: " .. e)
ok(e:find("CampTravel (retry)", 1, true) and e:find("pressed 2x", 1, true), "message says the step and presses: " .. e)
ok(e:find("widgets=[NotificationWidget] (+1 HUD layers)", 1, true), "message names the non-HUD widgets: " .. e)
ok(#commands == nCamp + 2 and j.result.step == "open", "pressed twice, step=open")

-- repress=false (the bridge decides, S030): one press, then fail after 4 s; a box that came and went is named
clock = clock + 60000
nCamp = #commands
r = ops["ui.long_rest"]({ repress = false })
j = jobs[r.job]
for _ = 1, 10 do clock = clock + 50 j.on_tick(j) end
widgets.Flash_box = { name = "Flash_box", visible = true, texts = {}, buttons = {} }
for _ = 1, 10 do clock = clock + 50 j.on_tick(j) end
widgets.Flash_box = nil
j = run(r.job, 12000)
e = tostring(j.error)
ok(j.status == "failed" and #commands == nCamp + 1, "repress=false: pressed once, then failed: " .. e)
ok(e:find("came and went: Flash_box", 1, true), "a widget that came and went is named: " .. e)
ok(j.result.seen_widgets and j.result.seen_widgets[1] == "Flash_box", "seen_widgets in the result")

-- no_press (the game's late yes): waits for the screen without pressing CampTravel
clock = clock + 60000
nCamp = #commands
r = ops["ui.long_rest"]({ repress = false, no_press = true })
j = run(r.job, 12000)
ok(#commands == nCamp and tostring(j.error):find("late yes", 1, true), "no_press: no CampTravel press: "
    .. tostring(j.error))

-- a camp scene starts instead of the rest screen after the question
clock = clock + 60000
widgets = {}
BG3AI.UI.command = (function(orig)
    return function(dc, name, param, dry)
        if name == "CampTravel" and not dry then
            commands[#commands + 1] = name
            W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() widgets.Dialog_box = nil W("Dialogue", {}, {}) end },
                { "No", nil, function() widgets.Dialog_box = nil end } })
            return true
        end
        return orig(dc, name, param, dry)
    end
end)(BG3AI.UI.command)
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
ok(j.status == "failed" and tostring(j.error):find("dialogue started instead", 1, true) and j.result.camp_scene,
    "camp scene before the rest screen is named: " .. tostring(j.error))

-- a conversation already on screen when long_rest is called: refused at once, CampTravel never pressed (S026)
clock = clock + 60000
widgets = {}
W("Dialogue", {}, {})
nCamp = #commands
local okr, er = pcall(ops["ui.long_rest"], {})
ok(not okr and tostring(er):find("conversation is open", 1, true) and #commands == nCamp,
    "dialogue on screen -> refused, no CampTravel: " .. tostring(er))
widgets = {}

-- ui.confirm on a dead box: still_open + needs_click
widgets = {}
W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() end }, { "No", nil, function() end } })
j = run(ops["ui.confirm"]({ answer = "no" }).job)
ok(j.status == "done" and j.result.still_open and j.result.needs_click == "no", "confirm dead box -> needs_click=no")

-- ui.confirm_probe: hover state of the answering button
local pr = ops["ui.confirm_probe"]({ answer = "yes" })
ok(pr.box == true and pr.label == "Yes" and pr.over == false and pr.over_other == nil, "probe: not over")
widgets.Dialog_box.buttons[2].over = true
pr = ops["ui.confirm_probe"]({ answer = "yes" })
ok(pr.over == false and pr.over_other == 2, "probe: over the other button")
widgets.Dialog_box.buttons[2].over = nil
widgets.Dialog_box.buttons[1].over = true
pr = ops["ui.confirm_probe"]({ answer = "yes" })
ok(pr.over == true, "probe: over Yes")
widgets = {}
ok(ops["ui.confirm_probe"]({}).box == false, "probe: no box")

-- ui.key names (live S007: press_key DELETE -> "not a SDLScanCode: DELETE")
local function ks(k, m) local a, b = A.keySpec(k, m) return a .. "|" .. table.concat(b or {}, ",") end
ok(ks("DELETE") == "DEL|", "DELETE -> DEL: " .. ks("DELETE"))
ok(ks("1") == "NUM_1|", "1 -> NUM_1: " .. ks("1"))
ok(ks("Backspace") == "BACKSPACE|", "Backspace")
ok(ks("ARROW_UP") == "UP|" and ks("left") == "LEFT|", "arrows")
ok(ks("f5") == "F5|" and ks("ENTER") == "RETURN|" and ks("ESC") == "ESCAPE|", "F5/ENTER/ESC")
ok(ks("CTRL+S") == "S|LCtrl", "CTRL+S: " .. ks("CTRL+S"))
ok(ks("Shift-Tab") == "TAB|LShift", "Shift-Tab: " .. ks("Shift-Tab"))
ok(ks("TAB", "SHIFT") == "TAB|LShift" and ks("A", { "lctrl", "alt" }) == "A|LCtrl,LAlt", "mods arg")
ok(ks("-") == "MINUS|" and ks("PGDN") == "PAGEDOWN|" and ks("NUMPAD_5") == "KP_5|", "- PGDN NUMPAD_5")
local injected
Ext.Input.InjectKeyPress = function(k, m)
    if k ~= "DEL" and k ~= "S" then error("Ext.Input.InjectKeyPress: not a SDLScanCode: " .. k) end
    injected = { k, m }
end
local kr = ops["ui.key"]({ key = "DELETE" })
ok(kr.key == "DEL" and injected[1] == "DEL", "ui.key DELETE injects DEL")
ops["ui.key"]({ key = "CTRL+S" })
ok(injected[1] == "S" and injected[2][1] == "LCtrl", "ui.key CTRL+S injects S + LCtrl")
local okk, ek = pcall(ops["ui.key"], { key = "FOO" })
ok(not okk and tostring(ek):find("unknown key 'FOO'") and tostring(ek):find("DELETE"), "unknown key lists names: " .. tostring(ek))

-- audit UI-3: the escape hatches don't save or load around save_game / load_game
do
    injected = nil
    local okf, ef = pcall(ops["ui.key"], { key = "F8" })
    ok(not okf and tostring(ef):find("quickload") and tostring(ef):find("load_game") and injected == nil,
        "ui.key F8 (quickload) refused: " .. tostring(ef))
    okf, ef = pcall(ops["ui.key"], { key = "f5" })
    ok(not okf and tostring(ef):find("quicksave") and injected == nil, "ui.key F5 (quicksave) refused: " .. tostring(ef))
    W("LoadGame", {}, {})
    okf, ef = pcall(ops["ui.key"], { key = "RETURN" })
    ok(not okf and tostring(ef):find("LoadGame page is open") and injected == nil, "RETURN on the Load page refused: " .. tostring(ef))
    local _ikp = Ext.Input.InjectKeyPress
    Ext.Input.InjectKeyPress = function(k) injected = { k } end
    okf = pcall(ops["ui.key"], { key = "ESCAPE" })
    ok(okf and injected and injected[1] == "ESCAPE", "ESCAPE still closes the Load page")
    widgets = {}
    okf = pcall(ops["ui.key"], { key = "RETURN" })
    ok(okf and injected[1] == "RETURN", "RETURN with no save/load page passes")
    Ext.Input.InjectKeyPress = _ikp

    local executed = {}
    local _cmd = BG3AI.UI.command
    BG3AI.UI.command = function(dc, name, param, dry) if not dry then executed[#executed + 1] = name end return true end
    for _, c in ipairs({ "QuickLoadCommand", "QuickSaveCommand", "OpenLoadGameDialog", "ContinueGameCommand",
        "DeleteSavegameCommand" }) do
        okf, ef = pcall(ops["ui.command"], { widget = "*", command = c })
        ok(not okf and tostring(ef):find("save_game / load_game"), "ui.command " .. c .. " refused: " .. tostring(ef))
    end
    ok(#executed == 0, "nothing executed: " .. table.concat(executed, ","))
    okf, ef = pcall(ops["ui.command"], { widget = "*", command = "QuickLoadCommand", dry = true })
    ok(okf, "a dry QuickLoadCommand (CanExecute only) passes: " .. tostring(ef))
    -- the refusal is not swallowed by the confirm-box fallback
    W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() pressed = "saveload-yes" end } })
    pressed = nil
    okf, ef = pcall(ops["ui.command"], { widget = "*", command = "QuickLoadCommand", param = "Yes" })
    ok(not okf and pressed == nil, "a refused QuickLoad doesn't press the box's Yes")
    widgets = {}
    W("SaveGame", {}, {})
    okf, ef = pcall(ops["ui.command"], { widget = "SaveGame", command = "SelectSaveCommand" })
    ok(not okf and tostring(ef):find("SaveGame page is open"), "any command on the Save page refused: " .. tostring(ef))
    okf, ef = pcall(ops["ui.command"], { widget = "SaveGame", command = "CloseCommand" })
    ok(okf, "Close on the Save page passes: " .. tostring(ef))
    widgets = {}
    okf, ef = pcall(ops["ui.command"], { widget = "*", command = "EndTurn" })
    ok(okf and executed[#executed] == "EndTurn", "an ordinary command passes: " .. tostring(ef))
    BG3AI.UI.command = _cmd

    -- ui.press: Save/Load buttons and anything but a way out on the Save/Load pages
    local _fb = BG3AI.UI.findButton
    BG3AI.UI.findButton = function(w, which)
        for _, b in ipairs(w.buttons) do
            if (b.name or ""):lower() == which:lower() or (b.label or ""):lower():find(which:lower(), 1, true) then return b, 1 end
        end
    end
    local hit
    W("GameMenu", {}, { { "Load Game", "LoadBtn", function() hit = "load" end }, { "Resume", "ResumeBtn", function() hit = "resume" end } })
    okf, ef = pcall(ops["ui.press"], { widget = "GameMenu", button = "Load Game" })
    ok(not okf and hit == nil, "ui.press Load Game refused: " .. tostring(ef))
    okf, ef = pcall(ops["ui.press"], { widget = "GameMenu", button = "Resume" })
    ok(okf and hit == "resume", "ui.press Resume passes: " .. tostring(ef))
    widgets = {}
    W("MainMenu", {}, { { "Continue", "ContinueBtn", function() hit = "continue" end } })
    hit = nil
    okf, ef = pcall(ops["ui.press"], { widget = "MainMenu", button = "Continue" })
    ok(not okf and hit == nil, "main menu Continue (loads the latest save) refused: " .. tostring(ef))
    widgets = {}
    W("Roll", {}, { { "Continue", "ContinueBtn", function() hit = "roll" end } })
    okf, ef = pcall(ops["ui.press"], { widget = "Roll", button = "Continue" })
    ok(okf and hit == "roll", "Continue elsewhere passes: " .. tostring(ef))
    widgets = {}
    W("SaveGame", {}, { { "My save", "SaveRow", function() hit = "row" end }, { "Back", "BackBtn", function() hit = "back" end } })
    hit = nil
    okf, ef = pcall(ops["ui.press"], { widget = "SaveGame", button = "My save" })
    ok(not okf and hit == nil, "a save row on the Save page refused: " .. tostring(ef))
    okf, ef = pcall(ops["ui.press"], { widget = "SaveGame", button = "Back" })
    ok(okf and hit == "back", "Back on the Save page passes: " .. tostring(ef))
    BG3AI.UI.findButton = _fb
    widgets = {}
end

-- waypoint names (live S007: travel_to waypoint="Roadside Cliffs" / "WAYP_CHA_Top" failed)
local WPS = { { name = "Overgrown Ruins", region = "Wilderness" }, { name = "Roadside Cliffs", region = "Wilderness" },
    { name = "Emerald Grove Environs", region = "Wilderness" }, { name = "The Risen Road", region = "Wilderness" } }
local function pw(w) local r, n = BG3AI.Services.pickWaypoint(WPS, w) return r and r.name or ("nil:" .. tostring(n)) end
ok(pw("Roadside Cliffs") == "Roadside Cliffs", "exact")
ok(pw("roadside cliffs") == "Roadside Cliffs", "case")
ok(pw("Waypoint: Roadside Cliffs") == "Roadside Cliffs", "Waypoint: prefix")
ok(pw("Roadside-Cliffs!") == "Roadside Cliffs", "punctuation")
ok(pw("cliffs") == "Roadside Cliffs" and pw("grove") == "Emerald Grove Environs", "partial word")
ok(pw("Emerald Grove") == "Emerald Grove Environs", "prefix words")
ok(pw("Risen Road waypoint") == "The Risen Road", "extra word: " .. pw("Risen Road waypoint"))
ok(pw("Grove Environs gate") == "Emerald Grove Environs", "word overlap")
ok(pw("r") == "nil:3" or pw("r"):sub(1, 4) == "nil:", "ambiguous one letter: " .. pw("r"))
ok(pw("Blighted Village"):sub(1, 4) == "nil:", "unknown stays unknown")
Ext.Loca = { GetTranslatedStringKey = function(id) return id == "WAYP_CHA_Top" and "h123" or nil end,
    GetTranslatedString = function(h) return h == "h123" and "Roadside Cliffs" or "" end }
ok(pw("WAYP_CHA_Top") == "Roadside Cliffs", "get_map id via loca: " .. pw("WAYP_CHA_Top"))
ok(BG3AI.Services.waypointNames(WPS):find("Roadside Cliffs %(Wilderness%)"), "names listed with region")

-- fast_travel from camp moved only the controlled character (live 2026-10-10): refused, the bridge leaves camp
widgets = {}
inCamp = true
local okc, ec = pcall(ops["ui.fast_travel"], { waypoint = "Roadside Cliffs" })
ok(not okc and tostring(ec):find("at camp: leave camp first"), "at camp refused: " .. tostring(ec))
inCamp = false

-- fast_travel waits for the map's list to fill, then says so instead of "no waypoint named"
widgets = {}
W("JournalMap", {}, {})
Ext.Entity = { GetAllEntitiesWithComponent = function() return {} end }
local ftr = ops["ui.fast_travel"]({ waypoint = "Roadside Cliffs" })
local ftj = jobs[ftr.job]
clock = clock + 50 ftj.on_tick(ftj)
ok(ftj.status == "running", "waits while the list is empty")
for _ = 1, 50 do clock = clock + 50 ftj.on_tick(ftj) if ftj.status ~= "running" then break end end
ok(ftj.status == "failed" and tostring(ftj.error):find("no waypoint list"), "empty list explained: " .. tostring(ftj.error))

-- withMap (live S021 x7): after a successful travel the M toggle was a step out of phase,
-- so the next press was swallowed and fast_travel failed "the map didn't open"; the
-- following call found the map up. Now: no map 1.2 s after M -> press again.
local swallow, mapKeys = 0, {}
Ext.Input.InjectKeyPress = function(k)
    mapKeys[#mapKeys + 1] = k
    if k == "M" then
        if swallow > 0 then swallow = swallow - 1
        elseif widgets.JournalMap then widgets.JournalMap = nil
        else W("JournalMap", {}, {}) end
    elseif k == "ESCAPE" then
        if widgets.GameMenu then widgets.GameMenu = nil
        elseif widgets.JournalMap then widgets.JournalMap = nil
        else W("GameMenu", {}, {}) end
    end
end
widgets = {}
swallow = 1
local wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "done" and wj.result.map_presses == 2, "swallowed M -> pressed again, map opened: "
    .. tostring(wj.status) .. " " .. tostring(wj.error) .. " presses=" .. tostring(wj.result and wj.result.map_presses))
ok(widgets.JournalMap == nil, "ui.waypoints closed the map it opened")
widgets, mapKeys = {}, {}
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "done" and wj.result.map_presses == 1 and #mapKeys == 2, "normal open: one M, one ESCAPE ("
    .. table.concat(mapKeys, ",") .. ")")
-- a stray ESCAPE left the game menu up: M does nothing there, so close it first
widgets, mapKeys = {}, {}
W("GameMenu", {}, {})
local _inj = Ext.Input.InjectKeyPress
Ext.Input.InjectKeyPress = function(k) if k == "M" and widgets.GameMenu then mapKeys[#mapKeys + 1] = k return end _inj(k) end
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "done" and mapKeys[1] == "ESCAPE" and mapKeys[2] == "M", "game menu closed, then M: " .. table.concat(mapKeys, ","))
Ext.Input.InjectKeyPress = _inj
-- the map never opens: 3 presses, then a failure that says what is on screen
widgets, mapKeys = {}, {}
swallow = 99
W("Tooltip", {}, {})
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "failed" and #mapKeys == 5 and tostring(wj.error):find("pressed M,M,M,M,M over 6%.0s")
    and tostring(wj.error):find("Tooltip"), "never opens: " .. tostring(wj.error))
swallow = 0
-- S055 06:46:20: the Act 3 map shows ~2.5 s after M; the 1.2 s re-press closed it again ("pressed M,M,M,M,M").
-- A JournalMap that appeared after our press but isn't visible yet is opening: wait, one M only.
do
    widgets, mapKeys = {}, {}
    local shownAt
    local _inj2 = Ext.Input.InjectKeyPress
    Ext.Input.InjectKeyPress = function(k)
        mapKeys[#mapKeys + 1] = k
        if k == "M" then
            if widgets.JournalMap then widgets.JournalMap = nil shownAt = nil
            else W("JournalMap", {}, {}).visible = false shownAt = clock + 2500 end
        elseif k == "ESCAPE" and widgets.JournalMap then widgets.JournalMap = nil end
    end
    local _wid = BG3AI.UI.widget
    BG3AI.UI.widget = function(n)
        local w = widgets[n]
        if w and n == "JournalMap" and shownAt and clock >= shownAt then w.visible = true end
        return w
    end
    local sj = run(ops["ui.waypoints"]({}).job, 8000)
    ok(sj.status == "done" and sj.result.map_presses == 1 and mapKeys[1] == "M" and mapKeys[2] ~= "M",
        "slow map: one M, waited for it: " .. tostring(sj.status) .. " " .. tostring(sj.error) .. " keys="
        .. table.concat(mapKeys, ","))
    BG3AI.UI.widget = _wid
    Ext.Input.InjectKeyPress = _inj2
    widgets = {}
end
-- a question box up instead of the map (vis live: a rest prompt during fast_travel): reported, not answered
widgets, mapKeys = {}, {}
swallow = 99
local restAnswered = false
W("Dialog_box", { "Long Rest", "Do you want to rest at camp?" }, { { "Yes", nil, function() restAnswered = true end },
    { "No", nil, function() end } })
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "failed" and wj.result.unanswered and tostring(wj.error):find("not answered", 1, true)
    and not restAnswered, "box instead of the map: reported, not answered: " .. tostring(wj.error))
widgets = {}
swallow = 0
-- travelVeto: rest/camp questions are never answered by fast_travel; travel questions are
ok(BG3AI.Services.travelVeto("Do you want to end the day?") ~= nil and BG3AI.Services.travelVeto("Long Rest") ~= nil,
    "veto: rest questions")
ok(BG3AI.Services.travelVeto("Travelling here will leave the region. Continue?") == nil
    and BG3AI.Services.travelVeto("[ForceUpdate]") == nil
    and BG3AI.Services.travelVeto("Leave the forest? Your interest awaits") == nil, "veto: travel/unreadable questions pass")
W("Dialog_box", { "Long Rest", "Do you want to rest?" }, { { "Yes", nil, function() restAnswered = true end },
    { "No", nil, function() end } })
local vst = {}
local vc, vres, verr = BG3AI.Services.handleConfirm(vst, {}, nil, BG3AI.Services.travelVeto)
ok(vc == "failed" and vres.unanswered and not restAnswered and tostring(verr):find("decide with confirm", 1, true),
    "handleConfirm with the veto leaves a rest question: " .. tostring(verr))
widgets = {}
-- fast_travel through the same path: swallowed press, then the waypoint is found and pressed
widgets, mapKeys = {}, {}
swallow = 1
local _byName, _content, _findAll = BG3AI.UI.byName, BG3AI.UI.content, BG3AI.UI.findAll
local btn = { name = "WaypointEntryButton" }
BG3AI.UI.byName = function(w, n) if n == "Waypoints" then return { list = true } end return _byName(w, n) end
BG3AI.UI.content = function() return { { t = "Wilderness" }, { n = "WaypointEntryButton", b = "The Risen Road" } } end
BG3AI.UI.findAll = function(el, pred) if el.list then return { btn } end return _findAll(el, pred) end
local executed
local _exec = BG3AI.UI.execute
BG3AI.UI.execute = function() executed = true return true end
local ftj2 = jobs[ops["ui.fast_travel"]({ waypoint = "Risen Road" }).job]
for _ = 1, 60 do clock = clock + 50 ftj2.on_tick(ftj2) if executed then break end end
ok(executed and ftj2.status == "running", "fast_travel pressed the waypoint after a swallowed M: "
    .. tostring(ftj2.status) .. " " .. tostring(ftj2.error))

-- right after a load TravelError reads "Unavailable" for a few s (live 2026-10-10): wait, no M meanwhile,
-- then travel; other errors and a lasting Unavailable still fail
local travelErr = "Unavailable"
local _path = BG3AI.UI.path
BG3AI.UI.path = function(dc, p) if p == "CurrentPlayer.TravelError" then return travelErr end return _path(dc, p) end
widgets, mapKeys, executed, swallow = {}, {}, false, 0
local ftj3 = jobs[ops["ui.fast_travel"]({ waypoint = "Risen Road" }).job]
for _ = 1, 40 do clock = clock + 100 ftj3.on_tick(ftj3) end
ok(ftj3.status == "running" and #mapKeys == 0, "Unavailable: waits without pressing M (" .. table.concat(mapKeys, ",") .. ")")
travelErr = nil
for _ = 1, 60 do clock = clock + 50 ftj3.on_tick(ftj3) if executed then break end end
ok(executed and mapKeys[1] == "M", "Unavailable cleared after 4 s: map opened, waypoint pressed")
travelErr, mapKeys = "Unavailable", {}
widgets = {}
local ftj4 = jobs[ops["ui.fast_travel"]({ waypoint = "Risen Road" }).job]
for _ = 1, 100 do clock = clock + 100 ftj4.on_tick(ftj4) if ftj4.status ~= "running" then break end end
ok(ftj4.status == "failed" and tostring(ftj4.error):find("Unavailable for 8 s") and #mapKeys == 0,
    "lasting Unavailable fails after 8 s: " .. tostring(ftj4.error))
travelErr = "Location"
local okl, el = pcall(ops["ui.fast_travel"], { waypoint = "Risen Road" })
ok(not okl and tostring(el):find("can't fast travel now %(Location%)"), "other travel errors fail at once: " .. tostring(el))
BG3AI.UI.path = _path
BG3AI.UI.byName, BG3AI.UI.content, BG3AI.UI.findAll, BG3AI.UI.execute = _byName, _content, _findAll, _exec

-- night camp (live 2026-10-10 11:1x): IsAtNightCamp + TravelError Unavailable for hours after a long rest whose
-- night never ended. fast_travel says so at once; long_rest at night goes to sleep (CampTravel) and waits for the
-- night to end; sleep_only refuses a fresh rest screen.
local night = true
inCamp = true
local _path2 = BG3AI.UI.path
BG3AI.UI.path = function(dc, p)
    if p == "CurrentPlayer.IsAtNightCamp" then return night end
    if p == "CurrentPlayer.TravelError" then return night and "Unavailable" or nil end
    return _path2(dc, p)
end
local okn, en = pcall(ops["ui.fast_travel"], { waypoint = "Risen Road" })
ok(not okn and tostring(en):find("night at camp", 1, true), "night camp: fast_travel says why at once: " .. tostring(en))
local tb = ops["ui.travel_block"]({})
ok(tb.night_camp == true and tb.error == "Unavailable", "travel_block reports night_camp")

local _cmd = BG3AI.UI.command
local sleeps = 0
local sleepEnds = true
BG3AI.UI.command = function(dc, name, param, dry)
    if name == "CampTravel" and not dry then
        sleeps = sleeps + 1
        if sleepEnds then night = false end
        return true
    end
    return _cmd(dc, name, param, dry)
end
clock = clock + 60000
widgets = {}
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
ok(sleeps == 1 and j.status == "done" and j.result.night_ended and j.result.rest == "night_finished",
    "night camp: long_rest goes to sleep and ends the night: " .. tostring(j.status) .. " " .. tostring(j.error))

-- the press does nothing: one retry after 4 s, then a failure that says travel stays blocked
clock, night, sleeps, sleepEnds = clock + 60000, true, 0, false
widgets = {}
r = ops["ui.long_rest"]({})
j = run(r.job, 40000)
ok(sleeps == 2 and j.status == "failed" and tostring(j.error):find("didn't end", 1, true)
    and tostring(j.error):find("blocked", 1, true), "night that won't end: 2 presses, then fails: " .. sleeps .. " "
    .. tostring(j.status) .. " " .. tostring(j.error))

-- a camp scene at night: done, and the note says to sleep after it
clock, night, sleeps = clock + 60000, true, 0
widgets = {}
BG3AI.UI.command = function(dc, name, param, dry)
    if name == "CampTravel" and not dry then sleeps = sleeps + 1 W("Dialogue", {}, {}) return true end
    return _cmd(dc, name, param, dry)
end
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
ok(j.status == "done" and j.result.camp_scene and j.result.night_open and tostring(j.result.note):find("long_rest again"),
    "night camp scene: done with a note: " .. tostring(j.status) .. " " .. tostring(j.result and j.result.note))

-- sleep_only (the bridge before travelling): the rest screen that opens at night is rested through, partial allowed
clock, sleeps = clock + 60000, 0
widgets = {}
local restCmds = {}
BG3AI.UI.command = function(dc, name, param, dry)
    if name == "CampTravel" and not dry then sleeps = sleeps + 1 W("MakeCamp", {}, {}).DataContext = { dc = "MakeCamp" } return true end
    if dc and dc.dc == "MakeCamp" then
        restCmds[#restCmds + 1] = name
        if name == "ProcessRestCommand" then widgets.MakeCamp = nil night = false end
        return true
    end
    return _cmd(dc, name, param, dry)
end
r = ops["ui.long_rest"]({ sleep_only = true })
j = run(r.job, 30000)
ok(j.status == "done" and restCmds[1] == "AutoSelectCommand" and restCmds[#restCmds] == "ProcessRestCommand",
    "sleep_only: rests from the night rest screen: " .. tostring(j.status) .. " " .. tostring(j.error) .. " "
    .. table.concat(restCmds, ","))
BG3AI.UI.command, BG3AI.UI.path = _cmd, _path2

-- ui.roll: a start command sent while the roll panel is still opening is dropped (live 2026-10-10 lockpick:
-- WaitForStart for 20 s, timeout). The job sends it again until the roll starts.
do
    local roll = { RollState = "WaitForStart", starts = 0, accept_after = 2 }
    widgets = { ActiveRoll = { name = "ActiveRoll", visible = true, DataContext = roll } }
    local _c, _p = BG3AI.UI.command, BG3AI.UI.prop
    BG3AI.UI.prop = function(e, k) if e == roll then return roll[k] end return _p(e, k) end
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then
            if dry then return true end
            roll.starts = roll.starts + 1
            if roll.starts > roll.accept_after then roll.RollState = "ResultReady" end   -- the first ones are dropped
            return true
        end
        return _c(dc, name, param, dry)
    end
    BG3AI.UI.widgetMap = BG3AI.UI.widgetMap or function() return { ActiveRoll = { el = widgets.ActiveRoll } } end
    BG3AI.UIState.roll = BG3AI.UIState.roll or function() return { phase = "result" } end
    local jr = run(ops["ui.roll"]({}).job, 10000)
    ok(jr.status == "done" and roll.starts == 3 and jr.result.retriggered == 2,
        "ui.roll re-sends the start until the roll starts: " .. tostring(jr.status) .. " starts=" .. roll.starts)
    -- an accepted first command: no re-send
    roll.RollState, roll.starts, roll.accept_after = "WaitForStart", 0, 0
    jr = run(ops["ui.roll"]({}).job, 10000)
    ok(jr.status == "done" and roll.starts == 1 and jr.result.retriggered == nil, "ui.roll: one start when it takes")
    ok(type(jr.result.phases) == "string" and jr.result.phases:find("ResultReady@", 1, true) ~= nil,
        "ui.roll: the state timeline rides along for metrics: " .. tostring(jr.result.phases))

    -- Inspiration reroll (S013-S026: 7/7 returned the first failed roll). RollAgainCommand leaves ResultReady a
    -- few frames later for WaitForReRoll, where the dice wait to be thrown again (like WaitForStart).
    local again = 0
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "RollAgainCommand" then
            if dry then return true end
            again = again + 1
            roll.pending = 3   -- ticks until the game switches state
            return true
        end
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then
            if dry then return true end
            roll.starts = roll.starts + 1
            if roll.RollState == "WaitForReRoll" then roll.RollState = "StartRoll" roll.pending = 4 end
            return true
        end
        return _c(dc, name, param, dry)
    end
    BG3AI.UI.prop = function(e, k)
        if e == roll then
            if k == "RollState" and roll.pending then
                roll.pending = roll.pending - 1
                if roll.pending <= 0 then
                    roll.pending = nil
                    roll.RollState = (roll.RollState == "ResultReady") and "WaitForReRoll" or "ResultReady"
                end
            end
            return roll[k]
        end
        return _p(e, k)
    end
    roll.RollState, roll.starts = "ResultReady", 0
    jr = run(ops["ui.roll"]({ reroll = true }).job, 10000)
    ok(jr.status == "done" and again == 1 and roll.starts == 1 and jr.result.rerolled == true
        and roll.RollState == "ResultReady" and (jr.result.phases or ""):find("WaitForReRoll", 1, true) ~= nil,
        "ui.roll reroll: waits out the old result, throws the dice again, returns the new result: "
        .. tostring(jr.status) .. " starts=" .. roll.starts .. " " .. tostring(jr.result.phases))
    -- the game ignores the reroll (state never leaves ResultReady): an honest reroll_ignored, no endless wait
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "RollAgainCommand" then return true end
        return _c(dc, name, param, dry)
    end
    roll.RollState, roll.pending = "ResultReady", nil
    jr = run(ops["ui.roll"]({ reroll = true }).job, 10000)
    ok(jr.status == "done" and jr.result.reroll_ignored == true, "ui.roll reroll ignored by the game: reported")
    -- entering at WaitForReRoll (a reroll pressed earlier) throws the dice like WaitForStart
    roll.RollState, roll.starts = "WaitForReRoll", 0
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then
            if dry then return true end
            roll.starts = roll.starts + 1
            roll.RollState = "ResultReady"
            return true
        end
        return _c(dc, name, param, dry)
    end
    jr = run(ops["ui.roll"]({}).job, 10000)
    ok(jr.status == "done" and roll.starts == 1 and roll.RollState == "ResultReady", "ui.roll at WaitForReRoll rolls")

    -- boosts by name (S024: roll_check(boosts="Guidance") failed "attempt to concatenate a nil value")
    local guidance = { Name = "Guidance" }
    local _path = BG3AI.UI.path
    BG3AI.UI.path = function(o, p)
        if o == roll and p == "BoostModifierList.1" then return guidance end
        if o == roll and p:match("^BoostModifierList%.") then return nil end
        return _path(o, p)
    end
    local _p2 = BG3AI.UI.prop
    BG3AI.UI.prop = function(e, k) if e == guidance then return guidance[k] end return _p2(e, k) end
    local addedWith
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "AddModifierCommand" then addedWith = param return true end
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then roll.RollState = "ResultReady" return true end
        return _c(dc, name, param, dry)
    end
    roll.RollState = "WaitForStart"
    jr = run(ops["ui.roll"]({ boosts = { "guidance" } }).job, 10000)
    ok(addedWith == guidance and jr.status == "done", "ui.roll boosts by name: Guidance added")
    roll.RollState = "WaitForStart"
    local okb, errb = pcall(ops["ui.roll"], { boosts = { "Bardic Inspiration" } })
    ok(not okb and tostring(errb):find("offered: Guidance", 1, true) ~= nil,
        "ui.roll unknown boost: names what is offered: " .. tostring(errb))
    BG3AI.UI.path = _path
    BG3AI.UI.command, BG3AI.UI.prop = _c, _p
    widgets = {}
end

-- Stack-split box (S028 15:51-15:52): confirm() answers it (amount sets TargetCount), the ops' automatic
-- answers never do, and long_rest cancels a leftover one before pressing CampTravel.
do
    widgets = {}
    local data = { ItemCount = 3, TargetCount = 1, Item = { Name = "Supply Pack" } }
    data.SetProperty = function(self, k, v) self[k] = v end
    local pressed = {}
    local split = W("SplitItem", {}, {
        { "Cancel", nil, function() pressed[#pressed + 1] = "Cancel" widgets.SplitItem = nil end },
        { "", "ConfirmSplitButton", function() pressed[#pressed + 1] = "Confirm" widgets.SplitItem = nil end } })
    split.DataContext = { dc = "SplitItem" }
    local _path, _prop = BG3AI.UI.path, BG3AI.UI.prop
    BG3AI.UI.path = function(dc, p)
        if type(dc) == "table" and dc.dc == "SplitItem" and p == "CurrentPlayer.UIData.SplitItemData" then return data end
        return _path(dc, p)
    end
    BG3AI.UI.prop = function(e, k)
        if e == data or e == data.Item then return e[k] end
        return _prop(e, k)
    end
    ok(A.confirmBox() == nil, "split box: not a question box (handleConfirm never confirms a quantity)")
    local sb = A.splitBox()
    ok(sb and sb.max == 3 and sb.title == "Supply Pack" and #sb.buttons == 2 and sb.buttons[2].label == "Confirm",
        "split box read: Supply Pack 1/3, Cancel/Confirm")
    local okr, err = pcall(ops["ui.confirm"], { answer = "yes", amount = 5 })
    ok(not okr and tostring(err):find("out of range", 1, true) ~= nil, "split amount out of range refused: " .. tostring(err))
    local r = ops["ui.confirm"]({ answer = "yes", amount = 2 })
    local j = run(r.job, 4000)
    ok(data.TargetCount == 2 and pressed[1] == "Confirm" and j.status == "done",
        "confirm(yes, amount=2) on the split box: " .. tostring(data.TargetCount) .. " " .. tostring(pressed[1]))

    -- a stale one (no Trade screen) before long_rest: cancelled, CampTravel pressed after it closed
    pressed = {}
    W("SplitItem", {}, {
        { "Cancel", nil, function() pressed[#pressed + 1] = "Cancel" widgets.SplitItem = nil end },
        { "", "ConfirmSplitButton", function() pressed[#pressed + 1] = "Confirm" end } }).DataContext = { dc = "SplitItem" }
    W("Trade", {}, {})
    ok(A.cancelStaleSplit() == nil and #pressed == 0, "split box under an open trade: left alone")
    widgets.Trade = nil
    commands = {}
    local okl, rl = pcall(ops["ui.long_rest"], {})
    ok(okl and pressed[1] == "Cancel" and #commands == 0,
        "long_rest: a leftover split box is cancelled, CampTravel not pressed under it: " .. tostring(rl)
        .. " pressed=" .. tostring(pressed[1]) .. " commands=" .. #commands)
    if okl and rl and rl.job then
        for _ = 1, 12 do clock = clock + 50 jobs[rl.job].on_tick(jobs[rl.job]) end
        ok(commands[1] == "CampTravel", "long_rest: CampTravel pressed once the split box closed")
    end
    BG3AI.UI.path, BG3AI.UI.prop = _path, _prop
    widgets = {}
end

-- The Long Rest button greyed out (EndTheDayState Disabled): refused at once, CampTravel not pressed (S028 15:53).
do
    widgets = {}
    local _path = BG3AI.UI.path
    local state = "Disabled"
    BG3AI.UI.path = function(dc, p)
        if p == "CurrentPlayer.EndTheDayState" then return state end
        return _path(dc, p)
    end
    commands = {}
    local okd, errd = pcall(ops["ui.long_rest"], {})
    ok(not okd and tostring(errd):find("greyed out", 1, true) ~= nil and #commands == 0,
        "long_rest with the button greyed out: refused, no CampTravel: " .. tostring(errd))
    state = "Enabled"
    local oke, re = pcall(ops["ui.long_rest"], {})
    ok(oke and commands[1] == "CampTravel", "long_rest with the button enabled: CampTravel pressed")
    BG3AI.UI.path = _path
    widgets = {}
end

-- Insert Item panel (ui.insert): lists the selected character's items, ToggleItemCommand(VM item) fills the
-- slot, StartActionCommand (no parameter) presses Insert, Success is done (S031 Silent Library pedestal).
do
    widgets = {}
    local saved = {}
    for _, k in ipairs({ "get", "prop", "props", "items", "count", "command" }) do saved[k] = BG3AI.UI[k] end
    -- VM items carry no name/uuid (live 17:5x): the entity comes from the party inventory slot (index 1, 2).
    local book = { IsSelected = false }
    local other = { IsSelected = false }
    local slots = { { Index = 1, Object = other }, { Index = 2, Object = book }, { Index = 3 } }
    local function ent(u, n) return { Uuid = { EntityUuid = u }, DisplayName = { NameKey = { Handle = { Handle = n } } } } end
    local INV = { InventoryContainer = { Items = { [1] = { Item = ent("uuid-other", "Sharran Initiate's Notes") },
                                                   [2] = { Item = ent("uuid-book", "Teachings of Loss: The Nightsinger") } } } }
    local P = { InventoryOwner = { PrimaryInventory = INV } }
    local _comp, _loca, _entity = BG3AI.Util.comp, BG3AI.Util.loca, Ext.Entity
    BG3AI.Util.comp = function(e, c) return type(e) == "table" and e[c] or nil end
    BG3AI.Util.loca = function(h) return h end
    Ext.Entity = { GetAllEntitiesWithComponent = function() return { P } end, Get = function() return nil end }
    local dc = { CurrentState = "Empty", Ingredients = { false }, BaseItem = { Name = "Riddle of the Night" } }
    dc.CharacterInventory = { Slots = slots }
    local cmds = {}
    BG3AI.UI.get = function(o, k) if type(o) == "table" then local v = o[k] if v == false then return nil end return v end end
    BG3AI.UI.prop = BG3AI.UI.get
    BG3AI.UI.props = function(o) return type(o) == "table" and o or {} end
    BG3AI.UI.count = function(c) return type(c) == "table" and #c or 0 end
    BG3AI.UI.items = function(c, max)
        local i = 0
        return function()
            i = i + 1
            if type(c) ~= "table" or i > #c or (max and i > max) then return nil end
            local v = c[i]
            if v == false then v = nil end
            return i, v
        end
    end
    BG3AI.UI.command = function(o, name, param, dry)
        if o ~= dc then return false, "wrong object" end
        if dry then return true end
        cmds[#cmds + 1] = name
        if name == "ToggleItemCommand" then
            param.IsSelected = true
            if dc.multi then  -- a 3-slot altar: the first empty slot takes it, the panel waits for the rest
                for i = 1, #dc.Ingredients do if dc.Ingredients[i] == false then dc.Ingredients[i] = param break end end
                dc.CurrentState = "Preparing"
                return true
            end
            dc.Ingredients = { param }
            dc.CurrentState = "Ready"
            return true
        end
        if name == "StartActionCommand" then
            if param ~= nil then return false, "param" end
            dc.CurrentState = dc.willFail and "Fail" or "Success"
            dc.FailReason = dc.willFail and "Invalid" or nil
            return true
        end
        return false, "no command '" .. name .. "'"
    end
    local okn, errn = pcall(ops["ui.insert"], {})
    ok(not okn and tostring(errn):find("no Insert Item panel", 1, true), "insert: no panel → says how to open it: " .. tostring(errn))
    widgets.Insert = { name = "Insert", visible = true, DataContext = dc, buttons = {}, texts = {} }
    local okl, lst = pcall(ops["ui.insert"], {})
    ok(okl and lst.panel == "Riddle of the Night" and #lst.items == 2 and lst.items[2].name == "Teachings of Loss: The Nightsinger"
        and lst.items[2].uuid == "uuid-book" and lst.slots[1] == false, "insert: list names the panel, the items and the empty slot")
    local okm, errm = pcall(ops["ui.insert"], { item = "uuid-missing" })
    ok(not okm and tostring(errm):find("Sharran Initiate's Notes", 1, true), "insert: unknown item → the bag's names: " .. tostring(errm))
    local oki, ri = pcall(ops["ui.insert"], { item = "uuid-book" })
    ok(oki and ri.job ~= nil, "insert: a job")
    if oki and ri.job then
        clock = clock + 1
        local j = run(ri.job, 5000)
        ok(j.status == "done" and j.result.inserted == "Teachings of Loss: The Nightsinger" and j.result.state == "Success",
            "insert: done with Success: " .. tostring(j.status) .. " " .. tostring(j.error))
        ok(cmds[1] == "ToggleItemCommand" and cmds[2] == "StartActionCommand", "insert: toggle then Insert: " .. table.concat(cmds, ","))
    end
    -- a refused item: Fail + FailReason, reported (not "inserted")
    book.IsSelected, dc.CurrentState, dc.Ingredients, dc.willFail, cmds = false, "Empty", { false }, true, {}
    local okf, rf = pcall(ops["ui.insert"], { item = "Nightsinger" })
    if okf and rf.job then
        local j = run(rf.job, 5000)
        ok(j.status == "done" and j.result.inserted == nil and j.result.state == "Fail" and j.result.fail_reason == "Invalid",
            "insert: refused → Fail + reason, by name match")
    else
        ok(false, "insert by name: " .. tostring(rf))
    end
    -- S037 20:23: the Verge altar's 3 gem slots: a gem in its slot with slots left is a partial fill, not a failure
    book.IsSelected, dc.CurrentState, dc.Ingredients, dc.willFail, dc.multi, cmds = false, "Empty", { false, false, false }, nil, true, {}
    local okp, rp = pcall(ops["ui.insert"], { item = "uuid-book" })
    if okp and rp.job then
        local j = run(rp.job, 5000)
        ok(j.status == "done" and j.result.placed == "Teachings of Loss: The Nightsinger" and j.result.filled == 1
            and j.result.slots_total == 3 and j.result.inserted == nil,
            "insert: partial fill of a 3-slot panel → done, placed 1/3: " .. tostring(j.status) .. " " .. tostring(j.error))
        ok(cmds[#cmds] == "ToggleItemCommand", "insert: partial fill presses no Insert: " .. table.concat(cmds, ","))
    else
        ok(false, "insert partial: " .. tostring(rp))
    end
    dc.multi = nil
    for k, v in pairs(saved) do BG3AI.UI[k] = v end
    BG3AI.Util.comp, BG3AI.Util.loca, Ext.Entity = _comp, _loca, _entity
    widgets = {}
end

-- live window #1 (10-16 18:34): load_game right after the launch hit the loading screen ("no command
-- 'OpenLoadGameDialog' on ls.LoadingScreen"): the load job now waits for the loading screen, then opens the page
do
    local _cmd, _get = BG3AI.UI.command, BG3AI.UI.get
    local opened = 0
    BG3AI.UI.get = function(o, k) return type(o) == "table" and o[k] or nil end
    BG3AI.UI.command = function(dc, name, param, dry)
        if name == "OpenLoadGameDialog" and not dry then
            if widgets.root then return false, "no command 'OpenLoadGameDialog' on ls.LoadingScreen" end
            opened = opened + 1
            W("LoadGame", {}, {})
        end
        return true
    end
    local _le = A.LOAD_ENABLED
    A.LOAD_ENABLED = true
    widgets = {}
    W("root", {}, {})                       -- the loading screen (AutoSkip: widget "root", DC ls.LoadingScreen)
    ok(A.loadingScreenUp() == true, "loading screen seen")
    local r = ops["ui.menu"]({ action = "load", name = "Claude-4810126690__S027-start" })
    local j = run(r.job, 6000)
    ok(j.status == "running" and opened == 0 and j.partial and j.partial.waiting == "loading screen",
        "load waits while the loading screen is up (no press): " .. tostring(j.status) .. " " .. tostring(j.error))
    widgets.root = nil
    j = run(r.job, 1000)
    ok(opened == 1 and j.partial and j.partial.stage == "select", "loading screen gone -> load page opened, select stage")
    -- a loading screen that never goes: a clear failure, still no press
    widgets, opened = {}, 0
    W("root", {}, {})
    r = ops["ui.menu"]({ action = "load", name = "Claude-4810126690__S027-start" })
    j = run(r.job, 29000)
    ok(j.status == "failed" and opened == 0 and j.result and j.result.loading == true
        and tostring(j.error):find("loading screen stayed up", 1, true), "loading screen stuck -> failed loading=true: " .. tostring(j.error))
    -- the DC: never the loading screen's
    widgets = {}
    W("root", {}, {}).DataContext = { CurrentPlayer = {}, OpenLoadGameDialog = {} }
    W("HUD", {}, {}).DataContext = { CurrentPlayer = {}, OpenLoadGameDialog = { hud = true } }
    local _ws = BG3AI.UI.widgets
    BG3AI.UI.widgets = function()
        local out = _ws()
        for _, w in ipairs(out) do if w.name == "root" then w.dc = "ls.LoadingScreen" end end
        return out
    end
    local dc = A.commandDC("OpenLoadGameDialog")
    ok(dc and dc.OpenLoadGameDialog.hud == true, "commandDC skips the loading screen's DC")
    BG3AI.UI.widgets = _ws
    widgets = {}
    A.LOAD_ENABLED, BG3AI.UI.command, BG3AI.UI.get = _le, _cmd, _get
end

print(("%d passed, %d failed"):format(passes, fails))
os.exit(fails == 0 and 0 or 1)
