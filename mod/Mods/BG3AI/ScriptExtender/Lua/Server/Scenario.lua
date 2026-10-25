-- Scenario setup (server context): seeded dice, Karmic Dice, the difficulty read, the avatar's name and the rename
-- perturbation for benchmark runs and start-save authoring (tools/saves/make_start_save.py).
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
--   avatar:  the name a bench run plays under. A loaded save shows its own name again in every field, also one
--            saved after a CustomName-only rename (research/fix/live.md "Bench B1"), so the mod re-applies the
--            recorded name after every load of that avatar (same uuid); scn.undo puts the originals back.
--   karmic:  NOT recorded. State of the loaded game (the save keeps it once saved), so its original lives in this
--            Lua state only: a load rebuilds it and scn.undo then has nothing to restore.

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
-- Record: {seed = n|nil, renames = {{uuid, handle, original, name}} (in apply order),
--          avatar = {uuid, handle, name, original, custom, cc}|nil (the originals: DisplayName text, CustomName,
--          CharacterCreationStats.Name)}
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

local function writeMode(mode)
    Ext.System.ServerRollSave.RollManager.RollModeType = mode
    streams().RollModeType = mode
end

-- What leaving Options with the checkbox changed does (live1 §3): RollModeType on the system and on the component
-- (the one a save keeps). Read back after the write; the first original is kept for scn.undo.
function S.setKarmic(on)
    local before = S.karmic()
    local want = on and "WeightedRandom" or "Random"
    writeMode(want)
    local after = S.karmic()
    if after.mode ~= want or after.component_mode ~= want then
        error("RollModeType read back " .. tostring(after.mode) .. " / " .. tostring(after.component_mode)
            .. " after writing " .. want)
    end
    if S.karmicOriginal == nil then S.karmicOriginal = before.mode end
    after.before = before.mode
    return after
end

-- The difficulty is the save's Ruleset component (Balanced, Explorer, Tactician, ...). Read only: the game sets it
-- through its own Options flow (Rulesets, RulesetModifiers, the DifficultyChanged event), not one field write.
function S.difficulty()
    local ok, rs = pcall(function()
        return Ext.Entity.GetAllEntitiesWithComponent("Ruleset")[1].Ruleset.Rulesets
    end)
    if not ok or not rs then error("no Ruleset component (no save loaded?)") end
    local out = {}
    for _, g in ipairs(rs) do
        local okr, r = pcall(Ext.StaticData.Get, g, "Ruleset")
        out[#out + 1] = { uuid = tostring(g), name = okr and r and r.Name or nil }
    end
    return { difficulty = out[1] and out[1].name or nil, rulesets = out }
end

------------------------------------------------------------------------
-- The avatar's name, in every field the game shows it from: CustomName (the party panel, the save folder's
-- owner), CharacterCreationStats.Name, and the text of its DisplayName handle (a ResStr_ handle of its own:
-- tooltips, the combat log and every bridge read, which goes through Osi.GetDisplayName).
------------------------------------------------------------------------

local function handleOf(uuid)
    local h = Osi.GetDisplayName(uuid)
    if type(h) ~= "string" or h == "" then error("no display name handle for " .. tostring(uuid)) end
    return h
end

local function avatarEntity()
    local host = Osi.GetHostCharacter()
    if type(host) ~= "string" or host == "" then error("no host character (no save loaded?)") end
    host = host:match("%x+%-%x+%-%x+%-%x+%-%x+$") or host
    local e = Ext.Entity.Get(host)
    if not e then error("no entity for the host character " .. host) end
    if not e.CustomName then error("the host character has no CustomName (an origin avatar can't be renamed)") end
    return e, host
end

local function ccStats(e)
    local ok, cc = pcall(function() return e.CharacterCreationStats end)
    return ok and cc or nil
end

-- Writes and reads back every field; custom/cc nil leaves that field as it is (undo of a field that had none).
local function writeAvatar(e, h, text, custom, cc)
    if custom ~= nil then
        e.CustomName.Name = custom
        pcall(function() e:Replicate("CustomName") end)
        if e.CustomName.Name ~= custom then error("CustomName read back " .. tostring(e.CustomName.Name)) end
    end
    local stats = ccStats(e)
    if stats and cc ~= nil then
        stats.Name = cc
        pcall(function() e:Replicate("CharacterCreationStats") end)
        if stats.Name ~= cc then error("CharacterCreationStats.Name read back " .. tostring(stats.Name)) end
    end
    if text ~= nil then
        if not Ext.Loca.UpdateTranslatedString(h, text) then error("UpdateTranslatedString refused " .. h) end
        local got = Ext.Loca.GetTranslatedString(h)
        if got ~= text then error("DisplayName text read back " .. tostring(got)) end
    end
