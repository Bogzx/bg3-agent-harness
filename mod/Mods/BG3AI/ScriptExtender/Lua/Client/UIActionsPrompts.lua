-- UIActions, part 2 of 3 (UIActions.lua loads the parts in order; split for the 1000-line cap):
-- the active roll, reaction prompts and loot.
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState
local A = BG3AI.UIActions
local I = A.inner
local dialogueSnapshot, waitJob, settleDialogue = I.dialogueSnapshot, I.waitJob, I.settleDialogue
local check, sig, try = I.check, I.sig, I.try

------------------------------------------------------------------------
-- Active roll
--
-- ui.roll {boosts?:[i], reroll?:bool, close?:bool, dry?}
--   phase ready  : add the listed boosts (indexes into roll.boosts from
--                  ui.state), then roll. Job ends at the result.
--   phase result : reroll=true spends Inspiration (RollAgainCommand) and
--                  waits for the new result; otherwise Continue closes the
--                  panel and the job settles like dialogue_choose.
------------------------------------------------------------------------

local function rollWidget()
    local w = UI.widget("ActiveRoll")
    return w, w and try(function() return w.DataContext end)
end

local function rollPhase(dc)
    local st = tostring(UI.prop(dc, "RollState"))
    return st
end

-- A start command sent while the panel is still opening is dropped: live 2026-10-10 a lockpick roll_check ~1 s
-- after the roll screen appeared sat in WaitForStart for 20 s (timeout); the same command 2 s later rolled.
-- So while the roll has not started, the start command is sent again every ROLL_RETRIGGER_MS.
local ROLL_RETRIGGER_MS = 1200
local function startRoll(dc)
    local ok, err = UI.command(dc, "UserTriggeredNextPhaseCommand", nil)
    if not ok then ok, err = UI.command(dc, "TriggerNextPhaseCommand", nil) end
    return ok, err
end

-- The roll's state timeline (ms since the call) for the bridge's metrics log: where do the 7-10 s of a dialogue
-- roll_check go (S010-S027: ~6.8 s from the start command to ResultReady, every time).
local function notePhase(j, st)
    if st ~= j.last_state then
        j.last_state = st
        j.phases = (j.phases and (j.phases .. " ") or "") .. st .. "@" .. (RPC.now_ms() - (j.t0 or RPC.now_ms()))
    end
end

-- waitRollResult(kind, retrigger, reroll): done at ResultReady. WaitForReRoll is NOT a result: after
-- RollAgainCommand (Inspiration) the panel waits there for the dice to be thrown again, like WaitForStart
-- (ActiveRoll.xaml: same boost list / tools enabled). S013-S026: 7/7 reroll_on_fail calls returned the first
-- (failed) result again, because ResultReady was still showing the frame after the command and WaitForReRoll
-- counted as done; the bridge then pressed Continue on the failure.
-- reroll=true: the old result must go away first (state leaves ResultReady); if it never does within
-- ROLL_REROLL_LEAVE_MS, the game ignored the command: done with reroll_ignored.
local ROLL_REROLL_LEAVE_MS = 3000
local function waitRollResult(kind, retrigger, reroll)
    return waitJob(kind, 20000, function(j)
        local w, dc = rollWidget()
        if not w then return "done", { closed = true, phases = j.phases } end
        local st = rollPhase(dc)
        notePhase(j, st)
        if st ~= "ResultReady" then j.left_result = true end
        if st == "ResultReady" and (j.left_result or not reroll) then
            local wmap = UI.widgetMap()
            return "done", { roll = S.roll(wmap), retriggered = (j.retriggered or 0) > 0 and j.retriggered or nil,
                rerolled = reroll and true or nil, phases = j.phases }
        end
        if st == "ResultReady" and RPC.now_ms() - j.t0 >= ROLL_REROLL_LEAVE_MS then
            return "done", { roll = S.roll(UI.widgetMap()), reroll_ignored = true, phases = j.phases }
        end
        local waiting = st == "WaitForStart" or st == "WaitForReRoll"
        if (retrigger or reroll) and (waiting or st == "IntroductionAnimation")
                and RPC.now_ms() - (j.triggered_at or 0) >= ROLL_RETRIGGER_MS then
            -- After a reroll the dice wait to be thrown: throw them at once (no 1.2 s wait for the first one).
            if waiting and (j.triggered_at or st == "WaitForReRoll") then
                startRoll(dc)
                if st == "WaitForStart" or j.rethrown then j.retriggered = (j.retriggered or 0) + 1 end
                if st == "WaitForReRoll" then j.rethrown = true end
            end
            j.triggered_at = RPC.now_ms()
        end
    end, function(j)
        j.t0 = RPC.now_ms()
        if not reroll then j.triggered_at = j.t0 end
    end)
