-- Scenario setup (server context): seeded dice, the Karmic Dice read and rename perturbation for benchmark runs.
--
-- Every op is refused unless the game process runs with BG3AI_SCENARIO_SETUP=1 (a normal playthrough never has it),
-- and only the harness calls them, never an MCP tool (bridge/tests/test_referee.py). These are not agent actions,
-- so they don't go through Guard's G.osi; the ops are fixed (no generic Osiris or Ext call), which is the allowlist.
--
-- What setup changed is kept in a record file (Ext.IO, SE's own folder) because a save load rebuilds this Lua
-- state while its effects behave differently (research/ovh/live1.md):
--   seed:    every load reseeds the dice from the wall clock (§4); the mod re-applies the recorded seed on
--            LevelGameplayStarted, before the first roll it can reach (writes go to the ServerRollStreams component,
--            the system copy is overwritten from it within a tick, §5).
--   renames: Ext.Loca changes are process-wide and survive loads (§8): the record keeps each original text so
--            scn.undo restores it even after a load, and re-applies after a game restart.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local S = BG3AI.Scenario or {}
BG3AI.Scenario = S

S.SWITCH = "BG3AI_SCENARIO_SETUP"
S.RECORD = "BG3AI/scenario.json"

-- The game process's environment: bg3le keeps os.getenv for its own code as Ext._Internal.Getenv.
function S.getenv(name)
    local get = (os and os.getenv) or (Ext._Internal and Ext._Internal.Getenv)
    if not get then return nil end
    local ok, v = pcall(get, name)
    return ok and v or nil
end

function S.enabled() return S.getenv(S.SWITCH) == "1" end

local function refuse()
    if not S.enabled() then
        error("scenario setup is off: the game was not started with " .. S.SWITCH .. "=1")
    end
end

------------------------------------------------------------------------
-- Record: {seed = n|nil, renames = {{uuid, handle, original, name}}} (renames in apply order)
------------------------------------------------------------------------

function S.load()
    local ok, txt = pcall(Ext.IO.LoadFile, S.RECORD)
    local rec = ok and txt and txt ~= "" and select(2, pcall(Ext.Json.Parse, txt)) or nil
    if type(rec) ~= "table" then rec = {} end
    rec.renames = type(rec.renames) == "table" and rec.renames or {}
    S.rec = rec
    return rec
end

local function save()
    local ok, err = pcall(Ext.IO.SaveFile, S.RECORD, Ext.Json.Stringify(S.rec, { Beautify = false }))
    if not ok then error("cannot write " .. S.RECORD .. ": " .. tostring(err)) end
end

------------------------------------------------------------------------
-- Dice
------------------------------------------------------------------------

local function streams()
    local ok, r = pcall(function()
        return Ext.Entity.GetAllEntitiesWithComponent("ServerRollStreams")[1].ServerRollStreams
    end)
    if not ok or not r then error("no ServerRollStreams component (no save loaded?)") end
    return r
end

-- live1 §5: BaseSeed = seed, stream i's Seed = seed + i, NumRolls = 0 gives the same roll sequence every time.
function S.applySeed(seed)
    local r = streams()
    r.BaseSeed = seed
    local n = 0
    for i, s in ipairs(r.Streams) do
        s.State.Seed = seed + i
        s.State.NumRolls = 0
        n = i
    end
    S.applied = { seed = seed, gt = Ext.Utils.GameTime and Ext.Utils.GameTime() or nil, streams = n }
    return { seed = seed, base_seed = r.BaseSeed, streams = n }
end

-- The Options setting lives in the system copy (it follows Options; the component's read differs, live1 §5).
function S.karmic()
    local mode = tostring(Ext.System.ServerRollSave.RollManager.RollModeType)
    local ok, comp = pcall(function() return tostring(streams().RollModeType) end)
    return { mode = mode, karmic = mode == "WeightedRandom", component_mode = ok and comp or nil }
end

------------------------------------------------------------------------
-- Renames
------------------------------------------------------------------------

local function handleOf(uuid)
    local h = Osi.GetDisplayName(uuid)
    if type(h) ~= "string" or h == "" then error("no display name handle for " .. tostring(uuid)) end
    return h
end

local function findRename(handle)
    for _, r in ipairs(S.rec.renames) do
        if r.handle == handle then return r end
    end
    return nil
end

-- Idempotent: a second apply of the same handle keeps the first original (never the perturbed text).
function S.rename(uuid, name)
    local h = handleOf(uuid)
    local r = findRename(h)
    if not r then
        r = { uuid = uuid, handle = h, original = Ext.Loca.GetTranslatedString(h) }
        S.rec.renames[#S.rec.renames + 1] = r
    end
    r.name = name
    save()
    if not Ext.Loca.UpdateTranslatedString(h, name) then error("UpdateTranslatedString refused " .. h) end
    return { uuid = uuid, handle = h, original = r.original, name = name }
end

-- Restores every original, newest first, then forgets them.
function S.undo()
    local restored = {}
    for i = #S.rec.renames, 1, -1 do
        local r = S.rec.renames[i]
        if r.original ~= nil then Ext.Loca.UpdateTranslatedString(r.handle, r.original) end
        restored[#restored + 1] = { handle = r.handle, original = r.original }
    end
    S.rec.renames = {}
    S.rec.seed = nil
    save()
    return restored
end

------------------------------------------------------------------------
-- After a load: re-apply what the record says (switch on only).
------------------------------------------------------------------------

function S.reapply()
    if not S.enabled() then return nil end
    S.load()
    local out = {}
    if S.rec.seed then
        local ok, res = pcall(S.applySeed, S.rec.seed)
        out.seed = ok and res or tostring(res)
    end
    for _, r in ipairs(S.rec.renames) do
        pcall(Ext.Loca.UpdateTranslatedString, r.handle, r.name)
    end
    out.renames = #S.rec.renames
    S.reapplied = out
    return out
end

S.load()
pcall(Ext.Osiris.RegisterListener, "LevelGameplayStarted", 2, "after", function()
    local ok, err = pcall(S.reapply)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] scenario re-apply: " .. tostring(err)) end
end)

------------------------------------------------------------------------
-- Ops (harness only; never an MCP tool). All refused without the switch.
------------------------------------------------------------------------

local function op(name, fn)
    RPC.register(name, function(args)
        refuse()
        return fn(args or {})
    end)
end

-- {seed}: write it now and record it for every later load.
op("scn.seed", function(args)
    local seed = math.tointeger and math.tointeger(tonumber(args.seed)) or tonumber(args.seed)
    if type(seed) ~= "number" or seed < 0 or seed % 1 ~= 0 or seed > 2000000000 then
        error("seed must be an integer in 0..2000000000")
    end
    S.rec.seed = seed
    save()
    return S.applySeed(seed)
end)

op("scn.karmic", function() return S.karmic() end)

-- {renames = [{uuid, name}]}
op("scn.rename", function(args)
    local out = {}
    for _, r in ipairs(args.renames or {}) do
        if type(r.uuid) ~= "string" or type(r.name) ~= "string" or r.name == "" then
            error("each rename needs a uuid and a non-empty name")
        end
        out[#out + 1] = S.rename(r.uuid, r.name)
    end
    return out
end)

op("scn.undo", function() return { restored = S.undo() } end)

op("scn.status", function()
    return { seed = S.rec.seed, applied = S.applied, reapplied = S.reapplied, renames = S.rec.renames }
end)

return S
