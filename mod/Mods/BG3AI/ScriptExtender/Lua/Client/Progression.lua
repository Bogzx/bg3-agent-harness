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
--     buttons:  SelectSpell / SelectAbilityBonus take a MultiBinding (selector VM + item VM) that Lua can
--               neither build nor read: on the live level-up page the button's CommandParameter and its
--               DataContext ({Binding}) both read nil (the item is its TemplatedParent ContentPresenter's
--               Content). So spell picks are real mouse clicks by the bridge (ui.level_up_spell_probe,
--               bg3ai/leveling.py click_spells). A selector's buttons exist once its sub-tab (ListBoxItem in
--               "subTabsSpellsList", DataContext = the VMSpellSelector) is selected.
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
--   VERIFIED  (2026-10-10) ui.level_up end to end: Gale Wizard 1->2 (subclass Evocation, spells
--             Magic Missile + Shield by click), Wyll Warlock 1->2 (invocations Agonising + Repelling Blast by
--             TogglePassive, spell Hex by click); FinishLevelUp(nil) works. Spell and passive VMs have no readable
--             name: P.nameSpells / P.namePassives name them from static data (SpellList / PassiveList order).
--   UNTESTED  FinishCreating by command (the run used the Proceed / Venture Forth buttons with the shim),
--             CC spell and ability-bonus presses (same MultiBinding problem as level-up spells), custom point
--             buy, feat levels, multiclassing, spell replacement.
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
    -- Spell and passive VMs show icons only; their names come from the static data lists (P.nameSpells).
    r = P.vmNames and P.vmNames[tostring(vm)]
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

-- Lower case, no spaces or punctuation, and British spellings folded ("Agonising" = "Agonizing",
-- "Armour" = "Armor"; applied to both sides, so exact names still match).
local function norm(s)
    return (tostring(s or ""):lower():gsub("[%s'’%-_]", ""):gsub("our", "or"):gsub("is", "iz"))
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

-- Keyed by norm()'s first 3 letters: norm turns "is" into "iz", so "wis"/"Wisdom" key as "wiz" (a plan's
-- {wis = 10} never matched the page's Wisdom before).
local ABILITIES = {
    str = "Strength", dex = "Dexterity", con = "Constitution",
    int = "Intelligence", wiz = "Wisdom", cha = "Charisma",
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
        if ctx.widget and selectTab(ctx.widget, tab) then
            -- ctx.fast (cc_apply, P.CC_STATE_WAITS): the next selectStage waits for the tab's labels instead
            if ctx.fast then ctx.tabAt = RPC.now_ms() return "next", 0 end
            return "next", 450
        end
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
    -- Level up: the feat panel is bound to SelectedFeatDetails (CCLib_k.xaml featsPanel); CC keeps FeatSkills.
    { key = "feat", path = "SelectedFeatDetails.FeatSkills" },
    { key = "feat", path = "FeatSkills" },
}

local function skillGroups(dc)
    local out, keys = {}, {}
    for _, g in ipairs(SKILL_GROUPS) do
        local grp = not keys[g.key] and UI.path(dc, g.path) or nil
        local skills = grp and UI.prop(grp, "Skills")
        if skills and UI.count(skills) > 0 then
            keys[g.key] = true
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
    "FeatDetails.PassiveSelectors", "SelectedFeatDetails.FeatPassives",
}

P.featSel = P.featSel or {}   -- tostring(selector) -> the chosen feat's label, for feat passive selectors

local function passiveSelectors(dc)
    local out, seen = {}, {}
    for _, path in ipairs(PASSIVE_PATHS) do
        for _, sel in ipairs(list(UI.path(dc, path))) do
            if not seen[tostring(sel)] then
                seen[tostring(sel)] = true
                if path:find("Feat", 1, true) then
                    P.featSel[tostring(sel)] = label(UI.prop(dc, "SelectedFeat")) or ""
                end
                out[#out + 1] = sel
            end
        end
    end
    return out
end

-- Passive names (invocations, fighting styles, ...): the VMs are unreadable like spells (P.nameSpells). A
-- selector's Passives are its PassiveList in list order (verified live: Wyll's Warlock 2 invocations = list
-- 333fb1b0 "Warlock invocations", 10 entries). The list is the one with that many entries among those the
-- character's class/subclass progressions offer at their next level (P.levelUpUuid, set by the level-up ops),
-- else among all SelectPassives lists of all progressions; only a unique match names it.
P.vmNames = P.vmNames or {}
local passiveIndex     -- { byTable = {"<table>:<level>" = {list ids}}, all = {list ids} }, built once
local passiveNames = {} -- list id -> {display names in order}

