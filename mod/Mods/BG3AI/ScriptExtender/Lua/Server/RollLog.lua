-- Hidden roll log (server context): every die roll the game makes, for the benchmark's luck covariate (D9).
--
-- Never agent-visible: nothing here calls RPC.push; only the harness reads it through roll.since / roll.snapshot
-- (bridge/src/bg3ai/referee.py; no MCP tool calls them, see bridge/tests/test_referee.py). The fair, filtered
-- roll_result event the agent sees stays in Server/Events.lua.
--
-- One record per roll: {seq, t (monotonic ms), gt, src, roller, party, success, active, crit, natural, dc, ...,
-- streams = {<stream> = rolls drawn since the previous record}}. Sources, merged into one record when they describe
-- the same roll (same roller within MERGE_MS):
--   osi      Osiris RollResult/6 (fires live: research/ovh/live1.md §4): success, active, criticality
--   dialog   Osiris DialogRollResult/5 (osi reference arity; not yet seen firing)
--   finished the ServerRollFinishedEvent one-frame component (natural roll, DC, skill, ability; not yet seen live)
-- roll.snapshot reports which sources fired at least once.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local L = BG3AI.RollLog or {}
BG3AI.RollLog = L

L.CAP = 4000
L.MERGE_MS = 250
L.ring = L.ring or {}
L.seq = L.seq or 0
L.fired = L.fired or { osi = 0, dialog = 0, finished = 0 }
L.lastRolls = L.lastRolls or nil -- stream key -> NumRolls at the previous record

local truthy = U.truthy

local function now_ms() return Ext.Utils.MonotonicTime() end
local function game_s()
    local ok, t = pcall(function() return Ext.Utils.GameTime() end)
    return ok and t or nil
end

local function isParty(u)
    local _, seen = V.party()
    return u ~= nil and seen ~= nil and seen[u] == true
end

------------------------------------------------------------------------
-- Roll streams (the ServerRollStreams component: live1 §5, the authority; the system copy lags)
------------------------------------------------------------------------

local ZERO = "00000000-0000-0000-0000-000000000000"

-- "Generic", "PlayerDamage:<player>", "NPCSpell@<combat>": one key per stream.
function L.streamKey(s)
    local key = tostring(s.StreamType)
    if s.PlayerUuid and s.PlayerUuid ~= "" then key = key .. ":" .. s.PlayerUuid end
    if s.CombatGuid and s.CombatGuid ~= "" and s.CombatGuid ~= ZERO then key = key .. "@" .. s.CombatGuid end
    if s.RandomCastUuid and s.RandomCastUuid ~= "" then key = key .. "#" .. s.RandomCastUuid end
    return key
end

function L.streams()
    local ok, r = pcall(function()
        return Ext.Entity.GetAllEntitiesWithComponent("ServerRollStreams")[1].ServerRollStreams
    end)
    if ok then return r end
    return nil
end

-- {key -> NumRolls} now, or nil when the component can't be read.
function L.counts()
    local r = L.streams()
    if not r then return nil end
    local out = {}
    local ok = pcall(function()
        for _, s in ipairs(r.Streams) do out[L.streamKey(s)] = s.State.NumRolls end
    end)
    return ok and out or nil
end

-- Rolls drawn per stream since the previous call (a stream new since then counts from 0).
local function drawn()
    local now = L.counts()
    if not now then return nil end
    local out, any = {}, false
    local before = L.lastRolls
    L.lastRolls = now
    if not before then return nil end
    for k, n in pairs(now) do
        local b = before[k] or 0
        local d = n >= b and n - b or n -- a reseed (Server/Scenario.lua) restarts NumRolls at 0
        if d > 0 then out[k], any = d, true end
    end
    return any and out or nil
end

------------------------------------------------------------------------
-- Ring
------------------------------------------------------------------------

-- A record of another source for the same roller a moment ago takes these fields instead of a new record.
local function mergeTarget(src, roller, t)
    for k = #L.ring, math.max(1, #L.ring - 8), -1 do
        local rec = L.ring[k]
        if t - rec.t > L.MERGE_MS then return nil end
        if rec.roller == roller and not rec.srcs[src] then return rec end
    end
    return nil
end

