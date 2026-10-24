-- Part of test_confirm.lua (split for the 1000-line cap): the insert-item panel, load_game, collapsed groups, ui.react.
-- test_confirm.lua runs it after its stubs and helpers, which it shares as globals.

-- Insert Item panel (ui.insert): lists the selected character's items, ToggleItemCommand(VM item) fills the
-- slot, StartActionCommand (no parameter) presses Insert, Success is done (S031 Silent Library pedestal).
do
    widgets = {}
    local saved = {}
    for _, k in ipairs({ "get", "prop", "props", "items", "count", "command" }) do saved[k] = BG3AI.UI[k] end
    -- VM items carry no name/uuid (live 17:5x): the entity comes from the party inventory slot (index 1, 2).
    local book = { IsSelected = false }
    local other = { IsSelected = false }
    local slots = { { Index = 1, Object = other }, { Index = 2, Object = book }, { Index = 3 } }
    local function ent(u, n) return { Uuid = { EntityUuid = u }, DisplayName = { NameKey = { Handle = { Handle = n } } } } end
    local INV = { InventoryContainer = { Items = { [1] = { Item = ent("uuid-other", "Sharran Initiate's Notes") },
                                                   [2] = { Item = ent("uuid-book", "Teachings of Loss: The Nightsinger") } } } }
    local P = { InventoryOwner = { PrimaryInventory = INV } }
    local _comp, _loca, _entity = BG3AI.Util.comp, BG3AI.Util.loca, Ext.Entity
    BG3AI.Util.comp = function(e, c) return type(e) == "table" and e[c] or nil end
    BG3AI.Util.loca = function(h) return h end
    Ext.Entity = { GetAllEntitiesWithComponent = function() return { P } end, Get = function() return nil end }
    local dc = { CurrentState = "Empty", Ingredients = { false }, BaseItem = { Name = "Riddle of the Night" } }
    dc.CharacterInventory = { Slots = slots }
    local cmds = {}
    BG3AI.UI.get = function(o, k) if type(o) == "table" then local v = o[k] if v == false then return nil end return v end end
    BG3AI.UI.prop = BG3AI.UI.get
    BG3AI.UI.props = function(o) return type(o) == "table" and o or {} end
    BG3AI.UI.count = function(c) return type(c) == "table" and #c or 0 end
    BG3AI.UI.items = function(c, max)
        local i = 0
        return function()
            i = i + 1
            if type(c) ~= "table" or i > #c or (max and i > max) then return nil end
            local v = c[i]
            if v == false then v = nil end
            return i, v
        end
    end
    BG3AI.UI.command = function(o, name, param, dry)
        if o ~= dc then return false, "wrong object" end
        if dry then return true end
        cmds[#cmds + 1] = name
        if name == "ToggleItemCommand" then
            param.IsSelected = true
            if dc.multi then  -- a 3-slot altar: the first empty slot takes it, the panel waits for the rest
                for i = 1, #dc.Ingredients do if dc.Ingredients[i] == false then dc.Ingredients[i] = param break end end
                dc.CurrentState = "Preparing"
                return true
            end
            dc.Ingredients = { param }
            dc.CurrentState = "Ready"
            return true
        end
        if name == "StartActionCommand" then
            if param ~= nil then return false, "param" end
            dc.CurrentState = dc.willFail and "Fail" or "Success"
            dc.FailReason = dc.willFail and "Invalid" or nil
            return true
        end
        return false, "no command '" .. name .. "'"
    end
    local okn, errn = pcall(ops["ui.insert"], {})
    ok(not okn and tostring(errn):find("no Insert Item panel", 1, true), "insert: no panel → says how to open it: " .. tostring(errn))
    widgets.Insert = { name = "Insert", visible = true, DataContext = dc, buttons = {}, texts = {} }
    local okl, lst = pcall(ops["ui.insert"], {})
    ok(okl and lst.panel == "Riddle of the Night" and #lst.items == 2 and lst.items[2].name == "Teachings of Loss: The Nightsinger"
        and lst.items[2].uuid == "uuid-book" and lst.slots[1] == false, "insert: list names the panel, the items and the empty slot")
    local okm, errm = pcall(ops["ui.insert"], { item = "uuid-missing" })
    ok(not okm and tostring(errm):find("Sharran Initiate's Notes", 1, true), "insert: unknown item → the bag's names: " .. tostring(errm))
    local oki, ri = pcall(ops["ui.insert"], { item = "uuid-book" })
    ok(oki and ri.job ~= nil, "insert: a job")
    if oki and ri.job then
        clock = clock + 1
        local j = run(ri.job, 5000)
        ok(j.status == "done" and j.result.inserted == "Teachings of Loss: The Nightsinger" and j.result.state == "Success",
            "insert: done with Success: " .. tostring(j.status) .. " " .. tostring(j.error))
        ok(cmds[1] == "ToggleItemCommand" and cmds[2] == "StartActionCommand", "insert: toggle then Insert: " .. table.concat(cmds, ","))
    end
    -- a refused item: Fail + FailReason, reported (not "inserted")
    book.IsSelected, dc.CurrentState, dc.Ingredients, dc.willFail, cmds = false, "Empty", { false }, true, {}
    local okf, rf = pcall(ops["ui.insert"], { item = "Nightsinger" })
    if okf and rf.job then
        local j = run(rf.job, 5000)
        ok(j.status == "done" and j.result.inserted == nil and j.result.state == "Fail" and j.result.fail_reason == "Invalid",
            "insert: refused → Fail + reason, by name match")
    else
        ok(false, "insert by name: " .. tostring(rf))
    end
    -- S037 20:23: the Verge altar's 3 gem slots: a gem in its slot with slots left is a partial fill, not a failure
    book.IsSelected, dc.CurrentState, dc.Ingredients, dc.willFail, dc.multi, cmds = false, "Empty", { false, false, false }, nil, true, {}
    local okp, rp = pcall(ops["ui.insert"], { item = "uuid-book" })
    if okp and rp.job then
        local j = run(rp.job, 5000)
        ok(j.status == "done" and j.result.placed == "Teachings of Loss: The Nightsinger" and j.result.filled == 1
            and j.result.slots_total == 3 and j.result.inserted == nil,
            "insert: partial fill of a 3-slot panel → done, placed 1/3: " .. tostring(j.status) .. " " .. tostring(j.error))
        ok(cmds[#cmds] == "ToggleItemCommand", "insert: partial fill presses no Insert: " .. table.concat(cmds, ","))
    else
        ok(false, "insert partial: " .. tostring(rp))
    end
    dc.multi = nil
    for k, v in pairs(saved) do BG3AI.UI[k] = v end
    BG3AI.Util.comp, BG3AI.Util.loca, Ext.Entity = _comp, _loca, _entity
    widgets = {}
end

-- live window #1 (10-16 18:34): load_game right after the launch hit the loading screen ("no command
-- 'OpenLoadGameDialog' on ls.LoadingScreen"): the load job now waits for the loading screen, then opens the page
do
    local _cmd, _get = BG3AI.UI.command, BG3AI.UI.get
    local opened = 0
    BG3AI.UI.get = function(o, k) return type(o) == "table" and o[k] or nil end
    BG3AI.UI.command = function(dc, name, param, dry)
        if name == "OpenLoadGameDialog" and not dry then
            if widgets.root then return false, "no command 'OpenLoadGameDialog' on ls.LoadingScreen" end
            opened = opened + 1
            W("LoadGame", {}, {})
        end
        return true
    end
    local _le = A.LOAD_ENABLED
    A.LOAD_ENABLED = true
    widgets = {}
    W("root", {}, {})                       -- the loading screen (AutoSkip: widget "root", DC ls.LoadingScreen)
    ok(A.loadingScreenUp() == true, "loading screen seen")
    local r = ops["ui.menu"]({ action = "load", name = "Claude-4810126690__S027-start" })
    local j = run(r.job, 6000)
    ok(j.status == "running" and opened == 0 and j.partial and j.partial.waiting == "loading screen",
        "load waits while the loading screen is up (no press): " .. tostring(j.status) .. " " .. tostring(j.error))
    widgets.root = nil
    j = run(r.job, 1000)
    ok(opened == 1 and j.partial and j.partial.stage == "select", "loading screen gone -> load page opened, select stage")
    -- a loading screen that never goes: a clear failure, still no press
    widgets, opened = {}, 0
    W("root", {}, {})
    r = ops["ui.menu"]({ action = "load", name = "Claude-4810126690__S027-start" })
    j = run(r.job, 29000)
    ok(j.status == "failed" and opened == 0 and j.result and j.result.loading == true
        and tostring(j.error):find("loading screen stayed up", 1, true), "loading screen stuck -> failed loading=true: " .. tostring(j.error))
    -- the DC: never the loading screen's
    widgets = {}
    W("root", {}, {}).DataContext = { CurrentPlayer = {}, OpenLoadGameDialog = {} }
    W("HUD", {}, {}).DataContext = { CurrentPlayer = {}, OpenLoadGameDialog = { hud = true } }
    local _ws = BG3AI.UI.widgets
    BG3AI.UI.widgets = function()
        local out = _ws()
        for _, w in ipairs(out) do if w.name == "root" then w.dc = "ls.LoadingScreen" end end
        return out
    end
    local dc = A.commandDC("OpenLoadGameDialog")
    ok(dc and dc.OpenLoadGameDialog.hud == true, "commandDC skips the loading screen's DC")
    BG3AI.UI.widgets = _ws
    -- live D1/D6 (pressed while the client was in LoadMenu: the page stayed over the loaded game) and D5 (pressed
    -- 1 s into the launch's own load, which then loaded twice): no loading screen widget yet, the state says it
    widgets, opened = {}, 0
    local _gs = Ext.Utils.GetGameState
    local gs = "LoadMenu"
    Ext.Utils.GetGameState = function() return gs end
    r = ops["ui.menu"]({ action = "load", name = "Claude-4810126690__S027-start" })
    j = run(r.job, 3000)
    ok(j.status == "running" and opened == 0 and j.partial and j.partial.state == "LoadMenu",
        "load waits while the client is in LoadMenu (no press): " .. tostring(j.status) .. " " .. tostring(opened))
    gs = "StartLoading"
    j = run(r.job, 3000)
    ok(j.status == "running" and opened == 0, "and while a save loads by itself (StartLoading): no press")
    gs = "Menu"
    j = run(r.job, 1000)
    ok(opened == 1 and j.partial and j.partial.stage == "select", "main menu up -> load page opened, select stage")
    Ext.Utils.GetGameState = _gs
    widgets = {}
    A.LOAD_ENABLED, BG3AI.UI.command, BG3AI.UI.get = _le, _cmd, _get
end

do -- A13 S069 20:37:23 (Vicar Humbletoes, trader gold 0): request Mantle 819g + Potions x6 198g, confirm: FinishBarter
    -- went out before the Balance's gold was on our side -> refused, ESCAPE + reopen. The table's *OfferGold props are
    -- each side's shown total (S069 "1017 gold" = 819 + 198): the gold on the table is a Gold stack among the items.
    local UIt = BG3AI.UI
    local saved = {}
    for _, k in ipairs({ "get", "prop", "props", "items", "pick", "command", "widget", "isVisible", "cachedByName",
                         "typeName" }) do saved[k] = UIt[k] end
    local _entity = Ext.Entity
    Ext.Entity = { GetAllEntitiesWithComponent = function() return {} end, Get = function() return nil end }
    local function inv(...) local l = {} for i, it in ipairs({ ... }) do l[i] = { Index = i - 1, Object = it } end
        return { Slots = l } end
    local trade, tw, cmds, landAt, landWith
    local function setup(yours, theirs, traderGold)
        trade = { Mode = "Barter", CharacterOffer = inv(table.unpack(yours)), TraderOffer = inv(table.unpack(theirs)),
            TraderInventoryGold = traderGold, TradePartyGold = 1374, TradeButtonsEnabled = true }
        local function total(side) local t = 0 for _, sl in ipairs(side.Slots) do t = t + sl.Object.TradePrice end
            return t end
        trade.CharacterOfferGold, trade.TraderOfferGold = total(trade.CharacterOffer), total(trade.TraderOffer)
        tw, cmds, landAt, landWith = { DataContext = trade }, {}, nil, nil
    end
    local function land()  -- the Balance's gold reaches our side of the table landAt
        if landAt and clock >= landAt then
            trade.CharacterOffer = inv(landWith)
            trade.CharacterOfferGold = landWith.TradePrice
            landAt = nil
        end
    end
    UIt.get = function(o, k)
        if o == trade then land() end
        if type(o) == "table" then return o[k] end
        return nil
    end
    UIt.prop = UIt.get
    UIt.props = function(o) if o == trade then land() end return type(o) == "table" and o or {} end
    UIt.items = function(c, max)
        local i = 0
        return function()
            i = i + 1
            if type(c) ~= "table" or i > #c or (max and i > max) then return nil end
            return i, c[i]
        end
    end
    UIt.pick = function(o, names) local out = {} for _, n in ipairs(names) do out[n] = o[n] end return out end
    UIt.widget = function(n) if n == "Trade" then return tw end return saved.widget(n) end
    UIt.isVisible = function(el) if el == tw then return true end return saved.isVisible(el) end
    UIt.cachedByName = function() return nil end
    UIt.typeName = function() return nil end
    UIt.command = function(o, name, param, dry)
        if o ~= trade then return saved.command(o, name, param, dry) end
        if dry then return true end
        cmds[#cmds + 1] = name
        if name == "BalanceTrade" and landWith and not landAt then landAt = clock + 400 end
        if name == "FinishBarter" then
            land()
            if trade.CharacterOfferGold >= trade.TraderOfferGold then
                trade.CharacterOffer, trade.TraderOffer = inv(), inv()
            else
                trade.TradeDeclined = true
            end
        end
        return true
    end
    local function count(name) local n = 0 for _, c in ipairs(cmds) do if c == name then n = n + 1 end end return n end
    local mantle, pots = { Count = 1, TradePrice = 819 }, { Count = 6, TradePrice = 198 }
    local gold = { Count = 1017, TradePrice = 1017, ItemType = "Gold" }

    -- the gold lands 400 ms after the Balance: barter only then
    setup({}, { mantle, pots }, 0)
    landWith = gold
    local j = run(ops["ui.trade"]({ confirm = true }).job, 6000)
    ok(j.status == "done" and j.result and j.result.confirmed == true,
        "S069: confirm waits for the Balance's gold, then barters: " .. tostring(j.status) .. " " .. tostring(j.error))
    ok(count("FinishBarter") == 1 and not trade.TradeDeclined, "S069: one FinishBarter, not refused")

    -- the table's shown totals are not gold: 2 wares (1017g) and nothing of ours = short by 1017, not 2034
    setup({}, { mantle, pots }, 0)
    local st = ops["ui.trade"]({})
    ok(st.balance.you_give == 0 and st.balance.you_get == 1017 and st.offer.trader.gold == 0,
        "table: TraderOfferGold (the side's total) is not gold on the table: " .. tostring(st.balance.you_get))
    trade.CharacterOffer = inv(gold)
    st = ops["ui.trade"]({})
    ok(st.balance.you_give == 1017 and st.offer.you.gold == 1017 and st.offer.you.items[1].gold == true,
        "table: our gold is the Gold stack: " .. tostring(st.balance.you_give))

    -- the gold never lands: Balance pressed again on a table that stopped changing, never FinishBarter
    setup({}, { mantle, pots }, 0)
    j = run(ops["ui.trade"]({ confirm = true }).job, 6000)
    ok(j.status == "failed" and tostring(j.error):find("you would get 1017g more than you give", 1, true)
        and count("FinishBarter") == 0 and count("BalanceTrade") >= 2,
        "gold never on the table: not confirmed, no refusal to clear: " .. tostring(j.error) .. " B="
        .. count("BalanceTrade"))

    -- a 0-gold trader can't pay a difference: items only, say so (no gift unless donate)
    setup({ { Count = 1, TradePrice = 100 } }, { { Count = 1, TradePrice = 60 } }, 0)
    j = run(ops["ui.trade"]({ confirm = true }).job, 6000)
    ok(j.status == "failed" and tostring(j.error):find("you would give 40g more", 1, true)
        and tostring(j.error):find("items only", 1, true) and count("FinishBarter") == 0,
        "overpay to a 0-gold trader: refused before the barter: " .. tostring(j.error))
    setup({ { Count = 1, TradePrice = 100 } }, { { Count = 1, TradePrice = 60 } }, 0)
    j = run(ops["ui.trade"]({ confirm = true, donate = true }).job, 6000)
    ok(count("FinishBarter") == 1, "overpay on purpose (donate): bartered")
    -- selling to a 0-gold trader: the old message
    setup({ { Count = 1, TradePrice = 23 } }, {}, 0)
    j = run(ops["ui.trade"]({ confirm = true }).job, 6000)
    ok(j.status == "failed" and tostring(j.error):find("the trader has no gold left", 1, true)
        and count("FinishBarter") == 0, "sell to a 0-gold trader: " .. tostring(j.error))

    for k, f in pairs(saved) do UIt[k] = f end
    Ext.Entity = _entity
end

-- A15 (L7 22:08 helm item): an expanded group above hides the owner's: collapse the other expanded groups
do
    local function grp(hdr, expanded)
        local el = { expanded = expanded }
        el.SetProperty = function(self, k, v) if k == "IsExpanded" then self.expanded = v end end
        return { el = el, hdr = hdr }
    end
    local _prop = BG3AI.UI.prop
    BG3AI.UI.prop = function(e, k) if k == "IsExpanded" then return e.expanded end return _prop(e, k) end
    local groups = { grp("Claude", true), grp("Marek Dunhallow  Level 1", true), grp("Tamsin Drey", false) }
    local done = A.collapseOthers(groups, "marek dunhallow")
    ok(#done == 1 and done[1] == "Claude", "collapseOthers collapses only the other expanded group: "
        .. table.concat(done, ","))
    ok(groups[1].el.expanded == false and groups[2].el.expanded == true and groups[3].el.expanded == false,
        "collapseOthers: the owner's group stays expanded")
    ok(#A.collapseOthers(groups, "marek dunhallow") == 0, "collapseOthers: nothing left to collapse")
    BG3AI.UI.prop = _prop
end

-- ui.react (act A28): S091 08:11:01 / 08:12:16 two answers timed out after 10 s with the prompt gone from the screen:
-- the wait read the first page named ReactionDecisionPopup, left hidden with its events, not the shown one
do
    local UIt = BG3AI.UI
    local saved = { widgets = UIt.widgets, widget = UIt.widget, path = UIt.path, command = UIt.command, count = UIt.count,
                    widgetMap = UIt.widgetMap }
    local pages = {}
    local sent = {}
    local function popup(visible, events)
        return { name = "ReactionDecisionPopup", visible = visible, buttons = {},
                 DataContext = { dc = "reaction", Data = { InterruptEvents = events } } }
    end
    UIt.widgets = function()
        local out = {}
        for _, w in ipairs(pages) do out[#out + 1] = { name = w.name, el = w } end
        return out
    end
    UIt.widget = function(n) for _, w in ipairs(pages) do if w.name == n then return w end end end
    UIt.widgetMap = function()   -- Client/UI.lua: the shown page wins over a hidden one of the same name
        local m = {}
        for _, w in ipairs(pages) do
            local prev = m[w.name]
            if not prev or (prev.el.visible == false and w.visible ~= false) then m[w.name] = { name = w.name, el = w } end
        end
        return m
    end
    UIt.path = function(o, p)
        for part in p:gmatch("[^%.]+") do
            if type(o) ~= "table" then return nil end
            o = o[tonumber(part) or part]
        end
        return o
    end
    UIt.count = function(c) return type(c) == "table" and #c or 0 end
    UIt.command = function(dc, name, param) sent[#sent + 1] = { dc = dc, name = name, ev = param } return true end
    local stale = popup(false, { { id = "old" } })      -- an earlier prompt's page, hidden, its event still listed
    local shown = popup(true, { { id = "mm" } })
    pages = { stale, shown }
    local jr = run(ops["ui.react"]({ use = false }).job, 600)
    ok(sent[1] and sent[1].dc == shown.DataContext and sent[1].ev.id == "mm", "ui.react answers the shown prompt")
    ok(jr.status == "running", "ui.react waits while the shown prompt is up: " .. tostring(jr.status))
    shown.visible = false                               -- the game hides it; its event list stays as it was
    jr = run(jr.id, 2000)
    ok(jr.status == "done", "ui.react: the prompt left the screen -> done (was a 10 s TIMEOUT): " .. tostring(jr.status))
    pages = { stale }
    local okr, errr = pcall(ops["ui.react"], { use = true })
    ok(not okr and tostring(errr):find("no reaction prompt on screen", 1, true), "ui.react: only a hidden page -> none")

    -- S091/S092 Gale's Shield prompt, one row per level (I-VI), one interruptor: the commands never answered it, a
    -- click on the row did -> the row goes to the bridge (needs_click), no command is sent
    local function rows(n, roots)
        local b = {}
        for k = 1, roots or 1 do b[#b + 1] = { name = "InterruptorRoot" } end
        for k = 1, n do b[#b + 1] = { name = "ReactButton", k = k } end
        return b
    end
    local shield = popup(true, { { id = "shield" } })
    shield.buttons = rows(6)
    pages, sent = { shield }, {}
    r = ops["ui.react"]({ use = true, which = 3 })
    ok(r.needs_click == 3 and r.rows == 6 and r.job == nil and #sent == 0, "ui.react: multi-level prompt -> needs_click 3")
    r = ops["ui.react"]({ use = true })
    ok(r.needs_click == 1, "ui.react: multi-level prompt, no which -> row 1")
    okr, errr = pcall(ops["ui.react"], { use = true, which = 7 })
    ok(not okr and tostring(errr):find("no reaction option 7 (6 shown)", 1, true), "ui.react: no row 7")
    r = ops["ui.react"]({ use = false })
    ok(r.job and sent[1] and sent[1].name == "InterruptDontReactPlayerCommand", "ui.react use=false: the command as before")
    -- two interruptors with a row each (their Accept button shows): the command path as before
    shield.buttons, sent = rows(2, 2), {}
    r = ops["ui.react"]({ use = true, which = 2 })
    ok(r.job and r.needs_click == nil, "ui.react: two interruptors -> commands, no click")
    -- ui.react_probe: over the wanted row, over another, watch reads no rows
    shield.buttons = rows(6)
    shield.buttons[1 + 4].over = true                                       -- row 4 under the cursor
    r = ops["ui.react_probe"]({ which = 4 })
    ok(r.open and r.events == 1 and r.rows == 6 and r.over == true and r.over_row == nil, "react_probe: over row 4")
    r = ops["ui.react_probe"]({ which = 2 })
    ok(r.over == nil and r.over_row == 4, "react_probe: on row 4, row 2 wanted")
    r = ops["ui.react_probe"]({ watch = true })
    ok(r.open and r.rows == nil and r.over == nil, "react_probe watch: no row walk")
    shield.visible = false
    r = ops["ui.react_probe"]({ which = 4 })
    ok(r.open == false and r.events == 0, "react_probe: gone")
    for k, f in pairs(saved) do UIt[k] = f end
end
