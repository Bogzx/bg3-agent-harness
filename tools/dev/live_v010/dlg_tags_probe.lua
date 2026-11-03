-- One-shot live probe (client context): where a dialogue answer's skill tags ([PERSUASION], [PALADIN] [DECEPTION])
-- live on the node on screen. v010 window 5 load 6: Lae'zel's cage "Get rid of them." showed the tags on screen, while
-- S.answerTags(vm) (AnswerTags items, CtxAnswer.Params with Type == "Tag") gave none. Run while the answers show:
--   call.py TAGS lua:client "$(cat tools/dev/live_v010/dlg_tags_probe.lua)"
-- Reads the live VMs from ActiveDialogue.Answers (GetAllProperties: VMs, not elements) and, per rendered row, only named
-- reads on its AnswerText runs (S017: never GetAllProperties on a live run).
local B = Mods.BG3AI.BG3AI
local UI, S = B.UI, B.UIState
local function show(v)
    local t = type(v)
    if t == "string" or t == "number" or t == "boolean" then return v end
    if v == nil then return nil end
    return "<" .. t .. " " .. tostring(UI.typeName(v) or v) .. ">"
end
local function all(o)
    local out = {}
    for k, v in pairs(UI.props(o)) do out[tostring(k)] = show(v) end
    return out
end
local w = UI.widgetMap().Dialogue
local dc = w and w.el.DataContext
local ad = S.activeDialogue(dc)
local vms = ad and UI.prop(ad, "Answers")
local r = { vms = {}, rows = {} }
for k = 1, vms and UI.count(vms) or 0 do
    local vm = vms:At(k)
    local ctx = UI.prop(vm, "CtxAnswer")
    local v = { props = all(vm), ctx = all(ctx), params = {}, tags = {}, answerTags = S.answerTags(vm) }
    for _, p in UI.items(UI.prop(ctx, "Params"), 20) do v.params[#v.params + 1] = all(p) end
    for _, t in UI.items(UI.prop(vm, "AnswerTags"), 20) do v.tags[#v.tags + 1] = all(t) end
    r.vms[k] = v
end
local list = UI.byName(w.el, "answerList")
local items = UI.findAll(list, function(e)
    local ty = UI.typeName(e)
    return ty == "ls.LSListBoxItem" or ty == "ListBoxItem"
end, { prune = true })
for k, it in ipairs(items) do
    local tb = UI.byName(it, "AnswerText")
    local runs = {}
    for _, x in UI.items(tb and UI.get(tb, "Inlines"), 20) do
        runs[#runs + 1] = { ty = UI.typeName(x), text = show(UI.get(x, "Text")), str = show(UI.get(x, "ToolTip")) }
    end
    r.rows[k] = { tostring = tb and tostring(UI.text(tb)), runs = runs }
end
return Ext.Json.Stringify(r)
