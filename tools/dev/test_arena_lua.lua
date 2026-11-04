-- Offline tests for two teams in one fight (an arena), server context: each side's bridge sends "side" in the request
-- envelope. The leak cases: a side never reads the other side's events, jobs, memories or caches; its party is its
-- team (summons by owner); turn ownership per side with a cross-team turn group serialised; refusals for another
-- side's turn and characters; arena.status/end/reset harness-only behind BG3AI_ARENA; with the switch off a "side"
-- changes nothing. test_arena_lua_client.lua has the client half (reaction prompt, HUD selection).
-- Ext, Osi and the game world are stubbed; Shared/RPC.lua, Util.lua, Arena.lua and Server/Visibility.lua, State.lua,
-- Events.lua, Guard.lua, Navigation.lua, Actions.lua, Referee.lua, RollLog.lua,
-- Arena.lua, ArenaSetup.lua, ArenaSymmetry.lua, ArenaMirror.lua and Shared/ArenaStats.lua are the real ones.
-- Run: tools/dev/test_arena_lua.sh  (Lua 5.3 + 5.4 in Docker)

LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

-- ---- stubs ---------------------------------------------------------------

A1 = "aaaaaaaa-0000-0000-0000-0000000000a1"
A2 = "aaaaaaaa-0000-0000-0000-0000000000a2"
B1 = "bbbbbbbb-0000-0000-0000-0000000000b1"
B2 = "bbbbbbbb-0000-0000-0000-0000000000b2"
SA = "aaaaaaaa-0000-0000-0000-0000000000f1" -- A1's summon (in DB_PartyMembers, as the game keeps summons)
GOB = "cccccccc-0000-0000-0000-000000000009"
NAMES = { [A1] = "Ana", [A2] = "Abe", [B1] = "Bo", [B2] = "Bea", [SA] = "Aspect", [GOB] = "Goblin" }

clock = 1000
env = {}          -- the game process's environment
world = {}        -- uuid -> {pos, comps}
osiv = {}         -- "Fn:a:b" -> value (queries)
listeners = {}    -- Osiris event -> {fn}
tickers = {}
net = {}          -- Ext.Net broadcasts
order = {}        -- TurnOrder.CurrentGroups: a uuid (a group of one) or {uuid..., team = id?} (a turn group) each
partyRows = {}
engineRound = 1  -- the combat's TurnOrder.Round
replicated = {}   -- entity:Replicate calls, "uuid:Component" each
decoded = {}      -- an encoded answer -> the value it encodes (rpc() reads answers back as tables)
statsLoaded = {}  -- Ext.Events.StatsLoaded subscribers

os.getenv = function(n) return env[n] end -- luacheck: ignore 122

