-- Pickpocket Steal button probe (owner: worker pi). live2 §1 (2026-10-21): ui.pickpocket's UI.press on StealBtn
-- never started a steal; only a real click on Steal did. The bridge (bridge/src/bg3ai/stealclick.py) clicks it the
-- way boxclick clicks a Yes/No box: move the cursor, ask here whether it is over StealBtn, click only when it is,
-- then read the roll here until it finished.
--
--   ui.steal_probe {} -> {open=false} | {open, button, over, enabled, can_steal, roll={finished, success, rolled,
--                        dc, chance}}
-- Reads named properties only (no tree walk beyond StealBtn's lookup, no command).

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local PP = BG3AI.PickpocketOps or {}
BG3AI.PickpocketOps = PP

local function shown()
    for _, w in ipairs(UI.widgets()) do
        if w.name == "Pickpocket" and UI.isVisible(w.el) then return w.el end
    end
    return nil
end

-- The selected item's roll as the window shows it (Roll.Chance 0..1, -1 = none; Roll.DifficultyCheck).
function PP.roll(dc)
    local function num(path) return tonumber(UI.path(dc, path)) end
    local chance = num("Roll.Chance") or num("Chance")
    return { chance = chance and chance >= 0 and math.floor(chance * 100 + 0.5) or nil,
             dc = num("Roll.DifficultyCheck"), rolled = num("Roll.RolledNumber"),
             finished = UI.path(dc, "Roll.Finished") == true, success = UI.path(dc, "Roll.Success") == true }
end

RPC.register("ui.steal_probe", function()
    local w = shown()
    if not w then return { open = false } end
    local ok, dc = pcall(function() return w.DataContext end)
    dc = ok and dc or nil
    local btn = UI.byName(w, "StealBtn")
    return { open = true, button = btn ~= nil, over = btn ~= nil and UI.get(btn, "IsMouseOver") == true,
             enabled = btn ~= nil and UI.get(btn, "IsEnabled") ~= false,
             can_steal = dc ~= nil and UI.prop(dc, "CanSteal") ~= false, roll = dc and PP.roll(dc) or nil }
end)

return PP
