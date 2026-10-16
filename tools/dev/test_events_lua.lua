-- Offline tests for Server/Events.lua fairness/correctness (audit 2026-10-13):
-- ACT-14 the `cast` event carries the engine spell id only for party casters,
-- ACT-15 approval changes are reported from the first one after a load (the
-- poll seeds E.approval from GetApprovalRating).
-- Ext, Osi, RPC and Visibility are stubbed; Shared/Util.lua is real.
-- Run: tools/dev/test_events_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local HOST = "aaaaaaaa-0000-0000-0000-000000000001"
local SH = "aaaaaaaa-0000-0000-0000-000000000002" -- companion in the party
local GOB = "cccccccc-0000-0000-0000-000000000009" -- a visible enemy

local listeners, tick = {}, nil
local pushed = {}
local ratings = { [SH .. "|" .. HOST] = 10 }

Ext = {
    Osiris = { RegisterListener = function(name, _, _, fn) listeners[name] = fn end },
    Utils = { MonotonicTime = function() return 100000 end, PrintWarning = function(m) print("WARN " .. m) end,
        Print = function() end, GetGameState = function() return "Running" end },
    Events = { Tick = { Subscribe = function(_, fn) tick = fn end } },
    Stats = { Get = function(id) return ({
        Target_FireBolt = { DisplayName = "hFire", SpellRoll = { Default = "Attack(AttackType.RangedSpellAttack)" } },
        Projectile_GOB_SecretHint = { DisplayName = "hScimitar", SpellRoll = { Default = "" } },
    })[id] end },
    Loca = { GetTranslatedString = function(h) return ({ hFire = "Fire Bolt", hScimitar = "Scimitar Slash" })[h] end },
}
Osi = {
    GetHostCharacter = function() return HOST end,
    GetApprovalRating = function(owner, rated) return ratings[owner .. "|" .. rated] end,
    PartyGetGold = function() return 50 end,
    CombatGetGuidFor = function() return nil end,
}

local party = { HOST, SH }
local isParty = { [HOST] = true, [SH] = true }
BG3AI = {
    RPC = { push = function(kind, data) pushed[#pushed + 1] = { kind = kind, data = data } end,
        register = function() end },
    Visibility = {
        osi = function(fn, ...) local ok, r = pcall(fn, ...) if ok then return r end return nil end,
        uuid = function(x) return type(x) == "string" and x:match("%x+%-%x+%-%x+%-%x+%-%x+$") or nil end,
        entity = function(u) return { ServerCharacter = {}, _u = u } end,
        context = function() return { party = party, is_party = isParty, observers = {}, rules = {} } end,
        characterVisible = function(_, u) return u == GOB end,
        name = function(u) return ({ [HOST] = "Tav", [SH] = "Shadowheart", [GOB] = "Goblin" })[u] end,
        index = { ready = false },
    },
}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/Events.lua")
local E = BG3AI.Events

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end
local function last(kind)
    for i = #pushed, 1, -1 do if pushed[i].kind == kind then return pushed[i].data end end
    return nil
end

-- ACT-14: an enemy's cast shows its display name only; the party's own cast keeps the id.
do
    listeners.CastedSpell("Goblin_" .. GOB, "Projectile_GOB_SecretHint", "", "", 0)
    local d = last("cast")
    check("enemy cast pushed", d ~= nil)
    check("enemy cast: shown name", d and d.name == "Scimitar Slash", d and d.name)
    check("enemy cast: no engine id", d and d.spell == nil, d and d.spell)
    listeners.CastedSpell("S_Player_" .. SH, "Target_FireBolt", "", "", 0)
    d = last("cast")
    check("party cast: id kept", d and d.spell == "Target_FireBolt", d and d.spell)
    check("party cast: roll", d and d.roll == "attack", d and d.roll)
end

-- ACT-15: unseeded, the first change after a load used to be dropped.
do
    E.approval = {}
    tick()
    check("seeded from GetApprovalRating", E.approval[SH .. "|" .. HOST] == 10, E.approval[SH .. "|" .. HOST])
    check("host not seeded against itself", E.approval[HOST .. "|" .. HOST] == nil)
    local n = #pushed
    listeners.ApprovalRatingChanged("S_Player_" .. SH, "Tav_" .. HOST, 12)
    local d = last("approval")
    check("first change after load reported", #pushed == n + 1 and d ~= nil)
    check("first change: direction", d and d.direction == "approves", d and d.direction)
    listeners.ApprovalRatingChanged("S_Player_" .. SH, "Tav_" .. HOST, 7)
    d = last("approval")
    check("second change: direction", d and d.direction == "disapproves", d and d.direction)
    n = #pushed
    listeners.ApprovalRatingChanged("S_Player_" .. SH, "Tav_" .. HOST, 7)
    check("same value: no event", #pushed == n)
    -- the poll never overwrites a value the listener already holds
    ratings[SH .. "|" .. HOST] = 99
    E.lastPoll = 0
    tick()
    check("seed keeps the live value", E.approval[SH .. "|" .. HOST] == 7, E.approval[SH .. "|" .. HOST])
end

-- a member with no rating (a hireling) is asked once, not every poll
do
    local HIRE = "aaaaaaaa-0000-0000-0000-000000000003"
    party[#party + 1] = HIRE; isParty[HIRE] = true
    local asked = 0
    local real = Osi.GetApprovalRating
    Osi.GetApprovalRating = function(o, r) if o == HIRE then asked = asked + 1 end return real(o, r) end
    for _ = 1, 3 do E.lastPoll = 0; tick() end
    check("no rating: asked once", asked == 1, asked)
    check("no rating: nothing seeded", E.approval[HIRE .. "|" .. HOST] == nil)
    Osi.GetApprovalRating = real
end

print(string.format("%d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
