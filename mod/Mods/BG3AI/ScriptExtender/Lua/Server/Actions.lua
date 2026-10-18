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
-- end_turn in a shared group (others still to act): report the request as done after this. 1500 until 2026-10-16:
-- the outcome at 1.5 s was always the same "requested" (the game ends the group only when the others end too), so
-- the hold only waited (naut series: 12 shared ends x 1.6 s). 400 ms leaves a few frames for the game to set
-- RequestedEndTurn / hand over (then the plain finish runs). Revert live: Mods.BG3AI.BG3AI.Actions.END_REQUEST_AFTER_MS = 1500
A.END_REQUEST_AFTER_MS = 400
A.END_IGNORED_MS = 3000 -- end_turn: the game ignored EndTurn (no group pending) this long: failed (bridge: HUD button)
A.END_IGNORED = "the game ignored the end-turn order"
-- S035 19:28-19:30: three jump_to calls each ran 25 s "still running" while Shadowheart never moved: the engine
-- dropped UseSpellAtPosition without CastSpellFailed. No CastSpell and no step this long after the order: fail.
A.CAST_NO_START_MS = 8000
-- In combat the cast never walks (WithoutMove): a cast the engine takes starts within a second, so 8 s of nothing
-- was ~6 s of a turn lost per dropped order (S062 Target_MainHandAttack ×2, audit ACT-11). A jump keeps the 8 s.
A.CAST_NO_START_COMBAT_MS = 3000
-- S066-S069: the 6 dropped combat orders no status or target explains (Rage x3, a melee swing x3) all came right
-- after a hit on the caster in its own turn (AttackedBy: a Bleeding tick at its turn start, an opportunity attack
-- during its walk, a psychic tick); the next order ~3 s later worked 6 of 6. S070 20:50 Guiding Bolt: no hit, but the
-- caster stood up from Prone at that turn start (S069's Claude also lost Hold Person then). A no-start whose caster
-- was hit or lost a status this long before the order (or after it) is ordered once more instead of failing.
A.CAST_HIT_WINDOW_MS = 2500
A.hit_ms = A.hit_ms or {} -- char guid -> {t, why}: when it was last hit (AttackedBy defender) or lost a status
-- act.cast {ui=true} (audit ACT-8, the hotbar N-target route): the bridge clicks the hotbar cell and each target as a
-- player does, after this job started; the job only checks the cast and watches CastSpell/CastedSpell. The clicks
-- take ~1 s + 0.5 s per target (hotbar.py budget, hard cap 3x + 4 s): no start this long = failed (the bridge cancels
-- earlier on its own failure).
A.CAST_UI_NO_START_MS = 15000
-- act.cast {ui=true}: a CastSpellFailed that came before the bridge's clicks were done (the upcast picker sends one)
-- fails the job only when no cast starts this long after the bridge says the clicks are done (act.ui_clicked).
A.CAST_UI_REFUSED_MS = 1500
A.CAST_UI_MAX_TARGETS = 10 -- darts/beams/allies one hotbar cast can name (Magic Missile at level 9 = 11 is not ours)
A.CAST_WALK_MAX_REACH = 3.0 -- act.cast out of combat walks into range only for touch-range casts (Help, melee)
A.MELEE_STOP_SLACK = 0.4 -- move_to {target, within}: stop this far inside the melee reach
A.CAST_SETTLE_MS = 250       -- cast: report this long after CastedSpell / the last hit
A.PROJECTILE_SETTLE_MS = 1500 -- cast: a projectile's hits can land this long after CastedSpell (no hit yet)
-- S038-S039: jump_to "done" 4x while the character landed back where it stood (moved 0.6 m, the landing 9 m away):
-- the engine shortens a landing it can't reach. A jump ending this far (horizontally) from its landing reports it.
A.JUMP_OFF_M = 3.0
A.JUMP_NOWHERE_M = 1.0 -- ... and one that moved less than this went nowhere: failed
A.JUMP_AIRBORNE_MAX_MS = 3000 -- wait up to this long after the settle for a still-moving jumper to land
-- S038 20:44:25: a forced jump into the Shadowfell chasm aimed at y=-12 landed at y=-59.4 and Claude died of the
-- fall, yet jump_to said "done". Landing this far below the aimed height is a fall: report it (and a death).
A.JUMP_FALL_M = 4.0
A.JUMP_FALL_CHECK_MS = 600 -- after a fall, wait this long for the fall damage before reading dead/downed
A.USE_REACH = 1.8    -- interact/pickup and the click ops: Use/Pickup/click from here
A.PICKUP_REORDER_MS = 1500 -- pickup: an order gone from the task queue (nothing picked) is re-sent after this

-- The engine's CastSpellFailed carries no reason. S024-S041: ~12 "the action failed — purged_tasks=1" in
-- combat_turn (Toll the Dead, Ranged Attack extra attacks, Spiritual Weapon attacks), each read as a mystery.
-- Say what a player would see at that moment: the target gone, no line of sight, or the projectile's blocked path.
function A.cast_failed_why(char, target, spell)
    local why = "the game refused the cast (no reason given)"
    -- S059 08:57-08:59: Dimension Door refused x3 with "the shot's path is blocked". It is a two-step teleport:
    -- the one who goes (the caster or an ally beside them), then the spot they land on.
    if A.two_step_spell(spell) and not A.revive_spell(spell) then
        return why .. ": " .. tostring(spell) .. " is a two-step teleport (who goes, then the landing spot"
            .. (A.last_place_why and (", which could not be given: " .. A.last_place_why) or "") .. "). The landing"
            .. " must be a spot the caster can SEE and stand on: a floor above you (behind its own ceiling/ledge) or"
            .. " behind a wall is out of sight; stand where you can see the landing (e.g. at the foot of an open"
            .. " drop, or on the far side of a gap), or climb/walk (route)"
    end
    if not target or G.same(char, target) then return why end
    -- S043/S057/S058: Revivify 0 of 14 live. The game data makes it a two-step cast (Teleportation_Revivify:
    -- OriginTargetConditions = the body, then a spot on the ground to stand them up), which a one-target
    -- UseSpell can't express. Party members CAN be revived: say so, and what the cast lacked.
    if A.two_step_spell(spell) then
        local playable = G.b(G.q("IsTagged", target, "PLAYABLE_25bf5042-5bf6-4360-8df8-ab107ccb0d37"))
        return why .. ": " .. tostring(spell) .. " is a two-step cast (the body, then the spot they stand up on)"
            .. (A.last_place_why and (" and the spot could not be given (" .. A.last_place_why .. ")") or "")
            .. (playable and "" or "; this one is not a party member: only party members come back")
            .. ". Read a Scroll of Revivify by name (use_ability ability=\"Scroll of Revivify\" target=<the body>:"
            .. " it clicks the body, then the ground beside it), or Withers at camp"
    end
    local ok, txt = pcall(function()
        if G.is_dead(target) then return "the target was already dead when it fired" end
        if not G.visible(target) then return "the target is out of sight now (hidden, invisible or behind cover)" end
        if not G.b(G.q("HasLineOfSight", char, target)) then
            return "no line of sight to the target now (it or something moved into the way)"
        end
        return nil
    end)
    if ok and txt then return why .. ": " .. txt end
    -- ACT-3 (S065 x9): the first melee swing right after combat_turn's move was refused here and read as "the
    -- shot's path is blocked". A swing has no path: say how far it swung from and the reach our guard allowed
    -- (the engine's own melee reach is unmeasured: tools/dev/acceptance.py "melee reach probe").
    local okm, mel = pcall(function()
        local range, melee = G.spell_range(spell and Ext.Stats.Get(spell), char)
        local p, q = G.pos(char), G.pos(target)
        if not (range and melee and p and q) then return nil end
        return string.format("a melee swing from %.1f m centre to centre (our reach %.1f m: range %.1f m + both"
            .. " bounding radii), probably just outside the engine's reach: step closer (within 1 m) and swing again",
            U.dist(p, q), G.melee_reach(char, target, range), range)
    end)
    if okm and mel then return why .. ": " .. mel end
    return why .. ": usually the shot's path is blocked (cover, a ledge, a creature in the way) though the target"
        .. " is in sight; try another target or step to a clearer spot"
end

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
        -- Ambient barks (automated dialogs) don't stop a player's walk. Only the walker's own conversation stops
        -- it (S066 19:11:55: Orin's dialogue pulled Claude in and stopped the 3 members walking to him 16-20 m in).
        if job.interrupt.dialogue and G.is_party(a[3]) and (not job.char or G.same(a[3], job.char))
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

-- What the game's story does around a jump it makes: the moved character pulled into a conversation, the party
-- entering combat, a quest step unlocking (S066 19:11:50: Orin's arena, `Get Orin's Netherstone` ×4 with the jump).
local TELE_SIGNALS = {
    DialogActorJoined = function(job, a)
        if G.same(a[3], job.char) and not G.b(G.q("DialogIsAutomated", a[1])) then return "a conversation started" end
    end,
    EnteredCombat = function(_, a)
        if G.is_party(a[1]) then return "combat started" end
    end,
    QuestUpdateUnlocked = function() return "a quest updated" end,
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
    -- A walk routed through this ladder stops at its first reversal on it (WALK_OSI.UseFinished); this stays the
    -- backstop at twice the uses (naut S001: 6).
    if job and job.status == "running" and job.walk and not job.walk.done and job.climbs and job.climbs[item]
        and s.n < 2 * A.LOOP_USES then return end
    A.use_streak[char] = nil
    RPC.push("loop_stopped", { uuid = char, item = item, uses = s.n, job = job and job.id or nil })
    if job and job.status == "running" then
        interrupt(job, "stuck: the character kept using the same item (" .. s.n
            .. " times, e.g. re-climbing a ladder); try another route or a jump")
    else
        pcall(G.osi, "PurgeOsirisQueue", char)
    end
end

function A.stirred(u, why)
    local g = G.guid(u)
    if not g then return end
    if not A.hit_ms[g] then
        A.hit_n = (A.hit_n or 0) + 1
        if A.hit_n > 500 then A.hit_ms, A.hit_n = {}, 1 end
    end
    A.hit_ms[g] = { t = G.now_ms(), why = why }
end

function A.dispatch(event, args)
    if event == "TurnEnded" and args[1] then
        local g = G.guid(args[1]) or args[1]
        if g then G.end_requested[g] = nil; G.turn_over[g] = G.now_ms() end
    elseif event == "TurnStarted" and args[1] then
        local g = G.guid(args[1]) or args[1]
        if g then G.turn_over[g] = nil; G.end_requested[g] = nil end
    elseif event == "AttackedBy" and args[1] then
        A.stirred(args[1], "a hit on the caster")
    elseif event == "StatusRemoved" and args[1] then
        -- shown statuses only, by their shown name (Prone: standing up; Hold Person); technical ones flap all the time
        local V = BG3AI.Visibility
        local info = V and V.statusInfo and V.statusInfo(tostring(args[2]))
        if info then A.stirred(args[1], "the caster's " .. tostring(info.name) .. " ending") end
    end
    each_running(function(job)
        if job.tele then -- a jump settling (teleport_check): only the story signals count now
            local sig = TELE_SIGNALS[event]
            local why = sig and sig(job, args)
            if why then A.tele_finish(job, why) end
            return
        end
        local rule = INTERRUPTS[event]
        if rule and job.interrupt then rule(job, args) end
        if job.status == "running" and job.osi and job.osi[event] then
            job.osi[event](job, args)
        end
    end)
    -- after the jobs: a walk's own ladder-reversal stop (WALK_OSI.UseFinished) names the ladder before this fires
    if event == "UseFinished" then
        local ok, err = pcall(A.loop_watch, args[1], args[2])
        if not ok then Ext.Utils.PrintWarning("[BG3AI] loop_watch: " .. tostring(err)) end
    end
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
    CastSpell = 5, CastedSpell = 5, CastSpellFailed = 5,
    AttackedBy = 7, EnteredCombat = 2, TurnEnded = 1, TurnStarted = 1,
    PickupFailed = 2, AddedTo = 3,
    Equipped = 2, EquipFailed = 2, Unequipped = 2, UnequipFailed = 2,
    CharacterSelectedForUser = 2, AttachedToPartyGroup = 1, DetachedFromPartyGroup = 1,
    TeleportedToCamp = 1, TeleportedFromCamp = 1, QuestUpdateUnlocked = 3,
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
-- spec.passive (act.select): not an action of the character. It neither supersedes the character's running job
-- nor becomes A.active (L8 23:58 helm ladders / trade travel: an act.select of the walker at its arrival
-- interrupted interact e39 and a 43 m move_to, "superseded by a new action (select)"; picking a portrait doesn't stop a
-- player's walk or climb).
local function start(kind, char, spec)
    if char and not spec.passive then
        local prev = A.active[char]
        if prev and prev.status == "running" then
            -- naut3-r2 15:51:34: Lae'zel's cast, cast and end_turn were each superseded within 0.3 s by a job that
            -- logged nothing but "done": name the new one, in the result and the log
            pcall(Ext.Utils.Print, string.format("[BG3AI] job %s (%s) superseded by a new %s for %s",
                tostring(prev.id), tostring(prev.kind), kind, tostring(char)))
            interrupt(prev, "superseded by a new action (" .. kind .. ")")
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
        if job.tele then return A.tele_settle(job) end -- a jump settling: nothing else runs (teleport_check)
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
    if char and not spec.passive then A.active[char] = job end
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
            return stop(string.format("never started moving (%.1f s, the route was there)", w.still_ms / 1000))
        end
    end
    return false
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
    if w.combat and combat_walk_stop(job) then return end
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
-- Acceptance 2026-10-11 01:00 (Knotted Roots, passed 10-10 14:48 with the same code): the Use answered success for
-- all four and nobody climbed in 10 s. A player clicks the ladder again: one purge + re-Use when the actor hasn't
-- moved vertically this long after the Use.
A.LADDER_RETRY_MS = 3000
-- 2nd cause (acceptance 01:07/01:5x: from the Mausoleum gate the job walked to the top landing and Used at the walk's
-- arrival, and nothing climbed, 2 of 2; from a standing start the same Use climbs, and a raw Use 1 s after a walk
-- climbs). A Use sent while the walk (and the chained followers behind it) is still settling, and a re-Use sent in place
-- after a purge (probe B: tasks 1→0, no climb), do nothing. So: after a walk, Use only once the actor stood still
-- LADDER_SETTLE_MS (at most LADDER_SETTLE_MAX_MS); the retry steps LADDER_STEP_M back from the ladder and walks in
-- again before the re-Use (probe C: walk, then Use → climbed).
A.LADDER_SETTLE_MS = 700
A.LADDER_FOLLOWER_M = 20.0 -- chained followers this close to the actor must stand still too before the Use
A.LADDER_SETTLE_MAX_MS = 3000
A.LADDER_STEP_M = 1.5
A.LADDER_STEP_MAX_MS = 3000
-- 4th look (acceptance after the follower hold, 10-11 08:37/10:43/12:22, the gate start): Claude climbed but the job
-- was "still running" at 25 s twice, and once ended "done climbed=down" with Claude back at the top 12 s later. The
-- climb watch (purge once the climb begins, retry, deadline) started only at the UseFinished; for a settled Use
-- (after a walk) `used_at` stayed nil until then, so a late UseFinished let the Use's re-climb loop (cause 1) run.
-- true: the watch starts at our own Use (a UseFinished keeps the earlier time). false: the old wait for UseFinished.
A.LADDER_WATCH_AT_USE = true

-- In combat a carried potion is drunk as from the hotbar: on the drinker's turn, for the item's own UseCosts
-- (a bonus action for Potion of Healing). S003/S017/S018/S023/S031: every in-combat potion was refused ("not
-- available during combat"), at 7 HP in S031. Returns the cost check, or nil when this is not such a drink.
function A.combat_potion(char, target)
    if not target or not G.in_combat(char) or not G.b(G.q("IsItem", target)) then return nil end
    if not G.same(holder(target), char) then return nil end
    local ok, st = pcall(function() return Ext.Stats.Get(G.entity(target).Data.StatsId) end)
    if not ok or not st or st.ItemUseType ~= "Potion" then return nil end
    G.combat_gate(char, "turn")
    local snap, payers = G.resources(char), {}
    for _, c in ipairs(G.parse_costs(st.UseCosts)) do
        local n, l = G.payer(snap, c)
        if not n then G.refuse("not enough " .. G.friendly(c.name) .. " to drink it") end
        payers[#payers + 1] = { name = n, level = l, amount = c.amount }
    end
    return { snapshot = snap, payers = payers }
end

-- Scrolls in combat (A7, 2026-10-17). In BG3 anyone reads a carried scroll mid-fight, on their own turn, for the
-- scroll's cost (_MagicScroll: ItemUseType "Scroll", UseCosts "ActionPoint:1": an action, like casting the spell).
-- The tools refused it ("not available during combat": S061 Scroll of Fear; claude-run-2's Orin plan reads Fear, Cone
-- of Cold, Scorching Ray and Revivify scrolls). The Use puts the scroll's spell in hand; the bridge clicks its target.
-- Osiris Use may skip the cost (it does for potions), so A.scroll_pending keeps what reading it owes and
-- act.scroll_settle charges, once the scroll is used up, whatever the engine did not.
A.scroll_pending = {}
function A.combat_scroll(char, target)
    if not target or not G.in_combat(char) or not G.b(G.q("IsItem", target)) then return nil end
    if not G.same(holder(target), char) then return nil end
    local ok, st = pcall(function() return Ext.Stats.Get(G.entity(target).Data.StatsId) end)
    if not ok or not st or st.ItemUseType ~= "Scroll" then return nil end
    G.combat_gate(char, "turn")
    local snap, payers = G.resources(char), {}
    for _, c in ipairs(G.parse_costs(st.UseCosts)) do
        local n, l = G.payer(snap, c)
        if not n then
            G.refuse("not enough " .. G.friendly(c.name) .. " to read it (reading a scroll costs "
                .. (c.name == "ActionPoint" and "an action, like casting" or G.friendly(c.name)) .. ")")
        end
        payers[#payers + 1] = { name = n, level = l, amount = c.amount, before = (snap[n] or {})[l] or 0 }
    end
    return { payers = payers, stack = tonumber((G.q("GetStackAmount", target))) or 1 }
end

-- Ladder, 3rd look (acceptance 04:31 still FAIL from the Mausoleum gate after the settle): every passing climb had
-- the followers already standing; every failing one had them still arriving, and all four "used" the ladder (the
-- engine sends the chained followers through the leader's Use). Only the actor climbs: the chained followers are
-- detached (and their own ladder orders purged) for the climb and re-chained to the actor when the job ends; they
-- then follow as usual (a player's followers take the ladder after the leader).
function A.ladder_followers(char)
    local out = {}
    for _, p in ipairs(G.party()) do
        if not G.same(p, char) and G.b(G.q("InSamePartyGroup", char, p)) then out[#out + 1] = p end
    end
    return out
end
function A.ladder_hold(char)
    local held = A.ladder_followers(char)
    for _, p in ipairs(held) do
        pcall(G.osi, "DetachFromPartyGroup", p)
        pcall(G.osi, "PurgeOsirisQueue", p)
    end
    return held
end
function A.ladder_release(char, held)
    for _, p in ipairs(held or {}) do
        if not G.b(G.q("InSamePartyGroup", char, p)) then pcall(G.osi, "AttachToPartyGroup", p, char) end
    end
end

A.STORY_LOCKED = "clicked it: still locked (no lock to pick: the game opens it some other way, e.g. a lever, a"
    .. " quest item carried or used on it, a spell)"

reg("interact", function(args)
    local char = G.actor(args.char)
    G.not_in_dialogue(char)
    local target = G.guid(args.target)
    local potion = A.combat_potion(char, target)
    local scroll = not potion and A.combat_scroll(char, target)
    if not potion and not scroll and target and G.in_combat(char) then
        -- naut3 ×3: the transponder inside combat_turn was refused "(use the UI)", and each run spent a model turn
        -- finding click_on. Name the tool: in a fight a player uses an object by clicking it.
        G.refuse("not available during combat through this action: in a fight an object is used by clicking it,"
            .. " click_on " .. target .. " (the selected character walks there on its own movement)")
    end
    if not potion and not scroll then G.combat_gate(char, "no_combat") end
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
        G.refuse("use talk_to to talk to characters")
    end
    -- Locked: as in the game, a party member carrying its key opens it (the
    -- player would select them and click). Otherwise refuse with the options.
    -- A story lock (no lockpick difficulty: the Temple of Bhaal door the amulet opens, a lever-worked gate) has no
    -- key to match: the click is still sent, as a player's, and the game's own script answers it (a prompt, the
    -- door opening, or "Locked"). Refusing it needed a hand click (audit ACT-6, impl-logs.md:679).
    local key_item, key_note, story_lock
    if G.b(G.q("IsLocked", target)) then
        local h, k = G.party_key(target, char)
        if not h and G.lock_pickable(target) == false then
            story_lock = true
        elseif not h then
            G.refuse("it is locked and nobody in the party carries its key (lockpick it, force it, or find the key)")
        end
        if story_lock then -- the actor clicks it themselves
        elseif not G.same(h, char) then
            char = h
            G.not_in_dialogue(char)
            G.combat_gate(char, "no_combat")
            G.within(char, target, G.MAX_INTERACT_DIST, "the key holder's target")
        end
        if not story_lock then
            key_item = k
            local function nm(u) local ok, n = pcall(function() return BG3AI.Visibility.name(u) end); return ok and n or u end
            key_note = { unlocked_with = nm(k), key_holder = nm(char) }
        end
    end
    local function story_locked()
        return story_lock and G.b(G.q("IsLocked", target))
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
        interrupt = { combat = not (potion or scroll), dialogue = true, attacked = not (potion or scroll) },
        on_finish = (potion or scroll or ladder) and function(job)
            if job.ladder and job.ladder.held then A.ladder_release(char, job.ladder.held) end
            if scroll and job.status == "done" then
                A.scroll_pending[G.guid(char)] = { item = target, stack = scroll.stack, payers = scroll.payers }
                if type(job.result) == "table" then job.result.combat_scroll = true end
                return
            end
            if not potion or job.status ~= "done" then return end
            -- Osiris Use skips the hotbar's cost: charge whatever the engine did not.
            local after, charged = G.resources(char), {}
            for _, p in ipairs(potion.payers) do
                local owed = p.amount - (((potion.snapshot[p.name] or {})[p.level] or 0)
                    - ((after[p.name] or {})[p.level] or 0))
                if owed > 0.01 and G.deduct(char, p.name, p.level, owed) then
                    charged[#charged + 1] = { resource = G.friendly(p.name), amount = owed }
                end
            end
            if type(job.result) == "table" then
                job.result.drunk = true
                if #charged > 0 then job.result.charged_by_guard = charged end
            end
        end or nil,
        osi = with_walk({
            UseFinished = function(job, a)
                if G.same(a[1], char) and G.same(a[2], target) then
                    if ladder and not (job.ladder and job.ladder.used) then return end -- not our Use yet (see below)
                    if not G.b(a[3]) or (story_locked() and not openable) then
                        RPC.finish_job(job, "failed", nil, key_note and G.b(G.q("IsLocked", target))
                            and ("still locked: " .. key_note.unlocked_with .. " did not open it")
                            or (story_locked() and A.STORY_LOCKED) or "could not use that")
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
            local y_start = (G.pos(char) or {})[2]
            begin_approach(job, target, plan, A.USE_REACH, function(j)
                -- The engine's own walk took the ladder already (live L3 helm probe: the walk to e39's foot climbed
                -- it, and the Use then climbed back down): the walk ended on the far end's floor, so it is climbed.
                local here, use_y = G.pos(char), j.approach and j.approach.plan.use_y
                if ladder and j.walk and here and use_y and y_start
                    and math.abs(here[2] - use_y) >= A.LADDER_DY and math.abs(here[2] - y_start) >= A.LADDER_DY
                    and (here[2] - use_y) * (here[2] - y_start) > 0 then
                    local dy = here[2] - y_start
                    RPC.finish_job(j, "done", with_key({ used = true, climbed = dy < 0 and "down" or "up",
                        dy = U.round(dy, 1), climbed_on_walk = true }))
                    return
                end
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
                if ladder then
                    j.ladder = { y0 = (G.pos(char) or {})[2], item = target }
                    if j.walk then -- walked here: settle first (on_tick sends the Use)
                        j.ladder.settle_from = RPC.now_ms()
                        j.ladder.followers = A.ladder_followers(char)
                        return
                    end
                    j.ladder.used = true
                    j.ladder.held = A.ladder_hold(char)
                end
                G.osi("Use", char, target, j.use_ev)
                if ladder and A.LADDER_WATCH_AT_USE then j.used_at = RPC.now_ms() end
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
            if job.ladder and not job.ladder.used then
                -- settling after the walk (or the retry's step back): Use once still, or at the cap
                local lj, here, now = job.ladder, G.pos(char), RPC.now_ms()
                local moved = here and lj.settle_last and U.dist(here, lj.settle_last) or 1
                lj.settle_last = here
                -- the chained followers settle too (they were still arriving in every failed climb)
                lj.fpos = lj.fpos or {}
                for _, p in ipairs(lj.followers or {}) do
                    local fp = G.pos(p)
                    if fp and here and U.dist(fp, here) <= A.LADDER_FOLLOWER_M then
                        local was = lj.fpos[p]
                        moved = math.max(moved, was and U.dist(fp, was) or 1)
                    end
                    lj.fpos[p] = fp
                end
                lj.still_since = (moved < 0.03) and (lj.still_since or now) or nil
                if lj.stepping and now - lj.stepping < A.LADDER_STEP_MAX_MS and not lj.still_since then return end
                if (lj.still_since and now - lj.still_since >= A.LADDER_SETTLE_MS)
                    or now - lj.settle_from >= A.LADDER_SETTLE_MAX_MS + (lj.stepping and A.LADDER_STEP_MAX_MS or 0) then
                    if lj.stepping then pcall(G.osi, "PurgeOsirisQueue", char) end -- the step's walk, if still queued
                    if not lj.held then lj.held = A.ladder_hold(char) end
                    lj.used, lj.y0 = true, here and here[2] or lj.y0
                    G.osi("Use", char, target, job.use_ev)
                    -- a re-Use restarts the climb deadline; the first one starts it (A.LADDER_WATCH_AT_USE)
                    job.used_at = (job.used_at or A.LADDER_WATCH_AT_USE) and now or nil
                end
                return
            end
            if job.ladder and job.used_at then
                -- Acceptance 13:4x: "done — used" 300 ms after the Use, and the job's end purges the task queue: the
                -- climb was cancelled at the top (the active character stayed up, followers went down). Wait for
                -- the climb itself: the actor on another floor and still, or give up after A.LADDER_CLIMB_MS.
                local lj, here = job.ladder, G.pos(char)
                local now = RPC.now_ms()
                -- Root cause (live probe 2026-10-11 01:5x, Knotted Roots): the Use stays in the Osiris queue after
                -- the climb and climbs again from the other end (down/up/down, < 0.5 s at each end), so the actor
                -- ended back where it started. Purged once the climb has begun (CLIMBING status, or 0.8 m along), the
                -- climb runs to its end and the actor stays there (probe: 31.59 held for 5 s).
                if not lj.purged and here and lj.y0 and (math.abs(here[2] - lj.y0) > 0.8
                    or G.b(G.q("HasActiveStatus", char, "CLIMBING"))) then
                    lj.purged = true
                    pcall(G.osi, "PurgeOsirisQueue", char)
                end
                if here and lj.y0 then
                    -- Off the ladder = vertically still (stepping off moves sideways), on the other floor. Live
                    -- 14:2x: the Use re-climbs in a loop with ~0.5 s at each end; the job's end purges it.
                    local vy = lj.last and math.abs(here[2] - lj.last[2]) or 1
                    lj.hmove = lj.last and math.sqrt((here[1] - lj.last[1]) ^ 2 + (here[3] - lj.last[3]) ^ 2) or 1
                    lj.still = (vy < 0.05) and (lj.still or now) or nil
                    lj.last = here
                    local dy = here[2] - lj.y0
                    -- Live probe 2026-10-11 01:4x (Knotted Roots): the Use's loop now turns round at the foot in
                    -- < 0.5 s (31.59 at one sample, climbing again at the next), so 200 ms of stillness there never
                    -- came and the job failed with the actor back at the top. Arrived = on the other floor and not
                    -- moving vertically for one tick, or at the ladder's foot going down (the item stands at its foot).
                    local lp = G.pos(lj.item)
                    local at_foot = dy < 0 and lp and math.abs(here[2] - lp[2]) <= 0.6
                    if math.abs(dy) >= A.LADDER_DY and (lj.still or at_foot) then
                        RPC.finish_job(job, "done", with_key({ used = true, climbed = dy < 0 and "down" or "up",
                            dy = U.round(dy, 1) }))
                        return
                    end
                end
                if not lj.retried and not lj.purged and here and lj.y0 and math.abs(here[2] - lj.y0) < 0.3
                    and (lj.hmove or 1) < 0.05
                    and now - job.used_at > A.LADDER_RETRY_MS then -- standing still (not the Use's own walk)
                    lj.retried = true
                    pcall(G.osi, "PurgeOsirisQueue", char)
                    -- step back from the ladder and walk in again (probe C), then the settle above re-Uses
                    local lp = G.pos(target)
                    local dx, dz = lp and here[1] - lp[1] or 0, lp and here[3] - lp[3] or 0
                    local n = math.sqrt(dx * dx + dz * dz)
                    local back
                    if n > 0.1 then
                        local x, z = here[1] + dx / n * A.LADDER_STEP_M, here[3] + dz / n * A.LADDER_STEP_M
                        local y = G.floor_at(x, z, here[2])
                        if y and math.abs(y - here[2]) < 0.6 then back = { x, y, z } end
                    end
                    -- NAV-8: the step back walks only with a path proof (a short direct path; CharacterMoveToPosition
                    -- with no path is the engine's teleport)
                    if back then
                        local okr, r, info = pcall(G.reachable, char, back)
                        if not (okr and r == true and not (info and info.needs_jump)
                            and (not (info and info.length_m) or info.length_m <= 2 * A.LADDER_STEP_M + 1.0)) then
                            back = nil
                        end
                    end
                    if back then
                        G.osi("CharacterMoveToPosition", char, back[1], back[2], back[3], "Walk", ev_name(job, "lstep"))
                        lj.stepping = now
                    end
                    lj.used, lj.settle_from, lj.still_since, lj.settle_last = false, now, nil, nil
                    job.used_at = now
                    return
                end
                if now - job.used_at > A.LADDER_CLIMB_MS then
                    RPC.finish_job(job, "failed", { used = true, climbed = false, used_twice = lj.retried or nil },
                        "used the ladder but did not climb it (still on the same floor after "
                        .. math.floor(A.LADDER_CLIMB_MS / 1000) .. " s" .. (lj.retried and ", Used twice" or "")
                        .. "): the game didn't start the climb; walk round by the route, or jump_to the ladder's foot"
                        .. " if the drop is safe")
                end
                return
            end
            if res then
                RPC.finish_job(job, "done", res)
            elseif job.used_at and RPC.now_ms() - job.used_at > (openable and 1500 or 300) then
                if story_locked() then RPC.finish_job(job, "failed", { used = true }, A.STORY_LOCKED)
                else RPC.finish_job(job, "done", with_key({ used = true })) end
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
    if item and G.pos(item) == nil and G.entity(item) == nil then
        G.refuse("that item is no longer in the world (picked up and stacked into another, used or destroyed); "
            .. "get_inventory shows whether the party has it")
    end
    if not item or not G.b(G.q("IsItem", item)) then G.refuse("that is not an item") end
    if G.q("GetInventoryOwner", item) ~= nil then
        G.refuse("that item is inside a container (open it and loot instead)")
    end
    G.target_visible(item)
    G.within(char, item, G.MAX_INTERACT_DIST, "item")

    -- S080 #9: the 2nd of two Half-Eaten Apples stacked into the 1st in Gale's bag (AddedTo named the stack e388,
    -- never e389, which left the world): the job waited for e389's holder until its ~45 s timeout. An add of the
    -- same template to the party while our item is gone from the world is our pick (stacked); an item gone with
    -- no such add ends the job at once.
    local tpl = tostring((G.q("GetTemplate", item)) or "")
    local function gone() return G.pos(item) == nil and G.entity(item) == nil end
    local function picked(job)
        local h = holder(item)
        if h and G.is_party(h) then
            RPC.finish_job(job, "done", { picked_up = true, by = h })
            return true
        end
        if not gone() then return false end
        if job.stacked then
            RPC.finish_job(job, "done", { picked_up = true, by = job.stacked.by, stacked_into = job.stacked.into })
        else
            RPC.finish_job(job, "failed", { gone = true }, "the item is no longer in the world (someone else took "
                .. "it, or it was used or destroyed); get_inventory shows whether the party has it")
        end
        return true
    end
    local plan = G.approach_point(char, item, A.USE_REACH)
    return start("pickup", char, {
        eta_s = walk_eta(plan.length_m) + 1.0, route_m = plan.length_m,
        timeout_ms = 30000 + walk_timeout(plan.length_m),
        interrupt = { combat = true, dialogue = true, attacked = true },
        osi = with_walk({
            AddedTo = function(job, a)
                if G.same(a[1], item) then
                    local h = G.guid(a[2])
                    h = h and (G.b(G.q("IsCharacter", h)) and h or holder(h))
                    if h and G.is_party(h) then   -- holder(item) is nil when it merged into a stack at once
                        RPC.finish_job(job, "done", { picked_up = true, by = h })
                    else
                        picked(job)
                    end
                elseif tpl ~= "" and tostring((G.q("GetTemplate", a[1])) or "") == tpl then
                    local h = holder(a[1])
                    if h and G.is_party(h) then
                        job.stacked = { by = h, into = G.guid(a[1]) }
                        picked(job)
                    end
                end
            end,
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
                j.picked_order = RPC.now_ms()
                G.osi("Pickup", char, item, ev_name(j, "pick"), 0)
            end)
        end,
        on_tick = function(job)
            if gone() and picked(job) then return end   -- walking toward an item that left the world: stop now
            if job.phase == "approach" then return approach_tick(job) end
            if job.phase ~= "act" then return end
            if picked(job) then return end
            -- S043 23:32-23:34 (×3): in a do_actions batch, step 1 ended with its leftover task purged and step 2's
            -- Pickup, sent at once, never ran (no task, travelled=0) until the 40 s timeout. An order that is gone
            -- from the queue with nothing picked is sent again (A.PICKUP_REORDER_MS after it, up to twice).
            local now = RPC.now_ms()
            if job.picked_order and now - job.picked_order >= A.PICKUP_REORDER_MS and osi_tasks(char) == 0
                and (job.reorders or 0) < 2 then
                job.reorders = (job.reorders or 0) + 1
                job.picked_order = now
                G.osi("Pickup", char, item, ev_name(job, "pick"), 0)
                return
            end
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

-- A spell picked on one creature, then aimed at a ground spot (Teleportation with OriginTargetConditions:
-- Revivify and its scroll, Resurrection, Dimension Door on an ally...). The UI asks for two clicks.
function A.two_step_spell(spell)
    local ok, r = pcall(function()
        local st = spell and Ext.Stats.Get(spell)
        return st ~= nil and tostring(st.SpellType) == "Teleportation"
            and tostring(st.OriginTargetConditions or "") ~= "" and tostring(st.TeleportSelf or "") ~= "Yes"
    end)
    return ok and r or false
end

-- A two-step cast that stands a body up (Revivify, Resurrection): its first pick must be dead/downed.
function A.revive_spell(spell)
    local ok, r = pcall(function()
        local st = spell and Ext.Stats.Get(spell)
        return st ~= nil and tostring(st.OriginTargetConditions or ""):find("Dead()", 1, true) ~= nil
    end)
    return ok and r or false
end

-- How far a two-step teleport's landing may be (stats AreaRadius; Dimension Door 50, Revivify 9). nil = unknown.
function A.landing_range(spell)
    local ok, r = pcall(function() return tonumber(Ext.Stats.Get(spell).AreaRadius) end)
    return ok and r and r > 0 and r or nil
end

-- The landing of a two-step teleport (Dimension Door): within its range and on a floor (stats TargetConditions
-- CanStand; the game's cursor shows no landing in the air). Refuses with what to do.
A.LANDING_FLOOR_SLACK_M = 2.0
function A.landing_ok(char, spell, dest)
    G.point_ok(char, dest)
    local p, rng = G.pos(char), A.landing_range(spell)
    if p and rng and U.dist(p, dest) > rng + 0.5 then
        G.refuse(string.format("the landing is out of range (%.1f m, range %.1f m): move closer first",
            U.dist(p, dest), rng))
    end
    local ok, hs = pcall(Ext.Level.GetHeightsAt, dest[1], dest[3])
    if not ok or type(hs) ~= "table" then return end -- can't tell
    for _, h in ipairs(hs) do
        if type(h) == "number" and math.abs(h - dest[2]) <= A.LANDING_FLOOR_SLACK_M then return end
    end
    if #hs == 0 then
        G.refuse(string.format("no ground at all under the landing (%.1f,%.1f,%.1f): pick a floor you can see",
            dest[1], dest[2], dest[3]))
    end
    local best
    for _, h in ipairs(hs) do
        if type(h) == "number" and (not best or math.abs(h - dest[2]) < math.abs(best - dest[2])) then best = h end
    end
    G.refuse(string.format("the landing (%.1f,%.1f,%.1f) is in the air: no floor within %.0f m of that height"
        .. "%s; give the floor's own y", dest[1], dest[2], dest[3], A.LANDING_FLOOR_SLACK_M,
        best and string.format(" (the nearest floor there is at y=%.1f)", best) or ""))
end

-- The ground spot for a two-step cast: beside the body, A.PLACE_M toward the caster (the body itself is a
-- character, and the second step's TargetConditions are "not Character()").
A.PLACE_M = 1.2
function A.place_spot(char, target)
    local a, b = G.pos(target), G.pos(char)
    if not a then return nil end
    if not b then return { a[1], a[2], a[3] } end
    local dx, dz = b[1] - a[1], b[3] - a[3]
    local d = math.sqrt(dx * dx + dz * dz)
    if d < 0.01 then return { a[1], a[2], a[3] } end
    local k = math.min(A.PLACE_M, d) / d
    return { a[1] + dx * k, a[2], a[3] + dz * k }
end

-- Writing TargetPos2 into the engine's queued task goes through a struct mapping (bg3se OsirisTask.h) never proven
-- on this build, and the read-back uses the same mapping, so a wrong offset would pass it: the lockpick write that
-- corrupted the heap was this class (HANDOFF §7, audit ACT-9). OFF until a live check proves the layout
-- (acceptance "revivify two-step spot set", opt-in); meanwhile a two-step spell cast is refused with the routes
-- that work (a scroll is clicked through the UI). Turn on for a probe: Mods.BG3AI.BG3AI.Actions.TASK_POS2 = true
A.TASK_POS2 = false
A.TASK_POS2_OFF = "the engine-task write for a second pick is off (audit ACT-9)"

-- Give the queued UseSpell task its second target (OsirisUseSpellTask.TargetPos2, bg3se OsirisTask.h). Returns
-- true, or nil + why. Read-checked before the write; any proxy error is caught. Nothing is written while
-- A.TASK_POS2 is off.
function A.set_task_pos2(char, spell, p)
    if not A.TASK_POS2 then return nil, A.TASK_POS2_OFF end
    local ok, err = pcall(function()
        local tasks = Ext.Entity.Get(char).ServerCharacter.OsirisController.Tasks
        local t
        for i = #tasks, 1, -1 do
            local ok2, sp = pcall(function() return tasks[i].Spell end)
            local id = ok2 and sp and (type(sp) == "string" and sp or sp.OriginatorPrototype or sp.Prototype)
            if id == spell then t = tasks[i] break end
        end
        if not t then error("no queued spell task", 0) end
        t.TargetPos2 = { p[1], p[2], p[3] }
        local q = t.TargetPos2
        if not q or math.abs((q[1] or 0) - p[1]) > 0.01 then error("the task kept no TargetPos2", 0) end
    end)
    if ok then return true end
    return nil, tostring(err)
end

-- After UseSpell(first pick): a two-step cast's second pick goes into the queued task. Revivify: the spot beside
-- the body; a teleport (Dimension Door): the landing given.
-- clicked: the hotbar route (act.cast ui=true) gives the second pick by a click, nothing is written; only the
-- result's landing / revive report is set up.
function A.give_second_pick(job, char, spell, targets, landing, clicked)
    if targets[2] or not targets[1] or not A.two_step_spell(spell) then return end
    if clicked then
        job.place = landing and { U.round(landing[1], 1), U.round(landing[2], 1), U.round(landing[3], 1) } or nil
        job.teleport = landing and true or nil
        job.revive = (not landing and A.revive_spell(spell)) or nil
        return
    end
    local p = landing or A.place_spot(char, targets[1])
    local ok2, why = nil, "the body's position is unknown"
    if p then ok2, why = A.set_task_pos2(char, spell, p) end
    A.last_place_why = (not ok2) and why or nil
    job.place = p and { U.round(p[1], 1), U.round(p[2], 1), U.round(p[3], 1) } or nil
    job.teleport = landing and true or nil
end

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

-- act.ui_clicked {id}: the bridge's hotbar clicks for that act.cast {ui=true} job are done (A17). A CastSpellFailed
-- held from before then fails the job CAST_UI_REFUSED_MS later unless the cast starts; one after it fails it at once.
RPC.register("act.ui_clicked", function(args)
    local job = RPC.jobs[tonumber(args.id)]
    if not job then error("no such job " .. tostring(args.id)) end
    if job.ui and job.status == "running" then job.clicked_ms = RPC.now_ms() end
    return { status = job.status, held = job.ui_failed }
end)

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
    -- ui: the player's route (hotbar cell, one click per target, the tick), clicked by the bridge once this job runs
    local ui = args.ui == true
    if ui then
        -- a two-step spell (ACT-9's player route): Revivify = the body, then the ground beside it; Dimension Door =
        -- who goes (the caster when nobody is named), then the landing given as pos. The bridge clicks both.
        local two = A.two_step_spell(base)
        local tele_land = two and args.pos ~= nil and not A.revive_spell(base)
        if #targets < 1 and not tele_land then G.refuse("a hotbar cast needs its targets") end
        if #targets > A.CAST_UI_MAX_TARGETS then
            G.refuse("more than " .. A.CAST_UI_MAX_TARGETS .. " targets in one cast")
        end
        if args.pos and not tele_land then G.refuse("a hotbar cast clicks characters, not a position") end
        if args.upcast then G.refuse("an upcast through the hotbar's level picker is not built yet") end
        if two and A.revive_spell(base) and #targets ~= 1 then
            G.refuse(tostring(base) .. " through the hotbar clicks one body, then the ground beside it")
        end
    elseif #targets > 2 then
        G.refuse("more than two targets is not supported yet")
    end
    for _, t in ipairs(targets) do if not t then G.refuse("bad target") end end
    local pos = args.pos
    if not Ext.Stats.Get(base) then
        -- e.g. Shout_Dodge: BG3 has no Dodge action (live: no such stats entry).
        G.refuse("unknown action " .. base .. " (not an ability id; get_actions lists them)")
    end
    -- S059 08:57-08:59, Dimension Door x3 (targets=Claude + a point, or a point alone): a two-step teleport picks
    -- who goes (an ally within its TargetRadius 2 m, or the caster), then the landing (AreaRadius). The point is the
    -- landing, never the first pick: it was checked against the 2 m pick range and cast at a position with nobody
    -- to send (the game refused), or replaced by the Revivify spot beside the ally.
    local landing
    if pos and A.two_step_spell(base) and not A.revive_spell(base) then
        landing, pos = pos, nil
        if #targets == 0 then targets[1] = char end -- nobody named: the caster goes alone
        if #targets > 1 then G.refuse("a teleport takes one who goes (the caster or an ally beside them), then the landing") end
    end
    -- ACT-9: with the second-pick write off, a two-step cast would be ordered with its first pick only: the game
    -- then waits for the second click (or refuses), and the next action cancels it. Refused before any cost.
    if A.two_step_spell(base) and not targets[2] and not A.TASK_POS2 and not ui then
        if A.revive_spell(base) then
            G.refuse(tostring(base) .. " is a two-step cast (the body, then the spot they stand up on) and this"
                .. " action can't give its second pick yet. Read a Scroll of Revivify by name (use_ability"
                .. " ability=\"Scroll of Revivify\" target=<the body>: it clicks the body, then the ground beside it),"
                .. " or Withers at camp")
        end
        G.refuse(tostring(base) .. " is a two-step teleport (who goes, then the landing) and this action can't give"
            .. " its landing yet: walk, jump or climb there (route),")
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
    -- S030: Guiding Bolt upcast 2 was refused ("cannot be cast at that level"), a wasted call: the game data has
    -- no Projectile_GuidingBolt_2 (nor Inflict Wounds / Chromatic Orb variants), and UseSpell has no slot-level
    -- input. Cast it at its own level and say so.
    local upcast_ignored
    if upcast and bst and not Ext.Stats.Get(base .. "_" .. tostring(upcast)) then
        upcast_ignored = "no level-" .. tostring(upcast) .. " form of this spell can be cast here: cast at level "
            .. tostring(tonumber(bst.Level) or 0)
        upcast = nil
    end

    -- Validate the base spell (known/prepared/cooldown) and the cast itself;
    -- with an upcast, slots are checked at the upcast level (not the base
    -- level, which may be spent).
    local spell = resolve_spell(base, upcast)
    local function prepare()
        local ok0, c = pcall(G.check_cast, char, base, targets[1], pos, upcast, args.force)
        if not ok0 then
            if landing and G.is_refusal(c) and c.reason:find("^target is out of range") then
                G.refuse(c.reason .. " (the ally who goes with the caster must stand beside them; leave targets"
                    .. " empty to go alone)")
            end
            error(c, 0)
        end
        if landing then A.landing_ok(char, base, landing) end
        for i = 2, #targets do
            -- every later pick passes the first one's checks (seen, near, in range); a repeat = another dart/beam
            local t = targets[i]
            G.target_visible(t)
            G.within(char, t, G.MAX_INTERACT_DIST, "target")
            if c.range and U.dist(G.pos(char), G.pos(t)) > c.range + 0.5 then
                G.refuse((i == 2 and "second" or ("target " .. i)) .. " is out of range: move closer first")
            end
        end
        if spell ~= base then
            -- Resources for the upcast variant's own costs.
            local vst = Ext.Stats.Get(spell)
            c.costs = G.parse_costs(vst.UseCosts)
            c.payers = {}
            local in_combat = G.in_combat(char)
            for _, cc in ipairs(c.costs) do
                local turn_res = cc.name == "ActionPoint" or cc.name == "BonusActionPoint"
                    or cc.name == "Movement" or cc.name == "ReactionActionPoint"
                if in_combat or not turn_res then
                    local n, l = G.payer(c.snapshot, cc)
                    if not n then G.refuse("not enough " .. G.friendly(cc.name)) end
                    c.payers[#c.payers + 1] = { name = n, level = l, amount = cc.amount }
                end
            end
        end
        return c
    end
    -- Out of combat, a cast refused only for its range walks into range first, as a click on the ability
    -- does in the game (S018-S028: ~1-3 per session, e.g. S028 Help on a downed Shadowheart 5 m away
    -- stopped a do_actions batch). Our own verified walk, never the engine's (WithoutMove stays 1).
    local ok, check = pcall(prepare)
    local walk
    if not ok then
        local why = G.is_refusal(check) and check.reason or ""
        if why:find("^target is out of range") and targets[1] and not targets[2] and not pos and not ui
            and not G.in_combat(char) and not G.same(char, targets[1]) then
            -- Touch range only (Help, Cure Wounds, a melee attack): approach_point stands beside the target. A
            -- ranged cast keeps the refusal: closing in on a hostile is the agent's call (move_to within).
            local rng, melee = G.spell_range(bst, char)
            local reach = rng and (melee and G.melee_reach(char, targets[1], rng) - A.MELEE_STOP_SLACK or rng - 0.3)
            if reach and reach <= A.CAST_WALK_MAX_REACH then
                reach = math.max(1.2, reach)
                walk = { plan = G.approach_point(char, targets[1], reach), reach = reach }
            end
        end
        if not walk then error(check, 0) end
        check = nil
    end
    local combat = G.in_combat(char)
    -- Never let the cast walk: the Osiris spell task's approach has no path
    -- check (teleports). The range was checked by G.check_cast whenever it is
    -- known; only an unknown range out of combat lets the engine walk into range.
    local without_move = (combat or walk or check.range) and 1 or 0
    local harmful = false
    pcall(function()
        for _, f in ipairs(bst.SpellFlags) do if f == "IsHarmful" then harmful = true end end
    end)

    local function matches(a)
        return G.same(a[1], char) and (a[2] == spell or a[2] == base)
    end
    -- A projectile (arrow, bolt, Fire Bolt) lands after CastedSpell: acceptance 14:5x, a Ranged Attack at
    -- 18 m reported no_damage while its 2 damage arrived later (the agent read a miss). Wait for its hits.
    local settle = A.CAST_SETTLE_MS
    if base:find("^Projectile_") or (bst and bst.SpellType == "Projectile") then
        settle = A.PROJECTILE_SETTLE_MS
    end

    local no_start_ms = (combat and without_move == 1 and base ~= "Projectile_Jump") and A.CAST_NO_START_COMBAT_MS
        or A.CAST_NO_START_MS
    if ui then no_start_ms = A.CAST_UI_NO_START_MS end
    local walk_m = walk and walk.plan.length_m or 0
    local function order(job)
        job.osi_task = "spell"
        job.ordered_ms, job.ordered_pos = RPC.now_ms(), G.pos(char)
        if pos then
            -- Engine form has 6 inputs (6th = WithoutMove), verified live
            -- on bg3le; the 5-input form is kept as a fallback for builds
            -- without it.
            G.osi_forms("UseSpellAtPosition", { char, spell, pos[1], pos[2], pos[3], without_move },
                { char, spell, pos[1], pos[2], pos[3] })
        else
            G.osi("UseSpell", char, spell, targets[1], targets[2] or NULL_GUID, without_move)
            A.give_second_pick(job, char, spell, targets, landing)
        end
    end
    return start("cast", char, {
        eta_s = 2.0 + (walk and walk_eta(walk_m) or 0),
        timeout_ms = 30000 + (walk and walk_timeout(walk_m) or 0),
        interrupt = { combat = walk ~= nil, dialogue = true, attacked = false },
        -- a hotbar cast cancelled by the bridge after the game began it (a late start): say so (the bridge then never
        -- casts it again by Osiris) and leave the player's own cast alone (no purge)
        on_cancel = ui and function(job)
            job.stopping = true
            if job.began then RPC.finish_job(job, "interrupted", { began = true, cast = spell }, "cancelled") end
        end or nil,
        osi = with_walk({
            CastSpell = function(job, a)
                if matches(a) then job.began = true end
            end,
            CastedSpell = function(job, a)
                if matches(a) and not job.casted then
                    -- Hits usually land before CastedSpell; give stragglers a
                    -- short settle window (see on_tick), then report.
                    job.casted = RPC.now_ms()
                    G.item_used(char, spell)
                end
            end,
            CastSpellFailed = function(job, a)
                if not matches(a) then return end
                local why = A.cast_failed_why(char, targets[1], spell)
                if job.ui and not job.clicked_ms and not job.began then
                    -- L8 23:59 (se, Dimension Door): picking the level in the upcast picker sent this 0.12 s
                    -- before the first target click, and the job failed while the bridge went on clicking. In the
                    -- hotbar route it is held until the bridge's clicks are done (act.ui_clicked).
                    job.ui_failed = job.ui_failed or why
                    return
                end
                RPC.finish_job(job, "failed", nil, why)
            end,
            -- AttackedBy(defender, attackerOwner, attacker, damageType, amount, cause, storyActionId)
            AttackedBy = function(job, a)
                if G.same(a[3], char) or G.same(a[2], char) then
                    local tgt = G.guid(a[1])
                    if tgt and G.visible(tgt) then
                        job.hits[#job.hits + 1] = { target = tgt, name = name_of(tgt),
                            amount = tonumber(a[5]), type = a[4] ~= "None" and a[4] or nil }
                        job.last_hit = RPC.now_ms()
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
        }),
        on_tick = function(job)
            if job.phase == "approach" then return approach_tick(job) end
            if job.ui_failed and job.clicked_ms and not job.casted and not job.began
                and RPC.now_ms() - job.clicked_ms >= A.CAST_UI_REFUSED_MS then
                RPC.finish_job(job, "failed", nil, job.ui_failed)
                return
            end
            if not job.casted and not job.began and job.ordered_ms then
                local p = G.pos(char)
                if p and job.ordered_pos and U.dist(p, job.ordered_pos) > 0.5 then
                    job.began = true -- the engine walks into range / the jump took off
                elseif RPC.now_ms() - job.ordered_ms > no_start_ms then
                    local hit = combat and not job.ui and A.hit_ms[G.guid(char)]
                    if hit and not job.reordered and hit.t >= job.ordered_ms - A.CAST_HIT_WINDOW_MS then
                        job.reordered, job.reorder_why = (hit.t - job.ordered_ms) / 1000, hit.why
                        pcall(G.osi, "PurgeOsirisQueue", char)
                        order(job)
                        return
                    end
                    local why = job.ui_failed or ("the game never started it ("
                        .. tostring(math.floor(no_start_ms / 1000)) .. " s, no cast, no step)")
                    if job.reordered then
                        why = why .. " twice: ordered again after " .. job.reorder_why .. " "
                            .. string.format("%.1f s %s the first order", math.abs(job.reordered),
                                job.reordered < 0 and "before" or "after")
                    end
                    if base == "Projectile_Jump" then
                        why = why .. ": it ignores a landing the jump can't reach (too far or too high for this"
                            .. " character, or no clear arc): pick a nearer, lower landing (route / get_layout)"
                    end
                    local blk = combat and G.blocking_status(char)
                    if blk then
                        why = why .. ": the caster is " .. tostring(blk.name) .. (blk.type == "FEAR"
                            and " (a fear status: the game dropped orders at such a turn's start before; nothing"
                                .. " was spent)" or " (it can't act while that lasts)")
                    end
                    pcall(G.osi, "PurgeOsirisQueue", char)
                    RPC.finish_job(job, "failed", nil, why)
                    return
                end
            end
            local now = job.casted and RPC.now_ms()
            -- Report CAST_SETTLE_MS after the cast once the hits are in (250 ms after the last one), at
            -- the latest `settle` after it (a projectile still in flight, or a miss).
            if now and now - job.casted >= A.CAST_SETTLE_MS
                and ((job.last_hit and now - job.last_hit >= A.CAST_SETTLE_MS) or now - job.casted >= settle) then
                local landing
                if base == "Projectile_Jump" and pos and job.ordered_pos then
                    local p = G.pos(char)
                    local last = job.jump_last
                    job.jump_last = p
                    -- still in the air (moved since the last tick): wait for the landing, up to a cap
                    if p and (not last or U.dist(p, last) > 0.05) and now - job.casted < settle + A.JUMP_AIRBORNE_MAX_MS then
                        return
                    end
                    if p then
                        local function flat(a, b) return math.sqrt((a[1] - b[1]) ^ 2 + (a[3] - b[3]) ^ 2) end
                        local off, moved = flat(p, pos), flat(p, job.ordered_pos)
                        if off > A.JUMP_OFF_M and moved < A.JUMP_NOWHERE_M then
                            RPC.finish_job(job, "failed", { cast = spell, moved_m = U.round(moved, 1) },
                                string.format("the jump went nowhere: moved %.1fm, the landing is still %.1fm away"
                                    .. " (the game shortens a landing it can't reach: too far or too high, no clear arc,"
                                    .. " or no floor there): pick a nearer, lower spot on solid floor (get_layout)",
                                    moved, off))
                            return
                        elseif off > A.JUMP_OFF_M then
                            landing = U.round(off, 1)
                        end
                        local drop = (pos[2] or p[2]) - p[2]
                        if drop > A.JUMP_FALL_M then
                            job.fell_ms = job.fell_ms or now
                            if now - job.fell_ms < A.JUMP_FALL_CHECK_MS then return end -- the fall damage lands
                            local dead, downed = G.is_dead(char), G.is_downed(char)
                            if dead or downed then
                                RPC.finish_job(job, "failed", { cast = spell, fell_m = U.round(drop, 1) },
                                    string.format("FELL %.1fm below the landing (to y=%.1f) and %s: the landing had no"
                                        .. " floor at that height (a chasm or a drop). %s Never jump there again; walk"
                                        .. " (route / get_layout) or aim at solid floor at your own height",
                                        drop, p[2], dead and "DIED" or "is DOWNED",
                                        dead and "Load a save or revive them." or "Help them up."))
                                return
                            end
                            job.fell_m = U.round(drop, 1)
                        end
                    end
                end
                local res = { cast = spell, extra_attack = check.extra_attack, walked_into_range = job.walked,
                    upcast_ignored = upcast_ignored, landed_m_from_target = landing, fell_m = job.fell_m,
                    via = ui and "hotbar" or nil, targets_n = ui and #targets or nil }
                if #job.hits > 0 then
                    res.damage = job.hits
                    local total = 0
                    for _, h in ipairs(job.hits) do total = total + (h.amount or 0) end
                    res.total_damage = total
                elseif harmful then
                    res.no_damage = true -- missed, saved, or no damage component
                end
                if #job.statuses > 0 then res.statuses = job.statuses end
                if job.teleport and job.place then
                    -- Dimension Door: where the caster stands now vs the landing asked for.
                    local p = G.pos(char)
                    res.landing = job.place
                    if p then
                        res.now_at = { U.round(p[1], 1), U.round(p[2], 1), U.round(p[3], 1) }
                        res.landed_m_from_target = U.round(U.dist(p, job.place), 1)
                        res.teleported = (job.ordered_pos and U.dist(p, job.ordered_pos) > 1.0) or false
                    end
                end
                -- Target state as the target's health bar shows it (if visible).
                if targets[1] and not G.same(targets[1], char) and G.visible(targets[1]) then
                    res.target = { uuid = targets[1], dead = G.is_dead(targets[1]) or nil,
                        downed = G.is_downed(targets[1]) or nil }
                    if job.place and not job.teleport then
                        res.placed_at = job.place
                        res.revived = not res.target.dead or nil
                    elseif job.revive then
                        res.revived = not res.target.dead or nil
                    end
                end
                RPC.finish_job(job, "done", res)
            end
        end,
        start = function(job)
            job.hits, job.statuses = {}, {}
            if walk then
                return begin_approach(job, targets[1], walk.plan, walk.reach, function(j)
                    local ok2, c = pcall(prepare)
                    if not ok2 then
                        RPC.finish_job(j, "failed", { walked = U.round(walk_m, 1) },
                            G.is_refusal(c) and c.reason or tostring(c))
                        return
                    end
                    check = c
                    j.walked = U.round(walk_m, 1)
                    j.osi_task = "spell"
                    j.ordered_ms, j.ordered_pos = RPC.now_ms(), G.pos(char)
                    G.osi("UseSpell", char, spell, targets[1], NULL_GUID, without_move)
                    A.give_second_pick(j, char, spell, targets, landing)
                end)
            end
            if ui then
                -- no order: the bridge clicks the hotbar now (the player's input); CastSpell/CastedSpell tell the rest
                job.ui = true
                job.ordered_ms, job.ordered_pos = RPC.now_ms(), G.pos(char)
                A.give_second_pick(job, char, spell, targets, landing, true)
                return
            end
            -- S039 21:53: the engine froze inside a jump cast and the crash left no trace: mark it in flight, so a
            -- crash during the jump turns this level's jump planning off at the next start (Navigation.lua guard)
            if pos and base == "Projectile_Jump" and BG3AI.Nav and BG3AI.Nav.inflight then
                job.jump_marked = pcall(BG3AI.Nav.inflight, "jump", pos)
            end
            order(job)
        end,
        on_finish = function(job)
            if job.jump_marked then pcall(BG3AI.Nav.inflightDone, "jump") end -- NAV-7: only its own marker
            if not job.casted then return end
            -- A hotbar cast is the player's own: the game charged it (nothing extra from us).
            if job.ui then return end
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
        local resent = {}
        if not requested then
            -- This member's end went through. Members whose earlier group end the game dropped (S052-S055 "stuck
            -- shared turn": still active, no RequestedEndTurn, only our pending flag) get their EndTurn again once
            -- nobody else in the group is really acting.
            local stale, others = {}, false
            for _, m in ipairs(G.party()) do
                if not G.same(m, char) then
                    local a, r = G.tb_flags(m)
                    if a and not r then
                        if G.end_requested[G.guid(m) or m] then stale[#stale + 1] = m else others = true end
                    end
                end
            end
            if not others then
                for _, m in ipairs(stale) do
                    pcall(G.osi, "EndTurn", m)
                    G.end_requested[G.guid(m) or m] = G.now_ms()
                    resent[#resent + 1] = m
                end
            end
        end
        local w = {}
        for _, m in ipairs(group_waiting(char)) do
            local again = false
            for _, x in ipairs(resent) do if G.same(x, m) then again = true end end
            if not again then w[#w + 1] = m end
        end
        if #w > 0 then res.still_to_act = w end
        if #resent > 0 then res.ended_again = resent end
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
                return
            end
            -- S044/S045: our Spiritual Weapon's EndTurn (×4) and turns during Ketheric's mid-fight lines (×5) were
            -- ignored: 10 s TIMEOUT each. Hand back early so the bridge presses the HUD's End Turn instead.
            if RPC.now_ms() - job.started >= A.END_IGNORED_MS then
                RPC.finish_job(job, "failed", { still_my_turn = true }, A.END_IGNORED)
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
    -- A stack arrives as new items (S031 17:09: Claude's Potion of Healing stack e64 → Gale came in as e410 x2,
    -- e411, e412 and merged; the job waited for e64 and timed out at 8 s, stopping the batch). So an AddedTo
    -- of the same template to the receiver counts, and so does the original leaving the giver.
    local tpl = tostring((G.q("GetTemplate", item)) or "")
    local function same_kind(x)
        return G.same(x, item) or (tpl ~= "" and tostring((G.q("GetTemplate", x)) or "") == tpl)
    end
    return start("transfer", nil, {
        timeout_ms = 8000,
        osi = {
            AddedTo = function(job, a)
                if G.same(a[2], to) and same_kind(a[1]) then RPC.finish_job(job, "done", { to = to, amount = amount }) end
            end,
        },
        -- ShowNotification=1, ClearOriginalOwner=0 (never launder stolen goods).
        start = function() G.osi("ToInventory", item, to, amount, 1, 0) end,
        on_tick = function(job)
            local h = holder(item)
            if G.same(h, to) or (amount >= stack and not G.same(h, from)) then
                RPC.finish_job(job, "done", { to = to, amount = amount })
            end
        end,
    })
end)

------------------------------------------------------------------------
-- act.select {char}
------------------------------------------------------------------------

-- S069 20:45:31 (bg3le log 17031.7): act.select for Claude was done 1 ms after MakePlayerActive (the post-condition
-- GetCurrentCharacter was already Claude) while another member held ClientControl: ui.reactions refused 6 times in
-- 0.6 s. Done = the character holds ClientControl (what the HUD and every ui.* op act on); GetCurrentCharacter counts
-- only while no member holds it (the host fallback, State.selectedCharacter). Still not after SELECT_RESYNC_MS: the
-- order was a no-op (the game's current character already) or lost in a hand-over: select another member, then
-- this one, once. Not by SELECT_FAIL_MS: failed, naming who holds it.
A.SELECT_RESYNC_MS, A.SELECT_FAIL_MS = 600, 2500
local function controls(u)
    local e = G.entity(u)
    local ok, cc = pcall(function() return e and e.ClientControl end)
    return ok and cc ~= nil
end
local function controller()
    for _, u in ipairs(G.party()) do if controls(u) then return u end end
    return nil
end

reg("select", function(args)
    local char = G.actor(args.char)
    local function selected()
        if controls(char) then return true end
        if controller() then return false end
        local user = G.q("GetReservedUserID", char)
        return user ~= nil and G.same(G.q("GetCurrentCharacter", user), char)
    end
    return start("select", char, {
        passive = true,
        timeout_ms = 3000,
        start = function(job)
            job.t0 = RPC.now_ms()
            G.osi("MakePlayerActive", char)
        end,
        on_tick = function(job)
            if selected() then
                RPC.finish_job(job, "done", { selected = true, resynced = job.resynced })
                return
            end
            local dt = RPC.now_ms() - job.t0
            if job.reselect then
                job.reselect = nil
                G.osi("MakePlayerActive", char)
            elseif not job.resynced and dt >= A.SELECT_RESYNC_MS then
                job.resynced = true
                for _, u in ipairs(G.party()) do
                    if not G.same(u, char) and not G.is_dead(u) then
                        G.osi("MakePlayerActive", u)
                        job.reselect = true
                        return
                    end
                end
                G.osi("MakePlayerActive", char)
            elseif dt >= A.SELECT_FAIL_MS then
                local who = controller()
                RPC.finish_job(job, "failed", { holder = who }, "the game kept " .. (who and name_of(who) or "nobody")
                    .. " selected (MakePlayerActive twice, once via another member)")
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
    -- S062 10:00:20: set_party unchain x3 on members already alone came back FAILED: already in the asked state
    -- is done (a player's click on a linked chain does nothing either), not a failure the agent has to read.
    local already = G.b(G.q("InSamePartyGroup", char, leader))
    return start("chain", char, {
        timeout_ms = 3000,
        start = function() if not already then G.osi("AttachToPartyGroup", char, leader) end end,
        on_tick = function(job)
            if G.b(G.q("InSamePartyGroup", char, leader)) then
                RPC.finish_job(job, "done", { chained = true, already = already or nil })
            end
        end,
    })
end)

reg("unchain", function(args)
    local char = G.actor(args.char)
    local alone = true
    for _, p in ipairs(G.party()) do
        if p ~= char and G.b(G.q("InSamePartyGroup", char, p)) then alone = false break end
    end
    return start("unchain", char, {
        timeout_ms = 3000,
        start = function() if not alone then G.osi("DetachFromPartyGroup", char) end end,
        on_tick = function(job)
            for _, p in ipairs(G.party()) do
                if p ~= char and G.b(G.q("InSamePartyGroup", char, p)) then return end
            end
            RPC.finish_job(job, "done", { unchained = true, already = alone or nil })
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
        -- The HUD's camp button is the party's: a dead or downed selected character doesn't grey it (S035 19:38:
        -- Claude fell to death, camp() refused "character is dead" twice). With no char given, any living member.
        local who = args.char
        if not who then
            who = G.host()
            if not pcall(G.actor, who) then
                for _, p in ipairs(G.party()) do
                    if pcall(G.actor, p) then who = p; break end
                end
            end
        end
        local char = G.actor(who)
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
            -- Done when the ACTOR has arrived, not the first party member: S028 15:52:12 / 15:58:52 camp() returned
            -- on Shadowheart's TeleportedToCamp, long_rest pressed CampTravel 40 ms later while Claude was still
            -- on the way (its "teleported e4 to=camp" event came inside the long_rest result), and the rest
            -- screen never opened.
            osi = {
                TeleportedToCamp = function(job, a)
                    if want_in_camp and G.same(a[1], char) then RPC.finish_job(job, "done", { at_camp = true }) end
                end,
                TeleportedFromCamp = function(job, a)
                    if not want_in_camp and G.same(a[1], char) then RPC.finish_job(job, "done", { at_camp = false }) end
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
    local reach = extra and extra.reach or A.USE_REACH
    local plan = G.approach_point(char, target, reach)
    local budget, mv_before = combat_budget(char, plan.length_m)
    local spec = {
        eta_s = walk_eta(plan.length_m) + 0.5, route_m = plan.length_m,
        timeout_ms = 30000 + walk_timeout(plan.length_m),
        interrupt = { combat = budget == nil, dialogue = true, attacked = budget == nil },
        osi = with_walk(extra and extra.osi),
        start = function(job)
            begin_approach(job, target, plan, reach, function(j)
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

-- A character out of sight that is no longer in the fight (S033 18:39-18:41: Umbral Tremors read
-- "not visible" for 4 turns of retargets and skipped attacks): "dead", "off_stage" (dissipated,
-- despawned: an entity the game no longer knows reads off stage too) or "left_fight" (the party is in a
-- fight and it isn't). A player sees each: its portrait leaves the turn order. nil: only out of sight.
local function gone_why(t)
    if G.is_dead(t) then return "dead" end
    if not G.b(G.q("IsOnStage", t)) then return "off_stage" end
    if not G.in_combat(t) then
        for _, p in ipairs(G.party()) do
            if G.in_combat(p) then return "left_fight" end
        end
    end
    return nil
end
A.gone_why = gone_why

-- Read-only follow-up for the bridge after a click: what a player sees on the
-- target (locked padlock icon, open/closed, trap marker of a discovered trap).
RPC.register("act.target_state", function(args)
    local t = G.guid(args.uuid)
    if t and G.b(G.q("IsItem", t)) then
        local h = holder(t)
        if h and G.is_party(h) then return { visible = true, held = true } end
    end
    if t and not G.is_party(t) and not G.b(G.q("IsItem", t)) and not G.visible(t) then
        return { visible = false, gone = gone_why(t) }
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
    -- Right-click menu "Disarm", as for Lockpick (S031 17:10: three left-clicks on a trapped bookcase did nothing;
    -- the agent's right-click -> Disarm by hand was the route).
    return approach("disarm", char, trap, function(job) needs_click(job, trap, "roll", "Disarm") end)
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
        -- Live (setup 14:xx, Last Light Inn): every Harper behind the counter was "no walkable path" with the
        -- 1.8 m use reach (rings 1.0/1.5 m only). Stand off like talk_to: the 2.2 m ring reaches over a counter.
        reach = G.TALK_RANGE,
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
        -- on a ladder: the bridge's queue starts the step after a climb only once the climber is off it (actq)
        if G.b(G.q("HasActiveStatus", u, "CLIMBING")) then row.climbing = true end
        -- a harmful surface underfoot the player sees (fire, acid, ...): the queue's "surface" interrupt
        local N = BG3AI.Nav
        if row.pos and type(N) == "table" and type(N.surfaceAt) == "function" then
            local ok, sf = pcall(N.surfaceAt, G.pos(u))
            -- not Chasm: a jumper over a gap reads Chasm in the air, and the queue cancelled the jump ("STOPPED: e4
            -- Claude is standing in Chasm", S040 22:04:40 / 22:06:25: two Shadowfell jumps; the same jump_to alone landed)
            if ok and sf and sf ~= "Chasm" then row.surface = sf end
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

------------------------------------------------------------------------
-- act.prepare_plan {char, add = [spell], remove = [spell]} (A5): the spellbook change prepare_spells will make,
--   checked first (Guard.prep_plan); {refused = reason} instead of an error. Read-only: the change itself is the
--   spellbook UI's (Client/Spellbook.lua ui.prepare_spells).
-- act.prepared {uuids?}: each preparing caster's prepared spells and limit (party by default). Read-only.
------------------------------------------------------------------------

RPC.register("act.prepare_plan", function(args)
    local ok, res = pcall(G.prep_plan, args.char, args.add, args.remove)
    if ok then return res end
    if G.is_refusal(res) then return { refused = res.reason } end
    error(res, 0)
end)

-- rows without the page look (it rides only in a plan, for the page's cells; this answer goes out every tick)
local function plain_rows(rows)
    local out = {}
    for i, r in ipairs(rows) do out[i] = { id = r.id, name = r.name, level = r.level } end
    return out
end

RPC.register("act.prepared", function(args)
    local out = {}
    for _, u in ipairs(args.uuids or G.party()) do
        local g = G.guid(u)
        local info = g and G.prep_info(g)
        if info and info.prepares then
            out[#out + 1] = { uuid = g, name = name_of(g), max = info.max, count = info.count, read = info.read,
                              prepared = plain_rows(info.prepared), always = plain_rows(info.always),
                              classes = info.classes, stats_max = info.stats_max, class_max = info.class_max }
        end
    end
    return { casters = out }
end)

-- act.scroll_settle {char, final?} (A7): after a combat scroll's target clicks. Once the scroll is used up (gone from
-- the reader's bags, or its stack smaller) it charges what reading it owed and the engine did not -> {read = true,
-- charged = [{resource, amount}]}. Not used up yet: {read = false} (the pending entry stays until final=true).
RPC.register("act.scroll_settle", function(args)
    local char = G.guid(args.char)
    local p = char and A.scroll_pending[char]
    if not p then return { read = false, none = true } end
    local left = 0
    if G.b(G.q("IsItem", p.item)) and G.same(holder(p.item), char) then
        left = tonumber((G.q("GetStackAmount", p.item))) or 1
    end
    if left >= p.stack then
        if args.final then A.scroll_pending[char] = nil end
        return { read = false }
    end
    A.scroll_pending[char] = nil
    local after, charged = G.resources(char), {}
    for _, q in ipairs(p.payers) do
        local owed = q.amount - (q.before - ((after[q.name] or {})[q.level] or 0))
        if owed > 0.01 and G.deduct(char, q.name, q.level, owed) then
            charged[#charged + 1] = { resource = G.friendly(q.name), amount = owed }
        end
    end
    return { read = true, charged = charged }
end)

RPC.register("act.note", function(args)
    local kind = tostring(args.kind or "")
    if not kind:match("^queue_[%w_]+$") then error("act.note: only queue_* events") end
    RPC.push(kind, args.data)
    return { ok = true }
end)

return A
