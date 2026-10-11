-- Offline tests for Client/Pick.lua P.same_entity (the hover check's no-Uuid object vs the target entity).
-- Run: tools/dev/test_confirm.sh (Lua 5.3 + 5.4 in Docker)
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- bg3le: entity proxies compare by handle only against another proxy; a raw handle is a number.
local proxies = {}
local Proxy = {}
Proxy.__eq = function(a, b) return a.h == b.h end
local function ent(h, pos) local e = setmetatable({ h = h, Transform = { Transform = { Translate = pos } } }, Proxy); proxies[h] = e; return e end
Ext = {
    Utils = {
        HandleToInteger = function(x)
            if type(x) == "number" then return x end
            if type(x) == "table" and x.h then return x.h end
            error("entity expected")
        end,
        IntegerToHandle = function(i) return proxies[i] end,
        MonotonicTime = function() return 0 end, Print = print, PrintWarning = print, PrintError = print,
    },
    Entity = {}, UI = {},
}
BG3AI = { Util = { round = function(x) return x end }, RPC = { register = function() end } }
dofile(LUA .. "/Client/Pick.lua")
local P = BG3AI.Pick

local n, bad = 0, 0
local function test(name, fn)
    n = n + 1
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else bad = bad + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end

local door = ent(42, { 10, 0, 5 })
test("the same proxy matches by __eq", function()
    local same, how = P.same_entity(ent(42, { 10, 0, 5 }), door)
    assert(same and how == "eq", tostring(how))
end)
test("a raw handle (number) of the target matches by handle", function()
    local same, how = P.same_entity(42, door)
    assert(same and how == "handle", tostring(how))
end)
test("another object at the target's origin matches by origin", function()
    local same, how, d = P.same_entity(ent(77, { 10.1, 0, 5.1 }), door)
    assert(same and how == "origin" and d < 0.3, tostring(how))
end)
test("an object 2 m away is not the target and says how far", function()
    local same, how, d = P.same_entity(ent(78, { 12, 0, 5 }), door)
    assert(not same and how == nil and math.abs(d - 2) < 1e-6)
end)
test("garbage does not raise", function()
    local same = P.same_entity("x", door)
    assert(not same)
end)

print(string.format("%d/%d passed", n - bad, n))
if bad > 0 then os.exit(1) end
