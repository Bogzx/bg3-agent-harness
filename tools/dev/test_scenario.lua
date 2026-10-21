-- Offline tests for Server/Scenario.lua: scenario setup RPCs (B-mod task 3).
-- Refused without BG3AI_SCENARIO_SETUP=1; seed writes the ServerRollStreams component and is re-applied after a
-- load; renames are idempotent and undone, also after a load (a fresh Lua state reading the record file).
-- Ext, Osi and RPC are stubbed. Run: tools/dev/test_scenario.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local MATTIS = "e1fda08b-45d4-41db-84b5-20e9931ba5af"
local GALE = "aaaaaaaa-0000-0000-0000-000000000003"
local env = {}
local files = {}
local loca = { hMattis = "Mattis", hGale = "Gale" }
local handles = { [MATTIS] = "hMattis", [GALE] = "hGale" }
local listeners, ops = {}, {}
local ZERO = "00000000-0000-0000-0000-000000000000"

local function stream(t, n)
    return { StreamType = t, PlayerUuid = "", CombatGuid = ZERO,
        State = { NumRolls = n, Seed = 1792610390, Frame = 1, FailDebt = 0.0, SuccessDebt = 0.0 } }
end
local RS
local function freshStreams()
    RS = { BaseSeed = 1792610389, RollModeType = "WeightedRandom",
        Streams = { stream("Generic", 614), stream("PlayerDamage", 3), stream("Dialog", 0) } }
end
freshStreams()
local systemMode = "Random"

-- Ext.Json stand-in: Stringify keeps a deep copy under a token string, Parse returns a fresh copy of it, so the
-- record file round-trips like JSON would (no shared tables between Lua states).
local function copy(v)
    if type(v) ~= "table" then return v end
    local out = {}
    for k, x in pairs(v) do out[k] = copy(x) end
    return out
end
local stored, nstored = {}, 0
local function encode(v)
    nstored = nstored + 1
    local tok = "json#" .. nstored
    stored[tok] = copy(v)
    return tok
end
local function decode(s) return copy(stored[s]) end

local function boot()
    listeners, ops = {}, {}
    BG3AI = {
        RPC = { register = function(name, fn) ops[name] = fn end, QUIET_OPS = {}, BOOT = "b" },
    }
    dofile(LUA .. "/Server/Scenario.lua")
    return BG3AI.Scenario
end

os.getenv = function(k) return env[k] end -- luacheck: ignore (stub)
Ext = {
    _Internal = {},
    Osiris = { RegisterListener = function(name, arity, _, fn) listeners[name .. "/" .. arity] = fn end },
    Utils = { GameTime = function() return 42.0 end, PrintWarning = function(m) print("WARN " .. m) end },
    IO = {
        LoadFile = function(p) return files[p] end,
        SaveFile = function(p, s) files[p] = s return true end,
    },
    Json = { Stringify = function(v) return encode(v) end, Parse = function(s) return decode(s) end },
    Entity = { GetAllEntitiesWithComponent = function() return { { ServerRollStreams = RS } } end },
    System = { ServerRollSave = { RollManager = { RollModeType = setmetatable({}, {
        __tostring = function() return systemMode end }) } } },
    Loca = {
        GetTranslatedString = function(h) return loca[h] end,
        UpdateTranslatedString = function(h, s) loca[h] = s return true end,
    },
}
Osi = { GetDisplayName = function(u) return handles[u] end }

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end
local function call(name, args)
    local ok, res = pcall(ops[name], args or {})
    return ok, res
end

local S = boot()

-- Refused without the switch: every op, and nothing changes.
for _, name in ipairs({ "scn.seed", "scn.karmic", "scn.rename", "scn.undo", "scn.status" }) do
    local ok, err = call(name, { seed = 1, renames = { { uuid = MATTIS, name = "X" } } })
    check("refused " .. name, not ok and tostring(err):find("BG3AI_SCENARIO_SETUP=1", 1, true), err)
end
check("refused: no seed written", RS.BaseSeed == 1792610389 and RS.Streams[1].State.NumRolls == 614)
check("refused: no rename", loca.hMattis == "Mattis")
check("refused: no record", files[S.RECORD] == nil)
env.BG3AI_SCENARIO_SETUP = "0"
check("switch must be 1", not call("scn.karmic"))

-- bg3le's Ext._Internal.Getenv is used when os.getenv is gone.
do
    local saved = os.getenv
    os.getenv = nil
    Ext._Internal.Getenv = function(k) return k == "BG3AI_SCENARIO_SETUP" and "1" or nil end
    check("internal getenv", S.enabled())
    os.getenv = saved
    Ext._Internal.Getenv = nil
end

env.BG3AI_SCENARIO_SETUP = "1"

