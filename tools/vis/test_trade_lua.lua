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

-- BalanceTrade as the game does it: the side that gets less puts the difference on the table as a Gold stack
-- (price = count) in its first free offer slot, if it has that much gold
local function offerSum(o)
    local t = 0
    for _, s in ipairs(o.Slots.list) do if s.Object then t = t + s.Object.TradePrice end end
    return t
end
local function addGold(o, n)
    for _, s in ipairs(o.Slots.list) do
        if not s.Object then s.Object = { TradePrice = n, Count = n, ItemType = "Gold" }; return end
    end
end
local function balance(d)
    local diff = offerSum(d.TraderOffer) - offerSum(d.CharacterOffer)
    if diff > 0 and d.TradePartyGold >= diff then addGold(d.CharacterOffer, diff) end
    if diff < 0 and d.TraderInventoryGold >= -diff then addGold(d.TraderOffer, -diff) end
end

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
    Require = function(p) return dofile(LUA .. "/" .. p) end, -- Services.lua loads its parts
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
        pcall = pcall,
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
            if name == "BalanceTrade" then balance(d) end
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
-- REVIEW-17 F3: the deal is priced in the mod. The moved record carries the price and count the screen showed when it
-- moved, and the result keeps the table FinishBarter took (the bridge read the whole screen before every confirm).
local mv = job.result and job.result.moved and job.result.moved[1] or {}
ok(mv.uuid == "t-1" and mv.side == "trader" and mv.price == 51 and mv.count == 1,
    "moved priced: " .. tostring(mv.price) .. " x" .. tostring(mv.count))
local deal = job.result and job.result.deal or {}
local got, gave = (deal.trader or {}).items or {}, (deal.you or {}).items or {}
ok(#got == 1 and got[1].uuid == "t-1" and got[1].name == "Ware1" and got[1].price == 51,
    "deal: the ware at its table price: " .. tostring(got[1] and got[1].price))
