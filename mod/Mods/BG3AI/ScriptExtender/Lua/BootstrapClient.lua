BG3AI = BG3AI or {}
Ext.Require("Shared/RPC.lua")
Ext.Require("Shared/Util.lua")
Ext.Require("Shared/Mem.lua")
Ext.Require("Shared/Arena.lua")
Ext.Require("Shared/ArenaStats.lua") -- harness only: the mirror twins' stat entries (BG3AI_ARENA=1)
local U = BG3AI.Util

-- Client modules: Noesis UI reading (what is on screen) and UI commands.
for _, path in ipairs({
    "Shared/HotbarMap.lua", -- worker se: hotbar slot -> spell id from the engine component (ACT-8)
    "Client/UI.lua",
    "Client/UIState.lua",
    "Client/UIStateArena.lua", -- a reaction prompt is its side's (split for the line cap)
    "Client/UIStateDialogue.lua", -- ui.answer_list, the answer rows' tag runs (state.dialogue_tags' merge)
    "Client/UIActions.lua",
    "Client/AutoSkip.lua",
    "Client/UISettings.lua",
    "Client/Services.lua",
    "Client/PickpocketOps.lua", -- worker pi: Steal button probe (live2 §1)
    "Client/FleeOps.lua",   -- worker pi: the HUD's Flee (live2 §2)
    "Client/Pick.lua",      -- worker act: world->screen projection, hover checks
    "Client/Progression.lua", -- worker se: new game, character creation, level up
    "Client/Hotbar.lua",    -- worker se: hotbar cell + targeting reads for the N-target cast route (ACT-8)
    "Client/Spellbook.lua", -- worker act: prepare/unprepare spells through the spellbook page (A5)
    "Client/ArenaCamera.lua", -- harness only: arena.camera keeps the view on an arena's fight
}) do
    U.require(path)
end

Ext.Utils.Print("[BG3AI] client context ready")
