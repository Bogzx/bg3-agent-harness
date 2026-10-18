-- Offline tests for Server/Visibility.lua's character filter (V.characterVisible).
-- vis V22, S081 03:01: get_nearby radius 60 at camp listed Withers and Scratch, not Karlach (waiting at camp,
-- past the 18.5 m sight range), and talk_to Karlach was "not visible"; swap_party then walked up to her.
-- vis V22b: State.campRoster said "at camp" by GetRegion (the level id), so on a level shared with the camp (the
-- Act 3 camp and the Lower City, S081) a camp companion's "!" and camp spot showed from anywhere in the city.
-- vis V23, S083 03:44: state.entity sent no position for a visible NPC, so travel_to/talk_to's walk to Nocturne
-- 90 m off was one move_to (capped at 60 m) instead of legs.
-- vis V25, S084 04:16: who of the party stands on a floor plate (S.plateOn).
-- vis V24, S083 03:55:56: a lock read flipping locked -> unlocked counts as the game unlocking it (S.lockRead).
-- Ext, Osi and RPC are stubbed; Shared/Util.lua, Server/Visibility.lua and Server/State.lua are real.
-- Run: tools/dev/test_fairness_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local CLAUDE = "aaaaaaaa-0000-0000-0000-000000000004"
local KARLACH = "aaaaaaaa-0000-0000-0000-000000000005" -- recruited, waiting at camp
local WITHERS = "cccccccc-0000-0000-0000-000000000352"
local GOB = "cccccccc-0000-0000-0000-000000000009"

-- S081: the party arrived at (-1421.2, 8.4, -2601.2); Karlach stands at her camp spot ~35 m off.
local ents = {
    [CLAUDE] = { pos = { -1421.2, 8.4, -2601.2 } },
    [KARLACH] = { pos = { -1450.0, 8.0, -2620.0 }, CampPresence = {}, HasExclamationDialog = {} },
    [WITHERS] = { pos = { -1418.7, 8.3, -2585.5 } },
    [GOB] = { pos = { -1450.0, 8.0, -2620.0 } },
}
local inCamp = {}      -- DB_PlayerInCamp rows
local sees = {}        -- uuid -> CanSee from Claude
local onStage = {}     -- uuid -> false when off stage

Ext = {
    Utils = { MonotonicTime = function() return 1000 end, PrintWarning = function() end, Print = function() end },
    Events = { Tick = { Subscribe = function() end } },
    Entity = { Get = function(u)
        local d = ents[u]
        if not d then return nil end
        local e = { Uuid = { EntityUuid = u }, Transform = { Transform = { Translate = d.pos } } }
        e.CampPresence, e.HasExclamationDialog = d.CampPresence, d.HasExclamationDialog
        e.ServerCharacter = {}
        return e
    end },
}
Osi = {
    IsOnStage = function(u) return onStage[u] == false and 0 or 1 end,
    IsInvisibleByScript = function() return 0 end,
    IsInvisible = function() return 0 end,
    HasActiveStatus = function() return 0 end,
    IsAlly = function(_, u) return u == KARLACH and 1 or 0 end,
    CanSee = function(_, u) return sees[u] and 1 or 0 end,
    HasLineOfSight = function() return 1 end,
    DB_PlayerInCamp = { Get = function() if inCamp == "error" then error("no such DB") end return inCamp end },
    DB_Origins = { Get = function() return { { "S_Player_Karlach_" .. KARLACH }, { "S_Player_Claude_" .. CLAUDE } } end },
    GetRegion = function() return "CTY_Main_A" end, -- the camp and the city: one level
    GetDisplayName = function() return nil end,
    IsDead = function() return 0 end,
}

local handlers = {}    -- RPC name -> handler
BG3AI = { RPC = { register = function(name, fn) handlers[name] = fn end, push = function() end } }
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/Visibility.lua")
dofile(LUA .. "/Server/State.lua")
local V, S = BG3AI.Visibility, BG3AI.State

local function ctx()
    return { party = { CLAUDE }, is_party = { [CLAUDE] = true },
        observers = { { uuid = CLAUDE, pos = ents[CLAUDE].pos, sight = 18.5 } } }
end
local function visible(u) return V.characterVisible(ctx(), u, V.entity(u)) end

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    inCamp, sees, onStage = {}, {}, {}
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what) if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end end