end

-- Recorded before the write, so a load re-applies it; a second rename of the same avatar keeps the first originals.
function S.renameAvatar(name)
    local e, host = avatarEntity()
    local h = handleOf(host)
    local stats = ccStats(e)
    local before = e.CustomName.Name
    local a = S.rec.avatar
    if not a or a.uuid ~= host then
        a = { uuid = host, handle = h, original = Ext.Loca.GetTranslatedString(h), custom = before,
            cc = stats and stats.Name or nil }
        S.rec.avatar = a
    end
    a.name = name
    save()
    writeAvatar(e, h, name, name, name)
    return { uuid = host, original = a.custom, before = before, name = name, handle = h,
        display = Ext.Loca.GetTranslatedString(h), cc = stats and stats.Name or nil }
end

-- After a load: the recorded name again, only on the avatar it was set on.
local function reapplyAvatar(a)
    local e, host = avatarEntity()
    if host ~= a.uuid then return "not this save's avatar" end
    writeAvatar(e, handleOf(host), a.name, a.name, a.name)
    return a.name
end

-- The originals back: the DisplayName text always (Ext.Loca is process-wide), CustomName and CharacterCreationStats
-- only on that avatar while it still has the recorded name (else someone renamed it since).
local function undoAvatar(a)
    local ok, e, host = pcall(avatarEntity)
    local mine = ok and host == a.uuid and e.CustomName.Name == a.name
    writeAvatar(mine and e or nil, a.handle, a.original, mine and a.custom or nil, mine and a.cc or nil)
    return mine and a.custom or nil
end

------------------------------------------------------------------------
-- Renames
------------------------------------------------------------------------

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

-- Restores every original, newest first, then forgets them. Karmic only when this Lua state changed it (no load
-- since); the avatar's name as undoAvatar says.
function S.undo()
    local restored = {}
    for i = #S.rec.renames, 1, -1 do
        local r = S.rec.renames[i]
        if r.original ~= nil then Ext.Loca.UpdateTranslatedString(r.handle, r.original) end
        restored[#restored + 1] = { handle = r.handle, original = r.original }
    end
    local a = S.rec.avatar
    S.rec.renames = {}
    S.rec.seed = nil
    S.rec.avatar = nil
    save()
    local out = { restored = restored }
    if S.karmicOriginal ~= nil then
        local ok, err = pcall(writeMode, S.karmicOriginal)
        out.karmic = ok and S.karmicOriginal or ("not restored: " .. tostring(err))
        S.karmicOriginal = nil
    end
    if a then
        local ok, res = pcall(undoAvatar, a)
        if ok then out.avatar = res else out.avatar_error = tostring(res) end
    end
    return out
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
    if S.rec.avatar then
        local ok, res = pcall(reapplyAvatar, S.rec.avatar)
        out.avatar = ok and res or ("not re-applied: " .. tostring(res))
    end
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

-- {} reads; {set = false|true} turns Karmic Dice off / on (read back).
op("scn.karmic", function(args)
    if args.set == nil then return S.karmic() end
    if type(args.set) ~= "boolean" then error("set must be true or false") end
    return S.setKarmic(args.set)
end)

-- {} reads; a set is refused (S.difficulty: the game's Options flow owns it).
op("scn.difficulty", function(args)
    if args.set ~= nil then
        error("difficulty is read-only here: the game applies it through its Options flow (Rulesets and "
            .. "modifiers together); change it in Options > Gameplay")
    end
    return S.difficulty()
end)

-- {name}: the host avatar's name (every field; re-applied after each load until scn.undo).
op("scn.rename_avatar", function(args)
    if type(args.name) ~= "string" or not args.name:match("^[%w][%w _-]*$") or #args.name > 40 then
        error("name must be 1..40 letters, digits, spaces, _ or - (it becomes the save folder's owner)")
    end
    return S.renameAvatar(args.name)
end)

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

op("scn.undo", function() return S.undo() end)

op("scn.status", function()
    return { seed = S.rec.seed, applied = S.applied, reapplied = S.reapplied, renames = S.rec.renames,
        avatar = S.rec.avatar }
end)

return S
