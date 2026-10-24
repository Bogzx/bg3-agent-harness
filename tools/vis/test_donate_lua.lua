-- Offline tests for Gale's donation window (ui.donate in Client/ServicesItems.lua), from the live D8 probe
-- (runtime/probes/fix-live/donate/probe_donation_gloves.json / probe_donation_2.json, 2026-10-24, S023 save):
-- each DonateSourceInventories entry has 2 TradeInventories, a bag VM (empty: no magic item in any bag) and an
-- equipment VM (EquipmentFlags) whose slots are the worn items at their ItemSlot index: Claude's Sword of Justice
-- (3, 2.7 kg) and Gloves of Power (10, 0.5 kg), Shadowheart's Linebreaker Boots (9, 0.55 kg). The "Inventory"
-- ListBoxes' Items read as empty collections there; their ListBoxItems hold the slots.
-- feed_gale(Gloves of Power) FAILED "isn't on this side of the donation window": only bags were matched.
-- Live D10 (research/fix/live.md "Window D10"): selecting the cell filled InventoryContainer.Tag (1 item), but
-- UI.press(DonateBtn) gave nothing (CommandParameter nil); DonateItemsCommand(Tag) gave it (the cell left the window,
-- which stayed open). feed_gale had replied "done — donated=e132 window_open" with the gloves still worn.
-- Run: tools/vis/test_donate.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local clock = 1000

-- ECS: an entity is a table of components.
local function ent(uuid, name, weight)
    return { Uuid = { EntityUuid = uuid }, DisplayName = { NameKey = { Handle = { Handle = name } } },
             Data = { Weight = weight, StatsId = name } }
end
local function inventory(items)
    local recs = {}
    for slot, e in pairs(items) do
        recs[slot] = { Item = e }
    end
    local inv = { InventoryContainer = { Items = recs } }
    for slot, e in pairs(items) do e.InventoryMember = { Inventory = inv, EquipmentSlot = slot } end
    return inv
end

-- Bag slots 3 / 9 / 10 hold other items (probe bags), so a bag can't take the equipment VM's match.
local byUuid = {}
local function item(uuid, name, w) local e = ent(uuid, name, w); byUuid[uuid] = e; return e end
local claudeBag = inventory({ [0] = item("potion", "Potion of Healing", 100), [3] = item("jav", "Javelin", 900),
                              [10] = item("topaz", "Topaz", 10) })
local claudeWorn = inventory({ [3] = item("sword", "Sword of Justice", 2700), [10] = item("gloves", "Gloves of Power", 500) })
local shBag = inventory({ [9] = item("cup", "Tin Cup", 50) })
local shWorn = inventory({ [9] = item("boots", "Linebreaker Boots", 550) })
local function member(bag, worn)
    return { PartyMember = {}, InventoryOwner = { PrimaryInventory = bag, Inventories = { bag, worn } } }
end
local claude, shadowheart = member(claudeBag, claudeWorn), member(shBag, shWorn)

