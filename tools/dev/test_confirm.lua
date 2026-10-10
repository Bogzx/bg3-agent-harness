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
widgets = { HotBar = { name = "HotBar", visible = true, texts = {}, buttons = {} } }
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
ok(e:find("widgets=[HotBar]", 1, true), "message says what's on screen: " .. e)
ok(#commands == nCamp + 2 and j.result.step == "open", "pressed twice, step=open")

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
ok(wj.status == "failed" and #mapKeys == 3 and tostring(wj.error):find("pressed M,M,M over 3%.6s")
    and tostring(wj.error):find("Tooltip"), "never opens: " .. tostring(wj.error))
swallow = 0
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
BG3AI.UI.byName, BG3AI.UI.content, BG3AI.UI.findAll, BG3AI.UI.execute = _byName, _content, _findAll, _exec

print(("%d passed, %d failed"):format(passes, fails))
os.exit(fails == 0 and 0 or 1)
