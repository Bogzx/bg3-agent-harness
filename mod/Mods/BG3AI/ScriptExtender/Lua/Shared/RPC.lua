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
RPC.events = RPC.events or {}
RPC.eventSeq = RPC.eventSeq or 0
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
    local ok2, res = pcall(fn, req.args or {})
    if not ok2 then
        return encode({ ok = false, error = tostring(res) })
    end
    return encode({ ok = true, result = res })
end

------------------------------------------------------------------------
-- Events: an append-only ring buffer with monotonically increasing seq.
-- Only player-visible facts may be pushed (see docs/FAIRNESS.md).
------------------------------------------------------------------------

function RPC.push(kind, data)
    RPC.eventSeq = RPC.eventSeq + 1
    local ev = { seq = RPC.eventSeq, t = now_ms(), kind = kind, data = data }
    RPC.events[#RPC.events + 1] = ev
    if #RPC.events > RPC.EVENT_CAP then
        table.remove(RPC.events, 1)
    end
    -- Wake jobs that wait on events.
    for _, job in pairs(RPC.jobs) do
        if job.status == "running" and job.on_event then
            local okj, err = pcall(job.on_event, job, ev)
            if not okj then RPC.finish_job(job, "failed", nil, tostring(err)) end
        end
    end
    return ev.seq
end

RPC.register("events.since", function(args)
    local since = tonumber(args.seq) or 0
    local limit = tonumber(args.limit) or 200
    local out = {}
    for _, ev in ipairs(RPC.events) do
        if ev.seq > since then
            out[#out + 1] = ev
            if #out >= limit then break end
        end
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
    job.status = status
    job.result = result
    job.error = err
    job.finished = now_ms()
    if job.on_finish then pcall(job.on_finish, job) end
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

RPC.register("job.poll", function(args)
    local job = RPC.jobs[tonumber(args.id)]
    if not job then error("no such job " .. tostring(args.id)) end
    return job_view(job)
end)

-- A waited-on job that outlived the bridge's wait: announce its end as job_finished from now on, as if it had been
-- started with notify (audit ACT-10: its outcome was invisible). The view says whether it already ended.
RPC.register("job.notify", function(args)
    local job = RPC.jobs[tonumber(args.id)]
    if not job then error("no such job " .. tostring(args.id)) end
    if job.status == "running" then job.notify = true end
    return job_view(job)
end)

RPC.register("job.cancel", function(args)
    local job = RPC.jobs[tonumber(args.id)]
    if not job then error("no such job " .. tostring(args.id)) end
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
            if job.on_tick then
                local ok, err = pcall(job.on_tick, job)
                if not ok then RPC.finish_job(job, "failed", nil, tostring(err)) end
            end
            if job.status == "running" and t > job.deadline then
                if job.on_timeout then pcall(job.on_timeout, job) end
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
