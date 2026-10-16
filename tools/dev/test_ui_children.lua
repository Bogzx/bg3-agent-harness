-- Offline tests for Client/UI.lua's child reads (S014 crash: Panel::GetVisualChild rebuilds the z-order
-- cache, so stock panels are walked through their Children collection instead).
--   lua tools/dev/test_ui_children.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local root
Ext = {
    UI = { GetRoot = function() return root end },
    Utils = { MonotonicTime = function() return 0 end },
    Events = { Tick = { Subscribe = function() end } },
}
BG3AI = { RPC = { register = function() end } }
dofile(LUA .. "/Client/UI.lua")
local UI = BG3AI.UI

-- A mock Noesis element. visualCalls counts VisualChild reads on it.
local visualCalls = 0
local E = {}
E.__index = E
function E:VisualChild(i) visualCalls = visualCalls + 1; return self.kids[i] end
function E:Child(i) error("logical children are not the visual ones") end
function E:GetProperty(name)
    if name == "Children" then
        if not self.isPanel then error("no property Children") end
        return self.coll
    end
    return self.props and self.props[name]
end
-- A UIElementCollection: Count + At.
local Coll = {}
Coll.__index = Coll
function Coll:At(i) return self.items[i] end
function Coll:GetProperty(name) if name == "Count" then return self.count or #self.items end end
local PANELS = { Grid = true, StackPanel = true, Canvas = true, ["ls::CustomPanel"] = true }
local function el(ty, name, kids, opts)
    opts = opts or {}
    local e = setmetatable({ Type = ty, Name = name, kids = kids or {}, props = opts.props }, E)
    e.VisualChildrenCount = #e.kids
    e.isPanel = PANELS[ty] or false
    if e.isPanel then e.coll = setmetatable({ items = e.kids, count = opts.collCount }, Coll) end
    return e
end

-- ContentRoot (a Grid) with two pages; a Border (templated) below one of them.
local btn = el("Button", "TakeAll")
local border = el("Border", "Frame", { btn })
local loot = el("Grid", "ContainerWidget", { border })
local hud = el("Grid", "Hud")
local cr = el("Grid", "ContentRoot", { hud, loot })
root = el("Border", "root", { cr })
root.Find = function(self, n) return n == "ContentRoot" and cr or nil end

visualCalls = 0
local names = {}
for _, w in ipairs(UI.widgets()) do names[#names + 1] = w.name end
ok(table.concat(names, ",") == "Hud,ContainerWidget", "widgets lists ContentRoot's pages")
ok(visualCalls == 0, "ContentRoot (a Grid) is walked without VisualChild")
ok(UI.widget("ContainerWidget") == loot, "widget by name")

visualCalls = 0
local found = UI.byName(loot, "TakeAll")
ok(found == btn, "find below a Grid and a Border")
ok(visualCalls == 1, "only the Border uses VisualChild (not a panel)")

-- The collection's count disagrees with the visual one (changing right now): visual read.
local item = el("ContentPresenter", "Item")
local host = el("StackPanel", "Host", { item }, { collCount = 2 })
visualCalls = 0
ok(UI.childCount(host) == 1 and UI.child(host, 1) == item, "count mismatch still lists the child")
ok(visualCalls == 1, "count mismatch falls back to VisualChild")

-- An unknown panel type (game's own): visual read, and no Children read (it would log an error).
local custom = el("ls::CustomPanel", "C", { el("TextBlock", "T") })
visualCalls = 0
ok(UI.child(custom, 1) ~= nil and visualCalls == 1, "unknown type falls back to VisualChild")

-- UI.child without a prior childCount decides by itself (cached index paths).
local fresh = el("Canvas", "Fresh", { el("TextBlock", "A") })
visualCalls = 0
ok(UI.child(fresh, 1) ~= nil and visualCalls == 0, "child() alone uses the collection read on a panel")

-- Counts drift (a child added between count and read): still answers, no error.
ok(UI.child(fresh, 5) == nil, "out-of-range index is nil")
ok(UI.childReads.collection > 0 and UI.childReads.visual > 0, "read counters move")

-- audit UI-5: with bg3le's strict mode (patch 0010) a changing stock panel is skipped, never read through
-- Panel::GetVisualChild (S014's crash site); non-panels and unknown types keep VisualChild
Ext._Internal = { UiStrict = function() return true, 0 end }
visualCalls = 0
ok(UI.childCount(host) == 1 and UI.child(host, 1) == nil and visualCalls == 0, "strict: count mismatch is skipped")
ok(UI.childReads.skipped >= 1, "strict: skipped reads are counted")
visualCalls = 0
ok(UI.child(custom, 1) ~= nil and visualCalls == 1, "strict: an unknown type still uses VisualChild")
ok(UI.child(fresh, 1) ~= nil, "strict: a settled panel reads its collection")
Ext._Internal = { UiStrict = function() return false, 0 end }
visualCalls = 0
ok(UI.child(host, 1) == item and visualCalls == 1, "strict off: the mismatch falls back as before")
Ext._Internal = nil

print(string.format("%d passed, %d failed", pass, fail))
os.exit(fail == 0 and 0 or 1)
