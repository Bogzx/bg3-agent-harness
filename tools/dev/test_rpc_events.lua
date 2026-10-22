-- Offline tests of Shared/RPC.lua's event ring: seq continuity, events.since with since/limit, the cap, and the
-- cost of a push once the ring is full (B4 in research/ovh/pkg-harness-analysis.md: table.remove(events, 1) shifted
-- 4,000 entries per push).
--   lua tools/dev/test_rpc_events.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local function load(events)
    Ext = {
        IsServer = function() return true end,
        Utils = { MonotonicTime = function() return 1000 end, Print = function() end, PrintWarning = function() end },
        Events = { Tick = { Subscribe = function() end } },
        Json = { Parse = function() return nil end, Stringify = function(v) return v end },
    }
    BG3AI = events and { RPC = events } or {}
    dofile(LUA .. "/Shared/RPC.lua")
    return BG3AI.RPC
end

local function since(RPC, seq, limit)
    return RPC.ops["events.since"]({ seq = seq, limit = limit })
end

local function seqs(res)
    local out = {}
    for i, ev in ipairs(res.events) do out[i] = ev.seq end
    return table.concat(out, ",")
end

-- 1. below the cap: every event, in order, from any seq
local RPC = load()
RPC.EVENT_CAP = 5
for i = 1, 3 do RPC.push("e", { i = i }) end
ok(seqs(since(RPC, 0)) == "1,2,3", "all events from 0: " .. seqs(since(RPC, 0)))
ok(seqs(since(RPC, 2)) == "3", "after seq 2")
ok(seqs(since(RPC, 3)) == "" and since(RPC, 3).latest == 3, "nothing new, latest 3")
ok(seqs(since(RPC, 0, 2)) == "1,2", "limit 2 from 0")
ok(since(RPC, 0).events[2].data.i == 2, "the event's data")

-- 2. past the cap: the oldest drop, seq stays continuous, the ring keeps exactly EVENT_CAP
for i = 4, 12 do RPC.push("e", { i = i }) end
ok(seqs(since(RPC, 0)) == "8,9,10,11,12", "from 0 past the cap: the newest 5: " .. seqs(since(RPC, 0)))
ok(seqs(since(RPC, 9)) == "10,11,12", "after 9")
ok(seqs(since(RPC, 0, 2)) == "8,9", "limit from a seq older than the ring: the oldest kept: " .. seqs(since(RPC, 0, 2)))
ok(seqs(since(RPC, 8, 2)) == "9,10", "limit inside the ring")
ok(seqs(since(RPC, 12)) == "" and since(RPC, 12).latest == 12, "caught up")
ok(seqs(since(RPC, 99)) == "", "a seq from a future (a reset ring): nothing, latest says where it is")
local n = 0
for _ in pairs(RPC.events) do n = n + 1 end
ok(n == 5, "exactly EVENT_CAP kept (" .. n .. ")")

-- 3. a push on a full ring doesn't touch the kept events (it used to shift all of them)
RPC = load()
local writes = 0
local store = {}
RPC.events = setmetatable({}, { __index = store, __newindex = function(_, k, v) writes = writes + 1; store[k] = v end })
for i = 1, RPC.EVENT_CAP + 100 do RPC.push("e", { i = i }) end
writes = 0
RPC.push("e", {})
ok(writes == 2, "one push on a full ring of " .. RPC.EVENT_CAP .. " = 2 table writes (" .. writes .. ")")
ok(#since(RPC, RPC.eventSeq - 3).events == 3, "events.since reads only what it returns")

-- 4. this file re-run over the older array ring (events[1] the oldest, seq 41..43) keeps every event and the seq
local old = { events = { { seq = 41, kind = "a" }, { seq = 42, kind = "b" }, { seq = 43, kind = "c" } }, eventSeq = 43 }
RPC = load(old)
RPC.push("d", {})
ok(seqs(since(RPC, 0)) == "41,42,43,44", "re-run over the array ring: " .. seqs(since(RPC, 0)))
ok(since(RPC, 42).events[1].kind == "c", "and its events")

print(string.format("%d passed, %d failed", pass, fail))
if fail > 0 then os.exit(1) end
