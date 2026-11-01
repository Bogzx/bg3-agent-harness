-- Offline tests for Server/StateOps.lua's state.status, the read every get_status/look and most tools start from
-- (140 calls in the recorded fixtures, no Lua test before). Pins the reply the bridge reads: party rows, the active
-- member, region, combat/turn-based/dialogue flags, gold, the camp roster, the op's timing in S.lastMs.
-- Ext, Osi and RPC are stubbed; Shared/Util.lua, Server/Visibility.lua and Server/State.lua (with its parts) are real.
-- Run: tools/dev/test_state_ops.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local TAV = "aaaaaaaa-0000-0000-0000-000000000001"
local GALE = "aaaaaaaa-0000-0000-0000-000000000002"
local KARLACH = "aaaaaaaa-0000-0000-0000-000000000005" -- recruited, waiting at camp

local ents, osi, names, clock -- reset per test
local function reset()
    ents = {
        [TAV] = { Health = { Hp = 12, MaxHp = 14, TemporaryHp = 0 }, EocLevel = { Level = 2 },
            Transform = { Transform = { Translate = { 10.04, 0.0, -5.06 } } }, ServerCharacter = {}, Origin = {} },
        [GALE] = { Health = { Hp = 8, MaxHp = 9, TemporaryHp = 3 }, EocLevel = { Level = 1 },
            AvailableLevel = { Level = 2 }, Transform = { Transform = { Translate = { 12.0, 0.0, -5.0 } } },
            ServerCharacter = {}, Origin = {} },
        [KARLACH] = { EocLevel = { Level = 2 }, CampPresence = {}, ServerCharacter = {}, Origin = {} },
    }
    osi = { party = { { "S_Player_Tav_" .. TAV }, { "S_Player_Gale_" .. GALE } }, region = "WLD_Main_A",
        gold = 120, combat = {}, ftb = {}, dead = {}, downed = {}, offstage = {}, host = TAV, chained = true }
    names = { [TAV] = "Tav", [GALE] = "Gale", [KARLACH] = "Karlach" }
    clock = 1000
end
reset()

Ext = {
    Utils = { MonotonicTime = function() clock = clock + 3 return clock end, PrintWarning = function() end,
        Print = function() end },
    Events = { Tick = { Subscribe = function() end } },
    Loca = { GetTranslatedString = function(h) return names[h] end },
    Entity = {
        Get = function(u) local e = ents[u] if e then e.Uuid = { EntityUuid = u } end return e end,
        GetAllEntitiesWithComponent = function() return {} end,
    },
}
Osi = {
    DB_PartyMembers = { Get = function() return osi.party end },
    DB_Origins = { Get = function() return { { "S_Player_Karlach_" .. KARLACH } } end },
    DB_PlayerInCamp = { Get = function() return {} end },
    GetDisplayName = function(u) return u end, -- names[] maps the uuid as its loca handle
    GetRegion = function() return osi.region end,
    GetHostCharacter = function() return osi.host end,
    PartyGetGold = function() return osi.gold end,
    IsInCombat = function(u) return osi.combat[u] and 1 or 0 end,
    IsInForceTurnBasedMode = function(u) return osi.ftb[u] and 1 or 0 end,
    IsDead = function(u) return osi.dead[u] and 1 or 0 end,
    HasActiveStatus = function(u, s) return s == "DOWNED" and osi.downed[u] and 1 or 0 end,
    IsOnStage = function(u) return osi.offstage[u] and 0 or 1 end,
    InSamePartyGroup = function() return osi.chained and 1 or 0 end,
    IsSummon = function() return 0 end,
    IsPartyFollower = function() return 0 end,
}

local handlers = {} -- RPC name -> handler
BG3AI = { RPC = { register = function(name, fn) handlers[name] = fn end, push = function() end } }
dofile(LUA .. "/Shared/Util.lua")
Ext.Require = function(p) return dofile(LUA .. "/" .. p) end -- the modules load their parts
dofile(LUA .. "/Server/Visibility.lua")
dofile(LUA .. "/Server/State.lua")
local V, S = BG3AI.Visibility, BG3AI.State
local realCombat = S.combat

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    reset()
    V.ctxCache, V.ctxFrame, S.lastMs = nil, nil, nil
    S.combat = realCombat
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what)
    if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end
end
local function status() return handlers["state.status"]({}) end
local function row(st, u)
    for _, r in ipairs(st.party) do if r.uuid == u then return r end end
    error("no party row for " .. u, 2)