end
A.ROLL_REROLL_LEAVE_MS = ROLL_REROLL_LEAVE_MS

-- A boost by its 1-based index in roll.boosts or by name ("Guidance", case-insensitive; the bridge sends names:
-- S024 roll_check(boosts="Guidance") failed "attempt to concatenate a nil value" from tonumber("Guidance")).
local function findBoost(dc, want)
    local i = tonumber(want)
    if i then return UI.path(dc, "BoostModifierList." .. i), i end
    local key = tostring(want):lower()
    local names = {}
    for n = 1, 12 do
        local vm = UI.path(dc, "BoostModifierList." .. n)
        if not vm then break end
        local name = UI.prop(vm, "Name") or UI.prop(vm, "Description")
        name = name and tostring(name) or ""
        names[#names + 1] = name
        if name:lower() == key or name:lower():find(key, 1, true) then return vm, n end
    end
    return nil, nil, (#names > 0 and ("offered: " .. table.concat(names, ", ")) or "the roll offers no boosts")
end

RPC.register("ui.roll", function(args)
    local w, dc = rollWidget()
    if not w then error("no active roll on screen") end
    local st = rollPhase(dc)
    if st == "WaitForStart" or st == "IntroductionAnimation" or st == "WaitForReRoll" then
        local added = {}
        for _, bi in ipairs(args.boosts or {}) do
            local boost, idx, offered = findBoost(dc, bi)
            if not boost then error("no boost '" .. tostring(bi) .. "' on this roll (" .. tostring(offered) .. ")") end
            -- XAML passes a PassThrough multi-binding here; the VM itself is
            -- the best available parameter on bg3le (untested live).
            local ok, err = UI.command(dc, "AddModifierCommand", boost, args.dry)
            if not ok then error("boost " .. tostring(bi) .. ": " .. tostring(err)) end
            added[#added + 1] = idx
        end
        local ok, err = UI.command(dc, "UserTriggeredNextPhaseCommand", nil, args.dry)
        if not ok then check(UI.command(dc, "TriggerNextPhaseCommand", nil, args.dry), err) end
        if args.dry then return { can = true } end
        return waitRollResult("ui.roll", true)
    elseif st == "ResultReady" then
        if args.reroll then
            check(UI.command(dc, "RollAgainCommand", nil, args.dry))
            if args.dry then return { can = true } end
            return waitRollResult("ui.roll", false, true)
        end
        local _, d0 = dialogueSnapshot()
        local before = sig(d0)
        local ok, err = UI.command(dc, "ContinueCommand", nil, args.dry)
        if not ok then check(UI.command(dc, "CancelCommand", nil, args.dry), err) end
        if args.dry then return { can = true } end
        return settleDialogue("ui.roll", before, { timeout_ms = 30000, leaving = "roll",
            before_text = d0 and d0.text, before_speaker = d0 and d0.speaker })
    end
    error("roll is animating (" .. st .. "); poll ui.state")
end)

------------------------------------------------------------------------
-- Reaction prompt
--
-- ui.react {use, which?, dry?}
--   use=false: "Don't react" (InterruptDontReactPlayerCommand(event)).
--   use=true : which = option number from ui.state reaction.options selects
--              that reaction's toggle first; then "React"
--              (InterruptAcceptPlayerCommand(event)).
--              A prompt with several rows (Gale's Shield at levels I-VI) -> {needs_click = which, rows}: the bridge
--              clicks that row with the mouse (bridge reactclick.py), nothing is sent here.
-- ui.react_probe {which?, watch?} -> {open, events, rows, over?, over_row?}
--   For that click: is the cursor over row `which` (over), or over another row (over_row = its number)?
--   watch=true reads only whether the prompt shows and its event count (the bridge's watch after the click).
------------------------------------------------------------------------

-- The reaction popup on screen (the shown page of that name, as ui.state reads it), nil when none is shown.
-- S091 08:11:01 / 08:12:16: two answers timed out after 10 s ("react: TIMEOUT") with the prompt gone from the screen
-- (the waits after them saw none): the wait read the first page of that name, left hidden with its events.
local function reactionPopup()
    local w = UI.widgetMap().ReactionDecisionPopup
    return w and UI.isVisible(w.el) and w.el or nil
end

local function reactRows(w)
    return UI.findAll(w, function(e) return UI.name(e) == "ReactButton" end, { prune = true })
end

RPC.register("ui.react", function(args)
    local w = reactionPopup() or error("no reaction prompt on screen")
    local dc = try(function() return w.DataContext end)
    local ev = UI.path(dc, "Data.InterruptEvents.1") or error("no pending interrupt")
    if args.use ~= false then
        -- S091 08:12:16 + S092 08:43:38 / 08:44:52 (TIMEOUT 4 of 5, by name and by number): on a prompt with one row per
        -- spell level (Gale's Shield I-VI) the row's press + InterruptAcceptPlayerCommand never answered it (the
        -- Accept button is collapsed for a lone interruptor), while each hand click on a Shield row cast it at once
        -- (S092 08:45:05, 08:47:50, 08:51:28, 08:53:30, 08:55:36). Single-row prompts (Counterspell, Shield Blow) go on
        -- by command, which works live; so do prompts with several interruptors (their Accept button shows).
        local btns = reactRows(w)
        local roots = UI.findAll(w, function(e) return UI.name(e) == "InterruptorRoot" end, { prune = true })
        if #btns > 1 and #roots <= 1 then
            local i = tonumber(args.which) or 1
            if not btns[i] then error("no reaction option " .. tostring(args.which) .. " (" .. #btns .. " shown)") end
            return { needs_click = i, rows = #btns }
        end
    end
    if args.use == false then
        check(UI.command(dc, "InterruptDontReactPlayerCommand", ev, args.dry))
    else
        if args.which then
            -- ReactButton toggles, in display order; their CommandParameter
            -- is the PassThrough (decision, interruptor) object.
            local btns = reactRows(w)
            local b = btns[tonumber(args.which)] or error("no reaction option " .. tostring(args.which) .. " (" .. #btns .. " shown)")
            if UI.prop(b, "IsChecked") ~= true then check(UI.press(b, args.dry)) end
        end
        check(UI.command(dc, "InterruptAcceptPlayerCommand", ev, args.dry))
    end
    if args.dry then return { can = true } end
    local before = UI.count(UI.path(dc, "Data.InterruptEvents"))
    return waitJob("ui.react", 10000, function()
        local w2 = reactionPopup()
        local d2 = w2 and try(function() return w2.DataContext end)
        local n = d2 and UI.count(UI.path(d2, "Data.InterruptEvents")) or 0
        if n < before or n == 0 then
            local wmap = UI.widgetMap()
            return "done", { pending = n > 0 and n or nil, reaction = n > 0 and S.reaction(wmap) or nil }
        end
    end)
end)

RPC.register("ui.react_probe", function(args)
    local w = reactionPopup()
    local dc = w and try(function() return w.DataContext end)
    local n = dc and UI.count(UI.path(dc, "Data.InterruptEvents")) or 0
    local out = { open = w ~= nil and n > 0, events = n }
    if not out.open or args.watch then return out end
    local btns = reactRows(w)
    out.rows = #btns
    local want = tonumber(args.which)
    for i, b in ipairs(btns) do
        if UI.prop(b, "IsMouseOver") == true then
            if i == want then out.over = true else out.over_row = i end
        end
    end
    return out
end)

------------------------------------------------------------------------
-- Loot
--
-- ui.loot {all?:true, slots?:[i], close?:bool, dry?}
--   all  : "Take all" (TakeAllAndCloseCommand.ActionCommand); job waits for
--          the container panel to close.
--   slots: take single slots by the index ui.state container.slots reports
--          (TakeItemCommand / PickUpCommand with the item VM; untested).
--   close: CloseContainerCommand, falling back to ESCAPE while the panel is
--          still visible (verified needed by act on an emptied corpse).
--   For items by uuid use the server action act.pickup: bg3le exposes no
--   names or entity ids on the container's item view models.
------------------------------------------------------------------------

local function containerSlotItem(dc, want)
    local i = 0
    for _, cont in UI.items(UI.path(dc, "CurrentPlayer.ContainerInventoryList"), 4) do
        for _, slot in UI.items(UI.path(cont, "LootInventory.Slots"), 120) do
            i = i + 1
            if i == want then return UI.props(slot).Object end
        end
    end
    return nil
end

RPC.register("ui.loot", function(args)
    local w = UI.widget("ContainerWidget") or error("no container open")
    local dc = try(function() return w.DataContext end)
    if args.all then
        local holder = UI.prop(dc, "TakeAllAndCloseCommand")
        local cmd = holder and UI.props(holder).ActionCommand
        if cmd then
            check(UI.execute(cmd, nil, args.dry))
        else
            local btn = UI.findButton(w, "TakeAll")
            check(btn and UI.press(btn, args.dry), "no Take All command or button")
        end
    elseif args.slots then
        local taken = {}
        for _, si in ipairs(args.slots) do
            local item = containerSlotItem(dc, tonumber(si)) or error("no item in slot " .. tostring(si))
            local ok, err = UI.command(dc, "TakeItemCommand", item, args.dry)
            if not ok then ok, err = UI.command(dc, "PickUpCommand", item, args.dry) end
            check(ok, "slot " .. si .. ": " .. tostring(err))
            taken[#taken + 1] = si
        end
        if args.dry then return { can = true } end
        if not args.close then return { taken = taken } end
        -- Closing: the command may be refused (e.g. an emptied corpse);
        -- the job falls back to ESCAPE below.
        UI.command(dc, "CloseContainerCommand", nil, false)
    elseif args.close then
        if args.dry then return { can = true } end
        UI.command(dc, "CloseContainerCommand", nil, false)
    else
        error("pass all=true, slots=[...] or close=true")
    end
    if args.dry then return { can = true } end
    -- Wait for the panel to close. If it is still visible after 600 ms
    -- (CloseContainerCommand refused or ignored; seen live on an emptied
    -- corpse), press ESCAPE, at most twice and only while the panel is up.
    local started = RPC.now_ms()
    return waitJob("ui.loot", 5000, function(job)
        local w2 = UI.widget("ContainerWidget")
        if not w2 or not UI.isVisible(w2) then
            return "done", { closed = true, via = job.escapes and "escape" or "command" }
        end
        local t = RPC.now_ms()
        job.escapes = job.escapes or 0
        if t - started > 600 and job.escapes < 2 and t - (job.last_esc or 0) > 800 then
            job.escapes = job.escapes + 1
            job.last_esc = t
            pcall(Ext.Input.InjectKeyPress, "ESCAPE")
        end
    end)
end)
