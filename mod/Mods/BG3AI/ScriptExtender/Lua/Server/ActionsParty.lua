-- Actions, part 5 of 6 (Actions.lua loads the parts in order; split for the 1000-line cap):
-- act.end_turn, equip/unequip, transfer, select, chain/unchain, sneak, turn_based, go_to_camp/leave_camp.
local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = BG3AI.Actions
local I = A.inner
local reg, start, holder, name_of, NULL_GUID = I.reg, I.start, I.holder, I.name_of, I.NULL_GUID

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
    local function finish(job, requested, by_flags)
        local res = { ended = true }
        if by_flags then
            -- S087 05:57:40: the game showed this end (RequestedEndTurn / the turn closed), then, while it applied
            -- the group's ends (18 s to TurnEnded), showed the turn open again: Claude read as acting and the next
            -- combat_turn was refused "e4 Claude (yours) must act first". Pending (and confirmed) until TurnEnded.
            local g, t = G.guid(char) or char, G.now_ms()
            G.end_requested[g], G.end_confirmed[g] = t, t
        end
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
            if not G.is_my_turn(char) then finish(job, false, true) return end
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
-- act.end_standup {char, toward}
-- Live N4 (2026-10-28, the bench B3 job 13 replay): a revived character keeps the revive's stand-up (a
-- ResurrectComponent and an action state, A.standing_up) until its first order; a short rest before that order
-- left the action state unfinished and no order of the character started again until a reload. A walk is the
-- order that ended it in N2 (raw moves of 2.8 and 3 m). Out of combat this walks the character STANDUP_STEP_M to
-- the first reachable point, toward `toward` (the reader) first, until the stand-up has ended; in combat its own
-- turn's first order ends it. Live N7 (C1 replay): the walk to the reader's own spot, 1.4 m off, left the stand-up
-- in place for 4 s (no step: the reader stands there), and the Cure Wounds + short rest after it left the
-- unfinished action again.
------------------------------------------------------------------------

A.STANDUP_STEP_M = 3.0
-- Live N8 (C1 replay A, 2026-10-28): the step's walk, ordered right after the revive, sat in the Osiris queue (osi=1)
-- and never started for 4 s; a raw CharacterMoveToPosition 3 m ordered ~7 s after the revive (B) walked 2.98 m and
-- ended the stand-up. A walk that hasn't moved STANDUP_MOVED_M after STANDUP_REORDER_MS is purged and ordered again,
-- up to STANDUP_END_MS after the first order.
A.STANDUP_END_MS = 10000
A.STANDUP_REORDER_MS = 1500
A.STANDUP_MOVED_M = 0.3

-- The point STANDUP_STEP_M from `char` that a plain walk reaches (no jump, no drop, at most twice the step) with no
-- harmful surface on the straight line there or at its end (N.routeHazard, the first metre skipped: the spot it
-- stands on), trying the direction of `toward` first, then 45 degrees either way in turn. Returns the point, or nil
-- and the first harmful surface a reachable point met (app review of #29: a member revived beside a fire was
-- walked into it).
local function standup_step(char, toward)
    local p = G.pos(char)
    if not p then return nil end
    local N = BG3AI.Nav
    local dx, dz = toward[1] - p[1], toward[3] - p[3]
    local a0 = dx * dx + dz * dz > 0.01 and math.atan(dz, dx) or 0
    local hazard
    for _, k in ipairs({ 0, 1, -1, 2, -2, 3, -3, 4 }) do
        local a = a0 + k * math.pi / 4
        local q = { p[1] + A.STANDUP_STEP_M * math.cos(a), p[2], p[3] + A.STANDUP_STEP_M * math.sin(a) }
        local ok, info = G.reachable(char, q)
        if ok and not info.needs_jump and not info.drop and (info.length_m or 0) <= 2 * A.STANDUP_STEP_M then
            local hok, h = true, nil
            if type(N) == "table" and type(N.routeHazard) == "function" then hok, h = pcall(N.routeHazard, { p, q }) end
            if hok and not h then return q end
            hazard = hazard or (hok and h.surface) or "unreadable surface"
        end
    end
    return nil, hazard
end

reg("end_standup", function(args)
    local char = G.actor(args.char)
    if not A.standing_up(char) then return { pending = false } end
    if G.in_combat(char) then return { pending = true, in_combat = true } end
    local toward = args.toward and G.pos(args.toward)
    if not toward then G.refuse("no position to walk toward (toward: a party member in view)") end
    local to, hazard = standup_step(char, toward)
    if not to then
        G.refuse(string.format("%s still stands up from the revive: %s", name_of(char) or "the character",
            hazard and string.format("every point %.0f m away a walk reaches is in or behind a harmful surface (%s)",
                A.STANDUP_STEP_M, hazard)
            or string.format("no point %.0f m away a walk reaches", A.STANDUP_STEP_M)))
    end
    local U = BG3AI.Util
    local function order(job)
        job.orders, job.ordered = (job.orders or 0) + 1, RPC.now_ms()
        G.osi("CharacterMoveToPosition", char, to[1], to[2], to[3], "Run", I.ev_name(job, "su" .. job.orders), job.id)
    end
    return start("end_standup", char, {
        timeout_ms = A.STANDUP_END_MS + 2000,
        start = function(job)
            job.t0, job.from = RPC.now_ms(), G.pos(char)
            job.osi_task = true -- the walk is purged once the stand-up has ended (start(): queued tasks)
            order(job)
        end,
        on_tick = function(job)
            local here = G.pos(char)
            local moved = here and job.from and U.dist(here, job.from) or 0
            if moved >= A.STANDUP_MOVED_M then job.walked = true end
            local res = { orders = job.orders, walked = job.walked or false, stepped_m = U.round(moved, 1) }
            if not A.standing_up(char) then
                res.pending, res.ended = false, true
                RPC.finish_job(job, "done", res)
            elseif RPC.now_ms() - job.t0 >= A.STANDUP_END_MS then
                res.pending = true
                RPC.finish_job(job, "failed", res, string.format("%s still stands up from the revive %.0f s after the"
                    .. " first walk order (ResurrectComponent kept): %d walk order(s), %s", name_of(char) or
                    "the character", A.STANDUP_END_MS / 1000, job.orders, job.walked
                    and string.format("it walked %.1f m", moved) or "none of them started (it never moved)"))
            elseif not job.walked and RPC.now_ms() - job.ordered >= A.STANDUP_REORDER_MS then
                pcall(G.osi, "PurgeOsirisQueue", char)
                order(job)
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
