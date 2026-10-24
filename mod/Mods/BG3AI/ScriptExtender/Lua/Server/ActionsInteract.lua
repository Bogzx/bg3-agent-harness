-- Actions, part 3 of 6 (Actions.lua loads the parts in order; split for the 1000-line cap):
-- act.interact, act.pickup and act.talk.
local U = BG3AI.Util
local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = BG3AI.Actions
local I = A.inner
local reg, ev_name, walk_eta, start, osi_tasks = I.reg, I.ev_name, I.walk_eta, I.start, I.osi_tasks
local holder, begin_approach, walk_timeout = I.holder, I.begin_approach, I.walk_timeout
local approach_tick, teleport_check, with_walk = I.approach_tick, I.teleport_check, I.with_walk

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
-- Live 2026-10-24 (S049-end, Karlach reads Revivify on Shadowheart): the scroll was still in her bags 2.6 s after
-- Shadowheart stood up, its cast still playing, and gone later (her action spent): the settle said "not used up".
-- The reader's own cast after the Use (CastSpell, then CastedSpell) is followed, so the settle waits for its end.
function A.scroll_cast(event, caster)
    local p = A.scroll_pending[G.guid(caster)]
    if not p then return end
    if event == "CastSpell" then p.cast = true else p.casted = true end
end
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

-- Only what the game shows on the click (audit C13: no story-lock read from the lock's data, no lever/puzzle hints).
A.STILL_LOCKED = "clicked it: still locked"
-- S085 04:52:05 Sluice Gate, 04:53:03 Vault Door: one Use of a door fired UseFinished twice, success then failure
-- (EVENTS `used … success` then `used …`). The success left the job waiting for the door to open (IsOpened flips a
-- few frames later); the failure then ended it "could not use that" (the door opened; ~28 s and 2 extra turns).
-- The game's success counts: a failure after it is ignored, and a failure waits this long for a success after it.
A.USE_FAIL_GRACE_MS = 300

-- S083 03:55:56-58 (V24): Knock on e720 "No Entry - No Exit"; look read it unlocked, 2 s later interact read IsLocked
-- true again (a live read, no cache: the game's own flag flipped back while it opened the door) and refused "nobody
-- carries its key" with door_opened in the same reply. An item the game unlocked or opened in the last LOCK_FRESH_MS
-- is not refused on that read: standing open, the reply says so; shut, the click is sent and the game answers it.
A.LOCK_FRESH_MS = 10000
A.unlocked_ms = A.unlocked_ms or {} -- item guid -> when Osiris last raised Unlocked/Opened on it
A.RELOCKED = "clicked it: still locked (the game unlocked it moments ago and it is locked again)"
function A.unlock_seen(item)
    local g = G.guid(item)
    if not g then return end
    local now = G.now_ms()
    for k, t in pairs(A.unlocked_ms) do
        if now - t > A.LOCK_FRESH_MS then A.unlocked_ms[k] = nil end
    end
    A.unlocked_ms[g] = now
end
function A.unlocked_lately(item)
    local t = A.unlocked_ms[G.guid(item) or ""]
    if t and G.now_ms() - t <= A.LOCK_FRESH_MS then return t end
end

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
    -- player would select them and click). With no key the click is still sent, as a player's, and the game
    -- answers it (a prompt, a story door opening, or "Locked"): refusing story locks needed a hand click (audit
    -- ACT-6, impl-logs.md:679), and telling them apart read the lock's hidden difficulty id (audit C13).
    local key_item, key_note, keyless, relocked
    if G.b(G.q("IsLocked", target)) then
        local h, k = G.party_key(target, char)
        local fresh = not h and A.unlocked_lately(target)
        if fresh and G.b(G.q("IsOpened", target)) then
            G.refuse("it is open now (the game unlocked or opened it " .. math.floor((G.now_ms() - fresh) / 1000)
                .. " s ago): walk through it (interact on an open door closes it)")
        elseif fresh then
            keyless, relocked = true, true
        elseif not h then
            keyless = true
        end
        if keyless then -- the actor clicks it themselves
        elseif not G.same(h, char) then
            char = h
            G.not_in_dialogue(char)
            G.combat_gate(char, "no_combat")
            G.within(char, target, G.MAX_INTERACT_DIST, "the key holder's target")
        end
        if not keyless then
            key_item = k
            local function nm(u) local ok, n = pcall(function() return BG3AI.Visibility.name(u) end); return ok and n or u end
            key_note = { unlocked_with = nm(k), key_holder = nm(char) }
        end
    end
    local function still_locked()
        return keyless and G.b(G.q("IsLocked", target))
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
                    if not G.b(a[3]) then
                        if not job.use_ok then job.use_fail = job.use_fail or RPC.now_ms() end -- on_tick decides
                        return
                    end
                    job.use_ok, job.use_fail = true, nil
                    if still_locked() and not openable then
                        RPC.finish_job(job, "failed", nil, relocked and A.RELOCKED or A.STILL_LOCKED)
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
            if job.use_fail and RPC.now_ms() - job.use_fail >= A.USE_FAIL_GRACE_MS then
                RPC.finish_job(job, "failed", nil, key_note and G.b(G.q("IsLocked", target))
                    and ("still locked: " .. key_note.unlocked_with .. " did not open it")
                    or (still_locked() and (relocked and A.RELOCKED or A.STILL_LOCKED)) or "could not use that")
                return
            end
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
                local climbing = G.b(G.q("HasActiveStatus", char, "CLIMBING"))
                -- Live P4 2026-10-24 (Knotted Roots from the gate, 3 of 3): the Use first walks from the settle spot
                -- (y 36.74) down onto the ladder's top landing (35.6); 0.8 m of that step purged the Use ~0.35 s
                -- before CLIMBING began, so nothing climbed and `purged` blocked the retry. Until the climb begins,
                -- a walking tick (it moves sideways at least half as far as up or down: no floor is that steep, a
                -- ladder is vertical) carries y0 along, so the checks below measure from where the climb starts.
                if not lj.purged and not climbing and here and lj.last then
                    local h = math.sqrt((here[1] - lj.last[1]) ^ 2 + (here[3] - lj.last[3]) ^ 2)
                    if math.abs(here[2] - lj.last[2]) < 2 * h then lj.y0 = here[2] end
                end
                -- Root cause (live probe 2026-10-11 01:5x, Knotted Roots): the Use stays in the Osiris queue after
                -- the climb and climbs again from the other end (down/up/down, < 0.5 s at each end), so the actor
                -- ended back where it started. Purged once the climb has begun (CLIMBING status, or 0.8 m along), the
                -- climb runs to its end and the actor stays there (probe: 31.59 held for 5 s).
                if not lj.purged and here and lj.y0 and (math.abs(here[2] - lj.y0) > 0.8 or climbing) then
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
                if still_locked() then
                    RPC.finish_job(job, "failed", { used = true }, relocked and A.RELOCKED or A.STILL_LOCKED)
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
