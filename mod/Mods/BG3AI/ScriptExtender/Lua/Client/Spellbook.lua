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

-- The selected character's preparing books: { {vm, class, value, max, cells = { [lower name] = cell }, n} } where
-- cell = {vm, name, activated, needs}.
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
                local name = str(UI.get(vm, "Name"))
                if name and not b.cells[name:lower()] then
                    b.cells[name:lower()] = { vm = vm, name = name, activated = UI.get(vm, "Activated") == true,
                                              needs = UI.get(vm, "NeedsActivation") }
                    b.n = b.n + 1
                end
            end
            for _, group in UI.items(UI.get(book, "ActionGroups"), 32) do
                for _, vm in UI.items(UI.get(group, "Actions"), 200) do add(vm) end
            end
            for _, vm in UI.items(UI.get(book, "ManuallyPreparedActions"), 64) do add(vm) end
            out[#out + 1] = b
        end
    end
    return out, sel
end

-- The cell for a spell {id, name} in any preparing book: cell, book.
function SB.cell(books, spell)
    for _, b in ipairs(books) do
        local c = b.cells[low(spell.name)] or b.cells[low(spell.id)]
        if c then return c, b end
    end
    return nil
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
                g.cells[#g.cells + 1] = { props = UI.props(vm), can_toggle = ok and can or ("error: " .. tostring(can)) }
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
        local names = {}
        for _, c in pairs(b.cells) do names[#names + 1] = c.name .. (c.activated and "*" or "") end
        table.sort(names)
        summary[#summary + 1] = { class = b.class, value = b.value, max = b.max, cells = names }
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
            local c, b = SB.cell(books, q.spell)
            local rec = { name = q.spell.name or q.spell.id, op = q.op }
            st.toggled[#st.toggled + 1] = rec
            if not c then
                rec.ok, rec.why = false, "no spell cell named " .. tostring(rec.name) .. " in the spellbook"
            elseif c.activated == q.want then
                rec.ok, rec.why = true, q.want and "already prepared" or "already unprepared"
            elseif q.want and b.max and b.value and b.value >= b.max then
                rec.ok, rec.why = false, string.format("the spellbook is full (%d/%d prepared)", b.value, b.max)
            else
                local ok, err = UI.command(dc, "ToggleCharacterAction", c.vm)
                if ok then
                    st.cur = { q = q, rec = rec }
                    phase("flip")
                else
                    rec.ok, rec.why = false, "the cell's command: " .. tostring(err)
                end
            end
            return
        end
        if st.phase == "flip" then
            local c = SB.cell(books, st.cur.q.spell)
            if c and c.activated == st.cur.q.want then
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
