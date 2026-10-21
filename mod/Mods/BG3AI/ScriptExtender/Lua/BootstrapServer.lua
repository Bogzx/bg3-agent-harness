BG3AI = BG3AI or {}
BG3AI.VERSION = "0.1.0"
Ext.Require("Shared/RPC.lua")
Ext.Require("Shared/Util.lua")
Ext.Require("Shared/Mem.lua")
local U = BG3AI.Util

-- Server modules: fair state (visibility-filtered), events, guarded actions.
for _, path in ipairs({
    "Shared/HotbarMap.lua", -- worker se: op hotbar.map, the hotbar slot -> spell id fallback (ACT-8)
    "Server/Visibility.lua",
    "Server/State.lua",
    "Server/Events.lua",
    "Server/Navigation.lua",
    "Server/Guard.lua",
    "Server/Actions.lua",
    "Server/Referee.lua", -- harness only, never an agent tool (B-mod)
    "Server/RollLog.lua", -- harness only, never an agent tool (B-mod)
    "Server/Scenario.lua", -- harness only, refused without BG3AI_SCENARIO_SETUP=1 (B-mod)
}) do
    U.require(path)
end

Ext.Utils.Print("[BG3AI] server context ready, v" .. BG3AI.VERSION)