local function passiveListNames(id)
    local c = passiveNames[id]
    if c == nil then
        c = {}
        local l = try(Ext.StaticData.Get, id, "PassiveList")
        for k, v in pairs(l and l.Passives or {}) do
            local pid = type(k) == "string" and k or v
            if type(pid) == "string" then
                local st = try(Ext.Stats.Get, pid)
                local name = st and try(Ext.Loca.GetTranslatedString, st.DisplayName)
                c[#c + 1] = (name ~= nil and name ~= "") and name or pid
            end
        end
        c.title = l and l.Name ~= "" and l.Name or nil   -- e.g. "Warlock invocations"
        passiveNames[id] = c
    end
    return c
end

local function passiveLists()
    if passiveIndex == nil then
        passiveIndex = { byTable = {}, all = {} }
        local seen = {}
        for _, pid in ipairs(try(Ext.StaticData.GetAll, "Progression") or {}) do
            local pr = try(Ext.StaticData.Get, pid, "Progression")
            for _, sp in ipairs(pr and try(function() return pr.SelectPassives end) or {}) do
                local id = sp.UUID
                if id then
                    local key = tostring(pr.TableUUID) .. ":" .. tostring(pr.Level)
                    passiveIndex.byTable[key] = passiveIndex.byTable[key] or {}
                    table.insert(passiveIndex.byTable[key], id)
                    if not seen[id] then seen[id] = true table.insert(passiveIndex.all, id) end
                end
            end
        end
        -- Feats' own choices (Elemental Adept's element, Weapon Master's weapons, ...): Feat resources carry
        -- SelectPassives like progressions do.
        passiveIndex.feats = {}
        for _, fid in ipairs(try(Ext.StaticData.GetAll, "Feat") or {}) do
            local f = try(Ext.StaticData.Get, fid, "Feat")
            local fname = f and norm(try(function() return f.Name end))
            for _, sp in ipairs(f and try(function() return f.SelectPassives end) or {}) do
                local id = sp.UUID
                if id then table.insert(passiveIndex.feats, { name = fname, id = id }) end
            end
        end
    end
    return passiveIndex
end

-- PassiveList ids the leveling character's classes and subclasses offer at their next level.
local function characterPassiveLists(uuid)
    local out = {}
    local ent = uuid and try(Ext.Entity.Get, uuid)
    local classes = ent and try(function() return ent.Classes.Classes end) or {}
    local idx = passiveLists()
    for _, c in ipairs(classes) do
        for _, cu in ipairs({ c.ClassUUID, c.SubClassUUID }) do
            local cd = cu and try(Ext.StaticData.Get, cu, "ClassDescription")
            if cd then
                for _, id in ipairs(idx.byTable[tostring(cd.ProgressionTableUUID) .. ":" .. tostring((c.Level or 0) + 1)] or {}) do
                    out[#out + 1] = id
                end
            end
        end
    end
    return out
end

function P.namePassives(sel)
    local items = list(UI.prop(sel, "Passives"))
    local n = #items
    if n == 0 then return nil end
    local function unique(ids)
        local found, key
        for _, id in ipairs(ids) do
            local names = passiveListNames(id)
            if #names == n then
                local k = table.concat(names, "\0")
                if found == nil then found, key = names, k elseif k ~= key then return nil, true end
            end
        end
        return found
    end
    local feat = P.featSel[tostring(sel)]
    local names
    if feat then
        -- A feat's choice: that feat's lists (Feat resource Name "ElementalAdept" = page "Elemental Adept"), else
        -- a unique one among all feats' lists.
        local mine, all = {}, {}
        for _, e in ipairs(passiveLists().feats) do
            all[#all + 1] = e.id
            if e.name == norm(feat) then mine[#mine + 1] = e.id end
        end
        names = unique(mine)
        if not names then
            local amb
            names, amb = unique(all)
            if amb then names = nil end
        end
    else
        names = unique(characterPassiveLists(P.levelUpUuid))
    end
    if not names and not feat then
        local amb
        names, amb = unique(passiveLists().all)
        if amb then names = nil end
    end
    if names then
        for i, it in ipairs(items) do P.vmNames[tostring(it)] = names[i] end
        if names.title then P.vmNames[tostring(sel)] = names.title end
    end
    return names
end

local function passivesView(dc)
    local out = {}
    for _, sel in ipairs(passiveSelectors(dc)) do
        P.namePassives(sel)
        local max = tonumber(UI.prop(sel, "MaxSelectedPassiveCount"))
        local chosen, opts = {}, {}
        for i, it in ipairs(list(UI.prop(sel, "Passives"))) do
            local p = UI.props(it)
            local n = label(it) or ("#" .. i)
            if p.IsSelected == true or p.Selected == true or (tonumber(p.Value) or 0) > 0 then chosen[#chosen + 1] = n
            elseif p.Enabled ~= false and p.Blocked ~= true then opts[#opts + 1] = n end
        end
        out[#out + 1] = { title = str(UI.prop(sel, "Title")) or label(sel), max = max,
                          picked = chosen, options = opts, complete = UI.prop(sel, "IsComplete") }
    end
    return out
end

-- { path, source }: the level-up feat panel's selectors sit under SelectedFeatDetails (Magic Initiate, ...).
local SPELL_PATHS = {
    { "ClassProgressionDetails.SpellSelectors", "ClassProgressionDetails" },
    { "RaceProgressionDetails.SpellSelectors", "RaceProgressionDetails" },
    { "FeatDetails.SpellSelectors", "FeatDetails" },
    { "SelectedFeatDetails.FeatDetails.SpellSelectors", "FeatDetails" },
}

local function spellSelectors(dc)
    local out, seen = {}, {}
    for _, e in ipairs(SPELL_PATHS) do
        for _, sel in ipairs(list(UI.path(dc, e[1]))) do
            if not seen[tostring(sel)] then
                seen[tostring(sel)] = true
                out[#out + 1] = { sel = sel, source = e[2] }
            end
        end
    end
    return out
end

-- Spell names. The VMs carry no readable name (TranslatedStrings bg3le cannot read; the lists render icons
-- only), so they come from static data: a selector's Available list is its SpellList in list order (verified
-- live: Gale's Wizard 2 selector = SpellList 11f331b0, 23 spells, the same level/school sequence). The list
-- whose level/school sequence equals the page's names it; ambiguous or unmatched lists stay unnamed. Only the
-- spells the page already shows get a name, which is what their tooltips show the player.
P.vmNames = P.vmNames or {}
P.spellStats = P.spellStats or {}
local spellListsByCount       -- n -> { {id, spells = {stat ids in order}} }, built once per Lua state

local function spellStat(sid)
    local c = P.spellStats[sid]
    if c == nil then
        local st = try(Ext.Stats.Get, sid)
        local name = st and try(Ext.Loca.GetTranslatedString, st.DisplayName)
        if name == "" then name = nil end
        c = { level = tonumber(st and st.Level) or -1, school = tostring(st and st.SpellSchool or ""), name = name or sid }
        P.spellStats[sid] = c
    end
    return c
end

local function listSpells(l)
    local out = {}
    -- The list is a set proxy: pairs gives the ids in list order (key = id, value = true).
    for k, v in pairs(l.Spells or {}) do
        local sid = type(k) == "string" and k or v
        if type(sid) == "string" then out[#out + 1] = sid end
    end
    return out
end

local function spellLists(n)
    if spellListsByCount == nil then
        spellListsByCount = {}
        for _, id in ipairs(try(Ext.StaticData.GetAll, "SpellList") or {}) do
            local l = try(Ext.StaticData.Get, id, "SpellList")
            local spells = l and listSpells(l) or {}
            local c = #spells
            if c > 0 then
                spellListsByCount[c] = spellListsByCount[c] or {}
                table.insert(spellListsByCount[c], { id = id, spells = spells })
            end
        end
    end
    return spellListsByCount[n] or {}
end
P.spellLists = spellLists

-- Forget the static-data name caches (offline tests; a data change).
function P.clearNameCaches()
    spellListsByCount, passiveIndex, passiveNames = nil, nil, {}
    P.featSel = {}
    P.vmNames, P.spellStats = {}, {}
end

local function vmSig(spellVM)
    return tostring(tonumber(UI.prop(spellVM, "Level")) or -1) .. "/" .. tostring(UI.prop(spellVM, "SpellSchool") or "")
end

-- Names a selector's Available spells (and every Spell VM they share with Additions). Returns names in order
-- (nil where unknown) and the matched list id.
function P.nameSpells(sel)
    local items = list(UI.prop(sel, "Available"))
    local n = #items
    if n == 0 then return {}, nil end
    local sig = {}
    for i, it in ipairs(items) do sig[i] = vmSig(UI.prop(it, "Spell")) end
    local found, foundKey, ambiguous = nil, nil, false
    for _, l in ipairs(spellLists(n)) do
        local ok = true
        for i, sid in ipairs(l.spells) do
            local st = spellStat(sid)
            if (tostring(st.level) .. "/" .. st.school) ~= sig[i] then ok = false break end
        end
        if ok then
            local key = table.concat(l.spells, ",")
            if found == nil then found, foundKey = l, key
            elseif key ~= foundKey then ambiguous = true end
        end
    end
    local names = {}
    if found and not ambiguous then
        for i, it in ipairs(items) do
            local st = spellStat(found.spells[i])
            names[i] = st.name
            P.vmNames[tostring(it)] = st.name
            local sp = UI.prop(it, "Spell")
            if sp then P.vmNames[tostring(sp)] = st.name end
        end
        return names, found.id
    end
    return names, nil
end

local function spellUnknown(it, i)
    local sp = UI.prop(it, "Spell")
    return "#" .. i .. " (level " .. tostring(UI.prop(sp, "Level")) .. " " .. tostring(UI.prop(sp, "SpellSchool")) .. ")"
end

-- What a selector offers and still needs: picked names, open slots, and the names that can be picked.
local function selectorState(sel)
    P.nameSpells(sel)
    local picked, open = {}, 0
    for _, it in ipairs(list(UI.prop(sel, "Additions"))) do
        if UI.prop(it, "Spell") == nil then open = open + 1 else picked[#picked + 1] = label(it) or "?" end
    end
    local can, known = {}, {}
    for i, it in ipairs(list(UI.prop(sel, "Available"))) do
        local n = label(it) or spellUnknown(it, i)
        if UI.prop(it, "NotAvailable") == true then known[#known + 1] = n
        elseif UI.prop(it, "Selected") ~= true then can[#can + 1] = n end
    end
    return { picked = picked, open = open, can_pick = can, known = known,
             cantrips = UI.prop(sel, "IsCantrips") == true, complete = UI.prop(sel, "IsComplete") }
end

local function selectorTitle(s, st)
    local t = str(UI.prop(s.sel, "Title"))
    if t then return t end
    local src = s.source:gsub("ProgressionDetails$", ""):gsub("Details$", "")
    return (src == "Class" and "" or (src:lower() .. " ")) .. (st.cantrips and "cantrips" or "spells")
end

local function spellsView(dc)
    local out = {}
    for _, s in ipairs(spellSelectors(dc)) do
        local st = selectorState(s.sel)
        out[#out + 1] = {
            title = selectorTitle(s, st), from = s.source, cantrips = st.cantrips, picked = st.picked,
            open = st.open, can_pick = st.can_pick, known = #st.known > 0 and st.known or nil, complete = st.complete,
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

-- File-local helpers the other parts use.
P.inner = { try = try, isObj = isObj, same = same, str = str, label = label, ident = ident, set = set,
    list = list, norm = norm, pick = pick, view = view, abilityName = abilityName, abilityOf = abilityOf,
    visibleWidget = visibleWidget, widgetDC = widgetDC, pages = pages, selectTab = selectTab,
    tabStage = tabStage, pressFor = pressFor, skillGroups = skillGroups, skillsView = skillsView,
    passiveSelectors = passiveSelectors, passivesView = passivesView, spellSelectors = spellSelectors,
    selectorState = selectorState, spellsView = spellsView, validity = validity }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads P and the
-- helpers in P.inner.
for _, part in ipairs({ "Client/ProgressionCC.lua", "Client/ProgressionLevelUp.lua" }) do
    Ext.Require(part)
end

return P