test("S081: a camp companion past sight range is visible while the party is at camp", function()
    inCamp = { { "S_Player_Claude_" .. CLAUDE } } -- the DB stores prefixed names
    eq(visible(KARLACH), true, "Karlach at camp")
    eq(visible(WITHERS), true, "Withers within sight")
end)

test("the same spot without CampPresence keeps the sight rule", function()
    inCamp = { { CLAUDE } }
    eq(visible(GOB), false, "a stranger 35 m off, not seen by the engine")
    sees[GOB] = true
    eq(visible(GOB), true, "the engine sees it")
end)

test("a camp companion is not visible from outside the camp", function()
    eq(visible(KARLACH), false, "party not at camp")
    inCamp = { { "S_GLO_Someone_" .. GOB } } -- a non-party row doesn't count
    eq(visible(KARLACH), false, "only a party member's row")
end)

test("an off-stage camp companion stays hidden", function()
    inCamp = { { CLAUDE } }
    onStage[KARLACH] = false
    eq(visible(KARLACH), false, "off stage")
end)

local function karlachRow()
    for _, o in ipairs(S.campRoster(ctx(), true)) do if o.uuid == KARLACH then return o end end
    error("Karlach not in the camp roster", 2)
end

test("campRoster on the camp's level but away from camp: no camp spot, no \"!\"", function()
    local o = karlachRow()
    eq(o.pos, nil, "pos")
    eq(o.wants_talk, nil, "wants_talk")
end)

test("campRoster at camp: the camp spot and the \"!\"", function()
    inCamp = { { "S_Player_Claude_" .. CLAUDE } }
    local o = karlachRow()
    eq(o.pos and o.pos[1], -1450.0, "pos x")
    eq(o.wants_talk, true, "wants_talk")
end)

test("campRoster with DB_PlayerInCamp unreadable fails closed", function()
    inCamp = "error"
    local o = karlachRow()
    eq(o.pos, nil, "pos")
    eq(o.wants_talk, nil, "wants_talk")
    eq(visible(KARLACH), false, "visible")
end)

test("S083: state.entity gives a visible NPC's position (travel_to walks legs from it)", function()
    sees[WITHERS] = true
    V.context = ctx
    local o = handlers["state.entity"]({ uuid = WITHERS })
    eq(o.pos and o.pos[1], -1418.7, "pos x")
    eq(o.pos and o.pos[3], -2585.5, "pos z")
end)

test("state.entity of an unseen NPC still refuses", function()
    V.context = ctx
    local ok, err = pcall(handlers["state.entity"], { uuid = GOB })
    eq(ok, false, "refused")
    eq(err, "not visible", "error")
end)

test("V24 S083: a listing's locked read then unlocked read tells interact the game just unlocked it", function()
    local seen = {}
    BG3AI.Actions = { unlock_seen = function(u) seen[#seen + 1] = u end }
    local DOOR = "dddddddd-0000-0000-0000-000000000720"
    local lockedNow = 1
    Osi.IsLocked = function(u) return u == DOOR and lockedNow or 0 end
    eq(S.lockRead(DOOR), true, "locked before Knock")
    eq(#seen, 0, "no unlock yet")
    lockedNow = 0
    eq(S.lockRead(DOOR), false, "unlocked after Knock")
    eq(seen[1], DOOR, "unlock_seen")
    eq(S.lockRead(DOOR), false, "read again")
    eq(#seen, 1, "one unlock per locked -> unlocked change")
    eq(S.lockRead(GOB), false, "never locked")
    eq(#seen, 1, "an item never seen locked is no unlock")
    Osi.IsLocked, BG3AI.Actions = nil, nil
end)

test("V25 S084: S.plateOn names the party members standing on a floor plate", function()
    local c = ctx()
    local plate = { -1421.6, 8.4, -2601.0 } -- 0.45 m from Claude
    local on = S.plateOn(c, plate, nil)
    eq(on and on[1], CLAUDE, "Claude on it")
    eq(S.plateOn(c, { -1419.0, 8.4, -2601.2 }, nil), nil, "2.2 m off: not on it")
    eq(S.plateOn(c, { -1421.2, 11.0, -2601.2 }, nil), nil, "a floor above")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