-- Karmic read: the system copy decides.
do
    local ok, k = call("scn.karmic")
    check("karmic off", ok and k.mode == "Random" and k.karmic == false and k.component_mode == "WeightedRandom")
    systemMode = "WeightedRandom"
    ok, k = call("scn.karmic")
    check("karmic on", ok and k.karmic == true)
end

-- Seed: live1 §5 layout; bad seeds refused; idempotent.
do
    check("bad seed", not call("scn.seed", { seed = -1 }) and not call("scn.seed", { seed = 1.5 })
        and not call("scn.seed", { seed = "x" }))
    local ok, res = call("scn.seed", { seed = 12345 })
    check("seed ok", ok and res.seed == 12345 and res.streams == 3, res)
    check("base seed", RS.BaseSeed == 12345)
    check("stream seeds", RS.Streams[1].State.Seed == 12346 and RS.Streams[3].State.Seed == 12348)
    check("num rolls 0", RS.Streams[1].State.NumRolls == 0 and RS.Streams[2].State.NumRolls == 0)
    RS.Streams[1].State.NumRolls = 7
    ok = call("scn.seed", { seed = 12345 })
    check("seed again", ok and RS.Streams[1].State.Seed == 12346 and RS.Streams[1].State.NumRolls == 0)
    check("seed recorded", decode(files[S.RECORD]).seed == 12345)
end

-- Rename: records the original once; a second apply doesn't record the perturbed text as original.
do
    local ok, res = call("scn.rename", { renames = { { uuid = MATTIS, name = "Test Name" } } })
    check("rename ok", ok and res[1].original == "Mattis" and loca.hMattis == "Test Name", res)
    ok, res = call("scn.rename", { renames = { { uuid = MATTIS, name = "Other Name" }, { uuid = GALE, name = "Gil" } } })
    check("rename idempotent", ok and res[1].original == "Mattis" and loca.hMattis == "Other Name")
    check("one record per handle", #S.rec.renames == 2, #S.rec.renames)
    check("bad rename", not call("scn.rename", { renames = { { uuid = MATTIS, name = "" } } }))
    check("unknown uuid", not call("scn.rename", { renames = { { uuid = "nope", name = "X" } } }))
    check("unknown uuid adds nothing", #S.rec.renames == 2)
end

-- A load: fresh Lua state, the engine reseeded from the clock; renames are still in the Loca table.
-- LevelGameplayStarted re-applies the seed from the record file.
do
    freshStreams()
    S = boot()
    check("record read back", S.rec.seed == 12345 and #S.rec.renames == 2, S.rec.seed)
    listeners["LevelGameplayStarted/2"]("WLD_Main_A", 0)
    check("seed re-applied after load", RS.BaseSeed == 12345 and RS.Streams[2].State.Seed == 12347
        and RS.Streams[1].State.NumRolls == 0)
    local ok, st = call("scn.status")
    check("status", ok and st.seed == 12345 and st.reapplied and st.reapplied.renames == 2)
end

-- A game restart: Loca back to the originals; the re-apply renames again.
do
    loca.hMattis, loca.hGale = "Mattis", "Gale"
    S = boot()
    listeners["LevelGameplayStarted/2"]("WLD_Main_A", 0)
    check("renames re-applied after restart", loca.hMattis == "Other Name" and loca.hGale == "Gil")
end

-- Undo (in the new Lua state): originals back, record cleared; a second undo is a no-op.
do
    local ok, res = call("scn.undo")
    check("undo ok", ok and #res.restored == 2)
    check("originals restored", loca.hMattis == "Mattis" and loca.hGale == "Gale")
    check("record cleared", S.rec.seed == nil and #S.rec.renames == 0)
    ok, res = call("scn.undo")
    check("undo idempotent", ok and #res.restored == 0 and loca.hMattis == "Mattis")
    freshStreams()
    S = boot()
    listeners["LevelGameplayStarted/2"]("WLD_Main_A", 0)
    check("no re-apply after undo", RS.BaseSeed == 1792610389)
end

-- Switch off at a later load: nothing re-applied even with a record.
do
    call("scn.seed", { seed = 777 })
    env.BG3AI_SCENARIO_SETUP = nil
    freshStreams()
    S = boot()
    listeners["LevelGameplayStarted/2"]("WLD_Main_A", 0)
    check("switch off: no re-apply", RS.BaseSeed == 1792610389)
end

-- No save loaded: seed fails cleanly.
do
    env.BG3AI_SCENARIO_SETUP = "1"
    local saved = Ext.Entity.GetAllEntitiesWithComponent
    Ext.Entity.GetAllEntitiesWithComponent = function() return {} end
    local ok, err = call("scn.seed", { seed = 1 })
    check("no component", not ok and tostring(err):find("ServerRollStreams", 1, true), err)
    Ext.Entity.GetAllEntitiesWithComponent = saved
end

print(string.format("scenario: %d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
