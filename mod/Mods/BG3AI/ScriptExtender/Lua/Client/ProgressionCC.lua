-- Progression, part 2 of 4 (Progression.lua loads the parts in order; split for the 1000-line cap):
-- the step runner, new game and character creation (options, apply stages, name).
local P = BG3AI.Progression
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local I = P.inner
local widgetDC, spellSelectors, set, list = I.widgetDC, I.spellSelectors, I.set, I.list
local str, label, skillGroups, same, selectTab = I.str, I.label, I.skillGroups, I.same, I.selectTab
local abilityName, skillsView, visibleWidget = I.abilityName, I.skillsView, I.visibleWidget
local tabStage, pages, validity, passivesView, norm = I.tabStage, I.pages, I.validity, I.passivesView, I.norm
local view, passiveSelectors, ident, abilityOf = I.view, I.passiveSelectors, I.ident, I.abilityOf
local pick, spellsView = I.pick, I.spellsView

------------------------------------------------------------------------
-- Step runner for multi-tick flows (one stage per call, waits between them)
------------------------------------------------------------------------

-- stages: { {name, fn(ctx) -> "next"[, wait_ms] | "wait"[, ms] | "fail", err | "done"} }
local function runJob(kind, stages, ctx, timeout_ms)
    local job = RPC.start_job(kind, { timeout_ms = timeout_ms or 60000 })
    ctx.log = ctx.log or {}
    job.ctx = ctx
    job.stage = 1
    job.waitUntil = 0
    job.stageStarted = RPC.now_ms()
    job.partial = ctx.result
    -- Time per stage (incl. its waits), for the bridge's log only (it pops _stage_ms before rendering): S028 Wyll
    -- L4+L5 at camp took 60 s and nothing said which stages.
    local spent = {}
    local function stamp(name, t)
        spent[#spent + 1] = name .. "=" .. math.floor(t - job.stageStarted)
    end
    local function finish(j, status, err)
        local st = stages[j.stage]
        if st then stamp(st[1], RPC.now_ms()) end
        ctx.result._stage_ms = table.concat(spent, " ")
        RPC.finish_job(j, status, ctx.result, err)
    end
    job.on_tick = function(j)
        local t = RPC.now_ms()
        if t < j.waitUntil then return end
        local st = stages[j.stage]
        if st == nil then
            ctx.result._stage_ms = table.concat(spent, " ")
            RPC.finish_job(j, "done", ctx.result)
            return
        end
        local ok, r, a = pcall(st[2], ctx)
        if not ok then
            ctx.result.stage = st[1]
            finish(j, "failed", st[1] .. ": " .. tostring(r))
            return
        end
        if r == "next" or r == "done" then
            stamp(st[1], t)
            if r == "done" then j.stage = #stages + 1 else j.stage = j.stage + 1 end
            j.waitUntil = t + (a or 250)
            j.stageStarted = t
        elseif r == "wait" then
            local limit = st[3] or 15000
            if t - j.stageStarted > limit then
                ctx.result.stage = st[1]
                ctx.result.pages = pages()
                finish(j, "failed", st[1] .. ": still waiting after " .. limit .. " ms"
                    .. (a and type(a) == "string" and (" (" .. a .. ")") or ""))
                return
            end
            j.waitUntil = t + (type(a) == "number" and a or 250)
        else
            ctx.result.stage = st[1]
            ctx.result.pages = pages()
            finish(j, "failed", st[1] .. ": " .. tostring(a))
        end
        j.partial = ctx.result
    end
    return { job = job.id }
end

------------------------------------------------------------------------
-- ui.new_game {difficulty = "Explorer"|"Balanced"|"Tactician"|"Honour"|"Custom", custom? = {option = value}}
------------------------------------------------------------------------

RPC.register("ui.new_game", function(args)
    local want = args.difficulty or "Balanced"
    local ctx = { result = {} }
    return runJob("ui.new_game", {
        { "main_menu", function()
            local dc = widgetDC("MainMenu")
            if not dc then
                if widgetDC("DMSettings") then return "next", 0 end
                if visibleWidget("SplashScreen") then
                    -- "Press any key to continue" after a fresh launch. The engine key press is tried every
                    -- 2 s; result.splash tells the caller to press one through the input shim as well.
                    ctx.result.splash = true
                    if RPC.now_ms() >= (ctx.splashKeyAt or 0) then
                        ctx.splashKeyAt = RPC.now_ms() + 2000
                        ctx.result.splash_keys = (ctx.result.splash_keys or 0) + 1
                        pcall(Ext.Input.InjectKeyPress, "SPACE")
                    end
                    return "wait", "splash screen up (press any key); SPACE injected x" .. ctx.result.splash_keys
                end
                return "wait", "main menu not visible; pages: " .. table.concat(pages(), ",")
            end
            ctx.result.splash = nil
            local ok, err = UI.command(dc, "StartGameCommand", nil)
            if not ok then return "fail", "StartGameCommand: " .. tostring(err) end
            ctx.result.new_game = true
            return "next", 500
        end, 60000 },
        { "difficulty_page", function()
            local dc = widgetDC("DMSettings")
            if not dc or UI.count(UI.prop(dc, "Presets")) == 0 then return "wait", "difficulty page not up" end
            ctx.dm = true
            return "next", 300
        end, 20000 },
        { "difficulty", function()
            local dc = widgetDC("DMSettings")
            if not dc then return "fail", "difficulty page closed" end
            local presets = UI.prop(dc, "Presets")
            P.refreshLabels("DMSettings")
            local opts = {}
            for _, p in ipairs(list(presets)) do opts[#opts + 1] = str(UI.path(p, "Title")) or label(p) end
            ctx.result.difficulties = opts
            local preset, idx, err
            if norm(want) == "custom" then
                for i, p in ipairs(list(presets)) do
                    if UI.prop(p, "IsCustom") == true then preset, idx = p, i end
                end
                if not preset then err = "no custom preset" end
            else
                -- Preset titles are TranslatedStrings: match on Title.Str.
                for i, p in ipairs(list(presets)) do
                    local t = norm(str(UI.path(p, "Title")) or label(p))
                    if t == norm(want) or (t:sub(1, #norm(want)) == norm(want) and not preset) then preset, idx = p, i end
                end
                if not preset then err = "no difficulty '" .. tostring(want) .. "'; choices: " .. table.concat(opts, ", ") end
            end
            if not preset then return "fail", err end
            local ok, serr = set(dc, "PresetSelectedIndex", idx - 1)
            if not ok then return "fail", serr end
            ctx.preset = preset
            ctx.result.difficulty = str(UI.path(preset, "Title"))
            return "next", 300
        end },
        { "custom_rules", function()
            if type(args.custom) ~= "table" or next(args.custom) == nil then return "next", 0 end
            local dc = widgetDC("DMSettings")
            local applied, missing = {}, {}
            local options = list(UI.prop(dc, "Options"))
            for _, o in ipairs(list(UI.prop(dc, "CustomSettings"))) do options[#options + 1] = o end
            for k, v in pairs(args.custom) do
                local hit
                for _, o in ipairs(options) do
                    if norm(label(o)) == norm(k) or norm(ident(o)) == norm(k) then hit = o break end
                end
                if hit and set(hit, "Value", v) then applied[k] = v else missing[#missing + 1] = k end
            end
            ctx.result.custom_applied = applied
            if #missing > 0 then ctx.result.custom_unknown = missing end
            return "next", 200
        end },
        { "start", function()
            local dc = widgetDC("DMSettings")
            if not dc then return "fail", "difficulty page closed" end
            local name = (norm(want) == "custom") and "StartNewCustomGame" or "StartNewGame"
            local ok, err = UI.command(dc, name, ctx.preset)
            if not ok and name == "StartNewCustomGame" then ok, err = UI.command(dc, name, nil) end
            if not ok then return "fail", name .. ": " .. tostring(err) end
            ctx.result.started = true
            return "next", 2000
        end },
        { "character_creation", function()
            local dc = P.ccDC()
            if dc and UI.count(UI.prop(dc, "SelectableOrigins")) > 0 then
                ctx.result.character_creation = true
                ctx.result.pages = pages()
                return "done"
            end
            return "wait", 1000
        end, 240000 },
    }, ctx, 300000)
end)

------------------------------------------------------------------------
-- ui.cc_options {}
------------------------------------------------------------------------

local function abilitiesView(dc)
    local scores = {}
    for _, a in ipairs(list(UI.path(dc, "DummyCharacter.Stats.Abilities"))) do
        local p = UI.props(a)
        scores[#scores + 1] = { a = abilityOf(a), v = p.Value, bonus = p.RaceBonus ~= 0 and p.RaceBonus or nil,
                                primary = p.IsPrimary or nil, up = p.CanIncrease, down = p.CanDecrease }
    end
    local bonus = {}
    for _, col in ipairs(list(UI.path(dc, "RaceProgressionDetails.AbilityBonusSelection"))) do
        local opts, chosen = {}, nil
        for _, it in ipairs(list(UI.prop(col, "BonusAbilities"))) do
            local n = abilityOf(it)
            opts[#opts + 1] = n
            local p = UI.props(it)
            if (tonumber(p.Improvement) or 0) ~= 0 then chosen = n end -- CharacterRespec.xaml: 0 hides the mark
        end
        bonus[#bonus + 1] = { bonus = UI.prop(col, "AbilityBonus"), picked = chosen, options = opts }
    end
    return {
        points_left = UI.prop(dc, "UnusedAbilityPoints"),
        can_recommend = UI.prop(dc, "CanUseRecommendedAbilities"),
        scores = scores,
        bonus = #bonus > 0 and bonus or nil,
    }
end

function P.ccOptions(dc, pg)
    pg = pg or P.CC_PAGES[1]
    local function sel(name) return UI.prop(dc, name) end
    return {
        page = pg.widget,
        step = str(UI.prop(dc, "CharacterCreationStep")),
        complete = pg.complete and UI.prop(dc, pg.complete) or nil,
        complete_except_name = UI.prop(dc, "IsCharacterCompleteExceptName"),
        name = str(UI.prop(dc, "CharacterName")),
        can_edit_name = UI.prop(dc, "CanEditName"),
        origins = view(UI.prop(dc, "SelectableOrigins"), sel("SelectedOrigin")),
        races = view(UI.prop(dc, "SelectableRaces"), sel("SelectedRace")),
        subraces = view(UI.prop(dc, "SelectableSubRaces"), sel("SelectedSubRace")),
        classes = view(UI.prop(dc, "SelectableClasses"), sel("SelectedClass")),
        subclasses = view(UI.prop(dc, "SelectableSubClasses"), sel("SelectedSubClass")),
        deities = view(UI.prop(dc, "SelectableDeities"), sel("SelectedDeity")),
        backgrounds = view(UI.prop(dc, "SelectableBackgrounds"), sel("SelectedBackground")),
        abilities = abilitiesView(dc),
        skills = skillsView(dc),
        passives = passivesView(dc),
        spells = spellsView(dc),
        validity = validity(dc),
    }
end

-- ui.cc_scan: show every gameplay tab once so all list labels are read (cc_options can then name them).
RPC.register("ui.cc_scan", function()
    local pg = P.ccPage()
    if not pg then error("character creation is not open") end
    local ctx = { result = {}, widget = pg.widget }
    local stages = {}
    local tabs = { "originTab", "raceTab", "subRaceTab", "classTab", "subClassTab", "deityTab", "backgroundTab", "abilityTab" }
    local first
    for _, tab in ipairs(tabs) do
        local key = tab:gsub("Tab$", ""):gsub("^subR", "subr"):gsub("^subC", "subc")
        if not pg.keeps[key] then
            local name = P.ccTab(pg, tab)
            first = first or name
            stages[#stages + 1] = { tab, tabStage(ctx, name) }
            stages[#stages + 1] = { tab .. "?", function()
                P.refreshLabels(pg.widget)
                return "next", 0
            end }
        end
    end
    stages[#stages + 1] = { "back", function()
        selectTab(pg.widget, first)
        local n = 0
        for _ in pairs(P.rendered or {}) do n = n + 1 end
        ctx.result.labels = n
        return "done"
    end }
    return runJob("ui.cc_scan", stages, ctx, 20000)
end)

RPC.register("ui.cc_options", function(args)
    local pg, dc = P.ccPage()
    if not dc then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    P.refreshLabels(pg.widget)
    local o = P.ccOptions(dc, pg)
    if args.descriptions then
        o.classes = view(UI.prop(dc, "SelectableClasses"), UI.prop(dc, "SelectedClass"), { desc = true })
        o.backgrounds = view(UI.prop(dc, "SelectableBackgrounds"), UI.prop(dc, "SelectedBackground"), { desc = true })
    end
    return o
end)

------------------------------------------------------------------------
-- Shared apply stages
------------------------------------------------------------------------

-- Two-way selection: write Selected<Name> from Selectable<Name>s. Records what ended up selected.
local function selectStage(getDC, ctx, key, prop, collection, want, required)
    return function()
        local dc = getDC()
        if not dc then return "fail", "page closed" end
        if ctx.widget then P.refreshLabels(ctx.widget) end
        local coll = UI.prop(dc, collection)
        -- ctx.fast: until the old fixed delays would have run out (tab wait + the pick wait cut to CC_SETTLE_MS),
        -- wait for what the pick needs (see P.CC_STATE_WAITS)
        local since = RPC.now_ms() - (ctx.tabAt or -1e9)
        local early = ctx.fast and since < P.CC_TAB_MS + P.CC_PICK_MS - P.CC_SETTLE_MS
        if UI.count(coll) == 0 then
            -- empty: final after one settle, unless the plan wants an item (the old pick's list may not be rebuilt
            -- yet: wait to the cap) or the page needs one (a default: at least the old pick wait since the last pick)
            if early and (want ~= nil or since < P.CC_SETTLE_MS
                          or (required and RPC.now_ms() - (ctx.lastPickAt or -1e9) < P.CC_PICK_MS)) then
                return "wait", P.CC_POLL_MS
            end
            return "next", 0
        end
        local after = ctx.fast and P.CC_SETTLE_MS or 300
        local cur = UI.prop(dc, prop)
        if early then
            -- a pick needs the whole list labelled; keeping the current choice only its label (for the report)
            local picks = want ~= nil or (required and cur == nil)
            if picks and not P.allLabelled(coll) then return "wait", P.CC_POLL_MS end
            if not picks and cur ~= nil and label(cur) == nil then return "wait", P.CC_POLL_MS end
        end
        if want == nil then
            if cur == nil and required then
                -- Nothing chosen and the page needs a choice: take the first selectable, and say so.
                for _, it in ipairs(list(coll)) do
                    if UI.prop(it, "IsSelectable") ~= false then
                        local ok, err = set(dc, prop, it)
                        if not ok then return "fail", err end
                        ctx.result.defaulted = ctx.result.defaulted or {}
                        ctx.result.defaulted[#ctx.result.defaulted + 1] = key
                        ctx.pending = { key = key, prop = prop, item = it, at = RPC.now_ms() }
                        ctx.lastPickAt = RPC.now_ms()
                        break
                    end
                end
                return "next", after
            end
            ctx.result.set[key] = label(cur)
            return "next", 0
        end
        local item, _, err = pick(coll, want)
        if not item then
            if early then return "wait", P.CC_POLL_MS end  -- e.g. the previous pick's dependent list not rebuilt yet
            return "fail", err
        end
        if not same(cur, item) then
            local ok, serr = set(dc, prop, item)
            if not ok then return "fail", serr end
        end
        ctx.pending = { key = key, prop = prop, item = item, at = RPC.now_ms() }
        ctx.lastPickAt = RPC.now_ms()
        return "next", after
    end
end

-- Confirms the previous selectStage took effect.
local function verifyStage(getDC, ctx)
    return function()
        local pend = ctx.pending
        ctx.pending = nil
        if not pend then return "next", 0 end
        if ctx.widget then P.refreshLabels(ctx.widget) end
        local cur = UI.prop(getDC(), pend.prop)
        if ctx.fast and not same(cur, pend.item) and RPC.now_ms() - (pend.at or 0) < P.CC_PICK_MS then
            ctx.pending = pend  -- ctx.fast: give a pick that hasn't read back yet the old 300 ms
            return "wait", P.CC_POLL_MS
        end
        ctx.result.set[pend.key] = label(cur)
        if not same(cur, pend.item) then
            ctx.result.problems[#ctx.result.problems + 1] =
                pend.key .. ": asked for " .. tostring(label(pend.item)) .. ", page shows " .. tostring(label(cur))
        end
        return "next", 0
    end
end

local function wantList(v)
    if v == nil then return nil end
    if type(v) == "string" then return { v } end
    return v
end

-- Skills: toggle each wanted skill on (searching class, race, expertise and feat groups in that order; `only` names
-- one group: expertise's skills are proficient ones, so the class group lists them too and would stop the search).
-- moves (a plan that is the group's whole pick list, on a page that opens with someone's picks in it): first turn off
-- the group's own picks the plan doesn't name, since a full group ignores ToggleSkill (live 2026-11-06: a hireling's
-- Rogue kept every pick, no error). Selected with Enabled false is a pick of another source (background, race).
local function skillsStage(getDC, ctx, wanted, only, moves)
    local cleared = false
    return function()
        local dc = getDC()
        local want = wantList(wanted)
        if only and not want then return "next", 0 end
        local groups = skillGroups(dc)
        if moves and not cleared then
            cleared = true
            for _, g in ipairs(groups) do
                if g.key == only then
                    local keep = {}
                    for _, name in ipairs(want) do
                        local item = pick(g.skills, name)
                        if item then keep[label(item)] = true end
                    end
                    for _, it in ipairs(list(g.skills)) do
                        if UI.prop(it, "Selected") == true and UI.prop(it, "Enabled") ~= false and not keep[label(it)] then
                            local ok, err = UI.command(dc, "ToggleSkill", it)
                            if not ok then
                                ctx.result.problems[#ctx.result.problems + 1] = "skill " .. tostring(label(it)) .. " off: "
                                    .. tostring(err)
                            end
                        end
                    end
                end
            end
            return "wait", ctx.fast and P.CC_SETTLE_MS or 300
        end
        if want then
            for _, name in ipairs(want) do
                local done = false
                for _, g in ipairs(groups) do
                    local item = (only == nil or g.key == only) and pick(g.skills, name) or nil
                    if item then
                        if UI.prop(item, "Selected") ~= true then
                            local ok, err = UI.command(dc, "ToggleSkill", item)
                            if not ok then
                                ctx.result.problems[#ctx.result.problems + 1] = "skill " .. name .. ": " .. tostring(err)
                            end
                        end
                        done = true
                        break
                    end
                end
                if not done then ctx.result.problems[#ctx.result.problems + 1] = "skill " .. name .. ": not offered" end
            end
        end
        return "next", ctx.fast and P.CC_SETTLE_MS or 300
    end
end

local function skillsReport(getDC, ctx)
    return function()
        local sv = skillsView(getDC())
        ctx.result.set.skills = {}
        -- "picked" lists every proficient skill (background ones included) and the groups' IsComplete was
        -- false on a complete paladin (live, 2026-10-09), so completeness is left to the page's own
        -- IsCharacterComplete / IsLevelUpComplete rather than reported here.
        for k, g in pairs(sv) do
            if #g.picked > 0 then ctx.result.set.skills[k] = g.picked end
        end
        return "next", 0
    end
end

-- Passives (fighting styles, invocations, ...): toggle wanted ones. A selector left incomplete is filled in list
-- order only with `defaults` (character creation, plan.allow_defaults); otherwise passivesReport names the options.
-- A picked passive VM reads Value = 1 (verified live on Wyll's invocations; no IsSelected).
local function passiveOn(it)
    local p = UI.props(it)
    return p.IsSelected == true or p.Selected == true or (tonumber(p.Value) or 0) > 0
end

-- A choice the page still has open that the plan doesn't fill: a problem, and one of ctx.missing, which the level
-- up's finish names before any spell click (S068 #67).
local function missingPick(ctx, msg)
    ctx.result.problems[#ctx.result.problems + 1] = msg
    ctx.missing = ctx.missing or {}
    ctx.missing[#ctx.missing + 1] = msg
end

local function passivesStage(getDC, ctx, wanted, defaults, moves)
    local cleared = false
    return function()
        local dc = getDC()
        local want = wantList(wanted)
        local found = {}
        if moves and want and not cleared then -- as skillsStage's moves: a full selector ignores the toggle
            cleared = true
            for _, sel in ipairs(passiveSelectors(dc)) do
                P.namePassives(sel)
                local items, keep, mine = UI.prop(sel, "Passives"), {}, false
                for _, name in ipairs(want) do
                    local item = pick(items, name)
                    if item then keep[label(item)], mine = true, true end
                end
                for _, it in ipairs(mine and list(items) or {}) do
                    if passiveOn(it) and UI.prop(it, "Enabled") ~= false and not keep[label(it)] then
                        UI.command(dc, "TogglePassive", it)
                    end
                end
            end
            return "wait", ctx.fast and P.CC_SETTLE_MS or 300
        end
        for _, sel in ipairs(passiveSelectors(dc)) do
            P.namePassives(sel)
            local items = UI.prop(sel, "Passives")
            if want then
                for _, name in ipairs(want) do
                    local item = pick(items, name)
                    if item then
                        found[name] = true
                        if not passiveOn(item) then
                            local ok, err = UI.command(dc, "TogglePassive", item)
                            if not ok then ctx.result.problems[#ctx.result.problems + 1] = "passive " .. name .. ": " .. tostring(err) end
                        end
                    end
                end
            elseif defaults and UI.prop(sel, "IsComplete") == false then
                local need = (tonumber(UI.prop(sel, "MaxSelectedPassiveCount")) or 1) - (tonumber(UI.prop(sel, "SelectedPassiveCount")) or 0)
                for i, it in ipairs(list(items)) do
                    if need <= 0 then break end
                    if not passiveOn(it) and UI.prop(it, "Enabled") ~= false and UI.prop(it, "Blocked") ~= true then
                        if UI.command(dc, "TogglePassive", it) then
                            need = need - 1
                            ctx.result.defaulted = ctx.result.defaulted or {}
                            ctx.result.defaulted[#ctx.result.defaulted + 1] = "passive " .. tostring(label(it) or ("#" .. i))
                        end
                    end
                end
            end
        end
        for _, name in ipairs(want or {}) do
            if not found[name] then ctx.result.problems[#ctx.result.problems + 1] = "passive " .. name .. ": not offered" end
        end
        return "next", ctx.fast and P.CC_SETTLE_MS or 300
    end
end

local function passivesReport(getDC, ctx)
    return function()
        local out = {}
        for _, v in ipairs(passivesView(getDC())) do
            local title = v.title or "passives"
            out[#out + 1] = { title = v.title, picked = v.picked }
            if v.complete == false then
                -- picked can hold already-known entries (live: Wyll L5 invocations "needs -1 more"): an incomplete
                -- choice always needs at least one
                local more = math.max(1, (tonumber(v.max) or 1) - #v.picked)
                missingPick(ctx, "passive choice '" .. title .. "' needs " .. more
                    .. " more in plan.passives; can pick: " .. table.concat(v.options, ", "))
                ctx.result.options = ctx.result.options or {}
                ctx.result.options[title] = v.options
            end
        end
        if #out > 0 then ctx.result.set.passives = out end
        return "next", 0
    end
end

-- Spells/cantrips. The select buttons pass a MultiBinding (selector + spell) that Lua can neither read nor
-- build (the button's CommandParameter and DataContext read as nil; executing SelectSpell with nil is not safe),
-- so a pick is a real mouse click on the button: the stage only resolves each wanted spell and leaves
-- result.needs_clicks = { {name, cantrips} } for the bridge (bg3ai/leveling.py click_spells), which shows the
-- selector with ui.level_up_spell_probe, hover-checks the button and clicks it, then reruns ui.level_up.
-- Returns sel entry, item, or nil + reason ("placed" when it is already picked).
function P.findSpell(dc, name, cantrips)
    local offered = false
    for _, s in ipairs(spellSelectors(dc)) do
        local isC = UI.prop(s.sel, "IsCantrips") == true
        if cantrips == nil or isC == cantrips then
            P.nameSpells(s.sel)
            if pick(UI.prop(s.sel, "Additions"), name) then return s, nil, "placed" end
            local item = pick(UI.prop(s.sel, "Available"), name)
            if item then
                offered = true
                if UI.prop(item, "Selected") == true then return s, item, "placed" end
                if UI.prop(item, "NotAvailable") ~= true then return s, item end
            end
        end
    end
    return nil, nil, offered and "already known" or "not offered"
end

local function spellsStage(widgetName, getDC, ctx, wantSpells, wantCantrips)
    return function()
        local dc = getDC()
        if not dc then return "fail", "page closed" end
        if #spellSelectors(dc) == 0 then return "next", 0 end
        local needs = {}
        local function queue(names, cantrips)
            for _, n in ipairs(wantList(names) or {}) do
                local s, item, why = P.findSpell(dc, n, cantrips)
                if item and why == nil then
                    needs[#needs + 1] = { name = label(item) or n, cantrips = cantrips }
                elseif why ~= "placed" then
                    ctx.result.problems[#ctx.result.problems + 1] = "spell " .. n .. ": " .. why
                end
            end
        end
        queue(wantCantrips, true)
        queue(wantSpells, false)
        if #needs > 0 then ctx.result.needs_clicks = needs end
        return "next", 0
    end
end

local function spellsReport(getDC, ctx)
    return function()
        local out = {}
        for _, s in ipairs(spellsView(getDC())) do
            out[#out + 1] = { title = s.title, picked = s.picked }
            if s.complete == false and s.open > 0 then
                local waiting = 0
                for _, nd in ipairs(ctx.result.needs_clicks or {}) do
                    if nd.cantrips == nil or nd.cantrips == s.cantrips then waiting = waiting + 1 end
                end
                if waiting < s.open then
                    missingPick(ctx, "spell choice '" .. s.title .. "' needs "
                        .. (s.open - waiting) .. " more " .. (s.cantrips and "cantrip" or "spell")
                        .. (s.open - waiting == 1 and "" or "s") .. " in plan." .. (s.cantrips and "cantrips" or "spells")
                        .. "; can pick: " .. table.concat(s.can_pick, ", "))
                    ctx.result.options = ctx.result.options or {}
                    ctx.result.options[s.title] = s.can_pick
                end
            end
        end
        ctx.result.set.spells = out
        return "next", 0
    end
end

------------------------------------------------------------------------
-- Character name: ui.cc_name {name?, page?}, ui.cc_finish {name?}
------------------------------------------------------------------------
-- The naming page (flowPage item 3, "Enter Character Name") shows LSTextBox "characterName"
-- (Text="{Binding CharacterName}", TextImmediateSourceUpdate=True, MaxLength=20) inside Control "namingPage"
-- (CharacterCreation.xaml / CCLib_k.xaml). The appearance panel has a second, collapsed "characterName",
-- hence the lookup under namingPage. Its Proceed button only sets flowPage.SelectedIndex = 3.
-- Verified live (Patch 8):
--   * the box is created when the page is first shown: right after page = true it can still be missing.
--   * CharacterName (ls.LocaString) reads back as its handle ("ResStr_3455818087") before and after
--     typing, so the character's name cannot be read here; the box's Text reads as nil. The box text is
--     read through its selection instead: select all, read SelectedText, restore the selection.
--   * IsMouseOver / IsKeyboardFocused read fine; a click focuses the box and selects its text; LCTRL+A
--     does nothing; typed text (SDL_TEXTINPUT) lands in the box.
-- So the name is set by typing it (bg3ai/naming.py: hover-probe, click, END + BACKSPACE, text) and checked
-- by the box text. Writing the box's Text from Lua is not used: it could not be verified to reach the
-- character. ui.cc_finish refuses to run FinishCreating unless the box shows the name.
local NAMING_PAGE = 3

local function namingParts()
    local np = UI.cachedByName("CharacterCreation", "namingPage")
    if not np then return nil end
    return np, UI.byName(np, "characterName", { hidden = true }), UI.byName(np, "textInputBox", { hidden = true })
end

-- The box's text: Text is unreadable, SelectedText is not. Selecting and restoring in one eval leaves
-- nothing on screen.
function P.boxText(box)
    if not box then return nil end
    local t = UI.prop(box, "Text")
    if type(t) == "string" then return t end
    local start, len = UI.prop(box, "SelectionStart"), UI.prop(box, "SelectionLength")
    if not (set(box, "SelectionStart", 0) and set(box, "SelectionLength", 1000)) then return nil end
    local s = UI.prop(box, "SelectedText")
    set(box, "SelectionStart", tonumber(start) or 0)
    set(box, "SelectionLength", tonumber(len) or 0)
    if type(s) ~= "string" then return nil end
    return s
end

-- CharacterName as text, or nil when it only reads as a localization handle.
function P.readableName(s)
    if type(s) ~= "string" or s == "" then return nil end
    if s:match("^ResStr_%d+$") or s:match("^h%x+g%x+g%x+g%x+g%x+$") then return nil end
    return s
end

-- What the naming page shows. With `want`: ok = the box shows it and the character's name is it (or is
-- unreadable, the usual case); confirmed = the character's name was read back as it.
function P.nameView(want)
    local dc = P.ccDC()
    local _, box, frame = namingParts()
    local flow = UI.cachedByName("CharacterCreation", "flowPage")
    local val = dc and validity(dc)
    local raw = dc and UI.prop(dc, "CharacterName") or nil
    local v = {
        name = P.readableName(raw),
        name_handle = (P.readableName(raw) == nil and type(raw) == "string") and raw or nil,
        text = P.boxText(box),
        can_edit = dc and UI.prop(dc, "CanEditName") or nil,
        valid = val and val.Name or nil,
        page = flow and UI.prop(flow, "SelectedIndex") or nil,
        on_naming_page = flow ~= nil and UI.prop(flow, "SelectedIndex") == NAMING_PAGE,
        box = box ~= nil,
        focused = box ~= nil and (UI.prop(box, "IsKeyboardFocused") == true or UI.prop(box, "IsFocused") == true),
        hover_box = box ~= nil and UI.prop(box, "IsMouseOver") == true,
        hover_frame = frame ~= nil and UI.prop(frame, "IsMouseOver") == true,
        complete = dc and UI.prop(dc, "IsCharacterComplete") or nil,
    }
    if want ~= nil then
        want = tostring(want)
        v.want = want
        v.confirmed = v.name == want
        v.ok = v.text == want and (v.name == nil or v.name == want)
    end
    return v
end

RPC.register("ui.cc_name", function(args)
    local pg, dc = P.ccPage()
    if not dc then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    if not pg.naming then error(pg.widget .. " has no naming page", 0) end
    if args.set then error("set is not supported: type the name (bg3ai.naming.ensure_name)") end
    local out = {}
    if args.page then
        local flow = UI.cachedByName("CharacterCreation", "flowPage")
        if not flow then error("no flowPage on the character creation page") end
        if UI.prop(flow, "SelectedIndex") ~= NAMING_PAGE then
            if UI.prop(dc, "IsCharacterCompleteExceptName") == false then
                error("the character is not complete; the naming page is not reachable yet")
            end
            local ok, err = set(flow, "SelectedIndex", NAMING_PAGE)
            if not ok then error(err) end
            out.moved = true
        end
    end
    local v = P.nameView(args.name)
    for k, x in pairs(out) do v[k] = x end
    return v
end)

RPC.register("ui.cc_finish", function(args)
    local pg, dc = P.ccPage()
    if not dc then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    local finish, complete = P.ccName(pg, "finish"), P.ccName(pg, "complete")
    if args.name ~= nil and not pg.naming then error(pg.widget .. " has no naming page", 0) end
    if args.name ~= nil then
        local v = P.nameView(args.name)
        if not v.ok then
            error("name check failed: wanted '" .. tostring(args.name) .. "', the box shows '" .. tostring(v.text)
                .. "' and the character is '" .. tostring(v.name or v.name_handle) .. "'; not finishing")
        end
    end
    if UI.prop(dc, complete) ~= true then
        error("character is not complete; not finishing (see ui.cc_options validity)")
    end
    local ok, err = UI.command(dc, finish, nil)
    if not ok then error(finish .. ": " .. tostring(err)) end
    return { finished = true, name = args.name, page = pg.widget }
end)

------------------------------------------------------------------------
-- ui.cc_apply {plan = {origin?, race?, subrace?, class?, subclass?, deity?, background?,
--               abilities? = "recommended" | {Str=,Dex=,Con=,Int=,Wis=,Cha=}, bonus? = {"+2 ability","+1 ability"},
--               skills?, expertise?, cantrips?, spells?, passives?, name?}, finish? = true}
------------------------------------------------------------------------

-- cc_apply pacing. Naut series (2026-10-13): every call took 6.1 s, all of it fixed waits (450 ms after each of the 8
-- tabs, 300 ms after each pick/command). With P.CC_STATE_WAITS a stage waits for the state the next step needs
-- instead: after a tab, until every item of the list it picks from has a readable label (what the 450 ms was for:
-- labels come from rendered list items, and a half-rendered list can match "Elf" to "Half-Elf") or the wanted
-- item is found; after a pick, CC_SETTLE_MS, then its read-back. Each old delay stays as the cap. Off live:
-- Mods.BG3AI.BG3AI.Progression.CC_STATE_WAITS = false, or per call args.paced (the bridge's BG3AI_CC_PACED=1).
P.CC_STATE_WAITS = true
P.CC_TAB_MS = 450
P.CC_PICK_MS = 300
P.CC_POINT_MS = 120  -- after each point-buy press (abilitiesStage)
P.CC_SETTLE_MS = 100
P.CC_POLL_MS = 40

-- Every item of a collection reads a label (property, rendered list item or static name), none is "#i"/an id only.
function P.allLabelled(coll)
    for _, it in ipairs(list(coll)) do
        if label(it) == nil then return false end
    end
    return true
end

RPC.register("ui.cc_apply", function(args)
    local plan = args.plan or {}
    local pg = P.ccPage()
    if not pg then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    -- Every page name the plan needs, at the call: a TODO name (P.CC_PAGES) fails before the page changes.
    for key in pairs(pg.keeps) do
        if plan[key] ~= nil then error(("%s keeps the %s: the plan can't set it"):format(pg.widget, key), 0) end
    end
    if plan.name ~= nil and not pg.naming then error(pg.widget .. " has no naming page", 0) end
    local tab = {}
    for _, t in ipairs({ "originTab", "raceTab", "subRaceTab", "classTab", "subClassTab", "deityTab", "backgroundTab",
        "abilityTab" }) do
        local key = t:gsub("Tab$", ""):gsub("^subR", "subr"):gsub("^subC", "subc")
        if not pg.keeps[key] then tab[t] = P.ccTab(pg, t) end
    end
    local complete = P.ccName(pg, "complete")
    local finish = args.finish ~= false and P.ccName(pg, "finish") or nil
    local ctx = { result = { set = {}, problems = {} }, widget = pg.widget, page = pg }
    ctx.result.page = pg.widget
    ctx.fast = P.CC_STATE_WAITS ~= false and not args.paced
    ctx.result.pacing = ctx.fast and "state" or "fixed"
    local get = P.ccDC
    local custom = plan.origin == nil and (plan.race or plan.class)
    local kept = {}
    local function unless(key, stage) -- a stage the page has (not one of its keeps)
        if pg.keeps[key] then return kept end
        return stage
    end
    local stages = {
        unless("origin", { "origin_tab", tabStage(ctx, tab.originTab) }),
        unless("origin", { "origin", function()
            local dc = get()
            if plan.origin == nil and not custom then return "next", 0 end
            if plan.origin ~= nil then
                return selectStage(get, ctx, "origin", "SelectedOrigin", "SelectableOrigins", plan.origin)()
            end
            -- A custom character: the custom origin ("Tav" / "The Dark Urge" is its own origin).
            if UI.prop(UI.prop(dc, "SelectedOrigin"), "IsCustom") == true then return "next", 0 end
            for _, o in ipairs(list(UI.prop(dc, "SelectableOrigins"))) do
                if UI.prop(o, "IsCustom") == true then
                    local ok, err = set(dc, "SelectedOrigin", o)
                    if not ok then return "fail", err end
                    ctx.pending = { key = "origin", prop = "SelectedOrigin", item = o, at = RPC.now_ms() }
                    return "next", ctx.fast and P.CC_SETTLE_MS or 400
                end
            end
            return "fail", "no custom origin offered"
        end }),
        unless("origin", { "origin?", verifyStage(get, ctx) }),
        unless("race", { "race_tab", tabStage(ctx, tab.raceTab) }),
        unless("race", { "race", selectStage(get, ctx, "race", "SelectedRace", "SelectableRaces", plan.race) }),
        unless("race", { "race?", verifyStage(get, ctx) }),
        unless("subrace", { "subrace_tab", tabStage(ctx, tab.subRaceTab) }),
        unless("subrace", { "subrace", selectStage(get, ctx, "subrace", "SelectedSubRace", "SelectableSubRaces",
            plan.subrace, true) }),
        unless("subrace", { "subrace?", verifyStage(get, ctx) }),
        { "before", function() -- what the page opened with: a respec's hireling brings its own picks
            ctx.result.before = { abilities = abilitiesView(get()), skills = skillsView(get()) }
            return "next", 0
        end },
        { "class_tab", tabStage(ctx, tab.classTab) },
        { "class", selectStage(get, ctx, "class", "SelectedClass", "SelectableClasses", plan.class) },
        { "class?", verifyStage(get, ctx) },
        { "subclass_tab", tabStage(ctx, tab.subClassTab) },
        { "subclass", selectStage(get, ctx, "subclass", "SelectedSubClass", "SelectableSubClasses", plan.subclass, true) },
        { "subclass?", verifyStage(get, ctx) },
        { "deity_tab", tabStage(ctx, tab.deityTab) },
        { "deity", selectStage(get, ctx, "deity", "SelectedDeity", "SelectableDeities", plan.deity, true) },
        { "deity?", verifyStage(get, ctx) },
        unless("background", { "background_tab", tabStage(ctx, tab.backgroundTab) }),
        unless("background", { "background", selectStage(get, ctx, "background", "SelectedBackground",
            "SelectableBackgrounds", plan.background) }),
        unless("background", { "background?", verifyStage(get, ctx) }),
        { "abilities", I.abilitiesStage(ctx, plan), 30000 },
        { "ability_bonus", I.abilityBonusStage(ctx, plan) },
        { "abilities?", function()
            local got = abilitiesView(get())
            ctx.result.set.abilities = got
            local left = tonumber(got.points_left) or 0
            if type(plan.abilities) == "table" and left ~= 0 then
                ctx.result.problems[#ctx.result.problems + 1] = ("abilities: %s points left"):format(left)
            end
            local cols = { table.unpack(got.bonus or {}) }
            table.sort(cols, function(a, b) return (tonumber(a.bonus) or 0) > (tonumber(b.bonus) or 0) end)
            for i, name in ipairs(type(plan.bonus) == "table" and plan.bonus or {}) do
                local has = cols[i] and cols[i].picked
                if has and has ~= abilityName(name) then
                    ctx.result.problems[#ctx.result.problems + 1] = ("bonus %s: the page has %s"):format(name, has)
                end
            end
            return "next", 0
        end },
        { "skills", skillsStage(get, ctx, plan.skills) },
        { "class_skills", skillsStage(get, ctx, plan.class_skills, "class", true) },
        { "race_skills", skillsStage(get, ctx, plan.race_skills, "race", true) },
        { "expertise", skillsStage(get, ctx, plan.expertise, "expertise", true) },
        { "skills?", skillsReport(get, ctx) },
        { "passives", passivesStage(get, ctx, plan.passives, true, true) },
        { "spells", spellsStage(pg.widget, get, ctx, plan.spells, plan.cantrips) },
        { "spells?", spellsReport(get, ctx) },
        { "picks?", I.picksStage(get, ctx, plan) },
        { "name?", function()
            if plan.name == nil then return "next", 0 end
            if UI.prop(get(), "CanEditName") == false then
                ctx.result.problems[#ctx.result.problems + 1] = "this origin's name cannot be changed"
                return "next", 0
            end
            -- Typed by the bridge (see "Character name"); the box may already show it (a re-run).
            local v = P.nameView(plan.name)
            if not v.ok then
                ctx.result.name_pending = tostring(plan.name)
                ctx.result.problems[#ctx.result.problems + 1] = "name not set (box shows '" .. tostring(v.text)
                    .. "', character is '" .. tostring(v.name or v.name_handle) .. "'); type it on the naming page "
                    .. "(bg3ai.naming.ensure_name), then ui.cc_finish {name}"
            end
            return "next", 0
        end },
        { "check", function()
            local dc = get()
            ctx.result.set.name = P.readableName(str(UI.prop(dc, "CharacterName")))
            ctx.result.complete = UI.prop(dc, complete)
            ctx.result.validity = validity(dc)
            return "next", 0
        end },
        { "finish", function()
            if args.finish == false then return "done" end
            if ctx.result.name_pending then
                -- Never "Venture forth" under the wrong name: saves are matched to the playthrough by it.
                ctx.result.needs_name = ctx.result.name_pending
                ctx.result.finished = false
                return "fail", "name '" .. ctx.result.name_pending .. "' is not set; not finishing. Type it on the "
                    .. "naming page (bg3ai.naming.ensure_name), then ui.cc_finish {name}"
            end
            local dc = get()
            if UI.prop(dc, complete) ~= true then
                return "fail", "character is not complete; not finishing (see problems/validity)"
            end
            local ok, err = UI.command(dc, finish, nil)
            if not ok then return "fail", finish .. ": " .. tostring(err) end
            ctx.result.finished = true
            return "next", 1500
        end },
        { "after_finish", function()
            -- Possible follow-ups: the "show tutorials?" question, then the loading screen / intro.
            ctx.result.pages = pages()
            return "done"
        end },
    }
    local open = {}
    for _, st in ipairs(stages) do
        if st ~= kept then open[#open + 1] = st end
    end
    return runJob("ui.cc_apply", open, ctx, 120000)
end)

P.inner.runJob, P.inner.selectStage, P.inner.verifyStage = runJob, selectStage, verifyStage
P.inner.skillsStage, P.inner.skillsReport, P.inner.missingPick = skillsStage, skillsReport, missingPick
P.inner.passivesStage, P.inner.passivesReport = passivesStage, passivesReport
P.inner.wantList, P.inner.passiveOn = wantList, passiveOn
P.inner.spellsStage, P.inner.spellsReport = spellsStage, spellsReport
