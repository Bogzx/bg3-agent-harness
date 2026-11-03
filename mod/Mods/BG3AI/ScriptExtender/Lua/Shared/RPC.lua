-- BG3AI RPC core, loaded in both the server and the client Lua context.
--
-- The external bridge reaches the game through bg3le's (or bg3se's) LuaDebug
-- evaluate channel and only ever sends one expression:
--
--     return BG3AI.rpc([==[{"op":"state.status","args":{}}]==])
--
-- and gets back a JSON string: {"ok":true,"result":...} or {"ok":false,"error":"..."}.
-- Everything else lives here: an op registry, a job table for long-running
-- actions, and an event ring buffer the bridge drains with "events.since".
--
-- Ops must return quickly (the debugger waits at most ~10 s for the game
-- thread). Anything that takes game time starts a job and returns its id.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC or {}
BG3AI.RPC = RPC

RPC.ops = RPC.ops or {}
RPC.jobs = RPC.jobs or {}
RPC.nextJob = RPC.nextJob or 1
RPC.events = RPC.events or {} -- keyed by seq: events[eventFirst .. eventSeq], at most EVENT_CAP of them
RPC.eventSeq = RPC.eventSeq or 0
if not RPC.eventFirst then
    -- first load, or this file re-run over the older array ring (events[1] was the oldest): key it by seq
    local keyed = {}
    for _, ev in ipairs(RPC.events) do keyed[ev.seq] = ev end
    RPC.events, RPC.eventFirst = keyed, RPC.events[1] and RPC.events[1].seq or RPC.eventSeq + 1
end
RPC.EVENT_CAP = 4000
RPC.JOB_CAP = 200

local CONTEXT = Ext.IsServer() and "server" or "client"
RPC.context = CONTEXT

local function now_ms()
    return Ext.Utils.MonotonicTime()
end
RPC.now_ms = now_ms

local function encode(value)
    local ok, out = pcall(Ext.Json.Stringify, value, { Beautify = false, StringifyInternalTypes = true })
    if ok then return out end
    return Ext.Json.Stringify({ ok = false, error = "encode failed: " .. tostring(out) })
end

-- register(name, fn): fn(args) -> result (any JSON-able value). Errors become
-- {"ok":false}. Re-registering replaces the op, so Ext.Debug.Reset() and
-- re-running a file are both safe.
function RPC.register(name, fn)
    RPC.ops[name] = fn
end

-- Polls and cheap reads: no breadcrumb (they run several times a second).
RPC.QUIET_OPS = {
    ["events.since"] = true, ["job.poll"] = true, ["ui.state"] = true, ["state.status"] = true,
    ["state.nearby"] = true, ["nav.markers"] = true, ["nav.activity"] = true, ["sys.ping"] = true,
    ["sys.info"] = true, ["sys.mem"] = true, ["batch"] = true,
}

