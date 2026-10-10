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
--   VERIFIED  (2026-10-10, machine 2) ui.level_up end to end: Gale Wizard 1->2 (subclass Evocation, spells
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

-- Passives (fighting styles, invocations, ...): toggle wanted ones. A selector left incomplete is filled in list
-- order only with `defaults` (character creation, plan.allow_defaults); otherwise passivesReport names the options.
-- A picked passive VM reads Value = 1 (verified live on Wyll's invocations; no IsSelected).
local function passiveOn(it)
    local p = UI.props(it)
    return p.IsSelected == true or p.Selected == true or (tonumber(p.Value) or 0) > 0
end

local function passivesStage(getDC, ctx, wanted, defaults)
    return function()
        local dc = getDC()
        local want = wantList(wanted)
        local found = {}
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
        return "next", 300
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
                ctx.result.problems[#ctx.result.problems + 1] = "passive choice '" .. title .. "' needs " .. more
                    .. " more in plan.passives; can pick: " .. table.concat(v.options, ", ")
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
                    ctx.result.problems[#ctx.result.problems + 1] = "spell choice '" .. s.title .. "' needs "
                        .. (s.open - waiting) .. " more " .. (s.cantrips and "cantrip" or "spell")
                        .. (s.open - waiting == 1 and "" or "s") .. " in plan." .. (s.cantrips and "cantrips" or "spells")
                        .. "; can pick: " .. table.concat(s.can_pick, ", ")
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
        { "passives", passivesStage(get, ctx, plan.passives, true) },
        { "spells", spellsStage("CharacterCreation", get, ctx, plan.spells, plan.cantrips) },
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
-- A screen fade (a conversation just ended: swap_party's camp dialogue) is up. A level-up page opened during it
-- takes its fade-in template and stays frozen at LevelUpStep=Started: no animations, no skip input, nothing
-- hit-testable (live 2026-10-10, Wyll L4 after swap_party: fade=true at open, 226-326 probes never hovered a
-- button; S018/S021 likewise). So the page is opened only once the fade is over.
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
        if #asiList > 0 then
            local max, spent = asiPoints(dc, asiList)
            o.ability_points = { max = max, spent = spent }
        end
        o.feat_panel = UI.prop(dc, "SelectedFeatDetails") ~= nil
    end
    for _, t in ipairs({ "levelupTab", "classTab", "subClassTab", "deityTab", "featTab", "spellReplaceTab", "spellPrepTab" }) do
        local el = UI.cachedByName("CharacterLevelUp", t)
        if el ~= nil and UI.prop(el, "IsSelected") == true then o.tab = t end
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
    if not P.luDC() then
        if not args.uuid then error("uuid required when the level-up page is not open") end
        if P.screenFade() then
            openLater = true
        else
            local ok, err = P.openLevelUp(args.uuid)
            if not ok then error(err) end
        end
    end
    local ctx = { result = { set = {}, problems = {} }, widget = "CharacterLevelUp" }
    local get = P.luDC
    local asi = { steps = 0 }
    local stages = {
        { "open", function()
            local cur = get()
            if cur and not openLater and str(UI.prop(cur, "LevelUpStep")) == "Finishing" then
                -- The previous level's page is still closing: wait for it, then open this level's page.
                ctx.result.waited_finish = true
                return "wait", 300
            end
            if not cur and ctx.result.waited_finish and not ctx.reopened then
                ctx.reopened, openLater = true, true
            end
            if openLater then
                if P.screenFade() then ctx.result.waited_fade = true return "wait", 200 end
                openLater = false
                local ok, err = P.openLevelUp(args.uuid)
                if not ok then return "fail", err end
                return "wait", 300
            end
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
                    ctx.result.problems[#ctx.result.problems + 1] = (max - spent) .. " of " .. max
                        .. " ability points unspent: plan.abilities; can raise: " .. canRaise(items)
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
            if RPC.now_ms() - ctx.closeWait > (step == "Finishing" and 9000 or 4000) then
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

return P
