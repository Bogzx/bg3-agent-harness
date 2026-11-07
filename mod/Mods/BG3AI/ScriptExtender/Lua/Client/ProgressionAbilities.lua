-- Progression, part 3 of 4 (Progression.lua loads the parts in order; split for the 1000-line cap): ui.cc_apply's
-- ability stages and the read-back of every plan pick. Its stages call these at run time, through P.inner.
local P = BG3AI.Progression
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local I = P.inner
local list, pick, skillGroups, selectTab = I.list, I.pick, I.skillGroups, I.selectTab
local abilityName, abilityOf, passiveSelectors = I.abilityName, I.abilityOf, I.passiveSelectors
local wantList, passiveOn, label = I.wantList, I.passiveOn, I.label

-- What `name` holds from the bonus columns (the page's +2/+1). Value includes it; RaceBonus reads 0 for it on the
-- respec page (live 2026-11-06: a hireling's picks stay through ResetAbilities, so 15 Value was 13 bought, 6 left).
local function pickedBonus(dc, name)
    local n = 0
    for _, col in ipairs(list(UI.path(dc, "RaceProgressionDetails.AbilityBonusSelection"))) do
        for _, it in ipairs(list(UI.prop(col, "BonusAbilities"))) do
            if abilityOf(it) == name and (tonumber(UI.prop(it, "Improvement")) or 0) ~= 0 then
                n = n + (tonumber(UI.prop(col, "AbilityBonus")) or 0)
            end
        end
    end
    return n
end

local function abilitiesStage(ctx, plan)
    local state = { reset = false, steps = 0 }
    return function()
        local dc = P.ccDC()
        local want = plan.abilities
        if want == nil or want == "recommended" then
            if UI.prop(dc, "CanUseRecommendedAbilities") ~= false then
                local ok, err = UI.command(dc, "UseRecommendedAbilities", nil)
                if not ok then
                    ctx.result.problems[#ctx.result.problems + 1] = "recommended abilities: " .. tostring(err)
                end
            end
            return "next", ctx.fast and P.CC_SETTLE_MS or 300
        end
        if type(want) ~= "table" then return "fail", "abilities must be \"recommended\" or a table" end
        if not state.reset then
            state.reset = true
            if UI.prop(dc, "CanResetAbilities") ~= false then UI.command(dc, "ResetAbilities", nil) end
            return "wait", 300
        end
        local abilities = list(UI.path(dc, "DummyCharacter.Stats.Abilities"))
        -- live9: a point-buy plan's ~27 presses at a fixed 120 ms each were 3.4 s of cc_apply's 5.6 s. With state
        -- waits a press waits until its score reads back changed (CC_POLL_MS polls), the 120 ms as the cap.
        local pend = state.pending
        if pend then
            local v
            for _, a in ipairs(abilities) do
                if abilityOf(a) == pend.name then v = tonumber(UI.prop(a, "Value")) end
            end
            if v == pend.value and RPC.now_ms() - pend.at < P.CC_POINT_MS then return "wait", P.CC_POLL_MS end
            state.pending = nil
        end
        -- One point per tick, re-reading the page each time (point-buy costs differ per score).
        state.steps = state.steps + 1
        if state.steps > 80 then return "fail", "point buy did not converge" end
        local stuck = {}
        for _, a in ipairs(abilities) do
            local name = abilityOf(a)
            local target
            for k, v in pairs(want) do
                if abilityName(k) == name then target = tonumber(v) end
            end
            local cur = tonumber(UI.prop(a, "Value")) or 0
            local base = cur - math.max(tonumber(UI.prop(a, "RaceBonus")) or 0, pickedBonus(dc, name))
            local cmd = target and (base < target and UI.prop(a, "CanIncrease") ~= false and "IncreaseAbility"
                or base > target and UI.prop(a, "CanDecrease") ~= false and "DecreaseAbility") or nil
            local ok, err
            if cmd then ok, err = UI.command(dc, cmd, a) end
            if ok then
                if not ctx.fast then return "wait", P.CC_POINT_MS end
                state.pending = { name = name, value = cur, at = RPC.now_ms() }
                return "wait", P.CC_POLL_MS
            end
            if target and base ~= target then
                local why = err and (": " .. tostring(err)) or ""
                stuck[#stuck + 1] = ("%s bought %s, wanted %s%s"):format(name, base, target, why)
            end
        end
        for _, s in ipairs(stuck) do ctx.result.problems[#ctx.result.problems + 1] = "abilities: " .. s end
        return "next", ctx.fast and P.CC_SETTLE_MS or 200
    end
end

-- The +2/+1: SelectNextAbilityBonus(column) until the plan's ability holds the column's pick. The check boxes' commands
-- take a MultiBinding Lua can't build, and pressed with nil they do nothing (live 2026-11-06: a Rogue's +1 stayed). The
-- controller page's arrows take the column ({Binding}, CharacterRespec_c.xaml :820), on the same DataContext.
local function abilityBonusStage(ctx, plan)
    local state = { tabbed = false, idx = 1, presses = 0 }
    return function()
        local want = plan.bonus
        if type(want) ~= "table" or #want == 0 then return "next", 0 end
        if not state.tabbed then
            state.tabbed = true
            selectTab(ctx.widget, P.ccTab(ctx.page, "abilityTab"))
            return "wait", 500
        end
        local dc = P.ccDC()
        local cols = list(UI.path(dc, "RaceProgressionDetails.AbilityBonusSelection"))
        table.sort(cols, function(a, b)
            return (tonumber(UI.prop(a, "AbilityBonus")) or 0) > (tonumber(UI.prop(b, "AbilityBonus")) or 0)
        end)
        local col = cols[state.idx]
        local name = want[state.idx]
        if not col or not name then return "next", 200 end
        local function done(why)
            if why then ctx.result.problems[#ctx.result.problems + 1] = "bonus " .. tostring(name) .. ": " .. why end
            state.idx, state.presses = state.idx + 1, 0
            return "wait", 0
        end
        local target
        local items = list(UI.prop(col, "BonusAbilities"))
        for _, it in ipairs(items) do
            if abilityOf(it) == abilityName(name) then target = it end
        end
        if not target then return done("not offered") end
        if (tonumber(UI.prop(target, "Improvement")) or 0) ~= 0 then return done() end
        if state.presses >= #items then return done(("not picked after %d presses"):format(state.presses)) end
        state.presses = state.presses + 1
        local ok, err = UI.command(dc, "SelectNextAbilityBonus", col)
        if not ok then return done(tostring(err)) end
        return "wait", ctx.fast and P.CC_SETTLE_MS or 300
    end
end

-- Every pick the plan names, read back from the page at the end: a command that returns ok proves nothing (a full
-- group ignores it). Spells waiting for the bridge's clicks are in needs_clicks.
local function picksStage(getDC, ctx, plan)
    return function()
        local dc = getDC()
        local function missing(what, name, where)
            ctx.result.problems[#ctx.result.problems + 1] = ("%s %s: not on the page%s"):format(what, name, where or "")
        end
        local function on(key, name)
            for _, g in ipairs(skillGroups(dc)) do
                local item = (key == nil or g.key == key) and pick(g.skills, name) or nil
                -- Selected with Enabled false in a named group is another source's pick, not this group's
                if item and UI.prop(item, "Selected") == true and (key == nil or UI.prop(item, "Enabled") ~= false) then
                    return true
                end
            end
            return false
        end
        for _, row in ipairs({ { "class", plan.class_skills }, { "race", plan.race_skills },
            { "expertise", plan.expertise } }) do
            for _, name in ipairs(wantList(row[2]) or {}) do
                if not on(row[1], name) then missing("skill", name, " (" .. row[1] .. ")") end
            end
        end
        for _, name in ipairs(wantList(plan.skills) or {}) do
            if not on(nil, name) then missing("skill", name) end
        end
        for _, name in ipairs(wantList(plan.passives) or {}) do
            local has = false
            for _, sel in ipairs(passiveSelectors(dc)) do
                local item = pick(UI.prop(sel, "Passives"), name)
                has = has or (item ~= nil and passiveOn(item))
            end
            if not has then missing("passive", name) end
        end
        local clicks = {}
        for _, nd in ipairs(ctx.result.needs_clicks or {}) do clicks[nd.name] = true end
        for _, row in ipairs({ { plan.spells, false }, { plan.cantrips, true } }) do
            for _, name in ipairs(wantList(row[1]) or {}) do
                local _, _, why = P.findSpell(dc, name, row[2])
                if why ~= "placed" and not clicks[name] then missing("spell", name) end
            end
        end
        return "next", 0
    end
end

-- A level's skill group can open full of the game's own picks (live 2026-11-07, Rogue L6: Deception, Sleight of Hand).
-- Toggling the plan's Acrobatics then Athletics left Sleight of Hand, Athletics with no error: Deception gone,
-- Acrobatics not on; how the game did that isn't read. Only for a named pick that didn't land: off with as many of its
-- group's unnamed picks as it lacks, then on with it. Its group is the one skillsStage toggled it in: `only`, else the
-- first holding the name. Only a row reading Enabled true comes off, as the page's own read on Rogue L6 and Bard L3:
-- false is an earlier level's or another source's (read on Rogue L6, Bard L10), and one reading anything else may be
-- one too and stays; the named pick then doesn't land and the read-back refuses. A name the game shows disabled can't
-- land, so it costs nothing and the read-back names it.
local function skillRoomStage(getDC, ctx, wanted, only)
    local lacking
    return function()
        local dc = getDC()
        if lacking then
            for _, it in ipairs(lacking) do UI.command(dc, "ToggleSkill", it) end
            return "next", ctx.fast and P.CC_SETTLE_MS or 300
        end
        lacking = {}
        local groups, turned = skillGroups(dc), false
        for _, g in ipairs(groups) do g.keep, g.lacks = {}, 0 end
        for _, name in ipairs(wantList(wanted) or {}) do
            for _, g in ipairs(groups) do
                local item = (only == nil or g.key == only) and pick(g.skills, name) or nil
                if item then
                    g.keep[label(item)] = true
                    if UI.prop(item, "Selected") ~= true and UI.prop(item, "Enabled") ~= false then
                        lacking[#lacking + 1] = item
                        g.lacks = g.lacks + 1
                    end
                    break
                end
            end
        end
        for _, g in ipairs(groups) do
            local off = 0
            for _, it in ipairs(list(g.skills)) do
                if off < g.lacks and UI.prop(it, "Selected") == true and UI.prop(it, "Enabled") == true
                    and not g.keep[label(it)] then
                    UI.command(dc, "ToggleSkill", it)
                    off = off + 1
                    turned = true
                end
            end
        end
        if #lacking == 0 then return "next", 0 end
        return "wait", turned and (ctx.fast and P.CC_SETTLE_MS or 300) or 0
    end
end
P.inner.skillRoomStage = skillRoomStage

-- What a level's toggles took off, whoever took it: the game can take a row too (above). skillsOpenStage reads every
-- group's selected rows before them, skillsOffStage names in result.set.<group>_off those on the page and no longer
-- selected after them.
local function skillsOpenStage(getDC, ctx)
    return function()
        ctx.skillsOpen = {}
        for _, g in ipairs(skillGroups(getDC())) do
            local on = {}
            for _, it in ipairs(list(g.skills)) do
                if UI.prop(it, "Selected") == true then on[label(it)] = true end
            end
            ctx.skillsOpen[g.key] = on
        end
        return "next", 0
    end
end
P.inner.skillsOpenStage = skillsOpenStage

local function skillsOffStage(getDC, ctx)
    return function()
        for _, g in ipairs(skillGroups(getDC())) do
            local open, off = ctx.skillsOpen[g.key] or {}, {}
            for _, it in ipairs(list(g.skills)) do
                if open[label(it)] and UI.prop(it, "Selected") ~= true then off[#off + 1] = label(it) end
            end
            if #off > 0 then ctx.result.set[g.key .. "_off"] = off end
        end
        return "next", 0
    end
end
P.inner.skillsOffStage = skillsOffStage

-- The plan's skills and passives read back on a level's page: the ones missing are also ctx.unlanded, and the finish
-- won't go on without them, since the game can call that page complete (live 2026-11-07, Rogue L6).
local function levelPicksStage(getDC, ctx, plan)
    local read = picksStage(getDC, ctx, { skills = plan.skills, expertise = plan.expertise, passives = plan.passives })
    return function()
        local problems = ctx.result.problems
        local before = #problems
        local to, wait = read()
        for i = before + 1, #problems do
            ctx.unlanded = ctx.unlanded or {}
            ctx.unlanded[#ctx.unlanded + 1] = problems[i]
            ctx.missing = ctx.missing or {}
            ctx.missing[#ctx.missing + 1] = problems[i]
        end
        return to, wait
    end
end
P.inner.levelPicksStage = levelPicksStage

-- The row's PassiveLists, in the row's order: the page's selectors are in it. A list's length can't always name them:
-- a Ranger's two have 5 entries each, and at a subclass's own level the character has no subclass yet to look it up by
-- (Hunter's Prey: 3 entries, 5 lists that long; Wildheart: 5 entries, 4 lists).
local function passiveListsStage(getDC, ctx, lists)
    return function()
        local ids, sels = lists or {}, I.passiveSelectors(getDC())
        if #ids > 0 and #ids ~= #sels then
            ctx.result.problems[#ctx.result.problems + 1] = ("passive lists: the page has %d selectors, the row %d")
                :format(#sels, #ids)
            return "next", 0
        end
        for i, id in ipairs(ids) do P.selPassiveList[tostring(sels[i])] = id end
        return "next", 0
    end
end
P.inner.passiveListsStage = passiveListsStage

-- A class's equipment choice, a Bard's instrument: Validity.ClassSelectors.EquipmentSelectors marks the class page, and
-- ToggleEquipment takes the entry (CCLib_k.xaml). A plan names none: a reference's dress removes the item and a twin
-- has none, so an open selector takes its first entry. Respec only, and each selector here takes one (the
-- row's SelectEquipment(list, 1, ...)), so a toggle can't take off one already held.
local function equipmentStage(getDC, ctx)
    return function()
        local dc = getDC()
        for _, s in ipairs(list(UI.path(dc, "ClassProgressionDetails.NotSubEquipmentSelectors"))) do
            local had = tonumber(UI.prop(s, "SelectedEquipmentCount"))
            local max = tonumber(UI.prop(s, "MaxSelectedEquipmentCount"))
            local first = list(UI.prop(s, "Equipments"))[1]
            ctx.result.set.equipment = { had = had, max = max }
            if had ~= max then
                local ok, err = UI.command(dc, "ToggleEquipment", first)
                if not ok then
                    ctx.result.problems[#ctx.result.problems + 1] = "equipment choice: " .. tostring(err)
                end
                ctx.result.set.equipment.took_first = ok
            end
        end
        return "next", 0
    end
end
P.inner.equipmentStage = equipmentStage

P.inner.abilitiesStage, P.inner.abilityBonusStage, P.inner.picksStage = abilitiesStage, abilityBonusStage, picksStage