ok(#gave == 1 and gave[1].gold == true and gave[1].count == 51, "deal: our side was the Balance's 51 gold")

-- S028 15:48 hand-over (Wulbren: no wares, no gold): a one-sided offer is refused unless donate=true, which presses
-- Barter and stops for the bridge to answer "Donate these items?"
local function runOffer(args)
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 0, vmInv({}), vmInv({})
    local pressed = {}
    local origCmd = BG3AI.UI.command
    BG3AI.UI.command = function(d, name, param)
        pressed[#pressed + 1] = name
        if name == "MoveTradeItem" then d.CharacterOffer = vmInv({ [0] = param }) end
        return true
    end
    Ext.Entity.Get = function(u) return u == "p-1" and partyItems[1] or nil end
    partyItems[1].InventoryMember = { Inventory = partyInv, EquipmentSlot = 1 }
    local j = jobs[ops["ui.trade"](args).job]
    for _ = 1, 100 do
        clock = clock + 50
        if j.status ~= "running" then break end
        j.on_tick(j)
    end
    BG3AI.UI.command = origCmd
    return j, table.concat(pressed, ",")
end
local j1, p1 = runOffer({ offer = { "p-1" }, confirm = true })
ok(j1.status == "failed" and tostring(j1.error):find("donate=true", 1, true), "one-sided refused: " .. tostring(j1.error))
ok(not p1:find("FinishBarter", 1, true), "no Barter press when refused: " .. p1)
local j2, p2 = runOffer({ offer = { "p-1" }, confirm = true, donate = true })
ok(j2.status == "done" and j2.result and j2.result.donate_box == true, "donate stops at the box: " .. tostring(j2.status) .. " " .. tostring(j2.error))
ok(p2:find("FinishBarter", 1, true) ~= nil, "donate pressed Barter: " .. p2)

-- S028 15:51: a stack (Supply Pack x3) opens the quantity box; the job sets the whole stack and presses Confirm once
do
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
    local split = { CurrentPlayer = { UIData = { SplitItemData = { ItemCount = 3, TargetCount = 1 } } } }
    local sw = { DataContext = split, visible = false }
    local applies, origW, origCmd = 0, BG3AI.UI.widget, BG3AI.UI.command
    BG3AI.UI.widget = function(n) if n == "SplitItem" then return sw end return origW(n) end
    BG3AI.UI.command = function(d, name, param)
        if name == "MoveTradeItem" then sw.visible = true; split.CurrentPlayer.UIData.SplitItemData.TargetCount = 1; pendingItem = param end
        if name == "ApplySplitCommand" then
            applies = applies + 1
            -- closes a tick later, with the chosen count on the table
            pendingClose = split.CurrentPlayer.UIData.SplitItemData.TargetCount
        end
        if name == "FinishBarter" then d.CharacterOffer, d.TraderOffer = vmInv({}), vmInv({}) end
        return true
    end
    Ext.Entity.Get = function(u) return u == "t-1" and traderItems[1] or nil end
    local j = jobs[ops["ui.trade"]({ request = { "t-1" } }).job]
    local closedWith
    for _ = 1, 100 do
        clock = clock + 50
        if pendingClose and applies > 0 and not closedWith then
            closedWith = pendingClose; sw.visible = false; dc.TraderOffer = vmInv({ [0] = pendingItem })
        end
        if j.status ~= "running" then break end
        j.on_tick(j)
    end
    BG3AI.UI.widget, BG3AI.UI.command = origW, origCmd
    ok(j.status == "done", "split trade done: " .. tostring(j.status) .. " " .. tostring(j.error))
    ok(applies == 1, "Confirm pressed once: " .. applies)
    ok(closedWith == 3, "whole stack chosen: " .. tostring(closedWith))
    ok(j.result and j.result.moved and j.result.moved[1] and j.result.moved[1].count == 3, "moved count 3")
end

-- REVIEW-17 F3: part of a stack (count=2 of a Supply Pack x3 at 156 g): the moved record is the part, at its share
do
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
    local pack = vmItem(501, 156, 3)
    local wareSlot = dc.TraderInventory.Slots.list[2]  -- slot 1: t-1 by its weight
    local was = wareSlot.Object
    wareSlot.Object = pack
    local split = { CurrentPlayer = { UIData = { SplitItemData = { ItemCount = 3, TargetCount = 1 } } } }
    local sw = { DataContext = split, visible = false }
    local origW, origCmd, closing = BG3AI.UI.widget, BG3AI.UI.command, nil
    BG3AI.UI.widget = function(n) if n == "SplitItem" then return sw end return origW(n) end
    BG3AI.UI.command = function(d, name, param)
        if name == "MoveTradeItem" then sw.visible = true end
        if name == "ApplySplitCommand" then closing = true end
        return true
    end
    Ext.Entity.Get = function(u) return u == "t-1" and traderItems[1] or nil end
    local j = jobs[ops["ui.trade"]({ request = { "t-1" }, count = 2 }).job]
    for _ = 1, 100 do
        clock = clock + 50
        if closing then sw.visible, closing = false, nil end
        if j.status ~= "running" then break end
        j.on_tick(j)
    end
    BG3AI.UI.widget, BG3AI.UI.command = origW, origCmd
    wareSlot.Object = was
    local m = j.result and j.result.moved and j.result.moved[1] or {}
    ok(j.status == "done" and m.count == 2 and m.price == 104, "part of a stack: 2 of 3 at 104 g: "
        .. tostring(m.count) .. " " .. tostring(m.price) .. " " .. tostring(j.error))
end

-- w15 a2 (Muddy Red Dye x8 at 48 g, request count=2, confirm): the game puts the 2 on the table as a new VMItem (the
-- part), not the moved one, so the deal's table read named nothing: 'BOUGHT (no id) ? x2 12 g | e127 Muddy Red Dye x2
-- 12 g' in the bridge. The part is the moved item; an unknown item that is no part we moved stays unnamed.
do
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
    local stack = vmItem(501, 48, 8)
    local wareSlot = dc.TraderInventory.Slots.list[2]  -- slot 1: t-1 by its weight
    local was = wareSlot.Object
    wareSlot.Object = stack
    local stranger = vmItem(900, 30, 1)  -- on the table already, not moved by this trade (put there by hand)
    dc.TraderOffer.Slots.list[1].Object = stranger
    local split = { CurrentPlayer = { UIData = { SplitItemData = { ItemCount = 8, TargetCount = 1 } } } }
    local sw = { DataContext = split, visible = false }
    local origW, origCmd, closing = BG3AI.UI.widget, BG3AI.UI.command, nil
    BG3AI.UI.widget = function(n) if n == "SplitItem" then return sw end return origW(n) end
    BG3AI.UI.command = function(d, name, param)
        if name == "MoveTradeItem" then sw.visible = true end
        if name == "ApplySplitCommand" then closing = split.CurrentPlayer.UIData.SplitItemData.TargetCount end
        if name == "BalanceTrade" then balance(d) end
        if name == "FinishBarter" then d.CharacterOffer, d.TraderOffer = vmInv({}), vmInv({}) end
        return true
    end
    Ext.Entity.Get = function(u) return u == "t-1" and traderItems[1] or nil end
    local j = jobs[ops["ui.trade"]({ request = { "t-1" }, count = 2, confirm = true }).job]
    for _ = 1, 100 do
        clock = clock + 50
        if closing then  -- the box closes; the part (a new VMItem) lands on the table, the rest stays a ware
            sw.visible = false
            stack.Count, stack.TradePrice = 8 - closing, 6 * (8 - closing)
            dc.TraderOffer.Slots.list[2].Object = vmItem(501, 6 * closing, closing)
            closing = nil
        end
        if j.status ~= "running" then break end
        j.on_tick(j)
    end
    BG3AI.UI.widget, BG3AI.UI.command = origW, origCmd
    wareSlot.Object = was
    local res = j.result or {}
    local m = res.moved and res.moved[1] or {}
    ok(j.status == "done" and res.confirmed and m.uuid == "t-1" and m.count == 2 and m.price == 12,
        "a2: moved t-1 x2 12 g: " .. tostring(m.count) .. " " .. tostring(m.price) .. " " .. tostring(j.error))
    local got = ((res.deal or {}).trader or {}).items or {}
    local part, other
    for _, it in ipairs(got) do if it.count == 2 then part = it elseif it.price == 30 then other = it end end
    ok(part and part.uuid == "t-1" and part.name == "Ware1" and part.price == 12,
        "a2: the part on the table is the moved item: " .. tostring(part and part.uuid) .. " " .. tostring(part and part.name))
    ok(other and other.uuid == nil and other.name == nil, "a2: an item no move put there stays unnamed")
    ok(#got == 2 and #(SV.splits or {}) == 0, "a2: two table items, no part left pending: " .. #got)
end

-- REVIEW-18k: a part moved at trader A in a trade never confirmed (its part not read before the screen went) stayed in
-- SV.splits and named a same-count, similar-price part at trader B ('BOUGHT e127 Muddy Red Dye' for another item).
-- A part is named only in its own trade session, with its stack's props and price; another session's parts are
-- dropped, and so are all when the screen is found closed.
do
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
    local stack = vmItem(501, 48, 8)
    local wareSlot = dc.TraderInventory.Slots.list[2]
    local was = wareSlot.Object
    wareSlot.Object = stack
    local split = { CurrentPlayer = { UIData = { SplitItemData = { ItemCount = 8, TargetCount = 1 } } } }
    local sw = { DataContext = split, visible = false }
    local origW, origCmd, closing = BG3AI.UI.widget, BG3AI.UI.command, nil
    BG3AI.UI.widget = function(n) if n == "SplitItem" then return sw end return origW(n) end
    BG3AI.UI.command = function(d, name)
        if name == "MoveTradeItem" then sw.visible = true end
        if name == "ApplySplitCommand" then closing = split.CurrentPlayer.UIData.SplitItemData.TargetCount end
        return true
    end
    Ext.Entity.Get = function(u) return u == "t-1" and traderItems[1] or nil end
    local j = jobs[ops["ui.trade"]({ npc = "trader-A", request = { "t-1" }, count = 2 }).job] -- no confirm
    for _ = 1, 100 do
        clock = clock + 50
        if closing then sw.visible, closing = false, nil end -- the part never shows before the agent leaves
        if j.status ~= "running" then break end
        j.on_tick(j)
    end
    BG3AI.UI.widget, BG3AI.UI.command = origW, origCmd
    wareSlot.Object = was
    ok(j.status == "done" and #SV.splits == 1, "18k: A's part pending after the unconfirmed move: " .. #SV.splits)
    local function partOn(d, item)
        d.TraderOffer = vmInv({ [0] = item })
        return SV.tradeState(d).offer.trader.items[1] or {}
    end
    -- trader B: another screen (DataContext) and trader; a same-count part at the same price
    local dcB = { Mode = "Barter", TradePartyGold = 300, TraderInventoryGold = 900, PriceModifierPercentage = 100,
        PartyCharacterInventories = dc.PartyCharacterInventories, TraderInventory = vmInv({}),
        CharacterOffer = vmInv({}), TraderOffer = vmInv({}), TradeButtonsEnabled = true }
    local origDC = widget.DataContext
    widget.DataContext = dcB
    SV.trader = "trader-B"
    local b = partOn(dcB, vmItem(900, 12, 2))
    ok(b.uuid == nil and b.name == nil, "18k: B's part is never A's: " .. tostring(b.uuid) .. " " .. tostring(b.name))
    -- back at A: only a part with the stack's props and its price is A's
    widget.DataContext, SV.trader = origDC, "trader-A"
    local stolen = vmItem(900, 12, 2)
    stolen.IsStolen = true
    ok(partOn(dc, stolen).uuid == nil, "18k: a stolen part isn't the part of a stack that wasn't")
    ok(partOn(dc, vmItem(900, 14, 2)).uuid == nil, "18k: a part at 14 g isn't the 12 g part")
    ok(partOn(dc, vmItem(900, 12, 2)).uuid == "t-1", "18k: A's part is still named after its move")
    SV.splits[1] = { uuid = "t-1", name = "Ware1", side = "trader", count = 2, price = 12, props = {} }
    widget.DataContext, SV.trader = dcB, "trader-B"
    ops["ui.trade"]({ npc = "trader-B" })
    ok(#SV.splits == 0, "18k: a trade call at B drops A's parts: " .. #SV.splits)
    -- the part pending, the screen found closed drops it
    widget.DataContext, SV.trader = origDC, "trader-A"
    SV.splits[1] = { uuid = "t-1", name = "Ware1", side = "trader", count = 2, price = 12, props = {} }
    widget.visible = false
    ok(not pcall(ops["ui.trade"], {}) and #SV.splits == 0, "18k: no trade screen open drops the parts")
    widget.visible = true
    dc.TraderOffer = vmInv({})
end

-- REVIEW-18n NOTE 1: the part's price is the stack's pro rata here (2 of 3 at 10 g: 7) and the game's may round per
-- unit (6 or 8): within the trade session, with the stack's props, +-1 g is the same part, named once.
for _, partPrice in ipairs({ 6, 8 }) do
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
    local stack = vmItem(501, 10, 3)
    local wareSlot = dc.TraderInventory.Slots.list[2]
    local was = wareSlot.Object
    wareSlot.Object = stack
    local split = { CurrentPlayer = { UIData = { SplitItemData = { ItemCount = 3, TargetCount = 1 } } } }
    local sw = { DataContext = split, visible = false }
    local origW, origCmd, closing = BG3AI.UI.widget, BG3AI.UI.command, nil
    BG3AI.UI.widget = function(n) if n == "SplitItem" then return sw end return origW(n) end
    BG3AI.UI.command = function(d, name)
        if name == "MoveTradeItem" then sw.visible = true end
        if name == "ApplySplitCommand" then closing = split.CurrentPlayer.UIData.SplitItemData.TargetCount end
        if name == "BalanceTrade" then balance(d) end
        if name == "FinishBarter" then d.CharacterOffer, d.TraderOffer = vmInv({}), vmInv({}) end
        return true
    end
    Ext.Entity.Get = function(u) return u == "t-1" and traderItems[1] or nil end
    local j = jobs[ops["ui.trade"]({ request = { "t-1" }, count = 2, confirm = true }).job]
    for _ = 1, 100 do
        clock = clock + 50
        if closing then
            sw.visible = false
            stack.Count, stack.TradePrice = 3 - closing, 10 - partPrice
            dc.TraderOffer.Slots.list[1].Object = vmItem(501, partPrice, closing)
            closing = nil
        end
        if j.status ~= "running" then break end
        j.on_tick(j)
    end
    BG3AI.UI.widget, BG3AI.UI.command = origW, origCmd
    wareSlot.Object = was
    local res = j.result or {}
    local got = ((res.deal or {}).trader or {}).items or {}
    local named = 0
    for _, it in ipairs(got) do if it.uuid == "t-1" then named = named + 1 end end
    ok(j.status == "done" and res.confirmed and named == 1 and got[1].count == 2 and got[1].price == partPrice,
        "18n: 2 of 3 at 10 g, the part at " .. partPrice .. " g is t-1, once: " .. named .. " " .. tostring(j.error))
end

-- launch-v1 haiku act3_gold_max r2 seq 16-21 (67 times in 19 runs): trade(offer=a,b) put both on the table, then
-- trade(offer=a,b,c, confirm=true) found a and b "not on this side of the trade screen" and refused the confirm.
-- Items already on our side of the table count as moved (no second MoveTradeItem, which would take them back off);
-- an id on no side still fails the confirm.
do
    dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
    local party = dc.PartyCharacterInventories.list[1].Inventory.Slots.list
    local moves, origCmd = {}, BG3AI.UI.command
    BG3AI.UI.command = function(d, name, param)
        if name == "MoveTradeItem" then
            moves[#moves + 1] = param
            for _, sl in ipairs(party) do if sl.Object == param then sl.Object = nil end end -- off the inventory
            for _, sl in ipairs(d.CharacterOffer.Slots.list) do
                if not sl.Object then sl.Object = param; break end
            end
        end
        if name == "BalanceTrade" then balance(d) end
        if name == "FinishBarter" then d.CharacterOffer, d.TraderOffer = vmInv({}), vmInv({}) end
        return true
    end
    Ext.Entity.Get = function(u) local i = tonumber(u:match("^p%-(%d+)$")); return i and partyItems[i] or nil end
    for i = 2, 4 do partyItems[i].InventoryMember = { Inventory = partyInv, EquipmentSlot = i } end
    local function run(args)
        local j = jobs[ops["ui.trade"](args).job]
        for _ = 1, 100 do
            clock = clock + 50
            if j.status ~= "running" then break end
            j.on_tick(j)
        end
        return j
    end
    local j1 = run({ offer = { "p-2", "p-3" } })
    ok(j1.status == "done" and #moves == 2, "offer put both on the table: " .. tostring(j1.error) .. " moves " .. #moves)
    local dry = ops["ui.trade"]({ offer = { "p-2", "p-3", "p-4" }, dry = true })
    ok(dry.can == true and dry.already and #dry.already == 2, "dry: two already on the table, can confirm")
    local j2 = run({ offer = { "p-2", "p-3", "p-4" }, confirm = true })
    ok(j2.status == "done", "confirm with the same ids again: " .. tostring(j2.status) .. " " .. tostring(j2.error))
    ok(#moves == 3, "only the new item moved (no second move of an item on the table): " .. #moves)
    local al = j2.result and j2.result.already or {}
    ok(#al == 2 and al[1].uuid == "p-2" and al[1].name == "PartyThing2" and al[2].uuid == "p-3",
        "already on the table: p-2, p-3")
    ok(j2.result and j2.result.moved and #j2.result.moved == 1 and j2.result.moved[1].uuid == "p-4", "moved p-4")
    local j3 = run({ offer = { "p-9x" }, confirm = true }) -- on no side: not in an inventory
    ok(j3.status == "failed" and tostring(j3.error):find("couldn't be moved", 1, true)
        and j3.result and j3.result.problems and #j3.result.problems == 1, "an id on no side still fails")
    BG3AI.UI.command = origCmd
end

-- latency survey 2026-11-04: one move per 200 ms tick (>= 5 s per 25 items). The next item moves once the last one
-- shows on the table; the 200 ms stays the cap for a move whose item doesn't show.
do
    local function run(landsAfter, first) -- offers p-<first> .. p-<first+2>
        dc.TraderInventoryGold, dc.CharacterOffer, dc.TraderOffer = 900, vmInv({}), vmInv({})
        local party = dc.PartyCharacterInventories.list[1].Inventory.Slots.list
        local at, origCmd, landing = {}, BG3AI.UI.command, {}
        BG3AI.UI.command = function(d, name, param)
            if name == "MoveTradeItem" then
                at[#at + 1] = clock
                landing[#landing + 1] = { item = param, t = clock + landsAfter }
            end
            return true
        end
        Ext.Entity.Get = function(u) local i = tonumber(u:match("^p%-(%d+)$")); return i and partyItems[i] or nil end
        local ids = {}
        for i = first, first + 2 do
            partyItems[i].InventoryMember = { Inventory = partyInv, EquipmentSlot = i }
            ids[#ids + 1] = "p-" .. i
        end
        local j = jobs[ops["ui.trade"]({ offer = ids }).job]
        for _ = 1, 200 do
            clock = clock + 16 -- a frame
            for _, l in ipairs(landing) do
                if not l.done and clock >= l.t then
                    l.done = true
                    for _, sl in ipairs(party) do if sl.Object == l.item then sl.Object = nil end end
                    for _, sl in ipairs(dc.CharacterOffer.Slots.list) do
                        if not sl.Object then sl.Object = l.item; break end
                    end
                end
            end
            if j.status ~= "running" then break end
            j.on_tick(j)
        end
        BG3AI.UI.command = origCmd
        return j, at
    end
    local j, at = run(16, 5) -- the item shows on the table the next frame
    ok(j.status == "done" and #at == 3, "fast trade done: " .. tostring(j.status) .. " moves " .. #at)
    ok(at[2] - at[1] <= 32 and at[3] - at[2] <= 32,
        "next move once the last shows (<= 2 frames): " .. tostring(at[2] - at[1]) .. ", " .. tostring((at[3] or 0) - at[2]))
    local j2, at2 = run(10000, 8) -- never shows in time: the old 200 ms tick
    ok(j2.status == "done" and #at2 == 3, "slow trade done: " .. tostring(j2.status) .. " moves " .. #at2)
    ok(at2[2] - at2[1] >= 200 and at2[3] - at2[2] >= 200,
        "a move whose item doesn't show waits the 200 ms cap: " .. tostring(at2[2] - at2[1]))
end

print(string.format("%d/%d passed", passes, passes + fails))
if fails > 0 then os.exit(1) end
