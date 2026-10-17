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
BG3AI = { Util = { round = function(x) return x end, comp = function(e, c) return type(e) == "table" and e[c] or nil end },
    RPC = { register = function() end } }
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

-- ui.ground_check's verdict (two-step casts: the landing / the spot beside a body, act 2026-10-16)
test("cursor_info: a CastWarning cursor (name or number) is a warning; Cast is not", function()
    local c = P.cursor_info({ RequestedCursor = "CastWarning", CurrentOverlay = "None" })
    assert(c.cursor == "CastWarning" and c.warning == true and c.overlay == nil)
    c = P.cursor_info({ RequestedCursor = 0xD })
    assert(c.cursor == "CastWarning" and c.warning == true)
    c = P.cursor_info({ RequestedCursor = "Cast" })
    assert(c.cursor == "Cast" and c.warning == false)
    c = P.cursor_info({ RequestedCursor = 12, CurrentOverlay = "WalkWarning" })
    assert(c.cursor == "Cast" and c.overlay == "WalkWarning" and c.warning == true)
end)
test("cursor_info: unreadable fields give nothing, not an error", function()
    local cc = setmetatable({}, { __index = function() error("no such property") end })
    local c = P.cursor_info(cc)
    assert(c.cursor == nil and c.warning == nil)
    assert(next(P.cursor_info(nil)) == nil)
end)
-- L7 22:09 Dimension Door: the caster under a spell cursor was never named by GameObject: the entry's own picks count
test("hovered: Character / Item / Selection picks name uuids after the GameObjects, via says which", function()
    local U1, U2, U3 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333"
    local function e(u) return { Uuid = { EntityUuid = u } } end
    local saved = Ext.UI.GetPickingHelper
    Ext.UI.GetPickingHelper = function()
        return { Inner = { Inner = { { GameObject = e(U1), Character = e(U1) }, { Character = e(U2) } } },
                 Selection = { field_0_Entity = e(U3) } }
    end
    local list, _, objs, via = P.hovered()
    Ext.UI.GetPickingHelper = saved
    assert(#list == 3 and list[1] == U1 and list[2] == U2 and list[3] == U3, table.concat(list, ","))
    assert(via[U1] == "GameObject" and via[U2] == "Character" and via[U3] == "Selection")
    assert(#objs == 1)
end)
test("hovered: an unreadable field is skipped, the rest still read", function()
    local U1 = "11111111-1111-1111-1111-111111111111"
    local saved = Ext.UI.GetPickingHelper
    local entry = setmetatable({ Item = { Uuid = { EntityUuid = U1 } } }, { __index = function(_, k)
        error("no property " .. k) end })
    Ext.UI.GetPickingHelper = function() return { Inner = { Inner = { entry } } } end
    local list, _, _, via = P.hovered()
    Ext.UI.GetPickingHelper = saved
    assert(#list == 1 and via[U1] == "Item", tostring(list[1]))
end)
-- L7 22:10 Misty Step: WorldPosition (the near-plane point by the camera) was read as the ground point: 11.56 m off
test("ground_point: the near-plane WorldPosition is not the ground; an on-ray hit field is", function()
    local C = { 0, 10, 0 }
    local W = { 0.06, 9.94, 0 }                       -- 0.085 m along the ray toward (10, 0, 0)
    local hits = { { "Position", { 10, 0, 0 } }, { "SceneryPosition", { 3, 1, 4 } } }
    local p, src = P.ground_point(C, W, hits, 0)
    assert(src == "Position" and p[1] == 10, tostring(src))
    -- the first field off the ray (a stale or misaligned read) is passed over for the next one on it
    p, src = P.ground_point(C, W, { { "Position", { 0, 0, 0 } }, { "SceneryPosition", { 3, 1, 4 } },
                                    { "PlaceablePosition", { 9.9, 0.1, 0 } } }, 0)
    assert(src == "PlaceablePosition", tostring(src))
    -- nothing on the ray: where the ray meets the landing's height
    p, src = P.ground_point(C, W, { { "Position", { 5, 5, 5 } } }, 0)
    assert(src == "ray_plane" and math.abs(p[1] - 10) < 1e-6 and p[2] == 0 and math.abs(p[3]) < 1e-6, tostring(src))
    -- a WorldPosition far from the camera is a hit itself
    p, src = P.ground_point(C, { 10, 0, 0 }, {}, 3)
    assert(src == "WorldPosition" and p[1] == 10, tostring(src))
    -- no camera: no point (the verdict then has no d_m and the click is not checked by distance)
    assert(P.ground_point(nil, W, hits, 0) == nil)
    -- the old read: W as the ground point was ~11.5 m off a landing 11.6 m out; the on-ray hit is on it
    local v = P.ground_verdict({ 10, 0, 0 }, 0.8, W, {}, {}, {})
    assert(v.near == false and v.d_m > 9, tostring(v.d_m))
    v = P.ground_verdict({ 10, 0, 0 }, 0.8, P.ground_point(C, W, hits, 0), {}, {}, {})
    assert(v.near == true, tostring(v.d_m))
end)
test("ground_verdict: flat distance to the landing, near within tol, the avoided body under the cursor", function()
    local r = P.ground_verdict({ 10, 0, 5 }, 1.0, { 10.6, 3, 5 }, { "body" }, { "body" }, { cursor = "Cast", warning = false })
    assert(r.near == true and math.abs(r.d_m - 0.6) < 1e-6, tostring(r.d_m))
    assert(r.on_avoid == true and r.hover == "body" and r.cursor == "Cast" and r.warning == false)
    r = P.ground_verdict({ 10, 0, 5 }, 0.5, { 12, 0, 5 }, {}, {}, {})
    assert(r.near == false and r.on_avoid == false and r.d_m == 2)
    r = P.ground_verdict(nil, nil, nil, { "x" }, { "y" }, {})
    assert(r.near == nil and r.d_m == nil and r.hover == "x" and r.on_avoid == false)
    r = P.ground_verdict({ 0, 0, 0 }, 1, { 0, 0, 0 }, { "barrel", "ally" }, {}, {}, function(u) return u == "ally" end)
    assert(r.on_avoid == true and r.hover == "barrel")
    r = P.ground_verdict({ 0, 0, 0 }, 1, { 0, 0, 0 }, { "barrel" }, {}, {}, function(u) return u == "ally" end)
    assert(r.on_avoid == false)
end)

-- A14 S070 21:08:39: a Dragon Head Torch ~1.8 m up the wall: item aims stopped at 1.1 m over the origin
test("aim_point: high/top reach a wall mount on an item; a character's are its chest", function()
    local torch = { Transform = { Transform = { Translate = { 1, 10, 2 } } } }
    local guy = { Transform = { Transform = { Translate = { 1, 10, 2 } } }, ClientCharacter = {} }
    Ext.Entity.Get = function(u) return ({ torch = torch, guy = guy })[u] end
    local p, aim = P.aim_point("torch", "top")
    assert(aim == "top" and math.abs(p[2] - 12.1) < 1e-9, tostring(aim) .. " " .. tostring(p and p[2]))
    p, aim = P.aim_point("torch", "high")
    assert(aim == "high" and math.abs(p[2] - 11.6) < 1e-9)
    p, aim = P.aim_point("guy", "top")
    assert(aim == "chest" and math.abs(p[2] - 11.1) < 1e-9, tostring(aim))
    Ext.Entity.Get = nil
end)

print(string.format("%d/%d passed", n - bad, n))
if bad > 0 then os.exit(1) end
