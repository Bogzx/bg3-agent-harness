-- Progression, part 3 of 3 (Progression.lua loads the parts in order; split for the 1000-line cap):
-- level up.
local P = BG3AI.Progression
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local I = P.inner
local set, list, str, label, same, selectTab = I.set, I.list, I.str, I.label, I.same, I.selectTab
local abilityName, skillsView, visibleWidget = I.abilityName, I.skillsView, I.visibleWidget
local tabStage, passivesView, selectorState, norm = I.tabStage, I.passivesView, I.selectorState, I.norm
local view, try, isObj, abilityOf, pick = I.view, I.try, I.isObj, I.abilityOf, I.pick
local spellsView, missingPick, skillsStage = I.spellsView, I.missingPick, I.skillsStage
local passivesReport, spellsReport, spellsStage = I.passivesReport, I.spellsReport, I.spellsStage
local verifyStage, runJob, passivesStage = I.verifyStage, I.runJob, I.passivesStage
local skillsReport, selectStage = I.skillsReport, I.selectStage

------------------------------------------------------------------------
-- Level up
------------------------------------------------------------------------

-- The character VMs the HUD shows (portraits), with what identifies them on the client.
local function partyVMs()
    local out = {}
    local hb = UI.dc("HotBar")
    local cp = hb and UI.prop(hb, "CurrentPlayer")
    for _, g in ipairs(list(cp and UI.prop(cp, "PartyGroups"))) do
        for _, c in ipairs(list(UI.prop(g, "Characters"))) do out[#out + 1] = c end
    end
    return out, cp
end

local function vmFingerprint(vm)
    return tonumber(UI.path(vm, "Stats.Health.Value")), tonumber(UI.path(vm, "Stats.Health.Max")),
        tonumber(UI.path(vm, "Stats.Level.Value"))
end

-- uuid -> the character VM. The selected character is exact (ClientControl marks it on the client, verified
-- live to match CurrentPlayer.SelectedCharacter). Otherwise match HP, max HP and level, and only accept a
-- unique match.
function P.characterVM(uuid)
    local vms, cp = partyVMs()
    local ent = try(Ext.Entity.Get, uuid)
    if not ent then return nil, "no such entity on the client" end
    if ent.ClientControl ~= nil and cp then
        local sel = UI.prop(cp, "SelectedCharacter")
        if sel then return sel end
    end
    local h = ent.Health
    local hp, maxhp = h and h.Hp, h and h.MaxHp
    local lvl = ent.EocLevel and ent.EocLevel.Level
    local hits = {}
    for _, vm in ipairs(vms) do
        local vhp, vmax, vlvl = vmFingerprint(vm)
        if vhp == hp and vmax == maxhp and vlvl == lvl then hits[#hits + 1] = vm end
    end
    if #hits == 1 then return hits[1] end
    if #hits == 0 then return nil, "character is not in the party portraits" end
    return nil, "cannot tell this character's portrait apart; select it first (act.select) and retry"
end

local function levelUpAvailable(vm)
    return UI.path(vm, "Stats.Level.LevelUpAvailable") == true
end

-- Open the level-up page for a party member: the hotbar/portrait level-up button (DCHotBar.StartLevelUp).
-- A screen fade (a conversation just ended: swap_party's camp dialogue) is up. A level-up page opened during it
-- takes its fade-in template and stays frozen at LevelUpStep=Started: no animations, no skip input, nothing
-- hit-testable (live 2026-10-10, Wyll L4 after swap_party: fade=true at open, 226-326 probes never hovered a
-- button; S018/S021 likewise). So the page is opened only once the fade is over.
P.INTRO_WAIT_MS = 5000     -- a Started page of this character this long before it counts as frozen
P.CLOSE_GIVEUP_MS = 6000   -- a close (button + ESCAPE) ignored this long: use this character's page as it is

function P.screenFade()
    for _, name in ipairs({ "CharacterLevelUp", "HotBar", "PlayerPortraits" }) do
        local v = UI.path(UI.dc(name), "CurrentPlayer.IsScreenFadeActive")
        if v ~= nil then return v == true end
    end
    return nil
end

function P.openLevelUp(uuid)
    local vm, err = P.characterVM(uuid)
    if not vm then return false, err end
    if not levelUpAvailable(vm) then return false, "no level up available for this character" end
    local hb = UI.dc("HotBar")
    if not hb then return false, "hotbar not available" end
    return UI.command(hb, "StartLevelUp", vm)
end

-- Whose page is open: "same" / "other" / nil (unknown). The page binds LevelUpCharacter (CharacterLevelUp.xaml
-- TooltipExtender.Owner); compared by proxy identity, else by the portrait fingerprint (HP, max HP, level).
function P.pageOwner(dc, uuid)
    local pv = dc and UI.prop(dc, "LevelUpCharacter")
    if not isObj(pv) then return nil end
    local want = P.characterVM(uuid)
    if not want then return nil end
    if same(pv, want) then return "same" end
    local a1, a2, a3 = vmFingerprint(pv)
    local b1, b2, b3 = vmFingerprint(want)
    if a1 == nil or a2 == nil or a3 == nil or b1 == nil or b2 == nil or b3 == nil then return nil end
    return (a1 == b1 and a2 == b2 and a3 == b3) and "same" or "other"
end

-- Close the level-up page without finishing it (its character keeps the level up): the page's close button
-- (CloseLevelUpButton: CustomEvent "CloseWidget"), else ESCAPE (S029 16:26: ESCAPE closed a frozen page live).
function P.closeLevelUp(useKey)
    local w = visibleWidget("CharacterLevelUp")
    if not w then return true end
    if useKey then return pcall(Ext.Input.InjectKeyPress, "ESCAPE") end
    local b = UI.byName(w, "CloseLevelUpButton", { hidden = true, max = 80000 })
    if b and UI.press(b) then return true end
    return UI.command(P.luDC(), "CustomEvent", "CloseWidget")
end

RPC.register("ui.level_up_open", function(args)
    if not args.uuid then error("uuid required") end
    P.levelUpUuid = args.uuid
    if P.luDC() then return { open = true } end
    if P.screenFade() then return { opening = false, fade = true, how = "a screen fade is up: call again in a moment" } end
    local ok, err = P.openLevelUp(args.uuid)
    if not ok then error(err) end
    return { opening = true }
end)

-- The feat panel (CCLib_k.xaml "featsPanel", the featTab) is bound to the page's SelectedFeatDetails: the chosen
-- feat's ability points (FeatDetails.AbilitySelection: VMAbility with Value, Improvement, IsPrimary; the -/+
-- buttons are DeselectAbility / SelectAbility(item) on the page DC, CommandParameter {Binding} = the item;
-- FeatDetails.MaxSelectedAbilityCount / SelectedAbilityCount), FeatSkills, FeatPassives and
-- FeatDetails.SpellSelectors. S018: the old read of dc.FeatDetails found nothing, so no point was ever spent.
local function featDetails(dc)
    local fd = UI.path(dc, "SelectedFeatDetails.FeatDetails")
    if fd ~= nil and UI.count(UI.prop(fd, "AbilitySelection")) > 0 then return fd end
    local root = UI.prop(dc, "FeatDetails")
    if root ~= nil and UI.count(UI.prop(root, "AbilitySelection")) > 0 then return root end
    return fd
end
P.featDetails = featDetails

local function asiItems(dc)
    -- Only a feat level has ability points. The next level's page reuses the view model and still carries the
    -- last feat's panel (live 12:5x: Wyll L5 right after L4 "the ability +/- buttons did nothing").
    if UI.prop(dc, "CanSelectFeat") ~= true then return {} end
    local fd = featDetails(dc)
    return fd and list(UI.prop(fd, "AbilitySelection")) or {}
end

-- Points the chosen feat gives (Ability Improvement 2, half feats 1) and how many are spent.
local function asiPoints(dc, items)
    local fd = featDetails(dc)
    local spent = 0
    for _, a in ipairs(items or asiItems(dc)) do spent = spent + (tonumber(UI.prop(a, "Improvement")) or 0) end
    local max = fd and tonumber(UI.prop(fd, "MaxSelectedAbilityCount"))
    if max == nil then
        max = norm(label(UI.prop(dc, "SelectedFeat"))) == norm("Ability Improvement") and 2 or 1
    end
    return max, spent
end

local function canRaise(items)
    local out = {}
    for _, a in ipairs(items) do
        local v = tonumber(UI.prop(a, "Value"))
        if v == nil or v < 20 then out[#out + 1] = abilityOf(a) .. (v and (" " .. math.floor(v)) or "") end
    end
    return table.concat(out, ", ")
end

-- plan.abilities: {Strength = 2} | {str = 1, Cha = 1} | {"Strength", "Charisma"} (+1 each) | "Strength" or
-- {"Strength"} (all points). -> {Ability = points}
local function abilityPlan(v, max)
    if v == nil then return nil end
    if type(v) == "string" then return { [abilityName(v)] = max } end
    if type(v) ~= "table" then return nil end
    local out = {}
    if v[1] ~= nil then
        if #v == 1 then return { [abilityName(v[1])] = max } end
        for _, a in ipairs(v) do
            local n = abilityName(a)
            out[n] = (out[n] or 0) + 1
        end
    else
        for k, n in pairs(v) do out[abilityName(k)] = (out[abilityName(k)] or 0) + (tonumber(n) or 0) end
    end
    return out
end

-- The multiclass list items render only "Lv N" (the next level of a class the character has) or nothing (a new
-- class): their labels are not class names. Name them from the character's Classes component instead
-- (S032 Wyll L6: "?/?/?/?/?/?/?/?/?/?/Lv 6/?" and class=Lv 6).
local function classLabel(item)
    local l = label(item)
    local lv = l and tonumber(l:match("^Lv%.? ?(%d+)$"))
    if not lv then return l end
    local ent = P.levelUpUuid and try(Ext.Entity.Get, P.levelUpUuid)
    local hit
    for _, c in ipairs(ent and try(function() return ent.Classes.Classes end) or {}) do
        if (c.Level or 0) + 1 == lv then
            if hit ~= nil then return nil end -- two classes at that level: ambiguous
            hit = c
        end
    end
    if hit == nil then return nil end
    local cd = try(Ext.StaticData.Get, hit.ClassUUID, "ClassDescription")
    local h = cd and try(function() return cd.DisplayName.Handle.Handle end)
    return (h and try(Ext.Loca.GetTranslatedString, h)) or (cd and try(function() return cd.Name end)) or nil
end

function P.luOptions(dc)
    local function sel(name) return UI.prop(dc, name) end
    local asi = {}
    local asiList = asiItems(dc)
    for _, a in ipairs(asiList) do
        local p = UI.props(a)
        asi[#asi + 1] = { a = abilityOf(a), v = p.Value, plus = (tonumber(p.Improvement) or 0) ~= 0 and p.Improvement or nil,
                          primary = p.IsPrimary == true or nil }
    end
    local o = {
        step = str(UI.prop(dc, "LevelUpStep")),
        complete = UI.prop(dc, "IsLevelUpComplete"),
        class = sel("SelectedMultiClass") ~= nil and (classLabel(sel("SelectedMultiClass")) or label(sel("SelectedMultiClass"))) or nil,
        class_level = UI.path(dc, "SelectedMultiClass.Level"),
        can_multiclass = UI.prop(dc, "IsMulticlassEnabled"),
        subclasses = view(UI.prop(dc, "SelectableSubClasses"), sel("SelectedSubClass")),
        deities = view(UI.prop(dc, "SelectableDeities"), sel("SelectedDeity")),
        can_feat = UI.prop(dc, "CanSelectFeat"),
        skills = skillsView(dc),
        passives = passivesView(dc),
        spells = spellsView(dc),
        ability_improvement = #asi > 0 and asi or nil,
    }
    -- The class list only when it says something: unnamed items (a new class shows no text) are dropped.
    local classes, items = {}, list(UI.prop(dc, "SelectableMultiClasses"))
    for _, e in ipairs(view(UI.prop(dc, "SelectableMultiClasses"), sel("SelectedMultiClass"))) do
        e.name = items[e.i] and classLabel(items[e.i]) or nil
        if e.name then classes[#classes + 1] = e end
    end
    if #classes > 1 or (#classes == 1 and not classes[1].sel) then o.classes = classes end
    if o.can_feat then
        o.feats = view(UI.prop(dc, "SelectableFeats"), sel("SelectedFeat"))
        o.feat = label(sel("SelectedFeat"))
        if #asiList > 0 then
            local max, spent = asiPoints(dc, asiList)
            o.ability_points = { max = max, spent = spent }
        end
        o.feat_panel = UI.prop(dc, "SelectedFeatDetails") ~= nil
    end
    for _, t in ipairs({ "levelupTab", "classTab", "subClassTab", "deityTab", "featTab", "spellReplaceTab", "spellPrepTab" }) do
        local el = UI.cachedByName("CharacterLevelUp", t)
        if el ~= nil and UI.prop(el, "IsSelected") == true and UI.prop(el, "Visibility") ~= "Collapsed"
            and (t ~= "featTab" or o.can_feat == true) then o.tab = t end
    end
    return o
end

-- What the page still needs, in words (the finish refusal's reason when no stage reported a problem).
function P.luMissing(dc)
    local out = {}
    if UI.count(UI.prop(dc, "SelectableSubClasses")) > 0 and UI.prop(dc, "SelectedSubClass") == nil then
        out[#out + 1] = "a subclass (plan.subclass)"
    end
    if UI.prop(dc, "CanSelectFeat") == true then
        if UI.prop(dc, "SelectedFeat") == nil then out[#out + 1] = "a feat (plan.feat)" end
        local items = asiItems(dc)
        if #items > 0 then
            local max, spent = asiPoints(dc, items)
            if spent < max then out[#out + 1] = (max - spent) .. " ability point(s) (plan.abilities)" end
        end
    end
    for _, sv in ipairs(spellsView(dc)) do
        if sv.open > 0 then out[#out + 1] = sv.open .. " " .. sv.title .. " (plan." .. (sv.cantrips and "cantrips" or "spells") .. ")" end
    end
    for _, v in ipairs(passivesView(dc)) do
        if v.complete == false then out[#out + 1] = "'" .. (v.title or "passives") .. "' (plan.passives)" end
    end
    if #out == 0 then return "the page shows nothing open (see get_level_up_options)" end
    return "still to choose: " .. table.concat(out, ", ")
end

RPC.register("ui.level_up_options", function(args)
    if args.uuid then P.levelUpUuid = args.uuid end
    local dc = P.luDC()
    if not dc then
        if args.uuid then
            local vm, err = P.characterVM(args.uuid)
            return { open = false, available = vm and levelUpAvailable(vm) or false, error = err,
                     how = "ui.level_up_open {uuid} (or ui.level_up {uuid, plan} opens it itself)" }
        end
        error("level up is not open")
    end
    local o = P.luOptions(dc)
    o.open = true
    return o
end)

-- ui.level_up {uuid, plan = {class?, subclass?, deity?, feat?, abilities? = {Str=1, Dex=1} | {Dex=2},
--              skills?, passives?, cantrips?, spells?, allow_defaults?}, finish? = true}
-- Subclass levels (Wizard 2, Druid 2, most others 3) need plan.subclass unless the page already shows one or
-- allow_defaults is set: the job fails with result.subclasses listing the choices, and nothing is changed.
-- Feat levels (4, 8, 12; Fighter 6, Rogue 10) default to Ability Improvement +2 on the class's primary ability.
-- Offline test: tools/dev/test_level_up.sh
RPC.register("ui.level_up", function(args)
    local plan = args.plan or {}
    if args.uuid then P.levelUpUuid = args.uuid end
    local openLater = false
    local ctx = { result = { set = {}, problems = {} }, widget = "CharacterLevelUp" }
    local cur = P.luDC()
    if cur and args.uuid then
        -- A page this call didn't open: another character's (S029 16:23: after Karlach's finish the game opened
        -- Gale's page by itself; Shadowheart's level_up then read Gale's wizard spells), or one still in its
        -- "Level Up!" intro (opened by the game during the finish fade: frozen, nothing hit-testable; Gale's spell
        -- clicks failed 3x / 2.5 min). Close it and open this character's page once the fade is over.
        local owner = P.pageOwner(cur, args.uuid)
        local step = str(UI.prop(cur, "LevelUpStep"))
        -- keep_page: the bridge's rerun right after it clicked spell picks on this very page (S032 17:28: Wyll L6,
        -- 4 calls: the page froze at Started, the click placed Hunger of Hadar, the rerun closed the page as "stuck
        -- in its intro" and the reopened page had lost the pick). This character's page is reused as it is.
        if owner == "other" then
            ctx.result.closed_page = "another character's page (" .. tostring(label(UI.prop(cur, "LevelUpCharacter")) or "?") .. ")"
            P.closeLevelUp()
            ctx.closing = RPC.now_ms()
            openLater = true
        elseif step == "Started" and not args.keep_page then
            -- Started is also the normal "Level Up!" intro of a page that just opened (the player's own Level Up
            -- click, or the game's). S047/S048 (Act 3, L8): closing it at once failed 12x ("still waiting after
            -- 15000 ms — closed_page=a page stuck in its intro": the page ignores the close during its intro),
            -- while the agent's hand click + 3 s wait + level_up worked. Give it P.INTRO_WAIT_MS to move on and
            -- reuse it; only a page still in Started after that is the frozen one (S029) and gets closed.
            ctx.intro = RPC.now_ms()
        end
    end
    if not P.luDC() or ctx.closing then
        if not args.uuid then error("uuid required when the level-up page is not open") end
        if ctx.closing or P.screenFade() then
            openLater = true
        else
            local ok, err = P.openLevelUp(args.uuid)
            if not ok then error(err) end
        end
    end
    local get = P.luDC
    local asi = { steps = 0 }
    local stages = {
        { "open", function()
            local cur = get()
            if ctx.intro then
                local still = cur and str(UI.prop(cur, "LevelUpStep")) == "Started"
                if still and RPC.now_ms() - ctx.intro < P.INTRO_WAIT_MS then return "wait", 200 end
                if still then
                    ctx.result.closed_page = "a page stuck in its intro (" .. P.INTRO_WAIT_MS // 1000 .. " s)"
                    P.closeLevelUp()
                    ctx.closing = RPC.now_ms()
                    openLater = true
                elseif cur then
                    ctx.result.waited_intro_ms = RPC.now_ms() - ctx.intro
                end
                ctx.intro = nil
                if not cur and not ctx.closing then
                    openLater = true  -- it closed by itself: open it again
                end
                return "wait", 100
            end
            if cur and not openLater and str(UI.prop(cur, "LevelUpStep")) == "Finishing" then
                -- The previous level's page is still closing: wait for it, then open this level's page.
                ctx.result.waited_finish = true
                return "wait", 300
            end
            if not cur and ctx.result.waited_finish and not ctx.reopened then
                ctx.reopened, openLater = true, true
            end
            if openLater then
                if ctx.closing and cur then
                    -- The page being closed: its button first, then ESCAPE once after 1.5 s.
                    if RPC.now_ms() - ctx.closing > 1500 and not ctx.escaped then
                        ctx.escaped = true
                        P.closeLevelUp(true)
                    end
                    if RPC.now_ms() - ctx.closing > P.CLOSE_GIVEUP_MS and P.pageOwner(cur, args.uuid) ~= "other" then
                        -- It won't close (button and ESCAPE ignored): this character's page is the one to use.
                        ctx.result.close_ignored = true
                        ctx.closing, openLater = nil, false
                        return "wait", 100
                    end
                    return "wait", 200
                end
                if P.screenFade() then ctx.result.waited_fade = true return "wait", 200 end
                openLater = false
                local ok, err = P.openLevelUp(args.uuid)
                if not ok then return "fail", err end
                return "wait", 300
            end
            local dc = get()
            if dc and UI.prop(dc, "SelectedMultiClass") ~= nil then
                -- S047/S048 (Act 3, L8 feat level): the page we just opened was still in its "Level Up!" intro
                -- (Started: no tab selectable, the feat list not built) and the run failed at the feat; the rerun then
                -- closed it as stuck. Wait for the intro to end; a page frozen in it (S029) goes on after the wait.
                if str(UI.prop(dc, "LevelUpStep")) == "Started" then
                    ctx.introWait = ctx.introWait or RPC.now_ms()
                    if RPC.now_ms() - ctx.introWait < P.INTRO_WAIT_MS then return "wait", 200 end
                    ctx.result.intro_timeout = true
                elseif ctx.introWait then
                    ctx.result.waited_intro_ms = RPC.now_ms() - ctx.introWait
                end
                return "next", 1500
            end
            return "wait", 300
        end, 25000 },
        { "class_tab", tabStage(ctx, "classTab") },
        { "class", function()
            local dc = get()
            P.refreshLabels("CharacterLevelUp")
            if plan.class == nil then
                ctx.result.set.class = classLabel(UI.prop(dc, "SelectedMultiClass")) or label(UI.prop(dc, "SelectedMultiClass"))
                return "next", 0
            end
            local item = pick(UI.prop(dc, "SelectableMultiClasses"), plan.class)
            if not item and not ctx.triedUnused and UI.prop(dc, "IsMulticlassEnabled") ~= false then
                -- A class not taken yet: the "add class" button switches the list to unused classes.
                ctx.triedUnused = true
                UI.command(dc, "SelectFirstUnusedClass", nil)
                return "wait", 500
            end
            if not item then
                local _, _, err = pick(UI.prop(dc, "SelectableMultiClasses"), plan.class)
                return "fail", err
            end
            local ok, err = set(dc, "SelectedMultiClass", item)
            if not ok then return "fail", err end
            ctx.pending = { key = "class", prop = "SelectedMultiClass", item = item }
            return "next", 600
        end },
        { "class?", verifyStage(get, ctx) },
        { "subclass_tab", tabStage(ctx, "subClassTab") },
        { "subclass_needed", function()
            -- A subclass is permanent: never let the "first in the list" default pick it (Gale's level 2 would
            -- become Abjuration). With nothing chosen on the page, ask for plan.subclass.
            local dc = get()
            if plan.subclass ~= nil or plan.allow_defaults then return "next", 0 end
            if UI.count(UI.prop(dc, "SelectableSubClasses")) == 0 or UI.prop(dc, "SelectedSubClass") ~= nil then
                return "next", 0
            end
            P.refreshLabels("CharacterLevelUp")
            local names = {}
            for _, e in ipairs(view(UI.prop(dc, "SelectableSubClasses"))) do
                if not e.off then names[#names + 1] = e.name or e.id or ("#" .. e.i) end
            end
            ctx.result.needs = "subclass"
            ctx.result.subclasses = names
            return "fail", "this level picks a subclass: pass plan.subclass (one of: " .. table.concat(names, ", ") .. ")"
        end },
        { "subclass", selectStage(get, ctx, "subclass", "SelectedSubClass", "SelectableSubClasses", plan.subclass, true) },
        { "subclass?", verifyStage(get, ctx) },
        { "deity_tab", tabStage(ctx, "deityTab") },
        { "deity", selectStage(get, ctx, "deity", "SelectedDeity", "SelectableDeities", plan.deity, true) },
        { "deity?", verifyStage(get, ctx) },
        -- Only on a feat level: elsewhere the (still shown) feat tab hides the spell list the bridge clicks in.
        { "feat_tab", function()
            if UI.prop(get(), "CanSelectFeat") ~= true then return "next", 0 end
            return tabStage(ctx, "featTab")()
        end },
        { "feat", function()
            local dc = get()
            if UI.prop(dc, "CanSelectFeat") ~= true then return "next", 0 end
            P.refreshLabels("CharacterLevelUp")
            local want = plan.feat or "Ability Improvement"
            local item, _, err = pick(UI.prop(dc, "SelectableFeats"), want)
            if not item then return "fail", err end
            if UI.prop(item, "IsRequirementsMet") == false or UI.prop(item, "IsAlreadyTaken") == true then
                return "fail", "feat " .. tostring(label(item)) .. " is not available"
            end
            local ok, serr = set(dc, "SelectedFeat", item)
            if not ok then return "fail", serr end
            if plan.feat == nil then
                ctx.result.defaulted = ctx.result.defaulted or {}
                ctx.result.defaulted[#ctx.result.defaulted + 1] = "feat"
            end
            ctx.pending = { key = "feat", prop = "SelectedFeat", item = item }
            return "next", 500
        end },
        { "feat?", verifyStage(get, ctx) },
        { "ability_improvement", function()
            local dc = get()
            local items = asiItems(dc)
            if #items == 0 then return "next", 0 end
            local max = asiPoints(dc, items)
            local featName = label(UI.prop(dc, "SelectedFeat")) or "this feat"
            if asi.want == nil then
                local want = abilityPlan(plan.abilities, max)
                if want == nil then
                    -- Default: all points into the class's primary ability (the page marks it IsPrimary).
                    for _, a in ipairs(items) do
                        local v = tonumber(UI.prop(a, "Value")) or 0
                        if UI.prop(a, "IsPrimary") == true and v < 20 then want = { [abilityOf(a)] = math.min(max, 20 - v) } end
                    end
                    if want == nil then
                        ctx.result.options = ctx.result.options or {}
                        ctx.result.options.abilities = canRaise(items)
                        return "fail", featName .. " gives " .. max .. " ability point" .. (max == 1 and "" or "s")
                            .. ": pass plan.abilities, e.g. {\"" .. abilityOf(items[1]) .. "\": " .. max .. "}; can raise: "
                            .. canRaise(items)
                    end
                    ctx.result.defaulted = ctx.result.defaulted or {}
                    ctx.result.defaulted[#ctx.result.defaulted + 1] = "abilities"
                end
                local byName, total = {}, 0
                for _, a in ipairs(items) do byName[abilityOf(a)] = a end
                for k, n in pairs(want) do
                    local a = byName[k]
                    if a == nil and n > 0 then
                        return "fail", featName .. " can't raise " .. tostring(k) .. "; can raise: " .. canRaise(items)
                    end
                    local v = a and tonumber(UI.prop(a, "Value")) or 0
                    if a and n > 0 and v - (tonumber(UI.prop(a, "Improvement")) or 0) + n > 20 then
                        return "fail", k .. " would go past 20 (now " .. math.floor(v) .. "); can raise: " .. canRaise(items)
                    end
                    total = total + n
                end
                if total > max then
                    return "fail", "plan.abilities spends " .. total .. " points; " .. featName .. " gives " .. max
                end
                asi.want = want
            end
            -- A press past the page's limits is ignored; a press that changes nothing three times in a row means
            -- the buttons don't take: say so instead of finishing on an incomplete page.
            local sig = {}
            for _, a in ipairs(items) do sig[#sig + 1] = tostring(UI.prop(a, "Improvement")) end
            sig = table.concat(sig, ",")
            if asi.pressed ~= nil then
                if asi.pressed == sig then asi.stall = (asi.stall or 0) + 1 else asi.stall = 0 end
                if asi.stall >= 3 then return "fail", "the ability +/- buttons did nothing (" .. asi.cmd .. " " .. asi.what .. ")" end
            end
            asi.steps = asi.steps + 1
            if asi.steps > 24 then return "next", 0 end
            local function target(a)
                return tonumber(asi.want[abilityOf(a)]) or 0
            end
            -- Take points back first, then spend them.
            for pass = 1, 2 do
                for _, a in ipairs(items) do
                    local have, want = tonumber(UI.prop(a, "Improvement")) or 0, target(a)
                    local cmd = (pass == 1 and have > want) and "DeselectAbility" or ((pass == 2 and have < want) and "SelectAbility" or nil)
                    if cmd then
                        asi.pressed, asi.cmd, asi.what = sig, cmd, abilityOf(a)
                        local ok, err = UI.command(dc, cmd, a)
                        if not ok then return "fail", cmd .. " " .. abilityOf(a) .. ": " .. tostring(err) end
                        return "wait", 150
                    end
                end
            end
            asi.pressed = nil
            return "next", 200
        end, 8000 },
        { "abilities?", function()
            local dc = get()
            local items = asiItems(dc)
            local out = {}
            for _, a in ipairs(items) do
                local plus = tonumber(UI.prop(a, "Improvement")) or 0
                if plus ~= 0 then out[abilityOf(a)] = plus end
            end
            if next(out) then ctx.result.set.abilities = out end
            if #items > 0 then
                local max, spent = asiPoints(dc, items)
                if spent < max then
                    missingPick(ctx, (max - spent) .. " of " .. max
                        .. " ability points unspent: plan.abilities; can raise: " .. canRaise(items))
                end
            end
            return "next", 0
        end },
        { "skills", skillsStage(get, ctx, plan.skills) },
        { "skills?", skillsReport(get, ctx) },
        { "passives", passivesStage(get, ctx, plan.passives, plan.allow_defaults) },
        { "passives?", passivesReport(get, ctx) },
        { "levelup_tab", function()
            -- Back to the level-up overview (its spell sub-tabs) after the feat tab, as a player would.
            if UI.prop(get(), "CanSelectFeat") == true and selectTab(ctx.widget, "levelupTab") then return "next", 450 end
            return "next", 0
        end },
        { "spells", spellsStage("CharacterLevelUp", get, ctx, plan.spells, plan.cantrips) },
        { "spells?", spellsReport(get, ctx) },
        { "check", function()
            local dc = get()
            ctx.result.complete = UI.prop(dc, "IsLevelUpComplete")
            return "next", 0
        end },
        { "finish", function()
            if args.finish == false then return "done" end
            local dc = get()
            if ctx.result.needs_clicks then
                local names = {}
                for _, nd in ipairs(ctx.result.needs_clicks) do names[#names + 1] = nd.name end
                -- S068 #67, Gale L10: plan.cantrips = [] with 2 spells planned. The clicks ran first and the level
                -- up failed as "no spell button found ... out of time (308 probes)"; a cantrip was what was missing.
                -- A pick the plan doesn't fill keeps the page from finishing after the clicks too: name it now and
                -- leave the spells unclicked (no needs_clicks: the bridge doesn't start its search).
                local missing = {}
                if UI.prop(dc, "CanSelectFeat") == true and UI.prop(dc, "SelectedFeat") == nil then
                    missing[1] = "no feat chosen (plan.feat)"
                end
                for _, m in ipairs(ctx.missing or {}) do missing[#missing + 1] = m end
                if #missing > 0 then
                    ctx.result.needs_clicks, ctx.result.not_clicked = nil, names
                    return "fail", "level up is not complete, not finishing: " .. table.concat(missing, "; ")
                        .. " (nothing clicked: pass the whole plan in one call, with " .. table.concat(names, ", ") .. ")"
                end
                return "fail", "spell picks pending (" .. table.concat(names, ", ")
                    .. "): the level_up tool clicks these itself; nothing to click by hand"
            end
            if UI.prop(dc, "IsLevelUpComplete") == false then
                local why = #ctx.result.problems > 0 and table.concat(ctx.result.problems, "; ") or P.luMissing(dc)
                return "fail", "level up is not complete, not finishing: " .. why
            end
            -- The button passes "Overview" (a camera mode); bg3le can only pass Noesis objects, so nil.
            local ok, err = UI.command(dc, "FinishLevelUp", nil)
            if not ok then return "fail", "FinishLevelUp: " .. tostring(err) end
            ctx.result.finished = true
            return "next", 500
        end },
        { "closed", function()
            if not get() then return "done" end
            -- FinishLevelUp went through on a complete page, but a page is still up after 4 s: the next pending
            -- level (Karlach x3 in S021) or the closing animation. The level was applied either way; S003/S021/S024
            -- reported these as FAILED ("closed: still waiting after 10000 ms … finished complete").
            ctx.closeWait = ctx.closeWait or RPC.now_ms()
            local step = str(UI.prop(get(), "LevelUpStep"))
            -- Still in its Finishing template (closing animation): keep waiting up to 9 s, so a following level_up
            -- doesn't work on this page (live 12:4x: Wyll L5 right after L4 read L4's feat panel: "the ability +/-
            -- buttons did nothing").
            -- A page in its intro after the finish is a new page the game opened by itself (the next level, or the
            -- next party member with a level up: S029 Claude → Karlach → Gale), during the finish fade: frozen, so
            -- the next level_up would read or click it in vain. Close it now (that character keeps its level up).
            local owner = step == "Started" and args.uuid and P.pageOwner(get(), args.uuid)
            local late = RPC.now_ms() - ctx.closeWait > (step == "Finishing" and 9000 or 4000)
            if step == "Started" and (owner == "other" or late) and not ctx.closedNext then
                ctx.closedNext = true
                ctx.result.next_page_closed = owner == "other" and "the game opened another character's level-up page: closed it (level_up(who) opens it again)"
                    or "the game opened a new level-up page: closed it; if this character has another level, call level_up again"
                P.closeLevelUp()
                ctx.closeWait = RPC.now_ms() - 2500  -- 1.5 s more for it to close, then ESCAPE below
                return "wait", 300
            end
            if ctx.closedNext and not ctx.escapedNext and RPC.now_ms() - ctx.closeWait > 4000 then
                ctx.escapedNext = true
                P.closeLevelUp(true)
                return "wait", 300
            end
            if late and (not ctx.closedNext or RPC.now_ms() - ctx.closeWait > 5500) then
                ctx.result.page_still_open = "another level up may be pending: call level_up again"
                ctx.result.close_step = step
                return "done"
            end
            return "wait", 300
        end, 10000 },
    }
    return runJob("ui.level_up", stages, ctx, 90000)
end)

-- ui.level_up_spell_probe {name, cantrips?}: for the bridge's spell clicks (bg3ai/leveling.py click_spells).
-- Shows the selector offering `name` (its sub-tab in the left list, as a click on "Spells 0/2" does) and reports
-- where its select button sits among the selector's visible buttons, and what the cursor is over:
--   {placed} | {index, n, cols, cell = {w, h}, over (cursor on the target), hovered (index under the cursor),
--    in_list (cursor over the button list), picked, open}
-- Index and cols give the button's grid cell (WrapPanel, row-major), so one hovered button locates them all.
local SPELL_BUTTONS = { SelectAvailableSpell = true, SelectSpell = true, SelectSelectableSpell = true }

local function buttonItem(b)
    local dc = try(function() return b.DataContext end)
    if dc ~= nil then return dc end
    -- Inside the item template the button's own DataContext ({Binding}) reads as nil; its ContentPresenter's
    -- Content is the item (verified live).
    return UI.prop(UI.prop(b, "TemplatedParent"), "Content")
end

-- Select the selector's sub-tab (ListBoxItem in "subTabsSpellsList" whose DataContext is the selector).
function P.showSelector(w, sel)
    for _, lb in ipairs(UI.findAll(w, function(e) return UI.name(e) == "subTabsSpellsList" end, { max = 80000 })) do
        for _, it in ipairs(UI.findAll(lb, function(e) return (UI.typeName(e) or ""):find("ListBoxItem", 1, true) ~= nil end,
                                       { hidden = true, prune = true, max = 4000 })) do
            if same(UI.prop(it, "DataContext"), sel) then
                if UI.prop(it, "IsSelected") == true then return true, false end
                local ok = set(it, "IsSelected", true)
                return ok, true
            end
        end
    end
    return false, false
end

local function tabSelected(name)
    local el = UI.cachedByName("CharacterLevelUp", name)
    return el ~= nil and UI.prop(el, "IsSelected") == true
end

-- Feat spell selectors (Magic Initiate) are edited on the feat tab, in its SpellSelectorGrid, which the selector's
-- edit toggle (ToggleButton "editSpellSelectorBtn", DataContext = the selector) opens (CCLib_k.xaml featsPanel).
-- -> ok, switched
function P.showFeatSelector(w, sel)
    if not tabSelected("featTab") then
        local ok = selectTab("CharacterLevelUp", "featTab")
        return ok == true, ok == true
    end
    for _, b in ipairs(UI.findAll(w, function(e) return UI.name(e) == "editSpellSelectorBtn" end, { prune = true, max = 80000 })) do
        if same(UI.prop(b, "DataContext"), sel) then
            if UI.prop(b, "IsChecked") == true then return true, false end
            return set(b, "IsChecked", true), true
        end
    end
    return false, false
end

-- Class/race selectors are sub-tabs of the level-up overview. On a feat level the feat tab was shown last and its
-- panel (and the LeftPanels centring, which depends on the panel width) moves everything: back to the overview first.
local function showClassSelector(w, sel)
    if tabSelected("featTab") then
        local ok = selectTab("CharacterLevelUp", "levelupTab")
        return ok == true, ok == true
    end
    return P.showSelector(w, sel)
end

-- How close the cursor is to the target's list: 1 = the button panel itself, 2.. = its ancestors up to the
-- selector's scroll viewer (at most 8 levels). nil = not over any of them. The bridge searches with big steps
-- until it is near, then with button-sized ones.
local function nearness(cp)
    local e = cp and try(function() return cp.VisualParent end)
    for depth = 1, 8 do
        if e == nil then return nil end
        if UI.prop(e, "IsMouseOver") == true then return depth end
        local t = UI.typeName(e) or ""
        if t:find("ScrollViewer", 1, true) then return nil end
        e = try(function() return e.VisualParent end)
    end
    return nil
end

RPC.register("ui.level_up_spell_probe", function(args)
    local dc = P.luDC()
    if not dc then error("level up is not open") end
    local w = visibleWidget("CharacterLevelUp")
    local s, item, why = P.findSpell(dc, args.name, args.cantrips)
    if why == "placed" then return { placed = true } end
    if not item then error("spell " .. tostring(args.name) .. ": " .. tostring(why)) end
    local st = selectorState(s.sel)
    local out = { picked = st.picked, open = st.open, name = label(item), feat = s.source == "FeatDetails" or nil,
                  -- Until "IntroComplete" the page shows its "Level Up!" intro and the panels are off-screen, so no
                  -- button can be hovered (S018/S021: 0 hits in 226-400 probes). A click skips the intro.
                  step = str(UI.prop(dc, "LevelUpStep")),
                  -- The intro waits while a screen fade is up (CharacterLevelUp.xaml: LevelUpFadeInTemplate while
                  -- LevelUpStep=Started and CurrentPlayer.IsScreenFadeActive): no skip binding, page not hit-testable.
                  fade = UI.path(dc, "CurrentPlayer.IsScreenFadeActive") == true or nil }
    if args.intro == "hit" and out.step ~= "IntroComplete" and w then
        -- A page frozen in its intro (opened during a screen fade) never runs the triggers that make it
        -- hit-testable (levelUp.IsHitTestVisible=True on SkipAnimation / sequence end). LevelUpStep itself is
        -- read-only (live: "Property LevelUpStep of gui::DCCharacterLevelUp is read-only"); setting the flag
        -- made the spell buttons hoverable live (IsMouseOver chain down to LSButton SelectAvailableSpell).
        local lu = UI.findAll(w, function(e) return UI.name(e) == "levelUp" end, { prune = true, hidden = true, max = 80000 })[1]
        out.hit_set = lu ~= nil and set(lu, "IsHitTestVisible", true) or false
    end
    local tabOk, switched
    if s.source == "FeatDetails" then tabOk, switched = P.showFeatSelector(w, s.sel)
    else tabOk, switched = showClassSelector(w, s.sel) end
    if switched then out.switched = true return out end
    if not tabOk then out.tab = "no sub-tab for this selector" end
    local mine = {}
    for _, it in ipairs(list(UI.prop(s.sel, "Available"))) do mine[tostring(it)] = true end
    local n = 0
    for _, b in ipairs(UI.findAll(w, function(e) return SPELL_BUTTONS[UI.name(e) or ""] == true end, { prune = true, max = 80000 })) do
        local it = buttonItem(b)
        if it ~= nil and mine[tostring(it)] then
            n = n + 1
            local over = UI.prop(b, "IsMouseOver") == true
            if over then out.hovered = n end
            if same(it, item) then
                out.index = n
                out.over = over
                out.enabled = UI.prop(b, "IsEnabled") ~= false
                local cp = UI.prop(b, "TemplatedParent")
                local cw, ch = tonumber(UI.prop(cp, "ActualWidth")) or 0, tonumber(UI.prop(cp, "ActualHeight")) or 0
                out.cell = { w = cw, h = ch }
                local panel = cp and try(function() return cp.VisualParent end)
                local pw = tonumber(UI.prop(panel, "ActualWidth")) or 0
                out.cols = (cw > 0 and pw >= cw) and math.floor(pw / cw + 0.01) or 1
                out.in_list = UI.prop(panel, "IsMouseOver") == true
                out.near = nearness(cp)
            end
        end
    end
    out.n = n
    return out
end)

-- ui.level_up_buttons {name?}: diagnostics for the spell-click search (tools/dev/check_levelup.py --dump):
-- every widget with IsMouseOver (deepest last: what the cursor is really over), every spell select button with
-- its item, size and visibility, and the selector sub-tabs.
RPC.register("ui.level_up_buttons", function(args)
    local w = visibleWidget("CharacterLevelUp")
    if not w then error("level up is not open") end
    local out = { over = {}, buttons = {}, tabs = {} }
    for _, e in ipairs(UI.findAll(w, function(e) return UI.prop(e, "IsMouseOver") == true end, { max = 80000 })) do
        if #out.over < 40 then
            out.over[#out.over + 1] = (UI.typeName(e) or "?") .. ":" .. tostring(UI.name(e) or "")
        end
    end
    for _, b in ipairs(UI.findAll(w, function(e)
        local n = UI.name(e) or ""
        return SPELL_BUTTONS[n] == true or (n:find("Spell", 1, true) ~= nil and n:find("Btn", 1, true) ~= nil)
    end, { prune = true, max = 80000, hidden = args.hidden == true })) do
        if #out.buttons < 60 then
            local it = buttonItem(b)
            out.buttons[#out.buttons + 1] = { name = UI.name(b), item = it and label(it) or nil,
                w = UI.prop(b, "ActualWidth"), h = UI.prop(b, "ActualHeight"), vis = UI.isVisible(b),
                enabled = UI.prop(b, "IsEnabled"), over = UI.prop(b, "IsMouseOver"), hit = UI.prop(b, "IsHitTestVisible") }
        end
    end
    for _, lb in ipairs(UI.findAll(w, function(e) return UI.name(e) == "subTabsSpellsList" end, { max = 80000 })) do
        for _, it in ipairs(UI.findAll(lb, function(e) return (UI.typeName(e) or ""):find("ListBoxItem", 1, true) ~= nil end,
                                       { hidden = true, prune = true, max = 4000 })) do
            local sel = UI.prop(it, "DataContext")
            out.tabs[#out.tabs + 1] = { selected = UI.prop(it, "IsSelected"), vis = UI.isVisible(it),
                cantrips = UI.prop(sel, "IsCantrips"), n = UI.count(UI.prop(sel, "Available")) }
        end
    end
    out.feat_tab = tabSelected("featTab")
    out.step = str(UI.prop(P.luDC(), "LevelUpStep"))
    return out
end)

-- Level-up availability for every party member the portraits show (cheap; HUD data).
RPC.register("ui.level_up_available", function()
    local out = {}
    for _, vm in ipairs((partyVMs())) do
        local hp, maxhp, lvl = vmFingerprint(vm)
        out[#out + 1] = { level = lvl, hp = hp, max_hp = maxhp, available = levelUpAvailable(vm) }
    end
    return out
end)
