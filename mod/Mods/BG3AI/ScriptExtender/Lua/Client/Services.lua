-- Services UI (owner: worker "kb"): trade, short and long rest, fast travel.
--
-- Everything goes through the same ICommands the screens' buttons bind (XAML
-- in Game.pak: Mods/MainUI/GUI/Pages/Trade.xaml, MakeCamp.xaml, HotBar.xaml,
-- JournalMap.xaml), so prices, supplies, travel restrictions and rest rules
-- are the engine's. Every op takes dry=true to only check CanExecute.
--
--   ui.trade {npc?, offer?:[uuid], request?:[uuid], confirm?, dry?}  -> job
--   ui.short_rest {dry?, confirm?}                                    -> job
--   ui.long_rest {dry?, confirm?}                                     -> job
--     confirm=false: stop at a game question ("End the day?") instead of
--     answering Yes; result.confirmed lists the questions answered.
--   ui.waypoints {}                                                   -> job (opens/closes the map)
--   ui.fast_travel {waypoint, dry?}                                   -> job
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
local function containerItems(inv)
    local out = {}
    local c = U.comp(inv, "InventoryContainer")
    if not c then return out end
    for slot, rec in pairs(c.Items or {}) do
        local it = try(function() return rec.Item end)
        if it then out[tonumber(slot)] = it end
    end
    return out
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
        local p = UI.props(slot)
        if p.Object ~= nil then out[#out + 1] = { index = tonumber(p.Index), item = p.Object } end
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
        local w = vmWeight(s.item)
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
local function traderCandidates()
    local t = entity(SV.trader)
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
local function tradeLayout(dc)
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
    local tc = traderCandidates()
    for _, vm in ipairs(traderVMs) do
        local inv, slots = bestInventory(vm, tc)
        layout.trader[#layout.trader + 1] = { vm = vm, inv = inv, slots = slots }
    end
    return layout
end

local function itemView(slotRec, inv)
    local item = slotRec.item
    local p = UI.props(item)
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
    if p.ItemType == "Gold" then return nil end
    return v
end

local function offerView(vmOffer)
    local out = {}
    for _, s in ipairs(vmSlots(vmOffer, 60)) do
        local p = UI.props(s.item)
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
    for _, it in ipairs(items) do t = t + (it.price or 0) * (it.count or 1) end
    return t
end

-- Everything the trade screen shows, as data.
function SV.tradeState(dc, layout, max)
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
    if not wantsChange then return SV.tradeState(dc) end

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
            local p = UI.props(live)
            local st = SV.tradeState(live)
            st.moved = moved
            if p.TradeDeclined == true then
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

RPC.register("ui.long_rest", function(args)
    local dc = hotbarDC()
    if selected(dc, "PlayerCharacterProperties.IsInCamp") ~= true then
        error("not at camp: travel to camp first (the camp tool), then long rest", 0)
    end
    local required = tonumber(UI.path(dc, "CurrentPlayer.RequiredPartySupplies"))
    if not visibleWidget("MakeCamp") then
        check(UI.command(dc, "CampTravel", nil, args.dry))
    end
    if args.dry then return { can = true, supplies_required = required } end

    local phase, sawActive, used, phaseStart = "open", false, nil, RPC.now_ms()
    return waitJob("ui.long_rest", 85000, function(job)
        local w = visibleWidget("MakeCamp")
        -- "End the day?" comes before the rest screen opens; others may follow it.
        if phase ~= "resting" then
            local c, cres, err = handleConfirm(job, args)
            if c == "failed" then return "failed", cres or { confirmed = job.confirms }, err end
            if c then
                if phase == "open" then phaseStart = RPC.now_ms() end
                return nil
            end
        end
        if phase == "open" then
            if not w then
                if RPC.now_ms() - phaseStart > 5000 then
                    return "failed", nil, "the rest screen didn't open" .. (popupText() and (": " .. popupText()) or "")
                end
                return nil
            end
            local mdc = w.DataContext
            local sv = UI.byName(w, "SuppliesView", { hidden = true })
            local sel = sv and UI.prop(sv, "SelectedItems") or nil
            if not isNoesis(sel) then sel = nil end
            local ok, err = UI.command(mdc, "AutoSelectCommand", sel, false)
            if not ok then return "failed", nil, "Auto-Select failed: " .. tostring(err) end
            phase, phaseStart = "selected", RPC.now_ms()
            return nil
        end
        if phase == "selected" then
            if not w then return "failed", nil, "the rest screen closed before resting" end
            if RPC.now_ms() - phaseStart < 250 then return nil end
            local mdc = w.DataContext
            used = tonumber(UI.prop(mdc, "SelectedSuppliesAmount"))
            required = tonumber(UI.path(mdc, "CurrentPlayer.RequiredPartySupplies")) or required
            local ok, err = UI.command(mdc, "ProcessRestCommand", nil, false)
            if not ok then return "failed", nil, "couldn't start the rest: " .. tostring(err) end
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
            result.note = "not enough camp supplies: partial rest (half HP and spell slots, no short rests restored)"
        end
        if visibleWidget("Dialogue") then
            result.camp_scene = true
            return "done", result
        end
        if not w and (sawActive and not active or RPC.now_ms() - phaseStart > 20000) then
            return "done", result
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

-- Opens the map if needed, runs fn(mapWidget, openedByUs) on a later tick.
local function withMap(kind, timeout_ms, fn)
    local opened = false
    if not mapWidget() then
        Ext.Input.InjectKeyPress("M")
        opened = true
    end
    local t0 = RPC.now_ms()
    local state = {}
    return waitJob(kind, timeout_ms, function(job)
        if not state.ready then
            local w = mapWidget()
            if not w then
                if RPC.now_ms() - t0 > 3000 then return "failed", nil, "the map didn't open" end
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
    return withMap("ui.waypoints", 5000, function(w, opened)
        local out = {}
        for _, wp in ipairs(mapWaypoints(w)) do
            out[#out + 1] = { region = wp.region, name = wp.name, off = wp.off }
        end
        closeMap(opened)
        return "done", { waypoints = out, travel_blocked = err }
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

RPC.register("ui.fast_travel", function(args)
    local want = args.waypoint or error("waypoint required")
    local dc = hotbarDC()
    local err = travelError(dc)
    if err then error("can't fast travel now (" .. err .. ")", 0) end
    if selected(dc, "CharacterMode") == "InCombat" then error("can't fast travel in combat", 0) end
    if args.dry then return { can = true } end

    local startPos = playerPos()
    return withMap("ui.fast_travel", 58000, function(w, opened, state)
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
            return "done", { arrived = state.target.name, region = state.target.region, confirmed = state.confirms }
        end
        local te = travelError()
        if te and RPC.now_ms() - state.t > 1000 then
            closeMap(opened)
            return "failed", nil, "travel refused (" .. te .. ")"
        end
        return nil
    end)
end)

return SV
