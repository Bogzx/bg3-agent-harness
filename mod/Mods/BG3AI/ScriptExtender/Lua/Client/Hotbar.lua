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
--       Also `over_ui` (CurrentPlayer.UIData.Cursor.IsOverUI: the cursor is on any HUD element, verified live) and
--       `hover` = the IsMouseOver chain under the HotBar widget (last names, deepest last) while it has one, so a
--       miss says where the cursor was. `cache=true` (the bridge's cursor search) reuses the deck/button walk for
--       CACHE_MS (the slots don't change while the cursor sweeps; window #2 spent 4 s on 39 full walks).
--   ui.cast_state {char?, map?}
--       -> {armed, spell?, spell_name?, char?, remaining?, can_force, counter, targeting?, preview?, upcast_open?,
--           cast?={spell, phase, picked, index, previewing}}
--       What the HUD shows about the spell in hand: armed (a spell task is active), the cursor's target counter
--       (TargetValues.Remaining, the "N" next to the cursor), whether the tick button can finish early, and
--       the client cast entity's own state (targets picked so far, cast phase) when it has one. No char: the HUD's
--       selected character. spell: the active cell's VM id, else the cast entity's, else the cell's SlotIndex in the
--       hotbar map (H.activeSpell; map = the server's hotbar.map shape); spell_name its shown name; char whose.
--   ui.cast_confirm {}
--       -> {done=true} | error. The multi-target tick button (ForceCastButton, ForceCastCommand): finish with
--       fewer targets than the spell's max. Only while a spell is armed and the button is enabled.
--   ui.hotbar_upcast {level?}
--       -> {done=true, level, index, levels, open} | error. The upcast level picker (act A7): pick `level`, else the
--       lowest level the caster can cast at now, by the picker button's own command (see H.upcast).
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
-- LIVE 2026-10-16 19:49 (window #2): ids from the entity map and the cell (CommonHotBar #10, 88x88) read fine, but
--   39 cursor probes along y=0.95 (x 0.5..0.88) never saw `at`/`over`/over_hud. Unknown which: wrong band, a hidden
--   duplicate "HotBar" widget read in place of the shown one (UI.widget takes the first), or IsMouseOver not set on
--   the cell buttons. Now: the shown HotBar widget is preferred, a hovered ListBoxItem of a HotbarItemsList also
--   names its cell, and `over_ui`/`hover` report what the cursor was on.
-- LIVE 2026-10-16 18:32 (window #1, S027-start, se.log PROBE hotbar): G1, G2 and G4 broke.
--   G2: ls.VMCharacterAction has no readable PrototypeID on bg3le (nil on all 65 slots). The spell id now comes
--       from the engine's hotbar component (Shared/HotbarMap.lua: Element.Slot = the slot VM's SlotIndex), first the
--       client entity's, else a map the bridge passes (args.map, from the server op hotbar.map). `ids` says which.
--   G1: a slot button's DataContext reads nil. Buttons are now matched by position too: the n-th button of a deck's
--       HotbarItemsList ListBox is the n-th slot of that deck (ListBox -> deck by ItemsSource, else by slot count in
--       deck order).
--   G4: ActiveTask is a gui::VMActiveTaskInfo with nothing in hand (PreviewType 4294967295, no TargetValues, no
--       CurrentSpellTask, no cast entity). armed = one of those says a spell is in hand (H.armedOf).

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

-- The shown "HotBar" page: a visible one beats a hidden duplicate (UI.widget takes the first in ContentRoot).
local function hotbarWidget()
    if UI.widgets then
        local first
        for _, w in ipairs(UI.widgets()) do
            if w.name == "HotBar" then
                if UI.isVisible(w.el) then return w.el end
                first = first or w.el
            end
        end
        if first then return first end
    end
    return UI.widget("HotBar")
end
H.widget = hotbarWidget

-- A named element under the shown HotBar widget (UI.cachedByName looks under the first "HotBar", maybe a hidden one).
local named = {}
local function byNameIn(w, name)
    if w == nil then return nil end
    local key = tostring(w) .. "/" .. name
    local e = named[key]
    if e ~= nil and UI.name(e) == name then return e end
    e = UI.findAll(w, function(x) return UI.name(x) == name end, { hidden = true, prune = true, max = H.WALK_MAX })[1]
    named[key] = e
    return e
end

-- Element identity across reads: the same proxy, else the same native address in its tostring.
local function same(a, b) return a == b or (a ~= nil and b ~= nil and tostring(a) == tostring(b)) end

local function hotbarDC()
    local w = hotbarWidget()
    return w and UI.get(w, "DataContext") or nil
end

local function hotbarMap() return BG3AI.HotbarMap end

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
-- `map` (optional, Shared/HotbarMap.lua shape { [HotBarType] = { [tostring(SlotIndex)] = spell } }) fills the ids the
-- slot VMs don't expose. Each slot record's `src` = "vm" | "map" | nil (no id).
function H.decks(dc, map)
    local sel = UI.path(dc, "CurrentPlayer.SelectedCharacter")
    local bars = UI.path(sel, "PlayerCharacterProperties.KeyboardHotBars")
    local out = {}
    for order, bar in UI.items(bars, 16) do
        if bar ~= nil then
            local d = { type = str(UI.get(bar, "HotBarType")) or ("deck" .. order), vm = bar, order = order,
                        cols = num(UI.get(bar, "CellColumns")), rows = num(UI.get(bar, "NumberOfBars")), slots = {} }
            local SL = UI.get(bar, "SlotList")
            d.list = SL
            local dm = type(map) == "table" and map[d.type] or nil
            for i, slot in UI.items(SL, 400) do
                local content = UI.get(slot, "Content")
                local si = num(UI.get(slot, "SlotIndex"))
                local sp, src = spellOf(content), nil
                if sp then src = "vm"
                elseif type(dm) == "table" and si ~= nil then
                    sp = dm[tostring(si)] or dm[si]
                    if sp then src = "map" end
                end
                d.slots[#d.slots + 1] = { vm = slot, index = i, slot = si, content = content,
                    spell = sp, src = src, can_use = UI.get(slot, "CanUse") ~= false,
                    container = UI.get(content, "IsContainer") == true or nil }
            end
            out[#out + 1] = d
        end
    end
    return out
end

-- Which source named the spells: "vm" (any slot VM had an id), else "map", else "none".
function H.idSource(decks)
    local seen = {}
    for _, d in ipairs(decks) do
        for _, s in ipairs(d.slots) do if s.src then seen[s.src] = true end end
    end
    return seen.vm and "vm" or seen.map and "map" or "none"
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
    local byVM, bySig, byContent = {}, {}, {}
    for _, d in ipairs(decks) do
        for _, s in ipairs(d.slots) do
            byVM[tostring(s.vm)] = { d, s }
            if s.content ~= nil then byContent[tostring(s.content)] = { d, s } end
            bySig[tostring(s.spell) .. "#" .. tostring(s.slot) .. "#" .. d.type] = { d, s }
        end
    end
    local out, lists = {}, {}
    if widget == nil then return out, lists end
    local function isButton(e) return H.SLOT_BUTTONS[UI.name(e) or ""] == true end
    -- by position (LIVE G1: DataContext reads nil): each deck's ListBox in tree order, its buttons in item order
    local byPos = {}
    local byList, used, nextDeck = {}, {}, 1
    for di, d in ipairs(decks) do if d.list ~= nil then byList[tostring(d.list)] = di end end
    for _, lb in ipairs(UI.findAll(widget, function(e) return UI.name(e) == "HotbarItemsList" end,
                                   { prune = true, max = H.WALK_MAX })) do
        local btns = UI.findAll(lb, isButton, { prune = true, hidden = true, max = 2000 })
        local src = UI.get(lb, "ItemsSource")
        local di = src ~= nil and byList[tostring(src)] or nil
        if not di then
            for j = nextDeck, #decks do
                if not used[j] and #decks[j].slots == #btns and #btns > 0 then di = j break end
            end
        end
        if di and not used[di] and #decks[di].slots == #btns then
            used[di], nextDeck = true, di + 1
            for k, e in ipairs(btns) do byPos[e] = { decks[di], decks[di].slots[k] } end
            lists[#lists + 1] = { lb = lb, deck = decks[di], btns = btns }
        end
    end
    for _, e in ipairs(UI.findAll(widget, isButton, { prune = true, max = H.WALK_MAX })) do
        local dc = UI.get(e, "DataContext")
        local m = dc ~= nil and byVM[tostring(dc)] or nil
        if not m then
            local c = UI.get(e, "Content")
            m = c ~= nil and byContent[tostring(c)] or nil
        end
        if not m then m = byPos[e] end
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
    return out, lists
end

-- The IsMouseOver chain under `el`: from el down through the hovered child at each level (a few reads per level).
-- Returns the elements, top first.
H.HOVER_DEPTH = 60
function H.hoverChain(el)
    local chain = {}
    if el == nil or UI.childCount == nil then return chain end
    local e = el
    for _ = 1, H.HOVER_DEPTH do
        local nxt
        for i = 1, UI.childCount(e) do
            local c = UI.child(e, i)
            if c ~= nil and UI.get(c, "IsMouseOver") == true then nxt = c break end
        end
        if not nxt then break end
        chain[#chain + 1] = nxt
        e = nxt
    end
    return chain
end

-- The cell a hover chain is on, when the chain passes a deck's HotbarItemsList but no slot button reads
-- IsMouseOver: the deepest hovered element below the list that holds exactly one slot button names that button's
-- cell (type-agnostic: ListBoxItem, a border, the button's own template). nil when the chain names none.
function H.cellFromChain(chain, lists)
    local function isButton(e) return H.SLOT_BUTTONS[UI.name(e) or ""] == true end
    for i, e in ipairs(chain) do
        if UI.name(e) == "HotbarItemsList" then
            local L
            for _, l in ipairs(lists) do if same(l.lb, e) then L = l break end end
            if not L then return nil end
            for j = #chain, i + 1, -1 do
                local under = isButton(chain[j]) and { chain[j] }
                    or UI.findAll(chain[j], isButton, { prune = true, hidden = true, max = 400 })
                if #under == 1 then
                    for k, b in ipairs(L.btns) do
                        if same(b, under[1]) then return L.deck, L.deck.slots[k] end
                    end
                    return nil
                end
                if #under > 1 then return nil end
            end
            return nil
        end
    end
    return nil
end

local function chainNames(chain, n)
    local out = {}
    for i = math.max(1, #chain - (n or 6) + 1), #chain do
        local e = chain[i]
        out[#out + 1] = UI.name(e) or (UI.typeName and UI.typeName(e)) or "?"
    end
    return out
end

H.CACHE_MS = 3000
H._cache = nil
local function nowMs()
    local ok, t = pcall(function() return Ext.Utils.MonotonicTime() end)
    return ok and tonumber(t) or nil
end

local function selectedIs(char)
    if not char then return nil end
    local e = try(Ext.Entity.Get, char)
    if not e then return nil end
    -- ClientControl marks the selected character on the client (Progression.characterVM, verified live)
    local ok, cc = pcall(function() return e.ClientControl end)
    return ok and cc ~= nil
end

-- The HUD's selected character (UISettings.selected: the ClientControl holder the HUD shows), nil when unknown.
local function hudSelected()
    local S = BG3AI.UISettings
    return S and S.selected and (S.selected()) or nil
end

-- The client entity's hotbar map: the caster's, else the selected character's (ClientControl).
function H.clientMap(char)
    local HM = hotbarMap()
    if not HM then return nil, "HotbarMap not loaded" end
    char = char or hudSelected()
    if char then return HM.forChar(char) end
    for _, e in ipairs(try(Ext.Entity.GetAllEntitiesWithComponent, "ClientControl") or {}) do
        local hc = try(function() return e.HotbarContainer end)
        if hc ~= nil then
            local map, info = HM.fromComponent(hc)
            if info.bars > 0 then return map, info end
        end
    end
    return nil, "no selected character with a HotbarContainer"
end

local function cellRef(b)
    return { deck = b.deck.type, index = b.rec.index, slot = b.rec.slot, spell = b.rec.spell, order = b.deck.order,
             cols = b.deck.cols, rows = b.deck.rows }
end

function H.slot(args)
    local w = hotbarWidget()
    local dc = w and UI.get(w, "DataContext") or nil
    if dc == nil then error("the hotbar is not on screen") end
    local want = args.spell
    if not want or want == "" then error("spell is required") end
    local out = { selected = selectedIs(args.char) }
    local t, c = nowMs(), H._cache
    local hit = args.cache and t and c and c.w == tostring(w) and c.char == tostring(args.char)
                and t - c.t < H.CACHE_MS
    local decks, buttons, lists
    if hit then
        decks, buttons, lists = c.decks, c.buttons, c.lists
        out.ids, out.ids_from, out.map_why, out.cached = c.ids, c.ids_from, c.map_why, true
    else
        decks = H.decks(dc)
        if H.idSource(decks) == "none" then
            -- LIVE G2: the VMs carry no ids: the engine's hotbar component (client entity), else the bridge's map
            local map, why
            if type(args.map) == "table" then map, out.ids_from = args.map, "arg"
            else
                map, why = H.clientMap(args.char)
                out.ids_from, out.map_why = map and "entity" or nil, (not map) and why or nil
            end
            if map then decks = H.decks(dc, map) end
        end
        out.ids = H.idSource(decks)
        buttons, lists = H.buttons(w, decks)
        if args.cache and t then
            H._cache = { w = tostring(w), char = tostring(args.char), t = t, decks = decks, buttons = buttons,
                         lists = lists, ids = out.ids, ids_from = out.ids_from, map_why = out.map_why }
        end
    end
    out.shown_deck = str(UI.path(dc, "CurrentShownDeck.HotBarType"))
    local deck, rec, upcast = H.findSlot(decks, want)
    for _, b in ipairs(buttons) do
        if UI.get(b.el, "IsMouseOver") == true then out.at = cellRef(b) break end
    end
    out.over_ui = UI.path(dc, "CurrentPlayer.UIData.Cursor.IsOverUI") == true
    local conc = byNameIn(w, "Concertina")
    out.over_hud = conc ~= nil and UI.get(conc, "IsMouseOver") == true
    if out.over_ui or out.over_hud then
        local chain = H.hoverChain(w)
        if #chain > 0 then
            out.hover = chainNames(chain)
            if not out.at then
                local d, r = H.cellFromChain(chain, lists)
                if d then
                    out.at = cellRef({ deck = d, rec = r })
                    out.at.via = "item"
                end
            end
        end
    end
    if not deck then
        out.found = false
        local n = 0
        for _, d in ipairs(decks) do n = n + #d.slots end
        out.why = #decks == 0 and "no hotbar decks for the selected character" or
            ("not on the hotbar (" .. n .. " slots in " .. #decks .. " decks"
             .. (out.ids == "none" and ("; no spell ids: " .. tostring(out.map_why or "the map has none")) or "") .. ")")
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
                or (out.at ~= nil and out.at.deck == deck.type and out.at.index == rec.index)
            out.cell = { w = num(UI.get(b.el, "ActualWidth")), h = num(UI.get(b.el, "ActualHeight")) }
            out.enabled = UI.get(b.el, "IsEnabled") ~= false
            break
        end
    end
    if w and args.tabs then   -- only while looking for a deck tab: a missing name walks the whole widget
        for _, name in pairs(H.DECK_BUTTONS) do
            local tab = byNameIn(w, name)
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

-- The spell id in the active hotbar cell when its VM carries none (LIVE G2): S091 11/11 `IN HAND: a spell`, the
-- slot VM's PrototypeID reads nil and the bridge passes no char (no cast entity). The cell's SlotIndex in the
-- character's hotbar map (args.map from the bridge's server read, else the client entity's), on the deck whose slot list holds the active slot (same VM, or a
-- slot with that SlotIndex marked IsActive: GUESS H1), else the shown deck (a clicked cell is on the deck on screen).
function H.activeSpell(dc, char, map)
    local active = UI.get(dc, "CurrentActiveSlot")
    local si = num(UI.get(active, "SlotIndex"))
    if si == nil then return nil end
    if type(map) ~= "table" then map = H.clientMap(char) end
    if type(map) ~= "table" then return nil end
    local deck
    for _, d in ipairs(H.decks(dc)) do
        for _, s in ipairs(d.slots) do
            if same(s.vm, active) or (s.slot == si and UI.get(s.vm, "IsActive") == true) then deck = d.type break end
        end
        if deck then break end
    end
    local dm = map[deck or str(UI.path(dc, "CurrentShownDeck.HotBarType")) or ""]
    return type(dm) == "table" and (dm[tostring(si)] or dm[si]) or nil
end

-- A spell id's shown name ("Projectile_MagicMissile" -> "Magic Missile"), nil when unknown.
local function spellName(id)
    local st = id and try(Ext.Stats.Get, id)
    local U = BG3AI.Util
    return st and U and U.loca and U.loca(try(function() return st.DisplayName end)) or nil
end

function H.castState(args)
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local char = args.char or hudSelected()
    local task = UI.path(dc, "CurrentPlayer.UIData.ActiveTask")
    local out = { task = task ~= nil }
    local tv = UI.get(task, "TargetValues") or UI.get(task, "HPTargetValues")
    out.counter = tv ~= nil
    out.remaining = num(UI.get(tv, "Remaining"))
    out.can_force = UI.get(task, "CanForceCast") == true
    out.preview = str(UI.get(task, "PreviewType"))
    local st = UI.path(dc, "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask")
    out.spell_task = st ~= nil
    out.targeting = str(UI.get(st, "TargetingType"))
    out.upcast_open = UI.get(dc, "IsSelectingUpcastedSpell") == true or nil
    out.spell = spellOf(UI.path(dc, "CurrentActiveSlot.Content"))
    local c = castEntity(char)
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
    out.armed, out.armed_by = H.armedOf(out)
    if not out.spell and (out.armed or out.upcast_open) then out.spell = H.activeSpell(dc, char, args.map) end
    out.spell_name = spellName(out.spell)
    out.char = char
    return out
end

-- LIVE G4: ActiveTask exists with nothing in hand (PreviewType 4294967295 = none). A spell is in hand when the
-- cursor counter shows, the selected character has a CurrentSpellTask, a client cast entity exists, or the task
-- previews something.
H.NO_PREVIEW = { ["4294967295"] = true, ["-1"] = true, ["None"] = true, ["0"] = true }
function H.armedOf(st)
    if not st.task then return false, nil end
    if st.counter then return true, "counter" end
    if st.spell_task then return true, "spell_task" end
    if st.cast then return true, "cast" end
    if st.preview and not H.NO_PREVIEW[st.preview] then return true, "preview" end
    return false, nil
end

function H.confirm()
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local task = UI.path(dc, "CurrentPlayer.UIData.ActiveTask")
    if task == nil or not H.castState({}).armed then error("no spell in hand") end
    if UI.get(task, "CanForceCast") ~= true then error("the tick button is disabled (no target picked yet?)") end
    local ok, err = UI.command(dc, "ForceCastCommand")
    if not ok then error("ForceCastCommand: " .. tostring(err)) end
    return { done = true }
end

function H.cancel()
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    if UI.path(dc, "CurrentPlayer.UIData.ActiveTask") == nil then return { done = true, was_armed = false } end
    -- the task object stays with nothing in hand (LIVE G4): cancel anyway (harmless), report what armedOf saw
    local armed = H.castState({}).armed
    local ok, err = UI.command(dc, "CancelTaskCommand")
    if not ok then error("CancelTaskCommand: " .. tostring(err)) end
    return { done = true, was_armed = armed }
end

-- The upcast level picker (act A7, 2026-10-17). HotBar.xaml "UpcastSection" (shown while IsSelectingUpcastedSpell):
-- ItemsControl ContainerItems, ItemsSource CurrentActiveSlot.Content.SpellUpcast; each level is an LSButton with
-- HotBarSlotStyle (Library/DataTemplates.xaml: Command = the widget's UseSlotCommand, CommandParameter = the
-- ls:VMUpcast item, IsEnabled = CanUse). A click on a level is exactly UseSlotCommand:Execute(item).
-- LIVE L4 (S065-end, Shadowheart's Dimension Door, ClassHotBar #26, IsContainer=true): SpellUpcast[5], #1-#3
-- IsFake=true CanUse=false, #4 IsFake=false CanUse=true. GUESS U1: entry i is slot level i when the entry has no
-- SpellSlotLevel of its own.
function H.upcast(args)
    local dc = hotbarDC()
    if dc == nil then error("the hotbar is not on screen") end
    local levels = {}
    for i, vm in UI.items(UI.path(dc, "CurrentActiveSlot.Content.SpellUpcast"), 12) do
        if vm ~= nil then
            levels[#levels + 1] = { vm = vm, index = i, level = num(UI.get(vm, "SpellSlotLevel")) or i,
                                    fake = UI.get(vm, "IsFake") == true, can = UI.get(vm, "CanUse") ~= false }
        end
    end
    if #levels == 0 then error("no upcast levels on the spell in hand (the picker is not open)") end
    local want = num(args.level)
    local pick
    for _, l in ipairs(levels) do
        if want and l.level == want then pick = l break end
        if not want and not l.fake and l.can then pick = l break end
    end
    if want and not pick then error("the picker has no level " .. want .. " (levels 1-" .. #levels .. ")") end
    if not pick then error("every level is greyed out: no spell slot left to cast it with") end
    if pick.fake or not pick.can then error("level " .. pick.level .. " is greyed out (no slot of that level left)") end
    local ok, err = UI.command(dc, "UseSlotCommand", pick.vm)
    if not ok then error("UseSlotCommand: " .. tostring(err)) end
    return { done = true, level = pick.level, index = pick.index, levels = #levels,
             open = UI.get(dc, "IsSelectingUpcastedSpell") == true }
end

RPC.register("ui.hotbar_slot", function(args) return H.slot(args or {}) end)
RPC.register("ui.hotbar_upcast", function(args) return H.upcast(args or {}) end)
RPC.register("ui.cast_state", function(args) return H.castState(args or {}) end)
RPC.register("ui.cast_confirm", function() return H.confirm() end)
RPC.register("ui.cast_cancel", function() return H.cancel() end)

return H