function key(name, ...)
    local parts = { name }
    for _, a in ipairs({ ... }) do parts[#parts + 1] = tostring(a) end
    return table.concat(parts, ":")
end

-- A deterministic JSON-ish encoding (sorted keys): two answers compare as strings.
function enc(v)
    local t = type(v)
    if t == "table" then
        if getmetatable(v) and getmetatable(v).__tostring then return string.format("%q", tostring(v)) end
        local n = #v
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = k end
        if n > 0 and #keys == n then
            local out = {}
            for i = 1, n do out[i] = enc(v[i]) end
            return "[" .. table.concat(out, ",") .. "]"
        end
        table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
        local out = {}
        for _, k in ipairs(keys) do out[#out + 1] = string.format("%q", tostring(k)) .. ":" .. enc(v[k]) end
        return "{" .. table.concat(out, ",") .. "}"
    elseif t == "string" then
        return string.format("%q", v)
    end
    return tostring(v)
end

-- bare: a turn-order member's entity (no CombatParticipant of its own)
local function entity(u, bare)
    local w = world[u]
    if not w then error("no entity " .. tostring(u)) end
    local e = { Uuid = { EntityUuid = u }, Transform = { Transform = { Translate = w.pos } },
        Replicate = function(_, comp) replicated[#replicated + 1] = u .. ":" .. comp end }
    for k, v in pairs(w.comps) do e[k] = v end
    if bare or not w.comps.ServerCharacter then return e end
    local groups = {}
    for _, g in ipairs(order) do
        local members = {}
        for _, m in ipairs(type(g) == "table" and g or { g }) do
            members[#members + 1] = { Entity = entity(m, true) }
        end
        groups[#groups + 1] = { Members = members, Team = type(g) == "table" and g.team or nil }
    end
    e.CombatParticipant = { CombatHandle = { TurnOrder = { CurrentGroups = groups, Round = engineRound } } }
    return e
end

local function characters(comp)
    local out = {}
    for u, w in pairs(world) do if w.comps[comp or "ServerCharacter"] then out[#out + 1] = entity(u, true) end end
    table.sort(out, function(a, b) return a.Uuid.EntityUuid < b.Uuid.EntityUuid end)
    return out
end

Ext = {
    IsServer = function() return true end,
    IsClient = function() return false end,
    Utils = { MonotonicTime = function() return clock end, PrintWarning = function() end, Print = function() end,
        Version = function() return 0 end, GetGameState = function() return "Running" end },
    Events = { Tick = { Subscribe = function(_, fn) tickers[#tickers + 1] = fn end },
        StatsLoaded = { Subscribe = function(_, fn) statsLoaded[#statsLoaded + 1] = fn end } },
    Entity = { Get = function(u) return entity(u) end,
        GetAllEntitiesWithComponent = function(c)
            return (c == "ServerCharacter" or c == "ServerItem") and characters(c) or {}
        end },
    Stats = { Get = function(n) return n == "Projectile_FireBolt" and { DisplayName = "Fire Bolt" } or nil end },
    StaticData = { Get = function(uuid) return { Name = uuid } end },
    Loca = { GetTranslatedString = function(h) return h end },
    Json = { Parse = function(s) return load("return " .. s)() end,
        Stringify = function(v) local s = enc(v) decoded[s] = v return s end },
    Osiris = { RegisterListener = function(name, _, _, fn)
        listeners[name] = listeners[name] or {}
        table.insert(listeners[name], fn)
    end },
    Net = { CreateChannel = function() return { Broadcast = function(_, msg) net[#net + 1] = msg end } end },
    Level = { GetHeightsAt = function() return { 0.0 } end },
    IO = { LoadFile = function() return nil end, SaveFile = function() return true end },
    Require = function(p)
        if p:find("JournalMarkers") then return {} end
        return dofile(LUA .. "/" .. p)
    end,
}

Osi = setmetatable({
    DB_PartyMembers = { Get = function()
        local rows = {}
        for _, u in ipairs(partyRows) do rows[#rows + 1] = { "S_Player_" .. u } end
        return rows
    end },
    GetDisplayName = function(u) return NAMES[u] end,
    GetPosition = function(u)
        local w = world[u]
        if w then return w.pos[1], w.pos[2], w.pos[3] end
        error("no position")
    end,
}, { __index = function(_, name)
    return function(...) return osiv[key(name, ...)] end
end })

BG3AI = {}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Shared/RPC.lua")
dofile(LUA .. "/Shared/Arena.lua")
dofile(LUA .. "/Shared/ArenaStats.lua")
dofile(LUA .. "/Server/Visibility.lua")
dofile(LUA .. "/Server/State.lua")
dofile(LUA .. "/Server/Events.lua")
dofile(LUA .. "/Server/Guard.lua")
dofile(LUA .. "/Server/Navigation.lua")
dofile(LUA .. "/Server/Actions.lua")
dofile(LUA .. "/Server/Referee.lua")
dofile(LUA .. "/Server/RollLog.lua")
dofile(LUA .. "/Server/Arena.lua")
dofile(LUA .. "/Server/ArenaSetup.lua")
dofile(LUA .. "/Server/ArenaSymmetry.lua")
dofile(LUA .. "/Server/ArenaMirror.lua")
U, RPC, Ar, V, G, N = BG3AI.Util, BG3AI.RPC, BG3AI.Arena, BG3AI.Visibility, BG3AI.Guard, BG3AI.Nav

-- One Osiris event, as the game fires it: every listener registered for it.
function fire(name, ...)
    for _, fn in ipairs(listeners[name] or {}) do fn(...) end
end

-- One bridge call: the request envelope as the bridge sends it, the decoded answer.
function rpc(op, args, side)
    local req = "{op=" .. string.format("%q", op) .. ",args=" .. (args or "{}")
        .. (side and (",side=" .. string.format("%q", side)) or "") .. "}"
    return decoded[BG3AI.rpc(req)]
end

-- The raw encoded answer (switch-off byte identity).
function raw(op, args, side)
    return BG3AI.rpc("{op=" .. string.format("%q", op) .. ",args=" .. (args or "{}")
        .. (side and (",side=" .. string.format("%q", side)) or "") .. "}")
end

function setTurn(u, active, requested)
    world[u].comps.TurnBased = { IsActiveCombatTurn = active == true, RequestedEndTurn = requested == true }
end

function reset()
    clock = clock + 100000
    env = { BG3AI_ARENA = "1" }
    osiv, net, order, replicated = {}, {}, {}, {}
    engineRound = 1
    partyRows = { A1, A2, B1, B2, SA }
    world = {
        [A1] = { pos = { 0, 0, 0 }, comps = { ServerCharacter = {}, Health = { Hp = 10, MaxHp = 20 } } },
        [A2] = { pos = { 1, 0, 0 }, comps = { ServerCharacter = {}, Health = { Hp = 20, MaxHp = 20 } } },
        [B1] = { pos = { 10, 0, 0 }, comps = { ServerCharacter = {}, Health = { Hp = 30, MaxHp = 30 } } },
        [B2] = { pos = { 11, 0, 0 }, comps = { ServerCharacter = {}, Health = { Hp = 15, MaxHp = 30 } } },
        [SA] = { pos = { 0, 0, 1 }, comps = { ServerCharacter = {}, Health = { Hp = 5, MaxHp = 5 },
            IsSummon = { Owner = { Uuid = { EntityUuid = A1 } } } } },
        [GOB] = { pos = { 5, 0, 0 }, comps = { ServerCharacter = {} } },
    }
    for u in pairs(world) do
        osiv[key("IsOnStage", u)] = 1
        osiv[key("IsCharacter", u)] = 1
        osiv[key("CombatGetGuidFor", u)] = "combat1"
        osiv[key("IsInCombat", u)] = 1
        setTurn(u, false, false)
    end
    osiv[key("GetHostCharacter")] = A1
    osiv[key("GetRegion", A1)] = "ARENA_Level"
    G.turn_start, G.foe_start, G.turn_over, G.end_requested, G.end_alone, G.end_confirmed = {}, {}, {}, {}, {}, {}
    G.turn_open, G.end_owed, G.combat_round = {}, {}, {}
    V.sideCtx, V.ctxCache, V.ctxFrame, V.memory = nil, nil, nil, {}
    N.frame = (N.frame or 0) + 1
    Ar.state = nil
    BG3AI.Referee.ring = {}
    RPC.clear_sides()
    RPC.Ctx.side = nil
end

function begin(opts)
    return Ar.begin({ A = { A1, A2 }, B = { B1, B2 } }, opts)
end

-- The referee's records of this kind (Server/Referee.lua R.ring).
function refs(ev)
    local out = {}
    for _, r in ipairs(BG3AI.Referee.ring) do if r.ev == ev then out[#out + 1] = r end end
    return out
end

-- ---- tiny test runner ------------------------------------------------------

local failures, count = 0, 0
function test(name, fn)
    count = count + 1
    reset()
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
function eq(a, b, what)
    if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end
end
function refused(fn, pat)
    local ok, err = pcall(fn)
    if ok then error("expected a refusal matching '" .. pat .. "'", 2) end
    local msg = G.is_refusal(err) and err.reason or tostring(err)
    if not msg:find(pat, 1, true) then error("refusal '" .. msg .. "' does not match '" .. pat .. "'", 2) end
end
function as(side, fn, ...) return RPC.with_side(side, fn, ...) end
-- One server tick past the arena's phase throttle.
function tick()
    clock = clock + Ar.PHASE_EVERY_MS
    for _, f in ipairs(tickers) do f() end
end
function kinds(res, kind)
    local out = {}
    for _, ev in ipairs(res.result.events) do if ev.kind == kind then out[#out + 1] = ev end end
    return out
end

local DIR = arg[0]:match("^(.*)/[^/]*$") or "."
for _, part in ipairs({ "test_arena_lua_sides.lua", "test_arena_lua_turns.lua", "test_arena_lua_fights.lua",
    "test_arena_lua_listeners.lua", "test_arena_lua_phases.lua", "test_arena_lua_setup.lua",
    "test_arena_lua_symmetry.lua", "test_arena_lua_mirror.lua", "test_arena_lua_order.lua" }) do
    dofile(DIR .. "/" .. part)
end

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
