BG3AI = BG3AI or {}
BG3AI.VERSION = "0.1.0"
Ext.Require("Shared/RPC.lua")
Ext.Require("Shared/Util.lua")
local U = BG3AI.Util

-- Client modules: Noesis UI reading (what is on screen) and UI commands.
for _, path in ipairs({
    "Client/UI.lua",
    "Client/UIState.lua",
    "Client/UIActions.lua",
    "Client/AutoSkip.lua",
    "Client/UISettings.lua",
    "Client/Services.lua",
    "Client/Pick.lua",      -- worker act: world->screen projection, hover checks
    "Client/Progression.lua", -- worker se: new game, character creation, level up
}) do
    U.require(path)
end

Ext.Utils.Print("[BG3AI] client context ready, v" .. BG3AI.VERSION)
