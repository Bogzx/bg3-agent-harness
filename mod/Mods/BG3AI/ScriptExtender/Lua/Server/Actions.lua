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
-- A30 (S090-S095, the Netherbrain fight): most combat casts that "never started" came with an enemy's cast in the
-- same seconds (Spellplague: Counterspell, Pactsplitter: Psionic Backlash), Keene's Fixers x2 (S094) and Gur
-- Huntwardens (S095) among them; the same cast sent again worked each time with nothing spent. In our own turn an
-- enemy casts only as a reaction. The no-start waits while that reaction plays (until its CastedSpell +
-- CAST_REACT_SETTLE_MS, at most CAST_REACT_HOLD_MS after the order: the 3 s purge could kill a cast the game holds
-- for it), then orders once more when nothing of ours started and nothing was spent.
A.CAST_REACT_HOLD_MS = 8000
A.CAST_REACT_SETTLE_MS = 500
A.react_seen = nil -- {t, done?, caster, spell}: the last cast by a character outside the party
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
-- A27: an ally group's call (Gather Your Allies -> The Watch) is done when newcomers stand within ALLY_CALL_R of its
-- point; they come after its 3.5 s camera wait (GROUND:CameraWait(3.5)), so the result waits up to ALLY_CALL_WAIT_MS.
A.ALLY_CALL_R = 9.0
A.ALLY_CALL_WAIT_MS = 6000
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
        if not G.los_ok(char, target) then
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

-- Live N2/N4 (2026-10-28, bench B3 job 13): a revive stands the character up with an esv::ActionState on layer 1 of
-- its action machine and a ResurrectComponent; both stay until its first order, which ends them (N2: a walk). A
-- short rest before that order removed the component and left the action state unfinished: no Osiris task of the
-- character started again (walks travelled 0, jumps never started, Uses stayed queued; a purge changed nothing).
local function has_comp(char, part)
    local ok, names = pcall(function() return Ext.Entity.Get(char):GetAllComponentNames() end)
    if not ok or type(names) ~= "table" then return nil end
    for _, n in ipairs(names) do
        if tostring(n):find(part, 1, true) then return true end
    end
    return false
end

function A.standing_up(char) return has_comp(char, "ResurrectComponent") == true end

-- An unfinished action state on layer 1 with no stand-up pending: { tid = its TransactionId }, else nil.
function A.held_action(char)
    local ok, st = pcall(function() return Ext.Entity.Get(char).ServerCharacter.AiActionMachine.Layers[1].State end)
    if not ok or st == nil then return nil end
    local okf, fin = pcall(function() return st.Finished end)
    if okf and fin then return nil end
    if has_comp(char, "ResurrectComponent") ~= false then return nil end
    local okt, tid = pcall(function() return st.TransactionId end)
    return { tid = okt and tid or nil }
end

-- The fact a failed start adds when the game holds the character that way ("" otherwise).
function A.held_text(char)
    local h = A.held_action(char)
    if not h then return "" end
    return string.format(": the game holds %s in an unfinished action (layer 1 of its action machine%s, no stand-up"
        .. " pending): none of its orders starts", U.loca(G.q("GetDisplayName", char)) or "the character",
        h.tid and (", TransactionId " .. tostring(h.tid)) or "")
end

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

-- CastSpell / CastedSpell by a character outside the party (A30): its start, and its end on the same caster's
-- CastedSpell (a CastedSpell alone counts as both).
function A.foe_cast(event, caster, spell)
    local g = G.guid(caster)
    if not g or G.is_party(g) then return end
    local now, r = G.now_ms(), A.react_seen
    if event == "CastedSpell" and r and r.caster == g and not r.done then
        r.done = now
    else
        A.react_seen = { t = now, done = event == "CastedSpell" and now or nil, caster = g, spell = tostring(spell) }
    end
end

function A.dispatch(event, args)
    if event == "TurnEnded" and args[1] then
        local g = G.guid(args[1]) or args[1]
        if g then G.end_requested[g] = nil; G.turn_over[g] = G.now_ms() end
    elseif event == "TurnStarted" and args[1] then
        local g = G.guid(args[1]) or args[1]
        if g then G.turn_over[g] = nil; G.end_requested[g] = nil end
        G.note_turn_started(args[1])
    elseif event == "AttackedBy" and args[1] then
        A.stirred(args[1], "a hit on the caster")
    elseif (event == "CastSpell" or event == "CastedSpell") and args[1] then
        A.foe_cast(event, args[1], args[2])
        A.scroll_cast(event, args[1])
        A.note_cast(args[1]) -- the idle teleport watch: a jump right after it is the caster's spell (ActionsMove)
    elseif (event == "Unlocked" or event == "Opened") and args[1] then
        A.unlock_seen(args[1])
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
    UseFinished = 3, Opened = 1, Closed = 1, Unlocked = 3,
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
        if char and not spec.passive and A.watch_idle then A.watch_idle(char, kind) end -- a jump after it (ActionsMove)
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

-- File-local helpers the other parts use.
A.inner = { interrupt = interrupt, osi_tasks = osi_tasks, listen = listen, pos_view = pos_view,
    ev_name = ev_name, start = start, walk_eta = walk_eta, reg = reg }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads A and the
-- helpers in A.inner.
for _, part in ipairs({ "Server/ActionsMove.lua", "Server/ActionsInteract.lua", "Server/ActionsCast.lua",
    "Server/ActionsParty.lua", "Server/ActionsClick.lua" }) do
    Ext.Require(part)
end

return A