function L.add(src, data)
    L.fired[src] = (L.fired[src] or 0) + 1
    local t = now_ms()
    local rec = data.roller and mergeTarget(src, data.roller, t)
    if rec then
        for k, v in pairs(data) do
            if rec[k] == nil then rec[k] = v end
        end
    else
        L.seq = L.seq + 1
        rec = data
        rec.seq, rec.t, rec.gt, rec.srcs = L.seq, t, game_s(), {}
        rec.party = isParty(data.roller)
        L.ring[#L.ring + 1] = rec
        while #L.ring > L.CAP do table.remove(L.ring, 1) end
    end
    rec.srcs[src] = true
    local d = drawn()
    if d then
        rec.streams = rec.streams or {}
        for k, n in pairs(d) do rec.streams[k] = (rec.streams[k] or 0) + n end
    end
    return rec
end

-- Osiris criticality: 1 = natural 20, 2 = natural 1 (Server/Events.lua, S009).
local CRIT = { [1] = "nat20", [2] = "nat1" }

local function listen(name, arity, fn)
    local ok, err = pcall(Ext.Osiris.RegisterListener, name, arity, "after", function(...)
        local ok2, err2 = pcall(fn, ...)
        if not ok2 then Ext.Utils.PrintWarning("[BG3AI] roll log " .. name .. ": " .. tostring(err2)) end
    end)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] roll log cannot listen to " .. name .. ": " .. tostring(err)) end
end

-- A load reseeds every stream (live1 §4): count from the loaded state.
listen("LevelGameplayStarted", 2, function() L.lastRolls = L.counts() end)

-- RollResult(eventName, roller, rollSubject, resultType, isActiveRoll, criticality)
listen("RollResult", 6, function(event, roller, subject, result, isActive, crit)
    L.add("osi", { roller = V.uuid(roller), subject = V.uuid(subject), event = event,
        success = truthy(result), active = truthy(isActive), crit = CRIT[tonumber(crit)] })
end)

-- DialogRollResult(character, success, dialog, ?, criticality)
listen("DialogRollResult", 5, function(char, success, _, _, crit)
    L.add("dialog", { roller = V.uuid(char), success = truthy(success), active = true, dialog = true,
        crit = CRIT[tonumber(crit)] })
end)

local function entityUuid(h)
    local ok, u = pcall(function() return h.Uuid.EntityUuid end)
    return ok and u or nil
end

-- esv::roll::FinishedEvent (BG3Extender GameDefinitions/Components/Roll.h): the natural die and the DC.
function L.onFinished(component)
    for _, ev in ipairs(component.Events or {}) do
        if not ev.Canceled then
            L.add("finished", { roller = entityUuid(ev.Roller), subject = entityUuid(ev.Subject),
                natural = ev.NaturalRoll, dc = ev.DC, skill = ev.Skill and tostring(ev.Skill) or nil,
                ability = ev.Ability and tostring(ev.Ability) or nil, roll_type = ev.RollType,
                advantage = ev.Advantage or nil, disadvantage = ev.Disadvantage or nil })
        end
    end
end

if not L.finishedSubscribed then
    local ok, err = pcall(Ext.Entity.OnCreate, "ServerRollFinishedEvent", function(entity)
        local okc, c = pcall(function() return entity.ServerRollFinishedEvent end)
        if okc and c then
            local ok2, err2 = pcall(L.onFinished, c)
            if not ok2 then Ext.Utils.PrintWarning("[BG3AI] roll log finished: " .. tostring(err2)) end
        end
    end)
    L.finishedSubscribed = ok
    if not ok then Ext.Utils.PrintWarning("[BG3AI] roll log: no ServerRollFinishedEvent: " .. tostring(err)) end
end

------------------------------------------------------------------------
-- Ops (harness only; never an MCP tool)
------------------------------------------------------------------------

local function view(rec)
    local out = {}
    for k, v in pairs(rec) do out[k] = v end
    local srcs = {}
    for s in pairs(rec.srcs) do srcs[#srcs + 1] = s end
    table.sort(srcs)
    out.srcs = srcs
    return out
end

-- Same cursor contract as ref.since (Server/Referee.lua).
RPC.register("roll.since", function(args)
    local since = tonumber(args.seq) or 0
    local limit = tonumber(args.limit) or 500
    local out = {}
    for _, rec in ipairs(L.ring) do
        if rec.seq > since then
            out[#out + 1] = view(rec)
            if #out >= limit then break end
        end
    end
    return { boot = RPC.BOOT, now = now_ms(), gt = game_s(), latest = L.seq,
        first = L.ring[1] and L.ring[1].seq or L.seq + 1, events = out }
end)

-- Stream states now (rolls drawn, Karmic debts, seeds) and which sources fired.
RPC.register("roll.snapshot", function()
    local r = L.streams()
    local streams = {}
    if r then
        pcall(function()
            for _, s in ipairs(r.Streams) do
                local st = s.State
                streams[#streams + 1] = { key = L.streamKey(s), type = tostring(s.StreamType),
                    num_rolls = st.NumRolls, seed = st.Seed, frame = st.Frame, fail_debt = st.FailDebt,
                    success_debt = st.SuccessDebt }
            end
        end)
    end
    return { boot = RPC.BOOT, now = now_ms(), latest = L.seq, fired = L.fired,
        finished_subscribed = L.finishedSubscribed or false, base_seed = r and r.BaseSeed or nil,
        mode = r and tostring(r.RollModeType) or nil, streams = streams }
end)

RPC.QUIET_OPS["roll.since"] = true

return L
