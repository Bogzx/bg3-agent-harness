-- Services, part 4 of 4 (Services.lua loads the parts in order; split for the 1000-line cap):
-- donate, the pickpocket window and insert-item panels.
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local SV = BG3AI.Services
local I = SV.inner
local entity, try, findVMItem, uuidOf = I.entity, I.try, I.findVMItem, I.uuidOf
local visibleWidget, bestInventory, waitJob, vmSlots = I.visibleWidget, I.bestInventory, I.waitJob, I.vmSlots
local nameOf, partyInventories, memoized = I.nameOf, I.partyInventories, I.memoized
local containerItems, collectInventories, itemView = I.containerItems, I.collectInventories, I.itemView
local inventoriesOf = I.inventoriesOf

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
