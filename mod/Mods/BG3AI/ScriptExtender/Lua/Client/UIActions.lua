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

-- Press a button element: its command, or (string parameter) its bound key.
function A.pressButton(el, dry)
    local ok, err = UI.press(el, dry)
    if ok then return true, "command" end
    local key = A.BOUND_KEYS[tostring(UI.get(el, "BoundEvent") or "")]
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

-- The open confirmation box: { name, el, title, text, buttons = { {el, label, name} } } or nil.
function A.confirmBox()
    for _, rec in ipairs(UI.widgets()) do
        local n = rec.name:lower()
        if (n == "dialog_box" or n:find("messagebox", 1, true) or n:find("confirm", 1, true))
            and UI.isVisible(rec.el) then
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

-- ui.dialogue_choose {i, dry?, skip?}
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
        local a = answers[i]
        if not a and counts and counts.below and i > #answers and i <= #answers + counts.below then
            -- An answer below the visible rows of a long list (companion menus): the live VM at that
            -- position in Answers, the order the list shows them in.
            local vms = UI.prop(S.activeDialogue(dc), "Answers")
            local vm = vms and try(vms.At, vms, i)
            if vm then a = { i = i, vm = vm, off = UI.prop(vm, "Enabled") == false } end
        end
        a = a or error("no option " .. i .. " (" .. #answers .. " shown"
            .. (counts and counts.below and (", " .. counts.below .. " more below") or "") .. ")")
        if a.off then error("option " .. i .. " is disabled") end
        local ok, err = UI.command(dc, "SelectAnswerCommand", a.vm, args.dry)
        if not ok then
            -- Fallback: the answer's own button (same command and parameter).
            local item = UI.findAll(UI.byName(w.el, "answerList") or w.el, function(e)
                return UI.typeName(e) == "ls.LSListBoxItem" or UI.typeName(e) == "ListBoxItem"
            end, { prune = true })[i]
            local btn = item and UI.find(item, UI.isButton)
            check(btn and UI.press(btn, args.dry), err)
        end
    end
    if args.dry then return { can = true } end
    return settleDialogue("ui.dialogue_choose", before, { skip = args.skip, timeout_ms = args.timeout_ms,
        before_text = cur0 and cur0.text, before_speaker = cur0 and cur0.speaker })
end)

------------------------------------------------------------------------
-- Active roll
--
-- ui.roll {boosts?:[i], reroll?:bool, close?:bool, dry?}
--   phase ready  : add the listed boosts (indexes into roll.boosts from
--                  ui.state), then roll. Job ends at the result.
--   phase result : reroll=true spends Inspiration (RollAgainCommand) and
--                  waits for the new result; otherwise Continue closes the
--                  panel and the job settles like dialogue_choose.
------------------------------------------------------------------------

local function rollWidget()
    local w = UI.widget("ActiveRoll")
    return w, w and try(function() return w.DataContext end)
end

local function rollPhase(dc)
    local st = tostring(UI.prop(dc, "RollState"))
    return st
end

-- A start command sent while the panel is still opening is dropped: live 2026-10-10 a lockpick roll_check ~1 s
-- after the roll screen appeared sat in WaitForStart for 20 s (timeout); the same command 2 s later rolled.
-- So while the roll has not started, the start command is sent again every ROLL_RETRIGGER_MS.
local ROLL_RETRIGGER_MS = 1200
local function startRoll(dc)
    local ok, err = UI.command(dc, "UserTriggeredNextPhaseCommand", nil)
    if not ok then ok, err = UI.command(dc, "TriggerNextPhaseCommand", nil) end
    return ok, err
end

-- The roll's state timeline (ms since the call) for the bridge's metrics log: where do the 7-10 s of a dialogue
-- roll_check go (S010-S027: ~6.8 s from the start command to ResultReady, every time).
local function notePhase(j, st)
    if st ~= j.last_state then
        j.last_state = st
        j.phases = (j.phases and (j.phases .. " ") or "") .. st .. "@" .. (RPC.now_ms() - (j.t0 or RPC.now_ms()))
    end
end

