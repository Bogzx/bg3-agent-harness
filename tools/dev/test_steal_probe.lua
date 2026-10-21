-- Offline tests for Client/PickpocketOps.lua (ui.steal_probe) with a fake UI tree.
--   lua tools/dev/test_steal_probe.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

-- Fake elements: {Name, IsVisible, IsMouseOver, IsEnabled, DataContext, kids}
local pages = {}
local executed = {}
local UI = {}
function UI.widgets() return pages end
function UI.isVisible(el) return el.IsVisible ~= false end
function UI.name(el) return el.Name end
function UI.get(o, k) return o and o[k] end
function UI.prop(o, k) return o and o[k] end
function UI.props(o)
    local out = {}
    for k, v in pairs(o or {}) do out[k] = v end
    return out
end
function UI.typeName(v) return type(v) == "table" and v.Type or nil end
function UI.path(o, p)
    for part in p:gmatch("[^%.]+") do
        if type(o) ~= "table" then return nil end
        o = o[part]
    end
    return o
end
function UI.find(el, pred, opts)
    local budget = (opts or {}).max or 20000
    local function rec(e)
        budget = budget - 1
        if budget < 0 or not UI.isVisible(e) then return nil end
        if pred(e) then return e end
        for _, c in ipairs(e.kids or {}) do
            local r = rec(c)
            if r then return r end
        end
    end
    return rec(el)
end
function UI.byName(el, n) return UI.find(el, function(e) return e.Name == n end) end
function UI.execute(cmd, param, dry)
    if not cmd.can then return false, "command cannot execute now" end
    if not dry then executed[#executed + 1] = cmd.id end
    return true
end
local ops = {}
BG3AI = { RPC = { register = function(n, f) ops[n] = f end }, UI = UI }
dofile(LUA .. "/Client/PickpocketOps.lua")

-- ui.steal_probe
ok(ops["ui.steal_probe"]().open == false, "steal_probe: no window")
local steal = { Name = "StealBtn", IsMouseOver = false, IsEnabled = true }
local ppdc = { CanSteal = true, Roll = { Chance = 0.55, DifficultyCheck = 12, Finished = false } }
pages = { { name = "Pickpocket", el = { DataContext = ppdc, kids = { { Name = "inventoryListBox" }, steal } } } }
local r = ops["ui.steal_probe"]()
ok(r.open and r.button and not r.over and r.enabled and r.can_steal, "steal_probe: open, button, not hovered")
ok(r.roll.chance == 55 and r.roll.dc == 12 and not r.roll.finished, "steal_probe: the shown odds")
steal.IsMouseOver = true
ppdc.Roll = { Chance = 0.55, DifficultyCheck = 12, Finished = true, Success = false, RolledNumber = 7 }
r = ops["ui.steal_probe"]()
ok(r.over and r.roll.finished and r.roll.success == false and r.roll.rolled == 7, "steal_probe: hovered, roll over")
pages[1].el.IsVisible = false
ok(ops["ui.steal_probe"]().open == false, "steal_probe: a hidden window is not open")
pages[1].el.IsVisible = true
steal.IsEnabled = false
ok(ops["ui.steal_probe"]().enabled == false, "steal_probe: greyed-out Steal")

print(string.format("%d passed, %d failed", pass, fail))
os.exit(fail == 0 and 0 or 1)
