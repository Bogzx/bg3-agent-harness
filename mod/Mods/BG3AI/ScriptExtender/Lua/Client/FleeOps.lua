-- The HUD's Flee (owner: worker pi). live2 §2 (2026-10-21): the Flee button right of End Turn opens "Do you want to
-- flee from combat?" once every enemy is 27 m away (its tooltip: "Enemy too close! (5.5/27m)"); Yes sends that
-- character to camp (FleeFromCombat, LeftCombat). The bridge's flee tool (bridge/src/bg3ai/fleeclick.py) runs the
-- button's command when a page has one (as End Turn's), else clicks the button hover-checked, then answers the box.
-- The command and element names were not read live yet: both are found by "flee" in their names.
--
--   ui.flee_probe {} -> {commands = {{widget, name, can}}, button = {widget, name, over, enabled} | nil}
--   ui.flee {name, widget} -> {pressed = name} (the command; error when it cannot execute)

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local FL = BG3AI.FleeOps or {}
BG3AI.FleeOps = FL

FL.BUTTON_SEARCH_MAX = 4000   -- elements per page searched for the button

local function isFlee(s) return type(s) == "string" and s:lower():find("flee", 1, true) ~= nil end

local function visiblePages()
    local out = {}
    for _, w in ipairs(UI.widgets()) do
        if UI.isVisible(w.el) then out[#out + 1] = w end
    end
    return out
end

-- {widget, name, owner (the VM that holds it), cmd} for every command named like flee on a page's DataContext or
-- its CurrentPlayer.
function FL.commands()
    local out = {}
    for _, w in ipairs(visiblePages()) do
        local ok, dc = pcall(function() return w.el.DataContext end)
        if ok and dc then
            for _, vm in ipairs({ dc, UI.prop(dc, "CurrentPlayer") }) do
                for k, v in pairs(UI.props(vm)) do
                    local tn = UI.typeName(v)
                    if isFlee(k) and tn and tn:find("Command") then
                        out[#out + 1] = { widget = w.name, name = k, owner = vm, cmd = v }
                    end
                end
            end
        end
    end
    table.sort(out, function(a, b) return a.widget .. a.name < b.widget .. b.name end)
    return out
end

function FL.button()
    for _, w in ipairs(visiblePages()) do
        local el = UI.find(w.el, function(e) return isFlee(UI.name(e)) end, { max = FL.BUTTON_SEARCH_MAX })
        if el then return { widget = w.name, name = UI.name(el), el = el } end
    end
    return nil
end

RPC.register("ui.flee_probe", function()
    local cmds = {}
    for _, c in ipairs(FL.commands()) do
        cmds[#cmds + 1] = { widget = c.widget, name = c.name, can = (UI.execute(c.cmd, nil, true)) == true }
    end
    local b = FL.button()
    return { commands = cmds, button = b and { widget = b.widget, name = b.name,
        over = UI.get(b.el, "IsMouseOver") == true, enabled = UI.get(b.el, "IsEnabled") ~= false } or nil }
end)

RPC.register("ui.flee", function(args)
    for _, c in ipairs(FL.commands()) do
        if c.name == args.name and (args.widget == nil or c.widget == args.widget) then
            local ok, err = UI.execute(c.cmd, nil)
            if not ok then error("Flee: " .. tostring(err), 0) end
            return { pressed = c.name, widget = c.widget }
        end
    end
    error("no flee command '" .. tostring(args.name) .. "' on the open pages", 0)
end)

return FL
