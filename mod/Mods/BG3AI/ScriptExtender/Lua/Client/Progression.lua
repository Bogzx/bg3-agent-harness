-- Progression UI: new game, character creation and level up (owner: worker "se").
--
-- Client context only. Drives the game's own pages through their DataContexts, the same objects the
-- buttons and lists are bound to, so every rule the UI enforces still applies:
--
--   MainMenu.xaml          ls:DCMainMenu        StartGameCommand (New Game, no parameter)
--   NewGameSettings.xaml   ls:DCDMSettings      Presets[] (VMPreset: Title.Str, Description.Str, IsCustom, IsHonour),
--     widget "DMSettings"                       PresetSelectedIndex (two-way, 0-based), StartNewGame(preset VM),
--                                               StartNewCustomGame, Options/CustomSettings (custom rules)
--   CharacterCreation.xaml ls:DCCharacterCreation
--     two-way:  SelectedOrigin/Race/SubRace/Class/SubClass/Deity/Background over Selectable*; CharacterName
--     commands: UseRecommendedAbilities, ResetAbilities, IncreaseAbility/DecreaseAbility(ability VM from
--               DummyCharacter.Stats.Abilities), ToggleSkill(skill VM from AllSkills.*.Skills),
--               TogglePassive(passive VM from *ProgressionDetails.(Not)SubPassiveSelectors[].Passives),
--               FinishCreating (button "ventureForth", enabled by IsCharacterComplete)
--     buttons:  SelectSpell / SelectAbilityBonus take a MultiBinding (selector VM + item VM) that Lua cannot
--               build, so those are done by pressing the realized button whose DataContext is the item.
--               The button exists only on the right gameplay tab (ListBoxItem x:Name="<tag>Tab").
--   CharacterLevelUp.xaml  ls:DCCharacterLevelUp (widget "CharacterLevelUp")
--     two-way:  SelectedMultiClass over SelectableMultiClasses, SelectedSubClass, SelectedFeat over SelectableFeats
--     commands: SelectFirstUsedClass/SelectNextUsedClass/SelectPrevUsedClass/SelectFirstUnusedClass,
--               SelectAbility/DeselectAbility(item of FeatDetails.AbilitySelection), ToggleSkill, TogglePassive,
--               FinishLevelUp (IsLevelUpComplete); spells as in CC
--     opened by: DCHotBar.StartLevelUp(VMCharacter) - the hotbar's level-up button passes
--               CurrentPlayer.SelectedCharacter, a portrait's passes its own character VM (DataTemplates_k
--               "CharacterPortrait" LevelUpButton). Shown when VM Stats.Level.LevelUpAvailable is true.
--
-- STATUS (2026-10-09, live on the AI's own new game):
--   VERIFIED  ui.new_game (StartGameCommand, PresetSelectedIndex + StartNewGame(preset VM) -> CC),
--             ui.cc_scan, ui.cc_options (all names), ui.cc_apply selections: origin Custom, race, class,
--             subclass, background via two-way Selected* writes (the page and dummy character update), and
--             UseRecommendedAbilities.
--   FINDINGS  VM labels are TranslatedStrings bg3le cannot read; names come from the rendered list items, and
--             those TextBlocks keep Text = "[ForceUpdate]" with the string in Inlines (Runs). Only the shown
--             tab's items are realized, hence the tab stages. CharacterName (ls.LocaString) is not writable:
--             the first live run finished as "Tav". See "Character name" (ui.cc_name / ui.cc_finish): cc_apply
--             now refuses to finish until the naming page's box shows the name; bg3ai/naming.py types it.
--             The "Reset Tutorials" message box ignored its button's command; a real click (input shim) works.
--   UNTESTED  FinishCreating by command (the run used the Proceed / Venture Forth buttons with the shim),
--             spell and ability-bonus button presses, passives, custom point buy, every level-up op except
--             availability (FinishLevelUp is bound with "Overview"; we pass nil).
--
-- Fairness: everything returned is what these pages show the player.

BG3AI = BG3AI or {}
local P = BG3AI.Progression or {}
BG3AI.Progression = P
local RPC = BG3AI.RPC
local UI = BG3AI.UI

------------------------------------------------------------------------
-- Small helpers (local; UI.lua belongs to worker io)
------------------------------------------------------------------------

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local function isObj(v)
    return (type(v) == "userdata" or type(v) == "table") and UI.typeName(v) ~= nil
end

-- Identity of a Noesis proxy: its printed pointer ("ls.X (userdata: 0000...)") is stable across reads.
local function same(a, b)
    return a ~= nil and b ~= nil and tostring(a) == tostring(b)
end

-- Readable string from a value that may be a string, a TranslatedString-like object or a number.
local function str(v)
    if v == nil then return nil end
    local t = type(v)
    if t == "string" then return UI.clean(v) end
    if t == "number" then
        if v == math.floor(v) then return tostring(math.floor(v)) end
        return tostring(v)
    end
    if t == "boolean" then return tostring(v) end
    if isObj(v) then
        local p = UI.props(v)
        for _, k in ipairs({ "Str", "Text", "Value", "Name" }) do
            if type(p[k]) == "string" then return UI.clean(p[k]) end
        end
        local s = UI.text(v)
        if s and s ~= UI.typeName(v) then return s end
    end
    return nil
end

local LABEL_KEYS = { "Name", "DisplayName", "Title", "Label", "ShortName", "Skill", "Ability", "PassiveName" }
local ID_KEYS = { "ClassIdentifier", "IDString", "Identifier", "Id", "ID", "StatsId", "Guid", "UUID" }

-- The label a list item shows, from the first label-ish field.
local function label(vm)
    if vm == nil then return nil end
    local p = UI.props(vm)
    for _, k in ipairs(LABEL_KEYS) do
        local s = str(p[k])
        if s then return s end
    end
    -- Spell references keep the spell one level down.
    for _, k in ipairs({ "Spell", "Passive", "Feat", "Content" }) do
        if isObj(p[k]) then
            local s = label(p[k])
            if s then return s end
        end
    end
    local s = UI.text(vm)
    if s and s ~= UI.typeName(vm) then return s end
    -- Many VM labels are TranslatedStrings that bg3le cannot read as properties; the page renders them, so
    -- fall back to the text of the list item showing this VM (refreshLabels).
    local r = P.rendered and P.rendered[tostring(vm)]
    if r then return r end
    return nil
end

local function ident(vm)
    local p = UI.props(vm)
    for _, k in ipairs(ID_KEYS) do
        if type(p[k]) == "string" and p[k] ~= "" then return p[k] end
    end
    return nil
end

-- Primitive (number/bool/short string) fields of a VM: the counts and flags the page shows.
local function meta(vm, keys)
    local out = {}
    local p = UI.props(vm)
    for k, v in pairs(p) do
        local t = type(v)
        if (not keys or keys[k]) and (t == "number" or t == "boolean" or (t == "string" and #v < 80)) then
            out[k] = v
        end
    end
    return out
end

local function set(obj, name, value)
    if obj == nil then return false, "no object" end
    local ok, err = pcall(obj.SetProperty, obj, name, value)
    if not ok then return false, "set " .. name .. ": " .. tostring(err) end
    return true
end

local function list(coll)
    local out = {}
    for _, item in UI.items(coll, 300) do
        if item ~= nil then out[#out + 1] = item end
    end
    return out
end

local function norm(s)
    return (tostring(s or ""):lower():gsub("[%s'’%-_]", ""))
end

-- Pick an item from a collection by 1-based index, label or id: exact (normalized) first, then a unique
-- prefix, then a unique substring. Returns item, index, or nil + error listing the choices.
local function pick(coll, want)
    local items = list(coll)
    if type(want) == "number" then
        if items[want] then return items[want], want end
        return nil, nil, "index " .. want .. " out of range (1-" .. #items .. ")"
    end
    local w = norm(want)
    local names = {}
    local exact, prefix, sub = {}, {}, {}
    for i, it in ipairs(items) do
        local l, id = label(it), ident(it)
        names[#names + 1] = l or id or ("#" .. i)
        local nl, ni = norm(l), norm(id)
        if nl == w or ni == w then exact[#exact + 1] = i
        elseif nl:sub(1, #w) == w or ni:sub(1, #w) == w then prefix[#prefix + 1] = i
        elseif w ~= "" and (nl:find(w, 1, true) or ni:find(w, 1, true)) then sub[#sub + 1] = i end
    end
    for _, hits in ipairs({ exact, prefix, sub }) do
        if #hits == 1 then return items[hits[1]], hits[1] end
        if #hits > 1 then
            return nil, nil, "'" .. tostring(want) .. "' is ambiguous: " .. table.concat(names, ", ")
        end
    end
    return nil, nil, "no '" .. tostring(want) .. "'; choices: " .. table.concat(names, ", ")
end

local function indexOf(coll, item)
    for i, it in ipairs(list(coll)) do
        if same(it, item) then return i end
    end
    return nil
end

-- Compact list view: { {i, name, id?, sel?, off?, why?} }
local function view(coll, selected, opts)
    opts = opts or {}
    local out = {}
    for i, it in ipairs(list(coll)) do
        local p = UI.props(it)
        local e = { i = i, name = label(it) }
        local id = ident(it)
        if id and norm(id) ~= norm(e.name) then e.id = id end
        if (selected and same(it, selected)) or p.IsSelected == true or p.Selected == true then e.sel = true end
        if p.IsSelectable == false or p.Enabled == false or p.IsEnabled == false
            or p.IsRequirementsMet == false or p.IsAlreadyTaken == true or p.IsLocked == true then
            e.off = true
        end
        if p.IsCustom == true then e.custom = true end
        if opts.desc then e.desc = str(p.Description) end
        out[#out + 1] = e
    end
    return out
end

local ABILITIES = {
    str = "Strength", dex = "Dexterity", con = "Constitution",
    int = "Intelligence", wis = "Wisdom", cha = "Charisma",
}

local function abilityName(s)
    if s == nil then return nil end
    local k = norm(s):sub(1, 3)
    return ABILITIES[k] or s
end

local function abilityOf(vm)
    return abilityName(str(UI.prop(vm, "Ability")) or label(vm))
end

------------------------------------------------------------------------
-- Pages
------------------------------------------------------------------------

local function visibleWidget(name)
    for _, w in ipairs(UI.widgets()) do
        if w.name == name and UI.isVisible(w.el) then return w.el end
    end
    return nil
end

local function widgetDC(name)
    local el = visibleWidget(name)
    return el and try(function() return el.DataContext end) or nil
end

P.ccDC = function() return widgetDC("CharacterCreation") end
P.luDC = function() return widgetDC("CharacterLevelUp") end

-- VM identity -> the first text its list item renders, for every realized list item on a page. Selectable
-- lists show one entry per VM, so this recovers names that are not readable as properties.
local LIST_ITEM_TYPES = { ["ListBoxItem"] = true, ["ls.LSListBoxItem"] = true, ["ComboBoxItem"] = true }

-- Rendered text of a TextBlock. Text set through ls:TextBlockFormatter (most CC labels) leaves Text as the
-- "[ForceUpdate]" placeholder and puts the string in Inlines as Runs (verified live), so read those.
function P.textOf(tb)
    local s = UI.text(tb)
    if s and not s:match("^%[.*%]$") then return s end
    local parts = {}
    for _, inl in UI.items(UI.prop(tb, "Inlines"), 50) do
        local t = UI.prop(inl, "Text")
        if type(t) == "string" then parts[#parts + 1] = t end
    end
    return UI.clean(table.concat(parts))
end

-- First real text of a list item.
local function itemText(it)
    for _, tb in ipairs(UI.findAll(it, function(e) return UI.typeName(e) == "TextBlock" end, { hidden = true, max = 400 })) do
        local s = P.textOf(tb)
        if s then return s end
    end
    return nil
end

-- Accumulates across calls while the same page instance is open, so labels read on one tab survive
-- switching to the next (only the shown tab's list items are realized).
function P.refreshLabels(widgetName)
    local w = UI.widget(widgetName)
    local key = widgetName .. "@" .. tostring(w)
    if P.renderedKey ~= key then
        P.rendered = {}
        P.renderedKey = key
    end
    local map = P.rendered
    if w then
        for _, it in ipairs(UI.findAll(w, function(e) return LIST_ITEM_TYPES[UI.typeName(e) or ""] == true end,
                                      { prune = true, hidden = true, max = 60000 })) do
            local dc = UI.prop(it, "DataContext")
            if dc ~= nil then
                local first = itemText(it)
                if first then map[tostring(dc)] = first end
            end
        end
    end
    return map
end

-- Names of visible pages that are not the always-on HUD, for "what is in the way" reports.
local HUD = {
    HudIndicator = true, OverheadInfo = true, Minimap = true, CombatLog = true, TurnModeInfo = true, TargetInfo = true,
    CombatantsOverlay = true, WorldTooltips = true, HotBar = true, PlayerPortraits = true, AlwaysOnTopOverlay = true,
    PassiveRoll = true, CursorText = true, Overlay = true, WorldContextMenu = true, NotificationWidget = true,
    DragAndDropPreview = true, ScreenFade = true, CrossplayOverlay = true,
}

local function pages()
    local out = {}
    for _, w in ipairs(UI.widgets()) do
        if not HUD[w.name] and UI.isVisible(w.el) then out[#out + 1] = w.name end
    end
    return out
end
P.pages = pages

-- Select a gameplay tab (ListBoxItem x:Name="<tag>Tab") the way a click does.
local function selectTab(widgetName, tabName)
    local el = UI.cachedByName(widgetName, tabName)
    if not el then return false, "no tab " .. tabName end
    if UI.prop(el, "Visibility") == "Collapsed" then return false, "tab " .. tabName .. " is not shown" end
    return set(el, "IsSelected", true)
end

-- Show a gameplay tab so its list items are realized (their labels are only readable when rendered).
local function tabStage(ctx, tab)
    return function()
        if ctx.widget and selectTab(ctx.widget, tab) then return "next", 450 end
        return "next", 0
    end
end

-- Press the realized, visible button named one of `names` whose DataContext is `item`.
local function pressFor(widgetName, names, item)
    local w = visibleWidget(widgetName)
    if not w then return false, "page not open" end
    local want = {}
    for _, n in ipairs(names) do want[n] = true end
    local hits = UI.findAll(w, function(e)
        return want[UI.name(e) or ""] == true and same(UI.prop(e, "DataContext"), item)
    end, { prune = true, max = 40000 })
    if #hits == 0 then return false, "button not on screen" end
    return UI.press(hits[1])
end

------------------------------------------------------------------------
-- Selectors shared by CC and level up: skills, passives, spells
------------------------------------------------------------------------

local SKILL_GROUPS = {
    { key = "class", path = "AllSkills.ClassProficientSkills" },
    { key = "race", path = "AllSkills.RaceProficientSkills" },
    { key = "expertise", path = "AllSkills.ExpertiseSkills" },
    { key = "feat", path = "FeatSkills" },
}

local function skillGroups(dc)
    local out = {}
    for _, g in ipairs(SKILL_GROUPS) do
        local grp = UI.path(dc, g.path)
        local skills = grp and UI.prop(grp, "Skills")
        if skills and UI.count(skills) > 0 then
            local m = meta(grp, { MaxSelectedSkillCount = true, SelectedSkillCount = true, IsComplete = true })
            out[#out + 1] = { key = g.key, grp = grp, skills = skills, max = m.MaxSelectedSkillCount,
                              selected = m.SelectedSkillCount, complete = m.IsComplete }
        end
    end
    return out
end

local function skillsView(dc)
    local out = {}
    for _, g in ipairs(skillGroups(dc)) do
        local opts, chosen = {}, {}
        for _, s in ipairs(list(g.skills)) do
            local p = UI.props(s)
            local n = label(s)
            if p.Selected == true then chosen[#chosen + 1] = n end
            if p.Enabled ~= false then opts[#opts + 1] = n end
        end
        out[g.key] = { max = g.max, picked = chosen, can_pick = opts, complete = g.complete }
    end
    return out
end

local PASSIVE_PATHS = {
    "ClassProgressionDetails.NotSubPassiveSelectors", "ClassProgressionDetails.SubPassiveSelectors",
    "RaceProgressionDetails.NotSubPassiveSelectors", "RaceProgressionDetails.SubPassiveSelectors",
    "FeatDetails.PassiveSelectors",
}

local function passiveSelectors(dc)
    local out, seen = {}, {}
    for _, path in ipairs(PASSIVE_PATHS) do
        for _, sel in ipairs(list(UI.path(dc, path))) do
            if not seen[tostring(sel)] then
                seen[tostring(sel)] = true
                out[#out + 1] = sel
            end
        end
    end
    return out
end

local function passivesView(dc)
    local out = {}
    for _, sel in ipairs(passiveSelectors(dc)) do
        local m = meta(sel)
        local chosen, opts = {}, {}
        for _, it in ipairs(list(UI.prop(sel, "Passives"))) do
            local p = UI.props(it)
            if p.IsSelected == true or p.Selected == true then chosen[#chosen + 1] = label(it) end
            opts[#opts + 1] = label(it)
        end
        out[#out + 1] = { title = str(UI.prop(sel, "Title")) or label(sel), max = m.MaxSelectedPassiveCount,
                          picked = chosen, options = opts, complete = m.IsComplete }
    end
    return out
end

local SPELL_PATHS = { "ClassProgressionDetails.SpellSelectors", "RaceProgressionDetails.SpellSelectors",
                      "FeatDetails.SpellSelectors" }

local function spellSelectors(dc)
    local out, seen = {}, {}
    for _, path in ipairs(SPELL_PATHS) do
        for _, sel in ipairs(list(UI.path(dc, path))) do
            if not seen[tostring(sel)] then
                seen[tostring(sel)] = true
                out[#out + 1] = { sel = sel, source = path:match("^(%a+)") }
            end
        end
    end
    return out
end

local function spellsView(dc)
    local out = {}
    for _, s in ipairs(spellSelectors(dc)) do
        local m = meta(s.sel)
        local added = {}
        for _, it in ipairs(list(UI.prop(s.sel, "Additions"))) do added[#added + 1] = label(it) end
        local avail = {}
        for _, it in ipairs(list(UI.prop(s.sel, "Available"))) do avail[#avail + 1] = label(it) end
        out[#out + 1] = {
            title = str(UI.prop(s.sel, "Title")), from = s.source, cantrips = m.IsCantrips,
            picked = added, available = avail, complete = m.IsComplete, counts = m,
        }
    end
    return out
end

-- Validation the page shows: the Validity object's flags and messages, plus appearance errors.
local function validity(dc)
    local v = UI.prop(dc, "Validity")
    local out = v and meta(v) or {}
    local name = v and str(UI.prop(v, "Name"))
    if name then out.Name = name end
    local app = str(UI.prop(dc, "AppearanceValidationError"))
    if app then out.appearance = app end
    if next(out) == nil then return nil end
    return out
end

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
    job.on_tick = function(j)
        local t = RPC.now_ms()
        if t < j.waitUntil then return end
        local st = stages[j.stage]
        if st == nil then
            RPC.finish_job(j, "done", ctx.result)
            return
        end
        local ok, r, a = pcall(st[2], ctx)
        if not ok then
            ctx.result.stage = st[1]
            RPC.finish_job(j, "failed", ctx.result, st[1] .. ": " .. tostring(r))
            return
        end
        if r == "next" or r == "done" then
            if r == "done" then j.stage = #stages + 1 else j.stage = j.stage + 1 end
            j.waitUntil = t + (a or 250)
            j.stageStarted = t
        elseif r == "wait" then
            local limit = st[3] or 15000
            if t - j.stageStarted > limit then
                ctx.result.stage = st[1]
                ctx.result.pages = pages()
                RPC.finish_job(j, "failed", ctx.result, st[1] .. ": still waiting after " .. limit .. " ms"
                    .. (a and type(a) == "string" and (" (" .. a .. ")") or ""))
                return
            end
            j.waitUntil = t + (type(a) == "number" and a or 250)
        else
            ctx.result.stage = st[1]
            ctx.result.pages = pages()
            RPC.finish_job(j, "failed", ctx.result, st[1] .. ": " .. tostring(a))
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
            if p.IsSelected == true or p.Selected == true or p.IsChecked == true then chosen = n end
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

function P.ccOptions(dc)
    local function sel(name) return UI.prop(dc, name) end
    return {
        step = str(UI.prop(dc, "CharacterCreationStep")),
        complete = UI.prop(dc, "IsCharacterComplete"),
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
    if not P.ccDC() then error("character creation is not open") end
    local ctx = { result = {}, widget = "CharacterCreation" }
    local stages = {}
    local tabs = { "originTab", "raceTab", "subRaceTab", "classTab", "subClassTab", "deityTab", "backgroundTab", "abilityTab" }
    for _, tab in ipairs(tabs) do
        stages[#stages + 1] = { tab, tabStage(ctx, tab) }
        stages[#stages + 1] = { tab .. "?", function()
            P.refreshLabels("CharacterCreation")
            return "next", 0
        end }
    end
    stages[#stages + 1] = { "back", function()
        selectTab("CharacterCreation", "originTab")
        local n = 0
        for _ in pairs(P.rendered or {}) do n = n + 1 end
        ctx.result.labels = n
        return "done"
    end }
    return runJob("ui.cc_scan", stages, ctx, 20000)
end)

RPC.register("ui.cc_options", function(args)
    local dc = P.ccDC()
    if not dc then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    P.refreshLabels("CharacterCreation")
    local o = P.ccOptions(dc)
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
        if UI.count(coll) == 0 then return "next", 0 end
        local cur = UI.prop(dc, prop)
        if want == nil then
            if cur == nil and required then
                -- Nothing chosen and the page needs a choice: take the first selectable, and say so.
                for _, it in ipairs(list(coll)) do
                    if UI.prop(it, "IsSelectable") ~= false then
                        local ok, err = set(dc, prop, it)
                        if not ok then return "fail", err end
                        ctx.result.defaulted = ctx.result.defaulted or {}
                        ctx.result.defaulted[#ctx.result.defaulted + 1] = key
                        ctx.pending = { key = key, prop = prop, item = it }
                        break
                    end
                end
                return "next", 300
            end
            ctx.result.set[key] = label(cur)
            return "next", 0
        end
        local item, _, err = pick(coll, want)
        if not item then return "fail", err end
        if not same(cur, item) then
            local ok, serr = set(dc, prop, item)
            if not ok then return "fail", serr end
        end
        ctx.pending = { key = key, prop = prop, item = item }
        return "next", 300
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

-- Skills: toggle each wanted skill on (searching class, race, expertise and feat groups in that order).
local function skillsStage(getDC, ctx, wanted)
    return function()
        local dc = getDC()
        local want = wantList(wanted)
        local groups = skillGroups(dc)
        if want then
            for _, name in ipairs(want) do
                local done = false
                for _, g in ipairs(groups) do
                    local item = pick(g.skills, name)
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
        return "next", 300
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

-- Passives (fighting styles, ...): toggle wanted ones; when omitted, fill incomplete selectors in list order.
local function passivesStage(getDC, ctx, wanted)
    return function()
        local dc = getDC()
        local want = wantList(wanted)
        for _, sel in ipairs(passiveSelectors(dc)) do
            local items = UI.prop(sel, "Passives")
            if want then
                for _, name in ipairs(want) do
                    local item = pick(items, name)
                    if item and UI.prop(item, "IsSelected") ~= true then
                        local ok, err = UI.command(dc, "TogglePassive", item)
                        if not ok then ctx.result.problems[#ctx.result.problems + 1] = "passive " .. name .. ": " .. tostring(err) end
                    end
                end
            elseif UI.prop(sel, "IsComplete") == false then
                local need = (tonumber(UI.prop(sel, "MaxSelectedPassiveCount")) or 1) - (tonumber(UI.prop(sel, "SelectedPassiveCount")) or 0)
                for _, it in ipairs(list(items)) do
                    if need <= 0 then break end
                    if UI.prop(it, "IsSelected") ~= true then
                        if UI.command(dc, "TogglePassive", it) then
                            need = need - 1
                            ctx.result.defaulted = ctx.result.defaulted or {}
                            ctx.result.defaulted[#ctx.result.defaulted + 1] = "passive " .. tostring(label(it))
                        end
                    end
                end
            end
        end
        return "next", 300
    end
end

-- Spells/cantrips: press the realized select button of each wanted spell. The buttons only exist on the
-- tab that shows the selector, so the stage walks `tabs` until every wanted spell is placed or all were tried.
local function spellsStage(widgetName, getDC, ctx, wantSpells, wantCantrips, tabs)
    local state = { tab = 0 }
    return function()
        local dc = getDC()
        if not dc then return "fail", "page closed" end
        local selectors = spellSelectors(dc)
        if #selectors == 0 then return "next", 0 end
        local todo = {}
        local function queue(names, cantrips)
            for _, n in ipairs(wantList(names) or {}) do todo[#todo + 1] = { name = n, cantrips = cantrips } end
        end
        queue(wantCantrips, true)
        queue(wantSpells, false)
        local remaining = {}
        for _, t in ipairs(todo) do
            local placed, found = false, false
            for _, s in ipairs(selectors) do
                local isC = UI.prop(s.sel, "IsCantrips") == true
                if t.cantrips == nil or isC == t.cantrips then
                    local added = UI.prop(s.sel, "Additions")
                    if pick(added, t.name) then placed = true break end
                    local item = pick(UI.prop(s.sel, "Available"), t.name)
                    if item then
                        found = true
                        if pressFor(widgetName, { "SelectSpell", "SelectSelectableSpell", "SelectAvailableSpell" }, item) then
                            placed = true
                            break
                        end
                    end
                end
            end
            if not placed then remaining[#remaining + 1] = { name = t.name, cantrips = t.cantrips, offered = found } end
        end
        if #remaining == 0 then return "next", 300 end
        -- Try the next tab where the selector buttons might be realized.
        state.tab = state.tab + 1
        if tabs[state.tab] then
            selectTab(widgetName, tabs[state.tab])
            return "wait", 400
        end
        for _, r in ipairs(remaining) do
            ctx.result.problems[#ctx.result.problems + 1] = "spell " .. r.name .. ": "
                .. (r.offered and "offered, but its button could not be pressed" or "not offered")
        end
        return "next", 0
    end
end

local function spellsReport(getDC, ctx)
    return function()
        local out = {}
        for _, s in ipairs(spellsView(getDC())) do
            out[#out + 1] = { title = s.title, picked = s.picked }
            if s.complete == false then
                ctx.result.problems[#ctx.result.problems + 1] = "spell choice '" .. tostring(s.title) .. "' incomplete"
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
-- Verified live (machine 2, Patch 8):
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
    local dc = P.ccDC()
    if not dc then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
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
    local dc = P.ccDC()
    if not dc then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    if args.name ~= nil then
        local v = P.nameView(args.name)
        if not v.ok then
            error("name check failed: wanted '" .. tostring(args.name) .. "', the box shows '" .. tostring(v.text)
                .. "' and the character is '" .. tostring(v.name or v.name_handle) .. "'; not finishing")
        end
    end
    if UI.prop(dc, "IsCharacterComplete") ~= true then
        error("character is not complete; not finishing (see ui.cc_options validity)")
    end
    local ok, err = UI.command(dc, "FinishCreating", nil)
    if not ok then error("FinishCreating: " .. tostring(err)) end
    return { finished = true, name = args.name }
end)

------------------------------------------------------------------------
-- ui.cc_apply {plan = {origin?, race?, subrace?, class?, subclass?, deity?, background?,
--               abilities? = "recommended" | {Str=,Dex=,Con=,Int=,Wis=,Cha=}, bonus? = {"+2 ability","+1 ability"},
--               skills?, cantrips?, spells?, passives?, name?}, finish? = true}
------------------------------------------------------------------------

local function abilitiesStage(ctx, plan)
    local state = { reset = false, steps = 0 }
    return function()
        local dc = P.ccDC()
        local want = plan.abilities
        if want == nil or want == "recommended" then
            if UI.prop(dc, "CanUseRecommendedAbilities") ~= false then
                local ok, err = UI.command(dc, "UseRecommendedAbilities", nil)
                if not ok then ctx.result.problems[#ctx.result.problems + 1] = "recommended abilities: " .. tostring(err) end
            end
            return "next", 300
        end
        if type(want) ~= "table" then return "fail", "abilities must be \"recommended\" or a table" end
        if not state.reset then
            state.reset = true
            if UI.prop(dc, "CanResetAbilities") ~= false then UI.command(dc, "ResetAbilities", nil) end
            return "wait", 300
        end
        -- One point per tick, re-reading the page each time (point-buy costs differ per score).
        state.steps = state.steps + 1
        if state.steps > 80 then return "fail", "point buy did not converge" end
        for _, a in ipairs(list(UI.path(dc, "DummyCharacter.Stats.Abilities"))) do
            local name = abilityOf(a)
            local target
            for k, v in pairs(want) do
                if abilityName(k) == name then target = tonumber(v) end
            end
            local cur = tonumber(UI.prop(a, "Value")) or 0
            local base = cur - (tonumber(UI.prop(a, "RaceBonus")) or 0)
            if target and base < target and UI.prop(a, "CanIncrease") ~= false then
                local ok = UI.command(dc, "IncreaseAbility", a)
                if ok then return "wait", 120 end
            elseif target and base > target and UI.prop(a, "CanDecrease") ~= false then
                local ok = UI.command(dc, "DecreaseAbility", a)
                if ok then return "wait", 120 end
            end
        end
        return "next", 200
    end
end

-- Racial +2/+1: press the bonus buttons on the ability tab (MultiBinding parameter).
local function abilityBonusStage(ctx, plan)
    local state = { tabbed = false, idx = 1 }
    return function()
        local want = plan.bonus
        if type(want) ~= "table" or #want == 0 then return "next", 0 end
        if not state.tabbed then
            state.tabbed = true
            selectTab("CharacterCreation", "abilityTab")
            return "wait", 500
        end
        local dc = P.ccDC()
        local cols = list(UI.path(dc, "RaceProgressionDetails.AbilityBonusSelection"))
        table.sort(cols, function(a, b) return (tonumber(UI.prop(a, "AbilityBonus")) or 0) > (tonumber(UI.prop(b, "AbilityBonus")) or 0) end)
        local col = cols[state.idx]
        local name = want[state.idx]
        if not col or not name then return "next", 200 end
        state.idx = state.idx + 1
        local target
        for _, it in ipairs(list(UI.prop(col, "BonusAbilities"))) do
            if abilityOf(it) == abilityName(name) then target = it end
        end
        if not target then
            ctx.result.problems[#ctx.result.problems + 1] = "bonus " .. tostring(name) .. ": not offered"
            return "wait", 0
        end
        local ok, err = pressFor("CharacterCreation", { "button" }, target)
        if not ok then ctx.result.problems[#ctx.result.problems + 1] = "bonus " .. tostring(name) .. ": " .. tostring(err) end
        return "wait", 300
    end
end

RPC.register("ui.cc_apply", function(args)
    local plan = args.plan or {}
    if not P.ccDC() then error("character creation is not open (pages: " .. table.concat(pages(), ",") .. ")") end
    local ctx = { result = { set = {}, problems = {} }, widget = "CharacterCreation" }
    local get = P.ccDC
    local custom = plan.origin == nil and (plan.race or plan.class)
    local stages = {
        { "origin_tab", tabStage(ctx, "originTab") },
        { "origin", function()
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
                    ctx.pending = { key = "origin", prop = "SelectedOrigin", item = o }
                    return "next", 400
                end
            end
            return "fail", "no custom origin offered"
        end },
        { "origin?", verifyStage(get, ctx) },
        { "race_tab", tabStage(ctx, "raceTab") },
        { "race", selectStage(get, ctx, "race", "SelectedRace", "SelectableRaces", plan.race) },
        { "race?", verifyStage(get, ctx) },
        { "subrace_tab", tabStage(ctx, "subRaceTab") },
        { "subrace", selectStage(get, ctx, "subrace", "SelectedSubRace", "SelectableSubRaces", plan.subrace, true) },
        { "subrace?", verifyStage(get, ctx) },
        { "class_tab", tabStage(ctx, "classTab") },
        { "class", selectStage(get, ctx, "class", "SelectedClass", "SelectableClasses", plan.class) },
        { "class?", verifyStage(get, ctx) },
        { "subclass_tab", tabStage(ctx, "subClassTab") },
        { "subclass", selectStage(get, ctx, "subclass", "SelectedSubClass", "SelectableSubClasses", plan.subclass, true) },
        { "subclass?", verifyStage(get, ctx) },
        { "deity_tab", tabStage(ctx, "deityTab") },
        { "deity", selectStage(get, ctx, "deity", "SelectedDeity", "SelectableDeities", plan.deity, true) },
        { "deity?", verifyStage(get, ctx) },
        { "background_tab", tabStage(ctx, "backgroundTab") },
        { "background", selectStage(get, ctx, "background", "SelectedBackground", "SelectableBackgrounds", plan.background) },
        { "background?", verifyStage(get, ctx) },
        { "abilities", abilitiesStage(ctx, plan), 30000 },
        { "ability_bonus", abilityBonusStage(ctx, plan) },
        { "abilities?", function()
            ctx.result.set.abilities = abilitiesView(get())
            return "next", 0
        end },
        { "skills", skillsStage(get, ctx, plan.skills) },
        { "skills?", skillsReport(get, ctx) },
        { "passives", passivesStage(get, ctx, plan.passives) },
        { "spells", spellsStage("CharacterCreation", get, ctx, plan.spells, plan.cantrips,
                                { "classTab", "subClassTab", "raceTab", "subRaceTab", "spellPrepTab" }), 20000 },
        { "spells?", spellsReport(get, ctx) },
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
            ctx.result.complete = UI.prop(dc, "IsCharacterComplete")
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
            if UI.prop(dc, "IsCharacterComplete") ~= true then
                return "fail", "character is not complete; not finishing (see problems/validity)"
            end
            local ok, err = UI.command(dc, "FinishCreating", nil)
            if not ok then return "fail", "FinishCreating: " .. tostring(err) end
            ctx.result.finished = true
            return "next", 1500
        end },
        { "after_finish", function()
            -- Possible follow-ups: the "show tutorials?" question, then the loading screen / intro.
            ctx.result.pages = pages()
            return "done"
        end },
    }
    return runJob("ui.cc_apply", stages, ctx, 120000)
end)

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
function P.openLevelUp(uuid)
    local vm, err = P.characterVM(uuid)
    if not vm then return false, err end
    if not levelUpAvailable(vm) then return false, "no level up available for this character" end
    local hb = UI.dc("HotBar")
    if not hb then return false, "hotbar not available" end
    return UI.command(hb, "StartLevelUp", vm)
end

RPC.register("ui.level_up_open", function(args)
    if not args.uuid then error("uuid required") end
    if P.luDC() then return { open = true } end
    local ok, err = P.openLevelUp(args.uuid)
    if not ok then error(err) end
    return { opening = true }
end)

function P.luOptions(dc)
    local function sel(name) return UI.prop(dc, name) end
    local asi = {}
    for _, a in ipairs(list(UI.path(dc, "FeatDetails.AbilitySelection"))) do
        local p = UI.props(a)
        asi[#asi + 1] = { a = abilityOf(a), v = p.Value, plus = p.Improvement ~= 0 and p.Improvement or nil }
    end
    local o = {
        step = str(UI.prop(dc, "LevelUpStep")),
        complete = UI.prop(dc, "IsLevelUpComplete"),
        classes = view(UI.prop(dc, "SelectableMultiClasses"), sel("SelectedMultiClass")),
        class = label(sel("SelectedMultiClass")),
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
    if o.can_feat then
        o.feats = view(UI.prop(dc, "SelectableFeats"), sel("SelectedFeat"))
        o.feat = label(sel("SelectedFeat"))
    end
    return o
end

RPC.register("ui.level_up_options", function(args)
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
    if not P.luDC() then
        if not args.uuid then error("uuid required when the level-up page is not open") end
        local ok, err = P.openLevelUp(args.uuid)
        if not ok then error(err) end
    end
    local ctx = { result = { set = {}, problems = {} }, widget = "CharacterLevelUp" }
    local get = P.luDC
    local asi = { steps = 0 }
    local stages = {
        { "open", function()
            local dc = get()
            if dc and UI.prop(dc, "SelectedMultiClass") ~= nil then return "next", 1500 end
            return "wait", 300
        end, 15000 },
        { "class_tab", tabStage(ctx, "classTab") },
        { "class", function()
            local dc = get()
            P.refreshLabels("CharacterLevelUp")
            if plan.class == nil then
                ctx.result.set.class = label(UI.prop(dc, "SelectedMultiClass"))
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
        { "feat_tab", tabStage(ctx, "featTab") },
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
            local items = list(UI.path(dc, "FeatDetails.AbilitySelection"))
            if #items == 0 then return "next", 0 end
            asi.steps = asi.steps + 1
            if asi.steps > 12 then return "next", 0 end
            local want = plan.abilities
            if want == nil then
                -- Default: everything into the class's primary ability (the page marks it IsPrimary).
                for _, a in ipairs(items) do
                    if UI.prop(a, "IsPrimary") == true then want = { [abilityOf(a)] = 2 } end
                end
                if want then
                    ctx.result.defaulted = ctx.result.defaulted or {}
                    ctx.result.defaulted[#ctx.result.defaulted + 1] = "abilities"
                    plan.abilities = want
                else
                    return "next", 0
                end
            end
            for _, a in ipairs(items) do
                local name = abilityOf(a)
                local target = 0
                for k, v in pairs(want) do if abilityName(k) == name then target = tonumber(v) or 0 end end
                local have = tonumber(UI.prop(a, "Improvement")) or 0
                if have < target then
                    if UI.command(dc, "SelectAbility", a) then return "wait", 150 end
                elseif have > target then
                    if UI.command(dc, "DeselectAbility", a) then return "wait", 150 end
                end
            end
            return "next", 200
        end },
        { "abilities?", function()
            local out = {}
            for _, a in ipairs(list(UI.path(get(), "FeatDetails.AbilitySelection"))) do
                local plus = tonumber(UI.prop(a, "Improvement")) or 0
                if plus ~= 0 then out[abilityOf(a)] = plus end
            end
            if next(out) then ctx.result.set.abilities = out end
            return "next", 0
        end },
        { "skills", skillsStage(get, ctx, plan.skills) },
        { "skills?", skillsReport(get, ctx) },
        { "passives", passivesStage(get, ctx, plan.passives) },
        { "spells", spellsStage("CharacterLevelUp", get, ctx, plan.spells, plan.cantrips,
                                { "levelupTab", "classTab", "subClassTab", "featTab", "spellPrepTab" }), 20000 },
        { "spells?", spellsReport(get, ctx) },
        { "check", function()
            local dc = get()
            ctx.result.complete = UI.prop(dc, "IsLevelUpComplete")
            return "next", 0
        end },
        { "finish", function()
            if args.finish == false then return "done" end
            local dc = get()
            if UI.prop(dc, "IsLevelUpComplete") == false then
                return "fail", "level up is not complete; not finishing (see problems)"
            end
            -- The button passes "Overview" (a camera mode); bg3le can only pass Noesis objects, so nil.
            local ok, err = UI.command(dc, "FinishLevelUp", nil)
            if not ok then return "fail", "FinishLevelUp: " .. tostring(err) end
            ctx.result.finished = true
            return "next", 500
        end },
        { "closed", function()
            if get() then return "wait", 300 end
            return "done"
        end, 10000 },
    }
    return runJob("ui.level_up", stages, ctx, 90000)
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

return P
