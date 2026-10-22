-- Offline tests for Server/Scenario.lua: scenario setup RPCs (B-mod task 3, mod round 6).
-- Refused without BG3AI_SCENARIO_SETUP=1; seed writes the ServerRollStreams component and is re-applied after a
-- load; renames are idempotent and undone, also after a load (a fresh Lua state reading the record file); Karmic
-- set/read back, the difficulty read (set refused) and the avatar's CustomName, undone only in the same Lua state.
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
local modeWrites = true -- false: the engine ignores a RollModeType write (the read-back must catch it)
local rollManager = setmetatable({}, {
    __index = function(_, k)
        if k == "RollModeType" then return setmetatable({}, { __tostring = function() return systemMode end }) end
    end,
    __newindex = function(_, k, v) if k == "RollModeType" and modeWrites then systemMode = v end end,
})
local BALANCED, EXPLORER = "11111111-0000-0000-0000-000000000001", "11111111-0000-0000-0000-000000000002"
local rulesets = { BALANCED }
local TAV = "S_Player_Claude_cccccccc-0000-0000-0000-000000000001"
local TAV_UUID = "cccccccc-0000-0000-0000-000000000001"
local replicated = {}
local avatar = { CustomName = { Name = "Claude" } }
function avatar:Replicate(c) replicated[#replicated + 1] = c end
local host = TAV

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
    Entity = {
        GetAllEntitiesWithComponent = function(c)
            if c == "Ruleset" then return rulesets and { { Ruleset = { Rulesets = rulesets } } } or {} end
            return { { ServerRollStreams = RS } }
        end,
        Get = function(u) return u == TAV_UUID and avatar or nil end,
    },
    StaticData = { Get = function(g, t)
        if t ~= "Ruleset" then return nil end
        return ({ [BALANCED] = { Name = "Balanced" }, [EXPLORER] = { Name = "Explorer" } })[g]
    end },
    System = { ServerRollSave = { RollManager = rollManager } },
    Loca = {
        GetTranslatedString = function(h) return loca[h] end,
        UpdateTranslatedString = function(h, s) loca[h] = s return true end,
    },
}
Osi = { GetDisplayName = function(u) return handles[u] end, GetHostCharacter = function() return host end }

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
for _, name in ipairs({ "scn.seed", "scn.karmic", "scn.rename", "scn.undo", "scn.status", "scn.difficulty",
    "scn.rename_avatar" }) do
    local ok, err = call(name, { seed = 1, renames = { { uuid = MATTIS, name = "X" } }, set = false, name = "Tav" })
    check("refused " .. name, not ok and tostring(err):find("BG3AI_SCENARIO_SETUP=1", 1, true), err)
end
check("refused: no seed written", RS.BaseSeed == 1792610389 and RS.Streams[1].State.NumRolls == 614)
check("refused: no rename", loca.hMattis == "Mattis")
check("refused: no record", files[S.RECORD] == nil)
check("refused: karmic and avatar untouched", systemMode == "Random" and RS.RollModeType == "WeightedRandom"
    and avatar.CustomName.Name == "Claude")
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

-- Karmic set: both copies written and read back; the first original is kept; undo restores it.
do
    S = boot()
    systemMode, RS.RollModeType = "WeightedRandom", "WeightedRandom"
    local res
    local ok, k = call("scn.karmic", { set = false })
    check("karmic set off", ok and k.karmic == false and k.mode == "Random" and k.component_mode == "Random"
        and k.before == "WeightedRandom", k)
    check("karmic off written", systemMode == "Random" and RS.RollModeType == "Random")
    ok, k = call("scn.karmic", { set = false })
    check("karmic off again", ok and k.before == "Random" and S.karmicOriginal == "WeightedRandom")
    check("karmic bad set", not call("scn.karmic", { set = "no" }))
    ok, k = call("scn.karmic")
    check("karmic read still", ok and k.karmic == false and k.before == nil)
    modeWrites = false
    local okw, err = call("scn.karmic", { set = true })
    check("karmic write ignored: refused", not okw and tostring(err):find("read back", 1, true), err)
    modeWrites = true
    RS.RollModeType = "Random"
    ok, res = call("scn.undo")
    check("undo restores karmic", ok and res.karmic == "WeightedRandom" and systemMode == "WeightedRandom"
        and RS.RollModeType == "WeightedRandom", res and res.karmic)
    ok, res = call("scn.undo")
    check("undo karmic once", ok and res.karmic == nil)
end

-- Difficulty: read from the Ruleset component; a set is refused and changes nothing.
do
    local ok, d = call("scn.difficulty")
    check("difficulty read", ok and d.difficulty == "Balanced" and d.rulesets[1].uuid == BALANCED, d)
    rulesets = { EXPLORER, "11111111-0000-0000-0000-00000000dead" }
    ok, d = call("scn.difficulty")
    check("difficulty explorer", ok and d.difficulty == "Explorer" and #d.rulesets == 2 and d.rulesets[2].name == nil)
    local okset, err = call("scn.difficulty", { set = "Balanced" })
    check("difficulty set refused", not okset and tostring(err):find("read-only", 1, true), err)
    check("difficulty unchanged", rulesets[1] == EXPLORER)
    rulesets = nil
    okset, err = call("scn.difficulty")
    check("no ruleset component", not okset and tostring(err):find("Ruleset", 1, true), err)
    rulesets = { BALANCED }
end

-- Avatar rename: CustomName written, replicated, read back; the first original kept; undo only while it's ours.
do
    local res
    local ok, r = call("scn.rename_avatar", { name = "Tav" })
    check("avatar renamed", ok and r.uuid == TAV_UUID and r.original == "Claude" and r.name == "Tav"
        and avatar.CustomName.Name == "Tav" and replicated[#replicated] == "CustomName", r)
    ok, r = call("scn.rename_avatar", { name = "Tav Two" })
    check("avatar rename again keeps original", ok and r.original == "Claude" and r.before == "Tav")
    for _, bad in ipairs({ "", " Tav", "Tav.", ("x"):rep(41), 5 }) do
        check("bad avatar name " .. tostring(bad), not call("scn.rename_avatar", { name = bad }))
    end
    check("bad names change nothing", avatar.CustomName.Name == "Tav Two")
    ok, res = call("scn.undo")
    check("undo restores avatar", ok and res.avatar == "Claude" and avatar.CustomName.Name == "Claude", res)
    -- Someone else renamed it since: undo leaves it.
    call("scn.rename_avatar", { name = "Tav" })
    avatar.CustomName.Name = "Other"
    ok, res = call("scn.undo")
    check("undo leaves a foreign name", ok and res.avatar == nil and avatar.CustomName.Name == "Other")
    -- A load (fresh Lua state): the save keeps the name; undo has nothing to restore and the record has no avatar.
    call("scn.rename_avatar", { name = "Tav" })
    S = boot()
    ok, res = call("scn.undo")
    check("no avatar undo after a load", ok and res.avatar == nil and avatar.CustomName.Name == "Tav")
    check("avatar not in the record", decode(files[S.RECORD]).avatar == nil)
    -- No CustomName / no host / no entity.
    avatar.CustomName = nil
    local okn, err = call("scn.rename_avatar", { name = "Tav" })
    check("no CustomName", not okn and tostring(err):find("CustomName", 1, true), err)
    avatar.CustomName = { Name = "Claude" }
    host = nil
    check("no host", not call("scn.rename_avatar", { name = "Tav" }))
    host = "dddddddd-0000-0000-0000-000000000009"
    check("no entity", not call("scn.rename_avatar", { name = "Tav" }))
    host = TAV
end

print(string.format("scenario: %d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
