-- Offline: Client/UI.lua reads live elements by name, never GetAllProperties (S017's crash).
-- Run: tools/dev/test_ui_named_reads.py (finds a Lua 5.3 interpreter), or lua5.3 <this file> [UI.lua].

local ui_path = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua/Client/UI.lua"

Ext = {
    Events = { Tick = { Subscribe = function() end } },
    Utils = { MonotonicTime = function() return 0 end },
    UI = {},
    Log = { PrintError = function(...) error("logged an error: " .. table.concat({ ... }, " ")) end },
}
BG3AI = { RPC = { register = function() end, now_ms = function() return 0 end } }
dofile(ui_path)
local UI = BG3AI.UI

-- A fake element proxy, like bg3le's: indexing an unknown name is a Lua error (not logged);
-- GetAllProperties on an element is what the S017 rule forbids.
local enumerated = 0
local function element(typeName, values)
    local self = {}
    return setmetatable(self, {
        __index = function(_, key)
            if key == "Type" then return typeName end
            if key == "GetAllProperties" then
                return function() enumerated = enumerated + 1; return values end
            end
            if key == "GetProperty" then
                return function(_, name)
                    if values[name] == nil then Ext.Log.PrintError("no property " .. name) end
                    return values[name]
                end
            end
            local v = values[key]
            if v == nil then error("Object " .. typeName .. " has no property named '" .. key .. "'", 2) end
            return v
        end,
    })
end

local function collection(items)
    return setmetatable({}, { __index = function(_, key)
        if key == "Count" then return #items end
        if key == "GetProperty" then return function(_, n) if n == "Count" then return #items end end end
        if key == "At" then return function(_, i) return items[i] end end
        error("no property " .. key, 2)
    end })
end

local failures = 0
local function check(name, cond, detail)
    if cond then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. " " .. tostring(detail or "")) end
end

-- UI.get / UI.pick
local run = element("Run", { Text = "Hello" })
check("get existing", UI.get(run, "Text") == "Hello")
check("get missing is nil, quiet", UI.get(run, "Nope") == nil)
check("get nil object", UI.get(nil, "Text") == nil)
local p = UI.pick(run, { "Text", "Missing" })
check("pick", p.Text == "Hello" and p.Missing == nil)

-- inlineText over Run / Span(Inlines) / LineBreak, no enumeration
local inlines = collection({
    element("Run", { Text = "[WIS] " }),
    element("Span", { Inlines = collection({ element("Run", { Text = "Dig into " }), element("Italic", {
        Inlines = collection({ element("Run", { Text = "his mind" }) }) }) }) }),
    element("LineBreak", {}),
    element("ls.LSRun", { Text = "." }),
})
check("inlineText", UI.inlineText(inlines) == "[WIS] Dig into his mind .", UI.inlineText(inlines))
check("inlineText does not enumerate", enumerated == 0, enumerated)

-- press: Command + IsEnabled + CommandParameter by name
local ran = 0
local cmd = element("ls.LSDelegateCommand", {
    CanExecute = function() return true end, Execute = function() ran = ran + 1 end })
local btn = element("Button", { Command = cmd, IsEnabled = true })
local ok, err = UI.press(btn)
check("press runs the command", ok and ran == 1, err)
local off = element("Button", { Command = cmd, IsEnabled = false })
ok, err = UI.press(off)
check("press refuses a disabled button", not ok and tostring(err):find("disabled"), err)
ok, err = UI.press(element("Border", {}))
check("press without a Command", not ok and tostring(err):find("no Command"), err)
check("press does not enumerate", enumerated == 0, enumerated)

-- props() still works on view models
local vm = element("ls.VMAnswer", { Enabled = true })
check("props on a VM", UI.props(vm).Enabled == true)

if failures > 0 then
    print(failures .. " FAILED")
    os.exit(1)
end
print("all ok")