end

test("exploring: party rows, the selected member, region, gold, no combat", function()
    ents[GALE].ClientControl = {} -- the player has Gale selected
    local st = status()
    eq(#st.party, 2, "party rows")
    eq(st.active, GALE, "active")
    eq(st.level, "WLD_Main_A", "level")
    eq(st.region, "Wilderness", "region")
    eq(st.gold, 120, "gold")
    eq(st.in_combat, false, "in_combat")
    eq(st.turn_based, false, "turn_based")
    eq(st.in_dialogue, false, "in_dialogue")
    eq(st.combat, nil, "combat")
    local t, g = row(st, TAV), row(st, GALE)
    eq(t.name, "Tav", "name")
    eq(t.hp, 12, "hp")
    eq(t.max_hp, 14, "max_hp")
    eq(t.level, 2, "level")
    eq(t.temp_hp, nil, "no temp_hp at 0")
    eq(t.active, nil, "Tav not active")
    eq(t.chained, true, "Tav chained to the active member")
    eq(g.active, true, "Gale active")
    eq(g.temp_hp, 3, "temp_hp")
    eq(g.level_up, 2, "Gale can reach level 2")
    eq(g.chained, nil, "the active member has no chained flag")
    eq(t.region, "WLD_Main_A", "row region")
    eq(type(t.pos), "table", "pos")
end)

test("a dead member that kept ClientControl doesn't outrank the living member selected", function()
    -- launch-v1 act1_level_3 r2: set_party selected Tav while Gale's corpse (earlier in the party) still held it
    osi.party = { { "S_Player_Gale_" .. GALE }, { "S_Player_Tav_" .. TAV } }
    ents[GALE].ClientControl, ents[TAV].ClientControl, osi.dead[GALE] = {}, {}, true
    eq(status().active, TAV, "the living holder")
    ents[TAV].ClientControl = nil
    V.ctxCache = nil
    eq(status().active, GALE, "the dead member when it is the only holder")
end)

test("no member selected: the host is active", function()
    eq(status().active, TAV, "active")
    osi.host = nil
    V.ctxCache = nil
    eq(status().active, TAV, "the first member without a host")
end)

test("an unknown level id is sent as the region", function()
    osi.region = "XYZ_Odd_A"
    eq(status().region, "XYZ_Odd_A", "region")
end)

test("a fight: in_combat, turn_based and the round/current from state.combat", function()
    osi.combat[GALE] = true
    S.combat = function() return { active = true, round = 3, current = GALE, order = {} } end
    local st = status()
    eq(st.in_combat, true, "in_combat")
    eq(st.turn_based, true, "turn_based")
    eq(st.combat.round, 3, "round")
    eq(st.combat.current, GALE, "current")
    eq(row(st, GALE).in_combat, true, "Gale's row")
    eq(row(st, TAV).in_combat, nil, "Tav's row")
end)

test("forced turn-based out of combat is turn_based, not in_combat", function()
    osi.ftb[TAV] = true
    local st = status()
    eq(st.in_combat, false, "in_combat")
    eq(st.turn_based, true, "turn_based")
    eq(st.combat, nil, "combat")
end)

test("the active member in a dialogue", function()
    ents[TAV].ServerCharacter.InDialog = true
    eq(status().in_dialogue, true, "in_dialogue")
end)

test("dead, downed and off-stage members are flagged", function()
    osi.dead[TAV], osi.downed[GALE] = true, true
    osi.offstage[GALE] = true
    local st = status()
    eq(row(st, TAV).dead, true, "dead")
    eq(row(st, GALE).downed, true, "downed")
    eq(row(st, GALE).off_stage, true, "off_stage")
end)

test("a member without an entity (mid-load) is a uuid + name row, not an error", function()
    ents[GALE] = nil
    local g = row(status(), GALE)
    eq(g.name, "Gale", "name")
    eq(g.hp, nil, "hp")
end)

test("the camp roster lists the recruited companion waiting at camp", function()
    local camp = status().camp
    eq(#camp, 1, "camp rows")
    eq(camp[1].uuid, KARLACH, "uuid")
    eq(camp[1].name, "Karlach", "name")
end)

test("the op's time goes to S.lastMs (state.timings)", function()
    status()
    eq(type(S.lastMs) == "table" and S.lastMs["state.status"] ~= nil, true, "lastMs entry")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
