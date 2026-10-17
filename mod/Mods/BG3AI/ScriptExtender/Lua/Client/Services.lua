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

local function partyInventories()
    local out = {}
    for _, e in ipairs(try(Ext.Entity.GetAllEntitiesWithComponent, "PartyMember") or {}) do
        inventoriesOf(e, out)
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

local function offerView(vmOffer)
    local out = {}
    for _, s in ipairs(vmSlots(vmOffer, 60)) do
        local p = UI.pick(s.item, ITEM_PROPS)
        local known = SV.offered[tostring(s.item)] or {}
        out[#out + 1] = {
            uuid = known.uuid, name = known.name,
            count = (tonumber(p.Count) or 1) > 1 and tonumber(p.Count) or nil,
            price = (tonumber(p.TradePrice) or -1) >= 0 and tonumber(p.TradePrice) or nil,
            gold = p.ItemType == "Gold" or nil, -- the gold a Balance puts on the table is a Gold stack (price = count)
        }
    end
    return out
end

local function sumPrices(items)
    local t = 0
    -- TradePrice is the whole stack's price (live 10:50 Talli: Supply Pack x3 = 156, Potion of Healing x5 = 40)
    for _, it in ipairs(items) do t = t + (it.price or 0) end
    return t
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
    local yours = offerView(p.CharacterOffer)
    local theirs = offerView(p.TraderOffer)
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
        if not pcall(data.SetProperty, data, "TargetCount", n) then pcall(function() data.TargetCount = n end) end
        if tonumber(UI.prop(data, "TargetCount")) ~= n then
            local sl = UI.byName(w, "Slider")
            if sl then pcall(sl.SetProperty, sl, "Value", n) end
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
        local problems = {}
        for _, u in ipairs(offer) do
            local it, err = findVMItem(u, layout.party)
            if not it then problems[#problems + 1] = err end
        end
        for _, u in ipairs(request) do
            local it, err = findVMItem(u, layout.trader)
            if not it then problems[#problems + 1] = err end
        end
        local st = SV.tradeState(dc, layout)
        st.can = #problems == 0
        st.problems = #problems > 0 and problems or nil
        return st
    end
    -- max: items listed per side (default 60); sell=junk reads every slot (S026: 55 junk items + the rest).
    if not wantsChange then return SV.tradeState(dc, nil, tonumber(args.max)) end

    -- Barter mode shows both offers and keeps the deal explicit until confirm.
    local phase, waitUntil, balanceTries, lastTable = "mode", 0, 0, nil
    SV.splitTries, SV.splitApplied, SV.splitCount = 0, nil, nil
    local moved, problems, confirmed, queue = {}, {}, false, {}
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
                                 note = "trade screen closed" }
            end
            return "failed", nil, "the trade screen closed"
        end
        if RPC.now_ms() < waitUntil then return nil end
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
                if moved[#moved] then moved[#moved].count = SV.splitCount end
                SV.splitCount = nil
            end
            local q = table.remove(queue, 1)
            if not q then
                phase, waitUntil = "moved", RPC.now_ms() + 250
                return nil
            end
            local it, err2, e = findVMItem(q.u, q.groups)
            if it then
                local ok, err3 = UI.command(live, "MoveTradeItem", it, false)
                if ok then
                    SV.offered[tostring(it)] = { uuid = q.u, name = nameOf(e) }
                    moved[#moved + 1] = { uuid = q.u, name = nameOf(e), side = q.side }
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
                return "done", st
            end
            if #problems > 0 then
                local st = SV.tradeState(live)
                st.moved, st.problems = moved, problems
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
            local yours, theirs = offerView(UI.prop(live, "CharacterOffer")), offerView(UI.prop(live, "TraderOffer"))
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
                st.moved = moved
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
            local param = UI.prop(live, "ActionCommandParameter")
            if not isNoesis(param) then param = nil end
            local ok, err = UI.command(live, "FinishBarter", param, false)
            if not ok then
                local st = SV.tradeState(live)
                st.moved = moved
                return "failed", st, "couldn't confirm the barter: " .. tostring(err)
            end
            phase, waitUntil = "finishing", RPC.now_ms() + 300
            -- S028 15:48 (Wulbren's tools: a quest hand-over through the trade screen): a one-sided deal makes the
            -- game ask "Donate these items?"; the bridge answers that box (trade donate=true), so stop here.
            if args.donate and #yours > 0 and #theirs == 0 then
                local st = SV.tradeState(live)
                st.moved, st.donate_box = moved, true
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
            st.moved = moved
            if declined then
                return "failed", st, "the trader refused the offer"
            end
            if #st.offer.you.items == 0 and #st.offer.trader.items == 0 then
                confirmed = true
                SV.offered = {}
                st.confirmed = confirmed
                return "done", st
            end
            return nil
        end
    end)
end)

------------------------------------------------------------------------
-- Rest
------------------------------------------------------------------------

-- Short rests left (party-wide, shown on the hotbar's rest button): the bridge's ALERTS line says
-- "short rests left N: short_rest" when someone is hurt.
RPC.register("ui.rest_info", function()
    local dc = hotbarDC()
    return {
        short_rests_left = tonumber(selected(dc, "Stats.ShortRestPointsResource.Value")),
        short_rests_max = tonumber(selected(dc, "Stats.ShortRestPointsResource.MaxValue")),
    }
end)

RPC.register("ui.short_rest", function(args)
    local dc = hotbarDC()
    local before = tonumber(selected(dc, "Stats.ShortRestPointsResource.Value"))
    local max = tonumber(selected(dc, "Stats.ShortRestPointsResource.MaxValue"))
    if selected(dc, "PlayerCharacterProperties.IsShortRestAvailable") == false then
        -- One reason, not both: "0/2 left; not possible in combat" read as a combat block at 0 left.
        if before == 0 then
            error("no short rests left (0/" .. tostring(max or "?") .. "): a long rest restores them", 0)
        end
        error("short rest isn't available now (" .. tostring(before or "?") .. "/" .. tostring(max or "?")
            .. " left): the game blocks it here (in combat, or in a dialogue/cutscene)", 0)
    end
    check(UI.command(dc, "ShortRest", nil, args.dry))
    if args.dry then return { can = true, short_rests_left = before } end
    return waitJob("ui.short_rest", 15000, function(job)
        local now = tonumber(selected(hotbarDC(), "Stats.ShortRestPointsResource.Value"))
        if before and now and now < before then
            return "done", { short_rests_left = now, of = max, confirmed = job.confirms }
        end
        local c, cres, err = handleConfirm(job, args, nil, nil, true)
        if c == "failed" then return "failed", cres, err end
    end)
end)

-- What is on screen, for a failure message: the question box, else the visible
-- widgets (names only, a dozen at most).
local function screenNow()
    local A = BG3AI.UIActions
    local box = A and A.confirmBox and A.confirmBox()
    if box then
        local labels = {}
        for _, b in ipairs(box.buttons) do labels[#labels + 1] = b.label end
        return "a question box: " .. tostring(box.text or box.title or "?") .. " [" .. table.concat(labels, "/") .. "]"
    end
    local split = A and A.splitBox and A.splitBox()
    if split then
        return "a stack-split box: " .. tostring(split.title) .. " " .. tostring(split.text)
            .. " [Cancel/Confirm]: answer it with confirm(\"yes\", amount=N) or confirm(\"no\")"
    end
    -- The always-on HUD layers are left out: S028-S030 rest failures listed 12 of them and cut the list there,
    -- so a box further down could never be named.
    local names, hud = {}, 0
    for _, w in ipairs(UI.widgets()) do
        if UI.isVisible(w.el) then
            if SV.HUD_WIDGETS[w.name] then
                hud = hud + 1
            elseif #names >= 12 then
                names[#names + 1] = "..."
                break
            else
                names[#names + 1] = w.name
            end
        end
    end
    return "widgets=[" .. table.concat(names, ", ") .. "] (+" .. hud .. " HUD layers)"
end

-- Visible on every plain game screen (S0xx failure dumps): not worth naming in screenNow / restSeen.
SV.HUD_WIDGETS = {}
for _, n in ipairs({ "CrossplayOverlay", "ScreenFade", "Overlay", "AlwaysOnTopOverlay", "PassiveRoll",
                     "PlayerPortraits", "OverheadInfo", "HudIndicator", "DragAndDropPreview", "CursorText",
                     "CombatantsOverlay", "Minimap", "HotBar", "TurnModeInfo", "TargetInfo", "WorldTooltips" }) do
    SV.HUD_WIDGETS[n] = true
end

-- Non-HUD widgets that became visible while a job waited (names added to `seen`, in order, once each). The rest
-- flow's "End the day?" box showed within ~1 s in every success (S024-S026) and never in the failures since S028:
-- this tells "no box at all" from "a box came and went" (closed by something, or under another name).
function SV.noteWidgets(seen, base)
    seen.set = seen.set or {}
    for _, w in ipairs(UI.widgets()) do
        if not SV.HUD_WIDGETS[w.name] and not base[w.name] and not seen.set[w.name] and UI.isVisible(w.el) then
            seen.set[w.name] = true
            seen[#seen + 1] = w.name
        end
    end
end
function SV.visibleNames()
    local base = {}
    for _, w in ipairs(UI.widgets()) do
        if UI.isVisible(w.el) then base[w.name] = true end
    end
    return base
end

-- The rest flow across jobs. Live S018 (x4, every first long_rest of a camp visit):
-- CampTravel -> "[ForceUpdate] Yes/No" whose Yes ignores its Command -> needs_click ->
-- the bridge clicks Yes and runs ui.long_rest again 0.5 s later. That re-run saw no
-- MakeCamp yet, pressed CampTravel a second time, and the rest screen never opened
-- (the agent's own retry then worked). So: a re-run within a few seconds of our own
-- question box waits for the screen instead of pressing CampTravel again, and only
-- re-presses (once) after REST_QUIET_MS of nothing.
SV.rest = SV.rest or {}
local REST_QUIET_MS = 4000      -- nothing on screen this long after the last press/answer: press again / give up
local REST_FOLLOWUP_MS = 10000  -- a long_rest job stopped at a box this recently: this call continues that flow

local function sinceTxt(ms) return ms and string.format("%.1fs ago", (RPC.now_ms() - ms) / 1000) or "never" end

-- Night camp (live 2026-10-10 11:1x): CurrentPlayer.IsAtNightCamp=true, LongRest.IsActive=false and
-- TravelError=Unavailable: a long rest whose night never ended (a camp scene ran, or our job returned
-- 'done' 20 s after the rest screen closed). Until the day ends, camp leave fails "cannot travel right now"
-- and fast travel says "Unavailable" (29/31 fast_travel failures 10:00-10:52). The camp's long rest command
-- (CampTravel) at night opens the rest screen again (live 11:2x) or goes to sleep; resting ends the night.
local function nightCamp(dc)
    return UI.path(dc or hotbarDC(), "CurrentPlayer.IsAtNightCamp") == true
end
SV.nightCamp = nightCamp
local NIGHT_END_MS = 30000      -- after the sleep press: the night must end within this

RPC.register("ui.long_rest", function(args)
    local dc = hotbarDC()
    if selected(dc, "PlayerCharacterProperties.IsInCamp") ~= true then
        error("not at camp: travel to camp first (the camp tool), then long rest", 0)
    end
    local required = tonumber(UI.path(dc, "CurrentPlayer.RequiredPartySupplies"))
    if args.dry then
        if not visibleWidget("MakeCamp") then check(UI.command(dc, "CampTravel", nil, true)) end
        return { can = true, supplies_required = required }
    end

    -- A conversation on screen swallows CampTravel: live S026 09:07 pressed it twice over 8 s during
    -- Karlach's camp talk, then failed with "rest screen didn't open". Say so at once instead.
    if visibleWidget("Dialogue") and not visibleWidget("MakeCamp") then
        error("a conversation is open: finish it first (choose_dialogue / dialogue_advance), then long_rest", 0)
    end
    -- The HUD's Long Rest item is greyed out when CurrentPlayer.EndTheDayState is Disabled/Unavailable
    -- (HotBar.xaml LongRestItem), but CampTravel.CanExecute stays true and the press does nothing: S028 15:53
    -- three long_rest calls "pressed CampTravel 2x, the rest screen didn't open", then screenshots and HUD
    -- clicks. A player sees the greyed button: say it at once. (At night camp the sleep flow below handles it.)
    local endDay = tostring(UI.path(dc, "CurrentPlayer.EndTheDayState") or "?")
    if (endDay == "Disabled" or endDay == "Unavailable") and not nightCamp(dc) and not visibleWidget("MakeCamp") then
        error("the game doesn't allow a long rest right now (the Long Rest button is greyed out: EndTheDayState="
            .. endDay .. "); a story event may be blocking it: carry on and try again later", 0)
    end
    local R = SV.rest
    local A = BG3AI.UIActions
    -- S028 15:52: a Supply Pack split box left from a closed trade swallowed CampTravel twice.
    local cancelledSplit = A and A.cancelStaleSplit and A.cancelStaleSplit()
    local presses, step, retried = 0, "open", false
    local night = nightCamp(dc) and not visibleWidget("MakeCamp")
    local function pressCamp()
        check(UI.command(hotbarDC(), "CampTravel", nil, false))
        presses, R.pressAt = presses + 1, RPC.now_ms()
    end
    local followup = R.boxAt and RPC.now_ms() - R.boxAt < REST_FOLLOWUP_MS
    local pendingPress = false
    if visibleWidget("MakeCamp") then
        step = "screen already open"
    elseif cancelledSplit then
        pendingPress, step = true, "after closing a leftover '" .. tostring(cancelledSplit) .. "' split box"
    elseif followup and not (A and A.confirmBox and A.confirmBox()) then
        step = "after the question was answered (waiting for the screen, CampTravel not pressed again)"
    elseif args.no_press and not (A and A.confirmBox and A.confirmBox()) then
        -- the bridge saw the game's late yes (end_day_ok): the night is starting, and a press now would be the
        -- story's "leave night mode" (GLO_Camp PROC_Camp_SwitchNightMode)
        step = "after the game's late yes (waiting for the screen, CampTravel not pressed again)"
    else
        pressCamp()
        step = "after CampTravel"
    end

    local seenBase, seen, seenAt = SV.visibleNames(), {}, 0
    local function openFail(what)
        local res = { step = "open", camp_travel_presses = presses }
        res.end_the_day = tostring(UI.path(hotbarDC(), "CurrentPlayer.EndTheDayState") or "?")
        res.seen_widgets = seen
        return "failed", res, "the rest screen didn't open (" .. what .. "; step: " .. step
            .. "; EndTheDayState " .. res.end_the_day
            .. "; CampTravel pressed " .. presses .. "x in this call, last " .. sinceTxt(R.pressAt)
            .. "; last question box " .. sinceTxt(R.boxAt)
            .. "; came and went: " .. (#seen > 0 and table.concat(seen, ", ") or "nothing")
            .. "). On screen: " .. screenNow()
    end

    local phase, sawActive, used, phaseStart = "open", false, nil, RPC.now_ms()
    if night then phase, step = "sleep", "at night camp: CampTravel = go to sleep" end
    local sleepAt, sleepRetried = night and RPC.now_ms() or nil, false
    -- The night ends (IsAtNightCamp false) after the sleep press; a camp scene or the rest screen may come first.
    local function sleepTick(job, result)
        local now = RPC.now_ms()
        local c, cres, err = handleConfirm(job, args, nil, nil, true)
        if c == "failed" then
            R.boxAt = now
            return "failed", cres, err
        end
        if c then sleepAt = now return nil end
        result.confirmed = job.confirms
        if not nightCamp() then
            result.night_ended = true
            return "done", result
        end
        if visibleWidget("Dialogue") then
            result.camp_scene, result.night_open = true, true
            result.note = "a camp scene started at night: finish it, then long_rest again to sleep (until the "
                .. "night ends, camp leave and fast travel are blocked)"
            return "done", result
        end
        if visibleWidget("MakeCamp") then return "rest_screen" end
        if not sleepRetried and now - sleepAt > REST_QUIET_MS then
            pressCamp()
            sleepRetried, sleepAt = true, now
            return nil
        end
        if now - sleepAt > NIGHT_END_MS then
            result.step = "sleep"
            return "failed", result, "the night at camp didn't end (still IsAtNightCamp " .. string.format("%.0f",
                (now - sleepAt) / 1000) .. " s after going to sleep; CampTravel pressed " .. presses
                .. "x). Camp leave and fast travel stay blocked until it does. On screen: " .. screenNow()
        end
        return nil
    end

    return waitJob("ui.long_rest", 85000 + NIGHT_END_MS, function(job)
        if phase == "sleep" then
            local st, res, err = sleepTick(job, { rest = night and "night_finished" or nil })
            if st ~= "rest_screen" then return st, res, err end
            -- Live 11:2x: at night camp the rest button opens the rest screen (supplies); resting from it ends the
            -- night. sleep_only (the bridge before travelling: the night blocks all travel) rests even partially.
            if args.sleep_only then args.partial = true end
            phase, phaseStart = "open", RPC.now_ms()
        end
        local w = visibleWidget("MakeCamp")
        -- "End the day?" comes before the rest screen opens; others may follow it, and
        -- a partial rest (too few supplies) may ask once more after Rest is pressed.
        if phase ~= "resting" or (w and not sawActive) then
            local c, cres, err = handleConfirm(job, args, nil, nil, true)
            if c == "failed" then
                R.boxAt = RPC.now_ms() -- the bridge clicks it (needs_click) or the agent answers, then calls again
                cres = cres or {}
                cres.confirmed, cres.step = cres.confirmed or job.confirms, phase
                return "failed", cres, err
            end
            if c then
                if phase == "open" then
                    phaseStart, step = RPC.now_ms(), "after answering " .. tostring(job.confirms and job.confirms[#job.confirms])
                end
                return nil
            end
        end
        if phase == "open" then
            if not w and pendingPress then
                -- the split box takes a moment to close; a CampTravel pressed under it is swallowed
                if RPC.now_ms() - phaseStart >= 400 then
                    pressCamp()
                    pendingPress, phaseStart = false, RPC.now_ms()
                end
                return nil
            end
            if not w then
                if RPC.now_ms() - seenAt >= 250 then
                    seenAt = RPC.now_ms()
                    pcall(SV.noteWidgets, seen, seenBase)
                end
                if visibleWidget("Dialogue") and (job.confirms or followup) then
                    local st, res, err = openFail("a dialogue started instead (a camp scene): finish it, then long_rest again")
                    res.confirmed, res.camp_scene = job.confirms, true
                    return st, res, err
                end
                if RPC.now_ms() - phaseStart > REST_QUIET_MS then
                    -- repress=false (the bridge): S030's 4 s re-press landed inside the engine's ~10 s End-the-Day
                    -- answer window (requested → gathered → requested → end_day_failed). The bridge reads the
                    -- server's rest flow and re-presses only when the first press never reached the story.
                    if not retried and args.repress ~= false then
                        -- nothing came of the last press/answer: press the camp button again, once
                        pressCamp()
                        retried, step, phaseStart = true, "after CampTravel (retry)", RPC.now_ms()
                        return nil
                    end
                    local st, res, err = openFail("nothing for " .. string.format("%.1f", REST_QUIET_MS / 1000) .. "s")
                    res.confirmed = job.confirms
                    return st, res, err
                end
                return nil
            end
            local mdc = w.DataContext
            local sv = UI.byName(w, "SuppliesView", { hidden = true })
            local sel = sv and UI.prop(sv, "SelectedItems") or nil
            if not isNoesis(sel) then sel = nil end
            local ok, err = UI.command(mdc, "AutoSelectCommand", sel, false)
            if not ok then
                return "failed", { step = "supplies" }, "the rest screen opened but Auto-Select (supplies) failed: "
                    .. tostring(err) .. ". On screen: " .. screenNow()
            end
            R.boxAt = nil
            phase, phaseStart = "selected", RPC.now_ms()
            return nil
        end
        if phase == "selected" then
            if not w then
                return "failed", { step = "supplies", confirmed = job.confirms },
                    "the rest screen closed after Auto-Select, before Rest was pressed. On screen: " .. screenNow()
            end
            if RPC.now_ms() - phaseStart < 250 then return nil end
            local mdc = w.DataContext
            used = tonumber(UI.prop(mdc, "SelectedSuppliesAmount"))
            required = tonumber(UI.path(mdc, "CurrentPlayer.RequiredPartySupplies")) or required
            -- Too few supplies = a partial rest (half HP and slots). S025 and S026 rested partially twice
            -- and went on at 34/88 HP: stop and say so unless the agent asked for it (partial=true).
            if used and required and used < required and not args.partial then
                return "failed", { step = "supplies", supplies_used = used, supplies_required = required,
                                   partial_refused = true },
                    "only " .. used .. "/" .. required .. " camp supplies: resting now is a PARTIAL rest (half HP "
                    .. "and spell slots). Get food first (get_inventory filter=food; traders sell it: trade wares "
                    .. "marked supply=N), or long_rest(partial=true) to rest anyway. The rest screen is still open "
                    .. "(press_key escape closes it)"
            end
            local ok, err = UI.command(mdc, "ProcessRestCommand", nil, false)
            if not ok then
                return "failed", { step = "rest", supplies_used = used, supplies_required = required },
                    "supplies selected (" .. tostring(used) .. "/" .. tostring(required)
                    .. ") but the Rest button didn't work: " .. tostring(err) .. ". On screen: " .. screenNow()
            end
            phase, phaseStart = "resting", RPC.now_ms()
            return nil
        end
        -- resting: the long rest runs; camp scenes start as dialogues
        local active = UI.path(hotbarDC(), "Data.LongRest.IsActive") == true
        if active then sawActive = true end
        local result = {
            rest = (used and required and used >= required) and "full" or "partial",
            supplies_used = used, supplies_required = required, confirmed = job.confirms,
            closed_split_box = cancelledSplit,
        }
        if result.rest == "partial" then
            -- Below the requirement the game camps WITHOUT supplies ("Camp without supplies? You will not use
            -- any camp supplies", live 2026-10-10: 18 carried before and after): none were used.
            result.supplies_used, result.supplies_carried = 0, used
            result.note = "not enough camp supplies: partial rest without supplies (half HP and spell slots, no "
                .. "short rests restored; the " .. tostring(used or 0) .. " supplies you carry were kept)"
        end
        if visibleWidget("Dialogue") then
            result.camp_scene = true
            -- live 11:2x: after a rest at night the sleep cutscene is a short Dialogue and the night is already over
            if night and not nightCamp() then result.night_ended = true end
            if nightCamp() then
                result.night_open = true
                result.note = (result.note and (result.note .. "; ") or "") .. "after the camp scene, long_rest "
                    .. "again to sleep (until the night ends, camp leave and fast travel are blocked)"
            end
            return "done", result
        end
        if not w and (sawActive and not active or RPC.now_ms() - phaseStart > 20000) then
            if not nightCamp() then
                if night then result.night_ended = true end
                return "done", result
            end
            -- the rest screen closed but the day didn't end: go to sleep (sleepTick finishes)
            if not sleepAt then
                pressCamp()
                sleepAt = RPC.now_ms()
                return nil
            end
            local st, res, err = sleepTick(job, result)
            if st == "rest_screen" then return nil end
            return st, res, err
        end
        if w and not sawActive and RPC.now_ms() - phaseStart > 10000 then
            result.step = "rest"
            return "failed", result, "Rest was pressed (supplies " .. tostring(used) .. "/" .. tostring(required)
                .. ") but the rest didn't start in 10s. On screen: " .. screenNow()
        end
        return nil
    end)
end)

------------------------------------------------------------------------
-- Waypoints and fast travel (the map's waypoint list)
------------------------------------------------------------------------

local function mapWidget() return visibleWidget("JournalMap") end

-- { {region, name, button}, ... } from the open map, in display order.
local function mapWaypoints(w)
    local list = UI.byName(w, "Waypoints", { hidden = true })
    if not list then return {} end
    local out, region = {}, nil
    local buttons = UI.findAll(list, function(e) return UI.name(e) == "WaypointEntryButton" end, { prune = true })
    local bi = 0
    for _, entry in ipairs(UI.content(list, { max = 4000 })) do
        if entry.t then
            region = entry.t
        elseif entry.n == "WaypointEntryButton" then
            bi = bi + 1
            out[#out + 1] = { region = region, name = entry.b, button = buttons[bi], off = entry.off }
        end
    end
    if #out == 0 then
        -- Panel not laid out / collapsed: read each button's own text (hidden too),
        -- else its view model's name.
        for _, b in ipairs(buttons) do
            local parts = {}
            for _, t in ipairs(UI.findAll(b, function(e) return UI.typeName(e) == "TextBlock" end, { hidden = true, max = 300 })) do
                parts[#parts + 1] = UI.text(t)
            end
            local name = #parts > 0 and table.concat(parts, " ") or nil
            if not name then
                local vm = try(function() return b.DataContext end)
                for _, k in ipairs({ "Name", "DisplayName", "Title", "Label" }) do
                    local v = vm and UI.prop(vm, k)
                    if type(v) == "string" and v ~= "" then name = v break end
                end
            end
            if name then out[#out + 1] = { name = name, button = b } end
        end
    end
    return out
end

local function travelError(dc)
    local e = UI.path(dc or hotbarDC(), "CurrentPlayer.TravelError")
    if e == nil or e == "None" then return nil end
    return tostring(e)
end

local function playerPos()
    local c = (try(Ext.Entity.GetAllEntitiesWithComponent, "ClientControl") or {})[1]
    local t = c and U.comp(c, "Transform")
    local v = t and try(function() return t.Transform.Translate end)
    return v and { v[1], v[2], v[3] } or nil
end

-- Opens the map if needed, runs fn(mapWidget, openedByUs, state) on a later tick.
-- Live S021 (x7): every fast_travel right after a successful one failed "the map didn't
-- open" after 3 s, and the call after that found the map already up (done in 0.5 s): the
-- M toggle was a step out of phase, so one press was swallowed. So: no map MAP_PRESS_MS
-- after a press -> press again (MAP_PRESSES in all). A game menu (a stray ESCAPE) eats M,
-- so that press is an ESCAPE instead. A map that opens slowly and gets closed by the
-- 2nd press is reopened by the 3rd (normal opens + travel take 0.5 s in all).
-- pre(job, state): runs each tick before the map is touched; nil = keep waiting, true = go on,
-- or "failed", result, err (fast_travel waits out a TravelError that clears after a load).
-- 5 (was 3): live 11:4x, right after a camp leave M was ignored for >3.6 s (the leave's transition)
local MAP_PRESS_MS, MAP_PRESSES = 1200, 5
-- S055 06:46:20: fast_travel "the map didn't open (pressed M,M,M,M,M over 6.2s)" in the Lower City, and check_all's
-- "screen map after closing the map (left open?)" before S052/S055: the Act 3 map takes longer than 1.2 s to show,
-- so the next M closed it again. A JournalMap widget that appeared after our press but isn't visible yet is the map
-- opening: wait for it up to MAP_OPENING_MS before pressing again.
SV.MAP_OPENING_MS = 4000
local function withMap(kind, timeout_ms, fn, pre)
    local state = { mapPresses = 0, mapKeys = {} }
    local function press()
        local k = visibleWidget("GameMenu") and "ESCAPE" or "M"
        if state.preexist == nil then state.preexist = UI.widget("JournalMap") ~= nil end
        Ext.Input.InjectKeyPress(k)
        state.mapPresses, state.mapPressT = state.mapPresses + 1, RPC.now_ms()
        state.mapKeys[#state.mapKeys + 1] = k
    end
    local opened = false
    local function begin()
        state.begun = true
        if not mapWidget() then
            press()
            opened = true
        end
    end
    if not pre then begin() end
    return waitJob(kind, timeout_ms, function(job)
        if not state.begun then
            local go, res, err = pre(job, state)
            if go == "failed" then return "failed", res, err end
            if not go then return nil end
            begin()
            return nil
        end
        if not state.ready then
            if not mapWidget() then
                -- A question box up instead of the map (vis live: a rest prompt; M then "didn't open the map"):
                -- report it, never answer it here.
                local A = BG3AI.UIActions
                local box = A and A.confirmBox and A.confirmBox()
                if box then
                    local labels = {}
                    for _, b in ipairs(box.buttons or {}) do labels[#labels + 1] = b.label end
                    local q = box.text or box.title or "?"
                    return "failed", { question = q, buttons = labels, unanswered = true, keys = state.mapKeys },
                        "the game asks: " .. q .. " [" .. table.concat(labels, "/") .. "] (instead of the map "
                        .. "opening): not answered; decide with confirm(...), then try again"
                end
                local opening = not state.preexist and UI.widget("JournalMap") ~= nil
                if opening then state.opening = true end
                if RPC.now_ms() - (state.mapPressT or 0) >= (opening and SV.MAP_OPENING_MS or MAP_PRESS_MS) then
                    if state.mapPresses >= MAP_PRESSES then
                        return "failed", { map_presses = state.mapPresses, keys = state.mapKeys, opening = state.opening },
                            "the map didn't open (pressed " .. table.concat(state.mapKeys, ",") .. " over " .. string.format("%.1f", (RPC.now_ms() - job.started) / 1000)
                            .. "s). On screen: " .. screenNow()
                    end
                    press()
                end
                return nil
            end
            state.ready = true
        end
        return fn(mapWidget(), opened, state)
    end)
end

local function closeMap(opened)
    if opened and mapWidget() then Ext.Input.InjectKeyPress("ESCAPE") end
end

-- check_all S11 (19:11:17 before S066, 19:56:53 before S068: "screen map after closing the map (left open?)", S068
-- started on the map): done went out on the tick of the ESCAPE, and that ESCAPE was swallowed. Done only once the map
-- is gone: none after SV.MAP_CLOSE_MS -> one more ESCAPE; still up after another -> done with map_open = true (the
-- list itself is good; the caller closes it).
SV.MAP_CLOSE_MS = 1500
RPC.register("ui.waypoints", function(args)
    local err = travelError()
    local out, closeT, escapes
    return withMap("ui.waypoints", 9000, function(w, opened, state)
        if not out then
            out = {}
            for _, wp in ipairs(mapWaypoints(w)) do
                out[#out + 1] = { region = wp.region, name = wp.name, off = wp.off }
            end
            closeMap(opened)
            closeT, escapes = RPC.now_ms(), opened and 1 or 0
        end
        local res = { waypoints = out, travel_blocked = err, map_presses = state.mapPresses, map_escapes = escapes }
        if not opened or not mapWidget() then return "done", res end
        if RPC.now_ms() - closeT < SV.MAP_CLOSE_MS then return nil end
        if escapes >= 2 then
            res.map_open = true
            return "done", res
        end
        Ext.Input.InjectKeyPress("ESCAPE")
        closeT, escapes = RPC.now_ms(), escapes + 1
        return nil
    end)
end)

-- screenshot(map=true) (S055 x2, S056 x3: M, screenshot, ESCAPE by hand; S056 07:17 a 2nd M closed the map and the
-- ESCAPE after it was refused): open the map like ui.waypoints does and leave it open for the picture;
-- ui.map_close closes it again (only if we opened it: the bridge passes that back).
RPC.register("ui.map_show", function(args)
    return withMap("ui.map_show", 8000, function(w, opened, state)
        return "done", { opened = opened, map_presses = state.mapPresses }
    end)
end)

RPC.register("ui.map_close", function(args)
    local had = mapWidget() ~= nil
    closeMap(true)
    return { closed = had }
end)

-- "Waypoint: Roadside-Cliffs" -> "roadside cliffs"
local function wpNorm(s)
    s = tostring(s or ""):lower():gsub("^%s*waypoint%s*[:%-]?%s*", ""):gsub("^wayp_", "")
    return (s:gsub("[^%w]+", " "):gsub("^%s+", ""):gsub("%s+$", ""))
end

-- WAYP_CHA_Top (get_map's id) -> its display name, when the client can read it.
local function wpDisplayName(id)
    if not tostring(id):match("^WAYP_") then return nil end
    local L = Ext.Loca
    local h = L and try(L.GetTranslatedStringKey, id)
    local name = h and (U.loca and U.loca(tostring(h)) or try(L.GetTranslatedString, tostring(h)))
    return (name and name ~= "") and name or nil
end

-- Waypoint by name, forgiving like a player reading the map: case, a
-- "Waypoint:" prefix, punctuation, partial words ("roadside", "cliffs"),
-- get_map ids (WAYP_CHA_Top). Returns wp, or nil + number of matches.
function SV.pickWaypoint(list, want)
    local names = { wpNorm(want) }
    local disp = wpDisplayName(want)
    if disp then table.insert(names, 1, wpNorm(disp)) end
    local function unique(pred)
        local hit, n = nil, 0
        for _, wp in ipairs(list) do
            if pred(wpNorm(wp.name)) then hit, n = wp, n + 1 end
        end
        return hit, n
    end
    local best = 0
    for _, w in ipairs(names) do
        if w ~= "" then
            local words = {}
            for x in w:gmatch("%S+") do words[#words + 1] = x end
            local tiers = {
                function(n) return n == w end,
                function(n) return n:find(w, 1, true) ~= nil or (#n >= 4 and w:find(n, 1, true) ~= nil) end,
                function(n)  -- every word of want starts a word of the name
                    for _, x in ipairs(words) do
                        if not (" " .. n):find(" " .. x, 1, true) then return false end
                    end
                    return true
                end,
            }
            for _, t in ipairs(tiers) do
                local hit, n = unique(t)
                if n == 1 then return hit end
                if n > 1 then return nil, n end
            end
            -- most shared words (≥3 letters), if one waypoint wins outright
            local top, topN, tie = nil, 0, false
            for _, wp in ipairs(list) do
                local n = " " .. wpNorm(wp.name) .. " "
                local c = 0
                for _, x in ipairs(words) do
                    if #x >= 3 and n:find(" " .. x, 1, true) then c = c + 1 end
                end
                if c > topN then top, topN, tie = wp, c, false elseif c == topN and c > 0 then tie = true end
            end
            if top and not tie then return top end
            if tie then best = math.max(best, 2) end
        end
    end
    return nil, best
end
local pickWaypoint = SV.pickWaypoint

-- "Overgrown Ruins (Wilderness), Roadside Cliffs (Wilderness)"
local function waypointNames(list)
    local out = {}
    for _, x in ipairs(list) do
        out[#out + 1] = tostring(x.name) .. (x.region and (" (" .. tostring(x.region) .. ")") or "")
    end
    return table.concat(out, ", ")
end
SV.waypointNames = waypointNames

-- What blocks travel right now (camp leave / fast travel), read like the map's greyed-out button:
-- {error = TravelError or nil, in_camp, dialogue, screen}. Live 2026-10-10 (LIVETEST-setup, a save taken
-- right after a long rest): camp leave failed "cannot travel right now" with no reason given.
RPC.register("ui.travel_block", function(args)
    local dc = hotbarDC()
    return {
        error = travelError(dc),
        in_camp = selected(dc, "PlayerCharacterProperties.IsInCamp") == true,
        night_camp = nightCamp(dc) or nil,
        hud = visibleWidget("HotBar") ~= nil,   -- false during the morning transition after a rest (live 11:3x)
        dialogue = visibleWidget("Dialogue") ~= nil,
        screen = screenNow(),
    }
end)

-- Questions fast_travel must never answer itself: resting/camp decisions (vis live 2026-10-10: a rest prompt was
-- answered during a fast travel). Box texts may read "[ForceUpdate]" (formatter-driven): those are answered as
-- before but listed by name in the result.
function SV.travelVeto(q)
    local t = tostring(q or ""):lower()
    for _, w in ipairs({ "rest", "sleep", "end the day", "camp", "supplies" }) do
        if t:find("%f[%a]" .. w .. "%f[%A]") then return "a rest/camp decision is yours, not fast_travel's" end
    end
    return nil
end

-- Right after a load (live 2026-10-10, LIVETEST-se / -setup at the red-forest camp) TravelError reads
-- "Unavailable" for a few seconds and then clears: wait it out instead of failing the call.
SV.FT_UNAVAILABLE_WAIT_MS = 8000

RPC.register("ui.fast_travel", function(args)
    local want = args.waypoint or error("waypoint required")
    local dc = hotbarDC()
    local err = travelError(dc)
    if err and err ~= "Unavailable" then error("can't fast travel now (" .. err .. ")", 0) end
    if err and nightCamp(dc) then
        error("can't fast travel now (Unavailable): the night at camp hasn't ended (a long rest that didn't "
            .. "finish): long_rest goes to sleep and ends it", 0)
    end
    if selected(dc, "CharacterMode") == "InCombat" then error("can't fast travel in combat", 0) end
    -- Live 2026-10-10: a fast travel from camp moved only the controlled character (companions and
    -- the camera stayed at the Campsite, PARTY APART 1800 m). The bridge leaves camp first.
    if selected(dc, "PlayerCharacterProperties.IsInCamp") == true and not args.from_camp then
        error("at camp: leave camp first (camp leave), then fast travel", 0)
    end
    if args.dry then return { can = true, waiting = err } end

    local startPos
    local function pre(job, state)
        local e = travelError()
        if e == nil then
            startPos = playerPos()
            state.unavailableMs = err and (RPC.now_ms() - job.started) or nil
            return true
        end
        if e ~= "Unavailable" then return "failed", nil, "can't fast travel now (" .. e .. ")" end
        if RPC.now_ms() - job.started >= SV.FT_UNAVAILABLE_WAIT_MS then
            return "failed", { travel_blocked = e }, "can't fast travel now (Unavailable for "
                .. string.format("%.0f", SV.FT_UNAVAILABLE_WAIT_MS / 1000) .. " s: the game blocks travel here; "
                .. "walk (travel_to) or try again after moving)"
        end
        return nil
    end
    return withMap("ui.fast_travel", 58000 + SV.FT_UNAVAILABLE_WAIT_MS, function(w, opened, state)
        if not state.sent then
            local list = mapWaypoints(w)
            -- The list fills a few frames after the map opens (live S007: an empty
            -- list right after M -> "no unlocked waypoint named 'Roadside Cliffs'").
            state.listT = state.listT or RPC.now_ms()
            if #list == 0 and RPC.now_ms() - state.listT < 2000 then return nil end
            local wp, n = pickWaypoint(list, want)
            if not wp then
                closeMap(opened)
                local names = {}
                for _, x in ipairs(list) do names[#names + 1] = x.name end
                local msg
                if #list == 0 then
                    msg = "the map shows no waypoint list (UI not loaded); try again, or walk (travel_to marker/objective)"
                elseif n and n > 1 then
                    msg = "'" .. want .. "' matches " .. n .. " waypoints; be more specific. Unlocked: " .. waypointNames(list)
                else
                    msg = "no unlocked waypoint named '" .. want .. "'. Unlocked: " .. waypointNames(list)
                end
                return "failed", { waypoints = names }, msg
            end
            local cmd = wp.button and UI.prop(wp.button, "Command")
            local vm = wp.button and try(function() return wp.button.DataContext end)
            local ok, e2 = UI.execute(cmd, vm, false)
            if not ok then
                closeMap(opened)
                return "failed", nil, "couldn't travel to " .. wp.name .. ": " .. tostring(e2)
            end
            state.sent, state.target, state.t = true, wp, RPC.now_ms()
            return nil
        end
        local c, cres, cerr = handleConfirm(state, args, nil, SV.travelVeto)
        if c == "failed" then
            if not (cres and cres.needs_click) then closeMap(opened) end
            return "failed", cres, cerr
        end
        if c then state.t = RPC.now_ms() return nil end
        -- Arrived once the party moved and the game is running again.
        local gs = tostring(try(Ext.Utils.GetGameState) or "?")
        if gs ~= "Running" then state.loading = true return nil end
        local pos = playerPos()
        local moved = startPos and pos and U.dist(startPos, pos) > 3
        if moved or (state.loading and RPC.now_ms() - state.t > 2000) then
            if mapWidget() then Ext.Input.InjectKeyPress("ESCAPE") end
            return "done", { arrived = state.target.name, region = state.target.region, confirmed = state.confirms,
                waited_unavailable_ms = state.unavailableMs }
        end
        local te = travelError()
        if te and RPC.now_ms() - state.t > 1000 then
            closeMap(opened)
            return "failed", nil, "travel refused (" .. te .. ")"
        end
        return nil
    end, pre)
end)

------------------------------------------------------------------------
-- Donate (Gale's magic-item hunger)
------------------------------------------------------------------------
-- ui.donate {item=uuid, dry?} -> job {donated=name}
-- The "Donate" page (Donate.xaml, DC ls.DCDonate) opens from Gale's "Give Gale a magic artefact" answer. One
-- ListBox "Inventory" per party member (ItemsSource = that member's sorted VMInventorySlots); selecting a slot
-- copies the ListBox's SelectedItems into InventoryContainer.Tag, and DonateBtn runs DonateItemsCommand(Tag).
-- So: match the item's VM slot (VM <-> ECS by slot index + weight, as trade does), select it in its ListBox
-- (what a click does), then press DonateBtn. S020 04:45 / S023 06:14: 5-6 hand clicks + screenshots each.
-- NOT tested live (the window only opens from Gale's story dialogue).
local function donateGroups(dc)
    local vms, seen = {}, {}
    for _, src in UI.items(UI.prop(dc, "DonateSourceInventories"), 8) do
        collectInventories(src, vms, seen, 0)
    end
    local pc, groups = partyInventories(), {}
    for _, vm in ipairs(vms) do
        local inv, slots = bestInventory(vm, pc)
        groups[#groups + 1] = { vm = vm, inv = inv, slots = slots }
    end
    return groups
end

-- The visible "Inventory" ListBox holding vmItem and its 0-based index there.
local function donateSlot(w, vmItem)
    local key = tostring(vmItem)
    for _, lb in ipairs(UI.findAll(w, function(e) return UI.name(e) == "Inventory" end, { prune = true })) do
        local coll = UI.get(lb, "Items") or UI.get(lb, "ItemsSource")
        for i, slot in UI.items(coll, 400) do
            local obj = slot and UI.get(slot, "Object")
            if obj ~= nil and tostring(obj) == key then return lb, i - 1 end
        end
    end
    return nil
end
SV.donateSlot = donateSlot

------------------------------------------------------------------------
-- Pickpocket window (Pickpocket.xaml): the NPC's pockets (Container.Inventory, ListBox "inventoryListBox"),
-- and for the selected item the game's own odds: Roll.Chance (0..1, the pie; -1 = none) and Roll.DifficultyCheck
-- (the number in the target ring). Selecting an item is the player's click on it; StealBtn (TakeItemCommand,
-- enabled by CanSteal) rolls Sleight of Hand: Roll.Finished, Roll.Success, Roll.RolledNumber.
------------------------------------------------------------------------

local function ppRoll(dc)
    local function num(path) return tonumber(UI.path(dc, path)) end
    local chance = num("Roll.Chance") or num("Chance")
    return { chance = chance and chance >= 0 and math.floor(chance * 100 + 0.5) or nil,
             dc = num("Roll.DifficultyCheck"), rolled = num("Roll.RolledNumber"),
             finished = UI.path(dc, "Roll.Finished") == true, success = UI.path(dc, "Roll.Success") == true,
             can_steal = UI.prop(dc, "CanSteal") ~= false }
end

-- {lb, rows = {{i (0-based ListBox index), view}}} for the shown pockets.
local function ppRows(w, dc, npc)
    local lb = UI.byName(w, "inventoryListBox")
    if not lb then return nil, "the pickpocket window has no item list" end
    local vmInv = UI.path(dc, "Container.Inventory")
    local owner = entity(npc)
    local inv = owner and vmInv and bestInventory(vmInv, inventoriesOf(owner)) or nil
    local rows = {}
    -- The ListBox lists every slot (empty cells too: the grid), in Container.Inventory.Slots order, so a slot's
    -- position there is its ListBox index. Read the Slots themselves; the ListBox's own Items as a fallback.
    local coll = vmInv and UI.prop(vmInv, "Slots")
    if UI.count(coll) == 0 then coll = UI.get(lb, "Items") or UI.get(lb, "ItemsSource") end
    for i, slot in UI.items(coll, 200) do
        local obj = slot and UI.get(slot, "Object")
        if obj ~= nil then
            -- itemView drops gold (trade lists it apart); here gold is a pocket like any other.
            local v = itemView({ index = tonumber(UI.get(slot, "Index")), item = obj }, inv)
                or { name = "Gold", count = tonumber(UI.get(obj, "Count")) }
            rows[#rows + 1] = { i = i - 1, view = v }
        end
    end
    -- Diagnostics for an empty list: occupied slots of the pockets VM (unfiltered) and whether names matched.
    return { lb = lb, rows = rows, slots = vmInv and #vmSlots(vmInv) or nil, named = inv ~= nil }
end

local function ppMatch(v, want)
    if v.uuid and v.uuid == want then return true end
    local n, w = (v.name or ""):lower(), tostring(want):lower()
    return n ~= "" and (n == w or n:find(w, 1, true) ~= nil)
end

-- ui.pickpocket {npc?, take? = item name or uuid, min_chance? (percent), max? = 20}
-- Without take: every item with the shown chance/DC (each selected in turn, as a player clicks through them).
-- With take: selects it, refuses below min_chance, presses Steal and reports the roll.
RPC.register("ui.pickpocket", function(args)
    local w = visibleWidget("Pickpocket") or error("no pickpocket window is open: pickpocket(npc) opens it", 0)
    local dc = try(function() return w.DataContext end) or error("the pickpocket window has no DataContext", 0)
    if args.leave then
        -- The window's Close button (CloseWidgetCommand, no parameter).
        local ok, err = UI.command(dc, "CloseWidgetCommand", nil)
        if not ok then error("couldn't close the pickpocket window: " .. tostring(err), 0) end
        return { left = true }
    end
    local got, err = memoized(function() return ppRows(w, dc, args.npc) end)
    if not got then error(err, 0) end
    local rows, lb = got.rows, got.lb
    if args.take then
        local row
        for _, r in ipairs(rows) do if ppMatch(r.view, args.take) then row = r break end end
        if not row then
            local names = {}
            for _, r in ipairs(rows) do names[#names + 1] = r.view.name or "?" end
            error("no '" .. tostring(args.take) .. "' in their pockets; they carry: " .. table.concat(names, ", "), 0)
        end
        rows = { row }
    end
    local max = tonumber(args.max) or 20
    local out, k, phase, t = {}, 0, "select", 0
    return waitJob("ui.pickpocket", 6000 + 200 * math.min(#rows, max), function(job)
        if phase == "select" then
            k = k + 1
            local r = rows[k]
            if r == nil or k > max then
                return "done", { items = out, more = #rows > max and (#rows - max) or nil, slots = got.slots,
                                 named = got.named }
            end
            local ok, serr = pcall(lb.SetProperty, lb, "SelectedIndex", r.i)
            if not ok then return "failed", nil, "couldn't select " .. tostring(r.view.name) .. ": " .. tostring(serr) end
            phase, t = "read", RPC.now_ms()
            return nil
        end
        if phase == "read" then
            if RPC.now_ms() - t < 100 then return nil end   -- SelectionChanged → SelectItemCommand fills Roll
            local r, roll = rows[k], ppRoll(dc)
            r.view.chance, r.view.dc = roll.chance, roll.dc
            out[#out + 1] = r.view
            if not args.take then phase = "select" return nil end
            if args.min_chance and roll.chance and roll.chance < tonumber(args.min_chance) then
                return "failed", { item = r.view }, string.format("%s: %d%% chance (DC %s) is below min_chance %s; nothing taken",
                    tostring(r.view.name), roll.chance, tostring(roll.dc), tostring(args.min_chance))
            end
            local btn = UI.byName(w, "StealBtn")
            if not btn then return "failed", nil, "the pickpocket window has no Steal button" end
            local ok, perr = UI.press(btn)
            if not ok then return "failed", { item = r.view }, "Steal didn't work: " .. tostring(perr) end
            phase, t = "roll", RPC.now_ms()
            return nil
        end
        if phase == "roll" then
            local roll = ppRoll(dc)
            if roll.finished then
                local v = rows[k].view
                return "done", { item = v, success = roll.success, rolled = roll.rolled, dc = roll.dc, chance = v.chance }
            end
            if RPC.now_ms() - t > 5000 then
                return "done", { item = rows[k].view, pending = true }
            end
            return nil
        end
    end)
end)

RPC.register("ui.donate", function(args)
    local w = visibleWidget("Donate") or error("no donation window is open: it opens from Gale's dialogue "
        .. "(answer 'Give Gale a magic artefact')", 0)
    local dc = try(function() return w.DataContext end) or error("the donation window has no DataContext", 0)
    local vmItem, err = memoized(function() return findVMItem(args.item, donateGroups(dc)) end)
    if not vmItem then error(((err or "item not found"):gsub("the trade screen", "the donation window")), 0) end
    local lb, idx = donateSlot(w, vmItem)
    if not lb then error("the item is not listed in the donation window (Gale only takes magic items)", 0) end
    local btn = UI.byName(w, "DonateBtn") or error("the donation window has no Donate button", 0)
    if args.dry then return { can = true, index = idx } end
    local phase, t = "select", 0
    return waitJob("ui.donate", 8000, function(job)
        if phase == "select" then
            local ok, serr = pcall(lb.SetProperty, lb, "SelectedIndex", idx)
            if not ok then return "failed", nil, "couldn't select the item: " .. tostring(serr) end
            phase, t = "press", RPC.now_ms()
            return nil
        end
        if phase == "press" then
            if RPC.now_ms() - t < 150 then return nil end   -- the SelectionChanged trigger fills the Tag
            local ok, perr = UI.press(btn)
            if not ok then return "failed", nil, "selected the item, but Donate didn't work: " .. tostring(perr) end
            phase, t = "wait", RPC.now_ms()
            return nil
        end
        if not visibleWidget("Donate") or RPC.now_ms() - t > 1500 then
            return "done", { donated = args.item, window_open = visibleWidget("Donate") ~= nil or nil }
        end
        return nil
    end)
end)

------------------------------------------------------------------------
-- Insert Item panels (Insert.xaml, ls:UIWidget "Insert", DC gui::DCCombine): pedestals, plaques, sockets
-- that take one item (Silent Library "Riddle of the Night": the story takes one book, any other book fires a
-- trap). The panel opens from interact on the object. DC: BaseItem (the object), Ingredients (the slots),
-- CurrentState (Ready enables the Insert button) / FailReason, StartActionCommand (the Insert button; the
-- controller page runs it with no parameter), ToggleItemCommand(VM item) (the controller page's "Add"/"Remove"
-- on an inventory item: what a mouse drag into a slot does, DropSlotCommand(slot index) needs a drag in
-- progress and an int parameter bg3le can't pass), CharacterInventory.Slots (the selected character's bag).
-- S031 17:12-17:14: ~25 hand clicks/screenshots, the agent gave up ("no tool can drag").
-- ui.insert {item=uuid?, dry?} -> list (no item) | job {inserted=name, state, fail_reason}
------------------------------------------------------------------------
local function insertVMItems(dc)
    -- {vm=VM item, uuid, name} for every item in the panel's inventory (CharacterInventory: the selected
    -- character's bag). Live 17:5x: its VM items carry no name/uuid (EntityHandle is an ls.EntityRef), so the
    -- entity comes from the slot index + weight, as trade matches them. Not collectInventories(dc): the DC is the
    -- whole player VM (a number-valued "Inventory"-like property raised in UI.prop).
    local out = {}
    local ci = UI.prop(dc, "CharacterInventory")
    if ci == nil then return out end
    local inv, slots = bestInventory(ci, partyInventories())
    local items = inv and containerItems(inv) or {}
    for _, s in ipairs(slots) do
        local e = s.index and items[s.index] or nil
        out[#out + 1] = { vm = s.item, uuid = e and uuidOf(e) or nil, name = e and nameOf(e) or nil,
                          selected = UI.get(s.item, "IsSelected") == true }
    end
    return out
end
SV.insertVMItems = insertVMItems

local function insertSlots(dc)
    local out = {}
    for _, x in UI.items(UI.prop(dc, "Ingredients"), 8) do
        out[#out + 1] = x ~= nil and (UI.get(x, "Name") or "?") or false
    end
    return out
end

local function insertView(dc)
    local bi = UI.prop(dc, "BaseItem")
    return { panel = bi and UI.get(bi, "Name") or nil, state = UI.prop(dc, "CurrentState"),
             fail_reason = UI.prop(dc, "FailReason"), slots = insertSlots(dc) }
end

local function insertMatch(it, want)
    if it.uuid and it.uuid == want then return true end
    local n, w = (it.name or ""):lower(), tostring(want or ""):lower()
    return w ~= "" and n ~= "" and (n == w or n:find(w, 1, true) ~= nil)
end

RPC.register("ui.insert", function(args)
    local w = visibleWidget("Insert") or error("no Insert Item panel is open: interact with the pedestal/socket opens it", 0)
    local dc = try(function() return w.DataContext end) or error("the Insert panel has no DataContext", 0)
    local items = memoized(insertVMItems, dc)
    local view = insertView(dc)
    if not args.item then
        local list = {}
        for _, it in ipairs(items) do list[#list + 1] = { uuid = it.uuid, name = it.name, selected = it.selected or nil } end
        view.items = list
        return view
    end
    local hit
    for _, it in ipairs(items) do if insertMatch(it, args.item) then hit = it break end end
    if not hit then
        local names = {}
        for _, it in ipairs(items) do names[#names + 1] = it.name or "?" end
        error("the Insert panel doesn't list that item (it shows the selected character's bag: "
            .. (#names > 0 and table.concat(names, ", ", 1, math.min(#names, 25)) or "nothing") .. ")", 0)
    end
    local ok, err = UI.command(dc, "ToggleItemCommand", hit.vm, true)
    if not ok and not hit.selected then error("the panel won't take " .. tostring(hit.name) .. ": " .. tostring(err), 0) end
    if args.dry then return { can = true, item = hit.name, panel = view.panel } end
    local phase, t, t0 = hit.selected and "ready" or "toggle", 0, RPC.now_ms()
    return waitJob("ui.insert", 10000, function(job)
        local now = RPC.now_ms()
        if not visibleWidget("Insert") then
            if phase == "pressed" then return "done", { inserted = hit.name, uuid = hit.uuid, state = job.last_state, closed = true } end
            return "failed", nil, "the Insert panel closed before Insert was pressed"
        end
        local st = UI.prop(dc, "CurrentState")
        job.last_state = st
        if phase == "toggle" then
            local okt, terr = UI.command(dc, "ToggleItemCommand", hit.vm)
            if not okt then return "failed", nil, "couldn't put " .. tostring(hit.name) .. " in the slot: " .. tostring(terr) end
            phase, t = "ready", now
            return nil
        end
        if phase == "ready" then
            if st == "Ready" then
                local okp, perr = UI.command(dc, "StartActionCommand", nil)
                if not okp then
                    local btn = UI.byName(w, "ButtonInsert")
                    okp, perr = UI.press(btn)
                end
                if not okp then return "failed", nil, "the item is in the slot, but Insert didn't work: " .. tostring(perr) end
                phase, t = "pressed", now
                return nil
            end
            -- S037 20:23: the Verge altar has 3 gem slots; each of the first two gems sat in its slot (state
            -- Preparing, slots "?, empty, empty") and was reported "didn't make the panel ready" after 2.5 s.
            -- In its slot with empty ones left = a partial fill: done, the panel waits for the rest.
            if now - t > 400 and UI.get(hit.vm, "IsSelected") == true then
                local v = insertView(dc)
                local filled, empty = 0, 0
                for _, x in ipairs(v.slots) do if x then filled = filled + 1 else empty = empty + 1 end end
                if empty > 0 and filled > 0 then
                    return "done", { placed = hit.name, uuid = hit.uuid, state = st, filled = filled, slots_total = filled + empty,
                        panel_open = true }
                end
            end
            if now - t > 2500 then
                local v = insertView(dc)
                return "failed", nil, "the item didn't make the panel ready (state " .. tostring(st) .. ", fail "
                    .. tostring(v.fail_reason) .. ", slots " .. table.concat((function() local o = {} for _, x in ipairs(v.slots) do o[#o + 1] = x or "empty" end return o end)(), ", ") .. ")"
            end
            return nil
        end
        -- pressed: Success / Fail (FailReason), then the panel closes by itself or stays.
        if st == "Success" or st == "Fail" or now - t > 4000 then
            if st == "Fail" or now - t > 4000 then
                return "done", { inserted = st ~= "Fail" and hit.name or nil, item = hit.name, uuid = hit.uuid, state = st,
                    fail_reason = UI.prop(dc, "FailReason"), panel_open = true }
            end
            job.success_at = job.success_at or now
            if now - job.success_at > 1500 then
                return "done", { inserted = hit.name, uuid = hit.uuid, state = st, panel_open = true }
            end
        end
        return nil
    end)
end)

return SV
