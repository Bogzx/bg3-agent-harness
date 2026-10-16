-- Offline tests for Shared/Mem.lua (Lua heap log, cap guard, breadcrumbs) and the RPC breadcrumb.
-- Run: tools/dev/test_mem.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local clock = 1000
local printed = {}
local function out(kind) return function(s) printed[#printed + 1] = kind .. " " .. s end end
local ticks = {}
Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return clock end, Print = out("I"), PrintWarning = out("W"),
        PrintError = out("E") },
    Events = { Tick = { Subscribe = function(_, fn) ticks[#ticks + 1] = fn end } },
    Json = {
        Parse = function(s)
            local op = s:match('"op":"([^"]+)"')
            local x, y, z = s:match('"pos":%[([%d.-]+),([%d.-]+),([%d.-]+)%]')
            return { op = op, args = x and { pos = { tonumber(x), tonumber(y), tonumber(z) } } or {} }
        end,
        Stringify = function(v) return v.ok and "ok" or ("err " .. tostring(v.error)) end,
    },
}

BG3AI = {}
dofile(LUA .. "/Shared/RPC.lua")
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/Mem.lua")
local M, RPC = BG3AI.Mem, BG3AI.RPC

local failures, count = 0, 0
local heap = 40
M.heap_mb = function() return heap end
local function reset()
    printed = {}
    heap = 40
    clock = clock + 100000
    M.state = { t = 0, logged = clock, mb = 0, peak = 0, high = false, trips = 0 }
    M.crumbSeen, M.crumbN, M.lastCrumb = {}, 0, { t = -1e9 }
end
local function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function has(pat)
    for _, l in ipairs(printed) do if l:find(pat) then return l end end
    return nil
end

test("a heap line every minute, not every check", function()
    M.check(clock, 40)
    assert(not has("mem: server lua"), "no line before a minute")
    clock = clock + 60000
    M.check(clock, 41.25)
    assert(has("^I %[BG3AI%] mem: server lua 41.2 MB %(peak 41.2%)") or has("mem: server lua 41.3 MB"),
        table.concat(printed, " | "))
end)

test("a jump of SPIKE_MB in one check is logged with the last breadcrumb", function()
    M.check(clock, 40)
    M.crumb("nav.landing (1,2,3)")
    clock = clock + 1000
    M.check(clock, 140)
    local l = has("^W .*heap %+100.0 MB")
    assert(l and l:find("nav.landing %(1,2,3%)"), table.concat(printed, " | "))
end)

test("over the cap after a full collect: paused, logged loudly once; resumes below RESUME_MB", function()
    heap = 600
    M.check(clock, 600)
    assert(M.paused(), "paused")
    assert(has("^E %[BG3AI%] MEMGUARD: server Lua heap 600.0 MB > cap 512"), table.concat(printed, " | "))
    printed = {}
    M.check(clock + 1000, 600)
    assert(not has("MEMGUARD"), "logged once")
    heap = 300
    M.check(clock + 2000, 300)
    assert(not M.paused() and has("resumed"), "resumed")
    assert(M.state.trips == 1)
end)

test("a heap the collect brings back under the cap does not trip", function()
    heap = 100 -- what heap_mb answers after collectgarbage("collect")
    M.check(clock, 600)
    assert(not M.paused(), "not paused")
end)

test("crumbs: same label deduped for CRUMB_REPEAT_MS, interleaved labels too", function()
    assert(M.crumb("a") == true)
    assert(M.crumb("b") == true)
    assert(M.crumb("a") == false, "a again within 10 s")
    clock = clock + M.CRUMB_REPEAT_MS + 1
    assert(M.crumb("a") == true, "a after 10 s")
    assert(M.lastCrumb.label == "a")
end)

test("rpc: a non-poll op leaves a breadcrumb with its pos; polls do not", function()
    RPC.register("nav.landing", function() return { safe = true } end)
    RPC.register("job.poll", function() return {} end)
    BG3AI.rpc('{"op":"job.poll","args":{}}')
    assert(not has("server> job.poll"), "poll quiet")
    BG3AI.rpc('{"op":"nav.landing","args":{"pos":[-408.5,1,-1437]}}')
    assert(has("^I %[BG3AI%] server> nav.landing %(%-408.5,1,%-1437%)"), table.concat(printed, " | "))
end)

test("sys.mem answers the heap, peak and cap", function()
    M.check(clock, 40)
    local r = RPC.ops["sys.mem"]({})
    assert(r.context == "server" and r.lua_mb == 40 and r.cap_mb == 512 and r.high == nil, "sys.mem")
end)

test("the Tick checks at most every CHECK_MS", function()
    assert(#ticks >= 1)
    local n = 0
    local real = M.check
    M.check = function(...) n = n + 1; return real(...) end
    M.state.t = 0
    ticks[#ticks]()
    ticks[#ticks]()
    clock = clock + M.CHECK_MS
    ticks[#ticks]()
    M.check = real
    assert(n == 2, "checks: " .. n)
end)

test("client: Noesis counters on the minute line, a line when parking starts and when it drains", function()
    local parked = 0
    Ext.IsServer = function() return false end
    Ext._Internal = { UiSerialize = function() return true, 0, 0, 68, 10, parked, 5000, 0 end }
    dofile(LUA .. "/Shared/Mem.lua")
    M.heap_mb = function() return heap end
    reset()
    M.check(clock, 40)                       -- first read: no transition line
    parked = 2700
    M.check(clock + 1000, 40)
    assert(printed[#printed]:find("parking started: noesis gen 68 parked 2700 leaked 10", 1, true), printed[#printed])
    local n = #printed
    M.check(clock + 2000, 40)                -- still parked: nothing new until the minute
    assert(#printed == n, "no repeat line")
    M.check(clock + M.LOG_EVERY_MS, 40)
    assert(printed[#printed]:find("client lua 40.0 MB (peak 40.0) | noesis gen 68 parked 2700", 1, true), printed[#printed])
    parked = 0
    M.check(clock + M.LOG_EVERY_MS + 1000, 40)
    assert(printed[#printed]:find("parked drained: noesis gen 68 parked 0", 1, true), printed[#printed])
    local m = RPC.ops["sys.mem"]({})
    assert(m.context == "client" and m.noesis and m.noesis.parked == 0 and m.noesis.leaked == 10, "sys.mem noesis")
    Ext._Internal = nil
    assert(M.noesis() == nil, "no bg3le patch: nil")
    Ext.IsServer = function() return true end
    dofile(LUA .. "/Shared/Mem.lua")
    M.heap_mb = function() return heap end
    assert(M.noesis() == nil, "server: nil")
end)

test("client: frame-lock timeouts and strict refusals on the minute line (audit UI-5)", function()
    Ext.IsServer = function() return false end
    Ext._Internal = { UiSerialize = function() return true, 7, 3, 68, 10, 0, 5000, 0 end,
        UiStrict = function() return true, 4 end }
    dofile(LUA .. "/Shared/Mem.lua")
    M.heap_mb = function() return heap end
    reset()
    M.check(clock, 40)
    M.check(clock + M.LOG_EVERY_MS, 40)
    assert(printed[#printed]:find("noesis gen 68 parked 0 leaked 10 released_late 5000 lock_timeouts lua 7 ui 3 strict true refused 4",
        1, true), printed[#printed])
    local m = RPC.ops["sys.mem"]({})
    assert(m.noesis.lua_timeouts == 7 and m.noesis.ui_timeouts == 3 and m.noesis.refused == 4, "sys.mem timeouts")
    Ext._Internal = { UiSerialize = function() return true, 0, 0, 68, 10, 0, 5000, 0 end }  -- a build without 0010
    assert(M.noesis().strict == nil, "no UiStrict: no strict field")
    Ext._Internal = nil
    Ext.IsServer = function() return true end
    dofile(LUA .. "/Shared/Mem.lua")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
