-- Offline tests for Server/Guard.lua + Server/Actions.lua (worker act): verified
-- approach points for entity targets, the position-only walker, teleport
-- detection, interact/pickup approach, cast range/arity, combat movement budget.
-- Ext/Osi/Nav are stubbed; Shared/Util.lua and Shared/RPC.lua are the real ones.
-- Run: tools/dev/test_act_lua.sh  (Lua 5.3 + 5.4 in Docker)

LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

clock = 1000
tickers = {}
world = {}   -- uuid -> {pos={x,y,z}, comps={}}
calls = {}   -- list of {name, args...}
osiv = {}    -- "Fn:a:b" -> value (queries)
nav = {}     -- nav.fn(char, pos) -> path result table
stats = {}   -- name -> stats table

function entity(u)
    local w = world[u]
    if not w then error("no entity " .. tostring(u)) end
    local e = {}
    for k, v in pairs(w.comps or {}) do e[k] = v end
    function e:Replicate() end
    return e
end

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end,
        Version = function() return 0 end },
    Events = { Tick = { Subscribe = function(_, fn) tickers[#tickers + 1] = fn end } },
    Entity = { Get = entity },
    Level = { GetHeightsAt = function() return { 0.0 } end },
    Stats = { Get = function(n) return stats[n] end },
    StaticData = { Get = function(uuid) return { Name = uuid } end },
    Loca = { GetTranslatedString = function() return nil end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    Osiris = { RegisterListener = function() end }, Require = function(p) return dofile(LUA .. "/" .. p) end, -- + parts
}

function key(name, ...)
    local parts = { name }
    for _, a in ipairs({ ... }) do parts[#parts + 1] = tostring(a) end
    return table.concat(parts, ":")
end

function record(name)
    return function(...)
        local c = { name, n = select("#", ...), ... }
        calls[#calls + 1] = c
    end
end

P1 = "aaaaaaaa-0000-0000-0000-000000000001"
NPC = "bbbbbbbb-0000-0000-0000-000000000002"
DOOR = "cccccccc-0000-0000-0000-000000000003"
GEM = "dddddddd-0000-0000-0000-000000000004"
GOB = "eeeeeeee-0000-0000-0000-000000000005"
P2 = "ffffffff-0000-0000-0000-000000000006"
KEY = "abababab-0000-0000-0000-000000000007"
LADDER = "acacacac-0000-0000-0000-000000000008"
party_rows = { P1 }

usp_arity = 6 -- UseSpellAtPosition inputs the "game" accepts
Osi = setmetatable({
    DB_PartyMembers = { Get = function()
        local rows = {}
        for _, u in ipairs(party_rows) do rows[#rows + 1] = { "S_Player_" .. u } end
        return rows
    end },
    Unlock = record("Unlock"),
    GetPosition = function(u)
        local w = world[u]
        if w then return w.pos[1], w.pos[2], w.pos[3] end
        error("no position")
    end,
    CharacterMoveTo = record("CharacterMoveTo"),
    CharacterMoveToPosition = record("CharacterMoveToPosition"),
    PurgeOsirisQueue = record("PurgeOsirisQueue"),
    Use = record("Use"),
    Pickup = record("Pickup"),
    UseSpell = record("UseSpell"),
    MakePlayerActive = record("MakePlayerActive"),
    EndTurn = record("EndTurn"),
    ToInventory = record("ToInventory"),
    ForceTurnBasedMode = record("ForceTurnBasedMode"),
    DetachFromPartyGroup = record("DetachFromPartyGroup"),
    AttachToPartyGroup = record("AttachToPartyGroup"),
    UseSpellAtPosition = function(...)
        calls[#calls + 1] = { "UseSpellAtPosition?", n = select("#", ...) }
        if select("#", ...) ~= usp_arity then
            error("Incorrect number of IN arguments for 'UseSpellAtPosition'; expected 6, got 5", 0)
        end
        record("UseSpellAtPosition")(...)
    end,
}, { __index = function(_, name)
    return function(...) return osiv[key(name, ...)] end
end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
BG3AI.Nav = { path = function(char, pos) return nav.fn(char, pos) end,
    known = function(_, p) return not (nav.unseen and nav.unseen(p)) end } -- N.known: all in view unless nav.unseen
dofile(LUA .. "/Server/Guard.lua")
dofile(LUA .. "/Server/Actions.lua")
G, A, RPC = BG3AI.Guard, BG3AI.Actions, BG3AI.RPC

function tick(n)
    for _ = 1, n or 1 do
        clock = clock + 60
        for _, f in ipairs(tickers) do f() end
    end
end
function job(r) return RPC.jobs[r.job] end
function called(name)
    local out = {}
    for _, c in ipairs(calls) do if c[1] == name then out[#out + 1] = c end end
    return out
end

function reset()
    calls, osiv, stats = {}, {}, {}
    Ext.System = nil
    party_rows = { P1 }
    if A then A.TASK_POS2 = false end
    if G then G.turn_over = {} end -- TurnEnded's marker (turn over until TurnStarted) is per test
    if G then G.turn_start, G.foe_start = {}, {} end -- TurnStarted records (Guard.turn_moved_on) too
    if A then A.hit_ms = {} end -- AttackedBy on a party member (cast re-order window) is per test
    -- the stub world moves in synthetic multi-metre steps per 60 ms tick: NAV-8's small-step rule off (own tests)
    if A then A.TELEPORT_SMALL_M = 1e9 end
    if A then A.trip_t = nil end -- a fast travel's arrival (act.trip_arrived) is per test
    if A then A.idle_watch, A.idle_last, A.cast_ms = {}, {}, {} end -- the after-a-job teleport watch is per test
    if A then A.unlocked_ms = {} end -- Unlocked/Opened seen on an item (V24) is per test
    world = {
        [P1] = { pos = { 0, 0, 0 }, comps = {} },
        [NPC] = { pos = { 10, 0, 0 }, comps = {} },
        [DOOR] = { pos = { 10, 0, 0 }, comps = { ServerItem = { IsDoor = true } } },
        [LADDER] = { pos = { 10, 0, 0 }, comps = { ServerItem = { IsLadder = true } } },
        [GEM] = { pos = { 10, 0, 0 }, comps = {} },
        [GOB] = { pos = { 2.4, 0, 0 }, comps = {} },
    }
    for _, u in ipairs({ P1, NPC, GOB }) do osiv[key("IsCharacter", u)] = 1 end
    for _, u in ipairs({ DOOR, GEM, LADDER }) do osiv[key("IsItem", u)] = 1 end
    osiv[key("IsOnStage", P1)] = 1
    for _, u in ipairs({ NPC, DOOR, GEM, GOB, LADDER }) do
        osiv[key("IsOnStage", u)] = 1
        osiv[key("CanSee", P1, u)] = 1
        osiv[key("HasLineOfSight", P1, u)] = 1
    end
    nav.fn = function(_, pos) return { reachable = true, length_m = U.dist(world[P1].pos, pos) } end
    nav.unseen = nil
    for _, j in pairs(RPC.jobs) do j.status = "done" end
    A.active = {}
end
U = BG3AI.Util

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end
function near(a, b, what) if math.abs(a - b) > 0.01 then error((what or "value") .. ": expected " .. b .. ", got " .. a, 2) end end
function refused(fn, pat)
    local ok, err = pcall(fn)
    if ok then error("expected a refusal matching '" .. pat .. "'", 2) end
    local msg = G.is_refusal(err) and err.reason or tostring(err)
    if not msg:find(pat, 1, true) then error("refusal '" .. msg .. "' does not match '" .. pat .. "'", 2) end
end
function op(name, args) return RPC.ops["act." .. name](args) end

-- Walk the character to `to` in small steps and fire the walk's EntityEvent.
function arrive(j, to)
    local from = world[P1].pos
    for i = 1, 5 do
        world[P1].pos = { from[1] + (to[1] - from[1]) * i / 5, to[2], from[3] + (to[3] - from[3]) * i / 5 }
        tick()
    end
    A.dispatch("EntityEvent", { P1, j.walk.ev })
end

-- ---- the tests: these files, in order, on the stubs above (split for the 1000-line cap) ----
local DIR = arg[0]:match("^(.*)/[^/]*$") or "."
for _, part in ipairs({ "test_act_lua_move.lua", "test_act_lua_cast.lua", "test_act_lua_think.lua",
    "test_act_lua_turns.lua", "test_act_lua_prep.lua", "test_act_lua_reach.lua", "test_act_lua_revive.lua",
    "test_act_lua_idle.lua" }) do
    dofile(DIR .. "/" .. part)
end

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
