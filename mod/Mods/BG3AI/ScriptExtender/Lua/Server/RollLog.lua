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
--             system's update (Ext.Entity.OnSystemPostUpdate). Neither path has ever seen one live: live3, live4 and
--             live5 (24 passive checks, research/ovh/live5.md §3) all read hooks finished_create/post_update true,
--             seen.finished_create 0 and fired.finished 0, while requested filled natural/dc on 24 of 24. Where the
--             event lives on this build is not known (not under the ServerRoll system's post-update, or not for
--             these rolls); natural/dc rely on `requested`. The hooks stay so roll.snapshot shows a build that
--             delivers it.
--   requested the roll entity's RequestedRoll component (eoc::RequestedRollComponent: natural, total, DC, roller,
--             subject, skill, ability), tracked from its OnCreate and read once Finished is set (it lives for the
--             roll's delay, so the deferred OnCreate still sees it).
--   osi       Osiris RollResult/6 (fires live: research/ovh/live1.md §4): success, active, criticality only
--   dialog    Osiris DialogRollResult/5 (osi reference arity; not yet seen firing)
--   cast      Osiris CastedSpell/5 of a spell with an attack roll (its SpellRoll has Attack(...)): one record per
--             attack cast (event "attack", roller = the caster, spell), never merged with another source nor batched,
--             so its streams are exactly the draws since the previous record (did the attack draw a die, from
--             which stream). natural nil; its d20 is in cast_roll (below).
--   castroll  the cast's SpellCastRolls component (eoc::spell_cast::RollsComponent: Rolls[i].Target,
--             .Casts[j].Hit.ConditionRolls[k] = RollType, Roll.Result.NaturalRoll/Total/Critical, Difficulty; live
--             window 5 2026-11-03: Ranged Attack 15/20 vs 15, Sacred Flame's save 14/14 vs 15, filled ~100-500 ms
--             after the cast state, before the hit). Every cast state is tracked from its OnCreate and read each
--             frame (its entity can be gone by CastedSpell); at the caster's CastedSpell an attack roll goes into the
--             cast record (cast_roll, the first; cast_rolls when several) and a saving throw into the saver's
--             RollResult record (within CAST_MERGE_MS) or a record of its own (event "save", roller = the saver).
--             cast_roll = {type, natural, total, dc, critical, advantage, disadvantage, ability, skill, target} is
--             kept apart from natural/result/dc on purpose: bg3bench.luck counts every record with a natural, and
--             whether attacks and saves enter the luck covariate is the owner's call (2026-11-03), not this log's.
-- roll.snapshot reports which sources fired at least once, and which hooks are in place.
-- So natural/dc are recorded for passive and skill/ability checks only (live, only their RequestedRoll carried a d20
-- and DC). Saving throws and attack rolls of a spell cast carry theirs in cast_roll (the 2026-10-24 probe passes
-- missed SpellCastRolls: their casts skipped the roll, IgnoreSpellRolls, ops/logs/arena-dice.md "H4").
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
L.CASTS_MAX = 32 -- tracked spell casts (castroll)
L.CAST_MERGE_MS = 3000 -- a save's cast_roll joins the saver's RollResult record this long before the CastedSpell
L.fired = L.fired or { osi = 0, dialog = 0, finished = 0, requested = 0, cast = 0, castroll = 0 }
L.seen = L.seen or { finished_create = 0, finished_empty = 0, requested_create = 0 } -- raw hook hits
L.hooks = L.hooks or {}
L.lastRolls = L.lastRolls or nil -- stream key -> NumRolls at the previous record
L.batch = L.batch or nil -- {recs, total, t0}: the records sharing the last nonzero stream delta
L.pending = L.pending or {} -- roll entities (RequestedRoll) not finished yet
L.lone = L.lone or setmetatable({}, { __mode = "k" }) -- records no other source merges into (cast)
L.casts = L.casts or {} -- {entity, caster, spell, rolls = {"i:j:k" -> cast_roll}, gone}: casts since their state

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
            and not (data.roll_uuid and rec.roll_uuid) and not L.lone[rec] then
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

-- lone: a record of its own (no merge, no batch; its streams are its own draws).
function L.add(src, data, lone)
    local t = now_ms()
    local rec = not lone and (data.roller or data.roll_uuid) and mergeTarget(src, data, t)
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
    if lone then
        L.lone[rec], L.batch = true, nil
        if d then apportion({ recs = { rec }, total = d }) end
        return rec
    end
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
        local ok2, err2 = pcall(U.with_side, nil, fn, ...) -- no side: the records are the harness's, whenever fired
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
-- "None" (AbilityId) and "Sentinel" (SkillId's default, bg3se Hit.h ConditionRoll.Skill: a death or concentration
-- save) name no ability or skill.
local function name(x)
    if x == nil then return nil end
    local s = tostring(x)
    return s ~= "None" and s ~= "Sentinel" and s or nil
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

-- An attack-roll spell: its stats' SpellRoll (Default) calls Attack(...) (as Server/Events.lua's cast event reads it).
function L.attackSpell(spell)
    local ok, r = pcall(function() return tostring(Ext.Stats.Get(spell).SpellRoll.Default or "") end)
    return ok and r:find("Attack%(") ~= nil
