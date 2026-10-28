-- Offline tests for Server/Visibility.lua (large-object perception) and
-- Server/Navigation.lua (objective markers), with Ext/Osi stubbed.
-- Run: tools/vis/test_lua.sh  (Lua 5.3 in Docker), or `lua5.3 test_lua_vis.lua <Lua dir>`.

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

local clock = 0
local world = {}      -- uuid -> {pos, comps = {...}}
local osiv = {}       -- "Fn:arg1:arg2" -> value
local ray = {}        -- ray.fn(a, b) -> hit | nil
local heights = {}    -- heights.fn(x, z) -> {y...} | nil (Ext.Level.GetHeightsAt)
local loca = {}
local charScans = 0

local function entity(u)
    local w = world[u]
    if not w then error("no entity " .. tostring(u)) end
    local e = { Uuid = { EntityUuid = u }, Transform = { Transform = { Translate = w.pos } } }
    for k, v in pairs(w.comps or {}) do e[k] = v end
    return e
end

Ext = {
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end },
    Events = { Tick = { Subscribe = function() end } },
    Entity = {
        Get = entity,
        GetAllEntitiesWithComponent = function(name)
            local out = {}
            if name == "ServerItem" then
                for u, w in pairs(world) do if w.comps and w.comps.ServerItem then out[#out + 1] = entity(u) end end
            elseif name == "ServerCharacter" then
                charScans = charScans + 1
                for u, w in pairs(world) do if w.comps and w.comps.ServerCharacter then out[#out + 1] = entity(u) end end
            end
            return out
        end,
    },
    Level = { RaycastClosest = function(a, b, ...) return ray.fn(a, b, ...) end,
        GetHeightsAt = function(x, z) return heights.fn and heights.fn(x, z) end },
    Loca = { GetTranslatedString = function(h) return loca[h] end },
    Require = function(p) return dofile(LUA .. "/" .. p) end,
    IO = { LoadFile = function() return nil end, SaveFile = function() end },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
}

local function key(name, ...)
    local parts = { name }
    for _, a in ipairs({ ... }) do parts[#parts + 1] = tostring(a) end
    return table.concat(parts, ":")
end
Osi = setmetatable({
    DB_PartyMembers = { Get = function() return { { "P1" } } end },
    DB_WaypointInfo = { Get = function() return {} end },
    GetPosition = function(u)
        local p = osiv[key("GetPosition", u)]
        if p then return p[1], p[2], p[3] end
        error("no position")
    end,
}, { __index = function(_, name)
    return function(...) return osiv[key(name, ...)] end
end })

BG3AI = { RPC = { ops = {}, register = function(n, f) BG3AI.RPC.ops[n] = f end } }
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/Visibility.lua")
dofile(LUA .. "/Server/Navigation.lua")
local V, N = BG3AI.Visibility, BG3AI.Nav

-- ---- tiny harness --------------------------------------------------------

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    -- fresh per-test state
    clock = clock + 100000
    V.rayCache, V.rayBroken, V.calib = {}, nil, { t = -1e9, ok = false }
    V.ctxCache, V.charsCache = nil, nil -- per-frame caches
    osiv = {}
    world = {}
    loca = {}
    ray.fn = function() return nil end
    heights.fn = nil
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, msg) if a ~= b then error((msg or "") .. " expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end

-- A party member P1 at the origin looking at things; floor ray hits.
local function setup()
    world.P1 = { pos = { 0, 0, 0 }, comps = { Sight = { Sight = 18.5 }, ClientControl = {} } }
    osiv[key("IsOnStage", "P1")] = 1
    osiv[key("GetHostCharacter")] = "P1"
    osiv[key("GetRegion", "P1")] = "TUT_Avernus_C"
end

local function floorThen(f)
    return function(a, b)
        if a[1] == b[1] and a[3] == b[3] and b[2] < a[2] then return { Position = { a[1], 0, a[3] }, Distance = a[2] } end
        return f(a, b)
    end
end

local function door(u, pos, extra)
    world[u] = { pos = pos, comps = { ServerItem = { IsDoor = true },
        Bound = { Bound = { AIBounds = { Hit = { Height = 3.0, ActualHeight = 3.0, Radius = 1.2 } } } } } }
    osiv[key("IsOnStage", u)] = 1
    osiv[key("GetCanInteract", u)] = 1
    for k, v in pairs(extra or {}) do osiv[key(k, u)] = v end
end

local function visible(u)
    local ctx = V.context()
    local e = V.entity(u)
    return (V.itemVisible(ctx, u, e, V.pos(e))) == true
end

-- ---- large-object perception --------------------------------------------

test("door with no LOS but a clear ray is perceived", function()
    setup(); door("D", { 10, 0, 0 })
    ray.fn = floorThen(function() return nil end)
    eq(visible("D"), true)
end)

test("an invisible-platform helper (the Faith-Leap path) is never perceived (S031 probe)", function()
    setup(); door("CUBE1", { 10, 0, 0 })
    osiv[key("GetTemplate", "CUBE1")] = "UNI_SHA_InvisiblePlatform_f8a25225-667f-48cf-853a-76a791a460"
    ray.fn = floorThen(function() return nil end)
    eq(visible("CUBE1"), false, "helper")
    door("D2", { 10, 0, 0 })
    eq(visible("D2"), true, "a plain door still is")
end)

test("vanished: a character seen moments ago that is invisible now (S030 Soft-Step Shadow)", function()
    setup()
    world.SH = { pos = { 2, 0, 0 }, comps = { ServerCharacter = {} } }
    V.memory = {}
    V.remember("SH", "Shadow", { 2, 0, 0 }, "neutral")
    osiv[key("IsInvisible", "SH")] = 1
    local gone, m = V.vanished("SH")
    eq(gone, true, "vanished"); eq(m.ago_s, 0, "ago")
    osiv[key("IsInvisible", "SH")] = 0
    eq((V.vanished("SH")), false, "visible again (or just out of sight): not vanished")
    osiv[key("IsInvisible", "SH")] = 1
    clock = clock + (V.INVISIBLE_SEEN_S + 1) * 1000
    eq((V.vanished("SH")), false, "seen too long ago")
end)

test("itemVisible without pos (Guard's call) uses the item's own position", function()
    setup(); door("D", { 10, 0, 0 })
    ray.fn = floorThen(function() return nil end)
    eq((V.itemVisible(V.context(), "D", V.entity("D"))) == true, true, "no pos")
    eq((V.itemVisible(V.context(), "D")) == true, true, "no entity, no pos")
end)

test("door behind a wall (ray hits halfway) is not perceived", function()
    setup(); door("D", { 10, 0, 0 })
    ray.fn = floorThen(function(a) return { Position = { a[1] + 5, a[2], a[3] }, Distance = 5 } end)
    eq(visible("D"), false)
end)

test("ray whose first hit is the door itself counts", function()
    setup(); door("D", { 10, 0, 0 })
    world.W = { pos = { 3, 0, 0 } }
    ray.fn = floorThen(function(a)
        return { Position = { a[1] + 3, a[2], a[3] }, Distance = 3, Shape = { PhysicsObject = { Entity = entity("D") } } }
    end)
    eq(visible("D"), true)
end)

test("hit at the object's near face counts", function()
    setup(); door("D", { 10, 0, 0 })
    ray.fn = floorThen(function(a, b) return { Position = { b[1] - 0.6, b[2], b[3] }, Distance = 9.4 } end)
    eq(visible("D"), true)
end)

test("beyond sight range is not perceived", function()
    setup(); door("D", { 30, 0, 0 })
    ray.fn = floorThen(function() return nil end)
    eq(visible("D"), false)
end)

test("uncalibrated rays (floor not hit) fail closed", function()
    setup(); door("D", { 10, 0, 0 })
    ray.fn = function() return nil end
    eq(visible("D"), false)
end)

test("raycast errors disable the rule (fail closed)", function()
    setup(); door("D", { 10, 0, 0 })
    ray.fn = floorThen(function() error("boom") end)
    eq(visible("D"), false)
    eq(V.rayBroken, true)
end)

test("small items don't use the ray rule", function()
    setup()
    world.C = { pos = { 5, 0, 0 }, comps = { ServerItem = { IsContainer = true },
        Bound = { Bound = { AIBounds = { Hit = { Height = 0.8, Radius = 0.5 } } } } } }
    osiv[key("IsOnStage", "C")] = 1
    osiv[key("GetCanInteract", "C")] = 1
    ray.fn = floorThen(function() return nil end)
    eq(visible("C"), false)
end)

test("large non-door interactable (by bounds) uses the ray rule", function()
    setup()
    world.S = { pos = { 8, 0, 4 }, comps = { ServerItem = { CanUse = true },
        Bound = { Bound = { AIBounds = { Hit = { Height = 2.5, Radius = 0.6 } } } } } }
    osiv[key("IsOnStage", "S")] = 1
    osiv[key("GetCanInteract", "S")] = 1
    ray.fn = floorThen(function() return nil end)
    eq(visible("S"), true)
end)

test("undiscovered standalone trap stays hidden with a clear ray", function()
    setup()
    world.T = { pos = { 6, 0, 0 }, comps = { ServerItem = {},
        Bound = { Bound = { AIBounds = { Hit = { Height = 2.5, Radius = 2.0 } } } } } }
    osiv[key("IsOnStage", "T")] = 1
    osiv[key("IsTrap", "T")] = 1
    ray.fn = floorThen(function() return nil end)
    eq(visible("T"), false)
end)

test("hidden secret door stays hidden with a clear ray", function()
    setup(); door("D", { 10, 0, 0 })
    world.D.comps.ServerItem.IsSecretDoor = true
    osiv[key("GetCanInteract", "D")] = 0
    ray.fn = floorThen(function() return nil end)
    eq(visible("D"), false)
end)

test("characters stay strict (no ray rule)", function()
    setup()
    world.G = { pos = { 10, 0, 0 }, comps = { ServerCharacter = {},
        Bound = { Bound = { AIBounds = { Hit = { Height = 1.8, Radius = 0.5 } } } } } }
    osiv[key("IsOnStage", "G")] = 1
    ray.fn = floorThen(function() return nil end)
    local ctx = V.context()
    eq(V.characterVisible(ctx, "G", V.entity("G"), { 10, 0, 0 }), false)
end)

-- vis V29 (S091 the Netherbrain): a big creature (bounds radius >= V.BIG_R) is seen by a clear ray to its body,
-- like a large item; a wall in the way still hides it.
test("a big creature: a clear ray to its body", function()
    setup()
    world.G = { pos = { 10, 0, 0 }, comps = { ServerCharacter = {},
        Bound = { Bound = { AIBounds = { Hit = { Height = 3.0, Radius = 2.0 } } } } } }
    osiv[key("IsOnStage", "G")] = 1
    ray.fn = floorThen(function() return nil end)
    eq(V.characterVisible(V.context(), "G", V.entity("G"), { 10, 0, 0 }), true)
    V.rayCache = {}
    ray.fn = floorThen(function(a) return { Position = { a[1] + 1, a[2], a[3] } } end) -- a wall 1 m ahead
    eq(V.characterVisible(V.context(), "G", V.entity("G"), { 10, 0, 0 }), false)
end)

test("dev.perception_probe counts ray-only doors", function()
    setup(); door("D", { 10, 0, 0 }); door("D2", { 4, 0, 0 })
    osiv[key("HasLineOfSight", "P1", "D2")] = 1
    ray.fn = floorThen(function() return nil end)
    V.index = { cells = {}, ready = false, built = 0 }
    local r = BG3AI.RPC.ops["dev.perception_probe"]({ radius = 30 })
    eq(r.counts.large, 2); eq(r.counts.by_ray, 1); eq(r.counts.by_sight, 1); eq(r.calibrated, true)
end)


-- read latency (S025/S026: ~0.85 ms of rays per unseen door, 155 in one frontier)
local function countingRays(f)
    local n = { 0 }
    ray.fn = floorThen(function(a, b) n[1] = n[1] + 1; return f(a, b) end)
    return n
end

test("blocked door: the verdict is reused while the party stands still, re-cast after a move", function()
    setup(); door("D", { 10, 0, 0 })
    local n = countingRays(function(a) return { Position = { a[1] + 5, a[2], a[3] }, Distance = 5 } end)
    eq(visible("D"), false)
    local cast = n[1]
    eq(cast > 0, true, "rays cast")
    clock = clock + 1000; V.ctxCache = nil -- a later read (another frame), nobody moved
    eq(visible("D"), false); eq(n[1], cast, "no new rays")
    clock = clock + V.RAY_STILL_MS -- too old even standing still
    V.ctxCache = nil
    eq(visible("D"), false); eq(n[1] > cast, true, "re-cast after RAY_STILL_MS")
    cast = n[1]
    clock = clock + 1000; V.ctxCache = nil
    world.P1.pos = { 2, 0, 0 } -- moved: the old verdict is stale
    eq(visible("D"), false); eq(n[1] > cast, true, "re-cast after a move")
end)

test("party members standing together cast one set of rays", function()
    setup(); door("D", { 10, 0, 0 })
    world.P2 = { pos = { 0.3, 0, 0.2 }, comps = { Sight = { Sight = 18.5 } } }
    world.P3 = { pos = { 0, 0, -4 }, comps = { Sight = { Sight = 18.5 } } }
    osiv[key("IsOnStage", "P2")] = 1; osiv[key("IsOnStage", "P3")] = 1
    local real = Osi.DB_PartyMembers
    Osi.DB_PartyMembers = { Get = function() return { { "P1" }, { "P2" }, { "P3" } } end }
    local ok, err = pcall(function()
        local n = countingRays(function(a) return { Position = { a[1] + 5, a[2], a[3] }, Distance = 5 } end)
        V.rayCalibrated(V.context().observers[1]); n[1] = 0
        eq(visible("D"), false)
        eq(n[1], 10, "P1 and P3: 5 sample points each, P2 shares P1's eye")
    end)
    Osi.DB_PartyMembers = real
    assert(ok, err)
end)

-- ---- pointSeen: ground below a drop edge ---------------------------------------
-- Live N14b (the Gauntlet altar): Claude's eye ray to the ledge 2.3 m below, 8 m off, ran into the altar floor's own
-- top 3.8 m out (he later stood at 4 m), so move_to there was refused "not in view". Here P1's floor (y 0) ends at
-- z = 4.2 and the ledge is at y -2.3; `floor_to(z)`: the floor plane is solid up to z (a storey: to infinity).
local function floor_to(zmax, extra)
    return floorThen(function(a, b)
        if extra then local h = extra(a, b); if h then return h end end
        if a[2] > 0 and b[2] < 0 then
            local t = a[2] / (a[2] - b[2])
            local z = a[3] + (b[3] - a[3]) * t
            if z <= zmax then return { Position = { a[1] + (b[1] - a[1]) * t, 0, z } } end
        end
        return nil
    end)
end

test("pointSeen: a ledge below the floor's edge is looked over (N14b); a floor that goes on is not", function()
    setup()
    ray.fn = floor_to(4.2)
    heights.fn = function(_, z) return z <= 4.2 and { 0.0 } or { -2.3 } end
    eq(V.pointSeen(V.context(), { 0, -2.3, 8 }), true, "the ledge past the edge")
    V.rayCache = {}
    ray.fn = floor_to(1e9) -- a storey: the floor goes on over a room below
    heights.fn = function() return { 0.0, -3.0 } end
    eq(V.pointSeen(V.context(), { 0, -3.0, 8 }), false, "the room under the floor")
    heights.fn = nil -- no heights answer: fail closed
    ray.fn = floor_to(4.2)
    eq(V.pointSeen(V.context(), { 0, -2.3, 8 }), false, "no heights: not seen")
end)

test("pointSeen: a parapet at the edge, or a spot level with the eye's floor, is not looked over", function()
    setup()
    heights.fn = function(_, z) return z <= 4.2 and { 0.0 } or { -2.3 } end
    ray.fn = floor_to(4.2, function(a, b) -- a 1 m parapet at z 4.1..4.2 stops the look over the edge
        if a[3] < 4.1 and b[3] > 4.2 then
            local t = (4.1 - a[3]) / (b[3] - a[3])
            local y = a[2] + (b[2] - a[2]) * t
            if y <= 1.0 and y >= 0 then return { Position = { a[1], y, 4.1 } } end
        end
    end)
    eq(V.pointSeen(V.context(), { 0, -2.3, 8 }), false, "behind the parapet")
    ray.fn = function(a, b) -- a wall 3 m off, the spot level with P1's floor
        if a[1] == b[1] and a[3] == b[3] and b[2] < a[2] then return { Position = { a[1], 0, a[3] }, Distance = a[2] } end
        return { Position = { 0, 0.2, 3 } }
    end
    heights.fn = function() return {} end
    eq(V.pointSeen(V.context(), { 0, 0, 8 }), false, "a wall, not an edge")
end)

test("pointSeen over an edge: a wall face hit low, a drop over 4 m, the foot of a face, 15 m back: not seen",
    function()
    setup()
    -- a wall face (z 4..5, no floor tiles) hit 0.47 m up: not floor, so no look from its raw hit
    ray.fn = floor_to(4.0, function(a, b)
        if a[3] < 4 and b[3] > 4 then
            local y = a[2] + (b[2] - a[2]) * (4 - a[3]) / (b[3] - a[3])
            if y >= 0 and y <= 2 then return { Position = { a[1], y, 4 } } end
        end
    end)
    heights.fn = function(_, z) return z < 4 and { 0.0 } or z <= 5 and {} or { -2.3 } end
    eq(V.pointSeen(V.context(), { 0, -2.3, 12 }), false, "past a wall")
    V.rayCache = {}
    ray.fn = floor_to(4.2)
    heights.fn = function(_, z) return z <= 4.2 and { 0.0 } or { -6.0 } end
    eq(V.pointSeen(V.context(), { 0, -6.0, 9 }), false, "6 m down")
    heights.fn = function(_, z) return z <= 4.2 and { 0.0 } or { -3.5 } end
    eq(V.pointSeen(V.context(), { 0, -3.5, 5.2 }), false, "1 m from the foot of a 3.5 m face")
    world.P1.pos = { 0, 0, -11 } -- 15 m back from the edge
    V.ctxCache = nil
    ray.fn = floor_to(4.2)
    heights.fn = function(_, z) return z <= 4.2 and { 0.0 } or { -2.3 } end
    eq(V.pointSeen(V.context(), { 0, -2.3, 7 }), false, "15 m back")
end)

test("pointSeen over an edge: an edge 6 m off, or a wall hit 0.1 m up its foot: not seen (app re-review)", function()
    setup()
    ray.fn = floor_to(6.0) -- the floor hit 4 m out, its edge 6 m out: past the 5 m bound
    heights.fn = function(_, z) return z <= 6.0 and { 0.0 } or { -2.3 } end
    eq(V.pointSeen(V.context(), { 0, -2.3, 8.5 }), false, "edge 6 m off")
    -- a wall face at z 4 hit 0.1 m up, the floor's height read at its foot: the hit passes as floor, and the look
    -- 0.3 m back toward the eye runs into the face
    ray.fn = floor_to(4.0, function(a, b)
        if a[3] < 4 and b[3] > 4 then
            local y = a[2] + (b[2] - a[2]) * (4 - a[3]) / (b[3] - a[3])
            if y >= 0 and y <= 2 then return { Position = { a[1], y, 4 } } end
        end
    end)
    heights.fn = function(_, z) return z <= 4.0 and { 0.0 } or z <= 5 and {} or { -2.3 } end
    eq(V.pointSeen(V.context(), { 0, -2.3, 9.07 }), false, "the wall's foot")
end)

-- ---- losFrom (los_spot planning) vs the engine's HasLineOfSight -------------------------

local function enemy(u, pos)
    world[u] = { pos = pos, comps = { ServerCharacter = {},
        Bound = { Bound = { AIBounds = { Hit = { Height = 1.8, Radius = 0.5 } } } } } }
end

test("losFrom, character: a side of the body in view is not line of sight (centre rays only)", function()
    setup(); enemy("G", { 10, 0, 0 })
    -- a pillar on the centre line: rays ending on the centre line (z = 0) stop at 5 m; the side samples pass
    ray.fn = floorThen(function(a, b)
        if math.abs(b[3]) < 0.01 then return { Position = { 5, a[2], 0 }, Distance = 5 } end
        return nil
    end)
    local ctx = V.context()
    eq(V.losFrom(ctx, "G", V.entity("G"), { 10, 0, 0 }, { 0, 0, 0 }), false, "character")
    world.G.comps.ServerCharacter = nil
    eq(V.losFrom(ctx, "G", V.entity("G"), { 10, 0, 0 }, { 0, 0, 0 }), true, "an item keeps the side samples")
end)

test("losFrom, character: chest and head clear -> true; a margin needs the offset rays clear too", function()
    setup(); enemy("G", { 10, 0, 0 })
    ray.fn = floorThen(function(a, b)
        if a[3] > 0.1 then return { Position = { 5, a[2], a[3] }, Distance = 5 } end -- cover on one side
        return nil
    end)
    local ctx = V.context()
    eq(V.losFrom(ctx, "G", V.entity("G"), { 10, 0, 0 }, { 0, 0, 0 }), true, "centre rays")
    eq(V.losFrom(ctx, "G", V.entity("G"), { 10, 0, 0 }, { 0, 0, 0 }, 0.25), false, "margin ray blocked")
    ray.fn = floorThen(function() return nil end)
    eq(V.losFrom(ctx, "G", V.entity("G"), { 10, 0, 0 }, { 0, 0, 0 }, 0.25), true, "all clear")
end)

test("losFrom, character: head behind a ledge, chest clear -> false (both must be clear)", function()
    setup(); enemy("G", { 10, 0, 0 })
    ray.fn = floorThen(function(a, b)
        if b[2] > 1.2 then return { Position = { 6, a[2], 0 }, Distance = 6 } end
        return nil
    end)
    eq(V.losFrom(V.context(), "G", V.entity("G"), { 10, 0, 0 }, { 0, 0, 0 }), false)
end)

-- ---- small fixtures (puzzle buttons recessed in a wall) -----------------------

local function button(u, pos)
    world[u] = { pos = pos, comps = { ServerItem = { CanUse = true },
        Bound = { Bound = { AIBounds = { Hit = { Height = 0.32, Radius = 0.12 } } } } } }
    osiv[key("IsOnStage", u)] = 1
    osiv[key("GetCanInteract", u)] = 1
end

test("recessed button: the ray stops 0.3 m short of its center (wall face) -> perceived", function()
    setup(); button("B", { 7, 1, 0 })
    ray.fn = floorThen(function(a, b) return { Position = { b[1] - 0.3, b[2], b[3] }, Distance = 6.7 } end)
    eq(visible("B"), true)
end)

test("button behind a wall (ray stops 2 m short) is not perceived", function()
    setup(); button("B", { 7, 1, 0 })
    ray.fn = floorThen(function(a, b) return { Position = { b[1] - 2, b[2], b[3] }, Distance = 5 } end)
    eq(visible("B"), false)
end)

test("button beyond FIXTURE_RANGE is not perceived even with a clear ray", function()
    setup(); button("B", { 14, 1, 0 })
    ray.fn = floorThen(function() return nil end)
    eq(visible("B"), false)
end)

test("a named fixture seen moments ago stays known when a plinth hides it (5 min, 30 m)", function()
    -- live 11:3x: after pressing the button under Moonrise Towers, the Grief one 12 m along the wall was refused
    setup(); button("B", { 7, 1, 0 })
    V.fixtureSeen = {}
    local oldState, oldName = BG3AI.State, V.name
    BG3AI.State = { FIXTURE_NAMES = { Button = true } }
    V.name = function(u) return u == "B" and "Button" or oldName(u) end
    ray.fn = floorThen(function(a, b) return { Position = { b[1] - 0.3, b[2], b[3] }, Distance = 6.7 } end)
    eq(visible("B"), true, "seen")
    ray.fn = floorThen(function(a, b) return { Position = { b[1] - 2, b[2], b[3] }, Distance = 5 } end)
    V.rayCache = {}
    clock = clock + 60000
    eq(visible("B"), true, "hidden by the plinth a minute later: remembered")
    clock = clock + 300000
    eq(visible("B"), false, "forgotten after 5 min")
    BG3AI.State = nil
    V.fixtureSeen = {}
    eq(visible("B"), false, "an unnamed small usable (candle) is never remembered")
    BG3AI.State, V.name = oldState, oldName
end)

test("a ladder seen moments ago stays known from a spot where it is out of sight (5 min, 30 m)", function()
    -- se 12:3x Roadside Cliffs: the ladder 13 m N was named in one travel_to stop and missing from the next
    setup(); door("L", { 10, 0, 0 })
    world.L.comps.ServerItem = { IsLadder = true }
    V.fixtureSeen = {}
    ray.fn = floorThen(function() return nil end)
    eq(visible("L"), true, "seen")
    ray.fn = floorThen(function(a) return { Position = { a[1] + 5, a[2], a[3] }, Distance = 5 } end)
    V.rayCache = {}
    clock = clock + 60000
    eq(visible("L"), true, "behind the cliff edge a minute later: remembered")
    world.P1.pos = { 50, 0, 0 }
    V.ctxCache = nil
    eq(visible("L"), false, "40 m away: not remembered")
    V.fixtureSeen = {}
end)

test("itemKnown: a static thing seen on this level stays a valid target (not listed) until gone/far/old/combat", function()
    -- S025 Grand Mausoleum: route e992 (listed 10.8 m away one call before) and interact e996 refused "not visible"
    setup(); door("D", { 10, 0, 0 })
    V.staticSeen, V.fixtureSeen = {}, {}
    local function known() V.ctxCache = nil; return (V.itemKnown(V.context(), "D")) == true end
    ray.fn = floorThen(function() return nil end)
    eq(visible("D"), true, "seen")
    ray.fn = floorThen(function(a) return { Position = { a[1] + 5, a[2], a[3] }, Distance = 5 } end)
    V.rayCache = {}
    clock = clock + 120000
    eq(visible("D"), false, "behind a wall now: not listed")
    eq(known(), true, "but still a target 2 min later")
    osiv[key("IsInCombat", "P1")] = 1
    eq(known(), false, "in a fight: sight only")
    osiv[key("IsInCombat", "P1")] = nil
    world.P1.pos = { 55, 0, 0 }
    eq(known(), false, "45 m away")
    world.P1.pos = { 0, 0, 0 }
    world.D.comps.InventoryMember = {}
    eq(known(), false, "picked up / put away")
    world.D.comps.InventoryMember = nil
    osiv[key("GetRegion", "P1")] = "WLD_Main_A"
    V.levelFrame = nil
    eq(known(), false, "another level")
    osiv[key("GetRegion", "P1")] = "TUT_Avernus_C"
    clock = clock + V.SEEN_STATIC_MS
    eq(known(), false, "forgotten after 10 min")
    door("D2", { 8, 0, 0 })
    eq((V.itemKnown(V.context(), "D2")) == true, false, "never seen, not in sight")
    V.staticSeen = {}
    clock = clock + 1
    world.D.pos = { 10, 0, 0 }
    V.staticSeen = {}
    ray.fn = floorThen(function() return nil end)
    V.rayCache = {}
    eq(visible("D"), true, "seen again")
    world.D.pos = { 13, 0, 0 }
    ray.fn = floorThen(function(a) return { Position = { a[1] + 5, a[2], a[3] }, Distance = 5 } end)
    V.rayCache = {}
    eq(known(), false, "moved 3 m since (carried off and dropped): not where it was seen")
    V.staticSeen = {}
end)

test("undiscovered trap fixture (vent) stays hidden with a clear ray", function()
    setup(); button("T", { 5, 0, 0 })
    world.T.comps.ServerItem.CanUse = false
    osiv[key("IsTrap", "T")] = 1
    ray.fn = floorThen(function() return nil end)
    eq(visible("T"), false)
end)

-- The live probe L7 (S069-mystra): Statue of Mystra, kind usable, onstage, CanInteract 1, a Bound component without
-- AI bounds (V.bounds nil), CanSee 0 / HasLineOfSight 0 from all four members 2.8-4.7 m away, and every ray to 0.5/1.0/
-- 1.6 m above its feet hit the statue ~0.3 m short of its axis.
local function statue(u, pos, comps)
    world[u] = { pos = pos, comps = comps or { ServerItem = { CanUse = true }, Bound = { Bound = { AIBounds = {} } } } }
    osiv[key("IsOnStage", u)] = 1
    osiv[key("GetCanInteract", u)] = 1
end
local function hitsFirst(u) -- every non-floor ray: its first hit is u, 0.3 m short of the target point
    return floorThen(function(a, b)
        local d = BG3AI.Util.dist(a, b)
        local f = (d - 0.3) / d
        return { Position = { a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, a[3] + (b[3] - a[3]) * f },
            Shape = { PhysicsObject = { Entity = entity(u) } } }
    end)
end

test("unbounded usable (Mystra's statue, probe L7): CanSee/LOS 0, no AI bounds, rays hit it first -> perceived", function()
    setup(); statue("M", { 2.4, 0, 1.2 })
    eq(V.bounds(V.entity("M")), nil, "no AI bounds")
    ray.fn = hitsFirst("M")
    eq(visible("M"), true)
end)

test("unbounded usable behind a pillar (rays hit something else first) is not perceived", function()
    setup(); statue("M", { 2.4, 0, 1.2 }); world.W = { pos = { 1, 0, 0.5 } }
    ray.fn = hitsFirst("W")
    eq(visible("M"), false)
end)

test("unbounded usable: clear rays (nothing hit) prove nothing, only self hits count", function()
    setup(); statue("M", { 2.4, 0, 1.2 })
    ray.fn = floorThen(function() return nil end)
    eq(visible("M"), false)
end)

test("unbounded usable beyond sight range is not perceived", function()
    setup(); statue("M", { 19, 0, 0 })
    ray.fn = hitsFirst("M")
    eq(visible("M"), false)
end)

test("unbounded pickable item keeps CanSee/LOS (no self-hit rays)", function()
    setup(); statue("I", { 2, 0, 0 }, { ServerItem = { CanBePickedUp = true } })
    ray.fn = hitsFirst("I")
    eq(visible("I"), false)
end)

test("used by a party member: the item is known to the whole party (S069: Gale's interact after a hand click)", function()
    setup(); statue("M", { 2.4, 0, 1.2 }); world.W = { pos = { 1, 0, 0.5 } }
    V.staticSeen = {}
    ray.fn = hitsFirst("W") -- out of sight
    local function known() V.ctxCache = nil; return (V.itemKnown(V.context(), "M")) == true end
    eq(known(), false, "not seen, not used")
    V.noteUsed("NPC", "M", 1)
    eq(known(), false, "an NPC's use is not the party's")
    V.noteUsed("P1", "M", 0)
    eq(known(), false, "a failed use")
    V.noteUsed("P1", "M", 1)
    eq(known(), true, "used by a party member")
    eq(visible("M"), false, "still not listed (get_nearby keeps the strict rule)")
    V.staticSeen = {}
end)

-- ---- objective markers ----------------------------------------------------

local function markerSetup(targetPos, viaOsi)
    setup()
    N.journalData = {
        markers = { M1 = { type = "Trigger", uuid = "TRG", text = "hEsc", level = "TUT_Avernus_C", icon = "QuestMarker" } },
        objectives = { OBJ1 = { quest = "Q1", desc = "hObj", markers = { "M1" } } },
    }
    N.stepObjectives = { { id = "Q1", title = "hQ", steps = { { id = "Start", objective = "OBJ1" }, { id = "Later", objective = "OBJ2" } } } }
    N.activeCache = nil
    loca.hEsc, loca.hObj, loca.hQ = "Escape the ship", "Find a way out", "Escape the Nautiloid"
    osiv[key("QuestIsAccepted", "P1", "Q1")] = 1
    osiv[key("QuestUpdateIsUnlocked", "P1", "Q1", "Start")] = 1
    if viaOsi then osiv[key("GetPosition", "TRG")] = targetPos else world.TRG = { pos = targetPos } end
end

test("nav.markers: active objective with dist and dir", function()
    markerSetup({ -16, 0, 0 })
    local r = BG3AI.RPC.ops["nav.markers"]({})
    eq(#r.markers, 1)
    local m = r.markers[1]
    eq(m.label, "Escape the ship"); eq(m.quest, "Escape the Nautiloid"); eq(m.dist, 16); eq(m.dir, "W"); eq(m.here, true)
end)

test("nav.markers: trigger position via Osi.GetPosition", function()
    markerSetup({ 0, 0, 12 }, true)
    local m = BG3AI.RPC.ops["nav.markers"]({}).markers[1]
    eq(m.dist, 12); eq(m.dir, "N")
end)

test("nav.markers: other level -> no position", function()
    markerSetup({ 5, 0, 0 })
    N.journalData.markers.M1.level = "WLD_Main_A"
    local m = BG3AI.RPC.ops["nav.markers"]({}).markers[1]
    eq(m.here, nil); eq(m.dist, nil)
end)

test("nav.markers: a sub-quest's step unlocked under its parent gives its markers (S030/S031 Gauntlet)", function()
    markerSetup({ -16, 0, 0 })
    N.stepObjectives = { { id = "Q1_SUB_Relic", parent = "Q1", title = "hQ", steps = { { id = "Start", objective = "OBJ1" } } } }
    osiv[key("QuestIsAccepted", "P1", "Q1_SUB_Relic")] = 1
    -- the step is unlocked under the parent "Q1" only (markerSetup), never under the sub-quest id
    local r = BG3AI.RPC.ops["nav.markers"]({})
    eq(#r.markers, 1, "marker"); eq(r.markers[1].label, "Escape the ship")
end)

test("nav.markers: an unlocked step without QuestIsAccepted counts (SHA_Nightsong, companion quests: probe 23:3x)", function()
    markerSetup({ -16, 0, 0 })
    osiv[key("QuestIsAccepted", "P1", "Q1")] = 0
    eq(#BG3AI.RPC.ops["nav.markers"]({}).markers, 1, "marker shown")
    osiv[key("QuestUpdateIsUnlocked", "P1", "Q1", "Start")] = 0
    N.activeCache = nil
    eq(#BG3AI.RPC.ops["nav.markers"]({}).markers, 0, "no step unlocked: nothing")
end)

test("nav.markers: closed quest -> nothing", function()
    markerSetup({ 5, 0, 0 })
    osiv[key("QuestIsClosed", "Q1")] = 1
    eq(#BG3AI.RPC.ops["nav.markers"]({}).markers, 0)
end)

test("generated JournalMarkers.lua loads and has the Nautiloid escape", function()
    local d = dofile(LUA .. "/Server/Data/JournalMarkers.lua")
    local m = d.markers["TUT_NautiloidEscape_Escape"]
    assert(m and m.uuid == "31a613b5-9fb5-4949-8fc3-730cc82cec30", "escape marker")
    assert(d.objectives["TUT_NautiloidEscape_Start"], "escape objective")
end)

test("nav.activity: positions, running jobs, dialogue/combat flags", function()
    setup()
    world.P1.comps.ServerCharacter = { InDialog = true }
    osiv[key("IsInCombat", "P1")] = 1
    BG3AI.RPC.jobs = { [1] = { status = "running", kind = "move_to" }, [2] = { status = "done", kind = "x" } }
    local r = BG3AI.RPC.ops["nav.activity"]({})
    eq(r.jobs, 1); eq(r.job_kinds[1], "move_to"); eq(r.in_dialogue, true); eq(r.in_combat, true)
    eq(r.pos.P1[1], 0)
    BG3AI.RPC.jobs = nil
end)

-- ---- per-frame caches (read cost) -------------------------------------------

test("V.context is built once per game frame", function()
    local n = 0
    local party = V.party
    V.party = function() n = n + 1; return party() end
    N.frame = (N.frame or 0) + 1
    local a, b = V.context(), V.context()
    V.party = party
    eq(a == b, true, "same table in one frame"); eq(n, 1, "party read once")
    N.frame = N.frame + 1
    eq(V.context() ~= a, true, "rebuilt next frame")
end)

test("V.characters: one scan per frame, rows {entity, pos}", function()
    world.C1 = { pos = { 1, 0, 2 }, comps = { ServerCharacter = {} } }
    world.C2 = { pos = { 50, 0, 0 }, comps = { ServerCharacter = {} } }
    world.I1 = { pos = { 0, 0, 0 }, comps = { ServerItem = {} } }
    N.frame = (N.frame or 0) + 1
    charScans = 0
    local rows = V.characters()
    V.characters()
    eq(#rows, 2, "rows"); eq(charScans, 1, "scans in one frame")
    eq(type(rows[1][1]) == "table" and type(rows[1][2]) == "table", true, "{e, p}")
    world.C3 = { pos = { 3, 0, 3 }, comps = { ServerCharacter = {} } }
    N.frame = N.frame + 1
    eq(#V.characters(), 3, "next frame sees the new character"); eq(charScans, 2, "rescanned")
end)

test("V.charactersNear: radius over the frame cache, nearest-center distance", function()
    world.C1 = { pos = { 1, 0, 2 }, comps = { ServerCharacter = {} } }
    world.C2 = { pos = { 50, 0, 0 }, comps = { ServerCharacter = {} } }
    world.C3 = { pos = { 3, 0, 3 }, comps = { ServerCharacter = {} } }
    world.C4 = { pos = { 0, 40, 0 }, comps = { ServerCharacter = {} } } -- in the xz box, 40 m up
    N.frame = (N.frame or 0) + 1
    charScans = 0
    local near = V.charactersNear({ 0, 0, 0 }, 10)
    eq(#near, 2, "C1, C3 within 10 m of a single pos")
    local two = V.charactersNear({ { 0, 0, 0 }, { 48, 0, 0 } }, 10)
    eq(#two, 3, "any of several centers")
    for _, r in ipairs(two) do
        if r[2][1] == 50 then eq(r[3], 2.0, "distance to the nearest center") end
    end
    eq(#V.charactersNear({}, 10), 0, "no centers")
    eq(charScans, 1, "all queries share one scan this frame")
    world.C1, world.C2, world.C3, world.C4 = nil, nil, nil, nil
end)

test("bounds: read once per item within BOUNDS_TTL_MS (look/frontier read them twice per item)", function()
    setup()
    door("D9", { 5, 0, 0 })
    local e = V.entity("D9")
    eq(V.bounds(e).height, 3.0, "first read")
    world.D9.comps.Bound = { Bound = { AIBounds = { Hit = { Height = 9.0, Radius = 1.0 } } } }
    eq(V.bounds(V.entity("D9")).height, 3.0, "cached")
    clock = clock + V.BOUNDS_TTL_MS + 1
    eq(V.bounds(V.entity("D9")).height, 9.0, "re-read after the TTL")
    world.D9.comps.Bound = nil
    clock = clock + V.BOUNDS_TTL_MS + 1
    eq(V.bounds(V.entity("D9")), nil, "no Bound component")
    eq(V.bounds(V.entity("D9")), nil, "cached miss stays nil")
end)

test("item index: built in <= INDEX_BUDGET_MS slices per tick, no closure per item; dirty and level rebuilds", function()
    setup()
    -- 1000 floor items, each read costs 0.05 ms of game thread
    for i = 1, 1000 do
        world["I" .. i] = { pos = { i % 50, 0, math.floor(i / 50) }, comps = { ServerItem = {} } }
    end
    local realGet = Ext.Entity.GetAllEntitiesWithComponent
    Ext.Entity.GetAllEntitiesWithComponent = function(name)
        local out = realGet(name)
        if name == "ServerItem" then
            for i, e in ipairs(out) do
                out[i] = setmetatable({}, { __index = function(_, k) clock = clock + 0.05; return e[k] end })
            end
        end
        return out
    end
    local ok, err = pcall(function()
        V.index = { cells = {}, ready = false, built = 0 }
        V.indexTick() -- starts a build
        local ticks, worst = 0, 0
        while V.index.building do
            local t0 = clock
            V.indexTick()
            worst = math.max(worst, clock - t0)
            ticks = ticks + 1
            assert(ticks < 10000, "never finished")
        end
        eq(V.index.ready, true, "built")
        assert(worst <= V.INDEX_BUDGET_MS + V.INDEX_CLOCK_EVERY * 0.15 + 0.01, "slice " .. worst .. " ms")
        assert(ticks > 3, "several slices: " .. ticks)
        eq(#V.itemsAround({ 25, 0, 10 }, 100), 1000, "every item indexed")
        -- idle: no rebuild before INDEX_REFRESH_MS
        clock = clock + 1000; V.indexTick(); eq(V.index.building, nil, "idle")
        -- an item left an inventory: rebuild after INDEX_DIRTY_MS
        V.indexDirty(); V.indexTick(); eq(V.index.building, nil, "not at once")
        clock = clock + V.INDEX_DIRTY_MS; V.indexTick(); eq(V.index.building ~= nil, true, "dirty rebuild")
        while V.index.building do V.indexTick() end
        -- a level change is seen within INDEX_LEVEL_CHECK_MS
        osiv[key("GetRegion", "P1")] = "WLD_Main_A"
        clock = clock + V.INDEX_LEVEL_CHECK_MS; V.indexTick(); eq(V.index.building ~= nil, true, "level rebuild")
        eq(#V.itemsAround({ 25, 0, 10 }, 100), 1000, "a query mid level-rebuild finishes it")
        eq(V.index.building, nil, "finished"); eq(V.index.level, "WLD_Main_A", "new level")
    end)
    Ext.Entity.GetAllEntitiesWithComponent = realGet
    for i = 1, 1000 do world["I" .. i] = nil end
    assert(ok, err)
end)

test("item effects: Mausoleum mural states named, generic glow only when asked, %%% EMPTY is no name", function()
    local stats = {
        SHA_MAUSOLEUM_MURAL_INPROGRESS = { StatusType = "EFFECT", StatusEffect = "a809", DisplayName = "h1;1" },
        VFX_ONLY = { StatusType = "EFFECT", StatusEffect = "b1", DisplayName = "h2;1" },
        NAMED = { StatusType = "EFFECT", StatusEffect = "c1", DisplayName = "h3;1" },
        NO_VFX = { StatusType = "EFFECT", StatusEffect = "", DisplayName = "h2;1" },
    }
    local realStats = Ext.Stats
    Ext.Stats = { Get = function(n) return stats[n] end }
    loca.h1, loca.h2, loca.h3 = "%%% EMPTY", "%%% EMPTY", "Burning"
    V.itemEffectCache = {}
    local ok, err = pcall(function()
        eq(BG3AI.Util.loca("h2;1"), nil, "%%% EMPTY")
        eq(BG3AI.Util.loca("h3;1"), "Burning", "a real name")
        eq(V.itemEffect("SHA_MAUSOLEUM_MURAL_INPROGRESS"), "lit", "known")
        eq(V.itemEffect("SHA_MAUSOLEUM_MURAL_WRONGORDER"), "flashing", "known 2: the look only (audit C11)")
        eq(V.itemEffect("SHA_MAUSOLEUM_MURAL_RIGHTORDER"), "glowing", "known 3: no 'solved' verdict")
        eq(V.itemEffect("VFX_ONLY"), nil, "generic not asked")
        eq(V.itemEffect("VFX_ONLY", true), "glowing", "generic")
        eq(V.itemEffect("NAMED", true), nil, "a named status is a status, not a glow")
        eq(V.itemEffect("NO_VFX", true), nil, "no visual effect")
        world.M = { pos = { 1, 0, 0 }, comps = { StatusContainer = { Statuses = {
            [{}] = "SHA_MAUSOLEUM_MURAL_INPROGRESS", [{}] = "NAMED" } } } }
        eq(V.itemEffects(V.entity("M"), true), "lit", "entity")
        world.N = { pos = { 1, 0, 0 }, comps = {} }
        eq(V.itemEffects(V.entity("N"), true), nil, "no container")
    end)
    Ext.Stats = realStats
    assert(ok, err)
end)

print(("%d/%d passed"):format(count - failures, count))
os.exit(failures == 0 and 0 or 1)
