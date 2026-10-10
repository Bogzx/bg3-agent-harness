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

local function show(n) widgets[n] = { name = n, visible = true } end
local function hide(n) widgets[n] = nil end

Ext = {
    Utils = { MonotonicTime = function() return clock end, GetGameState = function() return gameState end },
    Events = { GameStateChanged = { Subscribe = function(_, h) stateHandlers[#stateHandlers + 1] = h end } },
    Input = { InjectKeyPress = function(k) keys[#keys + 1] = k pendingKey[#pendingKey + 1] = k end },
    IO = { LoadFile = function() return nil end },
    Json = { Parse = function() return {} end },
}

-- the game's menus: a key lands one frame later
local function frame()
    if saveAt and clock >= saveAt and gameState == "Running" then setState("Save") end
    if saveAt and clock >= saveAt + 1000 and gameState == "Save" then setState("Running") saveAt = nil end
    local ks = pendingKey
    pendingKey = {}
    for _, k in ipairs(ks) do
        if k == "RETURN" then
            if widgets.SaveGamePopup then
                hide("SaveGamePopup") hide("SaveGame")  -- saved: back to the game menu
                if saveStarts then saveAt = clock + 500 end
            elseif widgets.SaveGame then show("SaveGamePopup") end
        elseif k == "ESCAPE" then
            if widgets.SaveGame then hide("SaveGame")
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
            for n, w in pairs(widgets) do m[n] = { el = w } end
            return m
        end,
        isVisible = function(el) return el.visible ~= false end,
        byName = function(w, n) if n == "SaveName" then return { SetProperty = function() end } end end,
        text = function() return "typed" end,
        clean = function(v) return v end,
        prop = function() return nil end,
        props = function() return {} end,
        findButton = function() return nil end,
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
local function runSave(name)
    local r = ops["ui.menu"]({ action = "save", name = name })
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

local tally = string.format("%d passed, %d failed", passes, fails)
print(tally)
if fails > 0 then os.exit(1) end
