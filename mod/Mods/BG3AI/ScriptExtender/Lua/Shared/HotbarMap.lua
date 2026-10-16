-- The hotbar's slot -> spell id map from the engine's own hotbar component (owner: worker se, audit ACT-8).
-- Live window 2026-10-16 18:32 (acceptance se "hotbar widget tree"): the HUD's slot VMs (ls.VMCharacterAction)
-- expose no PrototypeID on bg3le (like ls.VMItem has no Name), so Client/Hotbar.lua could not tell which cell holds
-- which spell. The character's eoc::hotbar::ContainerComponent (bg3se Components/Hotbar.h) holds the same layout:
--   HotbarContainer.Containers[<key>] = { Bar{Index (0=Common, 1=Class, 2=Item), Controller, Elements, Name} }
--   Element = { Item, SpellId{Prototype, OriginatorPrototype}, Passive, Slot }
-- and Element.Slot is the slot VM's SlotIndex. Loaded in both contexts: the client reads its own entity first; when
-- the client entity has no component, the bridge asks the server (op hotbar.map) and passes the map along.
--
-- map shape (both contexts, JSON-safe): { [deck HotBarType] = { [tostring(Slot)] = spell id } }

BG3AI = BG3AI or {}
local M = {}
BG3AI.HotbarMap = M

M.BAR_DECKS = { [0] = "CommonHotBar", [1] = "ClassHotBar", [2] = "ItemHotBar" }
M.DECK_NAMES = { CommonHotBar = true, ClassHotBar = true, ItemHotBar = true, PassivesHotBar = true,
                 FixedSideBar = true, CustomHotBar = true }
M.CONTAINER_KEYS = { "DefaultBarContainer" }

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local function len(a)
    return try(function() return #a end) or 0
end

local function fs(v)
    if v == nil then return nil end
    local s = tostring(v)
    if s == "" or s == "nil" then return nil end
    return s
end

-- The bar list to use: the active container, else DefaultBarContainer, else the first one.
local function bars(hc)
    local cont = try(function() return hc.Containers end)
    if cont == nil then return nil end
    local key = fs(try(function() return hc.ActiveContainer end))
    local b = key and try(function() return cont[key] end)
    if b ~= nil then return b, key end
    for _, k in ipairs(M.CONTAINER_KEYS) do
        b = try(function() return cont[k] end)
        if b ~= nil then return b, k end
    end
    for k, v in pairs(cont) do return v, tostring(k) end
    return nil
end

-- hc = a HotbarContainer component (or a table of the same shape). Returns map, info{container, bars, slots}.
function M.fromComponent(hc)
    local list, key = bars(hc)
    local map, info = {}, { container = key, bars = 0, slots = 0 }
    if list == nil then return map, info end
    local ctrl = {}
    for i = 1, len(list) do
        local bar = try(function() return list[i] end)
        local name = fs(try(function() return bar.Name end))
        local deck = (name and M.DECK_NAMES[name] and name) or M.BAR_DECKS[tonumber(try(function() return bar.Index end)) or -1]
        local c = tonumber(try(function() return bar.Controller end)) or 0
        -- a controller layout of the same deck: the keyboard one (Controller 0) wins
        if deck and (map[deck] == nil or (ctrl[deck] ~= 0 and c == 0)) then
            local m = {}
            local els = try(function() return bar.Elements end)
            for j = 1, len(els) do
                local el = try(function() return els[j] end)
                local slot = tonumber(try(function() return el.Slot end))
                local id = fs(try(function() return el.SpellId.Prototype end))
                if slot and id then
                    m[tostring(slot)] = id
                    info.slots = info.slots + 1
                end
            end
            map[deck], ctrl[deck] = m, c
            info.bars = info.bars + 1
        end
    end
    return map, info
end

-- The map for a character uuid in this context, or nil + why.
function M.forChar(uuid)
    if not uuid or uuid == "" then return nil, "no character" end
    local e = try(Ext.Entity.Get, uuid)
    if not e then return nil, "no entity" end
    local hc = try(function() return e.HotbarContainer end)
    if hc == nil then return nil, "no HotbarContainer" end
    local map, info = M.fromComponent(hc)
    if info.bars == 0 then return nil, "no bars" end
    return map, info
end

local RPC = BG3AI.RPC
if RPC and RPC.register and try(Ext.IsServer) then
    -- server: hotbar.map {char} -> {map, container, bars, slots} | {why}
    RPC.register("hotbar.map", function(args)
        local map, info = M.forChar((args or {}).char)
        if not map then return { why = info } end
        return { map = map, container = info.container, bars = info.bars, slots = info.slots }
    end)
end

return M
