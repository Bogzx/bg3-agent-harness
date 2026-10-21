-- Offline tests for Server/RollLog.lua: the hidden roll log (B-mod task 2).
-- Ext, Osi, RPC and Visibility are stubbed; Shared/Util.lua is real. The stream layout is the live one
-- (runtime/probes/live1/l3_streams0.txt).
-- Run: tools/dev/test_rolllog.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local HOST = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
local GOB = "b1e2d6a7-0fad-4485-ae2e-2ec78ce4e658"
local ZERO = "00000000-0000-0000-0000-000000000000"

local listeners, ops, pushed, onCreate, postUpdate = {}, {}, {}, {}, {}
local oneFrame = {} -- entities carrying ServerRollFinishedEvent this frame
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
            if name == "ServerRollFinishedEvent" then return oneFrame end
            assert(name == "ServerRollStreams")
            return { { ServerRollStreams = RS } }
        end,
        OnCreate = function(name, fn) onCreate[name] = fn end,
        OnSystemPostUpdate = function(name, fn) postUpdate[name] = fn end,
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
check("subscribed requested", onCreate.RequestedRoll ~= nil and L.hooks.requested_create == true)
check("post-update hook on ServerRoll", postUpdate.ServerRoll ~= nil and L.hooks.post_update == true
    and L.hooks.tick == nil)
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

------------------------------------------------------------------------
-- Round 5: natural/total/dc from the in-frame sources; per-record stream counts (research/ovh/live3.md §4).
------------------------------------------------------------------------
L.CAP = 4000
local function fresh()
    L.ring, L.batch, L.pending = {}, nil, {}
    clock = clock + 10000
end
local function ent(uuid) return { Uuid = { EntityUuid = uuid } } end
local function osi(i, ok, crit)
    clock = clock + 5
    listeners["RollResult/6"]("probe" .. i, "Player_" .. HOST, "Player_" .. HOST, ok and 1 or 0, 0, crit or 0)
end

