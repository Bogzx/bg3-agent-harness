-- Services, part 2 of 4 (Services.lua loads the parts in order; split for the 1000-line cap):
-- rest.
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local SV = BG3AI.Services
local I = SV.inner
local hotbarDC, visibleWidget, waitJob, isNoesis = I.hotbarDC, I.visibleWidget, I.waitJob, I.isNoesis
local check, handleConfirm, selected = I.check, I.handleConfirm, I.selected

------------------------------------------------------------------------
-- Rest
------------------------------------------------------------------------

-- Short rests left (party-wide, shown on the hotbar's rest button): the bridge's ALERTS line says
-- "short rests left N: short_rest" when someone is hurt.
RPC.register("ui.rest_info", function()
    local dc = hotbarDC()
    return {
        short_rests_left = tonumber(selected(dc, "Stats.ShortRestPointsResource.Value")),
        short_rests_max = tonumber(selected(dc, "Stats.ShortRestPointsResource.MaxValue")),
    }
end)

RPC.register("ui.short_rest", function(args)
    local dc = hotbarDC()
    local before = tonumber(selected(dc, "Stats.ShortRestPointsResource.Value"))
    local max = tonumber(selected(dc, "Stats.ShortRestPointsResource.MaxValue"))
    if selected(dc, "PlayerCharacterProperties.IsShortRestAvailable") == false then
        -- One reason, not both: "0/2 left; not possible in combat" read as a combat block at 0 left.
        if before == 0 then
            error("no short rests left (0/" .. tostring(max or "?") .. "): a long rest restores them", 0)
        end
        error("short rest isn't available now (" .. tostring(before or "?") .. "/" .. tostring(max or "?")
            .. " left): the game blocks it here (in combat, or in a dialogue/cutscene)", 0)
    end
    check(UI.command(dc, "ShortRest", nil, args.dry))
    if args.dry then return { can = true, short_rests_left = before } end
    return waitJob("ui.short_rest", 15000, function(job)
        local now = tonumber(selected(hotbarDC(), "Stats.ShortRestPointsResource.Value"))
        if before and now and now < before then
            return "done", { short_rests_left = now, of = max, confirmed = job.confirms }
        end
        local c, cres, err = handleConfirm(job, args, nil, nil, true)
        if c == "failed" then return "failed", cres, err end
    end)
end)

-- What is on screen, for a failure message: the question box, else the visible
-- widgets (names only, a dozen at most).
local function screenNow()
    local A = BG3AI.UIActions
    local box = A and A.confirmBox and A.confirmBox()
    if box then
        local labels = {}
        for _, b in ipairs(box.buttons) do labels[#labels + 1] = b.label end
        return "a question box: " .. tostring(box.text or box.title or "?") .. " [" .. table.concat(labels, "/") .. "]"
    end
    local split = A and A.splitBox and A.splitBox()
    if split then
        return "a stack-split box: " .. tostring(split.title) .. " " .. tostring(split.text)
            .. " [Cancel/Confirm]: answer it with confirm(\"yes\", amount=N) or confirm(\"no\")"
    end
    -- The always-on HUD layers are left out: S028-S030 rest failures listed 12 of them and cut the list there,
    -- so a box further down could never be named.
    local names, hud = {}, 0
    for _, w in ipairs(UI.widgets()) do
        if UI.isVisible(w.el) then
            if SV.HUD_WIDGETS[w.name] then
                hud = hud + 1
            elseif #names >= 12 then
                names[#names + 1] = "..."
                break
            else
                names[#names + 1] = w.name
            end
        end
    end
    return "widgets=[" .. table.concat(names, ", ") .. "] (+" .. hud .. " HUD layers)"
end

-- Visible on every plain game screen (S0xx failure dumps): not worth naming in screenNow / restSeen.
SV.HUD_WIDGETS = {}
for _, n in ipairs({ "CrossplayOverlay", "ScreenFade", "Overlay", "AlwaysOnTopOverlay", "PassiveRoll",
                     "PlayerPortraits", "OverheadInfo", "HudIndicator", "DragAndDropPreview", "CursorText",
                     "CombatantsOverlay", "Minimap", "HotBar", "TurnModeInfo", "TargetInfo", "WorldTooltips" }) do
    SV.HUD_WIDGETS[n] = true
end

-- Non-HUD widgets that became visible while a job waited (names added to `seen`, in order, once each). The rest
-- flow's "End the day?" box showed within ~1 s in every success (S024-S026) and never in the failures since S028:
-- this tells "no box at all" from "a box came and went" (closed by something, or under another name).
function SV.noteWidgets(seen, base)
    seen.set = seen.set or {}
    for _, w in ipairs(UI.widgets()) do
        if not SV.HUD_WIDGETS[w.name] and not base[w.name] and not seen.set[w.name] and UI.isVisible(w.el) then
            seen.set[w.name] = true
            seen[#seen + 1] = w.name
        end
    end
end
function SV.visibleNames()
    local base = {}
    for _, w in ipairs(UI.widgets()) do
        if UI.isVisible(w.el) then base[w.name] = true end
    end
    return base
end

-- The rest flow across jobs. Live S018 (x4, every first long_rest of a camp visit):
-- CampTravel -> "[ForceUpdate] Yes/No" whose Yes ignores its Command -> needs_click ->
-- the bridge clicks Yes and runs ui.long_rest again 0.5 s later. That re-run saw no
-- MakeCamp yet, pressed CampTravel a second time, and the rest screen never opened
-- (the agent's own retry then worked). So: a re-run within a few seconds of our own
-- question box waits for the screen instead of pressing CampTravel again, and only
-- re-presses (once) after REST_QUIET_MS of nothing.
SV.rest = SV.rest or {}
local REST_QUIET_MS = 4000      -- nothing on screen this long after the last press/answer: press again / give up
local REST_FOLLOWUP_MS = 10000  -- a long_rest job stopped at a box this recently: this call continues that flow

local function sinceTxt(ms) return ms and string.format("%.1fs ago", (RPC.now_ms() - ms) / 1000) or "never" end

-- Night camp (live 2026-10-10 11:1x): CurrentPlayer.IsAtNightCamp=true, LongRest.IsActive=false and
-- TravelError=Unavailable: a long rest whose night never ended (a camp scene ran, or our job returned
-- 'done' 20 s after the rest screen closed). Until the day ends, camp leave fails "cannot travel right now"
-- and fast travel says "Unavailable" (29/31 fast_travel failures 10:00-10:52). The camp's long rest command
-- (CampTravel) at night opens the rest screen again (live 11:2x) or goes to sleep; resting ends the night.
local function nightCamp(dc)
    return UI.path(dc or hotbarDC(), "CurrentPlayer.IsAtNightCamp") == true
end
SV.nightCamp = nightCamp
local NIGHT_END_MS = 30000      -- after the sleep press: the night must end within this

RPC.register("ui.long_rest", function(args)
    local dc = hotbarDC()
    if selected(dc, "PlayerCharacterProperties.IsInCamp") ~= true then
        error("not at camp: travel to camp first (the camp tool), then long rest", 0)
    end
    local required = tonumber(UI.path(dc, "CurrentPlayer.RequiredPartySupplies"))
    if args.dry then
        if not visibleWidget("MakeCamp") then check(UI.command(dc, "CampTravel", nil, true)) end
        return { can = true, supplies_required = required }
    end

    -- A conversation on screen swallows CampTravel: live S026 09:07 pressed it twice over 8 s during
    -- Karlach's camp talk, then failed with "rest screen didn't open". Say so at once instead.
    if visibleWidget("Dialogue") and not visibleWidget("MakeCamp") then
        error("a conversation is open: finish it first (choose_dialogue / dialogue_advance), then long_rest", 0)
    end
    -- The HUD's Long Rest item is greyed out when CurrentPlayer.EndTheDayState is Disabled/Unavailable
    -- (HotBar.xaml LongRestItem), but CampTravel.CanExecute stays true and the press does nothing: S028 15:53
    -- three long_rest calls "pressed CampTravel 2x, the rest screen didn't open", then screenshots and HUD
    -- clicks. A player sees the greyed button: say it at once. (At night camp the sleep flow below handles it.)
    local endDay = tostring(UI.path(dc, "CurrentPlayer.EndTheDayState") or "?")
    if (endDay == "Disabled" or endDay == "Unavailable") and not nightCamp(dc) and not visibleWidget("MakeCamp") then
        error("the game doesn't allow a long rest right now (the Long Rest button is greyed out: EndTheDayState="
            .. endDay .. "); a story event may be blocking it: carry on and try again later", 0)
    end
    local R = SV.rest
    local A = BG3AI.UIActions
    -- S028 15:52: a Supply Pack split box left from a closed trade swallowed CampTravel twice.
    local cancelledSplit = A and A.cancelStaleSplit and A.cancelStaleSplit()
    local presses, step, retried = 0, "open", false
    local night = nightCamp(dc) and not visibleWidget("MakeCamp")
    local function pressCamp()
        check(UI.command(hotbarDC(), "CampTravel", nil, false))
        presses, R.pressAt = presses + 1, RPC.now_ms()
    end
    local followup = R.boxAt and RPC.now_ms() - R.boxAt < REST_FOLLOWUP_MS
    local pendingPress = false
    if visibleWidget("MakeCamp") then
        step = "screen already open"
    elseif cancelledSplit then
        pendingPress, step = true, "after closing a leftover '" .. tostring(cancelledSplit) .. "' split box"
    elseif followup and not (A and A.confirmBox and A.confirmBox()) then
        step = "after the question was answered (waiting for the screen, CampTravel not pressed again)"
    elseif args.no_press and not (A and A.confirmBox and A.confirmBox()) then
        -- the bridge saw the game's late yes (end_day_ok): the night is starting, and a press now would be the
        -- story's "leave night mode" (GLO_Camp PROC_Camp_SwitchNightMode)
        step = "after the game's late yes (waiting for the screen, CampTravel not pressed again)"
    else
        pressCamp()
        step = "after CampTravel"
    end

    local seenBase, seen, seenAt = SV.visibleNames(), {}, 0
    local function openFail(what)
        local res = { step = "open", camp_travel_presses = presses }
        res.end_the_day = tostring(UI.path(hotbarDC(), "CurrentPlayer.EndTheDayState") or "?")
        res.seen_widgets = seen
        return "failed", res, "the rest screen didn't open (" .. what .. "; step: " .. step
            .. "; EndTheDayState " .. res.end_the_day
            .. "; CampTravel pressed " .. presses .. "x in this call, last " .. sinceTxt(R.pressAt)
            .. "; last question box " .. sinceTxt(R.boxAt)
            .. "; came and went: " .. (#seen > 0 and table.concat(seen, ", ") or "nothing")
            .. "). On screen: " .. screenNow()
    end

    local phase, sawActive, used, phaseStart = "open", false, nil, RPC.now_ms()
    if night then phase, step = "sleep", "at night camp: CampTravel = go to sleep" end
    local sleepAt, sleepRetried = night and RPC.now_ms() or nil, false
    -- The night ends (IsAtNightCamp false) after the sleep press; a camp scene or the rest screen may come first.
    local function sleepTick(job, result)
        local now = RPC.now_ms()
        local c, cres, err = handleConfirm(job, args, nil, nil, true)
        if c == "failed" then
            R.boxAt = now
            return "failed", cres, err
        end
        if c then sleepAt = now return nil end
        result.confirmed = job.confirms
        if not nightCamp() then
            result.night_ended = true
            return "done", result
        end
        if visibleWidget("Dialogue") then
            result.camp_scene, result.night_open = true, true
            result.note = "a camp scene started at night: finish it, then long_rest again to sleep (until the "
                .. "night ends, camp leave and fast travel are blocked)"
            return "done", result
        end
        if visibleWidget("MakeCamp") then return "rest_screen" end
        if not sleepRetried and now - sleepAt > REST_QUIET_MS then
            pressCamp()
            sleepRetried, sleepAt = true, now
            return nil
        end
        if now - sleepAt > NIGHT_END_MS then
            result.step = "sleep"
            return "failed", result, "the night at camp didn't end (still IsAtNightCamp " .. string.format("%.0f",
                (now - sleepAt) / 1000) .. " s after going to sleep; CampTravel pressed " .. presses
                .. "x). Camp leave and fast travel stay blocked until it does. On screen: " .. screenNow()
        end
        return nil
    end

    return waitJob("ui.long_rest", 85000 + NIGHT_END_MS, function(job)
        if phase == "sleep" then
            local st, res, err = sleepTick(job, { rest = night and "night_finished" or nil })
            if st ~= "rest_screen" then return st, res, err end
            -- Live 11:2x: at night camp the rest button opens the rest screen (supplies); resting from it ends the
            -- night. sleep_only (the bridge before travelling: the night blocks all travel) rests even partially.
            if args.sleep_only then args.partial = true end
            phase, phaseStart = "open", RPC.now_ms()
        end
        local w = visibleWidget("MakeCamp")
        -- "End the day?" comes before the rest screen opens; others may follow it, and
        -- a partial rest (too few supplies) may ask once more after Rest is pressed.
        if phase ~= "resting" or (w and not sawActive) then
            local c, cres, err = handleConfirm(job, args, nil, nil, true)
            if c == "failed" then
                R.boxAt = RPC.now_ms() -- the bridge clicks it (needs_click) or the agent answers, then calls again
                cres = cres or {}
                cres.confirmed, cres.step = cres.confirmed or job.confirms, phase
                return "failed", cres, err
            end
            if c then
                if phase == "open" then
                    phaseStart, step = RPC.now_ms(), "after answering " .. tostring(job.confirms and job.confirms[#job.confirms])
                end
                return nil
            end
        end
        if phase == "open" then
            if not w and pendingPress then
                -- the split box takes a moment to close; a CampTravel pressed under it is swallowed
                if RPC.now_ms() - phaseStart >= 400 then
                    pressCamp()
                    pendingPress, phaseStart = false, RPC.now_ms()
                end
                return nil
            end
            if not w then
                if RPC.now_ms() - seenAt >= 250 then
                    seenAt = RPC.now_ms()
                    pcall(SV.noteWidgets, seen, seenBase)
                end
                if visibleWidget("Dialogue") and (job.confirms or followup) then
                    local st, res, err = openFail("a dialogue started instead (a camp scene): finish it, then long_rest again")
                    res.confirmed, res.camp_scene = job.confirms, true
                    return st, res, err
                end
                if RPC.now_ms() - phaseStart > REST_QUIET_MS then
                    -- repress=false (the bridge): S030's 4 s re-press landed inside the engine's ~10 s End-the-Day
                    -- answer window (requested → gathered → requested → end_day_failed). The bridge reads the
                    -- server's rest flow and re-presses only when the first press never reached the story.
                    if not retried and args.repress ~= false then
                        -- nothing came of the last press/answer: press the camp button again, once
                        pressCamp()
                        retried, step, phaseStart = true, "after CampTravel (retry)", RPC.now_ms()
                        return nil
                    end
                    local st, res, err = openFail("nothing for " .. string.format("%.1f", REST_QUIET_MS / 1000) .. "s")
                    res.confirmed = job.confirms
                    return st, res, err
                end
                return nil
            end
            local mdc = w.DataContext
            local sv = UI.byName(w, "SuppliesView", { hidden = true })
            local sel = sv and UI.prop(sv, "SelectedItems") or nil
            if not isNoesis(sel) then sel = nil end
            local ok, err = UI.command(mdc, "AutoSelectCommand", sel, false)
            if not ok then
                return "failed", { step = "supplies" }, "the rest screen opened but Auto-Select (supplies) failed: "
                    .. tostring(err) .. ". On screen: " .. screenNow()
            end
            R.boxAt = nil
            phase, phaseStart = "selected", RPC.now_ms()
            return nil
        end
        if phase == "selected" then
            if not w then
                return "failed", { step = "supplies", confirmed = job.confirms },
                    "the rest screen closed after Auto-Select, before Rest was pressed. On screen: " .. screenNow()
            end
            if RPC.now_ms() - phaseStart < 250 then return nil end
            local mdc = w.DataContext
            used = tonumber(UI.prop(mdc, "SelectedSuppliesAmount"))
            required = tonumber(UI.path(mdc, "CurrentPlayer.RequiredPartySupplies")) or required
            -- Too few supplies = a partial rest (half HP and slots). S025 and S026 rested partially twice
            -- and went on at 34/88 HP: stop and say so unless the agent asked for it (partial=true).
            if used and required and used < required and not args.partial then
                return "failed", { step = "supplies", supplies_used = used, supplies_required = required,
                                   partial_refused = true },
                    "only " .. used .. "/" .. required .. " camp supplies: resting now is a PARTIAL rest (half HP "
                    .. "and spell slots). Get food first (get_inventory filter=food; traders sell it: trade wares "
                    .. "marked supply=N), or long_rest(partial=true) to rest anyway. The rest screen is still open "
                    .. "(press_key escape closes it)"
            end
            local ok, err = UI.command(mdc, "ProcessRestCommand", nil, false)
            if not ok then
                return "failed", { step = "rest", supplies_used = used, supplies_required = required },
                    "supplies selected (" .. tostring(used) .. "/" .. tostring(required)
                    .. ") but the Rest button didn't work: " .. tostring(err) .. ". On screen: " .. screenNow()
            end
            phase, phaseStart = "resting", RPC.now_ms()
            return nil
        end
        -- resting: the long rest runs; camp scenes start as dialogues
        local active = UI.path(hotbarDC(), "Data.LongRest.IsActive") == true
        if active then sawActive = true end
        local result = {
            rest = (used and required and used >= required) and "full" or "partial",
            supplies_used = used, supplies_required = required, confirmed = job.confirms,
            closed_split_box = cancelledSplit,
        }
        if result.rest == "partial" then
            -- Below the requirement the game camps WITHOUT supplies ("Camp without supplies? You will not use
            -- any camp supplies", live 2026-10-10: 18 carried before and after): none were used.
            result.supplies_used, result.supplies_carried = 0, used
            result.note = "not enough camp supplies: partial rest without supplies (half HP and spell slots, no "
                .. "short rests restored; the " .. tostring(used or 0) .. " supplies you carry were kept)"
        end
        if visibleWidget("Dialogue") then
            result.camp_scene = true
            -- live 11:2x: after a rest at night the sleep cutscene is a short Dialogue and the night is already over
            if night and not nightCamp() then result.night_ended = true end
            if nightCamp() then
                result.night_open = true
                result.note = (result.note and (result.note .. "; ") or "") .. "after the camp scene, long_rest "
                    .. "again to sleep (until the night ends, camp leave and fast travel are blocked)"
            end
            return "done", result
        end
        if not w and (sawActive and not active or RPC.now_ms() - phaseStart > 20000) then
            if not nightCamp() then
                if night then result.night_ended = true end
                return "done", result
            end
            -- the rest screen closed but the day didn't end: go to sleep (sleepTick finishes)
            if not sleepAt then
                pressCamp()
                sleepAt = RPC.now_ms()
                return nil
            end
            local st, res, err = sleepTick(job, result)
            if st == "rest_screen" then return nil end
            return st, res, err
        end
        if w and not sawActive and RPC.now_ms() - phaseStart > 10000 then
            result.step = "rest"
            return "failed", result, "Rest was pressed (supplies " .. tostring(used) .. "/" .. tostring(required)
                .. ") but the rest didn't start in 10s. On screen: " .. screenNow()
        end
        return nil
    end)
end)

SV.inner.screenNow, SV.inner.nightCamp = screenNow, nightCamp
