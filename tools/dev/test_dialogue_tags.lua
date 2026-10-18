-- Offline tests for Client/UIState.lua dialogue answers (worker se): skill/class
-- tags from the answer VMs (AnswerTags, CtxAnswer tag params), tag runs read
-- from the rendered inlines when ToString is the bare line, and the VM-only
-- fallback when the rendered list and Answers disagree (S007/S010: options on
-- screen for minutes, never reported). UI and RPC are stubbed with plain tables
-- shaped like the live objects (probed 2026-10-10: gui::VMDialogueAnswer has
-- CtxAnswer.Params and AnswerTags collections).
-- Run: tools/dev/test_dialogue_tags.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local function coll(items)
    return { Type = "ObservableCollection<T>", _items = items, At = function(self, i) return self._items[i] end }
end

local function clean(s)
    if type(s) ~= "string" then return nil end
    s = s:gsub("<[^>]->", ""):gsub("%s+", " "):gsub("^ ", ""):gsub(" $", "")
    if s == "" then return nil end
    return s
end

local function inlineText(inl)
    local parts = {}
    for _, r in ipairs(inl._items) do parts[#parts + 1] = r._props.Text or "" end
    return table.concat(parts)
end

local function findAll(root, pred, out)
    out = out or {}
    for _, c in ipairs(root._children or {}) do
        if pred(c) then out[#out + 1] = c else findAll(c, pred, out) end
    end
    return out
end

BG3AI = {
    RPC = { now_ms = function() return 0 end, register = function() end },
    UI = {
        props = function(o) return (o and o._props) or {} end,
        prop = function(o, k) return o and o._props and o._props[k] end,
        clean = clean,
        items = function(c, max)
            local i, n = 0, math.min(#c._items, max or 1e9)
            return function() i = i + 1 if i <= n then return i, c._items[i] end end
        end,
        count = function(c) return #c._items end,
        typeName = function(o) return o and o.Type end,
        byName = function(root, name)
            local hit = findAll(root, function(e) return e._name == name end)
            return hit[1]
        end,
        findAll = function(root, pred) return findAll(root, pred) end,
        find = function(root, pred) return findAll(root, pred)[1] end,
        get = function(o, k) return o and o._props and o._props[k] end,
        pick = function(o, names)
            local out = {}
            for _, k in ipairs(names) do if o and o._props and o._props[k] ~= nil then out[k] = o._props[k] end end
            return out
        end,
        text = function(el) return clean(el._tostring) or (el._props.Inlines and clean(inlineText(el._props.Inlines))) end,
        inlineText = inlineText,
        allText = function() return nil end,
        quiet = function() return QUIET == true end,
    },
}

dofile(LUA .. "/Client/UIState.lua")
local S = BG3AI.UIState

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end

local function run(text, tp) return { Type = "ls.LSRun", _props = { Text = text, Type = tp } } end

-- A rendered answer item; runs = the TextBlock inlines, tostring = what ToString returns.
local function item(runs, tostring)
    local tb = { Type = "TextBlock", _name = "AnswerText", _tostring = tostring, _props = { Inlines = coll(runs) } }
    return { Type = "ls.LSListBoxItem", _children = { tb } }
end

-- An answer VM: params = CtxAnswer.Params props, tags = AnswerTags items' props.
local function vm(params, tags, enabled)
    local ps = {}
    for i, p in ipairs(params) do ps[i] = { _props = p } end
    local ts = {}
    for i, t in ipairs(tags or {}) do ts[i] = { _props = t } end
    return { Type = "gui::VMDialogueAnswer", _props = {
        Enabled = enabled ~= false, AnswerIdx = 0,
        CtxAnswer = { Type = "ls.VMContextTransString", _props = { ParamsDirty = false, Params = coll(ps) } },
        AnswerTags = coll(ts),
    } }
end

local function widget(items)
    return { _children = { { _name = "answerList", _children = items } } }
end

local function ad(vms, show)
    return { _props = { Answers = coll(vms), ShowAnswers = show ~= false } }
end

-- 1. Tag runs in the inlines, ToString the bare line (S007 Lae'zel): tags come from the runs.
do
    local items = { item({ run("[PERSUASION] ", "Tag"), run("Shoot the trap down.") }, "Shoot the trap down.") }
    local vms = { vm({ { Type = "Tag", Text = "Persuasion" }, { Text = "Shoot the trap down." } }) }
    local out, changing = S.dialogueAnswers(widget(items), ad(vms))
    check("inline tags: not changing", changing == false)
    check("inline tags: one option", #out == 1)
    check("inline tags: tag", out[1] and out[1].tags and out[1].tags[1] == "PERSUASION", out[1] and table.concat(out[1].tags or {}, ","))
    check("inline tags: body", out[1] and out[1].text == "Shoot the trap down.", out[1] and out[1].text)
    check("inline tags: no duplicate from the VM", out[1] and #out[1].tags == 1)
end

-- 2. Rendered text has no tags at all (runs not generated yet): tags from AnswerTags + params.
do
    local items = {
        item({ run("Your quarry looks dangerous.") }, "Your quarry looks dangerous."),
        item({ run("Attack the tieflings.") }, nil),
    }
    local vms = {
        vm({ { Type = "Tag", Text = "Paladin" }, { Type = "Tag", Text = "Deception" }, { Text = "Your quarry looks dangerous." } },
           { { Text = "Paladin" } }),
        vm({ { Text = "Attack the tieflings." } }),
    }
    local out = S.dialogueAnswers(widget(items), ad(vms))
    check("vm tags: two options", #out == 2)
    local t = out[1] and out[1].tags or {}
    check("vm tags: PALADIN + DECEPTION, once each", #t == 2 and t[1] == "PALADIN" and t[2] == "DECEPTION", table.concat(t, ","))
    check("vm tags: untagged stays untagged", out[2] and out[2].tags == nil)
end

-- 3. Rendered list and Answers disagree, answers shown (S007/S010): options from the VMs.
do
    local items = { item({ run("old answer") }, nil) } -- 1 rendered vs 3 VMs
    local vms = {
        vm({ { Text = "True Soul?" } }),
        vm({ { Type = "Tag", Text = "Insight" }, { Text = "What happened to your brother?" } }),
        vm({ { Text = "Leave." } }, nil, false),
    }
    local out, changing = S.dialogueAnswers(widget(items), ad(vms, true))
    check("mismatch shown: not changing", changing == false)
    check("mismatch shown: three options", #out == 3, #out)
    check("mismatch shown: text", out[1] and out[1].text == "True Soul?")
    check("mismatch shown: tag", out[2] and out[2].tags and out[2].tags[1] == "INSIGHT")
    check("mismatch shown: tag split off the body", out[2] and out[2].text == "What happened to your brother?", out[2] and out[2].text)
    check("mismatch shown: disabled", out[3] and out[3].off == true)
    check("mismatch shown: vm kept for choosing", out[1] and out[1].vm == vms[1])
end

-- 4. Mismatch while a line still plays (ShowAnswers false): still "changing", nothing reported.
do
    local items = { item({ run("old answer") }, nil) }
    local vms = { vm({ { Text = "a" } }), vm({ { Text = "b" } }) }
    local out, changing, counts = S.dialogueAnswers(widget(items), ad(vms, false))
    check("mismatch listening: changing", changing == true)
    check("mismatch listening: counts for the error", counts and counts.rendered == 1 and counts.vms == 2)
    check("mismatch listening: no options", #out == 0)
end

-- 5. Mismatch and a VM without text: no guessing.
do
    local items = {}
    local vms = { vm({ { Text = "a" } }), vm({}) }
    local out, changing = S.dialogueAnswers(widget(items), ad(vms, true))
    check("mismatch no text: changing", changing == true)
    check("mismatch no text: no options", #out == 0)
end

-- 6. Plain matched answers are unchanged.
do
    local items = { item({}, "Who are you?"), item({}, "Leave.") }
    local vms = { vm({ { Text = "Who are you?" } }), vm({ { Text = "Leave." } }) }
    local out, changing = S.dialogueAnswers(widget(items), ad(vms))
    check("plain: not changing", changing == false)
    check("plain: texts", #out == 2 and out[1].text == "Who are you?" and out[2].text == "Leave.")
    check("plain: no tags", out[1].tags == nil and out[2].tags == nil)
end

-- 7. S015 Olak: 6 answers on screen, 7 VMs (one never shown, text-less here) -> paired by text.
do
    local texts = { "I'm looking for someone named Halsin.", "Stand aside.", "I'm a hired sword.",
                    "Glossy coat on that animal.", "Your leader summoned me.", "Read the goblin's mind." }
    local items, vms = {}, {}
    for k, t in ipairs(texts) do
        items[k] = (k == 5) and item({ run("[DECEPTION] ", "Tag"), run(t) }, t) or item({}, t)
        vms[#vms + 1] = (k == 5) and vm({ { Type = "Tag", Text = "Deception" }, { Text = t } }) or vm({ { Text = t } })
        if k == 3 then vms[#vms + 1] = vm({}) end -- the hidden one
    end
    local out, changing = S.dialogueAnswers(widget(items), ad(vms, true))
    check("paired: not changing", changing == false)
    check("paired: six options in screen order", #out == 6 and out[6].text == texts[6], #out)
    check("paired: vm of option 4 skips the hidden one", out[4] and out[4].vm == vms[5])
    check("paired: tag kept", out[5] and out[5].tags and out[5].tags[1] == "DECEPTION")
end

-- 8. Same, but the hidden VM has text too (so the all-VMs rule can't be used): still paired by text.
do
    local items = { item({}, "Who are you?"), item({}, "Leave.") }
    local vms = { vm({ { Text = "Who are you?" } }), vm({ { Text = "A secret line." } }), vm({ { Text = "Leave." } }) }
    local show = ad(vms, true)
    show._props.ShowAnswers = nil -- not readable: pairing still allowed (only an explicit false blocks it)
    local out, changing = S.dialogueAnswers(widget(items), show)
    check("paired hidden text: not changing", changing == false)
    check("paired hidden text: option 2 is Leave", #out == 2 and out[2].text == "Leave." and out[2].vm == vms[3])
end

-- 9. Stale rendered list from the previous node (texts match no VM): still changing.
do
    local items = { item({}, "Old answer one"), item({}, "Old answer two") }
    local vms = { vm({ { Text = "New a" } }), vm({}), vm({ { Text = "New b" } }) }
    local out, changing = S.dialogueAnswers(widget(items), ad(vms, true))
    check("stale: changing", changing == true)
    check("stale: no options", #out == 0)
end

-- 10. S026 Karlach's companion menu: a scrolling (virtualized) list, 6 of 13 answers rendered, one
--     rendered text not pairable (markup differs), so neither the all-VMs rule (a text-less hidden VM)
--     nor pairing applies: the live VMs with text, in game order.
do
    local items, vms = {}, {}
    for k = 1, 13 do
        local t = "Answer number " .. k .. "?"
        if k <= 6 then items[k] = item({}, k == 3 and "So not only do you have this thing" or t) end
        vms[#vms + 1] = vm({ { Text = k == 3 and "So not only do you have this <i>thing</i>, but..." or t } })
        if k == 9 then vms[#vms + 1] = vm({}) end -- a hidden answer
    end
    local out, changing = S.dialogueAnswers(widget(items), ad(vms, true))
    check("virtualized: not changing", changing == false)
    check("virtualized: all 13 texted answers", #out == 13, #out)
    check("virtualized: option 13 is the last VM, the hidden one skipped", out[13] and out[13].vm == vms[14])
    -- the same while the game says a line still plays: no options
    local out2, changing2, counts = S.dialogueAnswers(widget(items), ad(vms, false))
    check("virtualized listening: changing", changing2 == true and #out2 == 0)
    check("virtualized: counts carry the diagnostics", counts and counts.show == false and counts.texted == 0)
end

-- 11. Live 12:1x Karlach's camp menu: 10 VMs whose one Text param has no Text, 6 rows rendered at the top of
--     the list. The rows pair with VMs 1..6 by position; 4 more sit below the visible part.
do
    local texts = { "It sounds like all we need to do to fix your engine is find some infernal iron.",
        "What does that infernal engine do to you?", "That fight at the tollhouse really... heated you up, didn't it?",
        "You mentioned a boss who gave you up to Zariel. Who was it?",
        "In your expert opinion, what's the best way to kill a devil?", "You and Mizora seem to know each other. How?" }
    local items, vms = {}, {}
    for k = 1, 10 do
        if texts[k] then items[k] = item({}, texts[k]) end
        vms[k] = vm({ { Type = "Text" } })
    end
    local out, changing, counts = S.dialogueAnswers(widget(items), ad(vms, true))
    check("textless: not changing", changing == false)
    check("textless: the 6 visible rows", #out == 6 and out[6].text == texts[6], #out)
    check("textless: row k is VM k", out[1].vm == vms[1] and out[6].vm == vms[6] and out[6].i == 6)
    check("textless: 4 below", counts and counts.below == 4)
    -- a line still playing (ShowAnswers false): nothing, still changing
    local out2, changing2 = S.dialogueAnswers(widget(items), ad(vms, false))
    check("textless listening: changing", changing2 == true and #out2 == 0)
end

-- 12. Live 13:4x Karlach's menu scrolled to the bottom (VerticalOffset 350 of ExtentHeight 840, rows 84 high):
--     the 6 realized rows are answers 5-10; VM 10 ("Leave.") has text, the others none (that made the rule
--     above give no options at all). Rows pair with VMs from floor(offset / row height) + 1.
do
    local all = { "a1", "a2", "a3", "a4", "In your expert opinion, what's the best way to kill a devil?",
        "You and Mizora seem to know each other. How?", "So you had a friend in Avernus. You never said.",
        "Tell me again why you want to collect soul coins.", "You'll need to stay behind in camp for now.", "Leave." }
    local function rows(first, n)
        local out = {}
        for k = first, first + n - 1 do
            local it = item({}, all[k]); it._props = { ActualHeight = 84.0 }; out[#out + 1] = it
        end
        return out
    end
    local vms = {}
    for k = 1, 10 do vms[k] = vm({ k == 10 and { Type = "Text", Text = "Leave." } or { Type = "Text" } }) end
    local function scrolled(items, off)
        local sv = { Type = "ls.LSScrollViewer", _props = { VerticalOffset = off, ExtentHeight = 840.0, ScrollableHeight = 350.0 },
            _children = items }
        return { _children = { { _name = "answerList", _children = { sv } } } }
    end
    local out, changing, counts = S.dialogueAnswers(scrolled(rows(5, 6), 350.0), ad(vms, true))
    check("window: not changing", changing == false)
    check("window: rows are answers 5-10", #out == 6 and out[1].i == 5 and out[6].i == 10 and out[6].text == "Leave.", #out)
    check("window: row k is VM first+k-1", out[1].vm == vms[5] and out[6].vm == vms[10])
    check("window: 4 above, 0 below", counts and counts.above == 4 and counts.below == 0)
    -- at the top (offset 0, 6 rows): answers 1-6, 4 below
    local out2, _, c2 = S.dialogueAnswers(scrolled(rows(1, 6), 0.0), ad(vms, true))
    check("window top: 1-6, 4 below", #out2 == 6 and out2[1].i == 1 and c2 and c2.below == 4 and c2.above == 0)
    -- part way (offset 144 = 1.7 rows): the first realized row is answer 2
    local out3 = S.dialogueAnswers(scrolled(rows(2, 6), 144.0), ad(vms, true))
    check("window mid: first row is answer 2", out3[1] and out3[1].i == 2)
    -- the list holds more rows than VMs (a hidden answer, S015): no positional pairing
    local vms9 = {}
    for k = 1, 9 do vms9[k] = vms[k] end
    local out4, changing4, c4 = S.dialogueAnswers(scrolled(rows(5, 6), 350.0), ad(vms9, true))
    check("window: extent != VMs -> not paired by position", out4[1] and out4[1].vm == nil, #out4)
    -- S063 10:53: that case showed no options at all (choose refused "changing", ~70 s by hand). Now the rows
    -- the player sees, numbered by the window, no VM claimed (choose presses the row: a.row)
    check("S063 rows only: not changing", changing4 == false)
    check("S063 rows only: answers 5-10 as rendered", #out4 == 6 and out4[1].i == 5 and out4[6].i == 10
        and out4[6].text == "Leave." and out4[1].row == 1 and out4[6].row == 6, #out4)
    check("S063 rows only: 4 above, flagged", c4 and c4.above == 4 and c4.rows_only == true and c4.below == nil)
    -- at the top of the list: 1-6 and the rest below (the bridge wheels the list to read them)
    local out5, _, c5 = S.dialogueAnswers(scrolled(rows(1, 6), 0.0), ad(vms9, true))
    check("S063 rows only top: 1-6, 4 below", #out5 == 6 and out5[1].i == 1 and c5 and c5.below == 4, c5 and c5.below)
    -- right after one of our commands (rows of the answered node may still be in the tree): nothing, changing
    QUIET = true
    local out6, changing6 = S.dialogueAnswers(scrolled(rows(5, 6), 350.0), ad(vms9, true))
    QUIET = false
    check("S063 rows only: not while our command settles", #out6 == 0 and changing6 == true)
    -- a line still playing (ShowAnswers false): nothing
    local out7, changing7 = S.dialogueAnswers(scrolled(rows(5, 6), 350.0), ad(vms9, false))
    check("S063 rows only: not while listening", #out7 == 0 and changing7 == true)
    -- fewer VMs than rendered rows (a stale list from a longer node): nothing
    local vms3 = { vms[1], vms[2], vms[3] }
    local out8, changing8 = S.dialogueAnswers(scrolled(rows(5, 6), 350.0), ad(vms3, true))
    check("S063 rows only: not with fewer VMs than rows", #out8 == 0 and changing8 == true)
end

-- 12b. S081 Karlach: the list scrolls (ScrollableHeight > 0) though the row arithmetic said nothing is below:
--      S.answersScroll says so (S.dialogue flags `scrollable`; the bridge then wheels and reads the whole list).
do
    local function list(scrollable)
        local sv = { Type = "ls.LSScrollViewer", _props = { VerticalOffset = 0.0, ExtentHeight = 588.0,
            ScrollableHeight = scrollable }, _children = {} }
        return { _children = { { _name = "answerList", _children = { sv } } } }
    end
    check("S081 scroll: a list with scrollable height scrolls", S.answersScroll(list(168.0)) == true)
    check("S081 scroll: a list that fits does not", S.answersScroll(list(0.0)) == false)
    check("S081 scroll: no ScrollViewer = no", S.answersScroll({ _children = { { _name = "answerList", _children = {} } } }) == false)
end

-- 13. Audit UI-4: ui.dialogue_choose for an option outside the visible rows. The i-th answer VM is the i-th row
--     only when the list's scroll extent counts exactly the VMs (by_position); for rows-only answers (a hidden
--     text-less VM: S015 "7 VMs, 6 shown") Answers.At(i) is another answer, so choose asks the bridge to wheel
--     the row into view (needs_scroll) instead of firing SelectAnswerCommand on the wrong one.
do
    local ops, cmds, pressed = {}, {}, {}
    Ext = {
        IO = { LoadFile = function() return nil end }, Json = { Parse = function() return {} end },
        Utils = { MonotonicTime = function() return 0 end, GetGameState = function() return "Running" end },
        Input = { InjectKeyPress = function() end },
        Events = { GameStateChanged = { Subscribe = function() end } },
    }
    BG3AI.RPC.register = function(name, fn) ops[name] = fn end
    BG3AI.RPC.start_job = function(kind, spec) return { id = 1 } end
    local vms = {}
    for k = 1, 10 do vms[k] = vm({ { Type = "Text" } }) vms[k]._id = k end
    local adObj = ad(vms, true)
    local rowsEls = {}
    for k = 1, 6 do rowsEls[k] = { Type = "ls.LSListBoxItem", _children = { { Type = "Button", _btn = k } } } end
    local dlg = { _children = { { _name = "answerList", _children = rowsEls } } }
    BG3AI.UI.widgetMap = function() return { Dialogue = { el = dlg } } end
    BG3AI.UI.command = function(dc, name, param, dry) cmds[#cmds + 1] = { name, param } return true end
    BG3AI.UI.isButton = function(e) return e._btn ~= nil end
    BG3AI.UI.press = function(el, dry) pressed[#pressed + 1] = el._btn return true end
    dofile(LUA .. "/Client/UIActions.lua")
    local S2 = BG3AI.UIState
    S2.dialogue = function() return { text = "line" } end
    S2.activeDialogue = function() return adObj end
    local shown, counts
    S2.dialogueAnswers = function() return shown, false, counts end
    local function rows(first, n, rowsOnly)
        local out = {}
        for k = first, first + n - 1 do
            out[#out + 1] = { i = k, text = "answer " .. k, row = k - first + 1, vm = (not rowsOnly) and vms[k] or nil }
        end
        return out
    end
    local function choose(i, text)
        cmds, pressed = {}, {}
        return pcall(ops["ui.dialogue_choose"], { i = i, text = text, dry = true })
    end

    shown, counts = rows(1, 6, true), { below = 4, rows_only = true }
    local okc, err = choose(8)
    check("UI-4 rows only: option 8 below the fold asks for a scroll", not okc and tostring(err):find("needs_scroll"), err)
    check("UI-4 rows only: no SelectAnswerCommand on Answers.At(8)", #cmds == 0 and #pressed == 0)
    okc = choose(3)
    check("UI-4 rows only: a visible row is pressed by its own button", okc and #cmds == 0 and pressed[1] == 3)
    shown, counts = rows(5, 6, true), { above = 4, rows_only = true }
    okc, err = choose(2)
    check("UI-4 rows only: option 2 above the window asks for a scroll", not okc and tostring(err):find("needs_scroll"), err)
    okc, err = choose(11)
    check("UI-4: beyond the list is no option", not okc and tostring(err):find("no option 11"), err)

    shown, counts = rows(1, 6, false), { below = 4, above = 0, by_position = true }
    okc, err = choose(8)
    check("UI-4 by position (extent == VMs): option 8 is VM 8", okc and cmds[1] and cmds[1][1] == "SelectAnswerCommand"
        and cmds[1][2] == vms[8], err)
    shown, counts = rows(1, 6, false), { below = 4 }   -- no scroll info: not proven, no At(i)
    okc, err = choose(8)
    check("UI-4 no scroll info: no At(i) guess", not okc and tostring(err):find("needs_scroll") and #cmds == 0, err)

    -- text: the bridge passes the option's text after wheeling; the pressed row must still read it
    shown, counts = rows(5, 6, true), { above = 4, rows_only = true }
    okc, err = choose(7, "Answer 7.")
    check("UI-4 text check: same text (case/punctuation) passes", okc and pressed[1] == 3, err)
    okc, err = choose(7, "answer 9")
    check("UI-4 text check: another text is refused", not okc and tostring(err):find("the list changed") and #pressed == 0, err)
end

-- A25 (S088 06:16:18): the reaction prompt's rows as shown. Gale's Shield at levels I-VI; the decision VMs carried no
-- names (options=[i=1 decision=Undecided]) and the prompt text had a 7th "name" (+1/1, her temporary HP).
do
    local function tb(s) return { Type = "TextBlock", _tostring = s, _props = {} } end
    local function btn(texts, checked)
        local kids = {}
        for k, s in ipairs(texts) do kids[k] = tb(s) end
        return { _name = "ReactButton", _props = { IsChecked = checked == true }, _children = kids }
    end
    local function root(who, buttons, hp)
        local kids = {}
        for _, s in ipairs(hp or {}) do kids[#kids + 1] = tb(s) end
        kids[#kids + 1] = { _name = "Stack", _children = buttons }
        return { _name = "InterruptorRoot", DataContext = { _props = { Character = { _props = { Name = who } } } },
                 _children = kids }
    end
    local shield = { btn({ "Shield" }) }
    for k, n in ipairs({ "II", "III", "IV", "V", "VI" }) do shield[k + 1] = btn({ n, "Shield", n }, k == 2) end
    local popup = { _children = { { _name = "EventRoot", _children = {
        root("Gale", shield, { "177/177", "+1/1" }),
        root("Wyll", { btn({ "Hellish Rebuke" }) }),
        { _name = "DoNotReactButton", _children = { tb("Do Not React") } } } } } }
    BG3AI.UI.name = function(e) return e and e._name end
    local rows = S.reactionRows(popup)
    check("A25 reaction rows: one per ReactButton, display order", #rows == 7, #rows)
    check("A25 reaction rows: names read from the rows", rows[1] and rows[1].name == "Shield"
        and rows[6].name == "Shield" and rows[7].name == "Hellish Rebuke", rows[7] and rows[7].name)
    check("A25 reaction rows: level from the numerals (none shown at level 1)", rows[1].level == nil
        and rows[2].level == 2 and rows[6].level == 6, rows[2].level)
    check("A25 reaction rows: who from the interruptor", rows[1].who == "Gale" and rows[7].who == "Wyll", rows[7].who)
    check("A25 reaction rows: the portrait's HP is no row", rows[1].i == 1 and rows[7].i == 7)
    check("A25 reaction rows: the toggled row", rows[3].chosen == true and not rows[2].chosen)
    -- S.reaction returns them as its options (the decision VMs only when no row is on screen)
    BG3AI.UI.path = function(o, path)
        for part in path:gmatch("[^%.]+") do o = o and o._props and o._props[part] end
        return o
    end
    local ev = { _props = { Interruptors = coll({}) } }
    popup.DataContext = { _props = { Data = { _props = { InterruptEvents = coll({ ev }) } } } }
    local r = S.reaction({ ReactionDecisionPopup = { el = popup } })
    check("A25 S.reaction: the shown rows are the options", r and r.options and #r.options == 7
        and r.options[4].name == "Shield" and r.options[4].level == 4, r and r.options and #r.options)
    local bare = { _children = {}, DataContext = popup.DataContext }
    local dec = { _props = { Decision = "Undecided", Interrupt = { _props = {} } } }
    ev._props.Interruptors = coll({ { _props = { Character = { _props = { Name = "Gale" } },
                                                 InterruptDecisions = coll({ dec }) } } })
    r = S.reaction({ ReactionDecisionPopup = { el = bare } })
    check("A25 S.reaction: no rows on screen: the decision VMs as before", r and r.options and #r.options == 1
        and r.options[1].decision == "Undecided", r and r.options and #r.options)
end

print(string.format("%d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
