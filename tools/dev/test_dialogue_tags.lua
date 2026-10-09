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
        text = function(el) return clean(el._tostring) or (el._props.Inlines and clean(inlineText(el._props.Inlines))) end,
        inlineText = inlineText,
        allText = function() return nil end,
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

print(string.format("%d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
