-- ui.state: what screen the player is looking at, and the decision-relevant
-- content of it, in short fields (owner: worker "io").
--
-- Classification is by the pages present under ContentRoot (names are the
-- XAML root x:Name, see UI.lua). Content comes from the DataContext where the
-- XAML binds it, and otherwise from the rendered TextBlocks, so everything
-- returned is something on screen.
--
-- Live-verified (2026-10-09, Moonrise Towers): game / game_menu
-- classification, region, subtitle fields, notifications (empty), widgets.
-- Untested live (no dialogue/roll/reaction allowed on the user's save):
-- dialogue, roll, reaction, container, popup, tutorial, game_over. They are
-- built from DialogueTemplates.xaml, ActiveRoll.xaml,
-- ReactionDecisionPopup.xaml, MessageBox.xaml, TutorialPopup.xaml,
-- Tutorial.xaml and Container.xaml bindings.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState or {}
BG3AI.UIState = S

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local prop, props, clean = UI.prop, UI.props, UI.clean

-- Page name -> screen, in priority order (modal things first).
S.SCREENS = {
    -- The end credits after the final cinematic (Mods/MainUI/GUI/Pages/Credits.xaml, ContextName "Credits",
    -- DCCredits; the main menu's Credits button opens the same page). First: it can sit over the main menu.
    -- The page has no x:Name: UI.widgets names it by its ContextName, S.state also by its DC type (S.PAGE_DC).
    { "Credits", "credits" },
    { "MainMenu", "main_menu" },
    -- "Press any key to continue" after a fresh launch, before the main menu.
    { "SplashScreen", "splash" },
    { "CharacterCreation", "character_creation" },
    { "CharacterRespec", "character_creation" },
    { "CharacterFullRespec", "character_creation" },
    { "CharacterChangeAppearance", "character_creation" },
    { "ReactionDecisionPopup", "reaction" },
    { "ActiveRoll", "roll" },
    { "Dialog_box", "popup" },
    { "SignUp", "popup" },
    { "Eula", "popup" },
    { "SaveGamePopup", "popup" },
    { "ModalTutorial", "tutorial" },
    { "TutorialPopup", "tutorial" },
    { "Dialogue", "dialogue" },
    { "CharacterLevelUp", "level_up" },
    { "Trade", "trade" },
    { "Donate", "donate" },
    { "Insert", "insert" },
    { "Pickpocket", "trade" },
    { "LongRest", "rest" },
    { "MakeCamp", "rest" },
    { "ContainerWidget", "container" },
    { "Book", "book" },
    { "RewardPanel", "reward" },
    { "LearnSpells", "learn_spells" },
    { "TadpolePowersTree", "tadpole_tree" },
    { "SplitItem", "popup" },
    { "SaveGame", "save_load" },
    { "LoadGame", "save_load" },
    { "GameMenu", "game_menu" },
    { "GameOptions", "options" },
    { "AudioOptions", "options" },
    { "VideoOptions", "options" },
    { "InterfaceOptions", "options" },
    { "KeybindingOptions", "options" },
    { "AccessibilityOptions", "options" },
    { "JournalMap", "map" },
    { "OverviewMap", "map" },
}

-- Screens where a modal blocks world actions (move/attack/interact).
S.BLOCKING = {
    main_menu = true, loading = true, character_creation = true, reaction = true, roll = true,
    popup = true, tutorial = true, dialogue = true, level_up = true, trade = true, rest = true,
    book = true, reward = true, donate = true, learn_spells = true, tadpole_tree = true, save_load = true,
    game_menu = true, options = true, game_over = true, credits = true, splash = true,
}

-- Pages also found by their DataContext type, in case the name doesn't read (live 2026-10-17: the Credits page
-- has an empty Name and was listed as "?").
S.PAGE_DC = { Credits = "gui::DCCredits" }

-- Always-there HUD pages; anything else that is visible counts as a panel.
S.HUD = {
    HudIndicator = true, OverheadInfo = true, Minimap = true, CombatLog = true, TurnModeInfo = true,
    TargetInfo = true, CombatantsOverlay = true, WorldTooltips = true, HotBar = true, PlayerPortraits = true,
    AlwaysOnTopOverlay = true, PassiveRoll = true, CursorText = true, Overlay = true, WorldContextMenu = true,
    NotificationWidget = true, DragAndDropPreview = true, ScreenFade = true, CrossplayOverlay = true,
    Busy = true, Saving = true, SelectionFlyOut = true, PinnedTooltips = true, ModDownloadingWidget = true,
    HudRoot = true,
}

------------------------------------------------------------------------
-- Pieces
------------------------------------------------------------------------

-- "[PERSUASION] [CLERIC] Let me help." -> tags {"PERSUASION","CLERIC"}, "Let me help."
function S.splitTags(text)
    local tags = {}
    if not text then return tags, text end
    local rest = text
    while true do
        local tag, after = rest:match("^%s*%[([^%]]+)%]%s*(.*)$")
        if not tag then break end
        tags[#tags + 1] = tag
        rest = after
    end
    return tags, rest
end

local function activeDialogue(dc)
    return UI.path(dc, "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.ActiveDialogue")
end
S.activeDialogue = activeDialogue

-- Live answer VM (gui::VMDialogueAnswer, probed live 2026-10-10): values
-- AnswerIdx, Enabled, BoundEvent; objects CtxAnswer (ls.VMContextTransString
-- with a Params collection: the runs the answer template renders, tag params
-- as "[TEXT] ") and AnswerTags (ls.VMDialogueAnswerTag items).
local function vmItems(obj, name, max)
    local coll = obj and props(obj)[name]
    local out = {}
    if coll == nil then return out end
    for _, x in UI.items(coll, max or 12) do
        if x ~= nil then out[#out + 1] = props(x) end
    end
    return out
end

local function tagName(p)
    for _, k in ipairs({ "Text", "Name", "DisplayName", "Label", "Tag" }) do
        local t = type(p[k]) == "string" and clean(p[k]) or nil
        if t then return t:upper() end
    end
end

-- Tags of an answer from its VM: AnswerTags, then CtxAnswer tag params
-- (S007-S013: the rendered AnswerText often came back without its
-- "[PERSUASION] " runs, so check options looked plain and got no annotation).
function S.answerTags(vm)
    local tags = {}
    for _, p in ipairs(vmItems(vm, "AnswerTags")) do
        tags[#tags + 1] = tagName(p)
    end
    local ctx = props(vm).CtxAnswer
    for _, p in ipairs(vmItems(ctx, "Params")) do
        if p.Type == "Tag" then tags[#tags + 1] = tagName(p) end
    end
    return tags
end

-- An answer's text from its VM alone (no rendered element): the CtxAnswer
-- params in order, tags as "[TAG] ". nil when no param has text.
function S.answerTextFromVM(vm)
    local parts = {}
    for _, p in ipairs(vmItems(props(vm).CtxAnswer, "Params", 20)) do
        if type(p.Text) == "string" then
            parts[#parts + 1] = p.Type == "Tag" and ("[" .. p.Text:upper() .. "] ") or p.Text
        end
    end
    return clean(table.concat(parts))
end

-- Rendered text of an AnswerText block: the inline runs (tag runs included)
-- when they say more than ToString (which can be the bare line without tags).
local function answerText(textEl)
    local plain = UI.text(textEl)
    local inl = UI.prop(textEl, "Inlines")
    local runs = inl and clean(UI.inlineText(inl)) or nil
    if runs and #runs > #(plain or "") then return runs end
    return plain
end

local function addTags(a, more)
    if not more or #more == 0 then return end
    a.tags = a.tags or {}
    local have = {}
    for _, t in ipairs(a.tags) do have[t:upper()] = true end
    for _, t in ipairs(more) do
        if not have[t:upper()] then a.tags[#a.tags + 1] = t; have[t:upper()] = true end
    end
end

-- Visible answers in display order: { {i, text, tags, off?, vm} }, changing.
-- SAFETY (S001 crash, use-after-free in bg3le's Noesis binding): never read
-- a rendered answer container's DataContext. When an answer is chosen the
-- answer VMs are released at once while the old LSListBoxItems stay in the
-- tree until the next layout pass, still pointing at the freed VMs. VMs are
-- taken from ActiveDialogue.Answers (live) and paired by index with the
-- rendered texts; if the counts differ the node is changing and nothing is
-- returned (changing = true).
function S.dialogueAnswers(widget, ad)
    local out = {}
    local list = UI.byName(widget, "answerList")
    if not list then return out, false end
    local items = UI.findAll(list, function(e)
        local ty = UI.typeName(e)
        return ty == "ls.LSListBoxItem" or ty == "ListBoxItem"
    end, { prune = true })
    local vms = ad and UI.prop(ad, "Answers")
    local n = vms and UI.count(vms) or 0
    if #items ~= n then
        -- S007/S010: the answers stayed on screen for minutes while the rendered
        -- list and Answers never matched, so options were never reported and
        -- choose failed ("dialogue is changing"). When the game says answers are
        -- shown, take them from the live VMs alone (never the rendered items).
        if n > 0 and props(ad).ShowAnswers == true then
            local fromVM = S.answersFromVMs(vms, n)
            if fromVM then return fromVM, false end
        end
        -- S015 (Olak, 6 answers on screen, 7 in Answers): one VM is never shown,
        -- so the rule above gave nothing for minutes and the agent read the
        -- options off a screenshot. Pair each on-screen answer text with the
        -- live VM that has the same text (rendered texts only, never their
        -- DataContext); used only when every rendered answer finds its own VM.
        if n > 0 and #items > 0 and props(ad).ShowAnswers ~= false then
            local paired = S.pairRenderedWithVMs(items, vms, n)
            if paired then return paired, false end
        end
        -- S026 Karlach's companion menu (6 answers rendered in a scrolling list, 13 VMs; choose failed
        -- twice, the agent clicked by hand): the list is virtualized, so never all rendered. When the game
        -- says answers are shown, list the live VMs that have text, in the game's order (a text-less VM is a
        -- hidden answer, S015), provided the rendered list is current (one of its texts is a VM's).
        local texted, current = 0, false
        if n > 0 and #items > 0 and props(ad).ShowAnswers == true then
            local fromVM
            fromVM, current = S.textedAnswersFromVMs(vms, n, items)
            texted = fromVM and #fromVM or 0
            if fromVM and #fromVM >= #items and current then return fromVM, false end
        end
        -- Live 12:1x (Karlach's camp menu, 10 VMs, 6 rows rendered): no VM had text at all (CtxAnswer's
        -- one Text param carries no Text), so nothing above matched and get_dialogue showed no options
        -- (dialogue_advance then waited 15 s on "line still playing"). The list shows Answers in order from
        -- its top, so the rendered rows are VMs 1..#items: pair them by position (texts from the rows only,
        -- never a row's DataContext) and say how many more sit below the visible part of the list.
        -- Live 13:4x (Karlach, 10 answers, 6 rows): the list is a scrolled window of rows. Its ScrollViewer says
        -- which: the first realized row is answer floor(VerticalOffset / row height) + 1 (true at offsets 0, 48,
        -- 144, 216, 350), and ExtentHeight / row height counts every answer. When that count is the VM count,
        -- the rows pair with VMs by position whatever texts the VMs have (with one texted VM, e.g. "Leave.",
        -- the rule above found none and get_dialogue showed no options at all).
        if n > #items and #items > 0 and props(ad).ShowAnswers == true then
            local win = S.listWindow(list, items)
            if win and win.total == n then
                local byPos = S.pairRenderedByPosition(items, vms, win.first)
                if byPos then
                    return byPos, false, { below = n - (win.first - 1) - #items, above = win.first - 1,
                        by_position = true }
                end
            elseif not win and texted == 0 then -- no scroll info: the list's top rows (live 12:1x)
                local byPos = S.pairRenderedByPosition(items, vms, 1)
                if byPos then return byPos, false, { below = n - #items } end
            end
        end
        -- S063 10:53 (Karlach's camp menu again: 6 rows on screen, "Come on, we're heading out" 8th): none of the
        -- pairings above held, so get_dialogue showed no options at all, choose refused "changing", swap_party
        -- stalled 25 s and the agent scrolled with DOWN and clicked by hand (~70 s). When the game says answers
        -- are shown, every rendered row has text, and no command of ours is still settling (rows of the node we
        -- just answered stay in the tree for a layout pass), list the rows as the player sees them, numbered by
        -- the list's window; choose presses the row's own button (rows_only: no VM pairing is claimed). A list
        -- whose texts match none of the VMs that have text is the previous node's: still changing.
        if #items > 0 and n >= #items and props(ad).ShowAnswers == true and not UI.quiet(6)
            and (texted == 0 or current) then
            local win = S.listWindow(list, items)
            local first = win and win.first or 1
            local rows = S.renderedRows(items, first)
            if rows then
                local below = (win and win.total or n) - (first - 1) - #items
                return rows, false, { below = below > 0 and below or nil, above = first > 1 and first - 1 or nil,
                    rows_only = true }
            end
        end
        return out, #items > 0 or n > 0, { rendered = #items, vms = n, show = props(ad).ShowAnswers, texted = texted }
    end
    for idx, item in ipairs(items) do
        local textEl = UI.byName(item, "AnswerText")
        local text = textEl and answerText(textEl) or UI.allText(item)
        if text then
            local vm = try(vms.At, vms, idx)
            local tags, body = S.splitTags(text)
            local a = { i = #out + 1, text = body, vm = vm }
            if #tags > 0 then a.tags = tags end
            if vm then addTags(a, try(S.answerTags, vm)) end
            if vm and props(vm).Enabled == false then a.off = true end
            out[#out + 1] = a
        end
    end
    return out, false
end

-- Answers built from the live answer VMs only; nil unless every one has text.
function S.answersFromVMs(vms, n)
    local out = {}
    for idx = 1, n do
        local vm = try(vms.At, vms, idx)
        local text = vm and try(S.answerTextFromVM, vm)
        if not text then return nil end
        local tags, body = S.splitTags(text)
        local a = { i = idx, text = body, vm = vm }
        if #tags > 0 then a.tags = tags end
        addTags(a, try(S.answerTags, vm))
        if props(vm).Enabled == false then a.off = true end
        out[idx] = a
    end
    return out
end

local function answerKey(text)
    local _, body = S.splitTags(text or "")
    body = clean(body)
    return body and (body:lower():gsub("[%s%p]+", " "))
end

-- Answers from the live VMs that have text (game order, renumbered), and whether
-- any rendered answer's text is one of them (the rendered list is current).
function S.textedAnswersFromVMs(vms, n, items)
    local out, keys = {}, {}
    for idx = 1, n do
        local vm = try(vms.At, vms, idx)
        local text = vm and try(S.answerTextFromVM, vm)
        if text then
            local tags, body = S.splitTags(text)
            local a = { i = #out + 1, text = body, vm = vm }
            if #tags > 0 then a.tags = tags end
            addTags(a, try(S.answerTags, vm))
            if props(vm).Enabled == false then a.off = true end
            out[#out + 1] = a
            local k = answerKey(text)
            if k then keys[k] = true end
        end
    end
    local current = false
    for _, item in ipairs(items or {}) do
        local textEl = UI.byName(item, "AnswerText")
        local text = textEl and answerText(textEl) or UI.allText(item)
        local k = text and answerKey(text)
        if k and keys[k] then current = true break end
    end
    return #out > 0 and out or nil, current
end

-- Rendered answers in display order, each with the live VM whose text matches
-- (by body, tags ignored). nil unless every rendered answer has text and its
-- own distinct VM, so a stale list from the previous node never matches.
function S.pairRenderedWithVMs(items, vms, n)
    local pool = {}
    for idx = 1, n do
        local vm = try(vms.At, vms, idx)
        local key = vm and answerKey(try(S.answerTextFromVM, vm))
        if key then pool[#pool + 1] = { vm = vm, key = key } end
    end
    local out = {}
    for _, item in ipairs(items) do
        local textEl = UI.byName(item, "AnswerText")
        local text = textEl and answerText(textEl) or UI.allText(item)
        local key = text and answerKey(text)
        if not key then return nil end
        local hit
        for j, c in ipairs(pool) do
            if c.key == key then hit = j break end
        end
        if not hit then return nil end
        local vm = table.remove(pool, hit).vm
        local tags, body = S.splitTags(text)
        local a = { i = #out + 1, text = body, vm = vm }
        if #tags > 0 then a.tags = tags end
        addTags(a, try(S.answerTags, vm))
        if props(vm).Enabled == false then a.off = true end
        out[#out + 1] = a
    end
    return out
end

-- The scrolled window of an answer list: { first = answer number of the first
-- realized row, total = answers in the whole list, offset, scrollable } from
-- its ScrollViewer (named reads only) and the rows' height. nil when unread.
function S.listWindow(list, items)
    local sv = UI.find(list, function(e) return (UI.typeName(e) or ""):find("ScrollViewer", 1, true) ~= nil end,
        { hidden = true, max = 400 })
    local p = sv and UI.pick(sv, { "VerticalOffset", "ExtentHeight", "ScrollableHeight" })
    local rowH = items[1] and tonumber(UI.get(items[1], "ActualHeight"))
    local off, ext = p and tonumber(p.VerticalOffset), p and tonumber(p.ExtentHeight)
    if not (off and ext and rowH and rowH > 1) then return nil end
    return { first = math.floor(off / rowH + 0.001) + 1, total = math.floor(ext / rowH + 0.5), offset = off,
        scrollable = tonumber(p.ScrollableHeight) }
end

-- Whether the answer list scrolls (its ScrollViewer has ScrollableHeight). S081 Karlach's camp menu: a two-line
-- first row made listWindow's row-height arithmetic count 7 of ~9 answers, no "more below", so the join answer below
-- the fold was never listed. The bridge wheels and reads a scrolling list whatever the counts say.
S.SCROLL_CHECK_MIN = 3 -- fewer answers than this never fill the list's window
function S.answersScroll(widget)
    local list = UI.byName(widget, "answerList")
    local sv = list and UI.find(list, function(e) return (UI.typeName(e) or ""):find("ScrollViewer", 1, true) ~= nil end,
        { hidden = true, max = 400 })
    local h = sv and tonumber(UI.get(sv, "ScrollableHeight"))
    return h ~= nil and h > 0.5
end

-- Rendered rows paired with VMs first..first+#items-1 by position (the list's
-- visible window; first = 1 at the top). nil unless every row has text.
function S.pairRenderedByPosition(items, vms, first)
    local out = {}
    first = first or 1
    for k, item in ipairs(items) do
        local textEl = UI.byName(item, "AnswerText")
        local text = textEl and answerText(textEl) or UI.allText(item)
        if not clean(text) then return nil end
        local idx = first + k - 1
        local vm = try(vms.At, vms, idx)
        if not vm then return nil end
        local tags, body = S.splitTags(text)
        local a = { i = idx, text = body, vm = vm, row = k }
        if #tags > 0 then a.tags = tags end
        addTags(a, try(S.answerTags, vm))
        if props(vm).Enabled == false then a.off = true end
        out[#out + 1] = a
    end
    return out
end

-- Rendered rows as answers without VMs (texts from the rows only, never their
-- DataContext): { i = first + k - 1, text, row = k }. nil unless every row has text.
function S.renderedRows(items, first)
    local out = {}
    for k, item in ipairs(items) do
        local textEl = UI.byName(item, "AnswerText")
        local text = textEl and answerText(textEl) or UI.allText(item)
        if not clean(text) then return nil end
        local tags, body = S.splitTags(text)
        local a = { i = (first or 1) + k - 1, text = body, row = k }
        if #tags > 0 then a.tags = tags end
        out[#out + 1] = a
    end
    return out
end

-- The continue answer VM (ActiveDialogue.ContinueAnswer) when present.
function S.continueVM(ad)
    return ad and props(ad).ContinueAnswer or nil
end

function S.dialogue(wmap)
    local w = wmap.Dialogue
    local dc = (w and try(function() return w.el.DataContext end)) or UI.anyDC()
    local ad = activeDialogue(dc)
    if ad == nil and not w then return nil end
    local p = props(ad)
    local d = {}
    d.speaker = clean(p.SpeakerName)
    d.text = clean(p.BodyText)
    if (not d.text) and w then
        local body = UI.byName(w.el, "TextBodyContainer")
        d.text = body and UI.text(body)
    end
    if w then
        local answers, changing, counts = S.dialogueAnswers(w.el, ad)
        if changing then d.changing = true; d.answer_counts = counts end
        if not changing and counts and counts.below then
            d.more_below = counts.below
            if (counts.above or 0) > 0 then d.more_above = counts.above end
        end
        if not changing and #answers >= S.SCROLL_CHECK_MIN and S.answersScroll(w.el) then d.scrollable = true end
        if #answers > 0 then
            d.options = {}
            for _, a in ipairs(answers) do
                d.options[#d.options + 1] = { i = a.i, text = a.text, tags = a.tags, off = a.off }
            end
        end
    end
    if d.text and not d.speaker then
        -- Rendered body is "<speaker>: <line>" when speaker names are shown.
        local sp, line = d.text:match("^([^:]+):%s+(.+)$")
        if sp and #sp <= 40 then d.speaker, d.text = sp, line end
    end
    -- The continue item already exists while a line is still being voiced;
    -- it is only a prompt once answers are shown (ShowAnswers).
    if not d.options then
        if p.ShowAnswers == false then
            d.listening = true
        elseif p.ContinueAnswer ~= nil then
            d.continue = true
        end
    end
    if p.CanTrade then d.can_trade = true end
    return d
end

local ROLL_PHASE = {
    IntroductionAnimation = "intro", WaitForStart = "ready", StartRoll = "rolling", StopRoll = "rolling",
    ResultReady = "result", WaitForReRoll = "reroll",
}

local function nameValueList(coll, max)
    local out = {}
    for _, vm in UI.items(coll, max or 12) do
        local p = props(vm)
        local name = clean(p.Name) or clean(p.Description)
        local e = { name = name }
        if type(p.Value) == "number" or type(p.Value) == "string" then e.value = p.Value end
        if p.IsDisabled == true then e.off = true end
        if name or e.value then out[#out + 1] = e end
    end
    return out
end

function S.roll(wmap)
    local w = wmap.ActiveRoll
    if not w then return nil end
    local dc = try(function() return w.el.DataContext end)
    local p = props(dc)
    local r = {
        phase = ROLL_PHASE[tostring(p.RollState)] or tostring(p.RollState),
        skill = clean(p.SkillOrAbility),
        context = p.RollContext ~= "None" and p.RollContext or nil,
    }
    local roll = p.Roll
    if roll then
        local rp = props(roll)
        if rp.ShowDC ~= false and type(rp.DifficultyCheck) == "number" and rp.DifficultyCheck > 0 then
            r.dc = rp.DifficultyCheck
        end
        if rp.RollAdvantageType and rp.RollAdvantageType ~= "None" then r.advantage = tostring(rp.RollAdvantageType) end
        r.modifiers = nameValueList(rp.Modifiers)
        local adv = nameValueList(rp.Advantages)
        if #adv > 0 then r.advantages = adv end
    end
    local boosts = nameValueList(p.BoostModifierList)
    if #boosts > 0 then r.boosts = boosts end
    local sel = nameValueList(p.SelectedBoostModifierList)
    if #sel > 0 then r.selected = sel end
    if type(p.InspirationPoints) == "number" then r.inspiration = p.InspirationPoints end
    if r.phase == "result" or r.phase == "reroll" then
        r.result = p.FinalResult
        r.success = p.Success
    end
    if type(p.MinBonusValue) == "number" then
        if p.MinBonusValue == p.MaxBonusValue then r.bonus = p.MinBonusValue
        else r.bonus = { p.MinBonusValue, p.MaxBonusValue } end
    end
    if r.modifiers and #r.modifiers == 0 then r.modifiers = nil end
    -- SkillOrAbility is not reflected on bg3le: read the panel's rendered
    -- texts. Layout (live, 2026-10-09): "<Speaker>: <chosen line>",
    -- "<Skill>", "<Ability> Check", "Total Bonus", ...
    if not r.skill then
        local texts = {}
        for _, it in ipairs(UI.content(w.el, { text_only = true, max = 3000 })) do
            if it.t and it.t ~= "[ForceUpdate]" then texts[#texts + 1] = it.t end
        end
        for i, t in ipairs(texts) do
            if t:match(" Check$") or t:match(" Saving Throw$") then
                r.check = t
                local prev = texts[i - 1]
                if prev and not prev:find(":") and not tonumber(prev) then r.skill = prev end
                break
            end
        end
        if texts[1] and texts[1]:find(":") then r.line = texts[1] end
        if not r.check then r.text = table.concat(texts, " | ") end
    end
    return r
end

-- The prompt's reactions as the rows show them, in display order: the ReactButtons ui.react's `which` numbers.
-- A25 (S088 06:16:18): react(which="Shield") was refused: the decision VMs gave no names (`options=[i=1
-- decision=Undecided]`, also S034) and they are not the display order (the popup sorts them by DecisionComparer).
-- A row reads `[level] Name [slot level]` (DataTemplates.xaml InterruptToggleBtnTemplate: Roman numerals, none at
-- level 1): Gale's Shield at levels I-VI showed `Shield | II | Shield | II | III | Shield | III …`.
local ROMAN = { I = 1, II = 2, III = 3, IV = 4, V = 5, VI = 6, VII = 7, VIII = 8, IX = 9 }
function S.reactionRows(el)
    local rows = {}
    local roots = UI.findAll(el, function(e) return UI.name(e) == "InterruptorRoot" end, { prune = true })
    if #roots == 0 then roots = { el } end
    for _, root in ipairs(roots) do
        local who = clean(prop(prop(try(function() return root.DataContext end), "Character"), "Name"))
        for _, b in ipairs(UI.findAll(root, function(e) return UI.name(e) == "ReactButton" end, { prune = true })) do
            local r = { i = #rows + 1, who = who }
            for _, t in ipairs(UI.findAll(b, function(e) return UI.typeName(e) == "TextBlock" end)) do
                local s = UI.text(t)
                if s and ROMAN[s] then
                    r.level = r.level or ROMAN[s]
                elseif s and not r.name and not s:match("^[%d/%s%+%-]+$") then
                    r.name = s
                end
            end
            if prop(b, "IsChecked") == true then r.chosen = true end
            rows[#rows + 1] = r
        end
    end
    return rows
end

-- Reaction prompt: Data.InterruptEvents[1] with its interruptors and the
-- reactions each can use.
function S.reaction(wmap)
    local w = wmap.ReactionDecisionPopup
    if not w then return nil end
    local dc = try(function() return w.el.DataContext end)
    local events = UI.path(dc, "Data.InterruptEvents")
    if UI.count(events) == 0 then return nil end
    local ev = try(events.At, events, 1)
    local out = { prompt = UI.allText(w.el, " | ", 3000), options = S.reactionRows(w.el) }
    if #out.options > 0 then
        if UI.count(events) > 1 then out.pending = UI.count(events) end
        return out
    end
    local i = 0
    for _, interruptor in UI.items(prop(ev, "Interruptors"), 8) do
        local ch = props(prop(interruptor, "Character"))
        for _, dec in UI.items(prop(interruptor, "InterruptDecisions"), 8) do
            local dp = props(dec)
            local ip = props(dp.Interrupt)
            i = i + 1
            out.options[#out.options + 1] = {
                i = i,
                who = clean(ch.Name),
                name = clean(ip.Name) or clean(ip.DisplayName),
                decision = dp.Decision and tostring(dp.Decision) or nil,
            }
        end
    end
    if #out.options == 0 then out.options = nil end
    if UI.count(events) > 1 then out.pending = UI.count(events) end
    return out
end

-- Generic popup (message box, split item, save name): title/text/buttons as
-- rendered.
function S.popup(el)
    local items = UI.content(el, { max = 4000 })
    local texts, buttons = {}, {}
    for _, it in ipairs(items) do
        if it.t then texts[#texts + 1] = it.t
        elseif it.b and it.b ~= "" then buttons[#buttons + 1] = it.b end
    end
    local p = {}
    if #texts > 0 then
        p.title = texts[1]
        if #texts > 1 then p.text = table.concat(texts, " ", 2) end
    end
    if #buttons > 0 then p.buttons = buttons end
    return p
end

-- An open book/note/letter: its text is not in TextBlocks (S.popup only saw the page number "1"); the
-- ls.LSBookPage elements carry the whole text in their Text property (live 13:4x, Ghustil Stornugoss'
-- Research Notes). Named reads only (no GetAllProperties on live elements).
function S.book(wmap)
    local w = wmap.Book and wmap.Book.el
    if not w then return nil end
    for _, nm in ipairs({ "PART_LeftPage", "PART_RightPage" }) do
        local e = UI.byName(w, nm, { hidden = true })
        local t = e and UI.clean(UI.get(e, "Text"))
        if t then return { text = t } end
    end
    return nil
end

function S.tutorial(wmap)
    local w = wmap.ModalTutorial or wmap.TutorialPopup
    if not w then return nil end
    return S.popup(w.el)
end

-- Open loot container. bg3le exposes no item names on ls.VMItem, so this
-- reports what the grid shows per slot (kind, rarity, stack size) plus the
-- panel's rendered text; names come from the server (state.inventory).
-- Slot index i is what ui.loot{slots=[i]} takes.
function S.container(wmap)
    local w = wmap.ContainerWidget
    if not w then return nil end
    local dc = try(function() return w.el.DataContext end)
    local out = { slots = {} }
    local i = 0
    for _, cont in UI.items(UI.path(dc, "CurrentPlayer.ContainerInventoryList"), 4) do
        for _, slot in UI.items(UI.path(cont, "LootInventory.Slots"), 120) do
            local obj = props(slot).Object
            i = i + 1
            if obj then
                local op = props(obj)
                local it = { i = i, kind = op.ItemType, rarity = op.Rarity ~= "Common" and op.Rarity or nil }
                if type(op.Count) == "number" and op.Count > 1 then it.n = op.Count end
                if op.IsStolen then it.stolen = true end
                if type(op.Gold) == "number" and op.Gold > 0 and op.ItemType ~= "Container" then it.gold = op.Gold end
                out.slots[#out.slots + 1] = it
            end
        end
    end
    if #out.slots == 0 then out.slots = nil end
    out.text = UI.allText(w.el, " | ", 3000)
    return out
end

-- Toasts and side notifications currently rendered.
function S.notifications(wmap)
    local w = wmap.NotificationWidget
    if not w or not UI.isVisible(w.el) then return nil end
    local out = {}
    for _, it in ipairs(UI.content(w.el, { text_only = true, max = 1500 })) do
        if it.t then out[#out + 1] = it.t end
    end
    if #out == 0 then return nil end
    return out
end

-- Region line under the minimap ("Moonrise Towers, First Floor").
function S.region()
    local e = UI.cachedByName("Minimap", "LocationHolder")
    return e and UI.allText(e) or nil
end

-- The story turn counter under the minimap (Minimap.xaml ContentPresenter "StoryTimer", bound to
-- PlayerCharacterProperties.StoryTimer; DataTemplates.xaml StoryTimerStyle draws its Description, the
-- "N Turns Remaining." line and a ring with the number, TurnAmountLabel). S091 08:19-08:33: the Netherbrain's psyche
-- ("The brain is resisting the Chains of Karsus! Defeat it before it breaks free!", 4 turns) was only on
-- screenshots and try 4 ran out of turns with the brain at 28 HP. {text, turns_left} or nil when none is shown.
function S.storyTimer()
    local e = UI.cachedByName("Minimap", "StoryTimer")
    if not (e and UI.isVisible(e)) then return nil end
    local parts, left = {}, nil
    for _, t in ipairs(UI.findAll(e, function(x) return UI.typeName(x) == "TextBlock" end, { max = 600 })) do
        local s = UI.text(t)
        if s and UI.name(t) == "TurnAmountLabel" then left = tonumber(s)
        elseif s then parts[#parts + 1] = s end
    end
    if #parts == 0 and not left then return nil end
    return { text = #parts > 0 and table.concat(parts, " ") or nil, turns_left = left }
end

-- Party wipe: BG3 shows a message box whose buttons are only load/main
-- menu actions. Heuristic (untested live): popup with a load button and no
-- cancel/back/resume.
function S.isGameOver(popup)
    if not popup or not popup.buttons then return false end
    local load, escape = false, false
    for _, b in ipairs(popup.buttons) do
        local l = b:lower()
        if l:find("load") then load = true end
        if l:find("cancel") or l:find("resume") or l:find("back") or l:find("no") == 1 then escape = true end
    end
    return load and not escape and (popup.title or ""):lower():find("save") == nil
end

------------------------------------------------------------------------
-- ui.state
------------------------------------------------------------------------

-- The credits page: ending = true for the end credits after the final cinematic (Credits.xaml: the widget's
-- Metadata is "Ending" and DCCredits.IsEndGameCredits turns its background black), false for the main menu's Credits
-- button, nil when neither reads. What the player sees either way; the runner ends the run on the end credits.
function S.credits(wmap)
    local w = wmap.Credits
    local el = w and w.el
    local dc = el and try(function() return el.DataContext end)
    local flag = dc and prop(dc, "IsEndGameCredits")
    local meta = el and (UI.get(el, "Metadata") or prop(el, "Metadata"))
    local ending = nil
    if flag ~= nil then ending = flag == true end
    if meta ~= nil then ending = ending or tostring(meta) == "Ending" end
    return { ending = ending, metadata = meta ~= nil and tostring(meta) or nil }
end

function S.state(args)
    local t0 = RPC.now_ms()
    local res = {}
    local gs = tostring(try(Ext.Utils.GetGameState) or "?")
    local wmap = UI.widgetMap()
    for page, dcType in pairs(S.PAGE_DC) do
        if not wmap[page] then
            local found
            for name, w in pairs(wmap) do
                if w.dc == dcType then found = name break end
            end
            if found then wmap[page], wmap[found] = wmap[found], nil end
        end
    end
    -- Our own background save (UIActions saveDriver), < 15 s old: its menus
    -- and the game's ~1 s Save state show as the game underneath (below).
    local SA = BG3AI.UIActions
    local ps = SA and SA.pendingSave
    local ourSave = ps ~= nil and RPC.now_ms() - (ps.started or 0) < 15000
    if next(wmap) == nil
        or (gs ~= "Running" and gs ~= "Paused" and gs ~= "Menu" and not (gs == "Save" and ourSave)) then
        res.screen = (gs == "Menu") and "main_menu" or "loading"
        -- A movie over the loading screen (the New Game intro): see AutoSkip.
        local K = BG3AI.AutoSkip
        if res.screen == "loading" and K and K.probeMovie and try(K.probeMovie) then res.screen = "movie" end
        res.game_state = gs
        res.blocking = true
        res.ms = RPC.now_ms() - t0
        return res
    end

    -- Bink movie: only movie-safe widgets, nothing else (see AutoSkip).
    do
        local only = true
        for name in pairs(wmap) do
            if not (name == "AlwaysOnTopOverlay" or name == "ScreenFade" or name == "CrossplayOverlay"
                or name == "ModDownloadingWidget") then only = false break end
        end
        if only and gs == "Running" then
            res.screen = "movie"
            res.blocking = true
            res.ms = RPC.now_ms() - t0
            return res
        end
    end

    for _, pair in ipairs(S.SCREENS) do
        local w = wmap[pair[1]]
        if w and UI.isVisible(w.el) then
            res.screen = pair[2]
            res.page = pair[1]
            break
        end
    end
    res.screen = res.screen or "game"

    -- Our own background save drives these menus for ~1 s (UIActions
    -- saveDriver), then the game saves for ~1 s: report the game underneath, so a running queue's watcher
    -- doesn't take the save name box for a popup and stop the walk.
    if ourSave and (res.screen == "save_load" or res.screen == "game_menu"
        or res.page == "SaveGamePopup" or res.page == "Dialog_box") then
        res.saving = { page = res.page }
        res.screen, res.page = "game", nil
    end
    if ourSave and gs == "Save" then res.saving = res.saving or { state = "Save" } end

    -- Panels open besides the HUD.
    local panels = {}
    for name, w in pairs(wmap) do
        if not S.HUD[name] and name ~= res.page and UI.isVisible(w.el) then panels[#panels + 1] = name end
    end
    table.sort(panels)
    if #panels > 0 then res.panels_open = panels end

    local scr = res.screen
    if scr == "dialogue" then
        res.dialogue = S.dialogue(wmap)
    elseif scr == "roll" then
        res.roll = S.roll(wmap)
    elseif scr == "reaction" then
        res.reaction = S.reaction(wmap)
    elseif scr == "popup" then
        res.popup = S.popup(wmap[res.page].el)
        if S.isGameOver(res.popup) then res.screen = "game_over" end
        -- 2+ buttons = a decision the agent must make (ui.press); otherwise
        -- ui.dismiss may close it.
        if res.popup.buttons and #res.popup.buttons >= 2 then res.popup.decision = true
        else res.popup.dismissable = true end
        if res.page == "SplitItem" then res.popup.split = true end  -- how many of a stack: confirm(amount=N)
    elseif scr == "tutorial" then
        res.tutorial = S.tutorial(wmap)
        if res.tutorial then res.tutorial.dismissable = true end
    elseif scr == "container" then
        res.container = S.container(wmap)
    elseif scr == "credits" then
        res.credits = S.credits(wmap)
    elseif scr == "main_menu" then
        -- The menu's buttons only: its texts are the Larian account corner panel ("Log In", "Create account?") and
        -- the copyright/version lines, which read as a popup titled "Log In" though nothing asks anything (live L6).
        res.menu = { buttons = S.popup(wmap[res.page].el).buttons }
    elseif scr == "game_menu" or scr == "save_load" or scr == "book"
        or scr == "reward" or scr == "options" then
        res.popup = S.popup(wmap[res.page].el)
        local b = scr == "book" and S.book(wmap)
        if b then res.popup.title, res.popup.text = nil, b.text end -- the popup's "title" was the page number
    end

    -- A dialogue can sit under a roll or popup: keep its context.
    if scr ~= "dialogue" and wmap.Dialogue and UI.isVisible(wmap.Dialogue.el) and args.full then
        res.dialogue = S.dialogue(wmap)
    end

    if scr ~= "main_menu" then
        res.region = S.region()
        local oh = wmap.OverheadInfo and try(function() return wmap.OverheadInfo.el.DataContext end)
        if oh then
            local sub = clean(prop(oh, "CurrentSubtitle"))
            if sub then res.subtitle = { speaker = clean(prop(oh, "CurrentSpeaker")), text = sub } end
        end
        res.notifications = S.notifications(wmap)
    end
    if S.BLOCKING[res.screen] then res.blocking = true end
    local A = BG3AI.UIActions
    if A and A.gates and (A.SAVE_ENABLED or A.LOAD_ENABLED or args.full) then res.gates = A.gates() end
    local K = BG3AI.AutoSkip
    if K and K.view then
        local v = K.view()
        if not v.on or v.skipped > 0 or args.full then res.autoskip = { on = v.on, skipped = v.skipped } end
    end
    res.ms = RPC.now_ms() - t0
    return res
end

RPC.register("ui.state", function(args) return S.state(args or {}) end)
RPC.register("ui.story_timer", function() return S.storyTimer() or { none = true } end)

-- ui.answer_list -> {over, first, total, offset, scrollable} of the open dialogue's answer list: the bridge
-- finds the list under the cursor (over) and wheels it to read the rows below the fold (a player scrolls).
RPC.register("ui.answer_list", function()
    local w = UI.widgetMap().Dialogue
    local list = w and UI.isVisible(w.el) and UI.byName(w.el, "answerList")
    if not list then return { open = false } end
    local items = UI.findAll(list, function(e)
        local ty = UI.typeName(e)
        return ty == "ls.LSListBoxItem" or ty == "ListBoxItem"
    end, { prune = true })
    local out = { open = true, over = UI.get(list, "IsMouseOver") == true, rows = #items }
    local win = #items > 0 and S.listWindow(list, items)
    if win then
        out.first, out.total, out.offset, out.scrollable = win.first, win.total, win.offset, win.scrollable
    end
    return out
end)

return S
