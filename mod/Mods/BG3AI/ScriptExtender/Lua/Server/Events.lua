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
listen("StatusApplied", 4, function(obj, status, causee, _)
    if not V.statusInfo(status) then return end
    local u = visibleChar(obj)
    if not u then return end
    local data = { target = ref(u), name = V.statusInfo(status).name }
    local src = causee and visibleChar(causee)
    if src and src ~= u then data.source = ref(src) end
    RPC.push("status_applied", data)
end)

listen("StatusRemoved", 4, function(obj, status, _, _)
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
    RPC.push("cast", { caster = ref(u), spell = spell, name = name })
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
    local party = {}
    for i = 1, np do
        local p = osi(Osi.DialogGetInvolvedPlayer, instance, i)
        local u = p and V.uuid(p)
        if u then party[#party + 1] = ref(u) end
    end
    if #party == 0 then return end -- not our dialogue (ambient NPC chatter)
    E.dialogs = E.dialogs or {}
    E.dialogs[instance] = true
    -- Party banter / barks (automated dialogs) play while you walk: flagged so queues don't stop on them.
    local auto = osi(Osi.DialogIsAutomated, instance)
    RPC.push("dialogue_started", { speakers = speakers, party = party,
        automated = (auto == 1 or auto == true) or nil })
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

local function questTitle(questId)
    local S = BG3AI.State
    if S and S.quests then
        for _, q in ipairs(S.quests) do
            if q.id == questId then return U.loca(q.title), q end
        end
    end
    return nil
end

listen("QuestAccepted", 2, function(char, quest)
    if not ctx().is_party[V.uuid(char) or ""] then return end
    local title = questTitle(quest)
    RPC.push("quest_added", { title = title or "New quest" })
end)

listen("QuestUpdateUnlocked", 3, function(char, quest, step)
    if not ctx().is_party[V.uuid(char) or ""] then return end
    local title, q = questTitle(quest)
    local data = { title = title or "Quest" }
    if q then
        for _, st in ipairs(q.steps) do
            if st.id == step then data.text = U.loca(st.desc) end
        end
    end
    RPC.push("quest_update", data)
end)

listen("LeveledUp", 1, function(char)
    local u = V.uuid(char)
    if ctx().is_party[u or ""] then RPC.push("leveled_up", ref(u)) end
end)

-- ApprovalRatingChanged(ratingOwner, ratedEntity, newValue): the toast shows
-- who approves/disapproves, not numbers; report direction only.
listen("ApprovalRatingChanged", 3, function(npc, _, value)
    local u = V.uuid(npc)
    if not u then return end
    local prev = E.approval[u]
    E.approval[u] = value
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
    RPC.push("item_added", data)
end)

listen("Opened", 1, function(item)
    local u = V.uuid(item)
    local e = V.entity(u)
    if not e then return end
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

listen("LongRestFinished", 0, function()
    RPC.push("rest", { type = "long" })
end)

listen("ShortRested", 1, function(char)
    local u = V.uuid(char)
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
    -- A trap became discovered near the party (the orange outline appears).
    local center = c.observers[1] and c.observers[1].pos
    if center and V.index.ready then
        for _, u in ipairs(V.itemsAround(center, 18)) do
            if not E.knownTraps[u] and truthy(osi(Osi.IsTrap, u)) and truthy(osi(Osi.IsTrapDiscovered, u)) then
                E.knownTraps[u] = true
                local e = V.entity(u)
                local p = e and V.pos(e)
                if E.trapsPrimed and p and truthy(osi(Osi.IsOnStage, u)) and V.perceived(c, u, p) then
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

return E
