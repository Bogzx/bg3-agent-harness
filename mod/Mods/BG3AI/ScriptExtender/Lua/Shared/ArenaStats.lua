-- Mirror twins' stat entries (both contexts, at StatsLoaded, only with BG3AI_ARENA=1): BG3AI_Arena_<Class>_L1,
-- the stats arena.mirror_spawn gives a hireling root template for the CreateAt of a pair. Made from _Hero (the player
-- base: its passives and action resources, no race, no origin) with the class, the level, a 27-point point-buy and,
-- as Progressions, the class's own progression table: the game applies an entry's Progressions table at spawn, fixed
-- parts and AddSpells included (live 2026-11-02, live check 11 load 3: POC_Player_Fighter's table is Origin_Laezel's,
-- and a copy got exactly its Str +2 Int +1, Gith_MartialProdigy and Mage Hand, but no class table: no Second Wind).
-- The choices (skills, fighting style, spells, the +2/+1) are the kit's (Server/ArenaMirror.lua), not the entry's.
-- Made every session, before any save loads, so a reloaded twin finds its entry (none is persisted in a save).
-- Abilities: POC_Player_<Class>'s (Shared Character.txt); the Rogue's Dex 17 there carries the old racial +2 (15
-- here); the Barbarian's POC entry is the Fighter's. The classes (orchestrator D300, D308): no subclass by level 2
-- (MakePlayer gives a copy the party's XP: level 2), every choice a selector list the kit fills. Level 1 only:
-- live check 12 step 5-6, a level-1 copy made a player reads level 2 with the class table's level-2 row (Action
-- Surge, Cunning Action) and no level-up pending; a level-2 entry is not needed for that.

BG3AI = BG3AI or {}
local Ar = BG3AI.Arena

local AS = {}
Ar.Stats = AS
AS.PREFIX = "BG3AI_Arena_"
AS.LEVELS = { 1 }
AS.ABILITIES = { "Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma" }
AS.CLASSES = { -- table: the class's progression table (ClassDescriptions / Progressions.lsx)
    Fighter = { table = "ba4e707e-4bbd-4630-963b-ce2f2947f971", casting = "Intelligence",
        abilities = { 15, 13, 14, 10, 12, 8 } },
    Rogue = { table = "9a44e828-92db-4d6c-acbc-a4f8ef340415", casting = "Intelligence",
        abilities = { 8, 15, 14, 12, 13, 10 } },
    Barbarian = { table = "60bbcc97-5381-4898-bc15-908c072895de", casting = "Intelligence",
        abilities = { 15, 13, 14, 10, 12, 8 } },
    Ranger = { table = "8f068974-ed32-43fc-a9d9-028be1c01a9c", casting = "Wisdom",
        abilities = { 12, 15, 13, 8, 14, 10 } },
}

function AS.name(class, level) return AS.PREFIX .. class .. "_L" .. tostring(level) end

-- Every entry, made (or remade) now: {names}. A failing one stops the rest with its error.
function AS.build()
    local names = {}
    for _, class in ipairs({ "Fighter", "Rogue", "Barbarian", "Ranger" }) do
        local c = AS.CLASSES[class]
        for _, level in ipairs(AS.LEVELS) do
            local name = AS.name(class, level)
            local s = Ext.Stats.Get(name) or Ext.Stats.Create(name, "Character", "_Hero")
            if not s then error("Ext.Stats.Create made no " .. name, 0) end
            s.Class = class
            s.Level = level
            s.SpellCastingAbility = c.casting
            s.Progressions = c.table
            for i, a in ipairs(AS.ABILITIES) do s[a] = c.abilities[i] end
            names[#names + 1] = name
        end
    end
    return names
end

Ext.Events.StatsLoaded:Subscribe(function()
    if not Ar.enabled() then return end
    local ok, err = pcall(AS.build)
    if not ok then Ext.Utils.PrintWarning("[BG3AI] arena stat entries: " .. tostring(err)) end
end)

return AS
