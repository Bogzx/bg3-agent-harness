-- Offline tests for the trade screen read in Client/Services.lua (SV.tradeState, ui.trade's job):
-- names/prices come out right, each ECS container map is built once per read (it was rebuilt for
-- every slot: O(n^2), job.poll waited 400-750 ms behind it live), view models are read by name
-- (no GetAllProperties per slot/item), and the "finishing" phase reads the full screen only once.
-- Run: tools/vis/test_trade.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local clock = 1000
local itemReads, propsCalls = 0, 0

-- ECS: an entity is a table of components.
local function ent(uuid, name, weight)
    return { Uuid = { EntityUuid = uuid }, DisplayName = { NameKey = { Handle = { Handle = name } } },
             Data = { Weight = weight, StatsId = name } }
end
local function inventory(items)
    local recs = {}
    for slot, e in pairs(items) do
        recs[slot] = setmetatable({}, { __index = function(_, k) if k == "Item" then itemReads = itemReads + 1 return e end end })
    end
    return { InventoryContainer = { Items = recs } }
end

-- VM: plain tables; collections are {list=...}
local function coll(list) return { list = list, Count = #list } end
local function vmItem(w, price, count) return { Stats = { Weight = { Value = w / 1000 } }, TradePrice = price, Count = count or 1, ItemType = "Item" } end
local function vmInv(slots) -- slots: index -> vmItem (nil = empty), size n
    local list = {}
    for i = 0, 39 do list[#list + 1] = { Index = i, Object = slots[i] } end
    return { Slots = coll(list) }
end

local partyItems, traderItems = {}, {}
for i = 0, 29 do partyItems[i] = ent("p-" .. i, "PartyThing" .. i, 100 + i) end
for i = 0, 19 do traderItems[i] = ent("t-" .. i, "Ware" .. i, 500 + i) end
local partyInv, traderInv = inventory(partyItems), inventory(traderItems)
local hero = { InventoryOwner = { PrimaryInventory = partyInv }, PartyMember = {} }
local trader = { InventoryOwner = { PrimaryInventory = traderInv }, ClientCharacter = {} }

local pslots, tslots = {}, {}
for i = 0, 29 do pslots[i] = vmItem(100 + i, 10 + i) end
for i = 0, 19 do tslots[i] = vmItem(500 + i, 50 + i) end
local dc = { Mode = "Barter", TradePartyGold = 300, TraderInventoryGold = 900, PriceModifierPercentage = 100,
    PartyCharacterInventories = coll({ { Inventory = vmInv(pslots) } }),
    TraderInventory = vmInv(tslots), CharacterOffer = vmInv({}), TraderOffer = vmInv({}),
    CharacterOfferGold = 0, TraderOfferGold = 0, TradeButtonsEnabled = true }
local widget = { DataContext = dc, visible = true }

Ext = {
    Utils = { MonotonicTime = function() return clock end },
    Entity = {
        GetAllEntitiesWithComponent = function(c)
            if c == "PartyMember" then return { hero } end
            if c == "InventoryOwner" then return { hero, trader } end
            return {}
        end,
        Get = function(u) return nil end,
    },
    Stats = { Get = function() return nil end },
    IO = { LoadFile = function() return nil end },
    Json = { Parse = function() return {} end },
}
local jobs, ops = {}, {}
BG3AI = {
    RPC = {
        now_ms = function() return clock end,
        register = function(name, fn) ops[name] = fn end,
        push = function() end,
        start_job = function(kind, spec) local j = spec or {}; j.id = #jobs + 1; j.status = "running"; jobs[j.id] = j; return j end,
        finish_job = function(job, status, result, err)
            if job.status ~= "running" then return end
            job.status, job.result, job.error = status, result, err
        end,
    },
    UI = {
        widget = function(n) return n == "Trade" and widget or nil end,
        isVisible = function(w) return w.visible end,
        prop = function(o, k) if type(o) == "table" then return o[k] end end,
        get = function(o, k) if type(o) == "table" then return o[k] end end,
        pick = function(o, names) local out = {}; for _, k in ipairs(names) do out[k] = o[k] end; return out end,
        props = function(o) propsCalls = propsCalls + 1; return type(o) == "table" and o or {} end,
        items = function(c, max)
            local i, n = 0, math.min(c and c.Count or 0, max or 1e9)
            return function() i = i + 1; if i <= n then return i, c.list[i] end end
        end,
        path = function(o, p) for part in p:gmatch("[^%.]+") do if type(o) ~= "table" then return nil end o = o[part] end return o end,
        command = function(d, name, param)
            if name == "FinishBarter" then d.CharacterOffer, d.TraderOffer = vmInv({}), vmInv({}) end
            if name == "MoveTradeItem" then d.TraderOffer = vmInv({ [0] = param }) end
            return true
        end,
        cachedByName = function() return nil end,
        dc = function() return dc end,
        anyDC = function() return dc end,
    },
    UIState = { SCREENS = {} },
    Util = { comp = function(e, c) return type(e) == "table" and e[c] or nil end,
             loca = function(h) return h end, dist = function() return 0 end },
}
dofile(LUA .. "/Client/Services.lua")
local SV = BG3AI.Services

local fails, passes = 0, 0
local function ok(cond, msg) if cond then passes = passes + 1 else fails = fails + 1; print("FAIL " .. msg) end end

-- tradeState: names/prices matched; containers mapped once per read
itemReads, propsCalls = 0, 0
local st = SV.tradeState(dc)
ok(#st.your_items == 30, "30 party items, got " .. #st.your_items)
ok(#st.wares == 20, "20 wares, got " .. #st.wares)
ok(st.wares[3].name == "Ware2" and st.wares[3].price == 52 and st.wares[3].uuid == "t-2", "ware matched by slot+weight")
ok(st.your_items[1].name == "PartyThing0", "party item named")
ok(st.party_gold == 300 and st.trader_gold == 900, "gold")
-- before: every itemView rebuilt its container map (30*30 + 20*20 = 1300 reads + the matching passes)
ok(itemReads <= 2 * (30 + 20), "container maps built once per read: " .. itemReads .. " item reads")
ok(SV.memo == nil, "memo cleared after the read")
ok(propsCalls <= 6, "no GetAllProperties per slot/item: " .. propsCalls .. " calls")

-- an error inside a read clears the memo
local orig = BG3AI.UI.items
BG3AI.UI.items = function() error("boom") end
ok(not pcall(SV.tradeState, dc), "error propagates")
ok(SV.memo == nil, "memo cleared after an error")
BG3AI.UI.items = orig

-- confirm: the finishing phase waits on the offer slot counts, the full read happens once at the end
local r = ops["ui.trade"]({ request = { "t-1" }, confirm = true })
Ext.Entity.Get = function(u) return u == "t-1" and traderItems[1] or nil end
traderItems[1].InventoryMember = { Inventory = traderInv, EquipmentSlot = 1 }
local job = jobs[r.job]
local fullReads = 0
local origState = SV.tradeState
SV.tradeState = function(...) fullReads = fullReads + 1 return origState(...) end
for _ = 1, 100 do
    clock = clock + 50
    if job.status ~= "running" then break end
    job.on_tick(job)
end
ok(job.status == "done", "trade done: " .. tostring(job.status) .. " " .. tostring(job.error))
ok(job.result and job.result.confirmed == true, "confirmed")
ok(fullReads == 1, "one full screen read in the job, got " .. fullReads)

print(string.format("%d/%d passed", passes, passes + fails))
if fails > 0 then os.exit(1) end
