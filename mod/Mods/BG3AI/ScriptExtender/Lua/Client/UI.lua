-- Noesis UI helpers for the client context (owner: worker "io").
-- Shared API for every client UI module (UIState, UIActions, Progression,
-- Services). Load order: Client/UI.lua first.
--
-- TREE
--   The live tree is Ext.UI.GetRoot() (ls.UICanvas) -> ... -> Grid "ContentRoot",
--   whose children are one ls.UIWidget per open page, named after the root
--   x:Name of its XAML (GameMenu, Dialogue, ActiveRoll, ContainerWidget,
--   Dialog_box, ModalTutorial, ...; see UIState.SCREENS). Each widget's
--   DataContext is a gui::DC* / ls.DC* with the page's properties and ICommands;
--   every DC also carries the shared base (CurrentPlayer, Data, QuickSaveCommand,
--   EndTurn, ShortRest, GoToWaypoint, StartLevelUp, CustomEvent, ...).
--
-- API (all functions are pcall-safe and return nil/false on failure)
--   Widgets
--     UI.widgets()                  -> { {name, dc=<type name>, el, index} } pages under ContentRoot
--     UI.widgetMap()                -> name -> record (visible one preferred)
--     UI.widget(name)               -> widget element or nil
--     UI.dc(name)                   -> widget DataContext or nil
--     UI.anyDC()                    -> any DC with the shared base (CurrentPlayer/Data)
--     UI.widgetByDC(typeName)       -> element, DC of the first page whose DC type matches
--   Properties / view models
--     UI.prop(obj, name)            -> value (only for names known to exist: unknown names log an error)
--     UI.props(obj)                 -> table of all reflected properties (view models only, never elements)
--     UI.get(obj, name)             -> one property, nil when missing (no error log); for elements
--     UI.pick(obj, names)           -> { name = value } for just these names; for elements
--     UI.path(obj, "A.B.2.C")       -> dotted walk; numbers index collections (1-based)
--     UI.items(coll[, max])         -> iterator i, item over a Noesis collection
--     UI.count(coll)                -> collection Count
--     UI.typeName(obj), UI.name(el), UI.isVisible(el)
--   Search (visible subtree only unless opts.hidden; opts.max = node budget)
--     UI.find(el, pred[, opts])     -> element, child-index path
--     UI.findAll(el, pred[, opts])  -> { elements } (opts.prune: don't descend into matches)
--     UI.byName(el, name[, opts])   -> element with that x:Name
--     UI.findByDC(el, typeName[, opts]) -> first element whose DataContext type matches
--     UI.cachedByName(widget, name) -> like byName, cached as a child-index path
--     UI.findButton(el, nameOrLabel[, nth]) -> button by x:Name or label substring, count
--   Text
--     UI.text(el)                   -> rendered text of a TextBlock: Text, else its inlines (markup stripped)
--     UI.inlineText(inlines)        -> text of a Run/Span inline tree
--     UI.allText(el[, sep])         -> all visible text under el
--     UI.content(el[, opts])        -> compact { {t=text} | {b=label, n=name, off=true} }
--     UI.clean(s)                   -> strip <markup>, squeeze spaces, nil if empty
--   Commands
--     UI.execute(cmd[, param, dry]) -> ok, err   (dry: CanExecute only)
--     UI.command(obj, name[, param, dry]) -> ok, err   (named ICommand on a DC/VM)
--     UI.press(el[, dry])           -> ok, err   ("click": the element's Command + CommandParameter)
--   ops: ui.dump, ui.widgets, ui.props (dev)
--
-- bg3le facts this module relies on (verified live, 2026-10-09):
--   * TextBlock:GetProperty("Text") is nil; TextBlock:ToString() returns the
--     plain Text. Formatted text (dialogue, answers) lives in TextBlock.Inlines
--     (Span/Run/Italic) and ToString() is empty: UI.text() walks the inlines.
--   * GetProperty(name) logs an error for unknown names, so optional
--     properties go through get()/pick() (proxy indexing: an unknown name is
--     a Lua error, caught, not logged) or props() (GetAllProperties).
--   * Never props() a live element (Run, Button, any DependencyObject):
--     GetAllProperties enumerates its stored values table, and S017 crashed
--     the game there (SIGSEGV in push_boxed_or_object: a stored value read
--     0x100 while a dialogue answer list was being re-templated). Read the
--     names you need with get()/pick(). View models are not
--     DependencyObjects; props() on them reads their reflected properties only.
--   * Collections: GetProperty("Count") and :At(i) (1-based).
--   * Many game view models expose no names on bg3le: ls.VMItem, ls.Character
--     have no Name, ls.EntityRef has no fields, ls.VMWaypoint(Group) has no
--     properties at all. Read names from rendered text or from the server.
--   * ICommand:Execute(arg) only accepts a Noesis object or nil. A boxed
--     string/number parameter (e.g. GameMenu's "CloseGameMenu") comes back
--     as a Lua value and cannot be passed back; execute()/press() report that.
--     Raising a routed "Click" event does not run a button's command either.
--   * Never keep element proxies across calls: a re-templated element is
--     freed and a stale pointer may crash the game. Lookups are cached as
--     child-index paths and re-resolved (and checked) on every use.
--   * A full visible-tree walk of the HUD (~3200 nodes) takes ~10 ms.

BG3AI = BG3AI or {}
local UI = BG3AI.UI or {}
BG3AI.UI = UI
local RPC = BG3AI.RPC

------------------------------------------------------------------------
-- Safe primitives
------------------------------------------------------------------------

-- UI busy (S086 05:17:45 hang, impl-setup S21): during the long-rest transition one client Tick ran job 28's on_tick
-- (ui.long_rest) for 10.2 s, the next for 35.8 s: ~930 Ext.UI calls, each waiting out bg3le's 50 ms frame-lock timeout
-- (the UI thread held the frame lock and waited on the Lua lock our Tick held), then the story thread stopped
-- answering. A call that took UI.BUSY_CALL_MS or more waited out that timeout (or bg3le refused it: "bg3le: UI busy"):
-- from then on, for UI.BUSY_COOLDOWN_MS, every UI read raises UI.BUSY_ERR at once without touching the UI, so the Tick
-- ends and the UI thread gets the lock. Never a nil in place of the read (a refused IsVisible read as visible, a box
-- read as closed). RPC.lua keeps a job running through a busy tick (its verdict that tick is dropped) and answers an
-- op that met it with an error.
UI.BUSY_ERR = "bg3le: UI busy"
UI.BUSY_CALL_MS = 40
UI.BUSY_COOLDOWN_MS = 200
UI.busyUntil = UI.busyUntil or -1
UI.busyHits = UI.busyHits or 0 -- calls that met a busy UI (RPC.lua compares it around a job tick / an op)
-- UI calls that passed the gate (a read ran, or a command got past checkBusy): an op that met the busy UI with this
-- unchanged since it began was refused before its first UI call, so it did nothing and is safe to ask again (RPC.lua)
UI.calls = UI.calls or 0

local function mono() return Ext.Utils.MonotonicTime() end

function UI.busy() return mono() < UI.busyUntil end

local function busyRaise()
    UI.busyHits = UI.busyHits + 1
    error(UI.BUSY_ERR .. " (a transition holds the game's UI; retried on a later tick)", 0)
end

-- The gate for code that reads a Noesis object itself (not through these helpers): raises while the UI is busy.
function UI.checkBusy()
    if mono() < UI.busyUntil then busyRaise() end
    UI.calls = UI.calls + 1
end

local function try(f, ...)
    local t0 = mono()
    if t0 < UI.busyUntil then busyRaise() end
    UI.calls = UI.calls + 1
    local ok, v = pcall(f, ...)
    local t1 = mono()
    if t1 - t0 >= UI.BUSY_CALL_MS or (not ok and tostring(v):find("UI busy", 1, true)) then
        UI.busyUntil = t1 + UI.BUSY_COOLDOWN_MS
        busyRaise()
    end
    if ok then return v end
    return nil
end

-- Property read for names known to exist on the object's type.
function UI.prop(obj, name)
    if obj == nil then return nil end
    return try(obj.GetProperty, obj, name)
end

-- All properties as a Lua table (no error log for missing names). For view
-- models; elements go through UI.get/UI.pick (see the S017 note above).
function UI.props(obj)
    if obj == nil then return {} end
    return try(obj.GetAllProperties, obj) or {}
end

-- One property by name, nil when the object has none (proxy indexing raises a
-- Lua error for an unknown name, without logging it). Reads that one value:
-- safe on live elements.
function UI.get(obj, name)
    if obj == nil then return nil end
    return try(function() return obj[name] end)
end

-- Just these properties, as { name = value }.
function UI.pick(obj, names)
    local out = {}
    if obj == nil then return out end
    for _, k in ipairs(names) do
        local v = UI.get(obj, k)
        if v ~= nil then out[k] = v end
    end
    return out
end

function UI.typeName(obj)
    if obj == nil then return nil end
    return try(function() return obj.Type end)
end

function UI.name(el)
    local n = try(function() return el.Name end)
    if n == "" then return nil end
    return n
end

-- Children of a Panel are read from its Children collection
-- (UIElementCollection:At, BaseCollection::GetComponent), never VisualChild:
-- in this build Panel::GetVisualChild always runs Panel::UpdateChildrenZOrder,
-- a lazy rebuild of the z-order cache, and our walk rebuilding it while the UI
-- thread's hit test (View::MouseMove) did the same crashed the game (S014:
-- SIGSEGV in UpdateChildrenZOrder, right after a loot panel was added to
-- ContentRoot). Reading the collection writes nothing. Same set of children;
-- the order is the insertion one, not the ZIndex one (ContentRoot uses ZIndex;
-- lookups go by name, and cached index paths check the name). Not the logical
-- children (el:Child): live, ContentRoot (a Grid) has 2 logical children that
-- are not in its visual tree. Only stock Noesis panels (GetProperty on an
-- unknown name logs an error), and only while the collection's count equals
-- the visual one; anything else falls back to VisualChild.
UI.PANEL_TYPES = {
    Panel = true, Grid = true, StackPanel = true, Canvas = true, DockPanel = true, WrapPanel = true,
    UniformGrid = true, VirtualizingPanel = true, VirtualizingStackPanel = true, TabPanel = true,
    ToolBarPanel = true, ToolBarOverflowPanel = true,
}
UI.childReads = UI.childReads or { collection = 0, visual = 0, skipped = 0 }
UI.childReads.skipped = UI.childReads.skipped or 0
-- element proxy -> its Children collection, false (read VisualChild) or "mismatch" (a stock panel whose
-- collection count is not the visual one: changing right now)
local childColl = setmetatable({}, { __mode = "k" })

-- bg3le's strict frame-lock mode (patch 0010, BG3LE_UI_STRICT=1 / Ext._Internal.UiStrict(true)), read per call
-- (an atomic load, not wrapped in the frame lock). Audit UI-5: in strict mode a changing stock panel is skipped
-- (its children read nil this time; callers retry on a later tick) instead of falling back to
-- Panel::GetVisualChild, the S014 crash site. Off: the fallback stays as before.
function UI.strict()
    local f = Ext._Internal and Ext._Internal.UiStrict
    if type(f) ~= "function" then return false end
    local ok, on = pcall(f)
    return ok and on == true
end

function UI.childCount(el)
    local n = try(function() return el.VisualChildrenCount end) or 0
    if el == nil then return n end
    local coll = false
    if n > 0 and UI.PANEL_TYPES[UI.typeName(el) or ""] then
        local c = UI.prop(el, "Children")
        if c ~= nil and tonumber(UI.prop(c, "Count")) == n then coll = c else coll = "mismatch" end
    end
    childColl[el] = coll
    return n
end

function UI.child(el, i)
    if el == nil then return nil end
    local coll = childColl[el]
    if coll == nil then
        UI.childCount(el)
        coll = childColl[el]
    end
    if coll == "mismatch" and UI.strict() then
        UI.childReads.skipped = UI.childReads.skipped + 1
        return nil
    end
    if coll and coll ~= "mismatch" then
        UI.childReads.collection = UI.childReads.collection + 1
        return try(coll.At, coll, i)
    end
    UI.childReads.visual = UI.childReads.visual + 1
    return try(el.VisualChild, el, i)
end

function UI.isVisible(el)
    local v = UI.prop(el, "IsVisible")
    return v ~= false
end

-- Strip the game's inline markup (<LSTag ...>, <br>, <i>) and squeeze spaces.
function UI.clean(s)
    if type(s) ~= "string" then return nil end
    s = s:gsub("<[bB][rR]%s*/?>", " "):gsub("<[^>]->", ""):gsub("%s+", " ")
    s = s:gsub("^ ", ""):gsub(" $", "")
    if s == "" or s == "ls::TranslatedStringRepository::s_HandleUnknown" then return nil end
    return s
end

-- Text of an inline tree (TextBlock.Inlines: Run / Span / Bold / Italic /
-- LSRun ...). Formatted game text (dialogue lines, answers, tooltips) is
-- built as inlines by ls:TextBlockFormatter, so TextBlock:ToString() is empty.
function UI.inlineText(inlines, depth)
    depth = depth or 0
    if inlines == nil or depth > 8 then return "" end
    local parts = {}
    for _, x in UI.items(inlines, 200) do
        -- Named reads only: these Runs are live elements (S017's crash was a
        -- GetAllProperties on one while the answer list was re-templated).
        local text = UI.get(x, "Text")
        local sub = type(text) ~= "string" and UI.get(x, "Inlines") or nil
        if type(text) == "string" then
            parts[#parts + 1] = text
        elseif sub ~= nil then
            parts[#parts + 1] = UI.inlineText(sub, depth + 1)
        elseif UI.typeName(x) == "LineBreak" then
            parts[#parts + 1] = " "
        end
    end
    return table.concat(parts)
end

-- Rendered text of a TextBlock: its Text (ToString) or, when that is empty,
-- its inlines. Markup stripped.
function UI.text(el)
    local s = UI.clean(try(el.ToString, el))
    if s then return s end
    if UI.typeName(el) == "TextBlock" then
        local inl = UI.prop(el, "Inlines")
        if inl then return UI.clean(UI.inlineText(inl)) end
    end
    return nil
end

-- Iterate a Noesis collection: for i, item in UI.items(coll) do ... end
function UI.items(coll, max)
    local n = tonumber(UI.prop(coll, "Count")) or 0
    if max and n > max then n = max end
    local i = 0
    return function()
        i = i + 1
        if i > n then return nil end
        return i, try(coll.At, coll, i)
    end
end

function UI.count(coll)
    return tonumber(UI.prop(coll, "Count")) or 0
end

-- Dotted path from an object: "CurrentPlayer.UIData.Notification",
-- numbers index collections ("Data.InterruptEvents.1").
function UI.path(obj, path)
    for part in tostring(path or ""):gmatch("[^%.]+") do
        if obj == nil then return nil end
        local idx = tonumber(part)
        if idx then obj = try(obj.At, obj, idx) else obj = UI.prop(obj, part) end
    end
    return obj
end

------------------------------------------------------------------------
-- Widgets
------------------------------------------------------------------------

function UI.contentRoot()
    local root = try(Ext.UI.GetRoot)
    if not root then return nil end
    return try(root.Find, root, "ContentRoot")
end

-- A page without an x:Name goes by its ls:UIWidget.ContextName: live (2026-10-17) the Credits page (Credits.xaml)
-- has Name "" and ContextName "Credits"; on the named pages the two are the same.
local function pageName(w)
    local n = UI.name(w)
    if n then return n end
    local ctx = UI.prop(w, "ContextName")
    ctx = ctx ~= nil and tostring(ctx) or ""
    return ctx ~= "" and ctx or nil
end

-- { {name, dc, el}, ... } for every page under ContentRoot (cheap: ~25).
function UI.widgets()
    local cr = UI.contentRoot()
    local out = {}
    if not cr then return out end
    for i = 1, UI.childCount(cr) do
        local w = UI.child(cr, i)
        if w then
            local dc = try(function() return w.DataContext end)
            out[#out + 1] = { name = pageName(w) or "?", dc = UI.typeName(dc), el = w, index = i }
        end
    end
    return out
end

-- Set of present widget names -> widget record (first match wins, except
-- that a visible one beats a hidden duplicate).
function UI.widgetMap()
    local map = {}
    for _, w in ipairs(UI.widgets()) do
        local prev = map[w.name]
        if not prev or (not UI.isVisible(prev.el) and UI.isVisible(w.el)) then
            map[w.name] = w
        end
    end
    return map
end

function UI.widget(name)
    for _, w in ipairs(UI.widgets()) do
        if w.name == name then return w.el end
    end
    return nil
end

function UI.dc(name)
    local w = UI.widget(name)
    return w and try(function() return w.DataContext end) or nil
end

-- First page whose DataContext type equals typeName (e.g. "gui::DCHotBar").
function UI.widgetByDC(typeName)
    for _, w in ipairs(UI.widgets()) do
        if w.dc == typeName then
            return w.el, try(function() return w.el.DataContext end)
        end
    end
    return nil
end

-- Any live DataContext that carries the shared base (CurrentPlayer, Data,
-- QuickSaveCommand, ...): every gui::DC* derives from it.
function UI.anyDC()
    for _, w in ipairs(UI.widgets()) do
        local dc = try(function() return w.el.DataContext end)
        if dc and UI.prop(dc, "CurrentPlayer") ~= nil then return dc end
    end
    return nil
end

------------------------------------------------------------------------
-- Tree search
------------------------------------------------------------------------

-- Depth-first search below el. pred(e) -> true to return e. Invisible
-- subtrees are skipped unless opts.hidden. Returns element, index path.
function UI.find(el, pred, opts)
    opts = opts or {}
    local budget = opts.max or 20000
    local path = {}
    local function rec(e)
        budget = budget - 1
        if budget < 0 then return nil end
        if not opts.hidden and not UI.isVisible(e) then return nil end
        if pred(e) then return e end
        for i = 1, UI.childCount(e) do
            local c = UI.child(e, i)
            if c then
                path[#path + 1] = i
                local r = rec(c)
                if r then return r end
                path[#path] = nil
            end
        end
        return nil
    end
    local r = rec(el)
    if r then
        local copy = {}
        for i, v in ipairs(path) do copy[i] = v end
        return r, copy
    end
    return nil
end

function UI.findAll(el, pred, opts)
    opts = opts or {}
    local out, budget = {}, opts.max or 20000
    local function rec(e)
        budget = budget - 1
        if budget < 0 then return end
        if not opts.hidden and not UI.isVisible(e) then return end
        if pred(e) then
            out[#out + 1] = e
            if opts.prune then return end
        end
        for i = 1, UI.childCount(e) do
            local c = UI.child(e, i)
            if c then rec(c) end
        end
    end
    rec(el)
    return out
end

function UI.byName(el, name, opts)
    return UI.find(el, function(e) return UI.name(e) == name end, opts)
end

function UI.findByDC(el, typeName, opts)
    return UI.find(el, function(e)
        local dc = try(function() return e.DataContext end)
        return dc ~= nil and UI.typeName(dc) == typeName
    end, opts)
end

-- Path cache: key -> {widget element id, index path, expected name}.
UI.pathCache = UI.pathCache or {}

local function follow(el, path)
    for _, i in ipairs(path) do
        el = UI.child(el, i)
        if not el then return nil end
    end
    return el
end

-- Find a named element under a widget, caching the child-index path. The
-- cached path is re-walked from the live widget each call and accepted only
-- if the element there still has the expected name.
function UI.cachedByName(widgetName, elName)
    local w = UI.widget(widgetName)
    if not w then return nil end
    local key = widgetName .. "/" .. elName
    local wid = tostring(w)
    local c = UI.pathCache[key]
    if c and c.wid == wid then
        local e = follow(w, c.path)
        if e and UI.name(e) == elName then return e end
    end
    local e, path = UI.byName(w, elName, { hidden = true })
    if e then UI.pathCache[key] = { wid = wid, path = path } end
    return e
end

------------------------------------------------------------------------
-- Compact content: visible text and buttons
------------------------------------------------------------------------

local BUTTON_TYPES = {
    ["Button"] = true, ["ls.LSButton"] = true, ["ToggleButton"] = true,
    ["ls.LSToggleButton"] = true, ["ls.LSRadioButton"] = true, ["RadioButton"] = true,
    ["MenuItem"] = true,
}
UI.BUTTON_TYPES = BUTTON_TYPES

function UI.isButton(el)
    return BUTTON_TYPES[UI.typeName(el) or ""] == true
end

-- All visible text under el, joined.
function UI.allText(el, sep, max)
    local parts = {}
    for _, t in ipairs(UI.findAll(el, function(e) return UI.typeName(e) == "TextBlock" end, { max = max or 2000 })) do
        local s = UI.text(t)
        if s then parts[#parts + 1] = s end
    end
    if #parts == 0 then return nil end
    return table.concat(parts, sep or " ")
end

-- Visible content of a subtree in document order:
--   { {t="text"}, {b="label", n="ElementName", off=true?}, ... }
-- A button's own texts become its label instead of separate entries.
-- opts: max (node budget), text_only, depth.
function UI.content(el, opts)
    opts = opts or {}
    local out, budget = {}, opts.max or 6000
    local seen = {}
    local function rec(e, d)
        budget = budget - 1
        if budget < 0 then return end
        if not UI.isVisible(e) then return end
        if opts.depth and d > opts.depth then return end
        local ty = UI.typeName(e)
        if ty == "TextBlock" then
            local s = UI.text(e)
            if s and not seen[s .. "#" .. #out] then out[#out + 1] = { t = s } end
            return
        end
        if BUTTON_TYPES[ty or ""] and not opts.text_only then
            local label = UI.allText(e, " ", 300)
            local entry = { b = label, n = UI.name(e) }
            if not label and not entry.n then return end
            if UI.prop(e, "IsEnabled") == false then entry.off = true end
            out[#out + 1] = entry
            return
        end
        for i = 1, UI.childCount(e) do
            local c = UI.child(e, i)
            if c then rec(c, d + 1) end
        end
    end
    rec(el, 0)
    return out
end

------------------------------------------------------------------------
-- Commands
------------------------------------------------------------------------

local function isNoesis(v)
    return type(v) == "userdata" or (type(v) == "table" and UI.typeName(v) ~= nil)
end

-- Execute an ICommand with an optional Noesis-object parameter.
-- dry=true only checks CanExecute. Returns ok, err.
function UI.execute(cmd, param, dry)
    if cmd == nil then return false, "no command" end
    if param ~= nil and not isNoesis(param) then
        return false, "parameter is a " .. type(param) .. " (" .. tostring(param) .. "); bg3le can only pass Noesis objects"
    end
    UI.checkBusy()
    local okc, can = pcall(cmd.CanExecute, cmd, param)
    if not okc then return false, "CanExecute failed: " .. tostring(can) end
    if not can then return false, "command cannot execute now" end
    if dry then return true end
    local oke, err = pcall(cmd.Execute, cmd, param)
    UI.lastCommandFrame = UI.frame
    UI.lastCommandMs = Ext.Utils.MonotonicTime()
    if not oke then return false, "Execute failed: " .. tostring(err) end
    return true
end

-- Named command on a DataContext (or any VM).
function UI.command(obj, name, param, dry)
    local cmd = UI.prop(obj, name)
    if cmd == nil then return false, "no command '" .. tostring(name) .. "' on " .. tostring(UI.typeName(obj)) end
    return UI.execute(cmd, param, dry)
end

-- "Click" a button element: run its Command with its CommandParameter.
function UI.press(el, dry)
    if not el then return false, "no element" end
    local p = UI.pick(el, { "Command", "IsEnabled", "CommandParameter" })
    local cmd = p.Command
    if cmd == nil then return false, "element has no Command (" .. tostring(UI.name(el) or UI.typeName(el)) .. ")" end
    if p.IsEnabled == false then return false, "button is disabled" end
    return UI.execute(cmd, p.CommandParameter, dry)
end

-- Find a visible button under a widget by element name or label text
-- (case-insensitive substring), nth match (1-based).
function UI.findButton(widgetEl, which, nth)
    local want = tostring(which):lower()
    local hits = UI.findAll(widgetEl, function(e)
        if not UI.isButton(e) then return false end
        if (UI.name(e) or ""):lower() == want then return true end
        local label = UI.allText(e, " ", 200)
        return label ~= nil and label:lower():find(want, 1, true) ~= nil
    end, { prune = true })
    return hits[nth or 1], #hits
end

------------------------------------------------------------------------
-- Frame counter and "quiet" window after our own commands: background
-- readers (AutoSkip) stay off the tree while the UI reacts to a command.
------------------------------------------------------------------------

UI.frame = UI.frame or 0
UI.lastCommandFrame = UI.lastCommandFrame or -100
UI.lastCommandMs = UI.lastCommandMs or 0
if not UI.frameSubscribed then
    UI.frameSubscribed = true
    Ext.Events.Tick:Subscribe(function() UI.frame = UI.frame + 1 end)
end

-- true while fewer than `frames` frames (and 150 ms) passed since our last command
function UI.quiet(frames)
    return UI.frame - UI.lastCommandFrame < (frames or 3)
        or Ext.Utils.MonotonicTime() - UI.lastCommandMs < 150
end

------------------------------------------------------------------------
-- ops
------------------------------------------------------------------------

-- ui.dump {root?=widget name, depth?, text_only?, max?, hidden?}
-- Without root: one entry per visible widget with its content.
RPC.register("ui.dump", function(args)
    local t0 = RPC.now_ms()
    local out = {}
    for _, w in ipairs(UI.widgets()) do
        if (args.root == nil or w.name == args.root) and (args.hidden or UI.isVisible(w.el)) then
            local items = UI.content(w.el, { depth = tonumber(args.depth), text_only = args.text_only, max = tonumber(args.max) })
            if #items > 0 or args.root then
                out[#out + 1] = { w = w.name, dc = w.dc, items = items }
            end
        end
    end
    return { widgets = out, ms = RPC.now_ms() - t0 }
end)

-- ui.widgets: names and DataContext types of the open pages.
RPC.register("ui.widgets", function()
    local out = {}
    for _, w in ipairs(UI.widgets()) do
        out[#out + 1] = { n = w.name, dc = w.dc, vis = UI.isVisible(w.el) or nil }
    end
    return out
end)

-- ui.props {widget, path?}: primitive properties and command names of a
-- DataContext (or a VM below it). Dev tool for finding bindings.
RPC.register("ui.props", function(args)
    local dc = UI.dc(args.widget or "")
    if not dc then error("no widget " .. tostring(args.widget)) end
    local obj = args.path and UI.path(dc, args.path) or dc
    if obj == nil then error("path not found: " .. tostring(args.path)) end
    local values, objects, commands = {}, {}, {}
    for k, v in pairs(UI.props(obj)) do
        local tn = isNoesis(v) and UI.typeName(v) or nil
        if tn == nil then
            values[k] = v
        elseif tn:find("Command") then
            commands[#commands + 1] = k
        else
            objects[k] = tn .. (tn:find("Collection") and ("[" .. UI.count(v) .. "]") or "")
        end
    end
    table.sort(commands)
    return { type = UI.typeName(obj), values = values, objects = objects, commands = args.commands and commands or #commands }
end)

return UI