-- waitRollResult(kind, retrigger, reroll): done at ResultReady. WaitForReRoll is NOT a result: after
-- RollAgainCommand (Inspiration) the panel waits there for the dice to be thrown again, like WaitForStart
-- (ActiveRoll.xaml: same boost list / tools enabled). S013-S026: 7/7 reroll_on_fail calls returned the first
-- (failed) result again, because ResultReady was still showing the frame after the command and WaitForReRoll
-- counted as done; the bridge then pressed Continue on the failure.
-- reroll=true: the old result must go away first (state leaves ResultReady); if it never does within
-- ROLL_REROLL_LEAVE_MS, the game ignored the command: done with reroll_ignored.
local ROLL_REROLL_LEAVE_MS = 3000
local function waitRollResult(kind, retrigger, reroll)
    return waitJob(kind, 20000, function(j)
        local w, dc = rollWidget()
        if not w then return "done", { closed = true, phases = j.phases } end
        local st = rollPhase(dc)
        notePhase(j, st)
        if st ~= "ResultReady" then j.left_result = true end
        if st == "ResultReady" and (j.left_result or not reroll) then
            local wmap = UI.widgetMap()
            return "done", { roll = S.roll(wmap), retriggered = (j.retriggered or 0) > 0 and j.retriggered or nil,
                rerolled = reroll and true or nil, phases = j.phases }
        end
        if st == "ResultReady" and RPC.now_ms() - j.t0 >= ROLL_REROLL_LEAVE_MS then
            return "done", { roll = S.roll(UI.widgetMap()), reroll_ignored = true, phases = j.phases }
        end
        local waiting = st == "WaitForStart" or st == "WaitForReRoll"
        if (retrigger or reroll) and (waiting or st == "IntroductionAnimation")
                and RPC.now_ms() - (j.triggered_at or 0) >= ROLL_RETRIGGER_MS then
            -- After a reroll the dice wait to be thrown: throw them at once (no 1.2 s wait for the first one).
            if waiting and (j.triggered_at or st == "WaitForReRoll") then
                startRoll(dc)
                if st == "WaitForStart" or j.rethrown then j.retriggered = (j.retriggered or 0) + 1 end
                if st == "WaitForReRoll" then j.rethrown = true end
            end
            j.triggered_at = RPC.now_ms()
        end
    end, function(j)
        j.t0 = RPC.now_ms()
        if not reroll then j.triggered_at = j.t0 end
    end)
end
A.ROLL_REROLL_LEAVE_MS = ROLL_REROLL_LEAVE_MS

