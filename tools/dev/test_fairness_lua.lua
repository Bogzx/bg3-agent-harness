-- Offline fairness tests for Server/Navigation.lua (audit 2026-10-10,
-- research/fairness-audit-2026-10-10.md): hazard patches only when a party
-- member sees them, no markers for hidden quests, explored places named only
-- after unlocked waypoints, no floor heights at far route goals.
-- Ext, Osi and Visibility are stubbed; Shared/Util.lua and Shared/RPC.lua are real.
-- Run: tools/dev/test_fairness_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local P1 = "aaaaaaaa-0000-0000-0000-000000000001"
local WP_A, WP_B = "bbbbbbbb-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b"
local world = { [P1] = { 0, 0, 0 }, [WP_A] = { 10, 0, 0 }, [WP_B] = { -10, 0, 0 } }
local fire = function() return false end
local seen = function() return true end -- pointSeen(pt) -> true|false|nil
local unlocked = {}                     -- PartyWaypoints names
local accepted = {}                     -- quest id -> accepted
local heightsCalls = {}

local QUESTS = [[
<node id="Quest">
<attribute id="QuestID" type="FixedString" value="Q_Shown"/>
<attribute id="QuestTitle" type="TranslatedString" handle="hShown"/>
<node id="QuestStep"><attribute id="ID" type="FixedString" value="S1"/><attribute id="Objective" type="FixedString" value="OBJ_SHOWN"/></node>
</node>
<node id="Quest">
<attribute id="QuestID" type="FixedString" value="Q_Tech"/>
<attribute id="QuestTitle" type="TranslatedString" handle="hTech"/>
<attribute id="QuestVisiblity" type="bool" value="false"/>
<node id="QuestStep"><attribute id="ID" type="FixedString" value="S1"/><attribute id="Objective" type="FixedString" value="OBJ_TECH"/></node>
</node>
]]

local MARKERS = {
    objectives = {
        OBJ_SHOWN = { markers = { "M_SHOWN" }, desc = "dShown" },
        OBJ_TECH = { markers = { "M_TECH" }, desc = "dTech" },
    },
    markers = {
        M_SHOWN = { uuid = WP_A, text = "tShown", level = "", icon = "", radius = 6 },
        M_TECH = { uuid = WP_B, text = "tTech", level = "", icon = "", radius = 0 },
    },
}