-- VM: plain tables; collections are {list=...}
local function coll(list) return { list = list, Count = #list } end
local function vmItem(w) return { Stats = { Weight = { Value = w } }, Count = 1, IsEquipment = true } end
local function vmInv(slots, n) -- slots: index -> vmItem, n cells
    local list = {}
    for i = 0, n - 1 do list[#list + 1] = { Index = i, Object = slots[i] } end
    return { Slots = coll(list) }
end
local sword, gloves, boots = vmItem(2.700000047683716), vmItem(0.5), vmItem(0.550000011920929)
local function source(worn) return { OwnerObject = {}, TradeInventories = coll({ vmInv({}, 0), vmInv(worn, 14) }) } end
local dc = { DonateSourceInventories = coll({ source({ [3] = sword, [10] = gloves }), source({}), source({}),
                                              source({ [9] = boots }), source({}) }) }

-- Widget tree: an "Inventory" ListBox per source (Items empty, as read live), its ListBoxItems = the shown slots.
-- Selecting a cell copies the selection into InventoryContainer.Tag (fillsTag=false: the trigger never ran).
local selected, fillsTag = {}, true
local box = { Name = "InventoryContainer", children = {} }
local function cell(slot)
    local c = { Type = "ls.LSListBoxItem", DataContext = slot, children = {} }
    c.SetProperty = function(self, k, v)
        if k == "IsSelected" and v then
            selected[#selected + 1] = self
            if fillsTag then box.Tag = coll({ slot }) end
        end
    end
    return c
end
local function listBox(slots)
    local lb = { Name = "Inventory", Type = "ListBox", Items = coll({}), children = {} }
    for _, s in ipairs(slots) do lb.children[#lb.children + 1] = cell(s) end
    return lb
end
local swordCell = { Index = 3, Object = sword }
local glovesCell = { Index = 10, Object = gloves }
local bootsCell = { Index = 9, Object = boots }
local lbClaude, lbSh = listBox({ swordCell, glovesCell }), listBox({ bootsCell })
local pressed = 0
local btn = { Name = "DonateBtn", children = {} }
local closeBtn = { Name = "CloseBtn", IsMouseOver = false, children = {} }
local widget = { DataContext = dc, visible = true, children = { closeBtn, box, lbClaude, listBox({}), lbSh, btn } }
-- DonateItemsCommand(Tag) gives the Tag's items: their cells leave the window, which stays open (D10).
local gives, given = true, {}
dc.DonateItemsCommand = {
    CanExecute = function() return true end,
    Execute = function(_, tag)
        given[#given + 1] = tag
        if not gives then return end
        for _, lb in ipairs({ lbClaude, lbSh }) do
            for i = #lb.children, 1, -1 do
                for _, s in ipairs(tag.list) do
                    if lb.children[i] and lb.children[i].DataContext == s then table.remove(lb.children, i) end
                end
            end
        end
        box.Tag = coll({})
    end,
}

Ext = {
    Utils = { MonotonicTime = function() return clock end },
    Entity = {
        GetAllEntitiesWithComponent = function(c)
            if c == "PartyMember" then return { claude, shadowheart } end
            return {}
        end,
        Get = function(u) return byUuid[u] end,
    },
    Stats = { Get = function() return nil end },
    IO = { LoadFile = function() return nil end },
    Require = function(p) return dofile(LUA .. "/" .. p) end, -- Services.lua loads its parts
    Json = { Parse = function() return {} end },
}
local jobs, ops = {}, {}
local function findAll(el, pred, opts)
    local out = {}
    local function rec(e)
        if pred(e) then
            out[#out + 1] = e
            if opts and opts.prune then return end
        end
        for _, c in ipairs(e.children or {}) do rec(c) end
    end
    rec(el)
    return out
end
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
        widget = function(n) return n == "Donate" and widget or nil end,
        isVisible = function(w) return w.visible end,
        prop = function(o, k) if type(o) == "table" then return o[k] end end,
        get = function(o, k) if type(o) == "table" then return o[k] end end,
        pick = function(o, names) local out = {}; for _, k in ipairs(names) do out[k] = o[k] end; return out end,
        props = function(o) return type(o) == "table" and o or {} end,
        items = function(c, max)
            local i, n = 0, math.min(c and c.Count or 0, max or 1e9)
            return function() i = i + 1; if i <= n then return i, c.list[i] end end
        end,
        path = function(o, p) for part in p:gmatch("[^%.]+") do if type(o) ~= "table" then return nil end o = o[part] end return o end,
        name = function(e) return e.Name end,
        typeName = function(e) return type(e) == "table" and e.Type or nil end,
        findAll = findAll,
        byName = function(w, n) return findAll(w, function(e) return e.Name == n end)[1] end,
        -- D10: pressing DonateBtn ran its command with a nil parameter: nothing given
        press = function(b) if b == btn then pressed = pressed + 1 end return true end,
        command = function(o, name, param)
            local cmd = o[name]
            if not cmd then return false, "no command '" .. name .. "'" end
            cmd.Execute(cmd, param)
            return true
        end,
        count = function(c) return type(c) == "table" and c.Count or 0 end,
        cachedByName = function() return nil end,
        anyDC = function() return dc end,
    },
    UIState = { SCREENS = {} },
    Util = { comp = function(e, c) return type(e) == "table" and e[c] or nil end,
             loca = function(h) return h end, dist = function() return 0 end },
}
dofile(LUA .. "/Client/Services.lua")

local fails, passes = 0, 0
local function ok(cond, msg) if cond then passes = passes + 1 else fails = fails + 1; print("FAIL " .. msg) end end

-- a worn item matches its equipment VM slot (before: "isn't on this side of the donation window")
local okd, r = pcall(ops["ui.donate"], { item = "gloves", dry = true })
ok(okd and r and r.can == true, "worn Gloves of Power found: " .. tostring(okd and r and r.can or r))
okd, r = pcall(ops["ui.donate"], { item = "boots", dry = true })
ok(okd and r and r.can == true, "Shadowheart's worn boots found: " .. tostring(okd and r and r.can or r))
-- a bag item the window doesn't offer still says so (no VM slot holds it)
okd, r = pcall(ops["ui.donate"], { item = "potion", dry = true })
ok(not okd and tostring(r):find("isn't on this side of the donation window", 1, true) ~= nil,
   "unlisted bag item refused: " .. tostring(r))

local function run(args)
    local job = jobs[ops["ui.donate"](args).job]
    for _ = 1, 200 do
        clock = clock + 50
        if job.status ~= "running" then break end
        job.on_tick(job)
    end
    return job
end

-- the item's cell staying in the window after Donate is not "done" (D10: "done — donated=e132 window_open")
selected, pressed, gives, given = {}, 0, false, {}
local job = run({ item = "gloves" })
ok(job.status == "failed" and tostring(job.error):find("still in the donation window", 1, true) ~= nil,
   "cell still listed after Donate -> failed: " .. tostring(job.status) .. " " .. tostring(job.error))

-- a selection that never reaches the Tag gives nothing and says so
selected, given, fillsTag, box.Tag = {}, {}, false, coll({})
job = run({ item = "gloves" })
ok(job.status == "failed" and tostring(job.error):find("didn't take the selection", 1, true) ~= nil,
   "empty Tag -> failed: " .. tostring(job.error))
ok(#given == 0, "nothing donated with an empty Tag")

-- the job selects the gloves' ListBoxItem (not the sword's), then runs DonateItemsCommand(Tag), not the button
selected, pressed, gives, given, fillsTag = {}, 0, true, {}, true
job = run({ item = "gloves" })
ok(job.status == "done", "donate done: " .. tostring(job.status) .. " " .. tostring(job.error))
ok(#selected == 1 and selected[1].DataContext == glovesCell, "the gloves' cell selected, once")
ok(pressed == 0, "DonateBtn not pressed (its parameter reads nil): " .. pressed)
ok(#given == 1 and given[1].list[1] == glovesCell, "DonateItemsCommand ran with the Tag holding the gloves")
ok(job.result and job.result.window_open == true, "the window still open is reported (Gale takes it on close)")

-- the close button probe for the bridge's hover-checked click
local pr = ops["ui.donate_probe"]({})
ok(pr.open and pr.button and pr.over == false and pr.enabled, "probe: open, X found, not hovered")
closeBtn.IsMouseOver = true
ok(ops["ui.donate_probe"]({}).over == true, "probe: hovered X")
widget.visible = false
ok(ops["ui.donate_probe"]({}).open == false, "probe: closed window")

print(string.format("%d/%d passed", passes, passes + fails))
if fails > 0 then os.exit(1) end
