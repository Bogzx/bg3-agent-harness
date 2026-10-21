-- Offline tests for Client/FleeOps.lua (ui.flee_probe, ui.flee) with a fake UI tree.
--   lua tools/dev/test_flee_ops.lua mod/Mods/BG3AI/ScriptExtender/Lua
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
dofile(LUA .. "/Client/FleeOps.lua")

-- ui.flee_probe / ui.flee
local fleeCmd = { Type = "ls.DelegateCommand", can = false, id = "flee" }
local endCmd = { Type = "ls.DelegateCommand", can = true, id = "end" }
local fleeBtn = { Name = "FleeButton", IsMouseOver = false }
pages = { { name = "Tooltip", el = { IsVisible = false, kids = { { Name = "FleeTooltip" } } } },
          { name = "HotBar", el = { DataContext = { EndTurn = endCmd, CurrentPlayer = { RequestFlee = fleeCmd } },
                                    kids = { { Name = "EndTurnBtn" }, fleeBtn } } } }
r = ops["ui.flee_probe"]()
ok(#r.commands == 1 and r.commands[1].name == "RequestFlee" and r.commands[1].widget == "HotBar"
   and r.commands[1].can == false, "flee_probe: the flee command (CurrentPlayer), not EndTurn; can't execute yet")
ok(r.button and r.button.name == "FleeButton" and r.button.widget == "HotBar" and not r.button.over,
   "flee_probe: the button on a visible page (not a hidden page's)")
local okc, err = pcall(ops["ui.flee"], { name = "RequestFlee" })
ok(not okc and tostring(err):find("cannot execute") and #executed == 0, "ui.flee: refused while it can't execute")
fleeCmd.can = true
r = ops["ui.flee"]({ name = "RequestFlee", widget = "HotBar" })
ok(r.pressed == "RequestFlee" and executed[1] == "flee" and #executed == 1, "ui.flee: runs the command once")
okc = pcall(ops["ui.flee"], { name = "Nope" })
ok(not okc, "ui.flee: an unknown name fails")
pages = { { name = "HotBar", el = { DataContext = {}, kids = {} } } }
r = ops["ui.flee_probe"]()
ok(#r.commands == 0 and r.button == nil, "flee_probe: nothing named flee")

print(string.format("%d passed, %d failed", pass, fail))
os.exit(fail == 0 and 0 or 1)
