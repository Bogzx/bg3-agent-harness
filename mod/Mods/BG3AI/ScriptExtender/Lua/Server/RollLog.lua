-- Hidden roll log (server context): every die roll the game makes, for the benchmark's luck covariate (D9).
--
-- Never agent-visible: nothing here calls RPC.push; only the harness reads it through roll.since / roll.snapshot
-- (bridge/src/bg3ai/referee.py; no MCP tool calls them, see bridge/tests/test_referee.py). The fair, filtered
-- roll_result event the agent sees stays in Server/Events.lua.
--
-- One record per roll: {seq, t (monotonic ms), gt, srcs, roller, party, success, active, crit, natural, result
-- (the total), dc, stream, streams = {<stream> = rolls drawn}, ...}. Sources, merged into one record when they
-- describe the same roll (same roll uuid, else the oldest record of the same roller within MERGE_MS that lacks
-- the source). natural/result/dc come from the first of these that fired:
--   finished  the ServerRollFinishedEvent one-frame component (natural, DC, skill, ability, advantage; no total).
--             OnCreate never delivered it live (research/ovh/live3.md §4): bg3le queues create events to the next
--             tick, when a one-frame component is gone. So it is also read inside the frame, after the ServerRoll
--             system's update (Ext.Entity.OnSystemPostUpdate).
--   requested the roll entity's RequestedRoll component (eoc::RequestedRollComponent: natural, total, DC, roller,
--             subject, skill, ability), tracked from its OnCreate and read once Finished is set (it lives for the
--             roll's delay, so the deferred OnCreate still sees it).
--   osi       Osiris RollResult/6 (fires live: research/ovh/live1.md §4): success, active, criticality only
--   dialog    Osiris DialogRollResult/5 (osi reference arity; not yet seen firing)
-- roll.snapshot reports which sources fired at least once, and which hooks are in place.
-- Streams: the stream counts are read at each new record; rolls the engine made in one frame arrive as one batch of
-- records (24 passive checks -> 24 RollResults in a row), so a batch's draws are shared out over its records
-- (streams_shared = batch size when > 1) instead of all landing on the first.

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
L.PENDING_MAX = 64
L.fired = L.fired or { osi = 0, dialog = 0, finished = 0, requested = 0 }
L.seen = L.seen or { finished_create = 0, finished_empty = 0, requested_create = 0 } -- raw hook hits
L.hooks = L.hooks or {}
L.lastRolls = L.lastRolls or nil -- stream key -> NumRolls at the previous record
L.batch = L.batch or nil -- {recs, total, t0}: the records sharing the last nonzero stream delta
L.pending = L.pending or {} -- roll entities (RequestedRoll) not finished yet

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

-- The record of another source for the same roll: the same roll uuid, else the oldest record of the same roller
-- within MERGE_MS that lacks this source (oldest first, so a batch of rolls pairs up in order).
local function mergeTarget(src, data, t)
    local found
    for k = #L.ring, math.max(1, #L.ring - L.PENDING_MAX), -1 do
        local rec = L.ring[k]
        if data.roll_uuid and rec.roll_uuid == data.roll_uuid then
            return rec
        end
        if t - rec.t <= L.MERGE_MS and rec.roller == data.roller and not rec.srcs[src]
            and not (data.roll_uuid and rec.roll_uuid) then
            found = rec
        end
    end
    return found
end

-- The batch's draws shared out over its records: per stream, total // n each and the remainder to the earliest.
-- A record's stream is the one it drew most from.
local function apportion(batch)
    local n = #batch.recs
    for i, rec in ipairs(batch.recs) do
        local share, best = {}, nil
        for k, total in pairs(batch.total) do
            local d = total // n + (i <= total % n and 1 or 0)
            if d > 0 then
                share[k] = d
                if not best or d > share[best] or (d == share[best] and k < best) then best = k end
            end
        end
        rec.streams, rec.stream = next(share) and share or nil, best
        rec.streams_shared = n > 1 and n or nil
    end
end

local function addDrawn(rec, d)
    local b = L.batch
    for _, r in ipairs(b and b.recs or {}) do
        if r == rec then
            for k, n in pairs(d) do b.total[k] = (b.total[k] or 0) + n end
            return apportion(b)
        end
    end
    rec.streams = rec.streams or {}
    for k, n in pairs(d) do rec.streams[k] = (rec.streams[k] or 0) + n end
end

function L.add(src, data)
    local t = now_ms()
    local rec = (data.roller or data.roll_uuid) and mergeTarget(src, data, t)
    if rec and rec.srcs[src] then return rec end -- the same roll seen again (a one-frame read twice)
    L.fired[src] = (L.fired[src] or 0) + 1
    local d = drawn()
    if rec then
        for k, v in pairs(data) do
            if rec[k] == nil then rec[k] = v end
        end
        rec.srcs[src] = true
        if d then addDrawn(rec, d) end
        return rec
    end
    L.seq = L.seq + 1
    rec = data
    rec.seq, rec.t, rec.gt, rec.srcs = L.seq, t, game_s(), { [src] = true }
    rec.party = isParty(data.roller)
    L.ring[#L.ring + 1] = rec
    while #L.ring > L.CAP do table.remove(L.ring, 1) end
    if d then
        L.batch = { recs = { rec }, total = d, t0 = t }
    elseif L.batch and t - L.batch.t0 <= L.MERGE_MS then
        L.batch.recs[#L.batch.recs + 1] = rec
    else
        L.batch = nil
    end
    if L.batch then apportion(L.batch) end
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

local function entityUuid(h)
    local ok, u = pcall(function() return h.Uuid.EntityUuid end)
    return ok and u or nil
end

local function d20(n)
    n = tonumber(n)
    return n and n >= 1 and n <= 20 and n or nil
end
local function positive(n)
    n = tonumber(n)
    return n and n > 0 and n or nil
end
local function name(x)
    if x == nil then return nil end
    local s = tostring(x)
    return s ~= "None" and s or nil
end

-- RequestedRoll entities (the roll in progress): kept from their OnCreate until Finished or gone.
local function requestedData(c)
    local res, roll = c.Result or {}, c.Roll or {}
    return { roller = entityUuid(c.Roller) or c.EntityUuid, subject = entityUuid(c.Subject),
        roll_uuid = c.RollUuid, natural = d20(res.NaturalRoll) or d20(c.NaturalRoll), result = tonumber(res.Total),
        dc = positive(c.DC), skill = name(c.Skill), ability = name(c.Ability),
        advantage = roll.Advantage or nil, disadvantage = roll.Disadvantage or nil }
end

function L.pollRequested()
    if #L.pending == 0 then return end
    local keep = {}
    for _, e in ipairs(L.pending) do
        local ok, c = pcall(function() return e.RequestedRoll end)
        if ok and c then
            if c.Canceled then -- not a roll
            elseif c.Finished then L.add("requested", requestedData(c))
            else keep[#keep + 1] = e end
        end
    end
    L.pending = keep
end

-- RollResult(eventName, roller, rollSubject, resultType, isActiveRoll, criticality)
listen("RollResult", 6, function(event, roller, subject, result, isActive, crit)
    L.pollRequested()
    L.add("osi", { roller = V.uuid(roller), subject = V.uuid(subject), event = event,
        success = truthy(result), active = truthy(isActive), crit = CRIT[tonumber(crit)] })
end)

-- DialogRollResult(character, success, dialog, ?, criticality)
listen("DialogRollResult", 5, function(char, success, _, _, crit)
    L.pollRequested()
    L.add("dialog", { roller = V.uuid(char), success = truthy(success), active = true, dialog = true,
        crit = CRIT[tonumber(crit)] })
end)

-- esv::roll::FinishedEvent (BG3Extender GameDefinitions/Components/Roll.h): the natural die and the DC.
function L.onFinished(component)
    for _, ev in ipairs(component.Events or {}) do
        if not ev.Canceled then
            L.add("finished", { roller = entityUuid(ev.Roller), subject = entityUuid(ev.Subject),
                roll_uuid = ev.RollUuid, natural = d20(ev.NaturalRoll), dc = positive(ev.DC),
                skill = name(ev.Skill), ability = name(ev.Ability), roll_type = ev.RollType,
                advantage = ev.Advantage or nil, disadvantage = ev.Disadvantage or nil })
        end
    end
end

-- The one-frame components alive in this frame (read right after the system that makes them).
function L.scanFinished()
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "ServerRollFinishedEvent")
    for _, e in ipairs(ok and list or {}) do
        local okc, c = pcall(function() return e.ServerRollFinishedEvent end)
        if okc and c then L.onFinished(c) end
    end
end

local function guarded(what, fn)
    return function(...)
        local ok, err = pcall(fn, ...)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] roll log " .. what .. ": " .. tostring(err)) end
    end
end

local function hook(key, what, fn)
    if L.hooks[key] ~= nil then return end
    local ok, err = pcall(fn)
    L.hooks[key] = ok
    if not ok then Ext.Utils.PrintWarning("[BG3AI] roll log: no " .. what .. ": " .. tostring(err)) end
end

hook("finished_create", "ServerRollFinishedEvent OnCreate", function()
    Ext.Entity.OnCreate("ServerRollFinishedEvent", guarded("finished", function(entity)
        L.seen.finished_create = L.seen.finished_create + 1
        local okc, c = pcall(function() return entity.ServerRollFinishedEvent end)
        if okc and c then L.onFinished(c) else L.seen.finished_empty = L.seen.finished_empty + 1 end
    end))
end)
hook("requested_create", "RequestedRoll OnCreate", function()
    Ext.Entity.OnCreate("RequestedRoll", guarded("requested", function(entity)
        L.seen.requested_create = L.seen.requested_create + 1
        L.pending[#L.pending + 1] = entity
        while #L.pending > L.PENDING_MAX do table.remove(L.pending, 1) end
    end))
end)
local function frame()
    L.scanFinished()
    L.pollRequested()
end
hook("post_update", "ServerRoll post-update hook", function()
    Ext.Entity.OnSystemPostUpdate("ServerRoll", guarded("post-update", frame))
end)
if L.hooks.post_update == false then
    hook("tick", "Tick", function() Ext.Events.Tick:Subscribe(guarded("tick", frame)) end)
end
L.finishedSubscribed = L.hooks.finished_create or L.hooks.post_update or false

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
        finished_subscribed = L.finishedSubscribed, hooks = L.hooks, seen = L.seen,
        pending = #L.pending, base_seed = r and r.BaseSeed or nil,
        mode = r and tostring(r.RollModeType) or nil, streams = streams }
end)

RPC.QUIET_OPS["roll.since"] = true

return L
