-- Offline tests for Server/RollLog.lua: the hidden roll log (B-mod task 2).
-- Ext, Osi, RPC and Visibility are stubbed; Shared/Util.lua is real. The stream layout is the live one
-- (runtime/probes/live1/l3_streams0.txt).
-- Run: tools/dev/test_rolllog.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local HOST = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
local GOB = "b1e2d6a7-0fad-4485-ae2e-2ec78ce4e658"
local ZERO = "00000000-0000-0000-0000-000000000000"

local listeners, ops, pushed, onCreate = {}, {}, {}, {}
local clock = 1000

local function stream(t, player, n)
    return { StreamType = t, PlayerUuid = player or "", CombatGuid = ZERO, RandomCastUuid = "",
        State = { NumRolls = n, Seed = 1792610390, Frame = 1, FailDebt = 0.0, SuccessDebt = 0.0 } }
end
local RS = { BaseSeed = 1792610389, RollModeType = "Random",
    Streams = { stream("Generic", nil, 614), stream("PlayerDamage", HOST, 0) } }

Ext = {
    Osiris = { RegisterListener = function(name, arity, _, fn) listeners[name .. "/" .. arity] = fn end },
    Utils = { MonotonicTime = function() return clock end, GameTime = function() return clock / 1000 end,
        PrintWarning = function(m) print("WARN " .. m) end, Print = function() end },
    Entity = {
        GetAllEntitiesWithComponent = function(name)
            assert(name == "ServerRollStreams")
            return { { ServerRollStreams = RS } }
        end,
        OnCreate = function(name, fn) onCreate[name] = fn end,
    },
}
Osi = {}
BG3AI = {
    RPC = { push = function(kind, data) pushed[#pushed + 1] = { kind = kind, data = data } end,
        register = function(name, fn) ops[name] = fn end, QUIET_OPS = {}, BOOT = "boot-1" },
    Visibility = {
        uuid = function(x) return type(x) == "string" and x:match("%x+%-%x+%-%x+%-%x+%-%x+$") or nil end,
        party = function() return { HOST }, { [HOST] = true } end,
    },
}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/RollLog.lua")
local L = BG3AI.RollLog

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end
local function fire(key, ...)
    clock = clock + 1
    listeners[key](...)
end
local function finished(roller, natural, dc)
    clock = clock + 1
    onCreate.ServerRollFinishedEvent({ ServerRollFinishedEvent = { Events = {
        { Roller = { Uuid = { EntityUuid = roller } }, Subject = { Uuid = { EntityUuid = roller } },
          NaturalRoll = natural, DC = dc, Skill = "Perception", Ability = "Wisdom", RollType = 3,
          Advantage = false, Disadvantage = false, Canceled = false },
    } } })
end

check("listens RollResult/6", listeners["RollResult/6"] ~= nil)
check("listens DialogRollResult/5", listeners["DialogRollResult/5"] ~= nil)
check("subscribed finished", onCreate.ServerRollFinishedEvent ~= nil and L.finishedSubscribed)
check("roll.since quiet", BG3AI.RPC.QUIET_OPS["roll.since"] == true)

-- A load seeds the per-stream counts; a passive Perception roll draws 2 Generic rolls (live1 §4).
fire("LevelGameplayStarted/2", "WLD_Main_A", 0)
RS.Streams[1].State.NumRolls = 616
fire("RollResult/6", "probe1", "Player_" .. HOST, "Player_" .. HOST, 1, 0, 0)
do
    local r = L.ring[1]
    check("one record", #L.ring == 1)
    check("osi fields", r.roller == HOST and r.party == true and r.success == true and r.active == false
        and r.event == "probe1")
    check("stream delta", r.streams and r.streams.Generic == 2 and r.streams["PlayerDamage:" .. HOST] == nil)
end

-- The finished component for the same roll merges into the same record (natural, DC).
finished(HOST, 14, 10)
do
    check("merged: still one record", #L.ring == 1, #L.ring)
    local r = L.ring[1]
    check("natural and dc", r.natural == 14 and r.dc == 10 and r.skill == "Perception")
    check("osi fields kept", r.success == true and r.event == "probe1")
end

-- A second roll by the same roller later is a new record; crits map; finished first, then osi.
clock = clock + 1000
RS.Streams[1].State.NumRolls = 618
finished(HOST, 20, 15)
fire("RollResult/6", "probe2", "Player_" .. HOST, "Player_" .. HOST, 1, 1, 1)
do
    check("second record", #L.ring == 2, #L.ring)
    local r = L.ring[2]
    check("crit nat20", r.crit == "nat20" and r.natural == 20 and r.active == true)
    check("streams on second", r.streams and r.streams.Generic == 2)
end

-- An NPC roll is logged too (luck is per side), never merged into the party's.
fire("RollResult/6", "atk", "S_GOB_" .. GOB, "Player_" .. HOST, 0, 1, 2)
do
    local r = L.ring[3]
    check("npc roll", #L.ring == 3 and r.roller == GOB and r.party == false and r.crit == "nat1")
end

-- A canceled roll is not a roll.
do
    local n = #L.ring
    clock = clock + 1000
    onCreate.ServerRollFinishedEvent({ ServerRollFinishedEvent = { Events = {
        { Roller = { Uuid = { EntityUuid = HOST } }, NaturalRoll = 3, DC = 10, Canceled = true } } } })
    check("canceled skipped", #L.ring == n)
end

-- A reseed restarts NumRolls at 0: the next delta counts from 0, never negative.
clock = clock + 1000
RS.Streams[1].State.NumRolls = 3
fire("DialogRollResult/5", "Player_" .. HOST, 1, "dlg", 0, 0)
do
    local r = L.ring[#L.ring]
    check("dialog record", r.dialog == true and r.success == true)
    check("reseed delta", r.streams and r.streams.Generic == 3, r.streams and r.streams.Generic)
end

-- Ops: roll.since lists records with srcs as a list; roll.snapshot reports streams and sources.
do
    local res = ops["roll.since"]({ seq = 0 })
    check("since all", #res.events == #L.ring and res.latest == L.seq and res.boot == "boot-1")
    local s1 = res.events[1].srcs
    check("srcs list", type(s1) == "table" and s1[1] == "finished" and s1[2] == "osi", s1 and s1[1])
    check("since cursor", #ops["roll.since"]({ seq = L.seq - 1 }).events == 1)
    local snap = ops["roll.snapshot"]({})
    check("snapshot streams", #snap.streams == 2 and snap.streams[1].key == "Generic"
        and snap.streams[2].key == "PlayerDamage:" .. HOST and snap.base_seed == 1792610389)
    check("snapshot fired", snap.fired.osi == 3 and snap.fired.finished == 2 and snap.fired.dialog == 1)
    check("snapshot mode", snap.mode == "Random")
end

-- No component (main menu): records still written, without streams.
do
    local saved = Ext.Entity.GetAllEntitiesWithComponent
    Ext.Entity.GetAllEntitiesWithComponent = function() return {} end
    clock = clock + 1000
    fire("RollResult/6", "x", "Player_" .. HOST, "", 1, 1, 0)
    check("no streams ok", L.ring[#L.ring].event == "x" and L.ring[#L.ring].streams == nil)
    check("snapshot without streams", #ops["roll.snapshot"]({}).streams == 0)
    Ext.Entity.GetAllEntitiesWithComponent = saved
end

-- Cap.
do
    L.CAP = 3
    clock = clock + 1000
    fire("RollResult/6", "y", "S_GOB_" .. GOB, "", 1, 1, 0)
    check("cap", #L.ring == 3 and ops["roll.since"]({ seq = 0 }).first == L.seq - 2)
end

check("no RPC.push", #pushed == 0, #pushed)

print(string.format("rolllog: %d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