-- A boost by its 1-based index in roll.boosts or by name ("Guidance", case-insensitive; the bridge sends names:
-- S024 roll_check(boosts="Guidance") failed "attempt to concatenate a nil value" from tonumber("Guidance")).
local function findBoost(dc, want)
    local i = tonumber(want)
    if i then return UI.path(dc, "BoostModifierList." .. i), i end
    local key = tostring(want):lower()
    local names = {}
    for n = 1, 12 do
        local vm = UI.path(dc, "BoostModifierList." .. n)
        if not vm then break end
        local name = UI.prop(vm, "Name") or UI.prop(vm, "Description")
        name = name and tostring(name) or ""
        names[#names + 1] = name
        if name:lower() == key or name:lower():find(key, 1, true) then return vm, n end
    end
    return nil, nil, (#names > 0 and ("offered: " .. table.concat(names, ", ")) or "the roll offers no boosts")
end

RPC.register("ui.roll", function(args)
    local w, dc = rollWidget()
    if not w then error("no active roll on screen") end
    local st = rollPhase(dc)
    if st == "WaitForStart" or st == "IntroductionAnimation" or st == "WaitForReRoll" then
        local added = {}
        for _, bi in ipairs(args.boosts or {}) do
            local boost, idx, offered = findBoost(dc, bi)
            if not boost then error("no boost '" .. tostring(bi) .. "' on this roll (" .. tostring(offered) .. ")") end
            -- XAML passes a PassThrough multi-binding here; the VM itself is
            -- the best available parameter on bg3le (untested live).
            local ok, err = UI.command(dc, "AddModifierCommand", boost, args.dry)
            if not ok then error("boost " .. tostring(bi) .. ": " .. tostring(err)) end
            added[#added + 1] = idx
        end
        local ok, err = UI.command(dc, "UserTriggeredNextPhaseCommand", nil, args.dry)
        if not ok then check(UI.command(dc, "TriggerNextPhaseCommand", nil, args.dry), err) end
        if args.dry then return { can = true } end
        return waitRollResult("ui.roll", true)
    elseif st == "ResultReady" then
        if args.reroll then
            check(UI.command(dc, "RollAgainCommand", nil, args.dry))
            if args.dry then return { can = true } end
            return waitRollResult("ui.roll", false, true)
        end
        local _, d0 = dialogueSnapshot()
        local before = sig(d0)
        local ok, err = UI.command(dc, "ContinueCommand", nil, args.dry)
        if not ok then check(UI.command(dc, "CancelCommand", nil, args.dry), err) end
        if args.dry then return { can = true } end
        return settleDialogue("ui.roll", before, { timeout_ms = 30000, leaving = "roll",
            before_text = d0 and d0.text, before_speaker = d0 and d0.speaker })
    end
    error("roll is animating (" .. st .. "); poll ui.state")
end)

------------------------------------------------------------------------
-- Reaction prompt
--
-- ui.react {use, which?, dry?}
--   use=false: "Don't react" (InterruptDontReactPlayerCommand(event)).
--   use=true : which = option number from ui.state reaction.options selects
--              that reaction's toggle first; then "React"
--              (InterruptAcceptPlayerCommand(event)).
------------------------------------------------------------------------

RPC.register("ui.react", function(args)
    local w = UI.widget("ReactionDecisionPopup") or error("no reaction prompt on screen")
    local dc = try(function() return w.DataContext end)
    local ev = UI.path(dc, "Data.InterruptEvents.1") or error("no pending interrupt")
    if args.use == false then
        check(UI.command(dc, "InterruptDontReactPlayerCommand", ev, args.dry))
    else
        if args.which then
            -- ReactButton toggles, in display order; their CommandParameter
            -- is the PassThrough (decision, interruptor) object.
            local btns = UI.findAll(w, function(e) return UI.name(e) == "ReactButton" end, { prune = true })
            local b = btns[tonumber(args.which)] or error("no reaction option " .. tostring(args.which) .. " (" .. #btns .. " shown)")
            if UI.prop(b, "IsChecked") ~= true then check(UI.press(b, args.dry)) end
        end
        check(UI.command(dc, "InterruptAcceptPlayerCommand", ev, args.dry))
    end
    if args.dry then return { can = true } end
    local before = UI.count(UI.path(dc, "Data.InterruptEvents"))
    return waitJob("ui.react", 10000, function()
        local w2 = UI.widget("ReactionDecisionPopup")
        local d2 = w2 and try(function() return w2.DataContext end)
        local n = d2 and UI.count(UI.path(d2, "Data.InterruptEvents")) or 0
        if n < before or n == 0 then
            local wmap = UI.widgetMap()
            return "done", { pending = n > 0 and n or nil, reaction = n > 0 and S.reaction(wmap) or nil }
        end
    end)
end)

------------------------------------------------------------------------
-- Loot
--
-- ui.loot {all?:true, slots?:[i], close?:bool, dry?}
--   all  : "Take all" (TakeAllAndCloseCommand.ActionCommand); job waits for
--          the container panel to close.
--   slots: take single slots by the index ui.state container.slots reports
--          (TakeItemCommand / PickUpCommand with the item VM; untested).
--   close: CloseContainerCommand, falling back to ESCAPE while the panel is
--          still visible (verified needed by act on an emptied corpse).
--   For items by uuid use the server action act.pickup: bg3le exposes no
--   names or entity ids on the container's item view models.
------------------------------------------------------------------------

local function containerSlotItem(dc, want)
    local i = 0
    for _, cont in UI.items(UI.path(dc, "CurrentPlayer.ContainerInventoryList"), 4) do
        for _, slot in UI.items(UI.path(cont, "LootInventory.Slots"), 120) do
            i = i + 1
            if i == want then return UI.props(slot).Object end
        end
    end
    return nil
end

RPC.register("ui.loot", function(args)
    local w = UI.widget("ContainerWidget") or error("no container open")
    local dc = try(function() return w.DataContext end)
    if args.all then
        local holder = UI.prop(dc, "TakeAllAndCloseCommand")
        local cmd = holder and UI.props(holder).ActionCommand
        if cmd then
            check(UI.execute(cmd, nil, args.dry))
        else
            local btn = UI.findButton(w, "TakeAll")
            check(btn and UI.press(btn, args.dry), "no Take All command or button")
        end
    elseif args.slots then
        local taken = {}
        for _, si in ipairs(args.slots) do
            local item = containerSlotItem(dc, tonumber(si)) or error("no item in slot " .. tostring(si))
            local ok, err = UI.command(dc, "TakeItemCommand", item, args.dry)
            if not ok then ok, err = UI.command(dc, "PickUpCommand", item, args.dry) end
            check(ok, "slot " .. si .. ": " .. tostring(err))
            taken[#taken + 1] = si
        end
        if args.dry then return { can = true } end
        if not args.close then return { taken = taken } end
        -- Closing: the command may be refused (e.g. an emptied corpse);
        -- the job falls back to ESCAPE below.
        UI.command(dc, "CloseContainerCommand", nil, false)
    elseif args.close then
        if args.dry then return { can = true } end
        UI.command(dc, "CloseContainerCommand", nil, false)
    else
        error("pass all=true, slots=[...] or close=true")
    end
    if args.dry then return { can = true } end
    -- Wait for the panel to close. If it is still visible after 600 ms
    -- (CloseContainerCommand refused or ignored; seen live on an emptied
    -- corpse), press ESCAPE, at most twice and only while the panel is up.
    local started = RPC.now_ms()
    return waitJob("ui.loot", 5000, function(job)
        local w2 = UI.widget("ContainerWidget")
        if not w2 or not UI.isVisible(w2) then
            return "done", { closed = true, via = job.escapes and "escape" or "command" }
        end
        local t = RPC.now_ms()
        job.escapes = job.escapes or 0
        if t - started > 600 and job.escapes < 2 and t - (job.last_esc or 0) > 800 then
            job.escapes = job.escapes + 1
            job.last_esc = t
            pcall(Ext.Input.InjectKeyPress, "ESCAPE")
        end
    end)
end)

------------------------------------------------------------------------
-- Menus
--
-- ui.menu {action, dry?}
--   open_menu / close_menu : Escape (game menu), checked against ui.state.
--   continue               : main menu "Continue" (loads the latest save).
--   load {name, owner?, index?, date?} : loads a named save (job, see MENU.load);
--                            without a name it only opens the Load page.
--   quickload              : QuickLoadCommand.
--   quicksave              : QuickSaveCommand.
--   save {name}            : named save, end to end (job, see MENU.save).
--   save_confirm {name?}   : continue a save after a shim key (needs_key).
--   Saving needs UIActions.SAVE_ENABLED, loading LOAD_ENABLED (both false:
--   the user's own save is loaded). dry=true only checks CanExecute.
------------------------------------------------------------------------

local function menuOpen()
    local w = UI.widget("GameMenu")
    return w ~= nil and UI.isVisible(w)
end

local MENU = {}

function MENU.open_menu(args)
    if menuOpen() then return { open = true } end
    if args.dry then return { can = true } end
    Ext.Input.InjectKeyPress("ESCAPE")
    return waitJob("ui.menu", 3000, function() if menuOpen() then return "done", { open = true } end end)
end

function MENU.close_menu(args)
    if not menuOpen() then return { open = false } end
    if args.dry then return { can = true } end
    Ext.Input.InjectKeyPress("ESCAPE")
    return waitJob("ui.menu", 3000, function() if not menuOpen() then return "done", { open = false } end end)
end

function MENU.continue(args)
    if not args.dry and not A.LOAD_ENABLED then error("loading is disabled (UIActions.LOAD_ENABLED=false)") end
    local dc = UI.dc("MainMenu") or error("not at the main menu")
    check(UI.command(dc, "ContinueGameCommand", nil, args.dry))
    return { done = not args.dry, can = true }
end

-- Load a named save, end to end (verified live, machine 2, S001-end).
-- The Load page (widget "LoadGame", gui::DCSavegames) lists saves grouped by
-- playthrough (an Expander per character name) newest first. bg3le can't
-- read a save's title (Run.Text is not marshalled) nor pass the Load
-- button's CustomEvent parameter, so:
--   1. OpenLoadGameDialog (in game or at the main menu)
--   2. expand the playthrough(s) named `owner` (folder prefix before "-")
--   3. select a save (LSListBoxItem.IsSelected): `index` (1 = newest of that
--      playthrough) first, then, if `date` is given, verify the info pane's
--      date ("9/10/2026 22:45") against it, scanning the others on mismatch
--   4. RETURN (= UIAccept = LoadSaveBtn); a confirm box gets RETURN too
--   5. done once the page closes or the game state leaves Running. The load
--      itself resets this Lua state; the caller waits for Running again.
-- args: name (folder), owner?, index?, date? = {y, m, d, H, M} (local time).
local function loadItems(owner)
    local w = UI.widget("LoadGame")
    if not w then return nil, {} end
    local want = owner and owner:lower()
    local out, matched = {}, {}
    for _, ex in ipairs(UI.findAll(w, function(e) return UI.typeName(e) == "Expander" end, { prune = true })) do
        local hdr = UI.allText(ex, " ", 80)
        hdr = hdr and hdr:match("^%s*(.-)%s*$") or ""
        if not want or hdr:lower() == want or hdr:lower():find(want, 1, true) == 1 then
            matched[#matched + 1] = ex
            if UI.prop(ex, "IsExpanded") ~= true then pcall(ex.SetProperty, ex, "IsExpanded", true) end
            for _, it in ipairs(UI.findAll(ex, function(e)
                return UI.typeName(e) == "ls.LSListBoxItem"
                    and UI.typeName(UI.prop(e, "DataContext")) == "ls.VMSavegame"
            end, { prune = true })) do
                out[#out + 1] = it
            end
        end
    end
    return w, out, matched
end

-- Info pane date of the selected save -> {a, b, y, H, M} or nil.
local function paneDate(w)
    local el = UI.find(w, function(e)
        return UI.name(e) == "Date" and UI.typeName(e) == "TextBlock"
    end)
    local s = el and UI.text(el)
    if not s then return nil end
    local a, b, y, H, M = s:match("(%d+)%D(%d+)%D(%d%d%d%d)%D+(%d+):(%d+)")
    if not a then return nil, s end
    H = tonumber(H)
    if s:find("[Pp]%.?[Mm]") and H < 12 then H = H + 12 end
    if s:find("[Aa]%.?[Mm]") and H == 12 then H = 0 end
    return { tonumber(a), tonumber(b), tonumber(y), H, tonumber(M) }, s
end

-- Pane date vs want {y,m,d,H,M}: either day/month order, +-1 minute.
local function dateMatches(p, want)
    if not p or not want then return false end
    if p[3] ~= tonumber(want.y) then return false end
    local m, d = tonumber(want.m), tonumber(want.d)
    if not ((p[1] == d and p[2] == m) or (p[1] == m and p[2] == d)) then return false end
    local a, b = p[4] * 60 + p[5], tonumber(want.H) * 60 + tonumber(want.M)
    return math.abs(a - b) <= 1
end

function MENU.load(args)
    if not args.dry and not A.LOAD_ENABLED then error("loading is disabled (UIActions.LOAD_ENABLED=false)") end
    local name = args.name
    if args.dry or not name or name == "" then
        if not UI.widget("LoadGame") or not UI.isVisible(UI.widget("LoadGame")) then
            check(UI.command(UI.dc("MainMenu") or UI.anyDC(), "OpenLoadGameDialog", nil, args.dry))
        end
        return { done = not args.dry, can = true, note = (not args.dry) and "load page opened (no save named)" or nil }
    end
    local owner = args.owner or tostring(name):match("^(.-)%-%d+__") or tostring(name):match("^([^%-_]+)")
    local index = tonumber(args.index) or 1
    local want = type(args.date) == "table" and args.date or nil
    local stage, since, tries, cand = "open", RPC.now_ms(), 0, nil
    local function enter(st) stage, since, tries = st, RPC.now_ms(), 0 end
    return waitJob("ui.menu.load", 30000, function(job)
        local now = RPC.now_ms()
        job.partial = { stage = stage, name = name }
        local st = tostring(try(Ext.Utils.GetGameState) or "")
        local page = UI.widget("LoadGame")
        local pageUp = page ~= nil and UI.isVisible(page)
        if stage == "open" then
            if pageUp then enter("select") return nil end
            if tries == 0 then
                tries = 1
                local ok, err = UI.command(UI.dc("MainMenu") or UI.anyDC(), "OpenLoadGameDialog", nil, false)
                if not ok then return "failed", { stage = stage, error = "could not open the load page: " .. tostring(err) } end
            elseif now - since > 4000 then
                return "failed", { stage = stage, error = "the load page did not open" }
            end
            return nil
        end
        if stage == "select" or stage == "verify" then
            if not pageUp then return "failed", { stage = stage, error = "the load page closed" } end
            local w, items, matched = loadItems(owner)
            if #matched == 0 then
                -- S025 08:35: failed 1 s after the page opened, the same save loaded on the next call: the
                -- playthrough list fills a few frames after the page shows. Wait like the save list below.
                if now - since > 3000 then return "failed", { stage = stage, error = "no playthrough named '" .. tostring(owner) .. "' on the load page (after 3s)" } end
                return nil
            end
            if #items == 0 then
                if now - since > 3000 then return "failed", { stage = stage, error = "no saves listed for '" .. tostring(owner) .. "'" } end
                return nil -- expanding: items appear on the next frames
            end
            if stage == "select" then
                -- candidates: the hinted index first, then the rest in order
                if not cand then
                    cand = { math.min(index, #items) }
                    for i = 1, #items do if i ~= cand[1] then cand[#cand + 1] = i end end
                    job.n_items = #items
                end
                local i = table.remove(cand, 1)
                if not i then return "failed", { stage = stage, error = "no save on the page matches the date of '" .. name .. "'", saves = #items } end
                pcall(items[i].SetProperty, items[i], "IsSelected", true)
                job.picked = i
                enter("verify")
                return nil
            end
            -- verify: the info pane follows the selection a frame or two later
            if not want then enter("accept") return nil end
            local p, raw = paneDate(w)
            job.pane = raw
            if dateMatches(p, want) then enter("accept") return nil end
            if now - since < 400 then return nil end
            enter("select")
            return nil
        end
        -- accept / confirm: RETURN until the page closes or the state changes
        if st ~= "Running" and st ~= "Menu" and st ~= "" then
            return "done", { loading = true, name = name, picked = job.picked, pane = job.pane, state = st }
        end
        local box = UI.widget("Dialog_box")
        local boxUp = box ~= nil and UI.isVisible(box)
        if not pageUp and not boxUp then
            if now - since > 300 then
                return "done", { loading = true, name = name, picked = job.picked, pane = job.pane, state = st }
            end
            return nil
        end
        if boxUp and stage ~= "confirm" then enter("confirm") end
        if now - since > 3000 then
            return "failed", { needs_key = "RETURN", stage = stage, name = name,
                error = "in-game RETURN did not load the save; send RETURN via the input shim" }
        end
        if tries < 2 and now - (job.lastKey or 0) > 1200 then
            tries = tries + 1
            job.lastKey = now
            pcall(Ext.Input.InjectKeyPress, "RETURN")
        end
        return nil
    end)
end

function MENU.quickload(args)
    if not args.dry and not A.LOAD_ENABLED then error("loading is disabled (UIActions.LOAD_ENABLED=false)") end
    check(UI.command(UI.anyDC(), "QuickLoadCommand", nil, args.dry))
    return { done = not args.dry, can = true }
end

function MENU.quicksave(args)
    if not args.dry and not A.SAVE_ENABLED then error("saving is disabled (UIActions.SAVE_ENABLED=false)") end
    check(UI.command(UI.anyDC(), "QuickSaveCommand", nil, args.dry))
    return { done = not args.dry, can = true }
end

-- Named save, end to end (untested live; written during S001).
--   1. OpenSaveGameDialogCommand            -> page "SaveGame"
--   2. RETURN = UIAccept = NewSaveBtn        -> page "SaveGamePopup"
--      (NewSaveBtn's command is CustomEvent "OpenNewSaveDialog", a string
--      parameter bg3le can't pass; with no save selected the button is
--      bound to UIAccept)
--   3. name -> LSTextBox "SaveName".Text and DC.SavegameNameInput (string
--      properties are boxed by bg3le, so no typing is needed)
--   4. RETURN = UIAccept = AcceptBtn         -> DoSaveWithName(SaveName.Text)
--   5. an overwrite prompt (Dialog_box) is confirmed
--   6. settles when SaveGamePopup/SaveGame/Dialog_box are gone and the
--      "Saving" indicator is off.
-- If an injected RETURN doesn't land within 2.5 s, the job ends with
-- {needs_key:"RETURN", stage}; the bridge sends RETURN through the input
-- shim and calls ui.menu{action:"save_confirm"} to continue.
A.pendingSave = A.pendingSave or nil

local function visible(name)
    local w = UI.widget(name)
    return w ~= nil and UI.isVisible(w), w
end

local function saveNameBox()
    local _, pop = visible("SaveGamePopup")
    if not pop then return nil, nil, "save name dialog not open" end
    local box = UI.byName(pop, "SaveName", { hidden = true })
    if not box then return nil, pop, "no SaveName box" end
    return box, pop
end

-- The box's Text property. Not UI.text: a TextBox's ToString() is its class name, so S056/S057 read
-- 'ls.LSTextBox' back and every save failed "the save name box keeps reading 'ls.LSTextBox'". nil = unreadable.
local function boxText(box)
    if box == nil then return nil end
    local v = UI.prop(box, "Text")
    if type(v) ~= "string" and UI.get then v = UI.get(box, "Text") end
    if type(v) ~= "string" then return nil end
    return v
end

local function saveNameNow()
    return boxText((saveNameBox()))
end

local function setSaveName(name)
    local box, pop, why = saveNameBox()
    if not box then return false, why end
    local ok = pcall(box.SetProperty, box, "Text", name)
    local dc = try(function() return pop.DataContext end)
    if dc then pcall(dc.SetProperty, dc, "SavegameNameInput", name) end
    local now = boxText(box)
    if not ok then return false, "could not set the name" end
    return true, now
end

-- S052 05:21:17: 'S052-lab-fight2' was written as '…__Steel Watch Foundry - Lab Level - 16h 14m': the name was set
-- once when the dialog appeared and RETURN followed; the dialog filled in its default name after us. Before each
-- RETURN the box must read our name (set again otherwise) and have held it SAVE_NAME_SETTLE_MS.
A.SAVE_NAME_SETTLE_MS = 150
A.SAVE_NAME_TRIES = 5

local function savingIndicator()
    local w = UI.widget("AlwaysOnTopOverlay")
    local panel = w and UI.byName(w, "SavingPanel", { hidden = true })
    return panel ~= nil and UI.isVisible(panel)
end

-- The client's own Save state (~1 s: the game is frozen meanwhile) is when a
-- save really happens: it starts 0.3-1 s after the name is accepted (live
-- S015/S017). The driver is done once the game has been through it and is
-- back; the SavingPanel overlay lingers ~3 s past it (S015: menu_s 4.5 s), and
-- finishing before it (S017: 0.1 s before) let the agent act into the freeze.
-- Seen from GameStateChanged (the job may not tick during Save) and polled.
A.SAVE_STATE_WAIT_MS = 3000  -- no Save state this long after the accept: done anyway
A.saveState = { started = 0, ended = 0 }
local function clientState()
    return tostring(try(Ext.Utils.GetGameState) or "")
end
pcall(function()
    Ext.Events.GameStateChanged:Subscribe(function(e)
        local from, to = tostring(e.FromState), tostring(e.ToState)
        if to == "Save" then A.saveState.started = RPC.now_ms() end
        if from == "Save" then A.saveState.ended = RPC.now_ms() end
    end)
end)

-- opts.probe: name check only (acceptance): open, set the name, read the box back after the settle, then ESCAPE
-- out without accepting; nothing is written. Result {probe=true, read=<box text>, matches=<read == name>}.
local function saveDriver(name, kind, opts)
    local probe = opts and opts.probe
    if not probe then A.pendingSave = { name = name, started = RPC.now_ms() } end
    local stage, stageSince, keyTries = nil, RPC.now_ms(), 0
    local t0 = RPC.now_ms()
    local ms = {}  -- stage -> ms since start (returned as result.ms: where a save's time goes)
    local function enter(st)
        if st ~= stage then
            stage, stageSince, keyTries = st, RPC.now_ms(), 0
            if ms[st] == nil then ms[st] = stageSince - t0 end
        end
    end
    return waitJob(kind, 30000, function(job)
        local now = RPC.now_ms()
        local popup = visible("SaveGamePopup")
        local page = visible("SaveGame")
        local box = visible("Dialog_box")
        job.partial = { stage = stage, name = name }
        local function esc()
            if now - (job.lastEsc or 0) > 900 then
                job.lastEsc = now
                job.escTries = (job.escTries or 0) + 1
                pcall(Ext.Input.InjectKeyPress, "ESCAPE")
            end
            if (job.escTries or 0) > 6 then
                if job.failRes then
                    job.failRes.error = job.failRes.error .. " (and the save menu did not close)"
                    return "failed", job.failRes
                end
                return "failed", { probe = true, stage = stage, read = job.probeRead, error = "the save menu did not close" }
            end
            return nil
        end
        -- S056 07:17: a failed name check left the save page open; the agent finished it by hand (ui_dump, RETURN
        -- x3) and the save landed later anyway. A failure closes our menus first, then reports.
        local function failClosed(res)
            job.failRes = res
            enter("fail_close")
            return esc()
        end
        if stage == "fail_close" then
            if popup or page or visible("GameMenu") then return esc() end
            A.pendingSave = nil
            return "failed", job.failRes
        end
        if stage == "probe_close" then
            if popup or page or visible("GameMenu") then return esc() end
            return "done", { probe = true, name = name, read = job.probeRead, matches = job.probeRead == name,
                renamed = job.renamed, ms = ms }
        end
        local function key(k)
            -- one in-game RETURN per stage (retried once); then ask the bridge
            if now - stageSince > 2500 then
                return "failed", { needs_key = k, stage = stage, name = name,
                    error = "in-game " .. k .. " did not advance '" .. tostring(stage) .. "'; send it via the input shim, then ui.menu{action:'save_confirm'}" }
            end
            if keyTries < 2 and now - (job.lastKey or 0) > 900 then
                keyTries = keyTries + 1
                job.lastKey = now
                pcall(Ext.Input.InjectKeyPress, k)
            end
        end
        if box and (stage == "accept" or stage == "overwrite") then
            enter("overwrite")
            -- "overwrite existing save?" -> the confirming button
            local w = UI.widget("Dialog_box")
            local btn = UI.findButton(w, "overwrite") or UI.findButton(w, "yes") or UI.findButton(w, "save")
                or UI.findButton(w, "ok") or UI.findButton(w, "confirm")
            if btn and keyTries == 0 then
                keyTries = 1
                local ok = A.pressButton(btn, false)
                if ok then return nil end
            end
            return key("RETURN")
        end
        if popup then
            if stage ~= "accept" then
                local ok, err = setSaveName(name)
                if not ok then return failClosed({ stage = "name", error = err }) end
                job.typed = err
                job.nameAt, job.nameSets = now, 1
                enter("accept")
                return nil
            end
            local cur = saveNameNow()
            if cur ~= nil and cur ~= name then
                if job.nameSets >= A.SAVE_NAME_TRIES then
                    return failClosed({ stage = "name", name = name, typed = cur,
                        error = "the save name box keeps reading '" .. tostring(cur) .. "' instead of '" .. name .. "'" })
                end
                setSaveName(name)
                job.nameAt, job.nameSets = now, job.nameSets + 1
                job.renamed = (job.renamed or 0) + 1
                stageSince = now   -- the RETURN timeout counts from the last good name
                return nil
            end
            if now - job.nameAt < A.SAVE_NAME_SETTLE_MS then return nil end
            if probe then
                job.probeRead = cur
                enter("probe_close")
                return esc()
            end
            return key("RETURN")
        end
        if page then
            if stage == "accept" or stage == "overwrite" or stage == "done" then
                -- back on the list after saving: close it
                enter("close")
                return key("ESCAPE")
            end
            enter("new")
            return key("RETURN")
        end
        -- no save UI visible
        if stage == nil then
            local ok, err = UI.command(UI.anyDC(), "OpenSaveGameDialogCommand", nil, false)
            if not ok then return "failed", { stage = "open", error = err } end
            enter("open")
            return nil
        end
        if stage == "open" then
            if now - stageSince > 4000 then return "failed", { stage = "open", error = "save page did not open" } end
            return nil
        end
        -- accept/overwrite/close passed and nothing is open: wait for the
        -- game's Save state to come and go (see A.saveState). The write itself
        -- is confirmed by the bridge on disk, in the background (S009: this
        -- floor was 600 ms of ~1.2 s; 250 ms still lets the game menu come
        -- back between pages).
        if clientState() == "Save" then
            job.sawSave = true
            enter("saving")
            return nil
        end
        local accepted = t0 + (ms.accept or ms.overwrite or (now - t0))
        local saved = job.sawSave or A.saveState.ended >= t0
        if not saved then
            if now - accepted < A.SAVE_STATE_WAIT_MS then
                if savingIndicator() then enter("saving") end
                return nil
            end
            job.noSaveState = true
            if ms.no_save_state == nil then ms.no_save_state = now - t0 end
        elseif ms.saved == nil then
            ms.saved = now - t0
        end
        if now - stageSince < 250 then return nil end
        -- leave the game menu (the save page returns to it) so the game is back
        if visible("GameMenu") then
            if not job.menuEsc or (now - job.menuEsc > 1200 and (job.menuTries or 0) < 2) then
                job.menuEsc = now
                job.menuTries = (job.menuTries or 0) + 1
                pcall(Ext.Input.InjectKeyPress, "ESCAPE")
            end
            return nil
        end
        A.pendingSave = nil
        ms.done = now - t0
        return "done", { saved = true, name = name, typed = job.typed, ms = ms,
            no_save_state = job.noSaveState, renamed = job.renamed }
    end)
end
A.saveDriver = saveDriver

function MENU.save(args)
    if not args.dry and not args.probe and not A.SAVE_ENABLED then error("saving is disabled (UIActions.SAVE_ENABLED=false)") end
    local name = args.name
    if args.dry then
        check(UI.command(UI.anyDC(), "OpenSaveGameDialogCommand", nil, true))
        return { can = true }
    end
    if args.probe then
        return saveDriver(tostring(name or "probe"):gsub("[^%w%-_ ]", "_"):sub(1, 40), "ui.menu.save", { probe = true })
    end
    if not name or name == "" then
        check(UI.command(UI.anyDC(), "OpenSaveGameDialogCommand", nil, false))
        return { done = true, note = "save page opened (no name given)" }
    end
    name = tostring(name):gsub("[^%w%-_ ]", "_"):sub(1, 40)
    return saveDriver(name, "ui.menu.save")
end

-- Continue a named save after the bridge sent a key via the input shim.
function MENU.save_confirm(args)
    if not A.SAVE_ENABLED then error("saving is disabled (UIActions.SAVE_ENABLED=false)") end
    local name = args.name or (A.pendingSave and A.pendingSave.name)
    if not name then error("no pending save; call ui.menu{action:'save', name}") end
    return saveDriver(name, "ui.menu.save")
end

RPC.register("ui.menu", function(args)
    local fn = MENU[args.action or ""]
    if not fn then
        local names = {}
        for k in pairs(MENU) do names[#names + 1] = k end
        table.sort(names)
        error("action must be one of " .. table.concat(names, ", "))
    end
    return fn(args)
end)

return A
