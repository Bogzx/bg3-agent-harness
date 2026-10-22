-- Part of test_confirm.lua (split for the 1000-line cap): confirm boxes, rest flows, box_open, ui.key and the save/load escape hatches.
-- test_confirm.lua runs it after its stubs and helpers, which it shares as globals.

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
r = ops["ui.confirm"]({ answer = "yes" })
j = run(r.job)
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

-- long rest with confirm=false: the unreadable "[ForceUpdate]" box (End the day?) is answered (S077 r1: it
-- stopped there and the agent clicked it by hand); the readable question after it stops
widgets = {}
r = ops["ui.long_rest"]({ confirm = false })
j = jobs[r.job]
for _ = 1, 40 do clock = clock + 50 j.on_tick(j) if j.status ~= "running" then break end end
ok(j.status == "failed" and tostring(j.error):find("answer with confirm"), "confirm=false fails with the question: " .. tostring(j.error))
ok(tostring(j.error):find("Someone wants to talk", 1, true), "stopped at the readable one: " .. tostring(j.error))
ok(j.confirms and #j.confirms == 1 and j.confirms[1]:find("[ForceUpdate]", 1, true), "the [ForceUpdate] box answered: "
    .. tostring(j.confirms and j.confirms[1]))
ok(widgets.Dialog_box ~= nil, "box left open")
ok(BG3AI.Services.unreadable("[ForceUpdate]") and BG3AI.Services.unreadable(nil)
    and BG3AI.Services.unreadable(" [ForceUpdate] ") and not BG3AI.Services.unreadable("End the day?"), "unreadable()")


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

-- ui.box_open (A18, the bridge's watch after its click): visibility of the top-level widget only, never the
-- buttons' IsMouseOver; nothing UI is read once the game is in a transition.
W("Dialog_box", { "[ForceUpdate]" }, { { "Yes", nil, function() end }, { "No", nil, function() end } })
local UI, overReads = BG3AI.UI, 0
local prop0 = UI.prop
UI.prop = function(e, k) if k == "IsMouseOver" then overReads = overReads + 1 end return prop0(e, k) end
local bo = ops["ui.box_open"]()
ok(bo.box == true and bo.state == "Running" and overReads == 0, "box_open: box shown, no IsMouseOver read")
widgets.Dialog_box.visible = false
ok(ops["ui.box_open"]().box == false, "box_open: hidden box = closed")
widgets = {}
W("SplitItem", {}, { { "Cancel", nil, function() end } })
ok(ops["ui.box_open"]().box == true, "box_open: split box")
local gs0, widgetReads, widgets0 = Ext.Utils.GetGameState, 0, UI.widgets
Ext.Utils.GetGameState = function() return "UnloadLevel" end
UI.widgets = function() widgetReads = widgetReads + 1 return widgets0() end
bo = ops["ui.box_open"]()
ok(bo.box == false and bo.state == "UnloadLevel" and widgetReads == 0, "box_open: transition, no UI read")
Ext.Utils.GetGameState, UI.widgets, UI.prop = gs0, widgets0, prop0
widgets = {}

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

-- live5 U12: GameMenu's Resume passes the string "CloseGameMenu" (bg3le can't pass it) and has no binding we map:
-- pressButton presses ESCAPE instead; a string parameter nobody maps still fails with bg3le's reason.
do
    local _press, _get, _ikp = BG3AI.UI.press, BG3AI.UI.get, Ext.Input.InjectKeyPress
    Ext.Input.InjectKeyPress = function(k) keys[#keys + 1] = k end
    local stringParam = "parameter is a string (CloseGameMenu); bg3le can only pass Noesis objects"
    BG3AI.UI.press = function() return false, stringParam end
    local props = { CommandParameter = "CloseGameMenu" }
    BG3AI.UI.get = function(_, k) return props[k] end
    keys = {}
    local okp, how = A.pressButton({ name = "ResumeBtn" }, false)
    ok(okp and how == "key:ESCAPE" and keys[#keys] == "ESCAPE",
        "Resume (CloseGameMenu) presses ESCAPE: " .. tostring(how))
    keys = {}
    okp, how = A.pressButton({ name = "ResumeBtn" }, true)
    ok(okp and how == "key:ESCAPE" and #keys == 0, "a dry Resume names the key and presses nothing")
    props = { CommandParameter = "SomethingElse" }
    okp, how = A.pressButton({ name = "OtherBtn" }, false)
    ok(not okp and how == stringParam and #keys == 0, "an unmapped string parameter still fails: " .. tostring(how))
    props = { BoundEvent = "UICancel", CommandParameter = "SomethingElse" }
    okp, how = A.pressButton({ name = "BackBtn" }, false)
    ok(okp and how == "key:ESCAPE", "a BoundEvent still wins: " .. tostring(how))
    BG3AI.UI.press, BG3AI.UI.get, Ext.Input.InjectKeyPress = _press, _get, _ikp
    keys = {}
end
