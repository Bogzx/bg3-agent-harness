-- The HUD's Flee (owner: worker pi). live2 §2 (2026-10-21): the Flee button right of End Turn opens "Do you want to
-- flee from combat?" once every enemy is 27 m away (its tooltip: "Enemy too close! (5.5/27m)"); Yes sends that
-- character to camp (FleeFromCombat, LeftCombat). The bridge's flee tool (bridge/src/bg3ai/fleeclick.py) runs the
-- button's command when a page has one (as End Turn's), else clicks the button hover-checked, then answers the box.
-- The command and element names were not read live yet: both are found by "flee" in their names.
--
--   ui.flee_probe {button_only?, char?} -> {commands = {{widget, name, can}}, flee = {can, cause, value, max, flags,
--     need, at} | nil, button = {widget, name, over, enabled} | nil}
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

-- The button found last, reused while its page shows and it still has its name: the bridge's hover search probes
-- once per cursor spot, and each probe searched every visible page again (up to BUTTON_SEARCH_MAX elements each).
-- launch-v1: a flee that found no spot over the button took ~49 s, of a 6 s hover budget.
FL.found = nil

function FL.button()
    local f, shown = FL.found, visiblePages()
    if f and UI.name(f.el) == f.name then
        for _, w in ipairs(shown) do
            if w.el == f.page then return f end
        end
    end
    FL.found = nil
    for _, w in ipairs(shown) do
        local el = UI.find(w.el, function(e) return isFlee(UI.name(e)) end, { max = FL.BUTTON_SEARCH_MAX })
        if el then
            FL.found = { widget = w.name, name = UI.name(el), el = el, page = w.el }
            return FL.found
        end
    end
    return nil
end

-- Whether the game lets the selected character flee, as the HUD's FleeButton reads it (HotBar.xaml: IsEnabled =
-- SelectedCharacter.PlayerCharacterProperties.CanFlee; its tooltip, which a player sees on hover, shows FleeBlockCause
-- and FleeDistance.Value/Max): {can, cause?, value?, max?}, nil when no visible page's DataContext has CanFlee.
-- v010 live window 4: flee failed after 48 s (20 commands without a question box, then 48 hover spots never "over")
-- with the button on screen; a disabled button never gets the mouse.
function FL.state(char)
    for _, w in ipairs(visiblePages()) do
        local ok, dc = pcall(function() return w.el.DataContext end)
        local p = ok and dc and UI.path(dc, "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties")
        local can = p and UI.prop(p, "CanFlee")
        if can ~= nil then
            local cause, dist = UI.prop(p, "FleeBlockCause"), UI.prop(p, "FleeDistance")
            local st = { can = can == true, cause = type(cause) == "string" and cause ~= "" and cause or nil,
                value = dist and tonumber(UI.prop(dist, "Value")), max = dist and tonumber(UI.prop(dist, "Max")) }
            if char then FL.capability(char, st) end
            return st
        end
    end
    return nil
end

-- The game's own flee state of `char` (eoc::FleeCapabilityComponent, what the HUD is built from): Flags
-- (FleeErrorFlags: Region, EnemyTooClose, CantMove, Downed, Incapacitated, Condition, Summon, PartyFollower),
-- FleeDistance (the nearest enemy) and CurrentFleeDistance (the distance needed), into st.flags / st.at / st.need.
-- v010 live window 5 (Balthazar's fight,
-- enemy at 1.4 m): CanFlee false but the VM's FleeBlockCause empty and FleeDistance -1/-1, so flee said "the game
-- gives no reason".
function FL.capability(char, st)
    local ok, cap = pcall(function() return Ext.Entity.Get(char).FleeCapability end)
    if not ok or cap == nil then return end
    local flags = {}
    pcall(function() for _, f in pairs(cap.Flags or {}) do flags[#flags + 1] = tostring(f) end end)
    table.sort(flags)
    st.flags = flags
    -- bg3se's names are the legacy field_1C / field_20 guesses; live (window 5, enemy at 2.3 m): FleeDistance 2.26189
    -- (the nearest enemy), CurrentFleeDistance 27 (the distance needed)
    local oka, at = pcall(function() return cap.FleeDistance end)
    local okn, need = pcall(function() return cap.CurrentFleeDistance end)
    st.need, st.at = okn and tonumber(need) or nil, oka and tonumber(at) or nil
end

-- button_only: the button's state only (the hover search's probe), without the commands scan
RPC.register("ui.flee_probe", function(args)
    local cmds = {}
    local only = (args or {}).button_only
    for _, c in ipairs(only and {} or FL.commands()) do
        cmds[#cmds + 1] = { widget = c.widget, name = c.name, can = (UI.execute(c.cmd, nil, true)) == true }
    end
    local b = FL.button()
    return { commands = cmds, flee = not only and FL.state((args or {}).char) or nil,
        button = b and { widget = b.widget, name = b.name,
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
