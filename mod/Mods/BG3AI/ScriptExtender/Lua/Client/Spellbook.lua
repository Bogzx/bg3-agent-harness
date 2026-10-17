-- BG3AI spellbook (client context): prepare and unprepare spells the player's way, through the character panel's
-- spellbook page (A5, 2026-10-17). The bridge checks the change first (server act.prepare_plan: out of combat, a
-- class that prepares, spells in the book, the limit) and selects the character; this op opens the page, runs the
-- spell cells' own command, waits for each cell to flip, and closes the page again.
--
--   ui.prepare_spells {name?, add = [{id, name}], remove = [{id, name}], probe?, key?}
--     -> job; result { toggled = [{name, op, ok, why?}], before = {value, max}, after = {value, max}, book, shows,
--                      opened, closed } (probe=true: { probe = {...} }, read-only)
--
-- From the game's own XAML (Game.pak, read offline 2026-10-17):
--   Mods/MainUI/GUI/Pages/CharacterPanel.xaml: widget "CharacterPanel"; the spellbook is its page whose Metadata is
--     "ToggleSpells" (the ToggleSpells input event opens it, and closes it again: CloseTabBinding).
--   Public/Game/GUI/Library/CharacterSheetTemplates_k.xaml MultiClassSpellbookTemplate: the tabs are
--     CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.SpellBooks (ls:VMSpellBook: Class.MustPrepareSpells,
--     PreparedSpellsStat.Value/.Max, ActionGroups[].Actions, ManuallyPreparedActions). Every spell cell
--     (ActionGroupItemTemplate, PreparedSpellCellTemplate) is an LSButton whose Command is the widget DataContext's
--     ToggleCharacterAction with the cell's ls:VMCharacterAction as CommandParameter: a click on a cell is exactly
--     ToggleCharacterAction:Execute(cell). Activated = prepared, NeedsActivation = must be prepared, Name = shown name.
-- GUESSES (unverified live; acceptance act "spellbook probe (A5, read-only)" logs PROBE spellbook):
--   S1 K is the ToggleSpells key (the game's default binding; key= overrides);
--   S2 those VM props read on bg3le (the hotbar's VMCharacterAction had no readable PrototypeID: cells are matched
--      by Name, the spell's shown name, which the server sends);
--   S3 the panel's DataContext is the one with ToggleCharacterAction; the page shows the selected character.
-- LIVE L4 (2026-10-17, S065-end, Claude): S1 and S3 held; S2 broke: a cell VM has no Name and its ActionId reads
--   "Sentinel", so every cell was dropped ("0 cells"). Readable per cell: Activated, NeedsActivation (false = always
--   prepared), SpellSlotLevel, SpellSchool, IsConcentrationSpell, MainCost (Action / BonusAction / BothActions), Icon
--   (an ImageBrush), ShortDescription (a VMContextTransString). Books: 0 Base (item spells), 1 Tadpole, 2 Class.
--   A7: a cell is now matched (SB.match) by its Name if one ever reads, else by its icon name (GUESS S4: the brush's
--   ImageSource names the stats Icon), else by its look (level, school, concentration, cost) among the cells in the
--   state the server says the spell is in. The server sends each spell's look and `twins` (other book spells that
--   look the same in that state); with twins and no icon the page can't tell them apart: refused, nothing clicked.

BG3AI = BG3AI or {}
local UI = BG3AI.UI
local RPC = BG3AI.RPC
local SB = {}
BG3AI.Spellbook = SB

SB.KEY = "K"
SB.OPEN_MS = 3000      -- key press -> the page is up
SB.READY_MS = 2500     -- page up -> the selected character's book has cells
SB.FLIP_MS = 1500      -- ToggleCharacterAction -> the cell's Activated flips
SB.CLOSE_MS = 1500
SB.PROBE_CELLS = 4

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

local function low(s) return (str(s) or ""):lower() end

-- The CharacterPanel page, shown or not (a visible one beats a hidden duplicate).
function SB.panel()
    local first
    for _, w in ipairs(UI.widgets()) do
        if w.name == "CharacterPanel" then
            if UI.isVisible(w.el) then return w.el, true end
            first = first or w.el
        end
    end
    return first, false
end

-- Is the shown panel on its spellbook page? nil when Metadata doesn't read.
function SB.onSpells(w)
    local m = str(UI.get(w, "Metadata"))
    if m == nil then return nil end
    return m == "ToggleSpells"
end

-- The icon name a cell's brush shows ("Spell_Enchantment_Bless"), nil when nothing names it (GUESS S4: the
-- ImageBrush's ImageSource, or the source's Uri/name, ends in the stats Icon).
function SB.iconOf(vm)
    local brush = UI.get(vm, "Icon")
    if brush == nil then return nil end
    local src = UI.get(brush, "ImageSource")
    local cands = { src, UI.get(src, "UriSource"), UI.get(src, "Source"), UI.get(src, "TextureName"),
                    UI.get(src, "Name"), UI.get(brush, "Name") }
    for _, v in ipairs(cands) do
        local t = type(v) == "string" and v or nil
        t = t and (t:match("([^/\\:]+)$") or t):gsub("%.%w+$", "")
        if t and t ~= "" then return t end
    end
    return nil
