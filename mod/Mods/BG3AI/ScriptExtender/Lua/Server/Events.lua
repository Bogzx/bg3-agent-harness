-- Fair event stream (server context). Osiris listeners push only facts a
-- player would see: the combat log, toasts, the journal, what the party sees.
-- Never story flags, dialog resource names, hidden rolls or roll subjects.
-- See docs/PROTOCOL.md (Events) and research/02-visibility.md §4.2-4.3.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local E = BG3AI.Events or {}
BG3AI.Events = E

local truthy = U.truthy
local osi = V.osi

E.openContainer = E.openContainer or nil
E.approval = E.approval or {}
E.combats = E.combats or {}

-- Per-event visibility context, cached for one game tick's worth of events.
local ctxCache, ctxTime = nil, 0
local function ctx()
    local t = Ext.Utils.MonotonicTime()
    if not ctxCache or t - ctxTime > 250 then
        ctxCache, ctxTime = V.context(), t
    end
    return ctxCache
end

local function isCharacter(uuid)
    local e = V.entity(uuid)
    return e ~= nil and U.comp(e, "ServerCharacter") ~= nil, e
end

-- Visible character (party always visible).
local function visibleChar(uuid)
    uuid = V.uuid(uuid)
    if not uuid then return nil end
    local isChar, e = isCharacter(uuid)
    if not isChar then return nil end
    local c = ctx()
    if c.is_party[uuid] or V.characterVisible(c, uuid, e) then return uuid end
    return nil
end

local function ref(uuid)
    return { uuid = uuid, name = V.name(uuid) }
end

local function partyInCombat(combatGuid)
    for _, u in ipairs(ctx().party) do
        if osi(Osi.CombatGetGuidFor, u) == combatGuid then return true end
    end
    return false
end

local function listen(name, arity, fn)
    local ok, err = pcall(Ext.Osiris.RegisterListener, name, arity, "after", function(...)
        local ok2, err2 = pcall(fn, ...)
        if not ok2 then Ext.Utils.PrintWarning("[BG3AI] event " .. name .. ": " .. tostring(err2)) end
    end)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] cannot listen to " .. name .. ": " .. tostring(err)) end
end

------------------------------------------------------------------------
-- Combat
------------------------------------------------------------------------

listen("CombatStarted", 1, function(combat)
    -- Party involvement may be registered a frame later; remember and check on round start too.
    E.combats[combat] = { announced = false }
    if partyInCombat(combat) then
        E.combats[combat].announced = true
        RPC.push("combat_started", {})
    end
end)

listen("CombatRoundStarted", 2, function(combat, round)
    local c = E.combats[combat]
    if not partyInCombat(combat) then return end
    if not c or not c.announced then
        E.combats[combat] = { announced = true }
        RPC.push("combat_started", {})
    end
    RPC.push("round_started", { round = round })
end)

listen("CombatEnded", 1, function(combat)
    local c = E.combats[combat]
    E.combats[combat] = nil
    if c and c.announced then RPC.push("combat_ended", {}) end
end)

listen("TurnStarted", 1, function(char)
    local u = visibleChar(char)
    if u then RPC.push("turn_started", ref(u)) end
end)

listen("TurnEnded", 1, function(char)
    local u = visibleChar(char)
    if u then RPC.push("turn_ended", ref(u)) end
end)

-- AttackedBy(defender, attackerOwner, attacker, damageType, damageAmount, damageCause, storyActionID)
listen("AttackedBy", 7, function(defender, attackerOwner, attacker, dtype, amount, cause, _)
    local d = visibleChar(defender)
    if not d then return end
    local data = { target = ref(d), amount = amount, type = dtype }
    local src = visibleChar(attackerOwner) or visibleChar(attacker)
    if src then
        data.source = ref(src)
    elseif V.uuid(attacker) or V.uuid(attackerOwner) then
        data.source = { name = "Someone" } -- unseen attacker, as the combat log shows it
    end
    RPC.push("damage", data)
end)

listen("Died", 1, function(char)
    local u = visibleChar(char)
    if u then RPC.push("died", ref(u)) end
end)

