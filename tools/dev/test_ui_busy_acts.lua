-- Offline tests for UI calls that act (bg3le 0011, research/fix/harness.md Task 4): after a frame-lock timeout bg3le
-- refuses every Lua UI call at once ("bg3le: UI busy ...; retry", the call never ran) until a frame takes the lock.
-- Reads go through Client/UI.lua try(); a command (UI.execute's CanExecute/Execute), a SetProperty or Pick's GetRoot
-- goes through UI.pcall: a refusal starts the busy cooldown and raises like a read (the job's tick is dropped, the op
-- answers busy), never a plain "CanExecute failed" / "couldn't select" / unknown screen size.
--   lua tools/dev/test_ui_busy_acts.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local clock = 1000
local ticks = {}
local nextReq
local refuse = false -- bg3le 0011: busy since a lock timeout, every call refused at once
local REFUSAL = "bg3le: UI busy (frame lock not taken since a timeout); retry"
local roots = 0
Ext = {
    IsServer = function() return false end,
    UI = { GetRoot = function()
        roots = roots + 1
        if refuse then error(REFUSAL, 0) end
        return { ActualWidth = 1920, ActualHeight = 1080 }
    end },
    Utils = { MonotonicTime = function() return clock end, Print = function() end, PrintWarning = function() end },
    Events = { Tick = { Subscribe = function(_, f) ticks[#ticks + 1] = f end } },
    Json = { Parse = function() return nextReq end, Stringify = function(v) return v end },
}
BG3AI = { Util = {} }
dofile(LUA .. "/Shared/RPC.lua")
dofile(LUA .. "/Client/UI.lua")
dofile(LUA .. "/Client/Pick.lua")
local RPC, UI, P = BG3AI.RPC, BG3AI.UI, BG3AI.Pick
local function tick() for _, f in ipairs(ticks) do f() end end
local function call(op, args)
    nextReq = { op = op, args = args or {} }
    return BG3AI.rpc("x")
end
local function free() refuse = false; clock = clock + UI.BUSY_COOLDOWN_MS end

local can, ran = 0, 0
local cmd = {}
function cmd.CanExecute() can = can + 1; if refuse then error(REFUSAL, 0) end return true end
function cmd.Execute() ran = ran + 1; if refuse then error(REFUSAL, 0) end end
local vm = { Cmd = cmd }
function vm:GetProperty(name) return self[name] end

-- 1. A job that presses a command: a refused CanExecute drops the tick (no "failed" verdict) and starts the cooldown.
local job = RPC.start_job("t.press", { timeout_ms = 60000, on_tick = function(j)
    local okp, err = UI.command(vm, "Cmd")
    if okp then RPC.finish_job(j, "done") else RPC.finish_job(j, "failed", nil, err) end
end })
refuse = true
tick()
ok(job.status == "running" and job.busy_ticks == 1, "refused CanExecute: the job keeps running, busy tick (" ..
    tostring(job.status) .. " " .. tostring(job.error) .. ")")
ok(UI.busy() and ran == 0, "and the cooldown starts, nothing executed")
can = 0
tick()
ok(can == 0 and job.status == "running", "within the cooldown the command is not asked again")
free()
tick()
ok(job.status == "done" and ran == 1,
    "after the cooldown it presses once and finishes (" .. tostring(job.status) .. ")")

-- 2. Execute refused after CanExecute ran: raises busy, not "Execute failed"; no command frame recorded.
can, ran = 0, 0
local saved = cmd.CanExecute
cmd.CanExecute = function() can = can + 1; refuse = true; return true end -- the lock times out right after it
UI.lastCommandMs = nil
local okx, err = pcall(UI.execute, cmd)
cmd.CanExecute = saved
ok(not okx and tostring(err):find(UI.BUSY_ERR, 1, true) and ran == 1 and UI.lastCommandMs == nil,
    "refused Execute raises busy, no command frame (" .. tostring(err) .. ")")
ok(UI.busy(), "and starts the cooldown")
free()
ok(UI.execute(cmd) == true and ran == 2, "free UI: executes")

-- 3. An op that presses: a refused command answers busy, never a plain failure.
RPC.register("t.cmd", function() local okp, e = UI.command(vm, "Cmd"); return { ok = okp, err = e } end)
refuse = true
local r = call("t.cmd")
ok(r.ok == false and tostring(r.error):find("UI busy", 1, true), "op: refused command -> busy error (" ..
    tostring(r.error) .. ")")
free()

-- 4. UI.pcall on a SetProperty (Services' quantity box, ServicesItems' pickpocket/donate select, Progression's set).
local sets = 0
local lb = {}
function lb.SetProperty(_, name)
    sets = sets + 1
    if refuse then error(REFUSAL, 0) end
    if name ~= "SelectedIndex" then error("no property " .. name, 0) end
end
refuse = true
local hits = UI.busyHits
local okp = pcall(UI.pcall, lb.SetProperty, lb, "SelectedIndex", 3)
ok(not okp and UI.busyHits == hits + 1 and UI.busy(), "refused SetProperty raises busy and starts the cooldown")
sets = 0
okp = pcall(UI.pcall, lb.SetProperty, lb, "SelectedIndex", 3)
ok(not okp and sets == 0, "during the cooldown no SetProperty reaches the UI")
free()
local oks, v = UI.pcall(lb.SetProperty, lb, "Nope", 1)
ok(oks == false and v == "no property Nope" and not UI.busy(), "a plain failed set is pcall's false, err, not busy")
oks = UI.pcall(lb.SetProperty, lb, "SelectedIndex", 3)
ok(oks == true, "free UI: the set runs")
-- a slow set did run (lock timeout, then unserialized): not raised, so its caller does not repeat it
local slow = function() clock = clock + 50 end
oks = UI.pcall(slow)
ok(oks == true, "a slow set that ran is not raised")
free()

-- 5. Pick.screen_size: a refused GetRoot raises busy (was nil: screen_pos without x/y, intrinsics' 16:9 guess).
refuse = true
local okz, zerr = pcall(P.screen_size)
ok(not okz and tostring(zerr):find(UI.BUSY_ERR, 1, true), "refused GetRoot raises busy (" .. tostring(zerr) .. ")")
free()
local w, h = P.screen_size()
ok(w == 1920 and h == 1080, "free UI: the root's size")

-- 6. ui.pickpocket's select step refused: the same row is selected on a later tick, none skipped.
BG3AI.Services = { inner = {
    try = function(f) return f() end, visibleWidget = function() return { DataContext = {} } end,
    memoized = function() return { lb = lb, rows = { { i = 0, view = { name = "Ring" } }, { i = 1, view = {
        name = "Gem" } } } } end,
    waitJob = function(kind, timeout_ms, fn)
        local j = RPC.start_job(kind, { timeout_ms = timeout_ms, on_tick = function(jb)
            local st, res, e = fn(jb)
            if st then RPC.finish_job(jb, st, res, e) end
        end })
        return { job = j.id }
    end } }
dofile(LUA .. "/Client/ServicesItems.lua")
local picked = {}
function lb.SetProperty(_, _, i)
    if refuse then error(REFUSAL, 0) end
    picked[#picked + 1] = i
end
local pj = RPC.jobs[RPC.ops["ui.pickpocket"]({}).job]
tick()                                     -- selects the Ring
clock = clock + 100
tick()                                     -- reads its roll
refuse = true
tick()                                     -- the Gem's select is refused
free()
tick()                                     -- the Gem again
clock = clock + 100
tick()
tick()
local names = {}
for _, it in ipairs(pj.result and pj.result.items or {}) do names[#names + 1] = it.name end
ok(pj.status == "done" and table.concat(picked, ",") == "0,1" and table.concat(names, ",") == "Ring,Gem",
    "pickpocket: refused select retried on the same row (" .. tostring(pj.status) .. " picked " ..
    table.concat(picked, ",") .. " items " .. table.concat(names, ",") .. ")")

-- 7. The acting calls in these files go through UI.pcall, not a bare pcall.
local function src(name)
    local f = assert(io.open(LUA .. "/Client/" .. name, "r"))
    local s = f:read("a")
    f:close()
    return s
end
for _, name in ipairs({ "UI.lua", "Services.lua", "ServicesItems.lua", "Progression.lua", "Spellbook.lua",
                        "Pick.lua" }) do
    local s, bare = src(name), {}
    for line in s:gmatch("[^\n]+") do
        local code = line:gsub("%-%-.*", "")
        if code:find("pcall%(") and not code:find("UI%.pcall%(") and (code:find("SetProperty") or
            code:find("CanExecute") or code:find("%.Execute") or code:find("GetRoot")) then
            bare[#bare + 1] = line
        end
    end
    ok(#bare == 0, name .. ": no bare pcall around an acting UI call" .. (#bare > 0 and (": " .. bare[1]) or ""))
end
local sb = src("Spellbook.lua")
ok(sb:find("UI.pcall(cmd.CanExecute", 1, true) ~= nil, "Spellbook probe: CanExecute through UI.pcall")
ok(src("Pick.lua"):find("UI.pcall(function()", 1, true) ~= nil, "Pick.screen_size: GetRoot through UI.pcall")

print(string.format("%d/%d passed", pass, pass + fail))
if fail > 0 then os.exit(1) end
