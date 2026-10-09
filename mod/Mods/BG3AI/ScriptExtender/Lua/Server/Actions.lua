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
    AttackedBy = function(job, a)
        if job.interrupt.attacked and G.same(a[1], job.char) then
            interrupt(job, "attacked")
        end
    end,
}

function A.dispatch(event, args)
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
    spec.on_finish = function(job)
        if char and A.active[char] == job then A.active[char] = nil end
        if user_finish then pcall(user_finish, job) end
        if char then
            if type(job.result) ~= "table" then job.result = {} end
            local ok, sum = pcall(A.summary, char)
            if ok then job.result.after = sum end
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
    return { job = job.id }
end

-- Run guard checks; a refusal becomes a failed job (so the bridge sees the
-- same shape for every outcome) and other errors propagate.
local function guarded(kind, fn)
    return function(args)
        local ok, res = pcall(fn, args or {})
        if ok then return res end
        if G.is_refusal(res) then
            local job = RPC.start_job("act." .. kind, { act = true })
            RPC.finish_job(job, "failed", nil, res.reason)
            return { job = job.id }
        end
        error(res, 0)
    end
end

local function reg(name, fn) RPC.register("act." .. name, guarded(name, fn)) end


------------------------------------------------------------------------
-- act.move_to {char, target | pos, run=true}
------------------------------------------------------------------------

local function movement_left(char)
    local snap = G.resources(char)
    return snap.Movement and (snap.Movement[0] or 0) or 0
end

-- Shared walker used by move_to and the approach phase of talk/interact.
-- opts: target (uuid) | pos, speed, tolerance, on_arrive(job, dist) (optional)
local function begin_walk(job, opts)
    job.walk = {
        target = opts.target, pos = opts.pos, ev = ev_name(job, "mv"),
        last_pos = G.pos(job.char), still_ms = 0, travelled = 0,
        tolerance = opts.tolerance or 1.5, on_arrive = opts.on_arrive,
    }
    if opts.target then
        G.osi("CharacterMoveTo", job.char, opts.target, opts.speed, job.walk.ev, job.id)
    else
        local p = opts.pos
        G.osi("CharacterMoveToPosition", job.char, p[1], p[2], p[3], opts.speed, job.walk.ev, job.id)
    end
end

local function walk_dist_left(job)
    local here = G.pos(job.char)
    local dest = job.walk.target and G.pos(job.walk.target) or job.walk.pos
    if not here or not dest then return nil, here end
    return U.dist(here, dest), here
end

local function walk_arrived(job)
    local w = job.walk
    if not w or w.done then return end
    w.done = true
    local d, here = walk_dist_left(job)
    if w.on_arrive then
        w.on_arrive(job, d or 0)
        return
    end
    local res = { pos = pos_view(here), dist = d and U.round(d, 1) or nil }
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
    if d and d > math.max(w.tolerance, 2.0) then
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
        if tonumber(a[2]) and tonumber(a[2]) ~= job.id then return end
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

-- Per-tick walk bookkeeping: distance travelled (combat budget) and stuck detection.
local function walk_tick(job)
    local w = job.walk
    if not w or w.done then return end
    local here = G.pos(job.char)
    if not here then return end
    local step = w.last_pos and U.dist(w.last_pos, here) or 0
    local t = RPC.now_ms()
    local dt = t - (w.last_t or t)
    -- Teleport detector: Osiris move tasks TELEPORT the character when no
    -- path exists (seen in S001). A real walk/run never exceeds ~12 m/s.
    if w.last_pos and (step > 4.0 or (dt >= 100 and step / (dt / 1000) > 12.0)) then
        w.done = true
        pcall(G.osi, "PurgeOsirisQueue", job.char)
        RPC.push("teleport_blocked", { uuid = job.char, from = pos_view(w.last_pos), to = pos_view(here) })
        RPC.finish_job(job, "failed", { pos = pos_view(here), teleported = true },
            "engine teleported the character (not allowed)")
        return
    end
    w.last_pos = here
    w.travelled = w.travelled + step
    job.partial = { pos = pos_view(here), travelled = U.round(w.travelled, 1) }
    w.last_t = t
    if step < 0.03 then w.still_ms = w.still_ms + dt else w.still_ms = 0 end

    if w.budget and w.travelled >= w.budget - 0.05 then
        w.done = true
        pcall(G.osi, "PurgeOsirisQueue", job.char)
        RPC.finish_job(job, "done", { pos = pos_view(here), stopped = "out of movement",
            travelled = U.round(w.travelled, 1) })
        return
    end
    if w.still_ms >= 5000 then
        local d = walk_dist_left(job)
        if d and d <= w.tolerance then
            walk_arrived(job)
        else
            w.done = true
            pcall(G.osi, "PurgeOsirisQueue", job.char)
            RPC.finish_job(job, "failed", job.partial, "no path to the destination")
        end
    end