listen("DownedChanged", 2, function(char, downed)
    local u = visibleChar(char)
    if u then RPC.push(truthy(downed) and "downed" or "revived", ref(u)) end
end)

-- StatusApplied(object, status, causee, storyActionID): displayable statuses only, by their shown name.
-- Never the engine's status id: a player never sees it, and ids carry hints the name doesn't
-- (MAG_MYRKULITES_UNDEAD_PRESENCE_BUFF = from a Myrkulite item; fairness audit S023-S027).
local function nearParty(u, m)
    local e = V.entity(u)
    local p = e and V.pos(e)
    if not p then return nil end
    for _, o in ipairs(ctx().observers) do
        if o.pos and U.dist(o.pos, p) <= m then return e end
    end
    return nil
end

-- An item the party sees or remembers (V.itemKnown) whose look changes: a painting lighting up. Not one merely
-- near the party: a painting behind a wall lights up unseen (audit C11).
local function itemEffectEvent(obj, status, on)
    local t = V.itemEffect(status)
    if not t then return false end
    if not on and t == "flashing" then return true end -- the flash ending says nothing new
    local u = V.uuid(obj)
    if u and V.itemKnown(ctx(), u) then
        RPC.push("item_effect", { item = ref(u), state = on and t or ("no longer " .. t) })
    end
    return true
end

listen("StatusApplied", 4, function(obj, status, causee, _)
    if itemEffectEvent(obj, status, true) then return end
    if not V.statusInfo(status) then return end
    local u = visibleChar(obj)
    if not u then return end
    local data = { target = ref(u), name = V.statusInfo(status).name }
    local src = causee and visibleChar(causee)
    if src and src ~= u then data.source = ref(src) end
    RPC.push("status_applied", data)
end)

listen("StatusRemoved", 4, function(obj, status, _, _)
    if itemEffectEvent(obj, status, false) then return end
    if not V.statusInfo(status) then return end
    local u = visibleChar(obj)
    if u then RPC.push("status_removed", { target = ref(u), name = V.statusInfo(status).name }) end
end)

