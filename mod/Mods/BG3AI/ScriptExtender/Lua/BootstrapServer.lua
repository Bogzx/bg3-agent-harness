BG3AI = BG3AI or {}
Ext.Require("Shared/RPC.lua")
Ext.Require("Shared/Util.lua")
Ext.Require("Shared/Mem.lua")
Ext.Require("Shared/Arena.lua")
Ext.Require("Shared/ArenaStats.lua") -- harness only: the mirror twins' stat entries (BG3AI_ARENA=1)
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
    "Server/Arena.lua", -- harness only, refused without BG3AI_ARENA=1; inert until an arena begins
    "Server/ArenaSetup.lua", -- harness only: arena.setup (same switch)
    "Server/ArenaField.lua", -- harness only: arena.setup's field clean (same switch)
    "Server/ArenaSymmetry.lua", -- harness only: arena.symmetry (same switch)
    "Server/ArenaMirror.lua", -- harness only: arena.mirror_spawn / arena.mirror_kit (same switch)
}) do
    U.require(path)
end

Ext.Utils.Print("[BG3AI] server context ready")
