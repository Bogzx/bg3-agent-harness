-- Hotbar reads for the player's own cast route: hotbar cell click -> one click per target -> confirm
-- (audit ACT-8, client half; owner: worker se). Osiris UseSpell takes 2 targets, so Bless x3, Magic Missile
-- darts and Scorching Ray / Eldritch Blast beams go through the hotbar as a player casts them. The bridge
-- (bg3ai/hotbar.py) moves the cursor and clicks; these ops only read what the HUD shows, plus the HUD's own
-- confirm (tick) button.
--
-- ops:
--   ui.hotbar_slot {spell, char?, tabs?}
--       -> {found, deck, index, slot, cols, rows, can_use, container?, upcast?, shown_deck, rendered, cell={w,h},
--           over, at?={deck,index,slot,spell,order,cols,rows}, over_hud, deck_button, deck_button_over?, selected?, why?}
--       The selected character's hotbar cell for a spell id. `index` is 1-based in the deck's SlotList,
--       `cols`/`rows` the deck grid (column-first: index-1 = col*rows + row). `over` = the cursor is on that
--       very cell (its IsMouseOver), `at` = the cell under the cursor now (any deck), `over_hud` = the cursor
--       is on the hotbar decks, `deck_button_over` (with tabs=true) the deck tab under the cursor. The bridge
--       finds the cell's screen spot with these (bg3le has no PointToScreen).
--   ui.cast_state {char?}
--       -> {armed, spell?, remaining?, can_force, counter, targeting?, preview?, upcast_open?,
--           cast?={spell, phase, picked, index, previewing}}
--       What the HUD shows about the spell in hand: armed (a spell task is active), the cursor's target counter
--       (TargetValues.Remaining, the "N" next to the cursor), whether the tick button can finish early, and
--       the client cast entity's own state (targets picked so far, cast phase) when it has one.
--   ui.cast_confirm {}
--       -> {done=true} | error. The multi-target tick button (ForceCastButton, ForceCastCommand): finish with
--       fewer targets than the spell's max. Only while a spell is armed and the button is enabled.
--   ui.cast_cancel {}
--       -> {done, was_armed}. Last resort after ESCAPE / right click: the HUD's CancelTaskCommand.
--
-- Source: HotBar.xaml and Library/DataTemplates.xaml from Game.pak (read 2026-10-16, tools/pak.py):
--   Concertina (x:Name) ItemsSource = CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars
--     (ls:VMHotBar: HotBarType, SlotList, CellColumns, NumberOfBars); each deck is a ListBox HotbarItemsList whose
--     items are ls:LSButton "contentContainer" (Style HotBarSlotStyle: Command = UseSlotCommand), DataContext the
--     slot VM (SlotIndex, IsActive, CanUse, Content = ls:VMCharacterAction with PrototypeID / IsContainer).
--     Panel: ls:LSGrid Columns=CellColumns Rows=NumberOfBars Orientation=Vertical.
--   Deck tabs: ListBoxItem HotbarDeckButton_{Common,Class,Items,Passives,Custom} -> SetCurrentShownDeckCommand.
--   Multi-target: Grid MultiTargetDashContainer (visible while ActiveTask.TargetValues is set) holding
--     LSButton ForceCastButton (IsEnabled = CurrentPlayer.UIData.ActiveTask.CanForceCast,
--     Command = ForceCastCommand) and a cursor text of TargetValues.Remaining + 1.
-- GUESSES (unverified live, marked GUESS below; acceptance se "hotbar widget tree (ACT-8)" checks them):
--   G1 a slot button's DataContext is the slot VM (the ListBoxItem's item);
--   G2 the spell id is Content.PrototypeID (the AutomationId binding "HotBarSlot_Spell_{PrototypeID}");
--   G3 LSGrid Orientation=Vertical fills column-first;
--   G4 CurrentPlayer.UIData.ActiveTask is nil while no spell is in hand;
--   G5 the client cast entity (SpellCastIsCasting.Cast -> ClientSpellCastState/Targeting) exists while targeting;
--   G6 the deck tab names map HotBarType -> HotbarDeckButton_* as in DECK_BUTTONS.

BG3AI = BG3AI or {}
local UI = BG3AI.UI
local RPC = BG3AI.RPC
local H = {}
BG3AI.Hotbar = H

