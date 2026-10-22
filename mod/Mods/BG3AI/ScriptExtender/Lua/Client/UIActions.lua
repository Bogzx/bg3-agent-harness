-- UI actions (owner: worker "io"): dialogue answers, active rolls, reaction
-- prompts, looting, menus, keys and a generic command escape hatch.
--
-- Everything goes through the game's own ICommands (the same request a
-- button click sends), so server rules and costs apply. Ops that take game
-- time return {job=id}; the job settles as soon as the UI reaches the next
-- decision point, so a batched caller can chain actions quickly:
--   ui.dialogue_choose -> next line with options / continue prompt / roll /
--                         dialogue ended (all lines seen meanwhile included)
--   ui.roll            -> roll result shown / roll panel closed
--   ui.react           -> prompt answered (closed or next prompt)
--   ui.loot            -> container closed (take all)
-- Every action takes dry=true to only check CanExecute (safe on the user's save).
--
-- Live-verified (2026-10-09): ui.key (ESCAPE opens/closes the game menu),
-- ui.menu open_menu/close_menu (jobs settle in ~100 ms), ui.command dry runs,
-- quicksave refusal, error paths of every op.
-- Untested live (rules forbid triggering them on the user's save):
-- dialogue_choose, roll, react, loot (non-dry), menu continue/load.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState
local A = BG3AI.UIActions or {}
BG3AI.UIActions = A

-- Saving and loading are hard-disabled while the user's own save is loaded.
-- The gates persist in bg3ai/config.json (Ext.IO, the extender's data dir) so
-- hot reloads and restarts keep them; defaults are false. The runner / MCP
-- server sets them with ui.config only when the AI's own playthrough is loaded.
A.CONFIG_PATH = "bg3ai/config.json"

function A.loadConfig()
    local cfg = {}
    local ok, text = pcall(Ext.IO.LoadFile, A.CONFIG_PATH)
    if ok and type(text) == "string" and text ~= "" then
        local okp, parsed = pcall(Ext.Json.Parse, text)
        if okp and type(parsed) == "table" then cfg = parsed end
    end
    A.config = cfg
    A.SAVE_ENABLED = cfg.save_enabled == true
    A.LOAD_ENABLED = cfg.load_enabled == true
    return cfg
end

function A.gates()
    return { save = A.SAVE_ENABLED == true, load = A.LOAD_ENABLED == true }
end

A.loadConfig()

-- ui.config {save_enabled?, load_enabled?, autoskip?} -> {gates, autoskip};
-- persists everything in bg3ai/config.json.
RPC.register("ui.config", function(args)
    local cfg = A.config or {}
    local changed = false
    if args.save_enabled ~= nil then cfg.save_enabled = args.save_enabled == true changed = true end
    if args.load_enabled ~= nil then cfg.load_enabled = args.load_enabled == true changed = true end
    if args.autoskip ~= nil then cfg.autoskip = args.autoskip == true changed = true end
    A.config = cfg
    A.SAVE_ENABLED = cfg.save_enabled == true
    A.LOAD_ENABLED = cfg.load_enabled == true
    if changed then
        local ok, err = pcall(Ext.IO.SaveFile, A.CONFIG_PATH, Ext.Json.Stringify(cfg))
        if not ok or err == false then error("could not write " .. A.CONFIG_PATH .. ": " .. tostring(err)) end
    end
    return { gates = A.gates(), autoskip = cfg.autoskip ~= false }
end)

local POLL_MS = 80

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local function check(ok, err)
    if not ok then error(err or "failed", 0) end
end

-- Start a job whose check(job) runs every POLL_MS; check returns
-- status, result to finish (or nil to keep waiting). On timeout the result
-- is job.partial.
local function waitJob(kind, timeout_ms, checkFn, init)
    local job = RPC.start_job(kind, { timeout_ms = timeout_ms })
    job.next_check = 0
    if init then init(job) end
    job.on_tick = function(j)
        local t = RPC.now_ms()
        if t < j.next_check then return end
        j.next_check = t + POLL_MS
        local status, result = checkFn(j)
        if status then RPC.finish_job(j, status, result, status ~= "done" and result and result.error or nil) end
    end
    return { job = job.id }
end
A.waitJob = waitJob

-- Saving and loading belong to the dedicated jobs (MENU.save/load/quick*,
-- reached through the bridge's save_game/load_game): they check the gates,
-- the bridge adds the save-prefix check, the saver's settle and the DELTA
-- reset. The agent-facing escape hatches (ui.command, ui.press, ui.key)
-- refuse anything that would save, load or delete a save (audit UI-3:
-- ui_action QuickLoadCommand widget="*" loaded with LOAD_ENABLED=false).
-- Internal jobs call UI.command directly and are not affected. dry runs pass.
A.SAVELOAD_PAGES = { SaveGame = true, SaveGamePopup = true, LoadGame = true }
A.SAVELOAD_WORDS = { "quicksave", "quickload", "savegame", "loadgame", "continuegame", "overwrite",
    "deletesave", "loadsave" }
A.SAVELOAD_LABELS = { save = true, load = true, quicksave = true, quickload = true, overwrite = true,
    savegame = true, loadgame = true }
-- the game's default bindings (assumed; keybindings are not read)
A.SAVELOAD_KEYS = { F5 = "quicksave", F8 = "quickload" }
A.SAVELOAD_HINT = "; saving and loading go through save_game / load_game (gated, prefix-checked)"

-- a command/button name or label that saves, loads or deletes a save -> the matched word, else nil. Pure.
function A.saveLoadName(name)
    local s = tostring(name or ""):lower():gsub("[%s_%-%.]", "")
    for _, w in ipairs(A.SAVELOAD_WORDS) do if s:find(w, 1, true) then return w end end
    return nil
end

-- a way out of a page (close / back / cancel) is always allowed. Pure.
function A.leaveName(name)
    local s = tostring(name or ""):lower()
    return s:find("close", 1, true) or s:find("back", 1, true) or s:find("cancel", 1, true)
        or s:find("hide", 1, true) or s:find("exit", 1, true)
end

-- the open Save/Load page's widget name, or nil
function A.saveLoadPage()
    for n in pairs(A.SAVELOAD_PAGES) do
        local w = UI.widget(n)
        if w and UI.isVisible(w) then return n end
    end
    return nil
end

local function refuseSaveLoad(what)
    error(what .. A.SAVELOAD_HINT, 0)
end

------------------------------------------------------------------------
-- ui.key {key, mods?, hold_ms?}
--   key: a key name as a player would say it ("ESCAPE", "Delete", "1",
--   "Left", "PgDn", "ctrl+s"), normalised to bg3se's SDLScanCode label
--   (DEL, NUM_1, LEFT, PAGEDOWN; live S007: "not a SDLScanCode: DELETE").
--   mods: modifier name or list ("shift", {"LCTRL","alt"}) -> SDLKeyModifier
--   labels (LShift, LCtrl, ...); "ctrl+"/"shift+" prefixes in key add to it.
--   hold_ms: press down now, release after hold_ms (returns a job).
------------------------------------------------------------------------

-- Common names -> SDLScanCode labels (the enum spells DEL, NUM_1, RETURN, ...).
A.KEY_ALIASES = {
    DELETE = "DEL", ENTER = "RETURN", ESC = "ESCAPE", BKSP = "BACKSPACE", BACK = "BACKSPACE",
    SPACEBAR = "SPACE", PGUP = "PAGEUP", PGDN = "PAGEDOWN", PAGE_UP = "PAGEUP", PAGE_DOWN = "PAGEDOWN",
    INS = "INSERT", ARROWUP = "UP", ARROWDOWN = "DOWN", ARROWLEFT = "LEFT", ARROWRIGHT = "RIGHT",
    UP_ARROW = "UP", DOWN_ARROW = "DOWN", LEFT_ARROW = "LEFT", RIGHT_ARROW = "RIGHT",
    ARROW_UP = "UP", ARROW_DOWN = "DOWN", ARROW_LEFT = "LEFT", ARROW_RIGHT = "RIGHT",
    CTRL = "LCTRL", CONTROL = "LCTRL", SHIFT = "LSHIFT", ALT = "LALT", CAPS = "CAPSLOCK",
    PRTSC = "PRINTSCREEN", PRINT = "PRINTSCREEN", NUMPAD_ENTER = "KP_ENTER",
    ["`"] = "GRAVE", ["~"] = "GRAVE", ["-"] = "MINUS", ["="] = "EQUALS", ["["] = "LEFTBRACKET",
    ["]"] = "RIGHTBRACKET", ["\\"] = "BACKSLASH", [";"] = "SEMICOLON", ["'"] = "APOSTROPHE",
    [","] = "COMMA", ["."] = "PERIOD", ["/"] = "SLASH",
}
A.MOD_ALIASES = {
    SHIFT = "LShift", LSHIFT = "LShift", RSHIFT = "RShift", CTRL = "LCtrl", CONTROL = "LCtrl",
    LCTRL = "LCtrl", RCTRL = "RCtrl", ALT = "LAlt", LALT = "LAlt", RALT = "RAlt",
}

-- (key, mods) -> scancode label, modifier label list or nil. Pure.
function A.keySpec(key, mods)
    local out = {}
    local function addMod(m)
        local l = A.MOD_ALIASES[tostring(m):upper()] or tostring(m)
        for _, x in ipairs(out) do if x == l then return end end
        out[#out + 1] = l
    end
    if type(mods) == "table" then for _, m in ipairs(mods) do addMod(m) end
    elseif mods ~= nil and mods ~= "" then addMod(mods) end
    local k = tostring(key):gsub("^%s+", ""):gsub("%s+$", "")
    -- "ctrl+s", "Shift-Tab": leading modifiers (a lone "-"/"+" stays a key)
    while true do
        local m, rest = k:match("^(%a+)%s*[%+%-]%s*(.+)$")
        if not (m and A.MOD_ALIASES[m:upper()]) then break end
        addMod(m)
        k = rest
    end
    local u = k:upper():gsub("[%s%-]+", "_")
    u = A.KEY_ALIASES[u] or A.KEY_ALIASES[k] or u
    if u:match("^%d$") then u = "NUM_" .. u end
    local kp = u:match("^NUMPAD_?(.+)$") or u:match("^KEYPAD_?(.+)$")
    if kp then u = "KP_" .. kp end
    return u, #out > 0 and out or nil
end

RPC.register("ui.key", function(args)
    local key, mods = A.keySpec(args.key or error("key required"), args.mods)
    args.mods = mods
    if A.SAVELOAD_KEYS[key] and not mods then
        refuseSaveLoad(key .. " is " .. A.SAVELOAD_KEYS[key] .. ": refused")
    end
    local page = key ~= "ESCAPE" and A.saveLoadPage()
    if page then refuseSaveLoad(key .. " refused: the " .. page .. " page is open (ESCAPE closes it)") end
    if args.hold_ms then
        Ext.Input.InjectKeyDown(key, args.mods)
        local release = RPC.now_ms() + tonumber(args.hold_ms)
        return waitJob("ui.key", tonumber(args.hold_ms) + 2000, function()
            if RPC.now_ms() >= release then
                Ext.Input.InjectKeyUp(key, args.mods)
                return "done", { key = key }
            end
        end)
    end
    local ok, err = pcall(Ext.Input.InjectKeyPress, key, args.mods)
    if not ok then
        if tostring(err):find("not a SDL") then
            error("unknown key '" .. tostring(args.key) .. "' (as " .. key .. "); use names like ESCAPE, RETURN, "
                .. "SPACE, TAB, DELETE, BACKSPACE, UP/DOWN/LEFT/RIGHT, F1-F12, A-Z, 0-9, ctrl+s", 0)
        end
        error(err, 0)
    end
    return { key = key, mods = args.mods }
end)

------------------------------------------------------------------------
-- ui.command {widget, command, target?, param?, dry?}
--   Executes DC.<target>.<command>(param). target/param are dotted paths
--   from the widget's DataContext (numbers index collections). widget may be
--   "*" for any DC with the shared base.
-- ui.press {widget, button, nth?, dry?}
--   Clicks a visible button by x:Name or label substring.
------------------------------------------------------------------------

local function dcOf(widget)
    if widget == nil or widget == "*" then return UI.anyDC() end
    return UI.dc(widget)
end

local function commandOp(args)
    if not args.dry then
        local w = A.saveLoadName(args.command)
        if w then refuseSaveLoad("ui.command " .. tostring(args.command) .. " refused (" .. w .. ")") end
        local page = A.SAVELOAD_PAGES[tostring(args.widget)] and args.widget or A.saveLoadPage()
        if page and not A.leaveName(args.command) then
            refuseSaveLoad("ui.command " .. tostring(args.command) .. " refused: the " .. page .. " page is open")
        end
    end
    local dc = dcOf(args.widget) or error("widget not open: " .. tostring(args.widget))
    local obj = args.target and UI.path(dc, args.target) or dc
    if obj == nil then error("target not found: " .. tostring(args.target)) end
    local param = nil
    if args.param ~= nil then
        param = UI.path(dc, args.param)
        if param == nil then error("param not found: " .. tostring(args.param)) end
    end
    check(UI.command(obj, args.command or error("command required"), param, args.dry))
    return { done = not args.dry, can = true }
end

RPC.register("ui.command", function(args)
    local ok, res = pcall(commandOp, args)
    if ok then return res end
    if tostring(res):find(A.SAVELOAD_HINT, 1, true) then error(res, 0) end
    -- With a confirmation box open, "Yes"/"OK"/"No" (as command or param) means
    -- its button (live S005: ui_action Yes -> "no command 'Yes' on ls.Widget").
    if A.confirmBox and A.confirmBox() then
        for _, k in ipairs({ "param", "command" }) do
            local want = args[k]
            if type(want) == "string" and want ~= "" then
                local pressed, view = A.answerConfirm(want, args.dry)
                if pressed then return view end
            end
        end
    end
    error(res, 0)
end)

-- BoundEvent -> key for buttons whose command needs a string parameter
-- (bg3le can't pass one): pressing the bound key fires the button the way
-- the game does. Only for buttons that declare the binding.
A.BOUND_KEYS = { UIAccept = "RETURN", UICancel = "ESCAPE", ToggleInGameMenu = "ESCAPE" }
-- CommandParameter -> key, for such buttons that declare no binding above: GameMenu's Resume passes "CloseGameMenu"
-- (live5 U12: ui.press Resume failed "parameter is a string (CloseGameMenu)"); ESCAPE closes that menu (live1 §2).
A.PARAM_KEYS = { CloseGameMenu = "ESCAPE" }

-- Press a button element: its command, or (string parameter) its bound key.
function A.pressButton(el, dry)
    local ok, err = UI.press(el, dry)
    if ok then return true, "command" end
    local key = A.BOUND_KEYS[tostring(UI.get(el, "BoundEvent") or "")]
        or A.PARAM_KEYS[tostring(UI.get(el, "CommandParameter") or "")]
    if key and tostring(err):find("parameter is a") then
        if dry then return true, "key:" .. key end
        Ext.Input.InjectKeyPress(key)
        return true, "key:" .. key
    end
    return false, err
end

-- ui.press {button, widget?, nth?, dry?}
--   button: the dump's n= name (e.g. "NewSaveBtn") or a label substring.
--   widget: page to search; omitted (or not an open page) = every visible page.
RPC.register("ui.press", function(args)
    local button = args.button or error("button required")
    local pages = {}
    local w = args.widget and UI.widget(args.widget)
    if w then
        pages[1] = { name = args.widget, el = w }
    else
        -- Agents sometimes pass the button name as the widget: accept it.
        if args.widget and not args.button then button = args.widget end
        for _, rec in ipairs(UI.widgets()) do
            if UI.isVisible(rec.el) then pages[#pages + 1] = rec end
        end
    end
    local found, total, page
    for _, pg in ipairs(pages) do
        local el, n = UI.findButton(pg.el, button, tonumber(args.nth))
        if el and not found then found, page = el, pg.name end
        total = (total or 0) + (n or 0)
    end
    if not found then error("no visible button '" .. tostring(button) .. "'" .. (args.widget and (" (widget " .. args.widget .. ")") or "")) end
    if not args.dry then
        local label = UI.allText(found, " ", 80)
        local name = UI.name(found)
        local flat = tostring(label or ""):lower():gsub("[%s_%-%.]", "")
        local w = A.saveLoadName(name) or A.saveLoadName(label) or (A.SAVELOAD_LABELS[flat] and flat)
            or (page == "MainMenu" and flat == "continue" and "continue")
        if w then refuseSaveLoad("ui.press " .. tostring(label or name or button) .. " refused (" .. w .. ")") end
        if A.SAVELOAD_PAGES[page] and not (A.leaveName(name) or A.leaveName(label)) then
            refuseSaveLoad("ui.press " .. tostring(label or name or button) .. " refused: the " .. page .. " page is open")
        end
    end
    local ok, how = A.pressButton(found, args.dry)
    check(ok, how)
    return { done = not args.dry, page = page, via = how, matches = total }
end)

------------------------------------------------------------------------
-- The right-click context menu (WorldContextMenu.xaml). Its LSEntityObject
-- "WorldContextEntity" opens an ls.ContextMenu popup, outside the widget's
-- visual tree, with one ls.ContextMenuItem row per entry the engine offers
-- for the right-clicked entity (Talk, Pickpocket, Lockpick, Attack, Examine;
-- a crime shows red). Each row has a Command, but pressing it does nothing
-- (live 13:3x: the menu stayed open): the bridge moves the cursor onto the
-- row (closed loop on its IsMouseOver) and clicks, as a player does.
--
--   ui.context_menu {item?} -> {open, items = {texts}, match?, over?}
--     item: an entry's label (case-insensitive prefix, e.g. "Lockpick");
--     match = its row number, over = the cursor is on that row now.
------------------------------------------------------------------------

function A.contextMenuRows()
    local wm = UI.widgetMap().WorldContextMenu
    local ent = wm and UI.byName(wm.el, "WorldContextEntity", { hidden = true })
    local cm = ent and UI.get(ent, "ContextMenu")
    if not cm or UI.get(cm, "IsOpen") ~= true then return nil end
    local rows = {}
    for _, e in ipairs(UI.findAll(cm, function(e)
        return (UI.typeName(e) or ""):find("ContextMenuItem", 1, true) ~= nil
    end, { hidden = true, prune = true })) do
        local text = UI.allText(e, " ", 80)
        if text then rows[#rows + 1] = { el = e, text = text } end
    end
    return rows
end

RPC.register("ui.context_menu", function(args)
    local rows = A.contextMenuRows()
    if not rows then
        if args.item then error("the context menu is not open") end
        return { open = false }
    end
    local out = { open = true, items = {} }
    local want = args.item and tostring(args.item):lower()
    for i, r in ipairs(rows) do
        out.items[#out.items + 1] = r.text
        if want and not out.match and r.text:lower():sub(1, #want) == want then
            out.match, out.over = i, UI.get(r.el, "IsMouseOver") == true
        end
    end
    if want and not out.match then
        error("no '" .. tostring(args.item) .. "' entry in the context menu (" .. table.concat(out.items, ", ") .. ")")
    end
    return out
end)

------------------------------------------------------------------------
-- Confirmation boxes (ls.DCMessageBox in "Dialog_box": "End the day?",
-- "Someone wants to talk, rest anyway?", "Leave party members behind?").
-- The box's title can read "[ForceUpdate]" (an unresolved handle), so the
-- answer is matched on the buttons, never on the question (live S005).
--
--   ui.confirm {answer?="yes"|"no"|<label>|<n>, dry?} -> job
--     settles when the box closes or shows a new question:
--     {pressed, title?, text?, buttons, next?={title,text,buttons}}
------------------------------------------------------------------------

A.CONFIRM_WORDS = {
    yes = { "yes", "ok", "confirm", "accept", "continue", "proceed" },
    no = { "no", "cancel", "back", "close" },
}
A.CONFIRM_WORDS.ok = A.CONFIRM_WORDS.yes
A.CONFIRM_WORDS.confirm = A.CONFIRM_WORDS.yes
A.CONFIRM_WORDS.cancel = A.CONFIRM_WORDS.no

local function trim(s) return (tostring(s or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")) end

local function isBoxName(name)
    local n = name:lower()
    return n == "dialog_box" or n:find("messagebox", 1, true) ~= nil or n:find("confirm", 1, true) ~= nil
end

-- The open confirmation box: { name, el, title, text, buttons = { {el, label, name} } } or nil.
function A.confirmBox()
    for _, rec in ipairs(UI.widgets()) do
        if isBoxName(rec.name) and UI.isVisible(rec.el) then
            local box = { name = rec.name, el = rec.el, buttons = {} }
            local p = S.popup(rec.el)
            box.title, box.text = p.title, p.text
            for _, b in ipairs(UI.findAll(rec.el, UI.isButton, { prune = true })) do
                local label = UI.allText(b, " ", 120)
                local name = UI.name(b)
                if (label and label ~= "") or name then
                    box.buttons[#box.buttons + 1] = { el = b, label = label or name, name = name }
                end
            end
            if #box.buttons > 0 then return box end
        end
    end
    return nil
end

-- Which button answers `answer` (pure; tested offline): exact label/name for
-- each synonym in order, then a word at the start of the label (len >= 3),
-- then BG3's layout (affirmative first, negative last) for yes/no.
function A.pickConfirm(buttons, answer)
    answer = trim(answer ~= nil and answer or "yes")
    local n = tonumber(answer)
    if n then return buttons[n] end
    local words = A.CONFIRM_WORDS[answer] or { answer }
    local function nm(b) return trim(b.name):gsub("btn$", ""):gsub("button$", "") end
    for _, w in ipairs(words) do
        for _, b in ipairs(buttons) do
            if trim(b.label) == w or nm(b) == w then return b end
        end
    end
    for _, w in ipairs(words) do
        if #w >= 3 then
            for _, b in ipairs(buttons) do
                local l = trim(b.label)
                if l:sub(1, #w) == w or l:find(w, 1, true) then return b end
            end
        end
    end
    if #buttons >= 2 and A.CONFIRM_WORDS[answer] == A.CONFIRM_WORDS.yes then return buttons[1] end
    if #buttons >= 2 and A.CONFIRM_WORDS[answer] == A.CONFIRM_WORDS.no then return buttons[#buttons] end
    return nil
end

-- The stack-split box (SplitItem.xaml: "how many?" slider over CurrentPlayer.UIData.SplitItemData.TargetCount
-- 1..ItemCount, buttons Cancel / ConfirmSplitButton). ui.state calls it a POPUP and says "answer with confirm",
-- but confirmBox() only knew question boxes: S028 15:51 confirm("Confirm") failed "no confirmation box is open"
-- (the agent screenshotted and clicked the arrow + Confirm), and a stale one left from a closed trade blocked
-- two long_rest calls ("the rest screen didn't open", nothing named). Not part of confirmBox(): the ops'
-- automatic answers (handleConfirm) must never confirm a quantity; only the agent's confirm() answers it.
function A.splitBox()
    local w = UI.widget("SplitItem")
    if not (w and UI.isVisible(w)) then return nil end
    local box = { name = "SplitItem", el = w, buttons = {}, split = true }
    local data = UI.path(try(function() return w.DataContext end), "CurrentPlayer.UIData.SplitItemData")
    box.data = data
    box.max = tonumber(UI.prop(data, "ItemCount"))
    box.count = tonumber(UI.prop(data, "TargetCount"))
    local item = UI.prop(data, "Item")
    box.title = item and UI.prop(item, "Name") and tostring(UI.prop(item, "Name")) or "split a stack"
    box.text = "how many? " .. tostring(box.count or "?") .. "/" .. tostring(box.max or "?")
    for _, b in ipairs(UI.findAll(w, UI.isButton, { prune = true })) do
        local label = UI.allText(b, " ", 120)
        local name = UI.name(b)
        if name == "ConfirmSplitButton" then label = label ~= nil and label ~= "" and label or "Confirm" end
        if (label and label ~= "") or name then
            box.buttons[#box.buttons + 1] = { el = b, label = label or name, name = name }
        end
    end
    if #box.buttons == 0 then return nil end
    return box
end

-- A split box nobody is using any more (its trade screen is gone): Cancel it, as a player closes it before
-- going on. -> the item name when one was cancelled.
function A.cancelStaleSplit()
    local box = A.splitBox()
    if not box then return nil end
    local tr = UI.widget("Trade")
    if tr and UI.isVisible(tr) then return nil end
    local b = A.pickConfirm(box.buttons, "cancel")
    if b and A.pressButton(b.el, false) then return box.title end
    return nil
end

local function boxView(box)
    local labels = {}
    for _, b in ipairs(box.buttons) do labels[#labels + 1] = b.label end
    return { title = box.title, text = box.text, buttons = labels }
end

-- Press the button for `answer` on the open box (a question box, else a stack-split box; amount = how many
-- to take from the stack before Confirm). -> ok, view|err
function A.answerConfirm(answer, dry, amount)
    local box = A.confirmBox() or A.splitBox()
    if not box then return false, "no confirmation box is open" end
    if box.split and amount ~= nil then
        local n = math.floor(tonumber(amount) or 0)
        if n < 1 or (box.max and n > box.max) then
            return false, "amount " .. tostring(amount) .. " is out of range (1-" .. tostring(box.max or "?") .. ")"
        end
        if not dry then
            local ok, err = pcall(box.data.SetProperty, box.data, "TargetCount", n)
            if not ok then return false, "couldn't set the amount: " .. tostring(err) end
        end
    end
    local b = A.pickConfirm(box.buttons, answer)
    local view = boxView(box)
    if not b then
        return false, "no button matches '" .. tostring(answer) .. "' (buttons: " .. table.concat(view.buttons, ", ") .. ")"
    end
    local ok, how = A.pressButton(b.el, dry)
    if not ok then return false, "couldn't press " .. tostring(b.label) .. ": " .. tostring(how) end
    view.pressed, view.via = b.label, how
    return true, view
end

RPC.register("ui.confirm", function(args)
    local ok, view = A.answerConfirm(args.answer, args.dry, args.amount)
    check(ok, view)
    if args.dry then return view end
    local sig0 = table.concat({ view.title or "", view.text or "", table.concat(view.buttons, "|") }, "#")
    return waitJob("ui.confirm", 4000, function(job)
        if RPC.now_ms() - job.started < 150 then return nil end
        local box = A.confirmBox() or A.splitBox()
        if not box then return "done", view end
        local v = boxView(box)
        if table.concat({ v.title or "", v.text or "", table.concat(v.buttons, "|") }, "#") ~= sig0 then
            view.next = v
            return "done", view
        end
        if RPC.now_ms() - job.started > 1500 then
            -- Same-looking box: either a second question with the same buttons (the
            -- rest flow asks twice) or the press didn't land; the caller looks again.
            view.next, view.still_open = v, true
            -- live S013: pressed=Yes via=command still_open, Yes and No alike; the bridge clicks it
            if tostring(view.via) == "command" then view.needs_click = args.answer or "yes" end
            return "done", view
        end
    end)
end)

-- ui.confirm_probe {answer?="yes"} -> {box=false} | {box=true, over, label, buttons, title?, text?}
-- Is the cursor over the button that answers `answer`? The bridge's hover-checked click on a box whose
-- button ignores its Command (boxclick.py) moves the cursor, probes, and clicks only when over = true.
RPC.register("ui.confirm_probe", function(args)
    local box = A.confirmBox() or A.splitBox()
    if not box then return { box = false } end
    local b = A.pickConfirm(box.buttons, args.answer)
    local view = boxView(box)
    view.box = true
    if not b then
        view.error = "no button matches '" .. tostring(args.answer) .. "'"
        return view
    end
    view.label = b.label
    view.over = UI.prop(b.el, "IsMouseOver") == true
    -- the cursor over another button of the box (which side to search)
    for i, o in ipairs(box.buttons) do
        if o ~= b and UI.prop(o.el, "IsMouseOver") == true then view.over_other = i end
    end
    return view
end)

-- ui.box_open -> {state, box}: is a question (or stack-split) box still showing? For the bridge's watch after
-- its click (boxclick.py), which must not touch the box: all three game crashes (S072/S077/S078, impl-setup S17)
-- came while confirm_probe walked the buttons + IsMouseOver of a box the rest / travel transition was tearing
-- down. Reads the game state, and only while Running/Paused the top-level widgets' visibility (what ui.state's
-- screen reads): never a box's children, never IsMouseOver.
RPC.register("ui.box_open", function()
    local gs = tostring(try(Ext.Utils.GetGameState) or "?")
    if gs ~= "Running" and gs ~= "Paused" then return { state = gs, box = false } end
    for _, rec in ipairs(UI.widgets()) do
        if (isBoxName(rec.name) or rec.name == "SplitItem") and UI.isVisible(rec.el) then
            return { state = gs, box = true }
        end
    end
    return { state = gs, box = false }
end)

------------------------------------------------------------------------
-- Dialogue
------------------------------------------------------------------------

local function dialogueSnapshot()
    local wmap = UI.widgetMap()
    local screen = nil
    for _, pair in ipairs(S.SCREENS) do
        local w = wmap[pair[1]]
        if w and UI.isVisible(w.el) then screen = pair[2] break end
    end
    local d = (wmap.Dialogue and UI.isVisible(wmap.Dialogue.el)) and S.dialogue(wmap) or nil
    return screen or "game", d, wmap
end

local function sig(d)
    if not d then return "none" end
    return table.concat({ d.speaker or "", d.text or "", d.options and #d.options or 0,
        d.options and d.options[1] and d.options[1].text or "", tostring(d.continue) }, "|")
end

-- Waits until the dialogue reaches a decision point after an action:
--   options present (and different from before), a continue prompt, another
--   modal (roll/trade/reaction/popup...), or the dialogue ended.
-- opts.skip: press SPACE to skip voiced lines while waiting (untested).
local function settleDialogue(kind, before, opts)
    opts = opts or {}
    return waitJob(kind, opts.timeout_ms or 45000, function(job)
        -- Don't touch the dialogue until the chosen node has been processed
        -- and re-rendered (see S.dialogueAnswers SAFETY note).
        if RPC.now_ms() - job.started < 250 or UI.frame - (job.frame0 or UI.frame) < 3 then return nil end
        local screen, d = dialogueSnapshot()
        if d and d.changing then return nil end
        local s = sig(d)
        if d and d.text and s ~= job.last_sig then
            job.last_sig = s
            local key = (d.speaker or "") .. "|" .. d.text
            if key ~= job.last_line then
                job.last_line = key
                job.lines[#job.lines + 1] = { speaker = d.speaker, text = d.text }
            end
        end
        job.partial = { screen = screen, lines = job.lines, dialogue = d }
        -- Still on the page we are leaving (e.g. the roll panel closing).
        if opts.leaving and screen == opts.leaving then return nil end
        if screen ~= "dialogue" then
            if not d then return "done", { screen = screen, ended = screen == "game" or nil, lines = job.lines } end
            return "done", { screen = screen, lines = job.lines, dialogue = d }
        end
        -- Settle on a new decision point only: fresh options, or a continue
        -- prompt on a line we haven't seen before the action (the old line
        -- lingers for a frame or two while the answer is processed).
        if d and s ~= before and d.options and d.text then
            return "done", { screen = "dialogue", lines = job.lines, dialogue = d }
        end
        if d and d.continue and d.text and d.text ~= job.before_text then
            return "done", { screen = "dialogue", lines = job.lines, dialogue = d }
        end
        if opts.skip and d and d.listening and RPC.now_ms() > (job.next_skip or 0) then
            job.next_skip = RPC.now_ms() + 700
            pcall(Ext.Input.InjectKeyPress, "SPACE")
        end
    end, function(job)
        -- Snapshot taken by the caller BEFORE executing (no reads after it).
        job.lines = {}
        job.last_sig = before
        job.frame0 = UI.frame
        job.before_text = opts.before_text
        if opts.before_text then job.last_line = (opts.before_speaker or "") .. "|" .. opts.before_text end
    end)
end
A.settleDialogue = settleDialogue

-- an answer's text for comparisons: tags, case, punctuation and spacing ignored. Pure.
function A.answerKey(text)
    local body = tostring(text or ""):gsub("^%s*%[[^%]]*%]%s*", "")
    return (body:lower():gsub("[%s%p]+", " "):gsub("^ ", ""):gsub(" $", ""))
end

-- ui.dialogue_choose {i, text?, dry?, skip?}
--   i = option number from ui.state (1..n); i = 0 or "continue" advances a
--   line without options. Returns a job; its result is
--   {screen, lines:[{speaker,text}], dialogue?:{speaker,text,options}, ended?}.
RPC.register("ui.dialogue_choose", function(args)
    local wmap = UI.widgetMap()
    local w = wmap.Dialogue or error("no dialogue on screen")
    local dc = try(function() return w.el.DataContext end)
    local cur0 = S.dialogue(wmap)
    local before = sig(cur0)
    local i = args.i
    if i == 0 or i == "continue" then
        local cur = S.dialogue(wmap)
        if cur and cur.options then error("options are shown; choose 1.." .. #cur.options) end
        local ad = S.activeDialogue(dc)
        local cont = S.continueVM(ad)
        if cont then
            check(UI.command(dc, "SelectAnswerCommand", cont, args.dry))
        elseif not args.dry then
            Ext.Input.InjectKeyPress("SPACE")
        end
    else
        i = tonumber(i) or error("i must be an option number")
        local answers, changing, counts = S.dialogueAnswers(w.el, S.activeDialogue(dc))
        if changing then
            error(("dialogue is changing (%s answers rendered, %s in the game, %s with text, ShowAnswers=%s); read ui.state again"):format(
                tostring(counts and counts.rendered), tostring(counts and counts.vms),
                tostring(counts and counts.texted), tostring(counts and counts.show)))
        end
        local a
        for _, x in ipairs(answers) do
            if x.i == i then a = x break end
        end
        local first, last = math.huge, 0
        for _, x in ipairs(answers) do
            if x.i < first then first = x.i end
            if x.i > last then last = x.i end
        end
        if #answers == 0 then first = 1 end
        local below, above = counts and counts.below or 0, counts and counts.above or 0
        local hidden = not a and ((i > last and i <= last + below) or (i < first and i >= first - above))
        if hidden and counts.by_position then
            -- An answer outside the visible rows of a long list (companion menus): the live VM at that
            -- position in Answers. Only when the list's scroll extent counts exactly the VMs (by_position):
            -- with a hidden text-less VM (S015: 7 VMs, 6 shown) or rows-only answers, the i-th VM is not
            -- the i-th row (audit UI-4: SelectAnswerCommand on the wrong answer, reported as success).
            local vms = UI.prop(S.activeDialogue(dc), "Answers")
            local vm = vms and try(vms.At, vms, i)
            if vm then a = { i = i, vm = vm, off = UI.prop(vm, "Enabled") == false } end
        elseif hidden then
            -- the row is not rendered: the bridge wheels it into view and asks again
            error(("option %d is not among the visible rows %d-%d of %d (needs_scroll)"):format(
                i, first, last, last + below), 0)
        end
        a = a or error("no option " .. i .. " (" .. #answers .. " shown"
            .. (below > 0 and (", " .. below .. " more below") or "") .. ")")
        if a.off then error("option " .. i .. " is disabled") end
        -- text: the option's text as the agent read it (the bridge passes it after wheeling the list); the row
        -- pressed must still read it
        if args.text and a.text and A.answerKey(a.text) ~= A.answerKey(args.text) then
            error(("option %d reads '%s', not '%s' (the list changed); read the dialogue again"):format(
                i, tostring(a.text), tostring(args.text)), 0)
        end
        local ok, err = false, "no answer VM (rendered row only)"
        if a.vm then ok, err = UI.command(dc, "SelectAnswerCommand", a.vm, args.dry) end
        if not ok then
            -- Fallback: the answer's own button (same command and parameter); a.row = its row in the window.
            local item = UI.findAll(UI.byName(w.el, "answerList") or w.el, function(e)
                return UI.typeName(e) == "ls.LSListBoxItem" or UI.typeName(e) == "ListBoxItem"
            end, { prune = true })[a.row or i]
            local btn = item and UI.find(item, UI.isButton)
            check(btn and UI.press(btn, args.dry), err)
        end
    end
    if args.dry then return { can = true } end
    return settleDialogue("ui.dialogue_choose", before, { skip = args.skip, timeout_ms = args.timeout_ms,
        before_text = cur0 and cur0.text, before_speaker = cur0 and cur0.speaker })
end)

-- File-local helpers the other parts use.
A.inner = { try = try, check = check, waitJob = waitJob, dialogueSnapshot = dialogueSnapshot, sig = sig,
    settleDialogue = settleDialogue }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads A and the
-- helpers in A.inner.
for _, part in ipairs({ "Client/UIActionsPrompts.lua", "Client/UIActionsMenu.lua" }) do
    Ext.Require(part)
end

return A
