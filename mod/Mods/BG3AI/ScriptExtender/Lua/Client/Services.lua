-- Services UI (owner: worker "kb"): trade, short and long rest, fast travel.
--
-- Everything goes through the same ICommands the screens' buttons bind (XAML
-- in Game.pak: Mods/MainUI/GUI/Pages/Trade.xaml, MakeCamp.xaml, HotBar.xaml,
-- JournalMap.xaml), so prices, supplies, travel restrictions and rest rules
-- are the engine's. Every op takes dry=true to only check CanExecute.
--
--   ui.trade {npc?, offer?:[uuid], request?:[uuid], confirm?, dry?, max?}  -> job
--   ui.short_rest {dry?, confirm?}                                    -> job
--   ui.long_rest {dry?, confirm?}                                     -> job
--     confirm=false: stop at a game question ("End the day?") instead of
--     answering Yes; result.confirmed lists the questions answered.
--   ui.waypoints {}                                                   -> job (opens/closes the map; done once closed)
--   ui.fast_travel {waypoint, dry?}                                   -> job (waits out "Unavailable"; refuses at camp)
--   ui.travel_block {}                                                -> {error, in_camp, dialogue, screen}
--
-- Verified live on bg3le (2026-10-09, read-only on the user's save):
--   * CurrentPlayer.TravelError ("None" when travel is allowed), the map's
--     WaypointEntryButton list (rendered names, region headers, DataContext =
--     ls.VMWaypoint, Command = ui::DeferredCommand, CanExecute(vm) = true),
--     M opens the map and ESCAPE closes it.
--   * HotBar DC: ShortRest / CampTravel / GoToCamp exist; IsShortRestAvailable,
--     Stats.ShortRestPointsResource.Value/MaxValue, IsInCamp,
--     RequiredPartySupplies, Data.LongRest.IsActive all resolve.
--   * VMItem has no readable name or entity id (EntityHandle is an opaque
--     ls.EntityRef in bg3le), but VMInventorySlot.Index is the container slot
--     and Stats.Weight.Value (kg) matches the item's Data.Weight (g), which is
--     how VM inventories are matched to client ECS inventories below.
-- NOT tested live (the rules forbid trading, resting and travelling on the
-- user's save): the Execute paths of all five ops, the Trade DC as a whole
-- (no trade can be opened without a story dialogue), MakeCamp's flow.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local U = BG3AI.Util
local SV = BG3AI.Services or {}
BG3AI.Services = SV

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

local function check(ok, err)
    if not ok then error(err or "failed", 0) end
end

-- A job whose on_tick(job) returns status, result to settle it (same shape as
-- the jobs in UIActions.lua).
local function waitJob(kind, timeout_ms, fn, extra)
    local spec = extra or {}
    spec.timeout_ms = timeout_ms
    spec.on_tick = function(job)
        local status, result, err = fn(job)
        if status then RPC.finish_job(job, status, result, err) end
    end
    local job = RPC.start_job(kind, spec)
    return { job = job.id }
end

local function visibleWidget(name)
    local w = UI.widget(name)
    if w and UI.isVisible(w) then return w end
    return nil
end

local function hotbarDC()
    return UI.dc("HotBar") or UI.anyDC() or error("game UI not ready (no HotBar)")
end

local function selected(dc, path)
    return UI.path(dc, "CurrentPlayer.SelectedCharacter" .. (path and ("." .. path) or ""))
end

local function isNoesis(v)
    return type(v) == "userdata" or (type(v) == "table" and UI.typeName(v) ~= nil)
end

-- Visible message boxes, to explain a command that opened one instead.
local function popupText()
    for _, w in ipairs(UI.widgets()) do
        local n = w.name:lower()
        if (n:find("dialog_box") or n:find("messagebox") or n:find("popup")) and UI.isVisible(w.el) then
            local t = UI.allText(w.el, " ", 400)
            if t then return t end
        end
    end
    return nil
end

-- A confirmation box raised by the op's own request ("End the day?", "Someone
-- still wants to talk, rest anyway?", live S005): the agent asked for this
-- rest/travel, so answer it the way that player would, at most `max` per job,
-- and keep each question for the result. args.confirm == false: leave it and
-- fail with the question so the agent answers with ui.confirm. rest = the rest flow's own boxes: one whose text
-- is only the unresolved "[ForceUpdate]" handle (the engine's "Do you want to end the day?" ready check, BACKLOG
-- S031 note) is answered even then: S077 r1 01:13 long_rest(confirm=false) stopped on "[ForceUpdate] [Yes/No]",
-- a question nobody can read, and the agent screenshot-clicked it. A readable question still stops.
-- Returns nil (no box), true (handled: keep waiting) or "failed", nil, err.
local function unreadable(s)
    return s == nil or s == "" or s == "?" or tostring(s):gsub("%[ForceUpdate%]", ""):match("^%s*$") ~= nil
end
SV.unreadable = unreadable

local function handleConfirm(job, args, max, veto, rest)
    local A = BG3AI.UIActions
    if not (A and A.confirmBox) then return nil end
    local box = A.confirmBox()
    if not box then return nil end
    local labels = {}
    for _, b in ipairs(box.buttons) do labels[#labels + 1] = b.label end
    local q = box.text or box.title or "?"
    if args.confirm == false and not (rest and unreadable(box.text) and unreadable(box.title)) then
        return "failed", nil, "the game asks: " .. q .. " [" .. table.concat(labels, "/") .. "]; answer with confirm"
    end
    -- veto(q): a reason this op must not answer the question (vis live: fast_travel answered a rest prompt).
    local why = veto and veto(q, box) or nil
    if why then
        return "failed", { question = q, buttons = labels, unanswered = true }, "the game asks: " .. q .. " ["
            .. table.concat(labels, "/") .. "]: not answered (" .. why .. "); decide with confirm(...)"
    end
    job.confirms = job.confirms or {}
    local now = RPC.now_ms()
    if now - (job.confirmAt or 0) < 700 then return true end -- the last press is still closing the box
    -- The same box still up after a press: the command press didn't land (live S013/S014: the rest
    -- flow's "[ForceUpdate] Yes/No" box ignores its button's Command, a mouse click works). Hand it
    -- to the bridge for a hover-checked click instead of pressing again (needs_click).
    local sig = q .. "#" .. table.concat(labels, "|")
    if job.confirmSig == sig then
        return "failed", { needs_click = "yes", confirmed = job.confirms, question = q },
            "the Yes press didn't close the game's question: " .. q .. " [" .. table.concat(labels, "/") .. "]"
    end
    if #job.confirms >= (max or 3) then
        return "failed", { confirmed = job.confirms }, "too many confirmations; still asking: " .. q .. " [" .. table.concat(labels, "/") .. "]"
    end
    local ok, view = A.answerConfirm("yes", false)
    job.confirmAt, job.confirmSig = now, sig
    if not ok then return "failed", { needs_click = "yes", question = q }, "couldn't answer the game's question (" .. q .. "): " .. tostring(view) end
    job.confirms[#job.confirms + 1] = (view.text or view.title or "?") .. " -> " .. tostring(view.pressed)
    return true
end
SV.handleConfirm = handleConfirm

------------------------------------------------------------------------
-- Client ECS helpers
------------------------------------------------------------------------

local function entity(uuid)
    if type(uuid) ~= "string" then return nil end
    return try(Ext.Entity.Get, uuid)
end

local function uuidOf(e)
    local u = U.comp(e, "Uuid")
    return u and u.EntityUuid or nil
end

local function nameOf(e)
    local dn = U.comp(e, "DisplayName")
    if not dn then return nil end
    return U.loca(try(function() return dn.NameKey.Handle.Handle end))
end

local function weightOf(e)
    local d = U.comp(e, "Data")
    return d and d.Weight or nil
end

local function primaryInventory(owner)
    local io = U.comp(owner, "InventoryOwner")
    return io and io.PrimaryInventory or nil
end

-- slot -> item entity for an inventory entity.
-- One container map per inventory while a trade read runs (SV.memo set by memoized()): itemView looked
-- each slot's item up through a fresh map, O(n^2) pcalls on a 200-item trade screen.
local function containerItems(inv)
    local memo = SV.memo
    if memo and memo[inv] then return memo[inv] end
    local out = {}
    local c = U.comp(inv, "InventoryContainer")
    if not c then return out end
    for slot, rec in pairs(c.Items or {}) do
        local it = try(function() return rec.Item end)
        if it then out[tonumber(slot)] = it end
    end
    if memo then memo[inv] = out end
    return out
end

-- fn(...) with the per-read container memo (nested calls share the outer one).
local function memoized(fn, ...)
    if SV.memo then return fn(...) end
    SV.memo = {}
    local res = table.pack(pcall(fn, ...))
    SV.memo = nil
    if not res[1] then error(res[2], 0) end
    return table.unpack(res, 2, res.n)
end

-- An owner's inventory plus the inventories of containers (bags) inside it.
local function inventoriesOf(owner, out)
    out = out or {}
    local inv = primaryInventory(owner)
    if not inv then return out end
    out[#out + 1] = inv
    for _, it in pairs(containerItems(inv)) do
        local sub = primaryInventory(it)
        if sub then out[#out + 1] = sub end
    end
    return out
end

-- worn: also each member's other inventories (the Equipment one: its slots are ItemSlot values, as state.inventory
-- reads them). Gale's donation window lists worn items too (live D8: Claude's equipped Gloves of Power / Sword of
-- Justice sat in an EquipmentFlags VM at indexes 10 / 3, and feed_gale FAILED "isn't on this side").
local function partyInventories(worn)
    local out = {}
    for _, e in ipairs(try(Ext.Entity.GetAllEntitiesWithComponent, "PartyMember") or {}) do
        inventoriesOf(e, out)
        local io = worn and U.comp(e, "InventoryOwner")
        for _, inv in ipairs(io and try(function() return io.Inventories end) or {}) do
            if inv ~= io.PrimaryInventory then out[#out + 1] = inv end
        end
    end
    return out
end

-- Where an item sits: inventory entity and container slot.
local function itemLocation(uuid)
    local e = entity(uuid)
    local m = e and U.comp(e, "InventoryMember")
    if not m then return nil end
    return m.Inventory, tonumber(m.EquipmentSlot), e
end

------------------------------------------------------------------------
-- VM inventories <-> ECS inventories
------------------------------------------------------------------------

-- { {index, item=VMItem}, ... } for the occupied slots of a VMInventory.
local function vmSlots(vmInv, max)
    local out = {}
    for _, slot in UI.items(UI.prop(vmInv, "Slots"), max or 400) do
        -- two named reads, not GetAllProperties (live: ~0.01 ms vs 0.4-1.7 ms per view model)
        local obj = UI.get(slot, "Object")
        if obj ~= nil then out[#out + 1] = { index = tonumber(UI.get(slot, "Index")), item = obj } end
    end
    return out
end

local function vmWeight(item)
    local v = tonumber(UI.path(item, "Stats.Weight.Value"))
    return v and math.floor(v * 1000 + 0.5) or nil
end

-- How well a VM inventory matches an ECS inventory: every occupied VM slot
-- must hold an item of the same weight in the same container slot.
local function matchScore(slots, inv)
    local items = containerItems(inv)
    local hit, miss = 0, 0
    for _, s in ipairs(slots) do
        local e = s.index and items[s.index]
        if s.w == nil then s.w = vmWeight(s.item) or false end
        local w = s.w or nil
        if e and (w == nil or weightOf(e) == w) then hit = hit + 1 else miss = miss + 1 end
    end
    return hit - 3 * miss, hit
end

local function bestInventory(vmInv, candidates)
    local slots = vmSlots(vmInv)
    if #slots == 0 then return nil, slots end
    local best, bestScore
    for _, inv in ipairs(candidates) do
        local score, hit = matchScore(slots, inv)
        if hit > 0 and score > 0 and (bestScore == nil or score > bestScore) then
            best, bestScore = inv, score
        end
    end
    return best, slots
end

-- Collect VMInventory-like objects (anything with Slots) under a VM.
local function collectInventories(obj, out, seen, depth)
    if obj == nil or depth > 3 then return end
    local key = tostring(obj)
    if seen[key] then return end
    seen[key] = true
    local p = UI.props(obj)
    if p.Slots ~= nil then out[#out + 1] = obj end
    for _, k in ipairs({ "TradeInventory", "Inventory" }) do
        if p[k] ~= nil then collectInventories(p[k], out, seen, depth + 1) end
    end
    for _, k in ipairs({ "TradeInventories", "Inventories" }) do
        if p[k] ~= nil then
            for _, x in UI.items(p[k], 20) do collectInventories(x, out, seen, depth + 1) end
        end
    end
end

------------------------------------------------------------------------
-- Trade
------------------------------------------------------------------------

SV.trader = SV.trader or nil      -- uuid of the trader the agent named last
SV.offered = SV.offered or {}     -- tostring(VMItem) -> {uuid, name} for items we moved
-- Parts of stacks we moved (count=N of a bigger stack): the game puts the part on the table as a new VMItem, so its
-- key isn't the moved one's. {uuid, name, side, count, price} until offerView finds the part (w15 a2: Muddy Red Dye
-- x2 of x8 read as a nameless table item next to its moved record: 'BOUGHT (no id) ? x2 12 g | e127 ... x2 12 g').
SV.splits = SV.splits or {}

local function tradeWidget()
    local w = visibleWidget("Trade")
    return w, w and try(function() return w.DataContext end) or nil
end

-- Candidate ECS inventories for the trader: the named trader's, else any
-- non-party inventory owner (matched by fingerprint).
local function traderCandidates(any)
    local t = not any and entity(SV.trader)
    if t then return inventoriesOf(t) end
    local party = {}
    for _, e in ipairs(try(Ext.Entity.GetAllEntitiesWithComponent, "PartyMember") or {}) do party[e] = true end
    local out = {}
    for _, e in ipairs(try(Ext.Entity.GetAllEntitiesWithComponent, "InventoryOwner") or {}) do
        if not party[e] and U.comp(e, "ClientCharacter") then
            local inv = primaryInventory(e)
            if inv then out[#out + 1] = inv end
        end
    end
    return out
end

-- The trade screen's inventories, each matched to an ECS inventory:
-- { party = {{vm, inv, slots}}, trader = {{vm, inv, slots}} }
local function tradeLayoutRaw(dc)
    local partyVMs, seen = {}, {}
    for _, pc in UI.items(UI.prop(dc, "PartyCharacterInventories"), 8) do
        collectInventories(pc, partyVMs, seen, 0)
    end
    local traderVMs = {}
    collectInventories(UI.prop(dc, "TraderInventory"), traderVMs, {}, 0)
    local layout = { party = {}, trader = {} }
    local pc = partyInventories()
    for _, vm in ipairs(partyVMs) do
        local inv, slots = bestInventory(vm, pc)
        layout.party[#layout.party + 1] = { vm = vm, inv = inv, slots = slots }
    end
    local tc, all = traderCandidates(), nil
    for _, vm in ipairs(traderVMs) do
        local inv, slots = bestInventory(vm, tc)
        if not inv and #slots > 0 then
            -- SV.trader is the last trader named with npc=..., not necessarily this one: S026 09:03 traded
            -- with Talli (opened by talk_to) while SV.trader was still Mattis, so every ware came back
            -- nameless ("price=26 count=2; …", unmatched=1). Match against every trader-like owner.
            all = all or traderCandidates(true)
            inv = bestInventory(vm, all)
        end
        layout.trader[#layout.trader + 1] = { vm = vm, inv = inv, slots = slots }
    end
    return layout
end

local ITEM_PROPS = { "Count", "TradePrice", "Rarity", "IsStolen", "IsStoryItem", "ItemType" }

local function itemView(slotRec, inv)
    local item = slotRec.item
    local p = UI.pick(item, ITEM_PROPS)
    local e = inv and slotRec.index and containerItems(inv)[slotRec.index] or nil
    local v = {
        uuid = e and uuidOf(e) or nil,
        name = e and nameOf(e) or nil,
        count = (tonumber(p.Count) or 1) > 1 and tonumber(p.Count) or nil,
        price = (tonumber(p.TradePrice) or -1) >= 0 and tonumber(p.TradePrice) or nil,
        rarity = (p.Rarity ~= nil and p.Rarity ~= "Common") and p.Rarity or nil,
        stolen = p.IsStolen == true or nil,
        story = p.IsStoryItem == true or nil,
    }
    if e then
        -- Food shows its camp-supply value (what a rest needs: 40), so the agent can buy food.
        pcall(function()
            local st = Ext.Stats.Get(e.Data.StatsId)
            local sv = st and tonumber(st.SupplyValue)
            if sv and sv > 0 then v.supply = sv end
            -- Armor/Weapon/Object: the bridge groups wares (gear by rarity) from it
            local kind = st and st.ModifierList
            if kind == "Armor" or kind == "Weapon" then v.kind = kind end
        end)
    end
    if p.ItemType == "Gold" then return nil end
    return v
end

-- A table item we don't know on `side` that is a stack part we moved (SV.splits: same count, the price its share,
-- +-1 g of rounding): it is that item now (SV.offered), and the part leaves SV.splits.
local function claimSplit(key, p, side)
    if p.ItemType == "Gold" or not SV.splits[1] then return nil end
    local n, price = tonumber(p.Count) or 1, tonumber(p.TradePrice)
    for i, sp in ipairs(SV.splits) do
        if sp.side == side and sp.count == n and (not sp.price or not price or math.abs(sp.price - price) <= 1) then
            table.remove(SV.splits, i)
            SV.offered[key] = { uuid = sp.uuid, name = sp.name }
            return SV.offered[key]
        end
    end
    return nil
end

local function offerView(vmOffer, side)
    local out = {}
    for _, s in ipairs(vmSlots(vmOffer, 60)) do
        local p = UI.pick(s.item, ITEM_PROPS)
        local key = tostring(s.item)
        local known = SV.offered[key] or (side and claimSplit(key, p, side)) or {}
        out[#out + 1] = {
            uuid = known.uuid, name = known.name,
            count = (tonumber(p.Count) or 1) > 1 and tonumber(p.Count) or nil,
            price = (tonumber(p.TradePrice) or -1) >= 0 and tonumber(p.TradePrice) or nil,
            gold = p.ItemType == "Gold" or nil, -- the gold a Balance puts on the table is a Gold stack (price = count)
        }
    end
    return out
end

-- What a side of the table is worth: TradePrice is the whole stack's price (live 10:50 Talli: Supply Pack x3 = 156,
-- Potion of Healing x5 = 40); a gold stack is worth its count (w11 L7: the trader's Balance stack read TradePrice 0,
-- {price 0, count 52, gold true}, so every sale was refused "you would give 52g more").
function SV.tableValue(items)
    local t = 0
    for _, it in ipairs(items) do t = t + (it.gold and (it.count or 1) or it.price or 0) end
    return t
end
local sumPrices = SV.tableValue

-- The names of the items on a side whose price could not be read (no TradePrice >= 0): tableValue counts them 0, so a
-- confirm on such a table would trade them for nothing (a player sees a price).
function SV.unpriced(items)
    local out = {}
    for _, it in ipairs(items) do
        if not it.gold and not it.price then out[#out + 1] = it.name or it.uuid or "?" end
    end
    return out
end

local function goldOn(items)
    local g = 0
    for _, it in ipairs(items) do if it.gold then g = g + (it.count or 1) end end
    return g
end

-- CharacterOfferGold / TraderOfferGold are each side's total as the screen shows it, not gold put down: every table
-- read had it equal to that side's item prices (S069 20:37:24 "you get: Mantle 819g, Potions 198g, 1017 gold"; S055
-- 03:45:46 "you give: ? x60 60g, 60 gold | you get: Potions x4 208g, 208 gold"; a Crude Mace 23g, 23 gold), so the
-- balance counted everything twice. The gold on the table is the Gold stacks among the items.
-- What the table still lacks before FinishBarter (prices as shown, a gold stack = its count): nil when even within
-- TRADE_TOLERANCE_G (or 1 %), else "short", n (we would get n more than we give: the trader refuses) or "over", n
-- (we would give n more: the trader takes it for nothing).
SV.TRADE_TOLERANCE_G = 2
function SV.tradeImbalance(give, get)
    local tol = math.max(SV.TRADE_TOLERANCE_G, math.floor(0.01 * math.max(give, get)))
    if get - give > tol then return "short", get - give end
    if give - get > tol then return "over", give - get end
    return nil
end

-- Everything the trade screen shows, as data.
local function tradeLayout(dc) return memoized(tradeLayoutRaw, dc) end

local tradeStateRaw
function SV.tradeState(dc, layout, max) return memoized(tradeStateRaw, dc, layout, max) end
function tradeStateRaw(dc, layout, max)
    max = max or 60
    layout = layout or tradeLayout(dc)
    local p = UI.props(dc)
    local function listOf(groups, onlyPriced)
        local out = {}
        for _, g in ipairs(groups) do
            for _, s in ipairs(g.slots) do
                local v = itemView(s, g.inv)
                if v and (not onlyPriced or v.price) then
                    out[#out + 1] = v
                    if #out >= max then return out, true end
                end
            end
        end
        return out, false
    end
    local party, partyMore = listOf(layout.party, true)
    local wares, waresMore = listOf(layout.trader, false)
    local yours = offerView(p.CharacterOffer, "you")
    local theirs = offerView(p.TraderOffer, "trader")
    local w = UI.widget("Trade")
    local fb = w and UI.cachedByName("Trade", "TraderFeedbackText")
    return {
        mode = p.Mode,
        party_gold = tonumber(p.TradePartyGold),
        trader_gold = tonumber(p.TraderInventoryGold),
        price_modifier_pct = tonumber(p.PriceModifierPercentage),
        offer = {
            you = { items = yours, gold = goldOn(yours) },
            trader = { items = theirs, gold = goldOn(theirs) },
        },
        -- What each side puts on the table at the shown prices (its gold stacks included, at their count).
        balance = { you_give = sumPrices(yours), you_get = sumPrices(theirs) },
        feedback = fb and UI.isVisible(fb) and UI.text(fb) or nil,
        declined = p.TradeDeclined == true or nil,
        can_confirm = p.TradeButtonsEnabled ~= false,
        your_items = party, your_items_more = partyMore or nil,
        wares = wares, wares_more = waresMore or nil,
        unmatched = (function()
            local n = 0
            for _, g in ipairs(layout.party) do if not g.inv and #g.slots > 0 then n = n + 1 end end
            for _, g in ipairs(layout.trader) do if not g.inv and #g.slots > 0 then n = n + 1 end end
            return n > 0 and n or nil
        end)(),
    }
end

-- The VMItem for an item uuid, from the matched inventories in groups.
local function findVMItem(uuid, groups)
    local inv, slot, e = itemLocation(uuid)
    if not inv then return nil, "item " .. tostring(uuid) .. " isn't in an inventory" end
    for _, g in ipairs(groups) do
        if g.inv == inv then
            for _, s in ipairs(g.slots) do
                if s.index == slot then return s.item, nil, e end
            end
        end
    end
    return nil, "item " .. (nameOf(e) or uuid) .. " isn't on this side of the trade screen"
end

-- An item this trade already moved onto `side`'s table ("you": CharacterOffer, "trader": TraderOffer): {uuid, name},
-- else nil. launch-v1 haiku act3_gold_max r2 seq 16-21 (67 times in 19 runs): trade(offer=...) put the items on the
-- table, trade(offer=<the same ids>, confirm=true) looked for them in the inventories again, found them "not on
-- this side of the trade screen" and refused the confirm.
local function onTable(dc, uuid, side)
    for _, s in ipairs(vmSlots(UI.prop(dc, side == "trader" and "TraderOffer" or "CharacterOffer"), 60)) do
        local key = tostring(s.item)
        local k = SV.offered[key] or claimSplit(key, UI.pick(s.item, ITEM_PROPS), side) -- a part we moved (w15 a2)
        if k and k.uuid == uuid then return k end
    end
    return nil
end

-- The item a MoveTradeItem just moved ({key = tostring(VMItem), side}) shows on that side's table: the next move
-- needn't wait out its tick (the 200 ms stays the cap; latency survey 2026-11-04: >= 5 s of fixed ticks per 25 items).
local function landed(dc, p)
    if not p then return false end
    for _, s in ipairs(vmSlots(UI.prop(dc, p.side == "trader" and "TraderOffer" or "CharacterOffer"), 60)) do
        if tostring(s.item) == p.key then return true end
    end
    return false
end

-- The game's quantity box (SplitItem.xaml) for a stack moved on the trade screen: DataContext
-- CurrentPlayer.UIData.SplitItemData {ItemCount, TargetCount}; Confirm runs the widget's ApplySplitCommand.
-- S028 15:51: trade(request=Supply Pack x3) opened it, the tool moved nothing and said done; the agent clicked
-- the slider arrow and Confirm by hand. -> nil (no box) | box, count (applied) | box, nil, err
-- `want` = how many (default the whole stack, as trade's doc says: "a stack moves whole").
SV.splitTries = 0
function SV.answerSplit(want)
    local w = visibleWidget("SplitItem")
    if not w then SV.splitTries, SV.splitApplied = 0, nil; return nil end
    if SV.splitApplied then  -- Confirm pressed: the box is closing, never press it twice
        if RPC.now_ms() - SV.splitApplied < 2000 then return w, nil end
        SV.splitApplied = nil
        return w, nil, "the quantity box stayed open after its Confirm"
    end
    local wdc = try(function() return w.DataContext end)
    local data = wdc and UI.path(wdc, "CurrentPlayer.UIData.SplitItemData")
    local total = tonumber(data and UI.prop(data, "ItemCount"))
    if not total then return w, nil, "a quantity box opened but its count is unreadable" end
    local n = math.max(1, math.min(total, math.floor(tonumber(want) or total)))
    SV.splitTries = SV.splitTries + 1
    if SV.splitTries > 8 then
        SV.splitTries = 0
        UI.command(wdc, "CustomEvent", "SplitItemToggle", false)  -- its Cancel button
        return w, nil, "the quantity box didn't take " .. n .. " of " .. total .. " (cancelled it)"
    end
    if tonumber(UI.prop(data, "TargetCount")) ~= n then
        -- The VM first (as UIActions' confirm(amount=) does); else the box's slider (x:Name Slider, Value
        -- TwoWay-bound to TargetCount).
        if not UI.pcall(data.SetProperty, data, "TargetCount", n) then UI.pcall(function() data.TargetCount = n end) end
        if tonumber(UI.prop(data, "TargetCount")) ~= n then
            local sl = UI.byName(w, "Slider")
            if sl then UI.pcall(sl.SetProperty, sl, "Value", n) end
        end
        return w, nil  -- re-read next tick
    end
    local ok, err = UI.command(wdc, "ApplySplitCommand", nil, false)
    if not ok then return w, nil, "the quantity box's Confirm: " .. tostring(err) end
    SV.splitTries, SV.splitApplied, SV.splitCount = 0, RPC.now_ms(), n
    return w, nil
end

RPC.register("ui.trade", function(args)
    local w, dc = tradeWidget()
    if args.npc then SV.trader = args.npc end
    if not w then
        if args.npc then
            error("no trade screen is open. Trading starts from dialogue: talk to the trader (talk_to) and "
                .. "pick the trade option in the conversation, then call trade again", 0)
        end
        error("no trade screen is open", 0)
    end
    dc = dc or error("trade screen has no DataContext")
    local offer, request = args.offer or {}, args.request or {}
    local wantsChange = #offer > 0 or #request > 0 or args.confirm

    if args.dry then
        local layout = tradeLayout(dc)
        local problems, already = {}, {}
        for _, q in ipairs({ { offer, layout.party, "you" }, { request, layout.trader, "trader" } }) do
            for _, u in ipairs(q[1]) do
                local k = onTable(dc, u, q[3]) -- first: a second MoveTradeItem would take it back off
                local it, err
                if not k then it, err = findVMItem(u, q[2]) end
                if k then
                    already[#already + 1] = { uuid = u, name = k.name, side = q[3] }
                elseif not it then
                    problems[#problems + 1] = err
                end
            end
        end
        local st = SV.tradeState(dc, layout)
        st.can = #problems == 0
        st.problems = #problems > 0 and problems or nil
        st.already = #already > 0 and already or nil
        return st
    end
    -- max: items listed per side (default 60); sell=junk reads every slot (S026: 55 junk items + the rest).
    if not wantsChange then return SV.tradeState(dc, nil, tonumber(args.max)) end

    -- Barter mode shows both offers and keeps the deal explicit until confirm.
    local phase, waitUntil, balanceTries, lastTable, lastMove = "mode", 0, 0, nil, nil
    SV.splitTries, SV.splitApplied, SV.splitCount = 0, nil, nil
    local moved, problems, confirmed, queue, already = {}, {}, false, {}, {}
    -- deal: the table as FinishBarter took it ({you = {items}, trader = {items}}, offerView rows: uuid/name of what
    -- this trade moved, count, price as shown, gold): what the bridge names SOLD/BOUGHT at (REVIEW-17 F3: it read
    -- the whole screen before every confirm for the prices, and items past the first 60 slots had none).
    local deal
    if UI.prop(dc, "Mode") ~= "Barter" then
        check(UI.command(dc, "SetTradeMode", nil, false))
    end

    -- One move per tick (~0.2 s, a stack's quantity box more) + balance + finish: 15 s plus 0.35 s per item (the bridge
    -- sells junk in barters of 25; acceptance 01:07 timed out moving 72 at once).
    return waitJob("ui.trade", 15000 + 350 * (#offer + #request), function()
        local _, live = tradeWidget()
        if not live then
            if phase == "finishing" then
                return "done", { confirmed = true, moved = moved, problems = #problems > 0 and problems or nil,
                                 already = already[1] and already or nil, deal = deal,
                                 note = "trade screen closed" }
            end
            return "failed", nil, "the trade screen closed"
        end
        if RPC.now_ms() < waitUntil and not (phase == "moving" and landed(live, lastMove)) then return nil end
        lastMove = nil
        if phase == "mode" then
            if UI.prop(live, "Mode") ~= "Barter" then return nil end
            local layout = tradeLayout(live)
            for _, u in ipairs(offer) do queue[#queue + 1] = { u = u, groups = layout.party, side = "you" } end
            for _, u in ipairs(request) do queue[#queue + 1] = { u = u, groups = layout.trader, side = "trader" } end
            phase = "moving"
        end
        if phase == "moving" then
            -- One item per tick: a stack opens the game's quantity box (SplitItem), answered before the next move.
            local sw, _, err = SV.answerSplit(args.count)
            if sw then
                if err then
                    problems[#problems + 1] = err
                    phase, waitUntil = "moved", RPC.now_ms() + 250
                    return nil
                end
                waitUntil = RPC.now_ms() + 150
                return nil
            end
            if SV.splitCount then  -- the box closed after its Confirm: that many of the last stack moved
                local m = moved[#moved]
                if m then
                    if m.count and SV.splitCount > 0 and SV.splitCount < m.count then
                        if m.price then
                            m.price = math.floor(m.price * SV.splitCount / m.count + 0.5) -- the stack's price, pro rata
                        end
                        -- the part lands as a new table item: offerView names it after this record
                        SV.splits[#SV.splits + 1] = { uuid = m.uuid, name = m.name, side = m.side,
                            count = SV.splitCount, price = m.price }
                    end
                    m.count = SV.splitCount
                end
                SV.splitCount = nil
            end
            local q = table.remove(queue, 1)
            if not q then
                phase, waitUntil = "moved", RPC.now_ms() + 250
                return nil
            end
            local k = onTable(live, q.u, q.side) -- first: a second MoveTradeItem would take it back off the table
            local it, err2, e
            if not k then it, err2, e = findVMItem(q.u, q.groups) end
            if k then
                already[#already + 1] = { uuid = q.u, name = k.name, side = q.side }
            elseif it then
                local ok, err3 = UI.command(live, "MoveTradeItem", it, false)
                if ok then
                    lastMove = { key = tostring(it), side = q.side }
                    SV.offered[tostring(it)] = { uuid = q.u, name = nameOf(e) }
                    -- price/count as the screen showed the item when it moved (TradePrice = the whole stack's)
                    local p = UI.pick(it, ITEM_PROPS)
                    moved[#moved + 1] = { uuid = q.u, name = nameOf(e), side = q.side,
                        count = tonumber(p.Count) or 1,
                        price = (tonumber(p.TradePrice) or -1) >= 0 and tonumber(p.TradePrice) or nil }
                else
                    problems[#problems + 1] = (nameOf(e) or q.u) .. ": " .. tostring(err3)
                end
            else
                problems[#problems + 1] = err2
            end
            waitUntil = RPC.now_ms() + 200
            return nil
        end
        if phase == "moved" then
            if not args.confirm then
                local st = SV.tradeState(live)
                st.moved, st.problems = moved, #problems > 0 and problems or nil
                st.already = already[1] and already or nil
                return "done", st
            end
            if #problems > 0 then
                local st = SV.tradeState(live)
                st.moved, st.problems = moved, problems
                st.already = already[1] and already or nil
                return "failed", st, "not confirmed: some items couldn't be moved"
            end
            -- Balance with gold the way the Balance button does, then barter.
            UI.command(live, "BalanceTrade", nil, false)
            phase, waitUntil, balanceTries = "balanced", RPC.now_ms() + 250, 0
            return nil
        end
        if phase == "balanced" then
            -- S026 09:39: confirm right after an offer, with our items on the table and nothing coming
            -- back (BalanceTrade not applied yet): the game asked to DONATE them, the job timed out, and the
            -- agent balanced + bartered by hand. S069 20:37:23 (a trader with 0 gold): request 2 wares (1017g),
            -- confirm: FinishBarter went out before the Balance's gold was on our side, refused, ESCAPE + reopen
            -- (~25 s). Never finish an uneven deal: wait for the Balance (pressed again on a table that stopped
            -- changing), else say why. An overpay goes through only on purpose (donate).
            local yours = offerView(UI.prop(live, "CharacterOffer"), "you")
            local theirs = offerView(UI.prop(live, "TraderOffer"), "trader")
            -- An item without a readable price would count 0 and pass as even: never barter it (a gift on purpose,
            -- donate=true, gives our side away whatever its price).
            local noPrice = SV.unpriced(theirs)
            if not args.donate then
                for _, n in ipairs(SV.unpriced(yours)) do noPrice[#noPrice + 1] = n end
            end
            if #noPrice > 0 then
                local st = SV.tradeState(live)
                st.moved, st.already = moved, already[1] and already or nil
                return "failed", st, "not confirmed: the price of " .. table.concat(noPrice, ", ")
                    .. " on the table could not be read"
            end
            local give, get = sumPrices(yours), sumPrices(theirs)
            local side, n = SV.tradeImbalance(give, get)
            if side and not (side == "over" and args.donate) then
                local sig = give .. "/" .. get
                if balanceTries < 5 then
                    if sig == lastTable then UI.command(live, "BalanceTrade", nil, false) end
                    lastTable, balanceTries = sig, balanceTries + 1
                    waitUntil = RPC.now_ms() + 300
                    return nil
                end
                local has = tonumber(UI.prop(live, "TraderInventoryGold")) or 0
                local ours = tonumber(UI.prop(live, "TradePartyGold")) or 0
                local st = SV.tradeState(live)
                st.moved, st.already = moved, already[1] and already or nil
                if side == "short" then
                    return "failed", st, string.format("not confirmed: you would get %dg more than you give (the Balance"
                        .. " didn't put your gold on the table; your gold %d), so the trader would refuse: call"
                        .. " trade(confirm=true) again, or offer items too", n, ours)
                end
                if #theirs == 0 then
                    return "failed", st, has > 0 and "not confirmed: the Balance didn't add the trader's gold, so the "
                        .. "game would take your items for nothing (donate): call trade(confirm=true) again"
                        or "not confirmed: the trader has no gold left, so the game would take your items for nothing "
                        .. "(donate): request wares of equal value instead (request=<ids>), or give them on purpose "
                        .. "with trade(offer=<ids>, confirm=true, donate=true)"
                end
                return "failed", st, string.format("not confirmed: you would give %dg more than you get (%s), so the"
                    .. " game would take it for nothing: request wares worth %dg more, or confirm it on purpose with"
                    .. " donate=true", n, has > 0 and "the Balance didn't add the trader's gold"
                        or "the trader has no gold to pay the difference: items only", n)
            end
            deal = { you = { items = yours }, trader = { items = theirs } }
            local param = UI.prop(live, "ActionCommandParameter")
            if not isNoesis(param) then param = nil end
            local ok, err = UI.command(live, "FinishBarter", param, false)
            if not ok then
                local st = SV.tradeState(live)
                st.moved, st.already = moved, already[1] and already or nil
                return "failed", st, "couldn't confirm the barter: " .. tostring(err)
            end
            phase, waitUntil = "finishing", RPC.now_ms() + 300
            -- S028 15:48 (Wulbren's tools: a quest hand-over through the trade screen): a one-sided deal makes the
            -- game ask "Donate these items?"; the bridge answers that box (trade donate=true), so stop here.
            if args.donate and #yours > 0 and #theirs == 0 then
                local st = SV.tradeState(live)
                st.moved, st.donate_box, st.deal = moved, true, deal
                st.already = already[1] and already or nil
                return "done", st
            end
            return nil
        end
        if phase == "finishing" then
            -- Waiting for the offers to clear: two slot counts per tick, the full screen read only once
            -- it settles (it was rebuilt every tick: job.poll waited 400-750 ms behind each, S023-S026).
            local declined = UI.get(live, "TradeDeclined") == true
            local pending = #vmSlots(UI.prop(live, "CharacterOffer"), 60) + #vmSlots(UI.prop(live, "TraderOffer"), 60)
            if not declined and pending > 0 then return nil end
            local st = SV.tradeState(live)
            st.moved, st.already = moved, already[1] and already or nil
            if declined then
                return "failed", st, "the trader refused the offer"
            end
            if #st.offer.you.items == 0 and #st.offer.trader.items == 0 then
                confirmed = true
                SV.offered, SV.splits = {}, {}
                st.confirmed, st.deal = confirmed, deal
                return "done", st
            end
            return nil
        end
    end)
end)

-- File-local helpers the other parts use.
SV.inner = { try = try, check = check, waitJob = waitJob, visibleWidget = visibleWidget, hotbarDC = hotbarDC,
    selected = selected, isNoesis = isNoesis, handleConfirm = handleConfirm, entity = entity, uuidOf = uuidOf,
    nameOf = nameOf, containerItems = containerItems, memoized = memoized, inventoriesOf = inventoriesOf,
    partyInventories = partyInventories, vmSlots = vmSlots, bestInventory = bestInventory,
    collectInventories = collectInventories, itemView = itemView, findVMItem = findVMItem }

-- The rest of the module, in this order (split for the 1000-line cap). Each part reads SV and the
-- helpers in SV.inner.
for _, part in ipairs({ "Client/ServicesRest.lua", "Client/ServicesWaypoints.lua",
    "Client/ServicesItems.lua" }) do
    Ext.Require(part)
end

return SV
