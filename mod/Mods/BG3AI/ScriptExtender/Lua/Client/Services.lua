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
--   ui.waypoints {}                                                   -> job (opens/closes the map)
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
-- fail with the question so the agent answers with ui.confirm.
-- Returns nil (no box), true (handled: keep waiting) or "failed", nil, err.
local function handleConfirm(job, args, max)
    local A = BG3AI.UIActions
    if not (A and A.confirmBox) then return nil end
    local box = A.confirmBox()
    if not box then return nil end
    local labels = {}
    for _, b in ipairs(box.buttons) do labels[#labels + 1] = b.label end
    local q = box.text or box.title or "?"
    if args.confirm == false then
        return "failed", nil, "the game asks: " .. q .. " [" .. table.concat(labels, "/") .. "]; answer with confirm"
    end
    job.confirms = job.confirms or {}
    local now = RPC.now_ms()
    if now - (job.confirmAt or 0) < 700 then return true end -- the last press is still closing the box
    -- The same box still up after a press: the command press didn't land (live S013/S014: the rest
    -- flow's "[ForceUpdate] Yes/No" box ignores its button's Command, a mouse click works). Hand it
    -- to the bridge for a hover-checked click instead of pressing again (needs_click).
    local sig = q .. "#" .. table.concat(labels, "|")
    if job.confirmSig == sig then
        return "failed", { needs_click = "yes", confirmed = job.confirms },
            "the Yes press didn't close the game's question: " .. q .. " [" .. table.concat(labels, "/") .. "]"
    end
    if #job.confirms >= (max or 3) then
        return "failed", { confirmed = job.confirms }, "too many confirmations; still asking: " .. q .. " [" .. table.concat(labels, "/") .. "]"
    end
    local ok, view = A.answerConfirm("yes", false)
    job.confirmAt, job.confirmSig = now, sig
    if not ok then return "failed", { needs_click = "yes" }, "couldn't answer the game's question (" .. q .. "): " .. tostring(view) end
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
            you = { items = yours, gold = tonumber(p.CharacterOfferGold) or 0 },
            trader = { items = theirs, gold = tonumber(p.TraderOfferGold) or 0 },
        },
        -- What each side puts on the table at the shown prices (items + gold).
        balance = {
            you_give = sumPrices(yours) + (tonumber(p.CharacterOfferGold) or 0),
            you_get = sumPrices(theirs) + (tonumber(p.TraderOfferGold) or 0),
        },
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
    local phase, waitUntil = "mode", 0
    local moved, problems, confirmed = {}, {}, false
    if UI.prop(dc, "Mode") ~= "Barter" then
        check(UI.command(dc, "SetTradeMode", nil, false))
    end

    return waitJob("ui.trade", 15000, function()
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
            local function move(list, groups, side)
                for _, u in ipairs(list) do
                    local it, err, e = findVMItem(u, groups)
                    if it then
                        local ok, err2 = UI.command(live, "MoveTradeItem", it, false)
                        if ok then
                            SV.offered[tostring(it)] = { uuid = u, name = nameOf(e) }
                            moved[#moved + 1] = { uuid = u, name = nameOf(e), side = side }
                        else
                            problems[#problems + 1] = (nameOf(e) or u) .. ": " .. tostring(err2)
                        end
                    else
                        problems[#problems + 1] = err
                    end
                end
            end
            move(offer, layout.party, "you")
            move(request, layout.trader, "trader")
            phase, waitUntil = "moved", RPC.now_ms() + 250
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
            phase, waitUntil = "balanced", RPC.now_ms() + 250
            return nil
        end
        if phase == "balanced" then
            local param = UI.prop(live, "ActionCommandParameter")
            if not isNoesis(param) then param = nil end
            local ok, err = UI.command(live, "FinishBarter", param, false)
            if not ok then
                local st = SV.tradeState(live)
                st.moved = moved
                return "failed", st, "couldn't confirm the barter: " .. tostring(err)
            end
            phase, waitUntil = "finishing", RPC.now_ms() + 300
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
        error("short rest isn't available now (" .. tostring(before or "?") .. "/" .. tostring(max or "?")
            .. " short rests left; not possible in combat)", 0)
    end
    check(UI.command(dc, "ShortRest", nil, args.dry))
    if args.dry then return { can = true, short_rests_left = before } end
    return waitJob("ui.short_rest", 15000, function(job)
        local now = tonumber(selected(hotbarDC(), "Stats.ShortRestPointsResource.Value"))
        if before and now and now < before then
            return "done", { short_rests_left = now, of = max, confirmed = job.confirms }
        end
        local c, cres, err = handleConfirm(job, args)
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
    local names = {}
    for _, w in ipairs(UI.widgets()) do
        if #names >= 12 then names[#names + 1] = "..." break end
        if UI.isVisible(w.el) then names[#names + 1] = w.name end
    end
    return "widgets=[" .. table.concat(names, ", ") .. "]"
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
    local R = SV.rest
    local A = BG3AI.UIActions
    local presses, step, retried = 0, "open", false
    local function pressCamp()
        check(UI.command(hotbarDC(), "CampTravel", nil, false))
        presses, R.pressAt = presses + 1, RPC.now_ms()
    end
    local followup = R.boxAt and RPC.now_ms() - R.boxAt < REST_FOLLOWUP_MS
    if visibleWidget("MakeCamp") then
        step = "screen already open"
    elseif followup and not (A and A.confirmBox and A.confirmBox()) then
        step = "after the question was answered (waiting for the screen, CampTravel not pressed again)"
    else
        pressCamp()
        step = "after CampTravel"
    end

    local function openFail(what)
        local res = { step = "open", camp_travel_presses = presses }
        return "failed", res, "the rest screen didn't open (" .. what .. "; step: " .. step
            .. "; CampTravel pressed " .. presses .. "x in this call, last " .. sinceTxt(R.pressAt)
            .. "; last question box " .. sinceTxt(R.boxAt) .. "). On screen: " .. screenNow()
    end

    local phase, sawActive, used, phaseStart = "open", false, nil, RPC.now_ms()
    return waitJob("ui.long_rest", 85000, function(job)
        local w = visibleWidget("MakeCamp")
        -- "End the day?" comes before the rest screen opens; others may follow it, and
        -- a partial rest (too few supplies) may ask once more after Rest is pressed.
        if phase ~= "resting" or (w and not sawActive) then
            local c, cres, err = handleConfirm(job, args)
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
            if not w then
                if visibleWidget("Dialogue") and (job.confirms or followup) then
                    local st, res, err = openFail("a dialogue started instead (a camp scene): finish it, then long_rest again")
                    res.confirmed, res.camp_scene = job.confirms, true
                    return st, res, err
                end
                if RPC.now_ms() - phaseStart > REST_QUIET_MS then
                    if not retried then
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
            return "done", result
        end
        if not w and (sawActive and not active or RPC.now_ms() - phaseStart > 20000) then
            return "done", result
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
local MAP_PRESS_MS, MAP_PRESSES = 1200, 3
local function withMap(kind, timeout_ms, fn, pre)
    local state = { mapPresses = 0, mapKeys = {} }
    local function press()
        local k = visibleWidget("GameMenu") and "ESCAPE" or "M"
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
                if RPC.now_ms() - (state.mapPressT or 0) >= MAP_PRESS_MS then
                    if state.mapPresses >= MAP_PRESSES then
                        return "failed", { map_presses = state.mapPresses, keys = state.mapKeys },
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

RPC.register("ui.waypoints", function(args)
    local err = travelError()
    return withMap("ui.waypoints", 6000, function(w, opened, state)
        local out = {}
        for _, wp in ipairs(mapWaypoints(w)) do
            out[#out + 1] = { region = wp.region, name = wp.name, off = wp.off }
        end
        closeMap(opened)
        return "done", { waypoints = out, travel_blocked = err, map_presses = state.mapPresses }
    end)
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
        dialogue = visibleWidget("Dialogue") ~= nil,
        screen = screenNow(),
    }
end)

-- Right after a load (live 2026-10-10, LIVETEST-se / -setup at the red-forest camp) TravelError reads
-- "Unavailable" for a few seconds and then clears: wait it out instead of failing the call.
SV.FT_UNAVAILABLE_WAIT_MS = 8000

RPC.register("ui.fast_travel", function(args)
    local want = args.waypoint or error("waypoint required")
    local dc = hotbarDC()
    local err = travelError(dc)
    if err and err ~= "Unavailable" then error("can't fast travel now (" .. err .. ")", 0) end
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
        local c, cres, cerr = handleConfirm(state, args)
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

return SV
