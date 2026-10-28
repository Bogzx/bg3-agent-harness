-- Actions, part 2 of 6 (Actions.lua loads the parts in order; split for the 1000-line cap):
-- act.move_to (walking, routes, teleport settling, pressure plates) and act.stop.
local U = BG3AI.Util
local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = BG3AI.Actions
local I = A.inner
local reg, interrupt, ev_name, pos_view, walk_eta = I.reg, I.interrupt, I.ev_name, I.pos_view, I.walk_eta
local start, osi_tasks = I.start, I.osi_tasks

------------------------------------------------------------------------
-- act.move_to {char, target | pos, run=true, within?, range?}  (within: a melee reach, range: a ranged spell's
-- reach (centres); both target only)
------------------------------------------------------------------------

-- move_to {target, range}: a ranged spell's reach (centres; app review of #37: a Scroll of Fireball read 20 m from a
-- goblin walked the reader to the 2.2 m approach ring, inside its own blast). The walk ends on the way in, at a
-- reachable floor point `reach` m from the target: on the line toward the walker, then 22.5 and 45 degrees off it.
A.RANGE_STOP_SLACK = 1.0
local function range_point(char, target, reach, range)
    local a, t = G.pos(char), G.pos(target)
    if not a or not t then G.refuse("cannot locate target") end
    local d = U.dist(a, t)
    if d <= range then return { here = true, dist = d, length_m = 0, tpos = t } end
    local base = math.atan(a[3] - t[3], a[1] - t[1])
    for _, k in ipairs({ 0, 1, -1, 2, -2 }) do
        local ang = base + k * math.pi / 8
        local x, z = t[1] + reach * math.cos(ang), t[3] + reach * math.sin(ang)
        local y = G.floor_at(x, z, t[2])
        if y then
            local ok, info = G.reachable(char, { x, y, z })
            if ok and not info.needs_jump then
                return { pos = { x, y, z }, length_m = info.length_m or U.dist(a, { x, y, z }), dist = d, tpos = t }
            end
        end
    end
    G.refuse(string.format("no walkable point %.1f m from the target on this side (its range)", reach))
end

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
        last_pos = G.pos(job.char), y0 = (G.pos(job.char) or {})[2], still_ms = 0, travelled = 0,
        tolerance = opts.tolerance or 1.5, reach = opts.reach, melee = opts.melee, on_arrive = opts.on_arrive,
        move_id = opts.move_id or job.id, combat = G.in_combat(job.char),
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
    -- naut S001 16:37 (Nautiloid helm): the walk to the second ladder went back down the first one and up again, 6
    -- times, before the loop watchdog stopped it. No route climbs a ladder and comes back down it: the first
    -- reversal on the same ladder during one job's walks stops it and names the climb to do first. Heights are
    -- compared, not UseFinished counts (one climb fires several, live 14:1x).
    UseFinished = function(job, a)
        local w = job.walk
        if not w or w.done or not G.same(a[1], job.char) then return end
        local item = G.guid(a[2])
        if not item or G.same(item, w.target) or (job.ladder and G.same(item, job.ladder.item)) then return end
        local isl = false
        pcall(function() isl = G.entity(item).ServerItem.IsLadder or false end)
        local here = isl and G.pos(job.char)
        if not here then return end
        job.climbs = job.climbs or {}
        local c = job.climbs[item]
        if not c then c = { y = w.y0 or here[2] }; job.climbs[item] = c end
        local dy = here[2] - c.y
        if math.abs(dy) < A.LADDER_DY then return end
        local dir = dy > 0 and "up" or "down"
        if c.dir and c.dir ~= dir then
            RPC.push("loop_stopped", { uuid = job.char, item = item, reversal = true, job = job.id })
            interrupt(job, "stuck: the walk took ladder " .. item .. " " .. c.dir .. " and then back " .. dir
                .. " (the game's route loops through it); climb it yourself first (interact " .. item
                .. "), then send this again from that floor, or route")
            return
        end
        c.dir, c.y = dir, here[2]
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