Ext = {
    IsServer = function() return true end,
    Utils = { MonotonicTime = function() return 1000 end, PrintWarning = function() end, Print = function() end,
        GameTime = function() return 0 end, GetGameState = function() return "Running" end },
    Events = { Tick = { Subscribe = function() end } },
    Level = {
        GetHeightsAt = function(x, z) heightsCalls[#heightsCalls + 1] = { x, z }; return { 0.0 } end,
        GetTileDebugInfo = function(p)
            return { GroundSurface = fire(p[1], p[3]) and "Fire" or "None", CloudSurface = "None", Flags = {} }
        end,
        BeginPathfindingImmediate = function(e, t) return { target = t, Nodes = {} } end,
        FindPath = function(path)
            path.Nodes[1] = { Position = { path.target[1], path.target[2], path.target[3] } }
            return true
        end,
        ReleasePath = function() end,
    },
    Entity = {
        GetAllEntitiesWithComponent = function(c)
            if c ~= "PartyWaypoints" then return {} end
            local set = {}
            for _, n in ipairs(unlocked) do set[{ Name = n, Level = "LVL" }] = true end
            -- a set-like container: pairs yields the entries as keys in the game; values here
            local wl = {}
            for w in pairs(set) do wl[#wl + 1] = w end
            return { { PartyWaypoints = { Waypoints = wl } } }
        end,
    },
    IO = {
        LoadFile = function(path) if path and path:find("quest_prototypes") then return QUESTS end return nil end,
        SaveFile = function() end,
    },
    Json = { Parse = function() return nil end, Stringify = function() return "{}" end },
    Loca = { GetTranslatedStringKey = function() return nil end, GetTranslatedString = function(h) return "L:" .. h end },
    Require = function(p) if p:find("JournalMarkers") then return MARKERS end return nil end,
}
Osi = {
    GetHostCharacter = function() return P1 end,
    GetRegion = function() return "LVL" end,
    QuestIsAccepted = function(_, q) return accepted[q] and 1 or 0 end,
    QuestIsClosed = function() return 0 end,
    QuestUpdateIsUnlocked = function() return 1 end,
    DB_WaypointInfo = { Get = function()
        return { { nil, "WAYP_A", WP_A }, { nil, "WAYP_B", WP_B } }
    end },
}

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
local CTX = { party = { P1 }, is_party = { [P1] = true }, observers = { { uuid = P1, pos = { 0, 0, 0 }, sight = 18 } } }
BG3AI.Visibility = {
    osi = function(f, ...) if f then return f(...) end end,
    entity = function(u)
        if not world[u] then return nil end
        return { uuid = u, Level = { LevelName = "LVL" } }
    end,
    pos = function(e) local p = world[e.uuid]; return p and { p[1], p[2], p[3] } or nil end,
    party = function() return { P1 } end,
    uuid = function(u) return u end,
    context = function() return CTX end,
    pointSeen = function(ctx, pt) return seen(pt) end,
    itemsAround = function() return {} end,
}
dofile(LUA .. "/Server/Navigation.lua")
local N, RPC = BG3AI.Nav, BG3AI.RPC

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    fire = function() return false end
    seen = function() return true end
    unlocked, accepted, heightsCalls = {}, {}, {}
    N.frame = (N.frame or 0) + 1
    N.activeCache, N.stepObjectives = nil, nil
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end

-- two patches: one east (x 4..6), one west behind a "wall" (x -6..-4)
local function twoFires(x, z) return math.abs(z) <= 1 and ((x >= 4 and x <= 6) or (x >= -6 and x <= -4)) end

test("hazards: a patch nobody sees (behind a wall) is not listed", function()
    fire = twoFires
    seen = function(pt) return pt[1] > 0 end
    local hz = N.hazardPatches(P1, 12, 1.0)
    eq(#hz, 1, "patches"); eq(hz[1].dir, "E", "the visible one")
    eq(N.lastHiddenPatches, 1, "hidden count")
end)

test("hazards: a patch seen at any sampled cell is listed", function()
    fire = twoFires
    seen = function(pt) return pt[1] > 0 or pt[1] <= -5.5 end -- the far edge of the west patch peeks out
    eq(#N.hazardPatches(P1, 12, 1.0), 2, "patches")
end)

test("hazards: the patch underfoot is always listed", function()
    fire = function(x, z) return math.abs(x) <= 1 and math.abs(z) <= 1 end
    seen = function() return false end
    local hz = N.hazardPatches(P1, 12, 1.0)
    eq(#hz, 1, "patches"); eq(hz[1].under, true, "under")
end)

test("hazards: no usable ray API (nil) keeps the patch (fail-safe for walking)", function()
    fire = twoFires
    seen = function() return nil end
    eq(#N.hazardPatches(P1, 12, 1.0), 2, "patches")
end)

test("nav.hazards: hidden patches don't reach the op's output", function()
    fire = twoFires
    seen = function(pt) return pt[1] > 0 end
    local r = RPC.ops["nav.hazards"]
    if not r then error("nav.hazards not registered") end
    local out = r({ char = P1 })
    eq(#out.hazards, 1, "hazards")
end)

test("markers: a hidden (technical) quest's objective has no marker", function()
    accepted = { Q_Shown = true, Q_Tech = true }
    local list = N.objectiveMarkers(CTX)
    eq(#list, 1, "markers")
    eq(list[1].quest, "L:hShown", "quest")
end)

test("markers: an area marker says so (area_m)", function()
    accepted = { Q_Shown = true }
    local list = N.objectiveMarkers(CTX)
    eq(list[1].area_m, 6, "area")
end)

test("explored: places are named only after unlocked waypoints", function()
    N.waypointItems = nil
    local h = RPC.ops["nav.explored"]
    -- record a place: run the breadcrumb store directly through the op's data
    PersistentVars = { nav = { levels = { LVL = { cells = {}, n = 0, bbox = nil,
        places = { { x = -9, y = 0, z = 0 } } } } } }
    if not h then error("nav.explored not registered") end
    local out = h({})
    eq(out.recent[1].near, nil, "undiscovered WAYP_B not named")
    unlocked = { "WAYP_B" }
    N.waypointItems = nil
    out = h({})
    eq(out.recent[1].near, "WAYP_B", "unlocked one named")
end)

test("route: a goal past path range gets no floor heights", function()
    local r = N.routeAhead(CTX, P1, { 500, 0, 0 }, false)
    eq(r.too_far, true, "too far"); eq(r.levels_goal, nil, "levels_goal")
    for _, c in ipairs(heightsCalls) do
        if c[1] == 500 then error("probed heights at the far goal") end
    end
end)

test("route: a goal in range still gets them", function()
    local r = N.routeAhead(CTX, P1, { 20, 0, 0 }, false)
    eq(type(r.levels_goal), "table", "levels_goal")
end)

print(string.format("%d/%d passed", count - failures, count))
os.exit(failures == 0 and 0 or 1)