-- CastedSpell(caster, spell, spellType, spellElement, storyActionID)
listen("CastedSpell", 5, function(caster, spell, _, _, _)
    local u = visibleChar(caster)
    if not u then return end
    local st = osi(Ext.Stats.Get, spell)
    local name = st and U.loca(st.DisplayName) or nil
    if not name then return end -- unnamed/technical casts are not shown in the log
    -- roll: how the spell lands (its stats: the tooltip's "Wisdom save" / attack roll), so a 0-damage result reads
    -- as a save or a miss in the fight debrief rather than a resisted damage type.
    local roll
    pcall(function()
        local r = tostring(st.SpellRoll and st.SpellRoll.Default or "")
        roll = r:find("SavingThrow%(") and "save" or (r:find("Attack%(") and "attack") or nil
    end)
    -- spell: the engine id, only for our own casters (the hotbar's spell, what a plan names). An enemy's cast shows
    -- a player only its display name; its id can carry hints the name doesn't (audit ACT-14, as for status ids).
    RPC.push("cast", { caster = ref(u), spell = ctx().is_party[u] and spell or nil, name = name, roll = roll })
end)

------------------------------------------------------------------------
-- Rolls: only what the roll UI / overheads show; never the subject or event.
-- RollResult(eventName, roller, rollSubject, resultType, isActiveRoll, criticality)
------------------------------------------------------------------------

listen("RollResult", 6, function(_, roller, _, result, isActive, crit)
    local u = V.uuid(roller)
    local c = ctx()
    if not (u and c.is_party[u]) then return end
    local success = truthy(result)
    local active = truthy(isActive)
    if not active and not success and c.rules.HIDE_FAILED_PERCEPTION_ROLLS then return end
    if not active and c.rules.DIALOGUE_PASSIVE_ROLL_HIDE and V.inDialog(u) then return end
    local data = { roller = ref(u), success = success, active = active }
    -- Osiris criticality: 1 = natural 20, 2 = natural 1 (S009: a nat-1 Persuasion read as "critical=2").
    if crit and crit ~= 0 then data.critical = ({ [1] = "success", [2] = "fail" })[tonumber(crit)] or crit end
    RPC.push("roll_result", data)
end)

------------------------------------------------------------------------
-- Dialogue: speakers only (never the dialog resource).
------------------------------------------------------------------------

listen("DialogStarted", 2, function(_, instance)
    local speakers = {}
    local n = osi(Osi.DialogGetNumberOfInvolvedNPCs, instance) or 0
    for i = 1, n do
        local npc = osi(Osi.DialogGetInvolvedNPC, instance, i)
        local u = npc and V.uuid(npc)
        if u then speakers[#speakers + 1] = ref(u) end
    end
    local np = osi(Osi.DialogGetNumberOfInvolvedPlayers, instance) or 0
    local party, at = {}, nil
    for i = 1, np do
        local p = osi(Osi.DialogGetInvolvedPlayer, instance, i)
        local u = p and V.uuid(p)
        if u then
            party[#party + 1] = ref(u)
            if not at then
                local e = V.entity(u)
                at = e and V.pos(e)
            end
        end
    end
    if #party == 0 then return end -- not our dialogue (ambient NPC chatter)
    E.dialogs = E.dialogs or {}
    E.dialogs[instance] = true
    -- Party banter / barks (automated dialogs) play while you walk: flagged so queues don't stop on them.
    local auto = osi(Osi.DialogIsAutomated, instance)
    -- at/level: where the party member stood when the talk began (the bridge's off-limits areas, vis V26).
    RPC.push("dialogue_started", { speakers = speakers, party = party,
        automated = (auto == 1 or auto == true) or nil, at = at and U.vec(at, 1) or nil,
        level = V.levelNow and V.levelNow() or nil })
end)

listen("DialogEnded", 2, function(_, instance)
    if E.dialogs and E.dialogs[instance] then
        E.dialogs[instance] = nil
        RPC.push("dialogue_ended", {})
    end
end)

------------------------------------------------------------------------
-- Journal, progression, approval
------------------------------------------------------------------------

-- The title of a quest the journal shows (QuestVisiblity, a localised title, as State.journal), else nil: hidden and
-- technical quests ("%%% EMPTY", no prototype) never reach the agent (audit C10).
local function questTitle(questId)
    local S = BG3AI.State
    local quests = S and (S.loadQuests and S.loadQuests() or S.quests)
    for _, q in ipairs(quests or {}) do
        if q.id == questId then
            local title = q.visible and U.loca(q.title)
            if title then return title, q end
            return nil
        end
    end
    return nil
end

listen("QuestAccepted", 2, function(char, quest)
    if not ctx().is_party[V.uuid(char) or ""] then return end
    local title = questTitle(quest)
    if title then RPC.push("quest_added", { title = title }) end
end)

-- Only a step whose text the journal shows (State.journal skips the others).
listen("QuestUpdateUnlocked", 3, function(char, quest, step)
    if not ctx().is_party[V.uuid(char) or ""] then return end
    local title, q = questTitle(quest)
    if not title then return end
    for _, st in ipairs(q.steps) do
        local text = st.id == step and U.loca(st.desc)
        if text then return RPC.push("quest_update", { title = title, text = text }) end
    end
end)

listen("LeveledUp", 1, function(char)
    local u = V.uuid(char)
    if ctx().is_party[u or ""] then RPC.push("leveled_up", ref(u)) end
end)

-- ApprovalRatingChanged(ratingOwner, ratedEntity, newValue): the toast shows
-- who approves/disapproves, not numbers; report direction only. E.approval is keyed owner|rated and seeded from
-- GetApprovalRating by the poll (E.seedApproval): unseeded, the first change per companion after every load had no
-- previous value and was dropped (audit ACT-15).
local function approvalKey(owner, rated) return owner .. "|" .. (rated or "") end

function E.seedApproval(c)
    local host = V.uuid(osi(Osi.GetHostCharacter)) or c.party[1]
    if not host then return end
    E.approvalAsked = E.approvalAsked or {}
    for _, u in ipairs(c.party) do
        local k = approvalKey(u, host)
        if u ~= host and E.approval[k] == nil and not E.approvalAsked[k] then
            E.approvalAsked[k] = true -- once per pair and Lua state: a hireling has no rating to read every poll
            local v = osi(Osi.GetApprovalRating, u, host)
            if type(v) == "number" then E.approval[k] = v end
        end
    end
end

listen("ApprovalRatingChanged", 3, function(npc, rated, value)
    local u = V.uuid(npc)
    if not u then return end
    local k = approvalKey(u, V.uuid(rated))
    local prev = E.approval[k]
    E.approval[k] = value
    if prev == nil or prev == value then return end
    RPC.push("approval", { npc = ref(u), direction = value > prev and "approves" or "disapproves" })
end)

------------------------------------------------------------------------
-- Items, containers, use
------------------------------------------------------------------------

-- AddedTo(item, holder, addType)
listen("AddedTo", 3, function(item, holder, _)
    local h = V.uuid(holder)
    if not ctx().is_party[h or ""] then return end
    local u = V.uuid(item)
    local data = { owner = ref(h), item = ref(u) }
    local n = osi(Osi.GetStackAmount, u)
    if n and n > 1 then data.count = n end
    -- a quest item (the inventory's story mark, get_inventory "story"): progress.py scores the first one in hand
    local ie = V.entity(u)
    pcall(function() if ie and ie.ServerItem and ie.ServerItem.StoryItem then data.story = true end end)
    RPC.push("item_added", data)
end)

-- Doors/walls opening in view: by a script too (Grand Mausoleum: the 3rd right press opens two
-- Stone Walls, PROC_ItemUnlockAndOpen). A player sees and hears them slide.
-- One the party opened itself is in the interact result already ("done — opened").
E.DOOR_OPEN_M = 30
E.partyUse = E.partyUse or {}
listen("UseStarted", 2, function(char, item)
    local c = V.uuid(char)
    if ctx().is_party[c or ""] then E.partyUse[V.uuid(item) or ""] = Ext.Utils.MonotonicTime() end
end)

listen("Opened", 1, function(item)
    local u = V.uuid(item)
    local e = V.entity(u)
    if not e then return end
    if V.itemKind(u, e) == "door" then
        local used = E.partyUse[u]
        E.partyUse[u] = nil
        if used and Ext.Utils.MonotonicTime() - used < 15000 then return end
        if nearParty(u, E.DOOR_OPEN_M) and V.itemVisible(ctx(), u, e) then RPC.push("door_opened", { item = ref(u) }) end
        return
    end
    local c = ctx()
    -- Only containers the party opened near itself are "on screen".
    local p = V.pos(e)
    if not p then return end
    local near = false
    for _, o in ipairs(c.observers) do
        if o.pos and U.dist(o.pos, p) < 4.0 then near = true end
    end
    if not near then return end
    E.openContainer = { uuid = u, t = Ext.Utils.MonotonicTime() }
    RPC.push("opened", { item = ref(u) })
end)

listen("Closed", 1, function(item)
    local u = V.uuid(item)
    if E.openContainer and E.openContainer.uuid == u then
        E.openContainer = nil
        RPC.push("closed", { item = ref(u) })
    end
end)

-- UseFinished(character, item, success)
listen("UseFinished", 3, function(char, item, success)
    local c = V.uuid(char)
    if not ctx().is_party[c or ""] then return end
    RPC.push("used", { char = ref(c), item = ref(V.uuid(item)), success = truthy(success) })
end)

------------------------------------------------------------------------
-- World / rest
------------------------------------------------------------------------

listen("LevelGameplayStarted", 2, function(level, _)
    ctxCache = nil
    E.trapsPrimed = false
    -- Fairness: the agent sees the region name a player sees (loading screen / map), not the level id.
    RPC.push("level_changed", { region = BG3AI.State and BG3AI.State.REGION_NAMES[level] or level })
end)

listen("TeleportedToCamp", 1, function(char)
    local u = V.uuid(char)
    if ctx().is_party[u or ""] then RPC.push("teleported", { char = ref(u), to = "camp" }) end
end)

-- The camp's End-the-Day handshake (GLO_Camp.txt): HUD button -> EndTheDayRequested -> the engine's gather
-- checks (RequestGatherAtCampSuccess/Fail) -> story checks -> RequestEndTheDay -> RequestEndTheDaySuccess/Fail ->
-- night mode / rest screen. S028 15:52-15:59 + S029 16:04: long_rest "rest screen didn't open" x7 with no box and
-- nothing on screen; these events say where the request died (a player sees the refusal notice the same way).
local REST_FLOW = {
    { "EndTheDayRequested", 1, "requested" }, { "RequestGatherAtCampFail", 1, "gather_failed" },
    { "RequestGatherAtCampSuccess", 1, "gathered" }, { "RequestEndTheDayFail", 0, "end_day_failed" },
    { "RequestEndTheDaySuccess", 0, "end_day_ok" }, { "LongRestStartFailed", 0, "rest_start_failed" },
    { "LongRestCancelled", 0, "rest_cancelled" },
    -- S031 17:15: can_rest clean, requested -> gathered, then no answer for 14 s and no "End the day?" box (the
    -- engine's ready check, shown within ~1 s in every success S024-S026). Did a ready check run, how did it end,
    -- and did the engine deny the rest with a reason? (arg 1 is kept: the check's id / the deny reason)
    { "ReadyCheckPassed", 1, "ready_ok" }, { "ReadyCheckFailed", 1, "ready_failed" },
    { "RequestLongRestDeny", 1, "rest_denied" }, { "RequestLongRestWaiting", 0, "rest_waiting" },
    { "LongRestStarted", 0, "rest_started" },
}
E.restFlow = E.restFlow or {}
for _, ev in ipairs(REST_FLOW) do
    listen(ev[1], ev[2], function(a1)
        local step = ev[3]
        if ev[2] == 1 and step ~= "requested" and step ~= "gathered" and step ~= "gather_failed" and a1 ~= nil then
            step = step .. ":" .. tostring(a1):sub(1, 60)
        end
        E.restFlow[#E.restFlow + 1] = { step = step, at = Ext.Utils.MonotonicTime() }
        if #E.restFlow > 20 then table.remove(E.restFlow, 1) end
        RPC.push("rest_flow", { step = step })
    end)
end

listen("LongRestFinished", 0, function()
    if BG3AI.Guard and BG3AI.Guard.item_rest then BG3AI.Guard.item_rest("long") end
    RPC.push("rest", { type = "long" })
end)

listen("ShortRested", 1, function(char)
    local u = V.uuid(char)
    if BG3AI.Guard and BG3AI.Guard.item_rest then BG3AI.Guard.item_rest("short", u) end
    if ctx().is_party[u or ""] then RPC.push("rest", { type = "short", char = ref(u) }) end
end)

------------------------------------------------------------------------
-- Polled facts: gold, newly spotted traps near the party.
------------------------------------------------------------------------

E.lastGold = E.lastGold or nil
E.knownTraps = E.knownTraps or {}
E.lastPoll = 0

local function poll()
    local t = Ext.Utils.MonotonicTime()
    if t - E.lastPoll < 1000 then return end
    E.lastPoll = t
    if Ext.Utils.GetGameState and tostring(Ext.Utils.GetGameState()) ~= "Running" then return end
    local c = V.context()
    ctxCache, ctxTime = c, t
    local host = c.party[1]
    if not host then return end
    local gold = osi(Osi.PartyGetGold, host)
    if gold and E.lastGold and gold ~= E.lastGold then
        RPC.push("gold_changed", { gold = gold, delta = gold - E.lastGold })
    end
    E.lastGold = gold
    E.seedApproval(c)
    -- A trap became discovered near the party (the orange outline appears).
    local center = c.observers[1] and c.observers[1].pos
    if center and V.index.ready then
        for _, u in ipairs(V.itemsAround(center, 18)) do
            if not E.knownTraps[u] and truthy(osi(Osi.IsTrap, u)) and truthy(osi(Osi.IsTrapDiscovered, u)) then
                E.knownTraps[u] = true
                local e = V.entity(u)
                local p = e and V.pos(e)
                -- V.itemVisible, not V.perceived alone: it also notes the trap as seen (V.staticSeen), so a disarm
                -- right after the stop finds it known (V.itemKnown). S064 11:37 / S065 11:40: "spotted e302", the
                -- walk stopped 10 m short, then disarm(e302) "target is not visible" (the CanSee that spotted it was
                -- gone) until the agent walked to 3-5 m.
                if E.trapsPrimed and p and V.itemVisible(c, u, e, p) then
                    RPC.push("spotted", { item = ref(u), kind = "trap", pos = U.vec(p, 1) })
                end
            end
        end
        -- First pass only learns traps that were already discovered before we loaded.
        E.trapsPrimed = true
    end
end

if not E.tickSubscribed then
    E.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(poll)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] events poll: " .. tostring(err)) end
    end)
end

RPC.register("state.open_container", function()
    return E.openContainer and { uuid = E.openContainer.uuid, name = V.name(E.openContainer.uuid) } or {}
end)

-- The engine's own long-rest validation per party member: CanDoRest.LongRestErrorFlags (RestErrorFlags:
-- GlobalDisabled, Script, Dialog, Combat, FTB, NotEnoughResources, DownedOrDead, DropInCCActive, Location). The HUD
-- greys the Long Rest button and its tooltip names the reason, so a player sees these. S030 16:30: the
-- End-the-Day handshake ran (requested -> gathered) and the engine answered RequestEndTheDayFail ~10 s later.
local function flagList(f)
    if type(f) ~= "table" and type(f) ~= "userdata" then return {} end
    local out = {}
    local ok = pcall(function() for _, n in ipairs(f) do out[#out + 1] = tostring(n) end end)
    if not ok or #out == 0 then
        local ok2, parsed = pcall(function() return Ext.Json.Parse(Ext.Json.Stringify(f)) end)
        out = (ok2 and type(parsed) == "table") and parsed or {}
    end
    return out
end
E.flagList = flagList

function E.canRest()
    local blocked = {}
    for _, u in ipairs(V.context().party) do
        local e = V.entity(u)
        local c = e and U.comp(e, "CanDoRest")
        local flags = c and flagList(c.LongRestErrorFlags) or {}
        if #flags > 0 then blocked[#blocked + 1] = { name = V.name(u), uuid = u, flags = flags } end
    end
    local ok, g = pcall(Ext.Entity.GetAllEntitiesWithComponent, "GlobalLongRestDisabled")
    local states = {}
    local ok2, ls = pcall(Ext.Entity.GetAllEntitiesWithComponent, "LongRestState")
    if ok2 then
        for _, e in ipairs(ls) do
            pcall(function() states[#states + 1] = Ext.Json.Stringify(e.LongRestState, { Beautify = false,
                MaxDepth = 2, IterateUserdata = true, AvoidRecursion = true }):sub(1, 300) end)
        end
    end
    return { blocked = blocked, global_disabled = ok and #g or -1, long_rest_state = states }
end

-- camp.can_rest -> {blocked=[{name, flags}], global_disabled, long_rest_state}: asked before the rest button press.
RPC.register("camp.can_rest", function() return E.canRest() end)

-- camp.rest_flow {since_ms?} -> {steps=[...], step_ms=[...], story={...}, can_rest={...}}: the End-the-Day
-- handshake steps seen recently (step_ms: how long ago each one fired), the camp story flags and the engine's
-- rest validation (diagnostics for the bridge's metrics log; can_rest's reasons are shown to the agent).
RPC.register("camp.rest_flow", function(args)
    local now = Ext.Utils.MonotonicTime()
    local since = now - (tonumber(args and args.since_ms) or 30000)
    local steps, stepMs = {}, {}
    for _, f in ipairs(E.restFlow or {}) do
        if f.at >= since then steps[#steps + 1], stepMs[#stepMs + 1] = f.step, math.floor(now - f.at) end
    end
    local story = {}
    for _, n in ipairs({ "DB_GoingToCamp", "DB_CampRequestHandled", "DB_Camp_NightMode", "DB_CAMP_JustWokeUp",
                         "DB_Camp_GoingToSleep", "DB_Camp_LongRest_CodeIsWaiting", "DB_PlayerInCamp" }) do
        local ok, rows = pcall(function() return Osi[n]:Get(nil) end)
        story[n] = ok and rows and #rows or -1
    end
    local okc, can = pcall(E.canRest)
    return { steps = steps, step_ms = stepMs, story = story, can_rest = okc and can or nil }
end)

return E