end

-- The selected character's preparing books: { {vm, class, value, max, cells = { cell }, n} } where cell = {vm, name?,
-- icon?, activated, needs, level, school, conc, cost}.
function SB.books(dc)
    local sel = UI.path(dc, "CurrentPlayer.SelectedCharacter")
    local out = {}
    for _, book in UI.items(UI.path(sel, "PlayerCharacterProperties.SpellBooks"), 16) do
        local stat = UI.get(book, "PreparedSpellsStat")
        local max = tonumber(UI.get(stat, "Max"))
        local must = UI.path(book, "Class.MustPrepareSpells")
        if book ~= nil and (must == true or (max or 0) > 0) then
            local b = { vm = book, class = str(UI.path(book, "Class.ClassDisplayName")), value = tonumber(UI.get(stat, "Value")),
                        max = max, cells = {}, n = 0 }
            local function add(vm)
                if vm == nil then return end
                b.cells[#b.cells + 1] = { vm = vm, name = str(UI.get(vm, "Name")), icon = SB.iconOf(vm),
                    activated = UI.get(vm, "Activated") == true, needs = UI.get(vm, "NeedsActivation"),
                    level = tonumber(UI.get(vm, "SpellSlotLevel")), school = str(UI.get(vm, "SpellSchool")),
                    conc = UI.get(vm, "IsConcentrationSpell"), cost = str(UI.get(vm, "MainCost")) }
                b.n = b.n + 1
            end
            -- the level groups only: ManuallyPreparedActions repeats the prepared cells (they would count twice)
            for _, group in UI.items(UI.get(book, "ActionGroups"), 32) do
                for _, vm in UI.items(UI.get(group, "Actions"), 200) do add(vm) end
            end
            out[#out + 1] = b
        end
    end
    return out, sel
end

local function looks_like(c, spell)
    local l = spell.look or {}
    if c.needs == false then return false end   -- always prepared: not a toggle
    if spell.level ~= nil and c.level ~= tonumber(spell.level) then return false end
    if l.school ~= nil and low(c.school) ~= low(l.school) then return false end
    if l.conc ~= nil and (c.conc == true) ~= (l.conc == true) then return false end
    if l.cost ~= nil and low(c.cost) ~= low(l.cost) then return false end
    return true
end

-- The cell for a spell {id, name, level?, look = {icon, school, conc, cost}?, twins?} whose Activated is `state`
-- (nil: any): cell, book, why. By name, then icon, then look (only when the look is the spell's own: twins 0).
-- exact = name or icon only (a look match can't prove "already prepared": its look-alike may be the one that is).
function SB.match(books, spell, state, exact)
    local function ok_state(c) return state == nil or c.activated == state end
    for _, b in ipairs(books) do
        for _, c in ipairs(b.cells) do
            if c.name and ok_state(c) and (low(c.name) == low(spell.name) or low(c.name) == low(spell.id)) then
                return c, b
            end
        end
    end
    local icon = spell.look and low(spell.look.icon)
    if icon and icon ~= "" then
        local any_icon = false
        for _, b in ipairs(books) do
            for _, c in ipairs(b.cells) do
                if c.icon then any_icon = true end
                if c.icon and low(c.icon) == icon and ok_state(c) then return c, b end
            end
        end
        if any_icon then return nil, nil, "no cell shows " .. tostring(spell.name) .. "'s icon" end
    end
    if exact then return nil, nil, "no cell named " .. tostring(spell.name) end
    if spell.look == nil then return nil, nil, "no spell cell named " .. tostring(spell.name) .. " in the spellbook" end
    if (tonumber(spell.twins) or 0) > 0 then
        return nil, nil, string.format("the spellbook cells carry no names, and %d other spell(s) look the same as %s"
            .. " there (level, school, concentration, cost): can't tell its cell apart, nothing clicked",
            tonumber(spell.twins), tostring(spell.name))
    end
    local hits = {}
    for _, b in ipairs(books) do
        for _, c in ipairs(b.cells) do
            if ok_state(c) and looks_like(c, spell) then hits[#hits + 1] = { c, b } end
        end
    end
    if #hits == 1 then return hits[1][1], hits[1][2] end
    if #hits == 0 then return nil, nil, "no spell cell like " .. tostring(spell.name) .. " in the spellbook" end
    return nil, nil, string.format("%d cells look like %s (the cells carry no names): nothing clicked", #hits,
        tostring(spell.name))
end

-- Compact per-cell looks for the probe ("L1 Enchantment conc Action on needs icon=…").
local function cellLine(c)
    return string.format("L%s %s%s %s %s%s%s", tostring(c.level), tostring(c.school), c.conc == true and " conc" or "",
        tostring(c.cost), c.activated and "on" or "off", c.needs == false and " always" or "",
        c.icon and (" icon=" .. c.icon) or "")
end

local function statOf(b) return b and { value = b.value, max = b.max } or nil end

-- probe: what the page holds, read-only (the one live check of S1-S3).
local function probeOf(w, dc, books, sel)
    local p = { widget_visible = w and UI.isVisible(w) or false, metadata = str(UI.get(w, "Metadata")),
                dc = UI.typeName(dc), selected = str(UI.get(sel, "Name")),
                toggle_cmd = UI.get(dc, "ToggleCharacterAction") ~= nil, books = {} }
    for _, book in UI.items(UI.path(sel, "PlayerCharacterProperties.SpellBooks"), 16) do
        local bp = { props = UI.props(book), stat = UI.props(UI.get(book, "PreparedSpellsStat")),
                     class = UI.props(UI.get(book, "Class")), groups = {}, manual = {} }
        for gi, group in UI.items(UI.get(book, "ActionGroups"), 8) do
            local g = { props = UI.props(group), cells = {} }
            for _, vm in UI.items(UI.get(group, "Actions"), SB.PROBE_CELLS) do
                local ok, can = pcall(function()
                    local cmd = UI.get(dc, "ToggleCharacterAction")
                    return cmd and cmd:CanExecute(vm)
                end)
                local brush = UI.get(vm, "Icon")
                local src = UI.get(brush, "ImageSource")
                local desc = {}
                for _, x in UI.items(UI.get(UI.get(vm, "ShortDescription"), "Params"), 6) do
                    desc[#desc + 1] = str(UI.get(x, "Text"))
                end
                g.cells[#g.cells + 1] = { props = UI.props(vm), can_toggle = ok and can or ("error: " .. tostring(can)),
                    icon = { brush = UI.props(brush), brush_type = UI.typeName(brush), source = UI.props(src),
                             source_type = UI.typeName(src), source_str = src ~= nil and tostring(src) or nil,
                             name = SB.iconOf(vm) },
                    desc = desc, desc_props = UI.props(UI.get(vm, "ShortDescription")) }
            end
            bp.groups[gi] = g
        end
        for _, vm in UI.items(UI.get(book, "ManuallyPreparedActions"), SB.PROBE_CELLS) do
            bp.manual[#bp.manual + 1] = UI.props(vm)
        end
        p.books[#p.books + 1] = bp
    end
    local summary = {}
    for _, b in ipairs(books) do
        local lines = {}
        for _, c in ipairs(b.cells) do lines[#lines + 1] = cellLine(c) end
        summary[#summary + 1] = { class = b.class, value = b.value, max = b.max, cells = lines }
    end
    p.preparing = summary
    return p
end

local function press(key) pcall(Ext.Input.InjectKeyPress, key) end

function SB.start(args)
    local key = str(args.key) or SB.KEY
    local queue = {}
    for _, s in ipairs(args.remove or {}) do queue[#queue + 1] = { spell = s, want = false, op = "remove" } end
    for _, s in ipairs(args.add or {}) do queue[#queue + 1] = { spell = s, want = true, op = "add" } end
    local st = { phase = "open", t = RPC.now_ms(), toggled = {}, opened = false }
    local job = RPC.start_job("ui.prepare_spells", { timeout_ms = 20000 })
    local function finish(status, err)
        local res = { toggled = st.toggled, before = st.before, after = st.after, book = st.book, shows = st.shows,
                      opened = st.opened, closed = st.closed, probe = st.probe }
        RPC.finish_job(job, status, res, err)
    end
    local function phase(p) st.phase, st.t = p, RPC.now_ms() end
    local function close(status, err)
        st.final = { status, err }
        if st.opened then press(key) phase("close") else st.closed = false; finish(status, err) end
    end
    job.on_tick = function()
        local now = RPC.now_ms()
        local w, shown = SB.panel()
        if st.phase == "open" then
            if shown and SB.onSpells(w) ~= false then
                phase("read")
            elseif not st.pressed then
                -- closed: K opens the spellbook page; open on another page (inventory): K switches to it. One press
                -- only: a page just opened may still read the old Metadata for a frame, and a 2nd K would close it.
                if not shown then st.opened = true end
                st.pressed = true
                press(key)
            elseif now - st.t > SB.OPEN_MS then
                finish("failed", "the spellbook didn't open (pressed " .. key .. "; no CharacterPanel page shown)")
            end
            return
        end
        if st.phase == "close" then
            if not shown or now - st.t > SB.CLOSE_MS then
                st.closed = not shown
                finish(st.final[1], st.final[2])
            end
            return
        end
        local dc = w and UI.get(w, "DataContext")
        local books, sel = SB.books(dc)
        st.shows = str(UI.get(sel, "Name"))
        if st.phase == "read" then
            if args.probe then
                st.probe = probeOf(w, dc, books, sel)
                return close("done")
            end
            if args.name and st.shows and low(st.shows) ~= low(args.name) then
                if now - st.t > SB.READY_MS then
                    return close("failed", "the spellbook shows " .. st.shows .. ", not " .. tostring(args.name))
                end
                return
            end
            if not books[1] or books[1].n == 0 then
                if now - st.t > SB.READY_MS then
                    return close("failed", "the spellbook has no prepare page for " .. tostring(st.shows or args.name)
                        .. " (no book with MustPrepareSpells and cells)")
                end
                return
            end
            st.before = statOf(books[1])
            st.book = books[1].class
            phase("next")
        end
        if st.phase == "next" then
            local q = table.remove(queue, 1)
            if not q then
                st.after = statOf(books[1])
                return close("done")
            end
            local c, b, why = SB.match(books, q.spell, not q.want)
            local rec = { name = q.spell.name or q.spell.id, op = q.op }
            st.toggled[#st.toggled + 1] = rec
            if not c and SB.match(books, q.spell, q.want, true) then
                rec.ok, rec.why = true, q.want and "already prepared" or "already unprepared"
            elseif not c then
                rec.ok, rec.why = false, why
            elseif q.want and b.max and b.value and b.value >= b.max then
                rec.ok, rec.why = false, string.format("the spellbook is full (%d/%d prepared)", b.value, b.max)
            else
                local ok, err = UI.command(dc, "ToggleCharacterAction", c.vm)
                if ok then
                    st.cur = { q = q, rec = rec, vm = c.vm }
                    phase("flip")
                else
                    rec.ok, rec.why = false, "the cell's command: " .. tostring(err)
                end
            end
            return
        end
        if st.phase == "flip" then
            -- the clicked cell's own VM (a look match can't find it again by state once it flipped)
            if UI.get(st.cur.vm, "Activated") == st.cur.q.want then
                st.cur.rec.ok = true
                phase("next")
            elseif now - st.t > SB.FLIP_MS then
                st.cur.rec.ok, st.cur.rec.why = false, "clicked, but the cell didn't change"
                phase("next")
            end
        end
    end
    return { job = job.id }
end

RPC.register("ui.prepare_spells", function(args) return SB.start(args or {}) end)

return SB
