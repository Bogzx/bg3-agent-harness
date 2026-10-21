-- Offline tests for the named-save driver (worker se): UIActions saveDriver
-- walks Save page -> name box -> accept -> back to the game, reports where
-- its time went (result.ms), and UIState hides our own save menus (screen
-- stays "game", saving={page}) so a running queue isn't interrupted.
-- Ext, RPC and UI are stubbed; the menus react to injected keys a tick later.
-- Run: tools/dev/test_save.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local clock = 1000
local keys = {}
local widgets = {}   -- name -> {visible=true}
local pendingKey = {}

-- the client state: a RETURN on the name box starts a Save state 500 ms
-- later, lasting 1000 ms (live: 0.3-1 s after the accept, ~1 s long);
-- saveStarts=false: the game never goes through Save
local gameState, saveAt, saveStarts = "Running", nil, true
local stateHandlers = {}
local function setState(to)
    local from = gameState
    gameState = to
    for _, h in ipairs(stateHandlers) do h({ FromState = from, ToState = to }) end
end

local nameBox = { t = "" }    -- the popup's SaveName box (mode: nil = Text is a string, "boxed" = a boxed value,
                              -- "classname" = its ToString() is the class name, "none" = unreadable)
local selectedRow = nil       -- audit UI-6: a save row selected on the Save page
local pressedNo = false
local defaultName = nil       -- S052: the dialog writes its default name this many frames after it opens
local overwriteIn = nil
local savedAs = nil           -- the name the game saved with (the box's text at the accepting RETURN)
local function show(n)
    widgets[n] = { name = n, visible = true }
    if n == "SaveGamePopup" then
        nameBox.t = "" overwriteIn = defaultName and 9 or nil  -- ~145 ms: after our first set (80 ms polls), before the accept
        widgets[n].DataContext = { isDC = true, SetProperty = function(_, k, v) if k == "SavegameNameInput" then nameBox.dc = v end end }
    end
end
local function hide(n) widgets[n] = nil end

Ext = {
    Utils = { MonotonicTime = function() return clock end, GetGameState = function() return gameState end },
    Events = { GameStateChanged = { Subscribe = function(_, h) stateHandlers[#stateHandlers + 1] = h end } },
    Input = { InjectKeyPress = function(k) keys[#keys + 1] = k pendingKey[#pendingKey + 1] = k end },
    IO = { LoadFile = function() return nil end },
    Json = { Parse = function() return {} end },
    Require = function(p) return dofile(LUA .. "/" .. p) end, -- UIActions.lua loads its parts
}

-- the game's menus: a key lands one frame later
local function frame()
    if overwriteIn then
        overwriteIn = overwriteIn - 1
        if overwriteIn <= 0 then nameBox.t = defaultName overwriteIn = nil end
    end
    if saveAt and clock >= saveAt and gameState == "Running" then setState("Save") end
    if saveAt and clock >= saveAt + 1000 and gameState == "Save" then setState("Running") saveAt = nil end
    local ks = pendingKey
    pendingKey = {}
    for _, k in ipairs(ks) do
        if k == "RETURN" then
            if widgets.SaveGamePopup then
                savedAs = nameBox.t
                hide("SaveGamePopup") hide("SaveGame")  -- saved: back to the game menu
                if saveStarts then saveAt = clock + 500 end
            elseif widgets.Dialog_box then
                savedAs = "OVERWRITTEN:" .. tostring(selectedRow)
                hide("Dialog_box")
            elseif widgets.SaveGame and selectedRow then show("Dialog_box")
            elseif widgets.SaveGame then show("SaveGamePopup") end
        elseif k == "ESCAPE" then
            if widgets.Dialog_box then hide("Dialog_box")
            elseif widgets.SaveGamePopup then hide("SaveGamePopup")
            elseif widgets.SaveGame then hide("SaveGame")
            elseif widgets.GameMenu then hide("GameMenu") end
        end
    end
end

local jobs, ops, nextId = {}, {}, 0
BG3AI = {
    RPC = {
        now_ms = function() return clock end,
        register = function(name, fn) ops[name] = fn end,
        push = function() end,
        start_job = function(kind, spec)
            nextId = nextId + 1
            local job = spec or {}
            job.id, job.kind, job.started, job.status = nextId, kind, clock, "running"
            job.deadline_at = clock + (job.timeout_ms or 20000)
            jobs[nextId] = job
            return job
        end,
        finish_job = function(job, status, result, err)
            if job.status ~= "running" then return end
            job.status, job.result, job.error = status, result, err
        end,
    },
    UI = {
        widget = function(n) return widgets[n] end,
        widgetMap = function()
            local m = { Hotbar = { el = { visible = true } } }
            for n, w in pairs(widgets) do m[w.listedAs or n] = { el = w, dc = w.dc } end
            return m
        end,
        isVisible = function(el) return el.visible ~= false end,
        byName = function(w, n) if n == "SaveName" then
            return { isNameBox = true, SetProperty = function(_, k, v)
                if k == "Text" and not nameBox.stuck then nameBox.t = v end end } end end,
        -- live (S056): a TextBox's ToString() is its class name; only the Text property holds the name
        text = function() return "ls.LSTextBox" end,
        clean = function(v) return v end,
        prop = function(obj, k)
            if obj and obj.isNameBox and k == "Text" then
                if nameBox.mode == "boxed" then return { ToString = function() return nameBox.t end } end
                if nameBox.mode == "classname" then return { ToString = function() return "ls.LSTextBox" end } end
                if nameBox.mode == "none" then return nil end
                return nameBox.t
            end
            if obj and obj.isDC and k == "SavegameNameInput" then return nameBox.dc end
            if obj and obj.creditsDC and k == "IsEndGameCredits" then return obj.ending end
            return nil
        end,
        get = function(obj, k) if obj and k == "Metadata" then return obj.metadata end return nil end,
        props = function() return {} end,
        findButton = function(w, which)
            if w and w.name == "Dialog_box" and which == "no" then return { isNo = true } end
            return nil
        end,
        press = function(el, dry)
            if el.isNo and not dry then pressedNo = true hide("Dialog_box") end
            return true
        end,
        findAll = function() return {} end,
        anyDC = function() return { dc = "any" } end,
        dc = function() return nil end,
        command = function(dc, name, param, dry)
            if name == "OpenSaveGameDialogCommand" then
                if not dry then show("GameMenu") show("SaveGame") end
                return true
            end
            return false, "no command " .. name
        end,
    },
    Util = { comp = function() return nil end, dist = function() return 0 end },
}

dofile(LUA .. "/Client/UIState.lua")
local S = BG3AI.UIState
S.region = function() return nil end
S.notifications = function() return nil end
S.popup = function() return { title = "menu" } end
dofile(LUA .. "/Client/UIActions.lua")
local A = BG3AI.UIActions
A.SAVE_ENABLED = true

local fails, passes = 0, 0
local function ok(cond, msg)
    if cond then passes = passes + 1 else fails = fails + 1 print("FAIL " .. msg) end
end

-- one save, driven frame by frame; ui.state sampled on every frame. While
-- the game is in its Save state the job doesn't tick (as live may).
local function runSave(name, probe)
    local r = ops["ui.menu"]({ action = "save", name = name, probe = probe })
    local job = jobs[r.job]
    local seen = { screens = {}, saving = false, doneState = nil }
    for _ = 1, 400 do
        clock = clock + 16
        frame()
        if job.status ~= "running" then break end
        if clock > job.deadline_at then job.status = "timeout" break end
        if gameState ~= "Save" then job.on_tick(job) end
        if job.status ~= "running" then seen.doneState = gameState break end
        local st = S.state({})
        seen.screens[st.screen] = true
        if st.saving then seen.saving = true end
    end
    return job, seen
end

local t0 = clock
local job, seen = runSave("S010-pre-kagha")
ok(job.status == "done", "save done: " .. tostring(job.status) .. " " .. tostring(job.error))
local res = job.result or {}
ok(res.saved and res.name == "S010-pre-kagha", "result names the save")
ok(type(res.ms) == "table" and res.ms.done and res.ms.accept and res.ms.new, "stage timings reported")
ok(res.ms and res.ms.saved and res.ms.done >= res.ms.saved, "done after the game's Save state: "
    .. tostring(res.ms and res.ms.saved))
ok(seen.doneState == "Running" and saveAt == nil, "not done before or during the Save state")
ok(res.ms and res.ms.done < 3000, "done within 3 s of game time (menus, the Save state, the game menu): " .. tostring(res.ms and res.ms.done))
ok(not res.no_save_state, "the Save state was seen")
local only = true
for s in pairs(seen.screens) do if s ~= "game" then only = false print("  screen seen: " .. s) end end
ok(only, "ui.state shows the game throughout our own save")
ok(seen.saving, "ui.state flags saving")
ok(widgets.GameMenu == nil and widgets.SaveGame == nil, "menus closed afterwards")
ok(A.pendingSave == nil, "pending save cleared")

ok(savedAs == "S010-pre-kagha", "the game saved under our name: " .. tostring(savedAs))

-- S052 05:21:17: the dialog wrote its default name after ours; RETURN saved '…Lab Level - 16h 14m'
defaultName = "Steel Watch Foundry - Lab Level - 16h 14m"
job = runSave("S052-lab-fight2")
defaultName = nil
ok(job.status == "done" and savedAs == "S052-lab-fight2" and (job.result.renamed or 0) >= 1,
    "default name overwritten back before the accept: saved as " .. tostring(savedAs) .. ", renamed="
    .. tostring(job.result and job.result.renamed))

-- name probe (acceptance): sets the name, reads it back through the Text property, ESCAPEs out, writes nothing
savedAs = nil
A.SAVE_ENABLED = false
job = runSave("probe-name", true)
A.SAVE_ENABLED = true
ok(job.status == "done" and job.result.probe and job.result.read == "probe-name" and job.result.matches == true,
    "probe reads our name back: " .. tostring(job.status) .. " read=" .. tostring(job.result and job.result.read)
    .. " err=" .. tostring(job.error))
ok(savedAs == nil and saveAt == nil, "probe wrote no save")
ok(widgets.SaveGamePopup == nil and widgets.SaveGame == nil and widgets.GameMenu == nil, "probe closed the menus")
defaultName = "Steel Watch Foundry - Lab Level - 16h 14m"
job = runSave("probe-name2", true)
defaultName = nil
ok(job.status == "done" and job.result.matches == true and (job.result.renamed or 0) >= 1,
    "probe re-sets over the dialog's default name: read=" .. tostring(job.result and job.result.read))

-- BACKLOG "save-name probe reads None": live, the Text property never read back as a Lua string (read=None,
-- renamed=None on every acceptance run). A boxed value unboxes through ToString; a class name doesn't count; an
-- unreadable box falls back to the dialog DC's SavegameNameInput; result.read_via says which.
nameBox.mode = "boxed"
job = runSave("probe-boxed", true)
ok(job.status == "done" and job.result.read == "probe-boxed" and job.result.read_via == "text",
    "probe unboxes a boxed Text: read=" .. tostring(job.result and job.result.read) .. " via=" .. tostring(job.result and job.result.read_via))
nameBox.mode = "classname"
job = runSave("probe-class", true)
ok(job.status == "done" and job.result.read == "probe-class" and job.result.read_via == "dc",
    "a class-name ToString is not the text; the DC answers: read=" .. tostring(job.result and job.result.read)
    .. " via=" .. tostring(job.result and job.result.read_via))
nameBox.mode = "none"
defaultName = "Steel Watch Foundry - Lab Level - 16h 14m"
savedAs = nil
job = runSave("S099-dc-read")
defaultName = nil
ok(job.status == "done" and savedAs == "S099-dc-read", "unreadable Text: the save still goes through under our name: "
    .. tostring(savedAs))
nameBox.mode = nil

-- audit UI-6: a save row selected → the "new" RETURN acts on it and an overwrite box comes up before our name.
-- It is answered No and the save fails closed; the 2nd RETURN that would confirm the overwrite never comes.
selectedRow = "S010-pre-kagha"
savedAs, pressedNo = nil, false
local nkeys = #keys
job = runSave("S100-selected-row")
selectedRow = nil
ok(job.status == "failed" and job.result and job.result.foreign_box == true and tostring(job.error):find("nothing saved", 1, true),
    "a box before the name fails the save: " .. tostring(job.status) .. " " .. tostring(job.error))
ok(savedAs == nil, "no overwrite confirmed: " .. tostring(savedAs))
ok(pressedNo, "the box was answered No")
local returns = 0
for i = nkeys + 1, #keys do if keys[i] == "RETURN" then returns = returns + 1 end end
ok(returns == 1, "exactly one RETURN (the 'new' one), none on the box: " .. returns)
ok(widgets.SaveGamePopup == nil and widgets.SaveGame == nil and widgets.GameMenu == nil and widgets.Dialog_box == nil,
    "menus closed after the refused overwrite")
ok(A.pendingSave == nil, "pending save cleared")

-- S056 07:17: the name never sticks → failed, and the save menus are closed first (the agent finished one by hand)
nameBox.stuck = true
savedAs = nil
job = runSave("S056-stuck")
nameBox.stuck = nil
ok(job.status == "failed" and tostring(job.error):find("keeps reading", 1, true) ~= nil,
    "a name that never sticks fails, saying so: " .. tostring(job.status) .. " " .. tostring(job.error))
ok(widgets.SaveGamePopup == nil and widgets.SaveGame == nil and widgets.GameMenu == nil,
    "a failed save closes its menus (popup/page/game menu)")
ok(savedAs == nil, "a failed name check writes no save")
ok(A.pendingSave == nil, "pending save cleared after the failure")

-- the SavingPanel overlay lingering after the Save state doesn't hold it (S015: 3 s)
show("AlwaysOnTopOverlay")
local panelUp = BG3AI.UI.byName
BG3AI.UI.byName = function(w, n, o)
    if n == "SavingPanel" then return { visible = true } end
    return panelUp(w, n, o)
end
job = runSave("S015-end")
ok(job.status == "done" and job.result.ms.done < 3000, "a lingering saving overlay is not waited for: "
    .. tostring(job.result and job.result.ms.done))
BG3AI.UI.byName = panelUp
hide("AlwaysOnTopOverlay")

-- no Save state at all: done anyway, A.SAVE_STATE_WAIT_MS after the accept, and says so
saveStarts = false
job = runSave("S017-x")
saveStarts = true
ok(job.status == "done" and job.result.no_save_state == true, "no Save state: done anyway, flagged")
ok(job.result.ms.done >= A.SAVE_STATE_WAIT_MS and job.result.ms.done < A.SAVE_STATE_WAIT_MS + 1500,
    "no Save state: gives up after the wait: " .. tostring(job.result.ms.done))

-- not our save: the game menu shows as itself
show("GameMenu")
ok(S.state({}).screen == "game_menu", "a player-opened game menu is not hidden")
hide("GameMenu")

-- a stale pending save (failed long ago) no longer hides anything
A.pendingSave = { name = "x", started = clock - 60000 }
show("SaveGamePopup")
ok(S.state({}).screen == "popup", "stale pending save doesn't hide a popup")
hide("SaveGamePopup")
A.pendingSave = nil

-- the credits page (Credits.xaml): the end credits vs the main menu's Credits button
show("Credits")
widgets.Credits.DataContext = { creditsDC = true, ending = true }
local st = S.state({})
ok(st.screen == "credits" and st.blocking and st.credits and st.credits.ending == true, "end credits: screen credits, ending")
widgets.Credits.DataContext.ending = false
ok(S.state({}).credits.ending == false, "menu credits: ending false")
widgets.Credits.DataContext.ending = nil
widgets.Credits.metadata = "Ending"
st = S.state({})
ok(st.credits.ending == true and st.credits.metadata == "Ending", "the widget's Metadata Ending alone")
widgets.Credits.metadata = nil
ok(S.state({}).credits.ending == nil, "neither reads: ending unknown")
show("MainMenu")
ok(S.state({}).screen == "credits", "credits over the main menu")
hide("MainMenu") hide("Credits")

-- live 2026-10-17 (main menu -> Credits button): the page's name didn't read (listed as "?", dc gui::DCCredits,
-- Metadata "", IsEndGameCredits false); the screen read "game" with panels_open {"?"}. Found by its DC type now.
show("MainMenu") show("Credits")
widgets.Credits.listedAs, widgets.Credits.dc = "?", "gui::DCCredits"
widgets.Credits.DataContext = { creditsDC = true, ending = false }
widgets.Credits.metadata = ""
st = S.state({})
ok(st.screen == "credits" and st.page == "Credits" and st.blocking, "nameless credits page found by its DC type")
ok(st.credits and st.credits.ending == false and st.credits.metadata == "", "menu credits by DC: ending false")
local function listed(t, n) for _, x in ipairs(t or {}) do if x == n then return true end end return false end
ok(not listed(st.panels_open, "?") and not listed(st.panels_open, "Credits"), "the credits page isn't listed as a panel")
widgets.Credits.dc = "gui::DCSomethingElse"
ok(S.state({}).screen == "main_menu", "another nameless page is not the credits")
hide("MainMenu") hide("Credits")

-- live 2026-10-17: the "Press any key" splash after a fresh launch read screen "game", panels_open {"SplashScreen"}
show("SplashScreen")
st = S.state({})
ok(st.screen == "splash" and st.page == "SplashScreen" and st.blocking, "splash: its own screen, blocking")
ok(not listed(st.panels_open, "SplashScreen"), "splash: not listed as a panel")
hide("SplashScreen")
show("MainMenu")
ok(S.state({}).screen == "main_menu", "after the splash: main menu")

-- live L6: the main menu's content read as popup {title "Log In", text "Create account? (c) 2023 Larian ...",
-- buttons = the menu's}: the Larian account corner panel. Not a popup: only the menu's buttons are reported.
local popup = S.popup
S.popup = function() return { title = "Log In", text = "Create account? \194\169 2023 Larian Studios",
    buttons = { "Continue", "New Game", "Load Game", "Credits", "Quit Game", "Larian Account" } } end
st = S.state({})
ok(st.screen == "main_menu" and st.popup == nil, "main menu: the Log In corner panel is no popup")
ok(st.menu and st.menu.buttons and st.menu.buttons[1] == "Continue" and #st.menu.buttons == 6,
    "main menu: its buttons under menu")
hide("MainMenu")
show("GameMenu")
ok(S.state({}).popup ~= nil, "the in-game menu still reports its content")
hide("GameMenu")
S.popup = popup

local tally = string.format("%d passed, %d failed", passes, fails)
print(tally)
if fails > 0 then os.exit(1) end
