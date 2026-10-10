-- BG3AI action layer (server context).
--
-- Every act.* op validates through BG3AI.Guard, starts a job (RPC.start_job)
-- and returns {job=id}. Jobs complete from Osiris listeners (registered once
-- per Lua state, see listen()) or from on_tick post-condition checks, and
-- always end as done / failed / interrupted / timeout.
--
-- Interrupts: jobs flagged interrupt.combat / .dialogue / .attacked finish
-- "interrupted" when a party member enters combat, a party member joins a
-- dialogue, or the acting character is attacked.
--
-- One action per character: starting a new action on a character supersedes
-- (interrupts) the previous one and purges its Osiris task queue.
--
-- Leftover tasks: Osiris tasks outlive the job that queued them (and are
-- saved with the game). A ladder Use task never ends on its own: it keeps
-- re-climbing (live, S001: "used Arterial Mesh" every few seconds, all other
-- orders queued behind it, still looping after a reload). So a job that
-- queued tasks purges the character's queue when it ends (see start()), and
-- the loop watchdog (A.loop_watch) stops a character that keeps using the
-- same item while a task drives it.
--
-- Cast path: guarded Osi.UseSpell / UseSpellAtPosition with full UI-style
-- pre-validation (G.check_cast) and a post-cast resource audit that makes the
-- cast pay exactly what a UI cast pays. See research/impl-act.md for why the
-- engine CastStartRequest path is not used yet.

BG3AI = BG3AI or {}
local U = BG3AI.Util
local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = {}
BG3AI.Actions = A

A.active = {}   -- char guid -> running job

local TICK_MS = 50   -- post-condition polling; cheap queries, keeps chaining fast
A.MOVE_REACH = 2.2   -- move_to {target}: stop within this of the entity (centres; melee reach is
                     -- weapon 1.5 + both radii, >= 2.2 m even for a tiny target, see G.check_cast)
A.END_REQUEST_AFTER_MS = 1500 -- end_turn in a shared group: report the request as done after this
A.MELEE_STOP_SLACK = 0.4 -- move_to {target, within}: stop this far inside the melee reach
A.USE_REACH = 1.8    -- interact/pickup and the click ops: Use/Pickup/click from here

------------------------------------------------------------------------
-- Osiris listeners: one registration per event per Lua state, dispatching to
-- running jobs' job.osi[event](job, args) handlers and to the interrupt rules.
------------------------------------------------------------------------

local registered = {}

local function each_running(fn)
    for _, job in pairs(RPC.jobs) do
        if job.status == "running" and job.act then
            local ok, err = pcall(fn, job)
            if not ok then RPC.finish_job(job, "failed", nil, tostring(err)) end
        end
    end
end

local function interrupt(job, why)
    if job.status ~= "running" then return end
    job.stopping = true -- purging fires CharacterMoveToCancelled synchronously
    if job.char then pcall(G.osi, "PurgeOsirisQueue", job.char) end
    RPC.finish_job(job, "interrupted", job.partial, why)
end

-- Interrupt rules, evaluated for every dispatched event.
local INTERRUPTS = {
    EnteredCombat = function(job, a)
        if job.interrupt.combat and G.is_party(a[1]) then
            interrupt(job, "combat started")
        end
    end,
    DialogActorJoined = function(job, a)
        -- Ambient barks (automated dialogs) don't stop a player's walk.
        if job.interrupt.dialogue and G.is_party(a[3])
            and not G.b(G.q("DialogIsAutomated", a[1])) then
            interrupt(job, "a conversation started")
        end
    end,
    -- AttackedBy(defender, attackerOwner, attacker, damageType, amount, cause, storyActionId).
    -- A 0-damage puff from a disarmed trap object (Grand Mausoleum vents: Blinded, source "Someone",
    -- live 11:40) doesn't stop the walk; any hit that hurts, or anything from a character (a
    -- hostile's miss too), does.
    AttackedBy = function(job, a)
        if job.interrupt.attacked and G.same(a[1], job.char)
            and ((tonumber(a[5]) or 0) > 0 or A.is_character(a[3]) or A.is_character(a[2])) then
            interrupt(job, "attacked")
        end
    end,
}

function A.is_character(u)
    local g = G.guid(u)
    return g ~= nil and G.b(G.q("IsCharacter", g)) == true
end

