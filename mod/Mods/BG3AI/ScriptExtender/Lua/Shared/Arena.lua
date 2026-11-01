-- Two teams in one fight, each side driven by its own bridge (both contexts). Inert unless the game process runs
-- with BG3AI_ARENA=1 (read per call, like Server/Scenario.lua's switch) and the server has begun an arena: until then
-- Ar.active() is false, RPC.side() is nil and every side-dependent branch in the mod is today's path.
-- The server holds the teams (Server/Arena.lua); the client gets the team uuids and display names over Ext.Net (one
-- process, two Lua states), for its own side-aware reads (the reaction prompt, the HUD's selection).

BG3AI = BG3AI or {}
local Ar = BG3AI.Arena or {}
BG3AI.Arena = Ar

Ar.SWITCH = "BG3AI_ARENA"
Ar.NET = { "65734c81-edd1-4b6f-b10e-663fec0e768e", "ArenaTeams" } -- module = meta.lsx UUID

-- The game process's environment: bg3le keeps os.getenv for its own code as Ext._Internal.Getenv.
function Ar.getenv(name)
    local get = (os and os.getenv) or (Ext._Internal and Ext._Internal.Getenv)
    if not get then return nil end
    local ok, v = pcall(get, name)
    return ok and v or nil
end

function Ar.enabled() return Ar.getenv(Ar.SWITCH) == "1" end

-- Ar.state: nil, or {teams = {A = {uuid}, B = {uuid}}, names = {A = {name}, B = {name}}, ...} (the server adds the
-- turn and the end). The state is checked first, so with no arena nothing reads the environment.
function Ar.active() return Ar.state ~= nil and Ar.enabled() end

-- The side a team member is on ("A" / "B"), nil for anyone else. The server's Ar.side_of also maps summons.
function Ar.member_side(uuid)
    local st = Ar.state
    if not (st and uuid) then return nil end
    for _, s in ipairs({ "A", "B" }) do
        for _, u in ipairs(st.teams[s] or {}) do
            if u == uuid then return s end
        end
    end
    return nil
end

-- The side whose team has a member of this display name, nil for none.
function Ar.name_side(name)
    local st = Ar.state
    if not (st and st.names and name) then return nil end
    for _, s in ipairs({ "A", "B" }) do
        for _, n in ipairs(st.names[s] or {}) do
            if n == name then return s end
        end
    end
    return nil
end

-- A team member's display name (the names come with the teams), nil for anyone else.
function Ar.name_of(uuid)
    local s = Ar.member_side(uuid)
    if not s then return nil end
    for i, u in ipairs(Ar.state.teams[s]) do
        if u == uuid then return Ar.state.names and Ar.state.names[s] and Ar.state.names[s][i] end
    end
    return nil
end

if Ext.IsClient and Ext.IsClient() and not Ar.netListening then
    Ar.netListening = true
    local ok, err = pcall(function()
        Ext.Net.CreateChannel(Ar.NET[1], Ar.NET[2], function(msg)
            -- {teams, names} from the server's Ar.begin; {} on its reset
            if type(msg) == "table" and type(msg.teams) == "table" then
                Ar.state = { teams = msg.teams, names = msg.names or {} }
            else
                Ar.state = nil
            end
        end)
    end)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] arena channel: " .. tostring(err)) end
end

return Ar
