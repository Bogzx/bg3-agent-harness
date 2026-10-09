-- Offline tests for Server/Visibility.lua (large-object perception) and
-- Server/Navigation.lua (objective markers), with Ext/Osi stubbed.
-- Run: tools/vis/test_lua.sh  (Lua 5.3 in Docker), or `lua5.3 test_lua_vis.lua <Lua dir>`.

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

local clock = 0
local world = {}      -- uuid -> {pos, comps = {...}}
local osiv = {}       -- "Fn:arg1:arg2" -> value
local ray = {}        -- ray.fn(a, b) -> hit | nil
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
    Level = { RaycastClosest = function(a, b, ...) return ray.fn(a, b, ...) end },
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
        Bound = { Bound = { AIBounds = { Hit = { Height = 3.0, Radius = 2.0 } } } } } }
    osiv[key("IsOnStage", "G")] = 1
    ray.fn = floorThen(function() return nil end)
    local ctx = V.context()
    eq(V.characterVisible(ctx, "G", V.entity("G"), { 10, 0, 0 }), false)
end)

test("dev.perception_probe counts ray-only doors", function()
    setup(); door("D", { 10, 0, 0 }); door("D2", { 4, 0, 0 })
    osiv[key("HasLineOfSight", "P1", "D2")] = 1
    ray.fn = floorThen(function() return nil end)
    V.index = { cells = {}, ready = false, built = 0 }
    local r = BG3AI.RPC.ops["dev.perception_probe"]({ radius = 30 })
    eq(r.counts.large, 2); eq(r.counts.by_ray, 1); eq(r.counts.by_sight, 1); eq(r.calibrated, true)
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

print(("%d/%d passed"):format(count - failures, count))
os.exit(failures == 0 and 0 or 1)