-- NAV-8: jobs tick every TICK_MS (50 ms; 50-66 ms at 30-60 fps), so a 2-3.9 m jump between two ticks (a run
-- does ~0.4 m) passed both old tests (> 4 m, or > 12 m/s over >= 100 ms). A step over TELEPORT_SMALL_M within a short tick is
-- judged at TELEPORT_MIN_DT_MS (a frame hitch that long is a long dt, the speed test). Not undone: TeleportToPosition
-- is on G.BLOCK (no harness teleports); the job fails and the teleport_blocked event reaches the runner's log.
A.TELEPORT_SMALL_M = 1.5
A.TELEPORT_MIN_DT_MS = 50
-- Who made the jump. The engine's no-path fallback puts the walker AT our destination; a story script (an arena
-- pull, a trial's put-back, a ritual drop) puts it somewhere else (S063 Shadowheart 22 m below her move's point,
-- S036 21 m off) or starts its scene right then (S066 Orin: at the spot, a quest update with it, her conversation
-- 5 s later). A jump over TELEPORT_GAME_M that lands farther than TELEPORT_DEST_M from where we walked is the
-- game's at once; any other waits TELEPORT_SETTLE_MS for such a signal (TELE_SIGNALS) before it is called the
-- engine's (a short NAV-8 jump mid-walk stays ours). Nothing is undone either way.
A.TELEPORT_DEST_M = 3.0
A.TELEPORT_GAME_M = 8.0
A.TELEPORT_SETTLE_MS = 1500
-- A fast travel's arrival (the bridge calls act.trip_arrived when ui.fast_travel is done). S073 22:33:39: 9 s after
-- the arrival at Lower City Central Wall, 19 m into a walk from the waypoint, Claude jumped the last 29.8 m (7.8 m
-- down) onto the walk's own end (dest_m 0) with no story signal, while the save menus were up. A jump in the first
-- TRIP_SETTLE_MS after an arrival is the game's: onto the walk's end the walk is done (event moved_after_trip),
-- elsewhere it is interrupted like a story move.
A.TRIP_SETTLE_MS = 15000
A.trip_t = nil

RPC.register("act.trip_arrived", function()
    A.trip_t = RPC.now_ms()
    return { t = A.trip_t }
end)

-- ms from the last arrival to t, or nil when there was none in the TRIP_SETTLE_MS before t.
local function trip_age(t)
    local age = A.trip_t and t - A.trip_t
    if age and age >= 0 and age <= A.TRIP_SETTLE_MS then return age end
end

