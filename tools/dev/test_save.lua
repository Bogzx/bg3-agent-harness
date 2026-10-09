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

local function show(n) widgets[n] = { name = n, visible = true } end
local function hide(n) widgets[n] = nil end

Ext = {
    Utils = { MonotonicTime = function() return clock end, GetGameState = function() return "Running" end },
    Input = { InjectKeyPress = function(k) keys[#keys + 1] = k pendingKey[#pendingKey + 1] = k end },
    IO = { LoadFile = function() return nil end },
    Json = { Parse = function() return {} end },
}

-- the game's menus: a key lands one frame later
local function frame()
    local ks = pendingKey
    pendingKey = {}
    for _, k in ipairs(ks) do
        if k == "RETURN" then
            if widgets.SaveGamePopup then hide("SaveGamePopup") hide("SaveGame")  -- saved: back to the game menu
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

-- named save end to end; sample ui.state on every frame meanwhile
local r = ops["ui.menu"]({ action = "save", name = "S010-pre-kagha" })
local job = jobs[r.job]
local screens, sawSaving = {}, false
for _ = 1, 200 do
    clock = clock + 16
    frame()
    if job.status ~= "running" then break end
    if clock > job.deadline_at then job.status = "timeout" break end
    job.on_tick(job)
    local st = S.state({})
    screens[st.screen] = true
    if st.saving then sawSaving = true end
end
ok(job.status == "done", "save done: " .. tostring(job.status) .. " " .. tostring(job.error))
local res = job.result or {}
ok(res.saved and res.name == "S010-pre-kagha", "result names the save")
ok(type(res.ms) == "table" and res.ms.done and res.ms.accept and res.ms.new, "stage timings reported")
ok(res.ms and res.ms.done < 1500, "menus done in under 1.5 s of game time: " .. tostring(res.ms and res.ms.done))
local only = true
for s in pairs(screens) do if s ~= "game" then only = false print("  screen seen: " .. s) end end
ok(only, "ui.state shows the game throughout our own save")
ok(sawSaving, "ui.state flags saving")
ok(widgets.GameMenu == nil and widgets.SaveGame == nil, "menus closed afterwards")
ok(A.pendingSave == nil, "pending save cleared")

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