------------------------------------------------------------------------
-- Sides (two teams in one fight, each driven by its own bridge). Inert unless BG3AI.Arena says an arena is active:
-- RPC.side() is nil then and every branch below is today's path. A bridge started for a side sends "side" in the
-- request envelope; Ctx.side holds it for that op (and for its jobs' callbacks, and for each listener run per side)
-- and is cleared after, error or not.
------------------------------------------------------------------------

RPC.Ctx = RPC.Ctx or {}
RPC.SIDES = { "A", "B" }
RPC.eventSide = RPC.eventSide or {} -- seq -> "A" | "B" | false (no side: the harness's), only while an arena is active
RPC.sideInputAt = RPC.sideInputAt or {} -- side -> when its last CAMERA_INPUT_OPS op began (arena.camera)
-- A click's steps that go through the camera (Client/Pick.lua: project, check the hover): arena.camera doesn't move the
-- view right after one. Reads (ui.story_timer, ui.selected, ...) aren't input: counting them kept the camera from ever
-- moving in a match (live window 6: each get_combat_state's ui.story_timer, every ~2 s per side).
RPC.CAMERA_INPUT_OPS = { ["ui.screen_pos"] = true, ["ui.hover_check"] = true, ["ui.ground_check"] = true }

function RPC.arena_active()
    local Ar = BG3AI.Arena
    return Ar ~= nil and Ar.active() == true
end

-- The side of the running op/job/listener run, nil without an active arena.
function RPC.side()
    if RPC.arena_active() then return RPC.Ctx.side end
    return nil
end

-- fn(...) with Ctx.side = side, the previous side restored after, error or not (the error goes on).
function RPC.with_side(side, fn, ...)
    local prev = RPC.Ctx.side
    RPC.Ctx.side = side
    local r = table.pack(pcall(fn, ...))
    RPC.Ctx.side = prev
    if not r[1] then error(r[2], 0) end
    return table.unpack(r, 2, r.n)
end

-- Listener-time code runs once with no side (today; the harness's view) and, in an arena, once more per side, so
-- each side's reads use its own team. Every run is attempted; the first error goes on after them.
function RPC.each_side(fn, ...)
    local first
    local runs = RPC.arena_active() and { false, "A", "B" } or { false }
    for _, s in ipairs(runs) do
        local ok, err = pcall(RPC.with_side, s or nil, fn, ...)
        if not ok and first == nil then first = err end
    end
    if first ~= nil then error(first, 0) end
end

-- A per-session memory: owner[key] with no side; a table of its own per side (created empty).
RPC.sidedOwners = RPC.sidedOwners or {}
function RPC.sided(owner, key)
    local s = RPC.side()
    if not s then return owner[key] end
    local v = RPC.sget(owner, key)
    if v == nil then
        v = {}
        RPC.sset(owner, key, v)
    end
    return v
end

-- A per-session value (a table that gets replaced, a counter): owner[key] with no side, else the side's own.
function RPC.sget(owner, key)
    local s = RPC.side()
    if not s then return owner[key] end
    local t = owner._sides and owner._sides[s]
    return t and t[key]
end

function RPC.sset(owner, key, v)
    local s = RPC.side()
    if not s then
        owner[key] = v
        return
    end
    RPC.sidedOwners[owner] = true
    owner._sides = owner._sides or {}
    owner._sides[s] = owner._sides[s] or {}
    owner._sides[s][key] = v
end

-- The end of an arena: every side's memories, side-tagged events and side marks on jobs go.
function RPC.clear_sides()
    for owner in pairs(RPC.sidedOwners) do owner._sides = nil end
    RPC.sidedOwners = {}
    for seq, tag in pairs(RPC.eventSide) do
        if tag then RPC.events[seq] = nil end
    end
    RPC.eventSide = {}
end

-- The busy answer says which: the bridge (busywait.py BEFORE_MARK) asks any op again when it reads BUSY_BEFORE.
RPC.BUSY_BEFORE = "refused before its first UI call, nothing was done"
RPC.BUSY_AFTER = "met it after a UI call ran"

function BG3AI.rpc(json)
    local ok, req = pcall(Ext.Json.Parse, json)
    if not ok or type(req) ~= "table" then
        return encode({ ok = false, error = "bad request: " .. tostring(req) })
    end
    local fn = RPC.ops[req.op or ""]
    if not fn then
        return encode({ ok = false, error = "unknown op '" .. tostring(req.op) .. "' in " .. CONTEXT .. " context" })
    end
    -- Breadcrumb (Shared/Mem.lua): a call that never returns (S038 20:48 OOM) leaves its name as the last line.
    local Mem = BG3AI.Mem
    if Mem and Mem.crumb and not RPC.QUIET_OPS[req.op] then
        local a = type(req.args) == "table" and req.args or {}
        local p = type(a.pos) == "table" and a.pos or nil
        Mem.crumb(req.op .. (p and string.format(" (%s,%s,%s)", tostring(p[1]), tostring(p[2]), tostring(p[3])) or "")
            .. (a.target and (" " .. tostring(a.target)) or ""))
    end
    local UI = BG3AI.UI
    local hits, calls, job0 = UI and UI.busyHits, UI and UI.calls, RPC.nextJob
    local side = (req.side == "A" or req.side == "B") and req.side or nil
    if side and RPC.CAMERA_INPUT_OPS[req.op] then RPC.sideInputAt[side] = now_ms() end
    local ok2, res
    if side then
        ok2, res = pcall(RPC.with_side, side, fn, req.args or {})
    else
        ok2, res = pcall(fn, req.args or {})
    end
    if hits and UI.busyHits ~= hits then
        -- a UI read met the busy UI (Client/UI.lua UI.busy): no answer built from refused reads; a job the op
        -- started on them is dropped
        for id = job0, RPC.nextJob - 1 do
            local j = RPC.jobs[id]
            if j then RPC.finish_job(j, "interrupted", nil, UI.BUSY_ERR) end
        end
        -- refused at the gate of its first UI call and that one refusal ended the op: nothing read, nothing pressed,
        -- so any op may be asked again (the bridge's busywait.py does). After a call ran, or when the op caught a
        -- refusal and went on (a raw Ext.Input press isn't gated), a press may have happened before this answer.
        local before = UI.calls == calls and UI.busyHits == hits + 1 and not ok2
            and tostring(res):find(UI.BUSY_ERR, 1, true) ~= nil
        return encode({ ok = false, error = UI.BUSY_ERR .. ": the game's UI is busy (a rest/travel/load transition); "
            .. (before and RPC.BUSY_BEFORE or RPC.BUSY_AFTER) .. "; retry in a moment" })
    end
    if not ok2 then
        return encode({ ok = false, error = tostring(res) })
    end
    return encode({ ok = true, result = res })
end

------------------------------------------------------------------------
-- Events: an append-only ring buffer with monotonically increasing seq.
-- Only player-visible facts may be pushed (see docs/FAIRNESS.md).
-- Keyed by seq, so a push and events.since cost O(1) and O(limit) on the game thread (shifting a full array ring
-- cost 41 us per push, a 100-event burst ~4 ms of a frame).
------------------------------------------------------------------------

function RPC.push(kind, data)
    RPC.eventSeq = RPC.eventSeq + 1
    local ev = { seq = RPC.eventSeq, t = now_ms(), kind = kind, data = data }
    RPC.events[ev.seq] = ev
    -- In an arena an event belongs to the side whose op/job/listener run pushed it (false: the harness's run)
    local tag
    if RPC.arena_active() then
        tag = RPC.Ctx.side or false
        RPC.eventSide[ev.seq] = tag
    end
    while RPC.eventSeq - RPC.eventFirst >= RPC.EVENT_CAP do
        RPC.events[RPC.eventFirst] = nil
        RPC.eventSide[RPC.eventFirst] = nil
        RPC.eventFirst = RPC.eventFirst + 1
    end
    -- Wake jobs that wait on events (in an arena: a job hears its own side's events only, as that side).
    for _, job in pairs(RPC.jobs) do
        if job.status == "running" and job.on_event and (tag == nil or tag == (job.side or false)) then
            local okj, err
            if tag == nil then
                okj, err = pcall(job.on_event, job, ev)
            else
                okj, err = pcall(RPC.with_side, job.side, job.on_event, job, ev)
            end
            if not okj then RPC.finish_job(job, "failed", nil, tostring(err)) end
        end
    end
    return ev.seq
end

RPC.register("events.since", function(args)
    local since = tonumber(args.seq) or 0
    local limit = tonumber(args.limit) or 200
    local out = {}
    local first = math.max(math.floor(since) + 1, RPC.eventFirst)
    if RPC.arena_active() then
        -- a side reads its own events; no side (the harness) reads the no-side run's and those from before the arena
        local want = RPC.side() or false
        for seq = first, RPC.eventSeq do
            if #out >= limit then break end
            local tag = RPC.eventSide[seq]
            if tag == want or (tag == nil and not want) then out[#out + 1] = RPC.events[seq] end
        end
        return { latest = RPC.eventSeq, events = out }
    end
    for seq = first, math.min(RPC.eventSeq, first + limit - 1) do
        out[#out + 1] = RPC.events[seq]
    end
    return { latest = RPC.eventSeq, events = out }
end)

------------------------------------------------------------------------
-- Jobs: long-running actions. A job is a table:
--   { id, kind, status = "running"|"done"|"failed"|"interrupted"|"timeout",
--     started, deadline, result, error, on_event(job, ev), on_tick(job) }
-- The bridge polls "job.poll" until status ~= "running".
------------------------------------------------------------------------

function RPC.start_job(kind, spec)
    local id = RPC.nextJob
    RPC.nextJob = id + 1
    local job = spec or {}
    job.id = id
    job.kind = kind
    job.status = "running"
    job.started = now_ms()
    job.deadline = job.started + (job.timeout_ms or 20000)
    job.side = RPC.side() -- its callbacks run as this side (tick_jobs, RPC.push, finish_job)
    RPC.jobs[id] = job
    -- Bound the table: drop the oldest finished jobs.
    local count = 0
    for _ in pairs(RPC.jobs) do count = count + 1 end
    if count > RPC.JOB_CAP then
        local oldest
        for jid, j in pairs(RPC.jobs) do
            if j.status ~= "running" and (oldest == nil or jid < oldest) then oldest = jid end
        end
        if oldest then RPC.jobs[oldest] = nil end
    end
    return job
end

function RPC.finish_job(job, status, result, err)
    if job.status ~= "running" then return end
    -- its on_tick met the busy UI this tick: whatever it concluded came from refused reads; it ticks again
    local tk = RPC.ticking
    if tk and tk.job == job and BG3AI.UI and BG3AI.UI.busyHits ~= tk.hits then return end
    job.status = status
    job.result = result
    job.error = err
    job.finished = now_ms()
    if job.on_finish then
        if job.side then pcall(RPC.with_side, job.side, job.on_finish, job) else pcall(job.on_finish, job) end
    end
    -- Push: bg3le sends every Lua print to each attached debugger, so the bridge's listener wakes the
    -- waiting call at once instead of polling job.poll every 0.1 s (~45 polls per combat_turn, S022-S026).
    pcall(Ext.Utils.Print, "BG3AI.job " .. CONTEXT .. " " .. tostring(job.id) .. " " .. tostring(status))
end

local function job_view(job)
    return {
        id = job.id, kind = job.kind, status = job.status,
        result = job.result, error = job.error,
        elapsed_ms = (job.finished or now_ms()) - job.started,
    }
end

-- The job by id; with a side, only that side's (a job started without a side is no side's).
local function own_job(id)
    local job = RPC.jobs[tonumber(id)]
    if not job then error("no such job " .. tostring(id)) end
    local side = RPC.side()
    if side and job.side ~= side then error("not your job", 0) end
    return job
end

RPC.register("job.poll", function(args)
    return job_view(own_job(args.id))
end)

-- A waited-on job that outlived the bridge's wait: announce its end as job_finished from now on, as if it had been
-- started with notify (audit ACT-10: its outcome was invisible). The view says whether it already ended.
RPC.register("job.notify", function(args)
    local job = own_job(args.id)
    if job.status == "running" then job.notify = true end
    return job_view(job)
end)

RPC.register("job.cancel", function(args)
    local job = own_job(args.id)
    if job.on_cancel then pcall(job.on_cancel, job) end
    RPC.finish_job(job, "interrupted", nil, "cancelled")
    return job_view(job)
end)

-- Tick running jobs (timeouts and polled conditions). Real-time based so it
-- keeps running while the game is paused.
local function tick_jobs()
    local t = now_ms()
    for _, job in pairs(RPC.jobs) do
        if job.status == "running" then
            -- S086 hang (Client/UI.lua UI.busy): while the UI is busy no job touches it; one that met it this tick
            -- stays running (no verdict, no failure from the busy error) and ticks again once the UI is free
            local UI = BG3AI.UI
            if job.on_tick and not (UI and UI.busy and UI.busy()) then
                local tk = { job = job, hits = UI and UI.busyHits }
                RPC.ticking = tk
                local ok, err
                if job.side then
                    ok, err = pcall(RPC.with_side, job.side, job.on_tick, job)
                else
                    ok, err = pcall(job.on_tick, job)
                end
                RPC.ticking = nil
                if UI and UI.busyHits ~= tk.hits then
                    job.busy_ticks = (job.busy_ticks or 0) + 1
                elseif not ok then
                    RPC.finish_job(job, "failed", nil, tostring(err))
                end
            end
            if job.status == "running" and t > job.deadline then
                if job.on_timeout then
                    if job.side then
                        pcall(RPC.with_side, job.side, job.on_timeout, job)
                    else
                        pcall(job.on_timeout, job)
                    end
                end
                RPC.finish_job(job, "timeout", job.partial, "timed out")
            end
        end
    end
end

if not RPC.tickSubscribed then
    RPC.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(tick_jobs)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] job tick: " .. tostring(err)) end
    end)
end

------------------------------------------------------------------------
-- System ops
------------------------------------------------------------------------

-- A new id per Lua state: a save load rebuilds the context, so load_game can tell that a load really happened.
local BOOT = string.format("%d-%d", math.floor(now_ms()), math.random(1, 1000000000))
RPC.BOOT = BOOT

RPC.register("sys.ping", function()
    return { context = CONTEXT, t = now_ms(), version = BG3AI.VERSION, boot = BOOT }
end)

RPC.register("sys.info", function()
    local ops = {}
    for name in pairs(RPC.ops) do ops[#ops + 1] = name end
    table.sort(ops)
    return {
        context = CONTEXT,
        version = BG3AI.VERSION,
        game_version = Ext.Utils.GameVersion and Ext.Utils.GameVersion() or nil,
        extender_version = Ext.Utils.Version(),
        game_state = tostring(Ext.Utils.GetGameState and Ext.Utils.GetGameState() or "?"),
        ops = ops,
        event_seq = RPC.eventSeq,
    }
end)

return RPC