-- Number of queued Osiris tasks on a character (0 if unknown).
local function osi_tasks(char)
    local ok, n = pcall(function() return #Ext.Entity.Get(char).ServerCharacter.OsirisController.Tasks end)
    return ok and n or 0
end
A.osi_tasks = osi_tasks

-- Loop watchdog: the same item used LOOP_USES times, each within LOOP_GAP_MS
-- of the last, while Osiris tasks still drive the character, is an engine
-- task looping (a ladder Use re-climbing, or a route through a ladder that
-- never arrives). It stops the character's job, or purges the orphaned
-- tasks when no job runs (e.g. tasks restored from a save).
A.LOOP_USES, A.LOOP_GAP_MS = 3, 20000
A.use_streak = {}   -- char guid -> {item, n, t}

function A.loop_watch(char, item)
    char, item = G.guid(char), G.guid(item)
    if not char or not item or not G.is_party(char) then return end
    local t = RPC.now_ms()
    local s = A.use_streak[char]
    if s and s.item == item and t - s.t <= A.LOOP_GAP_MS then
        s.n, s.t = s.n + 1, t
    else
        s = { item = item, n = 1, t = t }
        A.use_streak[char] = s
    end
    if s.n < A.LOOP_USES or osi_tasks(char) == 0 then return end
    local job = A.active[char]
    -- One ladder climb fires several UseFinished (live 14:1x: interrupted mid-climb). A ladder interact awaiting
    -- its climb has its own deadline (A.LADDER_CLIMB_MS, then failed + purge), so it is not a loop yet.
    if job and job.status == "running" and job.ladder and G.same(job.ladder.item, item) then return end
    A.use_streak[char] = nil
    RPC.push("loop_stopped", { uuid = char, item = item, uses = s.n, job = job and job.id or nil })
    if job and job.status == "running" then
        interrupt(job, "stuck: the character kept using the same item (" .. s.n
            .. " times, e.g. re-climbing a ladder); try another route or a jump")
    else
        pcall(G.osi, "PurgeOsirisQueue", char)
    end
end

function A.dispatch(event, args)
    if event == "TurnEnded" and args[1] then
        local g = G.guid(args[1]) or args[1]
        if g then G.end_requested[g] = nil end
    end
    if event == "UseFinished" then
        local ok, err = pcall(A.loop_watch, args[1], args[2])
        if not ok then Ext.Utils.PrintWarning("[BG3AI] loop_watch: " .. tostring(err)) end
    end
    each_running(function(job)
        local rule = INTERRUPTS[event]
        if rule and job.interrupt then rule(job, args) end
        if job.status == "running" and job.osi and job.osi[event] then
            job.osi[event](job, args)
        end
    end)
end

local function listen(event, arity)
    if registered[event] then return end
    registered[event] = true
    local ok, err = pcall(Ext.Osiris.RegisterListener, event, arity, "after", function(...)
        local args = { ... }
        local ok2, err2 = pcall(A.dispatch, event, args)
        if not ok2 then Ext.Utils.PrintWarning("[BG3AI] act dispatch " .. event .. ": " .. tostring(err2)) end
    end)
    if not ok then
        registered[event] = nil
        Ext.Utils.PrintWarning("[BG3AI] cannot listen to " .. event .. ": " .. tostring(err))
    end
end

-- Arities checked against the shipped story goals (e.g. UseFinished(_, _Item, 1)).
for event, arity in pairs({
    EntityEvent = 2, CharacterMoveToCancelled = 2, CharacterMoveFailedUseJump = 1,
    UseFinished = 3, Opened = 1, Closed = 1,
    DialogStarted = 2, DialogActorJoined = 4, DialogRequestFailed = 2,
    CastedSpell = 5, CastSpellFailed = 5,
    AttackedBy = 7, EnteredCombat = 2, TurnEnded = 1,
    PickupFailed = 2, AddedTo = 3,
    Equipped = 2, EquipFailed = 2, Unequipped = 2, UnequipFailed = 2,
    CharacterSelectedForUser = 2, AttachedToPartyGroup = 1, DetachedFromPartyGroup = 1,
    TeleportedToCamp = 1, TeleportedFromCamp = 1,
    EnteredForceTurnBased = 1, LeftForceTurnBased = 1,
    StatusApplied = 4, StatusRemoved = 4,
}) do
    listen(event, arity)
end

------------------------------------------------------------------------
-- Job helpers
------------------------------------------------------------------------

-- Event names we hand to Osiris (move/use/pickup completion).
local function pos_view(p) return p and U.vec(p, 1) or nil end

local function ev_name(job, tag) return "BG3AI_" .. (tag or job.kind) .. "_" .. tostring(job.id) end

-- What the player sees on the actor's portrait/hotbar after an action, so the
-- agent can plan its next step without another query.
function A.summary(char)
    if not char then return nil end
    local out = { pos = pos_view(G.pos(char)) }
    local e = G.entity(char)
    local h = e and U.comp(e, "Health")
    if h then pcall(function() out.hp = h.Hp; out.max_hp = h.MaxHp end) end
    local snap = G.resources(char)
    local function amt(name) local r = snap[name]; return r and U.round(r[0] or 0, 1) or nil end
    out.action, out.bonus, out.reaction = amt("ActionPoint"), amt("BonusActionPoint"), amt("ReactionActionPoint")
    out.movement = amt("Movement")
    for _, n in ipairs({ "SpellSlot", "WarlockSpellSlot" }) do
        local lv = snap[n]
        if lv then
            local t = {}
            for l, a in pairs(lv) do if l > 0 then t[tostring(l)] = math.floor(a + 0.5) end end
            if next(t) then out[n == "SpellSlot" and "slots" or "pact_slots"] = t end
        end
    end
    if G.in_combat(char) then
        out.in_combat = true
        out.my_turn = G.is_my_turn(char)
    end
    if G.b(G.q("HasActiveStatus", char, "SNEAKING")) then out.sneaking = true end
    return out
end

-- Start an action job for `char` (nil for party-wide actions). spec fields:
--   timeout_ms, interrupt = {combat, dialogue, attacked}, osi = {Event = fn},
--   on_tick(job), on_finish(job), start(job) -> performs the game call.
local function start(kind, char, spec)
    if char then
        local prev = A.active[char]
        if prev and prev.status == "running" then
            interrupt(prev, "superseded by a new action")
        end
    end
    spec.act = true
    spec.char = char
    spec.interrupt = spec.interrupt or {}
    local user_tick = spec.on_tick
    local last = 0
    spec.on_tick = user_tick and function(job)
        local t = RPC.now_ms()
        if t - last < TICK_MS then return end
        last = t
        user_tick(job)
    end or nil
    local user_finish = spec.on_finish
    spec.notify = A.notify_next
    spec.on_finish = function(job)
        if char and A.active[char] == job then A.active[char] = nil end
        if user_finish then pcall(user_finish, job) end
        -- Leftover tasks this job queued (a timed-out walk, a ladder Use that
        -- keeps climbing after its UseFinished): purge them, or they run on,
        -- block every later order and are saved with the game. A finished
        -- cast is left alone (its spell task may still be resolving).
        if char and job.osi_task and not job.stopping and A.active[char] == nil
            and (job.status ~= "done" or job.osi_task ~= "spell") then
            local n = osi_tasks(char)
            if n > 0 then
                job.stopping = true
                pcall(G.osi, "PurgeOsirisQueue", char)
                if type(job.result) ~= "table" then job.result = {} end
                job.result.purged_tasks = n
            end
        end
        if char then
            if type(job.result) ~= "table" then job.result = {} end
            local ok, sum = pcall(A.summary, char)
            if ok then job.result.after = sum end
        end
        -- Async jobs (the bridge returned before they settled) announce their
        -- end in the event stream, so any later tool result shows it.
        if job.notify then
            local r = type(job.result) == "table" and job.result or {}
            RPC.push("job_finished", { job = "j" .. tostring(job.id), op = job.kind, status = job.status,
                error = job.error, dist = r.dist, pos = r.pos })
        end
    end
    spec.on_cancel = spec.on_cancel or function(job)
        job.stopping = true
        if job.char then pcall(G.osi, "PurgeOsirisQueue", job.char) end
    end
    local job = RPC.start_job("act." .. kind, spec)
    if char then A.active[char] = job end
    local ok, err = pcall(spec.start, job)
    if not ok then
        local reason = G.is_refusal(err) and err.reason or tostring(err)
        RPC.finish_job(job, "failed", nil, reason)
    end
    return A.reply(job)
end

-- The reply to an act.* call: the job id, an ETA for long actions, and the
-- outcome right away when the job already settled (refusals, no-op moves), so
-- the bridge needs no extra poll.
function A.reply(job)
    local out = { job = job.id }
    if job.eta_s then out.eta_s = U.round(job.eta_s, 1) end
    if job.route_m then out.route_m = U.round(job.route_m, 1) end
    if job.status ~= "running" then
        out.status, out.result, out.error = job.status, job.result, job.error
    end
    return out
end

-- Rough walking time for an ETA (run ~4 m/s, walk ~1.8 m/s, plus the start).
A.RUN_MPS, A.WALK_MPS = 4.0, 1.8
local function walk_eta(m, speed) return 0.4 + (m or 0) / (speed == "Walk" and A.WALK_MPS or A.RUN_MPS) end

-- Run guard checks; a refusal becomes a failed job (so the bridge sees the
-- same shape for every outcome) and other errors propagate.
-- Ops that walk: refused at once while the actor is in turn-based mode outside
-- combat (S024-S026 Shadow Curse: walks failed "no path" with 0 movement after
-- seconds). The bridge then leaves turn-based mode (the HUD's Exit TB button)
-- and retries; tb_ok=true (the agent turned it on itself) walks anyway.
A.WALK_KINDS = { move_to = true, interact = true, pickup = true, talk = true, loot_corpse = true, pickpocket = true }
A.TB_REFUSAL = "turn-based mode outside combat"

function A.tb_gate(kind, args)
    if not A.WALK_KINDS[kind] or args.tb_ok then return end
    local char = args.char and G.guid(args.char) or G.host()
    if not char or G.in_combat(char) then return end
    if G.b(G.q("IsInForceTurnBasedMode", char)) then
        G.refuse(A.TB_REFUSAL .. " (the game switched it on): a walk gets only the turn's movement; leave it first")
    end
end

local function guarded(kind, fn)
    return function(args)
        args = args or {}
        A.notify_next = args.notify == true -- read by start(); Lua runs one op at a time
        local ok, res = pcall(function()
            A.tb_gate(kind, args)
            return fn(args)
        end)
        A.notify_next = false
        if ok then return res end
        if G.is_refusal(res) then
            local job = RPC.start_job("act." .. kind, { act = true })
            RPC.finish_job(job, "failed", nil, res.reason)
            return A.reply(job)
        end
        error(res, 0)
    end
end

local function reg(name, fn) RPC.register("act." .. name, guarded(name, fn)) end


------------------------------------------------------------------------
-- act.move_to {char, target | pos, run=true, within?}  (within: a melee reach, target only)
------------------------------------------------------------------------

local function movement_left(char)
    local snap = G.resources(char)
    return snap.Movement and (snap.Movement[0] or 0) or 0
end

-- Shared walker used by move_to and the approach phase of talk/interact/
-- pickup and the click ops. It ALWAYS walks to a position
-- (CharacterMoveToPosition): entity targets are first resolved to a verified
-- reachable point next to them (G.approach_point), because CharacterMoveTo
-- teleports the character when no path exists.
-- opts: pos, target (uuid, optional: distances are then reported to it),
--       speed, tolerance (m, to pos), reach (m, to target), on_arrive(job, dist)
local function begin_walk(job, opts)
    job.walk = {
        target = opts.target, pos = opts.pos, ev = ev_name(job, "mv"),
        last_pos = G.pos(job.char), still_ms = 0, travelled = 0,
        tolerance = opts.tolerance or 1.5, reach = opts.reach, melee = opts.melee, on_arrive = opts.on_arrive,
        move_id = opts.move_id or job.id,
    }
    local p = opts.pos
    -- The engine's own route to p: big steps along it (jump links, a frame
    -- hitch mid-walk) are the walk, not a teleport (teleport_check).
    local N = BG3AI.Nav
    if type(N) == "table" and type(N.route) == "function" then
        local ok, pts = pcall(N.route, job.char, p)
        if ok and type(pts) == "table" then job.walk.route = pts end
    end
    job.osi_task = job.osi_task or true
    G.osi("CharacterMoveToPosition", job.char, p[1], p[2], p[3], opts.speed, job.walk.ev, job.walk.move_id)
end

-- Distance left to the walk's destination point, and to its target entity.
local function walk_dist_left(job)
    local here = G.pos(job.char)
    if not here then return nil, nil, nil end
    local w = job.walk
    local tpos = w.target and G.pos(w.target)
    return U.dist(here, w.pos), here, tpos and U.dist(here, tpos) or nil
end

-- Is the walk close enough? Point walks: within tolerance of the point.
-- Entity walks: within reach of the entity, or at the approach point.
local function walk_close_enough(job, d, de)
    local w = job.walk
    if de and w.reach and de <= w.reach + 0.5 then return true end
    return d ~= nil and d <= math.max(w.tolerance, 2.0)
end

local function walk_arrived(job)
    local w = job.walk
    if not w or w.done then return end
    w.done = true
    local d, here, de = walk_dist_left(job)
    if w.on_arrive then
        w.on_arrive(job, de or d or 0)
        return
    end
    local res = { pos = pos_view(here), dist = (de or d) and U.round(de or d, 1) or nil }
    if w.target then res.target = w.target end
    if w.melee and de then res.in_reach = de <= w.melee end
    if d and d > 1.5 and not w.target then
        -- Arrived short of a point destination: say so, with the reason if the
        -- pathfinder knows one (e.g. the spot is off the walkable mesh).
        res.stopped_short = true
        local r, info = G.reachable(job.char, w.pos)
        if r == false then
            res.reason = "destination is not reachable" .. (info.reason and (" (" .. tostring(info.reason) .. ")") or "")
        elseif info.needs_jump then
            res.reason = "the rest of the way needs a jump"
        else
            res.reason = "path blocked"
        end
    end
    if not walk_close_enough(job, d, de) then
        res.reached = false
        RPC.finish_job(job, "failed", res, "could not get closer to the destination")
    else
        RPC.push("moved_done", { uuid = job.char, move_id = job.id })
        RPC.finish_job(job, "done", res)
    end
end

local WALK_OSI = {
    EntityEvent = function(job, a)
        if job.walk and a[2] == job.walk.ev then walk_arrived(job) end
    end,
    -- CharacterMoveToCancelled(char, moveId): we pass job.id as the MoveID, so
    -- a late cancel from a superseded walk never fails the next one.
    CharacterMoveToCancelled = function(job, a)
        if job.stopping or not job.walk or job.walk.done then return end
        if tonumber(a[2]) and tonumber(a[2]) ~= job.walk.move_id then return end
        if G.same(a[1], job.char) then
            job.walk.done = true
            RPC.finish_job(job, "failed", job.partial, "movement was cancelled")
        end
    end,
    CharacterMoveFailedUseJump = function(job, a)
        if job.walk and not job.walk.done and G.same(a[1], job.char) then
            job.walk.done = true
            pcall(G.osi, "PurgeOsirisQueue", job.char)
            RPC.finish_job(job, "failed", job.partial, "the path requires a jump")
        end
    end,
}

-- A job's osi handlers plus the walker's; both run when both handle an event.
local function with_walk(osi)
    local out = {}
    for k, f in pairs(WALK_OSI) do out[k] = f end
    for k, f in pairs(osi or {}) do
        local w = out[k]
        out[k] = w and function(job, a)
            w(job, a)
            if job.status == "running" then f(job, a) end
        end or f
    end
    return out
end

-- Teleport detector: Osiris move tasks (and the walks Use/Pickup/UseSpell
-- start on their own) TELEPORT the character when no path exists (seen in
-- S001). A real walk/run never exceeds ~12 m/s. `track` keeps last_pos and
-- last_t between ticks. `armed(prev_pos)` (optional) limits detection, e.g.
-- to moments when the character was not yet next to the item it uses, so a
-- ladder climb or portal is not mistaken for a teleport.
-- Returns here, step, dt; or nil after failing the job.
local function on_route(job, track, prev, here)
    local N = BG3AI.Nav
    if not track.route or type(N) ~= "table" or type(N.onRoute) ~= "function" then return false end
    local ok, yes = pcall(N.onRoute, track.route, prev, here)
    if ok and yes then
        RPC.push("route_jump", { uuid = job.char, from = pos_view(prev), to = pos_view(here) })
        return true
    end
    return false
end

local function teleport_check(job, track, armed)
    local here = G.pos(job.char)
    if not here then return nil end
    local t = RPC.now_ms()
    local prev, dt = track.last_pos, t - (track.last_t or t)
    local step = prev and U.dist(prev, here) or 0
    if prev and (step > 4.0 or (dt >= 100 and step / (dt / 1000) > 12.0))
        and (not armed or armed(prev)) and not on_route(job, track, prev, here) then
        if job.walk then job.walk.done = true end
        pcall(G.osi, "PurgeOsirisQueue", job.char)
        RPC.push("teleport_blocked", { uuid = job.char, from = pos_view(prev), to = pos_view(here) })
        RPC.finish_job(job, "failed", { pos = pos_view(here), teleported = true },
            "engine teleported the character (not allowed)")
        return nil
    end
    track.last_pos, track.last_t = here, t
    return here, step, dt
end

-- Per-tick walk bookkeeping: teleports, distance travelled (combat budget)
-- and stuck detection.
local function walk_tick(job)
    local w = job.walk
    if not w or w.done then return end
    local here, step, dt = teleport_check(job, w)
    if not here then return end
    w.travelled = w.travelled + step
    job.partial = { pos = pos_view(here), travelled = U.round(w.travelled, 1) }
    if step < 0.03 then w.still_ms = w.still_ms + dt else w.still_ms = 0 end

    if w.budget and w.travelled >= w.budget - 0.05 then
        w.done = true
        pcall(G.osi, "PurgeOsirisQueue", job.char)
        local res = { pos = pos_view(here), stopped = "out of movement", travelled = U.round(w.travelled, 1) }
        if w.on_arrive then
            -- an approach that cannot finish: the action after it never happens
            RPC.finish_job(job, "failed", res, "ran out of movement before getting there")
        else
            RPC.finish_job(job, "done", res)
        end
        return
    end
    if w.still_ms >= 5000 then
        local d, _, de = walk_dist_left(job)
        if walk_close_enough(job, d, de) or (d and d <= w.tolerance) then
            walk_arrived(job)
        else
            w.done = true
            pcall(G.osi, "PurgeOsirisQueue", job.char)
            RPC.finish_job(job, "failed", job.partial, "no path to the destination")
        end
    end
end

-- Combat: the walk must fit in the remaining Movement. Returns budget,
-- movement before (both nil out of combat) or refuses.
local function combat_budget(char, route)
    if not G.in_combat(char) then return nil, nil end
    local mv = movement_left(char)
    if mv < 0.5 then G.refuse("no movement left") end
    if route > mv + 0.5 then
        G.refuse(string.format("destination is beyond remaining movement (path %.1f m, %.1f m left)", route, mv))
    end
    return mv, mv
end

-- Combat movement must cost what the UI path costs: if the engine did not
-- deduct the distance walked, the guard does.
local function charge_movement(job, char, mv_before)
    if not mv_before or not job.walk then return end
    local used = mv_before - movement_left(char)
    local owed = job.walk.travelled - used
    if owed > 0.25 then
        G.deduct(char, "Movement", 0, owed)
        if type(job.result) == "table" then job.result.movement_charged_by_guard = U.round(owed, 1) end
    end
end

-- Walk timeout for a route of `m` metres.
local function walk_timeout(m) return math.min(90000, 8000 + math.floor((m or 0) / 0.8 * 1000)) end

-- Approach phase shared by talk/interact/pickup and the click ops: walk to the
-- verified point of `plan` (G.approach_point), then call ready(job). A target
-- that moves (an NPC walking off) is re-planned: mid-walk once it is
-- REPLAN_MOVED_M from where we planned, or on arrival if it is out of reach.
A.REPLAN_MOVED_M = 2.0
A.MAX_REPLANS = 4

local function walk_to_plan(job)
    local ap = job.approach
    local carried = job.walk and job.walk.travelled or 0
    begin_walk(job, { pos = ap.plan.pos, target = ap.target, speed = "Run", tolerance = 1.0, reach = ap.reach,
        move_id = job.id + ap.replans * 1000000,
        on_arrive = function(j, left)
            if left <= ap.reach + 1.0 then
                j.phase = "act"
                return ap.ready(j)
            end
            -- An item that did not move, reached at the verified point: its
            -- position can sit far from where it is used (a ladder's is at its
            -- foot: 3.2 m from the top landing, live S001), and re-planning
            -- gives the same point. Use/Pickup walk the last bit themselves,
            -- under the teleport check.
            local here, t = G.pos(j.char), G.pos(ap.target)
            if here and t and ap.plan.tpos and U.dist(here, ap.plan.pos) <= 1.0
                and U.dist(t, ap.plan.tpos) <= 0.5 and G.b(G.q("IsItem", ap.target)) then
                j.phase = "act"
                return ap.ready(j)
            end
            local ok, why = ap.replan(j)
            if not ok then
                RPC.finish_job(j, "failed", { dist = U.round(left, 1) }, why or "could not get close enough")
            end
        end })
    job.walk.travelled = carried -- cumulative over re-plans (combat budget, guard charge)
    job.walk.budget = ap.budget
end

-- Start a new walk toward the target's current position. Returns true if the
-- job goes on (walking again, or already in reach), else false, reason.
local function replan(job)
    local ap = job.approach
    if ap.replans >= A.MAX_REPLANS then return false, "could not catch up with the target" end
    if not G.visible(ap.target) then return false, "the target is no longer visible" end
    local ok, plan = pcall(G.approach_point, job.char, ap.target, ap.reach)
    if not ok then return false, G.is_refusal(plan) and plan.reason or tostring(plan) end
    ap.replans = ap.replans + 1
    if job.walk then job.walk.done = true end
    pcall(G.osi, "PurgeOsirisQueue", job.char)
    if plan.here then
        job.phase = "act"
        ap.ready(job)
        return true
    end
    ap.plan = plan
    walk_to_plan(job)
    return true
end

local function begin_approach(job, target, plan, reach, ready, budget)
    if plan.here then
        job.phase = "act"
        return ready(job)
    end
    job.phase = "approach"
    job.approach = { target = target, reach = reach, ready = ready, budget = budget, plan = plan,
        replans = 0, replan = replan }
    walk_to_plan(job)
end

-- walk_tick for approach phases, plus the moving-target check.
local function approach_tick(job)
    local ap, w = job.approach, job.walk
    if ap and w and not w.done and ap.plan.tpos then
        local t = G.pos(ap.target)
        if t and U.dist(t, ap.plan.tpos) > A.REPLAN_MOVED_M then
            local ok, why = replan(job)
            if not ok then
                w.done = true
                pcall(G.osi, "PurgeOsirisQueue", job.char)
                RPC.finish_job(job, "failed", job.partial, why)
            end
            return
        end
    end
    return walk_tick(job)
end

reg("move_to", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local target = args.target and G.guid(args.target)
    local pos = args.pos
    local route, melee, reach
    if target then
        -- Entity targets: walk to a verified reachable point next to them.
        G.target_visible(target)
        G.within(char, target, G.MAX_MOVE_DIST, "destination")
        -- within (melee, combat_turn): stop inside the guard's own edge-to-edge reach, not at 2.2 m
        -- centres (S023-S025: 22 melee attacks refused "out of range (2.5 m, range 1.5 m)" after a move
        -- said done; a Spiritual Weapon is small, so 2.2-2.7 m was outside its reach).
        melee = tonumber(args.within) and G.melee_reach(char, target, tonumber(args.within)) or nil
        reach = melee and math.max(1.2, math.min(A.MOVE_REACH, melee - A.MELEE_STOP_SLACK)) or A.MOVE_REACH
        local plan = G.approach_point(char, target, reach)
        if plan.here then
            return start("move_to", char, {
                timeout_ms = 1000,
                start = function(job)
                    RPC.finish_job(job, "done", { pos = pos_view(G.pos(char)), dist = U.round(plan.dist, 1),
                        target = target, already_there = true,
                        in_reach = melee and plan.dist <= melee or nil })
                end,
            })
        end
        pos, route = plan.pos, plan.length_m
    elseif pos then
        G.point_ok(char, pos)
        route = G.point_reachable(char, pos).length_m or U.dist(G.pos(char), pos)
    else
        G.refuse("no destination given")
    end

    local budget, mv_before = combat_budget(char, route)
    local combat = budget ~= nil
    local speed = (args.run == false and not combat) and "Walk" or "Run"

    return start("move_to", char, {
        eta_s = walk_eta(route, speed), route_m = route,
        timeout_ms = walk_timeout(route),
        interrupt = { combat = not combat, dialogue = true, attacked = not combat },
        osi = WALK_OSI,
        start = function(job)
            begin_walk(job, { pos = pos, target = target, speed = speed,
                tolerance = 1.5, reach = reach, melee = melee })
            job.walk.budget = budget
        end,
        on_tick = walk_tick,
        on_finish = function(job) charge_movement(job, char, mv_before) end,
    })
end)

------------------------------------------------------------------------
-- act.stop {char}: stop what the character is doing, like a player's new
-- order: the running action is interrupted and queued Osiris tasks (ours,
-- or left over from a save) are purged. Never moves the character.
------------------------------------------------------------------------

reg("stop", function(args)
    local char = G.actor(args.char)
    local job = A.active[char]
    local stopped = job and job.status == "running" and job.id or nil
    local n = osi_tasks(char)
    if stopped then interrupt(job, "stopped") end -- purges too
    if osi_tasks(char) > 0 then pcall(G.osi, "PurgeOsirisQueue", char) end
    A.use_streak[char] = nil
    return start("stop", char, {
        timeout_ms = 1000,
        start = function(j)
            RPC.finish_job(j, "done", { stopped_job = stopped, purged_tasks = n, left = osi_tasks(char) })
        end,
    })
end)

-- Top-level character holding an item (walks up bags), or nil.
local function holder(item)
    local cur = item
    for _ = 1, 6 do
        local o = G.guid(G.q("GetInventoryOwner", cur))
        if not o then return nil end
        if G.b(G.q("IsCharacter", o)) then return o end
        cur = o
    end
    return nil
end
A.holder = holder

------------------------------------------------------------------------
-- act.interact {char, target}: doors, levers, containers, books, ladders...
------------------------------------------------------------------------

A.LADDER_DY = 1.5         -- a climb ends on another floor at least this far up/down
A.LADDER_CLIMB_MS = 10000 -- interact on a ladder: the longest it waits for the climb after the Use

reg("interact", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local target = G.guid(args.target)
    if not target then G.refuse("no target given") end
    -- An item in the actor's own bags (potion, scroll, book): use it in place.
    local carried = G.b(G.q("IsItem", target)) and holder(target)
    if carried and not G.same(carried, char) then G.refuse("that is in another character's inventory") end
    if not carried then
        G.target_visible(target)
        G.within(char, target, G.MAX_INTERACT_DIST, "target")
    end
    if G.b(G.q("IsCharacter", target)) then
        if G.is_dead(target) then
            G.refuse("looting corpses is not supported by this action yet (use the UI)")
        end
        G.refuse("use act.talk to talk to characters")
    end
    -- Locked: as in the game, a party member carrying its key opens it (the
    -- player would select them and click). Otherwise refuse with the options.
    local key_item, key_note
    if G.b(G.q("IsLocked", target)) then
        local h, k = G.party_key(target, char)
        if not h then
            G.refuse("it is locked and nobody in the party carries its key (lockpick it, force it, or find the key)")
        end
        if not G.same(h, char) then
            char = h
            G.not_in_dialogue(char)
            G.combat_gate(char, "no_combat")
            G.within(char, target, G.MAX_INTERACT_DIST, "the key holder's target")
        end
        key_item = k
        local function nm(u) local ok, n = pcall(function() return BG3AI.Visibility.name(u) end); return ok and n or u end
        key_note = { unlocked_with = nm(k), key_holder = nm(char) }
    end
    local was_open = G.b(G.q("IsOpened", target))
    local openable, ladder = false, false
    pcall(function()
        local si = G.entity(target).ServerItem
        openable = si.IsDoor or si.IsContainer or false
        ladder = si.IsLadder or false
    end)

    -- Doors/containers: report the open/closed state change. UseFinished lands
    -- a few frames before IsOpened flips, so wait (briefly) for the flip.
    local function with_key(res)
        if key_note then for kk, v in pairs(key_note) do res[kk] = v end end
        return res
    end
    local function state_result()
        local open = G.b(G.q("IsOpened", target))
        if open == was_open then return nil end
        local res = open and { opened = true } or { closed = true }
        if open then
            pcall(function() if G.entity(target).ServerItem.IsContainer then res.container_open = true end end)
        end
        return with_key(res)
    end

    local plan = carried and { here = true, length_m = 0 } or G.approach_point(char, target, A.USE_REACH)

    return start("interact", char, {
        eta_s = walk_eta(plan.length_m) + 1.0, route_m = plan.length_m,
        timeout_ms = 30000 + walk_timeout(plan.length_m),
        interrupt = { combat = true, dialogue = true, attacked = true },
        osi = with_walk({
            UseFinished = function(job, a)
                if G.same(a[1], char) and G.same(a[2], target) then
                    if ladder and not job.ladder then return end -- not our Use yet (see below)
                    if not G.b(a[3]) then
                        RPC.finish_job(job, "failed", nil, key_note and G.b(G.q("IsLocked", target))
                            and ("still locked: " .. key_note.unlocked_with .. " did not open it") or "could not use that")
                        return
                    end
                    local res = state_result()
                    if ladder then
                        -- Acceptance 14:0x: the walk to a ladder's top fires a UseFinished of its own before our
                        -- Use: ending the job there purged the queue and cancelled the climb. Only our Use counts,
                        -- and its climb is awaited in on_tick.
                        if job.ladder then job.used_at = job.used_at or RPC.now_ms() end
                    elseif res then RPC.finish_job(job, "done", res)
                    elseif not openable then RPC.finish_job(job, "done", with_key({ used = true }))
                    else job.used_at = RPC.now_ms() end
                end
            end,
            EntityEvent = function(job, a)
                if a[2] == job.use_ev and not job.used_at then job.used_at = RPC.now_ms() end
            end,
        }),
        start = function(job)
            begin_approach(job, target, plan, A.USE_REACH, function(j)
                if key_item then
                    local ok, err = pcall(G.unlock_with_key, target, char, A.USE_REACH + 0.7)
                    if not ok then
                        RPC.finish_job(j, "failed", nil, "could not unlock it: " .. tostring(err))
                        return
                    end
                end
                j.use_ev = ev_name(j, "use")
                j.use_track = {}
                j.osi_task = true
                if ladder then j.ladder = { y0 = (G.pos(char) or {})[2], item = target } end
                G.osi("Use", char, target, j.use_ev)
            end)
        end,
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if job.phase ~= "act" then return end
            -- Use walks on its own if still out of range; a jump while far from
            -- the item is a teleport, a jump next to it is the use (ladders, portals).
            if not teleport_check(job, job.use_track, function(prev)
                local t = G.pos(target)
                return not t or U.dist(prev, t) > A.USE_REACH + 0.7
            end) then return end
            local res = state_result()
            if job.ladder and job.used_at then
                -- Acceptance 13:4x: "done — used" 300 ms after the Use, and the job's end purges the task queue: the
                -- climb was cancelled at the top (the active character stayed up, followers went down). Wait for
                -- the climb itself: the actor on another floor and still, or give up after A.LADDER_CLIMB_MS.
                local lj, here = job.ladder, G.pos(char)
                local now = RPC.now_ms()
                if here and lj.y0 then
                    local moved = lj.last and U.dist(lj.last, here) or 1
                    lj.still = (moved < 0.03) and (lj.still or now) or nil
                    lj.last = here
                    local dy = here[2] - lj.y0
                    if math.abs(dy) >= A.LADDER_DY and lj.still and now - lj.still >= 300 then
                        RPC.finish_job(job, "done", with_key({ used = true, climbed = dy < 0 and "down" or "up",
                            dy = U.round(dy, 1) }))
                        return
                    end
                end
                if now - job.used_at > A.LADDER_CLIMB_MS then
                    RPC.finish_job(job, "failed", { used = true, climbed = false },
                        "used the ladder but did not climb it (still on the same floor after "
                        .. math.floor(A.LADDER_CLIMB_MS / 1000) .. " s)")
                end
                return
            end
            if res then
                RPC.finish_job(job, "done", res)
            elseif job.used_at and RPC.now_ms() - job.used_at > (openable and 1500 or 300) then
                RPC.finish_job(job, "done", with_key({ used = true }))
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.pickup {char, item}   (untested live)
------------------------------------------------------------------------

function A.selected(char)
    local user = G.q("GetReservedUserID", char)
    return user ~= nil and G.same(G.q("GetCurrentCharacter", user), char)
end


reg("pickup", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local item = G.guid(args.item)
    if not item or not G.b(G.q("IsItem", item)) then G.refuse("that is not an item") end
    if G.q("GetInventoryOwner", item) ~= nil then
        G.refuse("that item is inside a container (open it and loot instead)")
    end
    G.target_visible(item)
    G.within(char, item, G.MAX_INTERACT_DIST, "item")

    local function picked(job)
        local h = holder(item)
        if h and G.is_party(h) then
            RPC.finish_job(job, "done", { picked_up = true, by = h })
            return true
        end
    end
    local plan = G.approach_point(char, item, A.USE_REACH)
    return start("pickup", char, {
        eta_s = walk_eta(plan.length_m) + 1.0, route_m = plan.length_m,
        timeout_ms = 30000 + walk_timeout(plan.length_m),
        interrupt = { combat = true, dialogue = true, attacked = true },
        osi = with_walk({
            AddedTo = function(job, a) if G.same(a[1], item) then picked(job) end end,
            PickupFailed = function(job, a)
                if G.same(a[1], char) and G.same(a[2], item) then
                    RPC.finish_job(job, "failed", nil, "could not pick that up")
                end
            end,
        }),
        start = function(job)
            begin_approach(job, item, plan, A.USE_REACH, function(j)
                j.use_track = {}
                -- ForcePickUpOnFailure = 0: never teleport the item into the bag.
                j.osi_task = true
                G.osi("Pickup", char, item, ev_name(j, "pick"), 0)
            end)
        end,
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if job.phase ~= "act" then return end
            if picked(job) then return end
            teleport_check(job, job.use_track, function(prev)
                local t = G.pos(item)
                return t ~= nil and U.dist(prev, t) > A.USE_REACH + 0.7
            end)
        end,
    })
end)

------------------------------------------------------------------------
-- act.talk {char, npc}   (untested live)
-- Walks into talking range, then raises DialogStartRequested(npc, char): the
-- same engine event a player's click produces, which the shipped story routes
-- through QRY_SelectAndStartDialog (Shared/__GLOBAL_Dialogs.txt).
------------------------------------------------------------------------

reg("talk", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local npc = G.guid(args.npc)
    if not npc or not G.b(G.q("IsCharacter", npc)) then G.refuse("that is not someone you can talk to") end
    if G.same(npc, char) then G.refuse("cannot talk to yourself") end
    if G.is_dead(npc) then G.refuse("they are dead") end
    G.target_visible(npc)
    G.within(char, npc, G.MAX_INTERACT_DIST, "character")
    local plan = G.approach_point(char, npc, G.TALK_RANGE)

    local function request(job)
        job.phase = "requested"
        job.requested_at = RPC.now_ms()
        G.osi("DialogStartRequested", npc, char)
    end

    return start("talk", char, {
        eta_s = walk_eta(plan.length_m) + 1.5, route_m = plan.length_m,
        timeout_ms = 60000,
        interrupt = { combat = true, dialogue = false, attacked = true },
        osi = with_walk({
            DialogActorJoined = function(job, a)
                if job.phase == "requested" and (G.same(a[3], char) or G.same(a[3], npc)) then
                    RPC.finish_job(job, "done", { dialogue = true })
                end
            end,
            DialogRequestFailed = function(job)
                if job.phase == "requested" then
                    RPC.finish_job(job, "failed", nil, "they have nothing to say")
                end
            end,
        }),
        start = function(job)
            begin_approach(job, npc, plan, G.TALK_RANGE, request)
        end,
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if job.phase == "requested" then
                if G.in_dialogue(char) then
                    RPC.finish_job(job, "done", { dialogue = true })
                elseif RPC.now_ms() - job.requested_at > 6000 then
                    RPC.finish_job(job, "failed", nil, "they have nothing to say")
                end
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.cast {char, spell, target? | pos? | targets?, upcast?}   (untested live)
------------------------------------------------------------------------

local function name_of(uuid)
    local V = BG3AI.Visibility
    if V and V.name then
        local ok, n = pcall(V.name, uuid)
        if ok and n then return n end
    end
    return U.loca(G.q("GetDisplayName", uuid))
end

local NULL_GUID = "NULL_00000000-0000-0000-0000-000000000000"

local function resolve_spell(spell, upcast)
    if upcast then
        local variant = spell .. "_" .. tostring(upcast)
        local st = Ext.Stats.Get(variant)
        if not st or st.RootSpellID ~= spell then G.refuse("that spell cannot be cast at that level") end
        return variant
    end
    return spell
end

reg("cast", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    G.cast_options_ok(args.options) -- callers may not request Ignore*/Forced options
    local base = args.spell
    if type(base) ~= "string" then G.refuse("no action given") end

    local targets = {}
    if args.target then targets[1] = G.guid(args.target) end
    for _, t in ipairs(args.targets or {}) do targets[#targets + 1] = G.guid(t) end
    if #targets > 2 then G.refuse("more than two targets is not supported yet") end
    for _, t in ipairs(targets) do if not t then G.refuse("bad target") end end
    local pos = args.pos
    if not Ext.Stats.Get(base) then
        -- e.g. Shout_Dodge: BG3 has no Dodge action (live: no such stats entry).
        G.refuse("unknown action " .. base .. " (not an ability id; get_actions lists them)")
    end
    if #targets == 0 and not pos then
        local st = Ext.Stats.Get(base)
        if tostring(st.SpellType) ~= "Shout" then G.refuse("this action needs a target") end
        targets[1] = char -- self-centred actions (Dash, Hide, Disengage...)
    end

    -- Upcasting to the spell's own level is just a normal cast.
    local upcast = args.upcast and tonumber(args.upcast)
    local bst = Ext.Stats.Get(base)
    if upcast and bst and upcast == (tonumber(bst.Level) or 0) then upcast = nil end

    -- Validate the base spell (known/prepared/cooldown) and the cast itself;
    -- with an upcast, slots are checked at the upcast level (not the base
    -- level, which may be spent).
    local check = G.check_cast(char, base, targets[1], pos, upcast)
    if targets[2] then
        G.target_visible(targets[2])
        G.within(char, targets[2], G.MAX_INTERACT_DIST, "target")
        if check.range and U.dist(G.pos(char), G.pos(targets[2])) > check.range + 0.5 then
            G.refuse("second target is out of range: move closer first")
        end
    end
    local spell = resolve_spell(base, upcast)
    if spell ~= base then
        -- Resources for the upcast variant's own costs.
        local vst = Ext.Stats.Get(spell)
        check.costs = G.parse_costs(vst.UseCosts)
        check.payers = {}
        local in_combat = G.in_combat(char)
        for _, c in ipairs(check.costs) do
            local turn_res = c.name == "ActionPoint" or c.name == "BonusActionPoint"
                or c.name == "Movement" or c.name == "ReactionActionPoint"
            if in_combat or not turn_res then
                local n, l = G.payer(check.snapshot, c)
                if not n then G.refuse("not enough " .. G.friendly(c.name)) end
                check.payers[#check.payers + 1] = { name = n, level = l, amount = c.amount }
            end
        end
    end
    local combat = G.in_combat(char)
    -- Never let the cast walk: the Osiris spell task's approach has no path
    -- check (teleports). The range was checked by G.check_cast whenever it is
    -- known; only an unknown range out of combat lets the engine walk into range.
    local without_move = (combat or check.range) and 1 or 0
    local harmful = false
    pcall(function()
        for _, f in ipairs(check.stats.SpellFlags) do if f == "IsHarmful" then harmful = true end end
    end)

    local function matches(a)
        return G.same(a[1], char) and (a[2] == spell or a[2] == base)
    end

    return start("cast", char, {
        eta_s = 2.0,
        timeout_ms = 30000,
        interrupt = { combat = false, dialogue = true, attacked = false },
        osi = {
            CastedSpell = function(job, a)
                if matches(a) and not job.casted then
                    -- Hits usually land before CastedSpell; give stragglers a
                    -- short settle window (see on_tick), then report.
                    job.casted = RPC.now_ms()
                end
            end,
            CastSpellFailed = function(job, a)
                if matches(a) then RPC.finish_job(job, "failed", nil, "the action failed") end
            end,
            -- AttackedBy(defender, attackerOwner, attacker, damageType, amount, cause, storyActionId)
            AttackedBy = function(job, a)
                if G.same(a[3], char) or G.same(a[2], char) then
                    local tgt = G.guid(a[1])
                    if tgt and G.visible(tgt) then
                        job.hits[#job.hits + 1] = { target = tgt, name = name_of(tgt),
                            amount = tonumber(a[5]), type = a[4] ~= "None" and a[4] or nil }
                    end
                end
            end,
            -- StatusApplied(target, status, causee, storyActionId): visible statuses only.
            StatusApplied = function(job, a)
                if G.same(a[3], char) then
                    local tgt = G.guid(a[1])
                    local V = BG3AI.Visibility
                    local info = V and V.statusInfo and V.statusInfo(tostring(a[2]))
                    if tgt and info and G.visible(tgt) then
                        job.statuses[#job.statuses + 1] = { target = tgt, status = info.name }
                    end
                end
            end,
        },
        on_tick = function(job)
            if job.casted and RPC.now_ms() - job.casted >= 250 then
                local res = { cast = spell }
                if #job.hits > 0 then
                    res.damage = job.hits
                    local total = 0
                    for _, h in ipairs(job.hits) do total = total + (h.amount or 0) end
                    res.total_damage = total
                elseif harmful then
                    res.no_damage = true -- missed, saved, or no damage component
                end
                if #job.statuses > 0 then res.statuses = job.statuses end
                -- Target state as the target's health bar shows it (if visible).
                if targets[1] and not G.same(targets[1], char) and G.visible(targets[1]) then
                    res.target = { uuid = targets[1], dead = G.is_dead(targets[1]) or nil,
                        downed = G.is_downed(targets[1]) or nil }
                end
                RPC.finish_job(job, "done", res)
            end
        end,
        start = function(job)
            job.hits, job.statuses = {}, {}
            job.osi_task = "spell"
            if pos then
                -- Engine form has 6 inputs (6th = WithoutMove), verified live
                -- on bg3le; the 5-input form is kept as a fallback for builds
                -- without it.
                G.osi_forms("UseSpellAtPosition", { char, spell, pos[1], pos[2], pos[3], without_move },
                    { char, spell, pos[1], pos[2], pos[3] })
            else
                G.osi("UseSpell", char, spell, targets[1], targets[2] or NULL_GUID, without_move)
            end
        end,
        on_finish = function(job)
            if not job.casted then return end
            -- Resource audit: charge whatever the engine did not.
            local after = G.resources(char)
            local charged = {}
            for _, p in ipairs(check.payers) do
                local before = (check.snapshot[p.name] or {})[p.level] or 0
                local now = (after[p.name] or {})[p.level] or 0
                local owed = p.amount - (before - now)
                if owed > 0.01 and G.deduct(char, p.name, p.level, owed) then
                    charged[#charged + 1] = { resource = G.friendly(p.name), level = p.level > 0 and p.level or nil, amount = owed }
                end
            end
            if #charged > 0 and type(job.result) == "table" then job.result.charged_by_guard = charged end
        end,
    })
end)

------------------------------------------------------------------------
-- act.end_turn {char}
-- Done when the character's turn is over: its TurnBased.IsActiveCombatTurn
-- drops, Osiris TurnEnded fires, or (a shared party turn group, the usual
-- case for party members with equal initiative) RequestedEndTurn is set while
-- the group stays active until the other members end too. TurnEnded only
-- fires for the whole group (live: one end_turn per member, then both
-- turn_ended events at once). result.still_to_act lists those members.
------------------------------------------------------------------------

local function group_waiting(char)
    local out = {}
    for _, m in ipairs(G.party()) do
        if not G.same(m, char) and G.is_my_turn(m) then out[#out + 1] = m end
    end
    return out
end

reg("end_turn", function(args)
    local char = G.actor(args.char)
    if not G.in_combat(char) then G.refuse("not in combat") end
    if G.turn_requested_end(char) then
        G.refuse("this character already ended its turn (the rest of its turn group is still acting)")
    end
    if not G.is_my_turn(char) then G.refuse("it is not this character's turn") end
    local function finish(job, requested)
        local res = { ended = true }
        if requested then
            -- The game applies it when the rest of the group ends; until then the guard counts it as ended.
            res.requested = true
        end
        local w = group_waiting(char)
        if #w > 0 then res.still_to_act = w end
        RPC.finish_job(job, "done", res)
    end
    return start("end_turn", char, {
        timeout_ms = 10000,
        osi = {
            TurnEnded = function(job, a)
                if G.same(a[1], char) then finish(job) end
            end,
        },
        start = function() G.osi("EndTurn", char) end,
        on_tick = function(job)
            if not G.is_my_turn(char) then finish(job) return end
            -- Shared turn group, others still up: EndTurn stays pending until they end (live S002:
            -- every such end_turn timed out at 10 s). Record it and return.
            if RPC.now_ms() - job.started >= A.END_REQUEST_AFTER_MS and #group_waiting(char) > 0 then
                G.end_requested[char] = G.now_ms()
                finish(job, true)
            end
        end,
        on_timeout = function(job)
            job.partial = { requested_end = G.turn_requested_end(char), still_to_act = group_waiting(char) }
        end,
    })
end)

------------------------------------------------------------------------
-- act.equip / act.unequip {char, item}   (untested live)
------------------------------------------------------------------------

local function party_item(item)
    local g = G.guid(item)
    if not g or not G.b(G.q("IsItem", g)) then G.refuse("that is not an item") end
    local h = holder(g)
    if not h or not G.is_party(h) then G.refuse("that item is not in the party's inventory") end
    return g, h
end

-- Why the engine refused an Equip (EquipFailed carries no reason). Live S025: the Sentinel and Studded
-- Shields failed on Claude, whose main hand held the Moonlantern (Quest_GLO_Moonlantern: Torch, Light,
-- Unstowable): a held light source or a two-handed weapon blocks the off hand.
local function stats_of(uuid)
    local ok, st = pcall(function()
        local e = Ext.Entity.Get(uuid)
        return Ext.Stats.Get(e.Data.StatsId)
    end)
    return ok and st or nil
end

local function has_prop(st, want)
    local ok, props = pcall(function() return st["Weapon Properties"] end)
    if not ok or type(props) ~= "table" then return false end
    for _, p in pairs(props) do if p == want then return true end end
    return false
end

function A.equip_why(char, item)
    local why = {}
    local ist = stats_of(item)
    local slot = ""
    pcall(function() slot = tostring(ist.Slot or "") end)
    local offhand = slot:find("Offhand") ~= nil or slot:find("Shield") ~= nil
    -- The melee main-hand slot itself (GetEquippedWeapon may answer another slot). Live 2026-10-10: the Moonlantern
    -- (Torch/Unstowable) in the main hand does NOT block a shield (Sentinel Shield worn with it); S025's failure was
    -- the two-handed Sword of Justice equipped one step earlier in the same batch.
    local w = G.guid(G.q("GetEquippedItem", char, "Melee Main Weapon"))
    local wst = w and stats_of(w)
    if offhand and w and wst and not G.same(w, item) and has_prop(wst, "Twohanded") then
        why[#why + 1] = ("the main hand holds a two-handed weapon (%s): equip a one-handed weapon first"):format(
            name_of(w) or "a weapon")
    end
    local h = holder(item)
    if h and not G.same(h, char) then why[#why + 1] = "it is in another member's bag (give it first)" end
    if not G.b(G.q("IsProficientWith", char, item)) then why[#why + 1] = "not proficient with it" end
    if #why == 0 then return "cannot equip that" end
    return "cannot equip that: " .. table.concat(why, "; ")
end

reg("equip", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local item = party_item(args.item)
    if not G.b(G.q("IsEquipable", item)) then G.refuse("that cannot be equipped") end
    return start("equip", char, {
        timeout_ms = 8000,
        osi = {
            Equipped = function(job, a)
                if G.same(a[1], item) and G.same(a[2], char) then RPC.finish_job(job, "done", { equipped = true }) end
            end,
            EquipFailed = function(job, a)
                if G.same(a[1], item) then
                    local ok, why = pcall(A.equip_why, char, item)
                    RPC.finish_job(job, "failed", nil, ok and why or "cannot equip that")
                end
            end,
        },
        -- Engine form is Equip/5 (verified live on bg3le): AddToMainInventoryOnFail,
        -- ShowNotification, ClearOriginalOwner=0 (never launder stolen goods).
        -- Equip/2 is a fallback for builds without it.
        start = function() G.osi_forms("Equip", { char, item, 1, 1, 0 }, { char, item }) end,
        on_tick = function(job)
            if G.b(G.q("IsEquipped", item)) and G.same(holder(item), char) then
                RPC.finish_job(job, "done", { equipped = true })
            end
        end,
    })
end)

reg("unequip", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local item, h = party_item(args.item)
    if not G.same(h, char) or not G.b(G.q("IsEquipped", item)) then G.refuse("that is not equipped by this character") end
    return start("unequip", char, {
        timeout_ms = 8000,
        osi = {
            Unequipped = function(job, a)
                if G.same(a[1], item) then RPC.finish_job(job, "done", { unequipped = true }) end
            end,
            UnequipFailed = function(job, a)
                if G.same(a[1], item) then RPC.finish_job(job, "failed", nil, "cannot unequip that") end
            end,
        },
        start = function() G.osi("Unequip", char, item) end,
        on_tick = function(job)
            if not G.b(G.q("IsEquipped", item)) then RPC.finish_job(job, "done", { unequipped = true }) end
        end,
    })
end)

------------------------------------------------------------------------
-- act.transfer {item, to: uuid | "camp", amount?}   (untested live)
------------------------------------------------------------------------

reg("transfer", function(args)
    local item, from = party_item(args.item)
    if G.in_combat(from) then G.refuse("not available during combat through this action (use the UI)") end
    if G.b(G.q("IsEquipped", item)) then G.refuse("unequip it first") end
    -- One value only: G.q returns three and tonumber(n, base) raised "bad argument #1 to 'tonumber'"
    -- on every give/camp (S018-S024, 7 calls).
    local stack = tonumber((G.q("GetStackAmount", item))) or 1
    local amount = math.max(1, math.min(stack, math.floor(tonumber(args.amount) or stack)))

    if args.to == "camp" then
        local host = G.host()
        return start("transfer", nil, {
            timeout_ms = 8000,
            start = function() G.osi("SendToCampChest", item, host) end,
            on_tick = function(job)
                local h = holder(item)
                if not h or not G.is_party(h) then RPC.finish_job(job, "done", { sent_to = "camp" }) end
            end,
        })
    end

    local to = G.actor(args.to)
    if G.same(to, from) then G.refuse("it is already there") end
    return start("transfer", nil, {
        timeout_ms = 8000,
        osi = {
            AddedTo = function(job, a)
                if G.same(a[1], item) and G.same(a[2], to) then RPC.finish_job(job, "done", { to = to, amount = amount }) end
            end,
        },
        -- ShowNotification=1, ClearOriginalOwner=0 (never launder stolen goods).
        start = function() G.osi("ToInventory", item, to, amount, 1, 0) end,
        on_tick = function(job)
            if G.same(holder(item), to) then RPC.finish_job(job, "done", { to = to, amount = amount }) end
        end,
    })
end)

------------------------------------------------------------------------
-- act.select {char}
------------------------------------------------------------------------

reg("select", function(args)
    local char = G.actor(args.char)
    return start("select", char, {
        timeout_ms = 3000,
        osi = {
            CharacterSelectedForUser = function(job, a)
                if G.same(a[1], char) then RPC.finish_job(job, "done", { selected = true }) end
            end,
        },
        start = function() G.osi("MakePlayerActive", char) end,
        -- Post-condition: the owning user's current character is `char`.
        on_tick = function(job)
            local user = G.q("GetReservedUserID", char)
            if user and G.same(G.q("GetCurrentCharacter", user), char) then
                RPC.finish_job(job, "done", { selected = true })
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.chain {char, leader} / act.unchain {char}
------------------------------------------------------------------------

reg("chain", function(args)
    local char = G.actor(args.char)
    local leader = G.actor(args.leader)
    if G.same(char, leader) then G.refuse("cannot chain a character to themselves") end
    if G.b(G.q("InSamePartyGroup", char, leader)) then G.refuse("already chained together") end
    return start("chain", char, {
        timeout_ms = 3000,
        start = function() G.osi("AttachToPartyGroup", char, leader) end,
        on_tick = function(job)
            if G.b(G.q("InSamePartyGroup", char, leader)) then RPC.finish_job(job, "done", { chained = true }) end
        end,
    })
end)

reg("unchain", function(args)
    local char = G.actor(args.char)
    local alone = true
    for _, p in ipairs(G.party()) do
        if p ~= char and G.b(G.q("InSamePartyGroup", char, p)) then alone = false break end
    end
    if alone then G.refuse("already unchained") end
    return start("unchain", char, {
        timeout_ms = 3000,
        start = function() G.osi("DetachFromPartyGroup", char) end,
        on_tick = function(job)
            for _, p in ipairs(G.party()) do
                if p ~= char and G.b(G.q("InSamePartyGroup", char, p)) then return end
            end
            RPC.finish_job(job, "done", { unchained = true })
        end,
    })
end)

------------------------------------------------------------------------
-- act.sneak {char, on}   (untested live)
-- Sneaking is the Hide action (Shout_Hide), cast like any other action; out
-- of combat it costs nothing. Stopping is moving out of stealth, which the
-- engine does on its own when the character stands up; we purge the Hide
-- and wait for SNEAKING to drop. RemoveStatus is never used.
------------------------------------------------------------------------

reg("sneak", function(args)
    local char = G.actor(args.char)
    local on = args.on ~= false
    local sneaking = G.b(G.q("HasActiveStatus", char, "SNEAKING"))
    if on == sneaking then G.refuse(on and "already sneaking" or "not sneaking") end
    if not on then
        G.refuse("leaving stealth is not supported by this action yet (use the sneak key via the UI)")
    end
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local check = G.check_cast(char, "Shout_Hide", char, nil, nil)
    return start("sneak", char, {
        timeout_ms = 8000,
        start = function(job) job.osi_task = "spell"; G.osi("UseSpell", char, "Shout_Hide", char, NULL_GUID, 1) end,
        on_tick = function(job)
            if G.b(G.q("HasActiveStatus", char, "SNEAKING")) then
                RPC.finish_job(job, "done", { sneaking = true })
            end
        end,
        on_finish = function(job)
            if job.status ~= "done" then return end
            local after = G.resources(char)
            for _, p in ipairs(check.payers) do
                local owed = p.amount - (((check.snapshot[p.name] or {})[p.level] or 0) - ((after[p.name] or {})[p.level] or 0))
                if owed > 0.01 then G.deduct(char, p.name, p.level, owed) end
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.turn_based {on, char?}: Osiris ForceTurnBasedMode for every party
-- member (or just char). The bridge prefers the HUD's own Enter/Exit TB
-- button (ui.press HotBar ExitFTBButton) and falls back to this. S021: the
-- host-only version timed out while the rest of the party stayed in TB.
-- act.mode {}: the cheap read behind the bridge's "leave turn-based mode
-- before a walk" reflex (S024-S026: the Shadow Curse forced the party into
-- turn-based mode outside combat; every walk then failed with 0 movement).
------------------------------------------------------------------------

function A.ftb_members(only)
    local out = {}
    for _, u in ipairs(only and { only } or G.party()) do
        if G.b(G.q("IsInForceTurnBasedMode", u)) then out[#out + 1] = u end
    end
    return out
end

RPC.register("act.mode", function()
    local combat = false
    for _, u in ipairs(G.party()) do
        if G.in_combat(u) then combat = true end
    end
    local ftb = A.ftb_members()
    return { combat = combat, ftb = #ftb > 0, ftb_members = ftb }
end)

reg("turn_based", function(args)
    local char = args.char and G.actor(args.char) or nil
    if G.in_combat(char or G.host()) then G.refuse("already in combat") end
    local on = args.on ~= false
    local members = char and { char } or G.party()
    local function pending()
        local out = {}
        for _, u in ipairs(members) do
            if G.b(G.q("IsInForceTurnBasedMode", u)) ~= on then out[#out + 1] = u end
        end
        return out
    end
    if #pending() == 0 then G.refuse(on and "already in turn-based mode" or "not in turn-based mode") end
    return start("turn_based", nil, {
        timeout_ms = 5000,
        start = function()
            for _, u in ipairs(pending()) do G.osi("ForceTurnBasedMode", u, on and 1 or 0) end
        end,
        on_tick = function(job)
            local left = pending()
            if #left == 0 then RPC.finish_job(job, "done", { turn_based = on }) end
            job.partial = { still_turn_based = left }
        end,
    })
end)

------------------------------------------------------------------------
-- act.go_to_camp {} / act.leave_camp {}   (untested live; NOT allowed on the user's save)
-- The UI's camp button raises the engine event TeleportToFromCamp(player);
-- the shipped story (Shared/GLO_Camp.txt "Camp Buttons") then enters or
-- leaves camp, applying its own blocks (story locks, night mode, etc.). We
-- raise the same event. Leaving uses the same toggle while in camp.
------------------------------------------------------------------------

local function in_camp(char)
    local ok, rows = pcall(function() return Osi.DB_PlayerInCamp:Get(char) end)
    if ok and rows and #rows > 0 then return true end
    -- the DB stores prefixed names; fall back to a scan
    ok, rows = pcall(function() return Osi.DB_PlayerInCamp:Get(nil) end)
    if ok and rows then
        for _, r in ipairs(rows) do if G.same(r[1], char) then return true end end
    end
    return false
end
A.in_camp = in_camp

local function camp_travel(kind, want_in_camp)
    return function(args)
        local char = G.actor(args.char or G.host())
        for _, p in ipairs(G.party()) do
            if G.in_combat(p) then G.refuse("cannot do that during combat") end
            if G.in_dialogue(p) then G.refuse("cannot do that during a conversation") end
        end
        if in_camp(char) == want_in_camp then
            G.refuse(want_in_camp and "already at camp" or "not at camp")
        end
        return start(kind, char, {
            timeout_ms = 30000,
            interrupt = { combat = true, dialogue = false, attacked = true },
            osi = {
                TeleportedToCamp = function(job, a)
                    if want_in_camp and G.is_party(a[1]) then RPC.finish_job(job, "done", { at_camp = true }) end
                end,
                TeleportedFromCamp = function(job, a)
                    if not want_in_camp and G.is_party(a[1]) then RPC.finish_job(job, "done", { at_camp = false }) end
                end,
            },
            start = function(job) G.osi("TeleportToFromCamp", char) end,
            on_tick = function(job)
                if in_camp(char) == want_in_camp then
                    RPC.finish_job(job, "done", { at_camp = want_in_camp })
                elseif RPC.now_ms() - job.started > 12000 then
                    RPC.finish_job(job, "failed", nil, "cannot travel right now")
                end
            end,
        })
    end
end

reg("go_to_camp", camp_travel("go_to_camp", true))
reg("leave_camp", camp_travel("leave_camp", false))

------------------------------------------------------------------------
-- Click-on-entity flows: act.loot_corpse / act.lockpick / act.disarm / act.pickpocket
--
-- The game starts these from a *click*: the client sends an interaction
-- request, the engine raises RequestCanLoot / RequestCanLockpick(char, item,
-- requestId) / RequestCanDisarmTrap, the story answers (LootRequestProcessed /
-- RequestProcessed(char, requestId, result)), and the engine then opens the
-- loot UI or the roll. The requestId belongs to the engine, so lockpicking and
-- disarming cannot be started from script: these ops walk the actor next to
-- the target, select it, and finish "done" with
--   {needs_click = uuid, expect = "container"|"roll"|"pickpocket", button = "L"}
-- for the bridge (bridge/src/bg3ai/clicker.py) to click and verify.
-- Exceptions with an engine path (no pixels):
--   * pickpocket: raise RequestPickpocket(char, npc), the engine's own event;
--     the story answers StartPickpocket(char, npc, 1|0) (_CRIME_CrimeTriggers).
--   * loot_corpse (when A.LOOT_ENGINE_SHORTCUT): OpenCharacterLootUI(char,
--     corpse), only when no story listener of RequestCanLoot would object
--     (no custom response, not an owned corpse, not Philgrave's lair).
------------------------------------------------------------------------

-- Verified live (Nautiloid): OpenCharacterLootUI makes the engine raise
-- RequestCanLoot itself (same story/crime handshake as a click), and
-- CharacterLootedCharacter fires when items are taken (Take All), as with a
-- click. The story-DB gate below stays as a conservative extra.
A.LOOT_ENGINE_SHORTCUT = true
-- No engine lockpick request: SE's esv::lock::LockpickingSystem.PickLockRequests is mapped 8 bytes off on
-- this build. Two pushes (live 12:2x, "0 -> 1 -> 2") wrote {capacity 2, size 2} over the game's own buffer
-- pointer (0x0000000200000002); the game's next real request (the right-click at 12:35) grew the array and
-- freed that pointer: SIGSEGV bg3+0x218f104 (core 1137503, impl-se 2026-10-10 crash analysis). Never write
-- engine system arrays through Ext.System. The right-click menu below is the S024 07:23 hand route.

-- Walk next to `target` (verified approach point; in combat within the
-- remaining Movement, which the walk is charged for), select `char`, then
-- call finish(job).
local function approach(kind, char, target, finish, extra)
    G.within(char, target, G.MAX_INTERACT_DIST, "target")
    local plan = G.approach_point(char, target, A.USE_REACH)
    local budget, mv_before = combat_budget(char, plan.length_m)
    local spec = {
        eta_s = walk_eta(plan.length_m) + 0.5, route_m = plan.length_m,
        timeout_ms = 30000 + walk_timeout(plan.length_m),
        interrupt = { combat = budget == nil, dialogue = true, attacked = budget == nil },
        osi = with_walk(extra and extra.osi),
        start = function(job)
            begin_approach(job, target, plan, A.USE_REACH, function(j)
                pcall(G.osi, "MakePlayerActive", char)
                finish(j)
            end, budget)
        end,
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if extra and extra.on_tick then extra.on_tick(job) end
        end,
        on_finish = function(job) charge_movement(job, char, mv_before) end,
    }
    return start(kind, char, spec)
end

local function needs_click(job, target, expect, menu)
    RPC.finish_job(job, "done", { needs_click = target, expect = expect, button = menu and "R" or "L", menu = menu })
end

-- Read-only follow-up for the bridge after a click: what a player sees on the
-- target (locked padlock icon, open/closed, trap marker of a discovered trap).
RPC.register("act.target_state", function(args)
    local t = G.guid(args.uuid)
    if t and G.b(G.q("IsItem", t)) then
        local h = holder(t)
        if h and G.is_party(h) then return { visible = true, held = true } end
    end
    if not t or not G.visible(t) then return { visible = false } end
    local out = { visible = true }
    if G.b(G.q("IsItem", t)) then
        local h = holder(t)
        if h and G.is_party(h) then out.held = true end
        out.locked = G.b(G.q("IsLocked", t))
        out.opened = G.b(G.q("IsOpened", t))
        if G.b(G.q("IsTrapDiscovered", t)) then out.trap_armed = G.b(G.q("IsTrapArmed", t)) end
    else
        out.dead = G.is_dead(t)
    end
    return out
end)

reg("loot_corpse", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local corpse = G.guid(args.corpse or args.target) -- bridge sends {target}
    if not corpse or not G.b(G.q("IsCharacter", corpse)) then G.refuse("that is not a body") end
    if not G.is_dead(corpse) then G.refuse("they are not dead") end
    if G.is_party(corpse) then G.refuse("cannot loot a party member") end
    G.target_visible(corpse)
    local story_cares = G.db_has("DB_CustomLootCorpseResponse", 3, char, corpse)
        or G.db_has("DB_OwnedCorpse", 2, corpse)
        or G.db_has("DB_LOW_PhilgravesMansion_LairEntities", 1, corpse)
    local engine = A.LOOT_ENGINE_SHORTCUT and not story_cares and not G.in_combat(char)
    return approach("loot_corpse", char, corpse, function(job)
        if not engine then return needs_click(job, corpse, "container") end
        local ok = G.osi("OpenCharacterLootUI", char, corpse)
        job.opened_at = RPC.now_ms()
        if ok == false then RPC.finish_job(job, "failed", nil, "cannot loot that") end
    end, {
        osi = {
            CharacterLootedCharacter = function(job, a)
                if G.same(a[1], char) and G.same(a[2], corpse) then job.looted_event = true end
            end,
        },
        on_tick = function(job)
            if job.opened_at and RPC.now_ms() - job.opened_at > 300 then
                RPC.finish_job(job, "done", { opened = "container", via = "engine",
                    looted_event = job.looted_event or false })
            end
        end,
    })
end)
listen("CharacterLootedCharacter", 2)

reg("lockpick", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local target = G.guid(args.target)
    if not target or not G.b(G.q("IsItem", target)) then G.refuse("that cannot be lockpicked") end
    G.target_visible(target)
    if not G.b(G.q("IsLocked", target)) then G.refuse("it is not locked") end
    local h = G.party_key(target, char)
    if h then
        local ok, n = pcall(function() return BG3AI.Visibility.name(h) end)
        G.refuse((ok and n or "a party member") .. " carries its key: use interact on it (opens it with the key)")
    end
    if G.lock_pickable(target) == false then
        G.refuse("this lock cannot be picked (the game offers no lockpick roll for it, only 'locked'): "
            .. "it opens with its key or a mechanism nearby (lever, button, puzzle), or force it if it is not Sturdy")
    end
    return approach("lockpick", char, target, function(job) needs_click(job, target, "roll", "Lockpick") end)
end)
listen("StartedLockpicking", 2)

reg("disarm", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local trap = G.guid(args.trap or args.target)
    if not trap or not G.b(G.q("IsItem", trap)) then G.refuse("that is not a trap") end
    -- Only traps the party has discovered (passive Perception / search).
    if not G.b(G.q("IsTrap", trap)) or not G.b(G.q("IsTrapDiscovered", trap)) then
        G.refuse("no known trap there")
    end
    G.target_visible(trap)
    return approach("disarm", char, trap, function(job) needs_click(job, trap, "roll") end)
end)

reg("pickpocket", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local npc = G.guid(args.npc or args.target)
    if not npc or not G.b(G.q("IsCharacter", npc)) or G.is_dead(npc) then G.refuse("nobody to pickpocket") end
    if G.is_party(npc) then G.refuse("cannot pickpocket a party member") end
    G.target_visible(npc)
    return approach("pickpocket", char, npc, function(job)
        job.requested_at = RPC.now_ms()
        G.osi("RequestPickpocket", char, npc)
    end, {
        on_tick = function(job)
            -- Server-side sign of the pickpocket screen: the ServerPickpocket
            -- component on the actor (UNVERIFIED); otherwise hand over to the
            -- bridge to confirm with ui.state.
            if not job.requested_at then return end
            local e = G.entity(char)
            if e and U.comp(e, "ServerPickpocket") then
                RPC.finish_job(job, "done", { opened = "pickpocket", via = "engine" })
            elseif RPC.now_ms() - job.requested_at > 1500 then
                RPC.finish_job(job, "done", { requested = true, expect = "pickpocket", verify_ui = true })
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.watch {radius?}: one cheap read for the bridge's action queue (its
-- interrupt checks) and the do_actions diff. Only what the player sees: the
-- party's portraits (pos, HP, downed, a harmful surface underfoot), the mode (combat / conversation), and
-- the characters in view (visibility-filtered, as in state.nearby).
-- act.note {kind, data}: the bridge's queue reports (queue_*) go into the
-- same event stream as the game's events.
------------------------------------------------------------------------

A.WATCH_RADIUS = 30
RPC.register("act.watch", function(args)
    local radius = tonumber(args.radius) or A.WATCH_RADIUS
    local out = { party = {}, seen = {}, combat = false, dialogue = false }
    local centers = {}
    for _, u in ipairs(G.party()) do
        local row = { uuid = u, pos = pos_view(G.pos(u)) }
        local e = G.entity(u)
        local h = e and U.comp(e, "Health")
        if h then pcall(function() row.hp = h.Hp; row.max_hp = h.MaxHp end) end
        if G.is_downed(u) then row.downed = true end
        -- a harmful surface underfoot the player sees (fire, acid, ...): the queue's "surface" interrupt
        local N = BG3AI.Nav
        if row.pos and type(N) == "table" and type(N.surfaceAt) == "function" then
            local ok, sf = pcall(N.surfaceAt, G.pos(u))
            if ok and sf then row.surface = sf end
        end
        if G.in_combat(u) then out.combat = true end
        if G.in_dialogue(u) then out.dialogue = true end
        if row.pos then centers[#centers + 1] = row.pos end
        out.party[#out.party + 1] = row
    end
    local V = BG3AI.Visibility
    if #centers == 0 or type(V) ~= "table" or not V.context or not V.characterVisible or not V.charactersNear then return out end
    local ctx = V.context()
    local host = G.host() or G.party()[1]
    -- the per-frame shared scan (state.nearby in the same eval reuses it)
    for _, c in ipairs(V.charactersNear(centers, radius)) do
        local e, p = c[1], c[2]
        local u = V.entityUuid(e)
        if u and not ctx.is_party[u] and V.characterVisible(ctx, u, e, p) then
            local row = { uuid = u, name = V.name and V.name(u) or nil }
            if G.is_dead(u) then row.dead = true
            elseif host and G.b(G.q("IsEnemy", host, u)) then row.hostile = true end
            out.seen[#out.seen + 1] = row
        end
    end
    return out
end)

RPC.register("act.note", function(args)
    local kind = tostring(args.kind or "")
    if not kind:match("^queue_[%w_]+$") then error("act.note: only queue_* events") end
    RPC.push(kind, args.data)
    return { ok = true }
end)

return A
