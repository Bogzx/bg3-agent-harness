-- Offline tests for the UI-busy yield (S086 05:17:45 hang, impl-setup S21): Client/UI.lua's try() gate and
-- Shared/RPC.lua's job loop / op answer around it. A lock timeout is a read that takes 50 ms (bg3le's frame-lock
-- wait, "ran unserialized"), or a quick "bg3le: UI busy" error (strict mode).
--   lua tools/dev/test_ui_busy.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local clock = 1000
local ticks = {}
local nextReq
Ext = {
    IsServer = function() return false end,
    UI = { GetRoot = function() return nil end },
    Utils = { MonotonicTime = function() return clock end, Print = function() end, PrintWarning = function() end },
    Events = { Tick = { Subscribe = function(_, f) ticks[#ticks + 1] = f end } },
    Json = { Parse = function() return nextReq end, Stringify = function(v) return v end },
}
BG3AI = {}
dofile(LUA .. "/Shared/RPC.lua")
dofile(LUA .. "/Client/UI.lua")
local RPC, UI = BG3AI.RPC, BG3AI.UI
local function tick() for _, f in ipairs(ticks) do f() end end
local function call(op, args)
    nextReq = { op = op, args = args or {} }
    return BG3AI.rpc("x")
end

-- A Noesis-like element: each read costs `cost` ms of game time (50 = a frame-lock timeout); `refuse` = strict mode.
local mode = { cost = 0, refuse = false }
local reads = 0
local el = {}
function el:GetProperty(name)
    reads = reads + 1
    if mode.refuse then error("bg3le: UI busy (frame lock taken by the UI thread); retry", 0) end
    clock = clock + mode.cost
    if name == "IsVisible" then return false end
    return "v"
end

-- 1. S086 shape: a job whose tick reads 300 properties, then decides from what it read.
local verdicts = {}
local job = RPC.start_job("ui.long_rest", { timeout_ms = 600000, on_tick = function(j)
    local got = 0
    for _ = 1, 300 do
        local okr, v = pcall(UI.prop, el, "Name") -- callers' own pcalls swallow the busy error (UIState, Services)
        if okr and v == "v" then got = got + 1 end
    end
    verdicts[#verdicts + 1] = got
    if got < 300 then RPC.finish_job(j, "failed", nil, "the rest screen didn't open") else RPC.finish_job(j, "done") end
end })
mode.cost, reads = 50, 0
local t0 = clock
tick()
ok(reads == 1, "a busy tick waits out ONE lock timeout, not 300 (reads " .. reads .. ")")
ok(clock - t0 < 100, "the tick ends at once (" .. (clock - t0) .. " ms, S086: 10 221 / 35 774 ms)")
ok(job.status == "running", "the job keeps running: no verdict from refused reads (" .. tostring(job.error) .. ")")
ok(job.busy_ticks == 1, "busy tick counted")
reads = 0
tick()
ok(reads == 0 and #verdicts == 1, "within the cooldown the job does not touch the UI")
-- the UI is free again: the job ticks and decides from real reads
mode.cost = 0
clock = clock + UI.BUSY_COOLDOWN_MS
tick()
ok(job.status == "done", "after the cooldown it reads the real UI and finishes (" .. tostring(job.status) .. ")")

-- 2. An op that meets the busy UI answers an error, never a guess (S086: a refused IsVisible read as visible).
RPC.register("t.visible", function() return { visible = UI.isVisible(el) } end)
mode.cost = 50
local r = call("t.visible")
ok(r.ok == false and tostring(r.error):find("UI busy", 1, true), "op: busy -> error, not visible=true")
clock = clock + UI.BUSY_COOLDOWN_MS
mode.cost = 0
r = call("t.visible")
ok(r.ok == true and r.result.visible == false, "op: free UI -> the real read")

-- 3. bg3le strict mode: a quick "UI busy" refusal is handled the same way.
mode.refuse = true
r = call("t.visible")
ok(r.ok == false and tostring(r.error):find("UI busy", 1, true), "strict refusal -> busy error")
ok(UI.busy(), "and the cooldown starts")
reads = 0
local okp = pcall(UI.prop, el, "Name")
ok(not okp and reads == 0, "during the cooldown no read reaches the UI")
mode.refuse = false
clock = clock + UI.BUSY_COOLDOWN_MS

-- 4. An op that starts a job on refused reads: the job is dropped with the error.
RPC.register("t.start", function()
    local j = RPC.start_job("t", { timeout_ms = 10000, on_tick = function() end })
    UI.prop(el, "Name")
    return { job = j.id }
end)
local before = RPC.nextJob
mode.cost = 50
r = call("t.start")
ok(r.ok == false, "job-starting op: busy -> error")
ok(RPC.jobs[before] and RPC.jobs[before].status == "interrupted", "its job is dropped")
clock = clock + UI.BUSY_COOLDOWN_MS
mode.cost = 0

-- 5. Normal reads are untouched: a missing property still reads nil, no busy state.
local bad = setmetatable({}, { __index = function() error("no such property") end })
local hits = UI.busyHits
ok(UI.get(bad, "Nope") == nil and UI.busyHits == hits and not UI.busy(), "a plain failed read is nil, not busy")
-- UI.execute (a command) is gated too: nothing pressed while the UI is busy
local pressed = 0
local cmd = { CanExecute = function() return true end, Execute = function() pressed = pressed + 1 end }
mode.cost = 50
pcall(UI.prop, el, "x")
local oke = pcall(UI.execute, cmd)
ok(not oke and pressed == 0, "no command pressed while busy")
clock = clock + UI.BUSY_COOLDOWN_MS
ok(UI.execute(cmd) == true and pressed == 1, "pressed once the UI is free")

print(string.format("%d/%d passed", pass, pass + fail))
if fail > 0 then os.exit(1) end