H.SLOT_BUTTONS = { contentContainer = true, HotbarslotBtn = true }
H.SPELL_KEYS = { "PrototypeID", "Prototype", "SpellId" }      -- GUESS G2: first that reads
-- GUESS G6 (ItemHotBar -> "_Items" is the tab's own CommandParameter in HotBar.xaml; the other two are by name)
H.DECK_BUTTONS = { CommonHotBar = "HotbarDeckButton_Common", ClassHotBar = "HotbarDeckButton_Class",
                   ItemHotBar = "HotbarDeckButton_Items", PassivesHotBar = "HotbarDeckButton_Passives",
                   CustomHotBar = "HotbarDeckButton_Custom" }
H.WALK_MAX = 30000

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local function str(v)
    if v == nil then return nil end
    local s = tostring(v)
    if s == "" or s == "nil" then return nil end
    return s
end

local function num(v) return tonumber(v) end

local function hotbarDC() return UI.dc("HotBar") end

local function spellOf(content)
    if content == nil then return nil end
    for _, k in ipairs(H.SPELL_KEYS) do
        local v = str(UI.get(content, k))
        if v then return v end
    end
    return nil
end
H.spellOf = spellOf

-- "Projectile_MagicMissile_3" -> "Projectile_MagicMissile", 3 (an upcast variant's id); nil when no suffix.
function H.baseSpell(id)
    local base, lvl = tostring(id or ""):match("^(.-)_(%d)$")
    if base and base ~= "" then return base, tonumber(lvl) end
    return nil
end

-- The selected character's decks as data: { {type, vm, slots = { {vm, index, slot, spell, can_use, container} },
-- cols, rows, order} }. Reads view models only (no elements).
function H.decks(dc)
    local sel = UI.path(dc, "CurrentPlayer.SelectedCharacter")
    local bars = UI.path(sel, "PlayerCharacterProperties.KeyboardHotBars")
    local out = {}
    for order, bar in UI.items(bars, 16) do
        if bar ~= nil then
            local d = { type = str(UI.get(bar, "HotBarType")) or ("deck" .. order), vm = bar, order = order,
                        cols = num(UI.get(bar, "CellColumns")), rows = num(UI.get(bar, "NumberOfBars")), slots = {} }
            for i, slot in UI.items(UI.get(bar, "SlotList"), 400) do
                local content = UI.get(slot, "Content")
                d.slots[#d.slots + 1] = { vm = slot, index = i, slot = num(UI.get(slot, "SlotIndex")),
                    spell = spellOf(content), can_use = UI.get(slot, "CanUse") ~= false,
                    container = UI.get(content, "IsContainer") == true or nil }
            end
            out[#out + 1] = d
        end
    end
    return out
end

-- Find `want` in the decks: exact id (case-insensitive) first, then an upcast variant's base id (upcast = level).
function H.findSlot(decks, want)
    local w = tostring(want or ""):lower()
    local base, lvl = H.baseSpell(want)
    local hit
    for _, d in ipairs(decks) do
        for _, s in ipairs(d.slots) do
            local sp = (s.spell or ""):lower()
            if sp == w then return d, s, nil end
            if base and not hit and sp == base:lower() then hit = { d, s, lvl } end
        end
    end
    if hit then return hit[1], hit[2], hit[3] end
    return nil
end

-- Slot buttons in the HotBar widget, each matched to its slot record by its DataContext (GUESS G1):
-- { {el, deck, rec} }.
function H.buttons(widget, decks)
    local byVM, bySig = {}, {}
    for _, d in ipairs(decks) do
        for _, s in ipairs(d.slots) do
            byVM[tostring(s.vm)] = { d, s }
            bySig[tostring(s.spell) .. "#" .. tostring(s.slot) .. "#" .. d.type] = { d, s }
        end
    end
    local out = {}
    if widget == nil then return out end
    for _, e in ipairs(UI.findAll(widget, function(e) return H.SLOT_BUTTONS[UI.name(e) or ""] == true end,
                                  { prune = true, max = H.WALK_MAX })) do
        local dc = UI.get(e, "DataContext")
        local m = dc ~= nil and byVM[tostring(dc)] or nil
        if not m and dc ~= nil then
            -- a VM proxy whose tostring differs per read: match by spell + SlotIndex within one deck only
            local sp, si = spellOf(UI.get(dc, "Content")), num(UI.get(dc, "SlotIndex"))
            local hits = {}
            for _, d in ipairs(decks) do
                local r = bySig[tostring(sp) .. "#" .. tostring(si) .. "#" .. d.type]
                if r then hits[#hits + 1] = r end
            end
            if #hits == 1 then m = hits[1] end
        end
        if m then out[#out + 1] = { el = e, deck = m[1], rec = m[2] } end
    end
    return out
end

local function selectedIs(char)
    if not char then return nil end
    local e = try(Ext.Entity.Get, char)
    if not e then return nil end
    -- ClientControl marks the selected character on the client (Progression.characterVM, verified live)
    local ok, cc = pcall(function() return e.ClientControl end)
    return ok and cc ~= nil
end

local function cellRef(b)
    return { deck = b.deck.type, index = b.rec.index, slot = b.rec.slot, spell = b.rec.spell, order = b.deck.order,
             cols = b.deck.cols, rows = b.deck.rows }
end

function H.slot(args)
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local want = args.spell
    if not want or want == "" then error("spell is required") end
    local out = { selected = selectedIs(args.char) }
    local decks = H.decks(dc)
    out.shown_deck = str(UI.path(dc, "CurrentShownDeck.HotBarType"))
    local deck, rec, upcast = H.findSlot(decks, want)
    local w = UI.widget("HotBar")
    local buttons = H.buttons(w, decks)
    for _, b in ipairs(buttons) do
        if UI.get(b.el, "IsMouseOver") == true then out.at = cellRef(b) break end
    end
    local conc = w and UI.cachedByName("HotBar", "Concertina")
    out.over_hud = conc ~= nil and UI.get(conc, "IsMouseOver") == true
    if not deck then
        out.found = false
        local n = 0
        for _, d in ipairs(decks) do n = n + #d.slots end
        out.why = #decks == 0 and "no hotbar decks for the selected character" or
            ("not on the hotbar (" .. n .. " slots in " .. #decks .. " decks)")
        return out
    end
    out.found = true
    out.deck, out.index, out.slot = deck.type, rec.index, rec.slot
    out.cols, out.rows, out.order = deck.cols, deck.rows, deck.order
    out.can_use = rec.can_use
    out.container = rec.container
    out.upcast = upcast
    out.spell = rec.spell
    out.deck_button = H.DECK_BUTTONS[deck.type]
    out.rendered = false
    for _, b in ipairs(buttons) do
        if b.rec == rec then
            out.rendered = UI.isVisible(b.el) ~= false
            out.over = UI.get(b.el, "IsMouseOver") == true
            out.cell = { w = num(UI.get(b.el, "ActualWidth")), h = num(UI.get(b.el, "ActualHeight")) }
            out.enabled = UI.get(b.el, "IsEnabled") ~= false
            break
        end
    end
    if w and args.tabs then   -- only while looking for a deck tab: a missing name walks the whole widget
        for _, name in pairs(H.DECK_BUTTONS) do
            local tab = UI.cachedByName("HotBar", name)
            if tab and UI.get(tab, "IsMouseOver") == true then out.deck_button_over = name end
        end
    end
    return out
end

-- The client cast entity of `char` (GUESS G5): SpellCastIsCasting.Cast on the caster; else any entity with a
-- ClientSpellCastState whose Caster is it.
local function castEntity(char)
    local e = char and try(Ext.Entity.Get, char)
    if not e then return nil end
    local cast = try(function() return e.SpellCastIsCasting.Cast end)
    if cast then return cast end
    local list = try(Ext.Entity.GetAllEntitiesWithComponent, "ClientSpellCastState") or {}
    for _, c in ipairs(list) do
        local okc, caster = pcall(function() return c.ClientSpellCastState.Caster end)
        if okc and caster ~= nil and tostring(caster) == tostring(e) then return c end
    end
    return nil
end

function H.castState(args)
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local task = UI.path(dc, "CurrentPlayer.UIData.ActiveTask")
    local out = { armed = task ~= nil }                     -- GUESS G4
    local tv = UI.get(task, "TargetValues") or UI.get(task, "HPTargetValues")
    out.counter = tv ~= nil
    out.remaining = num(UI.get(tv, "Remaining"))
    out.can_force = UI.get(task, "CanForceCast") == true
    out.preview = str(UI.get(task, "PreviewType"))
    out.targeting = str(UI.path(dc, "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask.TargetingType"))
    out.upcast_open = UI.get(dc, "IsSelectingUpcastedSpell") == true or nil
    out.spell = spellOf(UI.path(dc, "CurrentActiveSlot.Content"))
    local c = castEntity(args.char)
    if c then
        local cs = {}
        pcall(function()
            local st = c.ClientSpellCastState
            cs.spell = str(st.Spell.Prototype)
            cs.phase = str(st.CastPhase)
        end)
        pcall(function()
            local tg = c.ClientSpellCastTargeting
            cs.picked = #tg.Targets
            cs.index = tg.CurrentTargetIndex
            cs.previewing = tg.IsPreviewing
        end)
        if next(cs) then out.cast = cs end
        out.spell = out.spell or cs.spell
    end
    return out
end

function H.confirm()
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local task = UI.path(dc, "CurrentPlayer.UIData.ActiveTask")
    if task == nil then error("no spell in hand") end
    if UI.get(task, "CanForceCast") ~= true then error("the tick button is disabled (no target picked yet?)") end
    local ok, err = UI.command(dc, "ForceCastCommand")
    if not ok then error("ForceCastCommand: " .. tostring(err)) end
    return { done = true }
end

function H.cancel()
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local armed = UI.path(dc, "CurrentPlayer.UIData.ActiveTask") ~= nil
    if not armed then return { done = true, was_armed = false } end
    local ok, err = UI.command(dc, "CancelTaskCommand")
    if not ok then error("CancelTaskCommand: " .. tostring(err)) end
    return { done = true, was_armed = true }
end

RPC.register("ui.hotbar_slot", function(args) return H.slot(args or {}) end)
RPC.register("ui.cast_state", function(args) return H.castState(args or {}) end)
RPC.register("ui.cast_confirm", function() return H.confirm() end)
RPC.register("ui.cast_cancel", function() return H.cancel() end)

return H