end

reg("move_to", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "turn")
    local target = args.target and G.guid(args.target)
    local pos = args.pos
    local path_info
    if target then
        -- Entity targets: the walker stops next to them; no point path check.
        G.target_visible(target)
        G.within(char, target, G.MAX_MOVE_DIST, "destination")
        pos = nil
    elseif pos then
        G.point_ok(char, pos)
        path_info = G.point_reachable(char, pos)
    else
        G.refuse("no destination given")
    end

    local here = G.pos(char)
    local dest = target and G.pos(target) or pos
    local straight = U.dist(here, dest)
    local route = (path_info and path_info.length_m) or straight
    local combat = G.in_combat(char)
    local budget, mv_before
    if combat then
        mv_before = movement_left(char)
        if mv_before < 0.5 then G.refuse("no movement left") end
        if route > mv_before + 0.5 then
            G.refuse(string.format("destination is beyond remaining movement (path %.1f m, %.1f m left)",
                route, mv_before))
        end
        budget = mv_before
    end
    local speed = (args.run == false and not combat) and "Walk" or "Run"
    local timeout = math.min(90000, 8000 + math.floor(route / 0.8 * 1000))

    return start("move_to", char, {
        timeout_ms = timeout,
        interrupt = { combat = not combat, dialogue = true, attacked = not combat },
        osi = WALK_OSI,
        start = function(job)
            begin_walk(job, { target = target, pos = pos, speed = speed,
                tolerance = target and 2.5 or 1.5 })
            job.walk.budget = budget
        end,
        on_tick = walk_tick,
        on_finish = function(job)
            -- Combat movement must cost what the UI path costs: if the engine
            -- did not deduct the distance walked, the guard does.
            if combat and job.walk then
                local used = mv_before - movement_left(char)
                local owed = job.walk.travelled - used
                if owed > 0.25 then
                    G.deduct(char, "Movement", 0, owed)
                    if type(job.result) == "table" then job.result.movement_charged_by_guard = U.round(owed, 1) end
                end
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.interact {char, target}: doors, levers, containers, books, ladders...
------------------------------------------------------------------------

reg("interact", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    G.combat_gate(char, "no_combat")
    local target = G.guid(args.target)
    if not target then G.refuse("no target given") end
    G.target_visible(target)
    G.within(char, target, G.MAX_INTERACT_DIST, "target")
    if G.b(G.q("IsCharacter", target)) then
        if G.is_dead(target) then
            G.refuse("looting corpses is not supported by this action yet (use the UI)")
        end
        G.refuse("use act.talk to talk to characters")
    end
    if G.b(G.q("IsLocked", target)) then
        G.refuse("it is locked")
    end
    local was_open = G.b(G.q("IsOpened", target))
    local openable = false
    pcall(function()
        local si = G.entity(target).ServerItem
        openable = si.IsDoor or si.IsContainer or false
    end)

    -- Doors/containers: report the open/closed state change. UseFinished lands
    -- a few frames before IsOpened flips, so wait (briefly) for the flip.
    local function state_result()
        local open = G.b(G.q("IsOpened", target))
        if open == was_open then return nil end
        local res = open and { opened = true } or { closed = true }
        if open then
            pcall(function() if G.entity(target).ServerItem.IsContainer then res.container_open = true end end)
        end
        return res
    end

    return start("interact", char, {
        timeout_ms = 30000,
        interrupt = { combat = true, dialogue = true, attacked = true },
        osi = {
            UseFinished = function(job, a)
                if G.same(a[1], char) and G.same(a[2], target) then
                    if not G.b(a[3]) then
                        RPC.finish_job(job, "failed", nil, "could not use that")
                        return
                    end
                    local res = state_result()
                    if res then RPC.finish_job(job, "done", res)
                    elseif not openable then RPC.finish_job(job, "done", { used = true })
                    else job.used_at = RPC.now_ms() end
                end
            end,
            EntityEvent = function(job, a)
                if a[2] == job.use_ev and not job.used_at then job.used_at = RPC.now_ms() end
            end,
        },
        start = function(job)
            job.use_ev = ev_name(job, "use")
            G.osi("Use", char, target, job.use_ev)
        end,
        on_tick = function(job)
            local res = state_result()
            if res then
                RPC.finish_job(job, "done", res)
            elseif job.used_at and RPC.now_ms() - job.used_at > (openable and 1500 or 300) then
                RPC.finish_job(job, "done", { used = true })
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.pickup {char, item}   (untested live)
------------------------------------------------------------------------

-- Top-level character holding an item (walks up bags), or nil.
function A.selected(char)
    local user = G.q("GetReservedUserID", char)
    return user ~= nil and G.same(G.q("GetCurrentCharacter", user), char)
end

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
    return start("pickup", char, {
        timeout_ms = 30000,
        interrupt = { combat = true, dialogue = true, attacked = true },
        osi = {
            AddedTo = function(job, a) if G.same(a[1], item) then picked(job) end end,
            PickupFailed = function(job, a)
                if G.same(a[1], char) and G.same(a[2], item) then
                    RPC.finish_job(job, "failed", nil, "could not pick that up")
                end
            end,
        },
        start = function(job)
            -- ForcePickUpOnFailure = 0: never teleport the item into the bag.
            G.osi("Pickup", char, item, ev_name(job, "pick"), 0)
        end,
        on_tick = picked,
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
    local d = G.within(char, npc, G.MAX_INTERACT_DIST, "character")

    local function request(job)
        job.phase = "requested"
        job.requested_at = RPC.now_ms()
        G.osi("DialogStartRequested", npc, char)
    end

    return start("talk", char, {
        timeout_ms = 60000,
        interrupt = { combat = true, dialogue = false, attacked = true },
        osi = setmetatable({
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
        }, { __index = WALK_OSI }),
        start = function(job)
            if d <= G.TALK_RANGE then
                request(job)
            else
                job.phase = "approach"
                begin_walk(job, { target = npc, speed = "Run", tolerance = G.TALK_RANGE,
                    on_arrive = function(j, left)
                        if left > G.TALK_RANGE + 1.5 then
                            RPC.finish_job(j, "failed", nil, "could not reach them")
                        else
                            request(j)
                        end
                    end })
            end
        end,
        on_tick = function(job)
            if job.phase == "approach" then return walk_tick(job) end
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
    if #targets == 0 and not pos then
        local st = Ext.Stats.Get(base)
        if not (st and tostring(st.SpellType) == "Shout") then G.refuse("this action needs a target") end
        targets[1] = char -- self-centred actions (Dash, Hide, Disengage...)
    end

    -- Validate the base spell (known/prepared/cooldown) and the cast itself.
    local check = G.check_cast(char, base, targets[1], pos, nil)
    if targets[2] then G.target_visible(targets[2]) end
    local spell = resolve_spell(base, args.upcast and tonumber(args.upcast))
    if spell ~= base then
        -- Re-validate resources for the upcast variant's costs.
        check = G.check_cast(char, base, targets[1], pos, tonumber(args.upcast))
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
    local harmful = false
    pcall(function()
        for _, f in ipairs(check.stats.SpellFlags) do if f == "IsHarmful" then harmful = true end end
    end)

    local function matches(a)
        return G.same(a[1], char) and (a[2] == spell or a[2] == base)
    end

    return start("cast", char, {
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
            if pos then
                G.osi("UseSpellAtPosition", char, spell, pos[1], pos[2], pos[3])
            elseif targets[2] then
                G.osi("UseSpell", char, spell, targets[1], targets[2], combat and 1 or 0)
            else
                G.osi("UseSpell", char, spell, targets[1], "NULL_00000000-0000-0000-0000-000000000000",
                    combat and 1 or 0)
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
-- act.end_turn {char}   (untested live)
------------------------------------------------------------------------

reg("end_turn", function(args)
    local char = G.actor(args.char)
    if not G.in_combat(char) then G.refuse("not in combat") end
    if not G.is_my_turn(char) then G.refuse("it is not this character's turn") end
    return start("end_turn", char, {
        timeout_ms = 10000,
        osi = {
            TurnEnded = function(job, a)
                if G.same(a[1], char) then RPC.finish_job(job, "done", { ended = true }) end
            end,
        },
        start = function() G.osi("EndTurn", char) end,
        on_tick = function(job)
            if not G.is_my_turn(char) then RPC.finish_job(job, "done", { ended = true }) end
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
                if G.same(a[1], item) then RPC.finish_job(job, "failed", nil, "cannot equip that") end
            end,
        },
        start = function() G.osi("Equip", char, item) end,
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
    local stack = tonumber(G.q("GetStackAmount", item)) or 1
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
        start = function() G.osi("UseSpell", char, "Shout_Hide", char) end,
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
-- act.turn_based {on, char?}   (untested live)
------------------------------------------------------------------------

reg("turn_based", function(args)
    local char = args.char and G.actor(args.char) or G.host()
    if G.in_combat(char) then G.refuse("already in combat") end
    local on = args.on ~= false
    local function state() return G.b(G.q("IsInForceTurnBasedMode", char)) end
    if state() == on then G.refuse(on and "already in turn-based mode" or "not in turn-based mode") end
    return start("turn_based", nil, {
        timeout_ms = 5000,
        start = function() G.osi("ForceTurnBasedMode", char, on and 1 or 0) end,
        on_tick = function(job)
            if state() == on then RPC.finish_job(job, "done", { turn_based = on }) end
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
local CLICK_RANGE = 1.8

-- Walk next to `target`, select `char`, then call finish(job).
local function approach(kind, char, target, finish, extra)
    local d = G.within(char, target, G.MAX_INTERACT_DIST, "target")
    local spec = {
        timeout_ms = 30000 + math.floor(d / 0.8 * 1000),
        interrupt = { combat = true, dialogue = true, attacked = true },
        osi = setmetatable(extra and extra.osi or {}, { __index = WALK_OSI }),
        start = function(job)
            local function arrived(j)
                pcall(G.osi, "MakePlayerActive", char)
                job.phase = "act"
                finish(j)
            end
            if d <= CLICK_RANGE then
                arrived(job)
            else
                job.phase = "approach"
                begin_walk(job, { target = target, speed = "Run", tolerance = CLICK_RANGE,
                    on_arrive = function(j, left)
                        if left > CLICK_RANGE + 1.5 then
                            RPC.finish_job(j, "failed", nil, "could not get close enough")
                        else
                            arrived(j)
                        end
                    end })
            end
        end,
        on_tick = function(job)
            if job.phase == "approach" then return walk_tick(job) end
            if extra and extra.on_tick then extra.on_tick(job) end
        end,
    }
    return start(kind, char, spec)
end

local function needs_click(job, target, expect)
    RPC.finish_job(job, "done", { needs_click = target, expect = expect, button = "L" })
end

-- Read-only follow-up for the bridge after a click: what a player sees on the
-- target (locked padlock icon, open/closed, trap marker of a discovered trap).
RPC.register("act.target_state", function(args)
    local t = G.guid(args.uuid)
    if not t or not G.visible(t) then return { visible = false } end
    local out = { visible = true }
    if G.b(G.q("IsItem", t)) then
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
    return approach("lockpick", char, target, function(job) needs_click(job, target, "roll") end)
end)

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

return A
