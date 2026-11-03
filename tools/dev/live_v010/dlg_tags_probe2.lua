-- One-shot live probe 2 (client context), after window 6 L2's dlg_tags_probe.lua (v010w6/tags-probe.json): the
-- answer VMs' CtxAnswer.Params with Type "Tag" list no label among GetAllProperties (only Type, HasTooltip,
-- TooltipCustomData = an ls.VMDialogueAnswerTag with AbilityValue / AdditionalValue / IsRoll), yet the game renders
-- "[PERSUASION] " from the param's `Text` (DialogueTemplates.xaml: <Setter Property="Text" Value="{Binding Text,
-- StringFormat='{}[{0}] '}"/>). This reads, per Tag param, NAMED properties GetAllProperties may not list, and expands
-- the collections the first probe left as "<table ...>". Run at Lae'zel's cage node with the answers showing:
--   call.py TAGS2 lua:client "$(cat tools/dev/live_v010/dlg_tags_probe2.lua)"
local B = Mods.BG3AI.BG3AI
local UI, S = B.UI, B.UIState
local NAMES = { "Text", "Name", "DisplayName", "Label", "Value", "Handle", "Key", "Tag", "TagName", "Title",
    "Description", "Ability", "Skill", "SkillName", "AbilityName", "Stat", "StatName", "Id", "Guid", "Uuid", "Icon",
    "TooltipTitle", "Caption", "Content", "String", "TranslatedString", "LocalizedText" }
local function show(v)
    local t = type(v)
    if t == "string" or t == "number" or t == "boolean" then return v end
    if v == nil then return nil end
    return "<" .. t .. " " .. tostring(UI.typeName(v) or v) .. ">"
end
local function named(o)
    local out = {}
    for _, k in ipairs(NAMES) do
        local v = UI.prop(o, k)
        if v ~= nil then out[k] = show(v) end
    end
    return out
end
local function all(o)
    local out = {}
    for k, v in pairs(UI.props(o)) do out[tostring(k)] = show(v) end
    return out
end
local function items(coll)
    local out = {}
    for _, x in UI.items(coll, 12) do out[#out + 1] = { all = all(x), named = named(x) } end
    return out
end
local w = UI.widgetMap().Dialogue
local ad = S.activeDialogue(w.el.DataContext)
local vms = UI.prop(ad, "Answers")
local r = {}
for k = 1, UI.count(vms) do
    local vm = vms:At(k)
    local v = { idx = UI.prop(vm, "AnswerIdx"), params = {} }
    for _, p in UI.items(UI.prop(UI.prop(vm, "CtxAnswer"), "Params"), 12) do
        if UI.prop(p, "Type") == "Tag" then
            local tag = UI.prop(p, "TooltipCustomData")
            v.params[#v.params + 1] = { named = named(p), extender = items(UI.prop(p, "ExtenderData")),
                rolls = items(UI.prop(p, "Rolls")), tooltip = items(UI.prop(p, "TooltipEntries")),
                tag = tag and { named = named(tag), modifiers = items(UI.prop(tag, "Modifiers")),
                    advantages = items(UI.prop(tag, "Advantages")) } or nil }
        end
    end
    if #v.params > 0 then r[#r + 1] = v end
end
return Ext.Json.Stringify(r)