-- A batch of 24 passive checks in one frame: the 68 draws (48 + 20 ambient, live3 §4) are shared out, every record
-- gets its own streams and stream, and the counts add up to the batch's.
fresh()
RS.Streams[1].State.NumRolls = 100
L.lastRolls = L.counts()
RS.Streams[1].State.NumRolls = 168
for i = 1, 24 do osi(i, i % 2 == 0) end
do
    check("batch: 24 records", #L.ring == 24, #L.ring)
    local sum, all = 0, true
    for _, r in ipairs(L.ring) do
        all = all and r.streams ~= nil and r.stream == "Generic" and r.streams_shared == 24
        sum = sum + (r.streams and r.streams.Generic or 0)
    end
    check("batch: each record has streams", all)
    check("batch: draws add up", sum == 68, sum)
    check("batch: remainder to the earliest", L.ring[1].streams.Generic == 3 and L.ring[20].streams.Generic == 3
        and L.ring[21].streams.Generic == 2 and L.ring[24].streams.Generic == 2)
    -- A roll after the window with no new draws starts no batch and changes nothing before it.
    clock = clock + 1000
    osi(25, true)
    check("after the batch: no streams", L.ring[25].streams == nil and L.ring[24].streams.Generic == 2)
end

-- finished, read in the frame by the ServerRoll post-update hook: natural and DC; reading the same one-frame again
-- (two updates in one frame) adds nothing.
fresh()
do
    local before = L.fired.finished
    oneFrame = { { ServerRollFinishedEvent = { Events = {
        { Roller = ent(HOST), Subject = ent(HOST), RollUuid = "roll-1", NaturalRoll = 7, DC = 12,
          Skill = "Perception", Ability = "Wisdom", RollType = 3, Advantage = false, Disadvantage = false,
          Canceled = false } } } } }
    postUpdate.ServerRoll()
    postUpdate.ServerRoll()
    oneFrame = {}
    check("in-frame finished: one record", #L.ring == 1 and L.fired.finished == before + 1, #L.ring)
    osi(1, false)
    local r = L.ring[1]
    check("in-frame finished + osi merged", #L.ring == 1 and r.natural == 7 and r.dc == 12 and r.roll_uuid == "roll-1"
        and r.success == false and r.srcs.finished and r.srcs.osi)
end

-- OnCreate after the frame (bg3le queues it to the next tick): the one-frame is gone; counted, nothing recorded.
do
    local n, e = #L.ring, L.seen.finished_empty
    onCreate.ServerRollFinishedEvent({})
    check("late OnCreate counted as empty", #L.ring == n and L.seen.finished_empty == e + 1)
end

-- requested: the roll entity is tracked from its OnCreate and read once Finished (natural, total, DC); a canceled
-- roll and a vanished entity are dropped.
fresh()
do
    local c = { Roller = ent(HOST), Subject = ent(HOST), RollUuid = "roll-2", DC = 10, NaturalRoll = 0,
        Finished = false, Canceled = false, Skill = "Perception", Ability = "Wisdom",
        Roll = { Advantage = false, Disadvantage = false }, Result = { Total = 0, NaturalRoll = 0 } }
    local live = { RequestedRoll = c }
    onCreate.RequestedRoll(live)
    onCreate.RequestedRoll({ RequestedRoll = { Roller = ent(HOST), Finished = false, Canceled = true } })
    onCreate.RequestedRoll({})
    postUpdate.ServerRoll()
    check("requested: unfinished kept, canceled and gone dropped", #L.ring == 0 and #L.pending == 1, #L.pending)
    c.Finished, c.Result = true, { Total = 17, NaturalRoll = 14 }
    osi(1, true)
    local r = L.ring[1]
    check("requested + osi: one record", #L.ring == 1 and #L.pending == 0, #L.ring)
    check("requested fields", r.natural == 14 and r.result == 17 and r.dc == 10 and r.skill == "Perception"
        and r.success == true and r.srcs.requested and r.srcs.osi and r.party == true)
    -- The finished one-frame of the same roll merges by roll uuid; its fields don't overwrite.
    oneFrame = { { ServerRollFinishedEvent = { Events = {
        { Roller = ent(HOST), RollUuid = "roll-2", NaturalRoll = 14, DC = 10, Canceled = false } } } } }
    postUpdate.ServerRoll()
    oneFrame = {}
    check("finished merges by roll uuid", #L.ring == 1 and L.ring[1].srcs.finished)
    local view = ops["roll.since"]({ seq = r.seq - 1 }).events[1]
    check("srcs listed", table.concat(view.srcs, ",") == "finished,osi,requested", table.concat(view.srcs, ","))
end

-- A batch of requested rolls by one roller pairs with the Osiris results in order (oldest first).
fresh()
do
    local nats, oks = { 5, 12, 19 }, { false, true, true }
    for i, n in ipairs(nats) do
        onCreate.RequestedRoll({ RequestedRoll = { Roller = ent(HOST), RollUuid = "b" .. i, DC = 10,
            Finished = true, Canceled = false, Result = { Total = n + 2, NaturalRoll = n } } })
    end
    for i, ok in ipairs(oks) do osi(i, ok) end
    check("batch pairing: 3 records", #L.ring == 3, #L.ring)
    local good = true
    for i, r in ipairs(L.ring) do
        good = good and r.natural == nats[i] and r.success == oks[i] and r.event == "probe" .. i
    end
    check("batch pairing in order", good)
end

-- A face outside 1..20 or a zero DC is not reported as one.
fresh()
do
    onCreate.RequestedRoll({ RequestedRoll = { Roller = ent(GOB), RollUuid = "z", DC = 0, Finished = true,
        Canceled = false, Result = { Total = 31, NaturalRoll = 0 }, NaturalRoll = 25 } })
    postUpdate.ServerRoll()
    local r = L.ring[1]
    check("bad face and dc dropped", r and r.natural == nil and r.dc == nil and r.result == 31 and r.party == false)
end

-- Snapshot: hooks, raw hook hits and pending entities.
do
    local snap = ops["roll.snapshot"]({})
    check("snapshot hooks", snap.hooks.post_update == true and snap.hooks.requested_create == true
        and snap.seen.requested_create >= 7 and snap.seen.finished_empty >= 1 and snap.pending == 0
        and snap.fired.requested >= 4)
end

check("no RPC.push", #pushed == 0, #pushed)

-- No system hook on this build: the Tick fallback reads the in-frame sources instead.
do
    local ticks = {}
    BG3AI.RollLog = nil
    Ext.Entity.OnSystemPostUpdate = function() error("not registered") end
    Ext.Events = { Tick = { Subscribe = function(_, fn) ticks[#ticks + 1] = fn end } }
    local warn = Ext.Utils.PrintWarning
    Ext.Utils.PrintWarning = function() end
    dofile(LUA .. "/Server/RollLog.lua")
    Ext.Utils.PrintWarning = warn
    local L2 = BG3AI.RollLog
    check("tick fallback", L2.hooks.post_update == false and L2.hooks.tick == true and #ticks == 1
        and L2.finishedSubscribed == true)
end

print(string.format("rolllog: %d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
