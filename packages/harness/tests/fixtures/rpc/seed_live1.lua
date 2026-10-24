-- Seed fixtures from the live1 probe (research/ovh/live1.md): the raw Osiris events and roll outcomes recorded in the
-- real game, played through the REAL mod files (Server/Referee.lua, RollLog.lua, Scenario.lua) with Ext/Osi stubbed
-- like tools/dev/test_*.lua. Prints fixture lines (bg3ai/rpc_record.py format v1) for ref.*, roll.* and scn.* to
-- stdout, one JSON object per line. Run through seed_live1.py, which feeds the data chunk (arg[2]).

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local DATA = dofile(arg[2])  -- { events = {{ev, args, t, tag}}, rolls = {res=..., crit=...}, streams0, streams1 }

------------------------------------------------------------------------
-- JSON out (sorted keys, integers as integers): the reply bodies as Ext.Json would write them.
------------------------------------------------------------------------
local encode
local function isArray(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    if n == 0 then return false end
    for i = 1, n do if t[i] == nil then return false end end
    return true
end
local function str(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        local m = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\t'] = '\\t', ['\r'] = '\\r' }
        return m[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
end
encode = function(v)
    local t = type(v)
    if v == nil then return "null" end
    if t == "boolean" then return tostring(v) end
    if t == "number" then
        if math.type(v) == "integer" then return tostring(v) end
        if v % 1 == 0 and math.abs(v) < 2 ^ 53 then return string.format("%d", v) end
        return string.format("%.14g", v)
    end
    if t == "string" then return str(v) end
    if isArray(v) then
        local out = {}
        for i = 1, #v do out[i] = encode(v[i]) end
        return "[" .. table.concat(out, ",") .. "]"
    end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    local out = {}
    for _, k in ipairs(keys) do
        local x = v[k]
        if x == nil then x = v[tonumber(k)] end
        out[#out + 1] = str(k) .. ":" .. encode(x)
    end
    return "{" .. table.concat(out, ",") .. "}"
end

------------------------------------------------------------------------
-- Stubs shared by the three modules
------------------------------------------------------------------------
local HOST = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
local TRADER = "e1fda08b-45d4-41db-84b5-20e9931ba5af"
local BARD = "4a405fba-3000-4c63-97e5-a8001ebb883c"
local GOB = "b1e2d6a7-0fad-4485-ae2e-2ec78ce4e658"
local GUARD = "825714dd-7df9-4cd1-aecb-edf577baa487"
local ZERO = "00000000-0000-0000-0000-000000000000"
-- Display names as the game shows them (the party leader is the probe save's neutral "Tav").
local NAMES = { [HOST] = "Tav", [TRADER] = "Mattis", [BARD] = "Bard", [GOB] = "Goblin", [GUARD] = "Guard" }
local ITEM_NAMES = { LOOT_Gold_A = "Gold", TOOL_GEN_Shovel_A = "Shovel", WPN_HUM_Shortbow_A_0 = "Shortbow",
    CONS_Potion_Invisible_A = "Potion of Invisibility", CONS_FOOD_Vegetable_Carrot_A = "Carrot",
    S_TUT_Lab_Book_001 = "Book", LOOT_MF_Rune_Tablet_E = "Rune Tablet", CONT_Barrel_A_126 = "Barrel" }
local itemTpl, dead = {}, {}
local function bare(x) return type(x) == "string" and x:match("%x+%-%x+%-%x+%-%x+%-%x+$") or nil end
for _, e in ipairs(DATA.events) do
    for _, a in ipairs(e.args) do
        local tpl = type(a) == "string" and a:match("^(.-)_%x+%-%x+%-%x+%-%x+%-%x+$")
        if tpl and ITEM_NAMES[tpl] then itemTpl[bare(a)] = tpl end
    end
end
local clock, gold = 0, 100
local listeners, ops, onCreate = {}, {}, {}
local RS
local env, files, loca = {}, {}, { hMattis = "Mattis" }

os.getenv = function(k) return env[k] end -- luacheck: ignore (stub)
Ext = {
    _Internal = {},
    Osiris = { RegisterListener = function(name, arity, _, fn) listeners[name .. "/" .. arity] = fn end },
    Utils = { MonotonicTime = function() return clock end, GameTime = function() return clock / 1000 end,
        PrintWarning = function(m) io.stderr:write("WARN " .. m .. "\n") end, Print = function() end },
    Entity = {
        GetAllEntitiesWithComponent = function() return { { ServerRollStreams = RS } } end,
        OnCreate = function(name, fn) onCreate[name] = fn end,
        OnSystemPostUpdate = function() end,
    },
    Events = { Tick = { Subscribe = function() end } },
    IO = { LoadFile = function(p) return files[p] end, SaveFile = function(p, s) files[p] = s return true end },
    Json = { Stringify = function(v) return encode(v) end, Parse = function() return nil end },
    System = { ServerRollSave = { RollManager = { RollModeType = "Random" } } },
    Loca = {
        GetTranslatedString = function(h) return loca[h] end,
        UpdateTranslatedString = function(h, s) loca[h] = s NAMES[TRADER] = s return true end,
    },
}
Osi = {
    PartyGetGold = function() return gold end,
    GetStackAmount = function() return 1 end,
    IsCharacter = function(u) return (NAMES[bare(u) or ""] and 1) or 0 end,
    IsItem = function(u) return itemTpl[bare(u) or ""] and 1 or 0 end,
    IsDead = function(u) return dead[bare(u) or ""] and 1 or 0 end,
    CanTrade = function(u) return bare(u) == TRADER and 1 or 0 end,
    CombatGetGuidFor = function() return nil end,
    GetTemplate = function(u) local t = itemTpl[bare(u) or ""] return t and (t .. "_" .. ZERO) or nil end,
    GetDisplayName = function(u) return bare(u) == TRADER and "hMattis" or nil end,
}
BG3AI = {
    RPC = { push = function() end, register = function(name, fn) ops[name] = fn end, QUIET_OPS = {},
        BOOT = "1000-123456789" },
    Visibility = {
        osi = function(fn, ...) local ok, r = pcall(fn, ...) if ok then return r end return nil end,
        uuid = bare,
        party = function() return { HOST }, { [HOST] = true } end,
        name = function(u)
            local b = bare(u) or ""
            return NAMES[b] or ITEM_NAMES[itemTpl[b] or ""] or ("Unknown " .. b:sub(1, 4))
        end,
    },
}
dofile(LUA .. "/Shared/Util.lua")

-- One fixture line per op call, answered as RPC.lua's dispatcher would ({ok, result} or {ok=false, error}).
local T0 = nil
local function call(op, args)
    T0 = T0 or clock
    local ok, res = pcall(ops[op], args)
    local reply = ok and { ok = true, result = res } or { ok = false, error = tostring(res) }
    io.write(encode({ v = 1, t = (clock - T0) / 1000, ms = 1, ctx = "server",
        calls = { { op = op, args = args } }, replies = { reply } }), "\n")
    return res
end

------------------------------------------------------------------------
-- Referee: the live1 events in time order, polled after each probe step (tag) the way RingReader polls.
------------------------------------------------------------------------
dofile(LUA .. "/Server/Referee.lua")
local GOLD_HOLDER = { [HOST] = true }
local cursor = 0
local function poll()
    local res = call("ref.since", { seq = cursor, limit = 500 })
    if res.latest then cursor = res.latest end
end
call("ref.since", { seq = 1000000000000, limit = 1 })
local tag
for _, e in ipairs(DATA.events) do
    if tag and e.tag ~= tag then poll() end
    tag = e.tag
    clock = math.floor(e.t * 1000 + 0.5)
    if e.ev == "GoldChanged" and GOLD_HOLDER[bare(e.args[1]) or ""] then gold = gold + tonumber(e.args[2]) end
    if e.ev == "Died" then dead[bare(e.args[1]) or ""] = true end
    local fn = listeners[e.ev .. "/" .. #e.args]
    if fn then fn(table.unpack(e.args)) end
end
poll()
call("ref.snapshot", {})

------------------------------------------------------------------------
-- Roll log: live1 l3 run 0, 24 passive Perception checks (RollResult/6), with the stream state before and after.
------------------------------------------------------------------------
local function streams(s)
    local out = {}
    for _, x in ipairs(s.streams) do
        out[#out + 1] = { StreamType = x.t, PlayerUuid = "", CombatGuid = ZERO,
            State = { NumRolls = x.n, Seed = x.seed, Frame = x.f, FailDebt = 0.0, SuccessDebt = 0.0 } }
    end
    return { BaseSeed = s.seed, RollModeType = s.mode, Streams = out }
end
RS = streams(DATA.streams0)
dofile(LUA .. "/Server/RollLog.lua")
call("roll.snapshot", {})
call("roll.since", { seq = 1000000000000, limit = 1 })
local P = "HalfOrcs_Female_Player_" .. HOST
clock = clock + 1000
for i, r in ipairs(DATA.rolls) do
    clock = clock + 5
    listeners["RollResult/6"]("probe" .. i, P, P, r.res, 0, r.crit)
end
RS = streams(DATA.streams1)
call("roll.since", { seq = 0, limit = 500 })
call("roll.snapshot", {})

------------------------------------------------------------------------
-- Scenario setup: refused without the switch, then the live1 §5 seed and the §8 rename/undo.
------------------------------------------------------------------------
RS = streams(DATA.streams0)
dofile(LUA .. "/Server/Scenario.lua")
call("scn.status", {})
env.BG3AI_SCENARIO_SETUP = "1"
call("scn.status", {})
call("scn.karmic", {})
call("scn.seed", { seed = 12345 })
call("scn.rename", { renames = { { uuid = TRADER, name = "Test Name" } } })
call("scn.status", {})
call("scn.undo", {})
call("scn.seed", { seed = -1 })
