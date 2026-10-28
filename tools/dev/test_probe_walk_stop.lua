-- Offline test for probe_walk_stop.py's INSTALL sampler with stubs (test_probe_walk_stop_lua.sh writes the chunk).
--   lua tools/dev/test_probe_walk_stop.lua <install.lua>
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local clock, tickFn = 0, nil
local claudePos = { -42.0, 26.4, -44.7 }
local tasks = {}
local deepCost = 0 -- ms a deep read takes (the clock advances inside P.compact)
local compacts = 0
Ext = {
    Utils = { MonotonicTime = function() return clock end },
    Events = { Tick = { Subscribe = function(_, fn) tickFn = fn; return 1 end } },
    Osiris = { RegisterListener = function() end },
    Entity = { Get = function() return { ServerCharacter = { OsirisController = { Tasks = tasks } },
        Movement = { Speed = 0, Speed2 = 0 } } end },
}
Osi = { GetPosition = function(u) if u == "C" then return claudePos[1], claudePos[2], claudePos[3] end
    return -38.5, 26.4, -53.5 end }
local P = {
    js = function() return "{}" end,
    compact = function() compacts = compacts + 1; clock = clock + deepCost; return "sig" .. compacts end,
    comp_names = function() return {} end,
}
Mods = { BG3AI = { BG3AI = { ReviveProbe = P, Guard = { osi = function() end }, RPC = { jobs = {}, finish_job = function() end },
    Util = { dist = function(a, b) return math.sqrt((a[1] - b[1]) ^ 2 + (a[2] - b[2]) ^ 2 + (a[3] - b[3]) ^ 2) end },
    Actions = {}, Nav = {} } } }
dofile(arg[1])
local W = P.ws
W.claude, W.wyll, W.near, W.log, W.last, W.t0, W.max_ms, W.on = "C", "W", {}, {}, {}, 0, 1e9, true

local function ticks(n, ms)
    for _ = 1, n do clock = clock + (ms or 16); tickFn() end
end
local function count(kind)
    local c = 0
    for _, r in ipairs(W.log) do if r.k == kind then c = c + 1 end end
    return c
end

ticks(125) -- 2 s at 60 fps, nothing changes
ok(count("tick") == 125, "positions every tick: " .. count("tick"))
ok(compacts >= 4 and compacts <= 5, "deep reads every 0.5 s when nothing changes: " .. compacts)

local c0 = compacts
repeat ticks(1) until compacts > c0 -- just after a time-driven deep read
local before = compacts
tasks[1] = "move" -- the cheap key changes (Osiris task count)
ticks(1); ok(compacts == before, "not again within DEEP_MIN_MS of the last")
ticks(7) -- > 100 ms since the last deep read
ok(compacts == before + 1, "a deep read soon after the cheap key changes: " .. (compacts - before))

before = compacts
for _ = 1, 30 do claudePos = { claudePos[1] + 0.1, claudePos[2], claudePos[3] }; ticks(1) end -- walking: key steady
ok(compacts - before <= 2, "walking does not force a deep read every tick: " .. (compacts - before))

deepCost = 80 -- one deep read takes 80 ms: deep reads stop, positions go on
ticks(40)
local after = compacts
ticks(60)
ok(count("deep_off") == 1 and compacts == after, "a deep read over 50 ms turns deep reads off")
ok(W.log[#W.log].k == "tick", "positions still every tick")

print(string.format("%d/%d passed", pass, pass + fail))
if fail > 0 then os.exit(1) end