-- why = the story signal (the game moved the character: interrupted, event teleported_by_game), nil = the
-- engine's own teleport of our walk (failed, event teleport_blocked, as before), unless it came right after a
-- fast travel (trip_age).
function A.tele_finish(job, why)
    local tp = job.tele
    job.tele = nil
    if not tp or job.status ~= "running" then return end
    local here = G.pos(job.char) or tp.to
    local ev = { uuid = job.char, from = pos_view(tp.from), to = pos_view(here), m = U.round(tp.m, 1) }
    local res = { pos = pos_view(here), teleported = true, from = pos_view(tp.from), m = U.round(tp.m, 1),
        dest_m = tp.dest_m and U.round(tp.dest_m, 1) or nil }
    local trip = not why and trip_age(tp.t)
    if trip then
        local s = U.round(trip / 1000, 1)
        -- a plain walk put on its own end: arrived (an approach's action after it is not done: interrupted below)
        if tp.dest_m and tp.dest_m <= A.TELEPORT_DEST_M and job.walk and not job.walk.on_arrive then
            ev.after_trip_s, res.after_trip_s, res.by_trip, res.dist = s, s, true, U.round(tp.dest_m, 1)
            RPC.push("moved_after_trip", ev)
            RPC.finish_job(job, "done", res)
            return
        end
        why = string.format("%.0f s after the fast travel", trip / 1000)
    end
    if why then
        ev.why, res.by_game, res.why = why, true, why
        RPC.push("teleported_by_game", ev)
        RPC.finish_job(job, "interrupted", res, "the game moved the character (" .. why .. "), not our walk")
    else
        RPC.push("teleport_blocked", ev)
        RPC.finish_job(job, "failed", res, "engine teleported the character (not allowed)")
    end
end

function A.tele_settle(job)
    if job.tele and RPC.now_ms() - job.tele.t >= A.TELEPORT_SETTLE_MS then A.tele_finish(job, nil) end
end

-- BACKLOG:1743, live N8 step 3 (2026-10-28): Claude's move_to ended at +0.87 s, Claude stood still, and at +4.52 s the
-- game put Claude 13.5 m off in one 77 ms tick (the Faith-Leap put-back): no job ran, so teleport_check never saw it.
-- For IDLE_WATCH_MS after one of our jobs for a member ends, a jump over IDLE_TELE_M in one tick with no job of
-- ours running for them is the game's: event teleported_by_game. Not when another member jumps in the same tick
-- (a fast travel or a load moves the whole party), while a party-wide job runs (camp), or after a fast travel.
A.IDLE_WATCH_MS = 10000
A.IDLE_TELE_M = 4.0
A.idle_watch = {} -- char -> { t = the job's end, kind }
A.idle_last = {}  -- party member -> position at the last tick, while a watch runs
-- app review of #34: a scroll's interact ends with its spell in hand, before the bridge's target click, so the
-- agent's own Misty Step / Dimension Door (or a teleport cast by click/hotbar) landed after the job. A jump while
-- the member has a read scroll pending (A.scroll_pending) or within IDLE_CAST_MS of their own CastSpell/CastedSpell
-- is their spell, not the game's.
A.IDLE_CAST_MS = 5000
A.cast_ms = {}    -- party member -> the time of their last CastSpell/CastedSpell

function A.note_cast(caster)
    local g = G.guid(caster)
    if g then A.cast_ms[g] = RPC.now_ms() end
end

function A.watch_idle(char, kind)
    A.idle_watch[char] = { t = RPC.now_ms(), kind = kind }
    A.idle_last[char] = G.pos(char) -- from where the job left it (a jump that ended the job was the job's)
end

function A.idle_tick()
    if next(A.idle_watch) == nil then
        A.idle_last = {}
        return
    end
    local now = RPC.now_ms()
    for char, w in pairs(A.idle_watch) do
        local j = A.active[char]
        if (j and j.status == "running") or now - w.t > A.IDLE_WATCH_MS then A.idle_watch[char] = nil end
    end
    local wide = false
    for _, job in pairs(RPC.jobs) do
        if job.status == "running" and job.act and not job.char then wide = true end
    end
    local jumped, last = {}, {}
    for _, u in ipairs(G.party()) do
        local here = G.pos(u)
        local prev = A.idle_last[u]
        if here and prev and U.dist(prev, here) > A.IDLE_TELE_M then
            jumped[#jumped + 1] = { u = u, from = prev, to = here, m = U.dist(prev, here) }
        end
        last[u] = here
    end
    A.idle_last = last
    if #jumped ~= 1 or wide or trip_age(now) then return end
    local jp = jumped[1]
    local w = A.idle_watch[jp.u]
    if not w then return end
    A.idle_watch[jp.u] = nil
    local cast = A.cast_ms[jp.u]
    if (A.scroll_pending or {})[jp.u] or (cast and now - cast <= A.IDLE_CAST_MS) then return end
    RPC.push("teleported_by_game", { uuid = jp.u, from = pos_view(jp.from), to = pos_view(jp.to),
        m = U.round(jp.m, 1), why = string.format("%.1f s after its %s ended, with no order of ours running",
            (now - w.t) / 1000, w.kind) })
end

if not A.idle_subscribed then
    A.idle_subscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(A.idle_tick)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] idle teleport watch: " .. tostring(err)) end
    end)
end

local function teleport_check(job, track, armed)
    local here = G.pos(job.char)
    if not here then return nil end
    local t = RPC.now_ms()
    local prev, dt = track.last_pos, t - (track.last_t or t)
    local step = prev and U.dist(prev, here) or 0
    local fast = dt >= 100 and step / (dt / 1000) > 12.0
        or (step > A.TELEPORT_SMALL_M and dt < 100 and step / (math.max(dt, A.TELEPORT_MIN_DT_MS) / 1000) > 12.0)
    if prev and (step > 4.0 or fast)
        and (not armed or armed(prev)) and not on_route(job, track, prev, here) then
        -- where this walk was going (none once it has ended: Use/Pickup's own walk from the approach point)
        local dest = job.walk and not job.walk.done and job.walk.pos
        if job.walk then job.walk.done = true end
        pcall(G.osi, "PurgeOsirisQueue", job.char)
        local dest_m = dest and U.dist(here, dest) or nil
        job.tele = { from = prev, to = here, m = step, t = t, dest_m = dest_m }
        if dest_m and dest_m > A.TELEPORT_DEST_M and step > A.TELEPORT_GAME_M then
            A.tele_finish(job, string.format("it landed %.0f m from where it was walking", dest_m))
        end
        return nil
    end
    track.last_pos, track.last_t = here, t
    return here, step, dt
end

-- S074 23:21:13 (bg3le 26405.1-26410.2): Claude's round-2 move (12 m route, in budget) stood still 5.1 s and came
-- back "no path to the destination" while his turn had passed to Cazador (he began it Lethargic: his Haste had just
-- ended); the plan's next step only then found the turn gone. A walk begun in combat stops at once when its turn has
-- passed (WALK_TURN_GONE_TICKS in a row, so a flag flipping as the fight ends is not a stop), and one that never
-- moves fails after WALK_NO_START_COMBAT_MS (the cast's no-start window is 3 s) naming what stops the walker.
A.WALK_TURN_GONE_TICKS = 3
A.WALK_NO_START_COMBAT_MS = 2500
local function combat_walk_stop(job)
    local w = job.walk
    local function stop(why)
        w.done = true
        pcall(G.osi, "PurgeOsirisQueue", job.char)
        local blk = G.blocking_status(job.char, true)
        RPC.finish_job(job, "failed", job.partial, why .. (blk and ("; the character is " .. tostring(blk.name))
            or ""))
        return true
    end
    if not G.in_combat(job.char) then return false end -- the fight ended mid-walk: an ordinary walk now
    local active = G.tb_flags(job.char)
    if active == false or G.turn_is_over(job.char) then
        w.gone_ticks = (w.gone_ticks or 0) + 1
        if w.gone_ticks >= A.WALK_TURN_GONE_TICKS then
            return stop(string.format("not this character's turn any more: the game passed the turn on during the "
                .. "walk (travelled %.1f m)", w.travelled))
        end
    else
        w.gone_ticks = 0
    end
    if w.travelled < 0.05 and w.still_ms >= A.WALK_NO_START_COMBAT_MS then
        local d, _, de = walk_dist_left(job)
        if not (walk_close_enough(job, d, de) or (d and d <= w.tolerance)) then
            return stop(string.format("never started moving (%.1f s, the route was there)", w.still_ms / 1000)
                .. A.held_text(job.char))
        end
    end
    return false
end

-- Bench act3_gold_max 12:50:50 (bg3le 82620.6-82629.8): Shadowheart's last leg back to the leader, a 1 m route to a
-- point 0.3 m from the one Karlach was walking to, ran 31.8 m round the others without getting there until the job's
-- 9.3 s deadline (move_to TIMEOUT, the run voided). Only standing still or a teleport stopped a walk. A move_to
-- walker that has run WALK_ROUND_X times its route plus WALK_ROUND_M and is still not there is stopped and fails.
A.WALK_ROUND_X = 2.0
A.WALK_ROUND_M = 10.0

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
    if w.combat and combat_walk_stop(job) then return end
    if job.round_cap_m and w.travelled > job.round_cap_m then
        local d, _, de = walk_dist_left(job)
        if not walk_close_enough(job, d, de) then
            w.done = true
            pcall(G.osi, "PurgeOsirisQueue", job.char)
            RPC.finish_job(job, "failed", job.partial, string.format("could not get closer to the destination: walked "
                .. "%.1f m on a %.1f m route and is still %.1f m off (the game walks round someone or something there)",
                w.travelled, job.route_m or 0, d or 0))
            return
        end
    end
    if w.still_ms >= 5000 then
        local d, _, de = walk_dist_left(job)
        if walk_close_enough(job, d, de) or (d and d <= w.tolerance) then
            walk_arrived(job)
        else
            w.done = true
            pcall(G.osi, "PurgeOsirisQueue", job.char)
            RPC.finish_job(job, "failed", job.partial, "no path to the destination" .. A.held_text(job.char))
        end
    end
end

-- Combat: the walk must fit in the remaining Movement. Returns budget,
-- movement before (both nil out of combat) or refuses.
local function combat_budget(char, route)
    if not G.in_combat(char) then return nil, nil end
    local blk = G.move_blocked(char)
    if blk then G.refuse("cannot move while " .. tostring(blk) .. " (the status keeps it in place this turn)") end
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

-- Floor plates (vis V25, S084 04:16-04:22, the Counting House High Security vault: nine Marble Plates, code 1-3-5-6).
-- Every click and move_to crossed other plates and reset the code ("Reset (red)"): 50 hand calls, never entered.
-- A player walks round the plates they can see and steps onto the one they mean. Known plates are the floor plates
-- the party has seen on this level (V.fixtureSeen: a listing showed them), never an unseen one.
-- move_to(target=<plate>) stands on the plate's centre; an out-of-combat move_to whose engine route crosses a known
-- plate walks via a point whose two legs cross none, else walks as before and names the plates it crosses.
A.PLATE_RADIUS_M = 0.8  -- a plate whose AI bounds can't be read
A.PLATE_CLEAR_M = 0.4   -- a route this far past a plate's edge doesn't step on it (the walker's own footprint)
A.PLATE_NEAR_M = 40     -- plates farther than this from both ends of the walk are not checked
A.PLATE_VIA_TRIES = 12  -- detour points tried, the shortest first (up to 2 path searches each)

local function dist_xz(a, b) return math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2) end

function A.is_plate(u)
    local V = BG3AI.Visibility
    return u ~= nil and V ~= nil and V.PLATE_NAMES ~= nil and V.PLATE_NAMES[V.name(u) or ""] == true
end

local function plate_radius(u)
    local r
    pcall(function()
        local b = BG3AI.Visibility.bounds(G.entity(u))
        r = b and b.radius
    end)
    if type(r) ~= "number" or r <= 0 then return A.PLATE_RADIUS_M end
    return math.min(math.max(r, 0.4), 1.6)
end

-- Seen floor plates on this level near a walk from `from` to `to`: { {uuid, pos, r}, ... }.
function A.known_plates(from, to)
    local V, out = BG3AI.Visibility, {}
    if not (V and V.fixtureSeen and V.levelNow) then return out end
    local level, now = V.levelNow(), Ext.Utils.MonotonicTime()
    for u, m in pairs(V.fixtureSeen) do
        if m.level == level and now - m.t <= V.FIXTURE_REMEMBER_MS and A.is_plate(u) then
            local p = G.pos(u)
            if p and (U.dist(p, from) <= A.PLATE_NEAR_M or U.dist(p, to) <= A.PLATE_NEAR_M) then
                out[#out + 1] = { uuid = G.guid(u) or u, pos = p, r = plate_radius(u) }
            end
        end
    end
    return out
end

-- Distance (x/z) from p to the segment a-b, and the segment's height there.
local function seg_dist_xz(p, a, b)
    local vx, vz = b[1] - a[1], b[3] - a[3]
    local l2 = vx * vx + vz * vz
    local t = l2 > 0 and math.max(0, math.min(1, ((p[1] - a[1]) * vx + (p[3] - a[3]) * vz) / l2)) or 0
    local x, z = a[1] + vx * t, a[3] + vz * t
    return math.sqrt((p[1] - x) ^ 2 + (p[3] - z) ^ 2), a[2] + (b[2] - a[2]) * t
end

-- The plates (not in skip) a route of points steps on, in route order. Also takes off-limits areas (pl.dy: their
-- height band; a plate's is 1.5 m).
function A.plates_on(points, plates, skip)
    local hit, seen = {}, {}
    if type(points) ~= "table" or #points == 0 then return hit end
    for i = 1, math.max(1, #points - 1) do
        local a, b = points[i], points[i + 1] or points[i]
        for _, pl in ipairs(plates) do
            if not skip[pl.uuid] and not seen[pl.uuid] then
                local d, y = seg_dist_xz(pl.pos, a, b)
                if d <= pl.r + A.PLATE_CLEAR_M and math.abs(y - pl.pos[2]) <= (pl.dy or 1.5) then
                    seen[pl.uuid] = true
                    hit[#hit + 1] = pl
                end
            end
        end
    end
    return hit
end

-- A point to walk via so that neither leg steps on a known plate (skip: plates that may be stepped on: the target,
-- the one the character stands on). Returns false (the direct route is clear), or via, or nil + the plates the
-- direct route crosses (no clear detour found within PLATE_VIA_TRIES).
function A.plate_detour(char, from, dest, plates, skip)
    local N = BG3AI.Nav
    if not (N and N.route and N.routeFrom) then return false end
    local crossed = A.plates_on(N.route(char, dest), plates, skip)
    if #crossed == 0 then return false end
    -- Points round every known plate, not only the crossed one: a grid of plates is walked round at its edge.
    -- An off-limits area (vis V26, 6 m) also gets wider rings: from one side to the other of a disc that big, a
    -- point at its edge leaves a leg through it. Points whose straight legs clear everything are tried first.
    local cands = {}
    for _, pl in ipairs(plates) do
        if not skip[pl.uuid] then
            local rr = pl.r + A.PLATE_CLEAR_M + 0.6
            for _, ring in ipairs(pl.area and { rr, rr * 1.5, rr * 2 } or { rr }) do
                for k = 0, 7 do
                    local a = k * math.pi / 4
                    local c = { pl.pos[1] + ring * math.cos(a), pl.pos[2], pl.pos[3] + ring * math.sin(a) }
                    local free = true
                    for _, q in ipairs(plates) do
                        if not skip[q.uuid] and dist_xz(c, q.pos) <= q.r + A.PLATE_CLEAR_M then free = false break end
                    end
                    if free then
                        local clear = #A.plates_on({ from, c }, plates, skip) == 0
                            and #A.plates_on({ c, dest }, plates, skip) == 0
                        cands[#cands + 1] = { pos = c, cost = U.dist(from, c) + U.dist(c, dest), clear = clear }
                    end
                end
            end
        end
    end
    table.sort(cands, function(x, y)
        if x.clear ~= y.clear then return x.clear end
        return x.cost < y.cost
    end)
    for i = 1, math.min(#cands, A.PLATE_VIA_TRIES) do
        local c = cands[i].pos
        local r1 = N.route(char, c)
        if r1 and #A.plates_on(r1, plates, skip) == 0 then
            local r2 = N.routeFrom(char, c, dest)
            if r2 and #A.plates_on(r2, plates, skip) == 0 then return c, crossed end
        end
    end
    return nil, crossed
end

-- Off-limits areas (vis V26, S084 04:08-04:12, the Counting House): a guard let the party off "with a warning.
-- This time.", then a travel_to walked into the guards' off-limits area and the second offence started a fight.
-- The bridge remembers where each trespass talk began once the game's own Narrator line said the area was
-- off-limits (offlimits.py: fair, only after the warning was shown) and passes them as args.avoid
-- ({id, pos, r, level, name}). Out of combat a walk never goes in: the way round it (as round the plates), else a
-- refusal naming it; a destination inside is refused. trespass=true walks in anyway; an area the walker stands
-- in doesn't count (walking out of it is right).
A.AREA_DY_M = 4.0  -- an area covers its own floor: the floors above and below are other rooms

local function in_area(p, a)
    return p ~= nil and dist_xz(p, a.pos) <= a.r and math.abs(p[2] - a.pos[2]) <= A.AREA_DY_M
end

-- args.avoid on this level near a walk from `from` to `to`, minus the ones `from` is in: obstacle rows.
function A.warned_areas(avoid, from, to)
    local V, out = BG3AI.Visibility, {}
    if type(avoid) ~= "table" or not from then return out end
    local level = V and V.levelNow and V.levelNow()
    for i, a in ipairs(avoid) do
        local p, r = type(a) == "table" and a.pos, type(a) == "table" and tonumber(a.r)
        if type(p) == "table" and #p >= 3 and r and r > 0 and (a.level == nil or a.level == level) then
            local row = { uuid = "area:" .. tostring(a.id or i), pos = p, r = r, dy = A.AREA_DY_M,
                area = a.name or "an off-limits area" }
            if not in_area(from, row) and (U.dist(p, from) <= A.PLATE_NEAR_M + r or U.dist(p, to) <= A.PLATE_NEAR_M + r) then
                out[#out + 1] = row
            end
        end
    end
    return out
end

local function area_names(rows)
    local out, seen = {}, {}
    for _, a in ipairs(rows) do
        if a.area and not seen[a.area] then seen[a.area] = true; out[#out + 1] = a.area end
    end
    return table.concat(out, "; ")
end

reg("move_to", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local target = args.target and G.guid(args.target)
    local pos = args.pos
    local route, melee, reach
    local plate = target and A.is_plate(target) and target or nil
    if plate then
        -- A floor plate: stand on its centre, as a player's click on it does (the ring point next to it is off it).
        G.target_visible(target)
        G.within(char, target, G.MAX_MOVE_DIST, "destination")
        pos = G.pos(target)
        local here = G.pos(char)
        local r = plate_radius(target)
        if here and pos and dist_xz(here, pos) <= r * 0.6 then
            return start("move_to", char, {
                timeout_ms = 1000,
                start = function(job)
                    RPC.finish_job(job, "done", { pos = pos_view(here), on_plate = target, already_there = true,
                        dist = U.round(dist_xz(here, pos), 1) })
                end,
            })
        end
        route = G.point_reachable(char, pos).length_m or U.dist(here, pos)
    elseif target then
        -- Entity targets: walk to a verified reachable point next to them.
        G.target_visible(target)
        G.within(char, target, G.MAX_MOVE_DIST, "destination")
        -- within (melee, combat_turn): stop inside the guard's own edge-to-edge reach, not at 2.2 m
        -- centres (S023-S025: 22 melee attacks refused "out of range (2.5 m, range 1.5 m)" after a move
        -- said done; a Spiritual Weapon is small, so 2.2-2.7 m was outside its reach).
        melee = tonumber(args.within) and G.melee_reach(char, target, tonumber(args.within)) or nil
        reach = melee and math.max(1.2, math.min(A.MOVE_REACH, melee - A.MELEE_STOP_SLACK)) or A.MOVE_REACH
        local range = not melee and tonumber(args.range)
        if range then -- in_reach is tested against the range itself (centres, walk_arrived)
            -- live N10 step 2: the game ended a touch walk (reach 2.5 m, centres) 1.8 m short of its point, 3.3 m off:
            -- aim RANGE_STOP_SLACK inside the reach whatever its size (never nearer than 1 m), as spell_reach measures
            melee, reach = range, math.max(1.0, range - A.RANGE_STOP_SLACK)
        end
        local plan = range and range_point(char, target, reach, range) or G.approach_point(char, target, reach)
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

    local via, crossed, areas
    if not combat then
        local from = G.pos(char)
        local plates = from and A.known_plates(from, pos) or {}
        areas = (not args.trespass) and A.warned_areas(args.avoid, from, pos) or {}
        for _, a in ipairs(areas) do
            if in_area(pos, a) then
                G.refuse("the destination is inside " .. a.area .. " you were warned about (a second offence"
                    .. " there can start a fight): talk to its guard first (a pass, a check), or trespass=true to"
                    .. " walk in anyway")
            end
            plates[#plates + 1] = a
        end
        if #plates > 0 then
            local skip = {}
            if plate then skip[plate] = true end
            for _, pl in ipairs(plates) do -- stepping off the plate the character stands on can't be helped
                if not pl.area and dist_xz(from, pl.pos) <= pl.r + A.PLATE_CLEAR_M then skip[pl.uuid] = true end
            end
            via, crossed = A.plate_detour(char, from, pos, plates, skip)
            if via then route = route + math.max(0, U.dist(from, via) + U.dist(via, pos) - U.dist(from, pos)) end
            local through = {}
            for _, pl in ipairs(crossed or {}) do if pl.area then through[#through + 1] = pl end end
            areas = through
            if via == nil and #through > 0 then
                G.refuse("the way there crosses " .. area_names(through) .. " you were warned about and no way"
                    .. " round it was found: go another way, talk to its guard first, or trespass=true to walk"
                    .. " through anyway")
            end
        end
    end
    local plate_r = plate and plate_radius(plate)
    local function on_plate(job)
        local here = G.pos(job.char)
        local d = here and pos and dist_xz(here, pos) or nil
        local res = { pos = pos_view(here), dist = d and U.round(d, 1) or nil }
        if d and d <= plate_r then
            res.on_plate = plate
            RPC.push("moved_done", { uuid = job.char, move_id = job.id })
            RPC.finish_job(job, "done", res)
        else
            res.reached = false
            RPC.finish_job(job, "failed", res, "stopped next to the plate, not on it (" .. (d and U.round(d, 1) or "?")
                .. " m from its centre)")
        end
    end
    local final = { pos = pos, target = not plate and target or nil, speed = speed, tolerance = plate and 0.5 or 1.5,
        reach = reach, melee = melee, on_arrive = plate and on_plate or nil }

    return start("move_to", char, {
        eta_s = walk_eta(route, speed), route_m = route,
        timeout_ms = walk_timeout(route),
        round_cap_m = A.WALK_ROUND_X * (route or 0) + A.WALK_ROUND_M,
        interrupt = { combat = not combat, dialogue = true, attacked = not combat },
        osi = WALK_OSI,
        start = function(job)
            if via then
                begin_walk(job, { pos = via, speed = speed, tolerance = 1.0, on_arrive = function(j)
                    begin_walk(j, final)
                    j.walk.budget = budget
                end })
            else
                begin_walk(job, final)
            end
            job.walk.budget = budget
        end,
        on_tick = walk_tick,
        on_finish = function(job)
            charge_movement(job, char, mv_before)
            if type(job.result) ~= "table" then return end
            if via then job.result.round_plates = pos_view(via) end
            if via and areas and #areas > 0 then job.result.avoiding = area_names(areas) .. " you were warned about" end
            if crossed and not via then
                local over = {}
                for _, pl in ipairs(crossed) do over[#over + 1] = pl.uuid end
                job.result.over_plates = over
            end
        end,
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
    A.drop_waiting(char, "stopped") -- orders that waited for a stand-up (Actions.lua start)
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

A.inner.with_walk, A.inner.teleport_check, A.inner.combat_budget = with_walk, teleport_check, combat_budget
A.inner.charge_movement, A.inner.walk_timeout = charge_movement, walk_timeout
A.inner.begin_approach, A.inner.approach_tick, A.inner.holder = begin_approach, approach_tick, holder