end

-- One ConditionRoll of a SpellCastRolls as a cast_roll, or nil before its d20 is in.
local function castRoll(cr, target)
    local ok, v = pcall(function()
        local res, def = cr.Roll.Result, cr.Roll.Roll
        local nat = d20(res.NaturalRoll)
        if not nat then return nil end
        local crit = tostring(res.Critical)
        return { type = tostring(def.RollType), natural = nat, total = tonumber(res.Total),
            dc = positive(cr.Difficulty), critical = crit ~= "None" and crit ~= "nil" and crit or nil,
            advantage = def.Advantage or nil, disadvantage = def.Disadvantage or nil, ability = name(cr.Ability),
            skill = name(cr.Skill), target = target }
    end)
    return ok and v or nil
end

-- Reads a tracked cast's rolls into c.rolls (the latest values win); false once its entity is gone.
local function readCast(c)
    local ok, rolls = pcall(function() return c.entity.SpellCastRolls.Rolls end)
    if not ok or rolls == nil then return false end
    for i, r in ipairs(rolls) do
        local target = entityUuid(r.Target)
        local okc, casts = pcall(function() return r.Casts end)
        for j, cast in ipairs(okc and casts or {}) do
            local okr, conds = pcall(function() return cast.Hit.ConditionRolls end)
            for k, cr in ipairs(okr and conds or {}) do
                local d = castRoll(cr, target)
                if d then c.rolls[string.format("%03d:%03d:%03d", i, j, k)] = d end
            end
        end
    end
    return true
end

function L.pollCasts()
    for _, c in ipairs(L.casts) do
        if not c.gone and not readCast(c) then c.gone = true end
    end
end

-- The caster's tracked casts (of this spell when any is; else all of the caster's), read once more and dropped:
-- their cast_rolls in order.
function L.takeCasts(caster, spell)
    local same = false
    for _, c in ipairs(L.casts) do
        if c.caster == caster and c.spell == spell then same = true end
    end
    local keep, got = {}, {}
    for _, c in ipairs(L.casts) do
        if c.caster == caster and (not same or c.spell == spell) then
            if not c.gone then readCast(c) end
            local keys = {}
            for k in pairs(c.rolls) do keys[#keys + 1] = k end
            table.sort(keys)
            for _, k in ipairs(keys) do got[#got + 1] = c.rolls[k] end
        else
            keep[#keep + 1] = c
        end
    end
    L.casts = keep
    return got
end

-- A saving throw of a cast: into the saver's RollResult record (the oldest within CAST_MERGE_MS without a d20), else
-- a record of its own.
function L.addSave(caster, spell, d)
    local t, found = now_ms(), nil
    for k = #L.ring, math.max(1, #L.ring - L.PENDING_MAX), -1 do
        local rec = L.ring[k]
        if t - rec.t > L.CAST_MERGE_MS then break end
        if rec.roller == d.target and rec.srcs.osi and not rec.srcs.castroll and rec.natural == nil
            and not L.lone[rec] then
            found = rec
        end
    end
    if not found then
        return L.add("castroll", { roller = d.target, subject = caster, event = "save", spell = spell, cast_roll = d })
    end
    L.fired.castroll = (L.fired.castroll or 0) + 1
    found.srcs.castroll, found.cast_roll = true, d
    found.subject, found.spell = found.subject or caster, found.spell or spell
    return found
end

-- CastedSpell(caster, spell, spellType, spellElement, storyActionID)
listen("CastedSpell", 5, function(caster, spell)
    local who = V.uuid(caster)
    local atk, saves = {}, {}
    for _, d in ipairs(L.takeCasts(who, spell)) do
        if d.type:find("Attack") then atk[#atk + 1] = d
        elseif d.type:find("SavingThrow") then saves[#saves + 1] = d end
    end
    if not L.attackSpell(spell) and #atk == 0 and #saves == 0 then return end
    L.pollRequested()
    if L.attackSpell(spell) or #atk > 0 then
        L.add("cast", { roller = who, event = "attack", spell = spell, subject = atk[1] and atk[1].target,
            cast_roll = atk[1], cast_rolls = #atk > 1 and atk or nil }, true)
    end
    for _, d in ipairs(saves) do L.addSave(who, spell, d) end
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
        local ok, err = pcall(U.with_side, nil, fn, ...)
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
hook("cast_create", "SpellCastState OnCreate", function()
    Ext.Entity.OnCreate("SpellCastState", guarded("cast state", function(entity)
        local st = entity.SpellCastState
        local caster = entityUuid(st.Caster)
        if not caster then return end
        local ok, spell = pcall(function() return st.SpellId.Prototype end)
        L.casts[#L.casts + 1] = { entity = entity, caster = caster, spell = ok and spell or nil, rolls = {} }
        while #L.casts > L.CASTS_MAX do table.remove(L.casts, 1) end
    end))
end)
local function frame()
    L.scanFinished()
    L.pollRequested()
    L.pollCasts()
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
    if U.side() then error("roll.since is the harness's (no side)", 0) end -- every die rolled (audit S3-3)
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
    if U.side() then error("roll.snapshot is the harness's (no side)", 0) end
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
