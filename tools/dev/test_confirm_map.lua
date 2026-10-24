-- Part of test_confirm.lua (split for the 1000-line cap): waypoint names and the map, ui.roll, the stack-split box, a greyed-out long rest.
-- test_confirm.lua runs it after its stubs and helpers, which it shares as globals.

-- waypoint names (live S007: travel_to waypoint="Roadside Cliffs" / "WAYP_CHA_Top" failed)
local WPS = { { name = "Overgrown Ruins", region = "Wilderness" }, { name = "Roadside Cliffs", region = "Wilderness" },
    { name = "Emerald Grove Environs", region = "Wilderness" }, { name = "The Risen Road", region = "Wilderness" } }
local function pw(w) local r, n = BG3AI.Services.pickWaypoint(WPS, w) return r and r.name or ("nil:" .. tostring(n)) end
ok(pw("Roadside Cliffs") == "Roadside Cliffs", "exact")
ok(pw("roadside cliffs") == "Roadside Cliffs", "case")
ok(pw("Waypoint: Roadside Cliffs") == "Roadside Cliffs", "Waypoint: prefix")
ok(pw("Roadside-Cliffs!") == "Roadside Cliffs", "punctuation")
ok(pw("cliffs") == "Roadside Cliffs" and pw("grove") == "Emerald Grove Environs", "partial word")
ok(pw("Emerald Grove") == "Emerald Grove Environs", "prefix words")
ok(pw("Risen Road waypoint") == "The Risen Road", "extra word: " .. pw("Risen Road waypoint"))
ok(pw("Grove Environs gate") == "Emerald Grove Environs", "word overlap")
ok(pw("r") == "nil:3" or pw("r"):sub(1, 4) == "nil:", "ambiguous one letter: " .. pw("r"))
ok(pw("Blighted Village"):sub(1, 4) == "nil:", "unknown stays unknown")
Ext.Loca = { GetTranslatedStringKey = function(id) return id == "WAYP_CHA_Top" and "h123" or nil end,
    GetTranslatedString = function(h) return h == "h123" and "Roadside Cliffs" or "" end }
ok(pw("WAYP_CHA_Top") == "Roadside Cliffs", "get_map id via loca: " .. pw("WAYP_CHA_Top"))
ok(BG3AI.Services.waypointNames(WPS):find("Roadside Cliffs %(Wilderness%)"), "names listed with region")

-- fast_travel from camp moved only the controlled character (live 2026-10-10): refused, the bridge leaves camp
widgets = {}
inCamp = true
local okc, ec = pcall(ops["ui.fast_travel"], { waypoint = "Roadside Cliffs" })
ok(not okc and tostring(ec):find("at camp: leave camp first"), "at camp refused: " .. tostring(ec))
inCamp = false

-- fast_travel waits for the map's list to fill, then says so instead of "no waypoint named"
widgets = {}
W("JournalMap", {}, {})
Ext.Entity = { GetAllEntitiesWithComponent = function() return {} end }
local ftr = ops["ui.fast_travel"]({ waypoint = "Roadside Cliffs" })
local ftj = jobs[ftr.job]
clock = clock + 50 ftj.on_tick(ftj)
ok(ftj.status == "running", "waits while the list is empty")
for _ = 1, 50 do clock = clock + 50 ftj.on_tick(ftj) if ftj.status ~= "running" then break end end
ok(ftj.status == "failed" and tostring(ftj.error):find("no waypoint list"), "empty list explained: " .. tostring(ftj.error))

-- withMap (live S021 x7): after a successful travel the M toggle was a step out of phase,
-- so the next press was swallowed and fast_travel failed "the map didn't open"; the
-- following call found the map up. Now: no map 1.2 s after M -> press again.
local swallow, mapKeys = 0, {}
Ext.Input.InjectKeyPress = function(k)
    mapKeys[#mapKeys + 1] = k
    if k == "M" then
        if swallow > 0 then swallow = swallow - 1
        elseif widgets.JournalMap then widgets.JournalMap = nil
        else W("JournalMap", {}, {}) end
    elseif k == "ESCAPE" then
        if widgets.GameMenu then widgets.GameMenu = nil
        elseif widgets.JournalMap then widgets.JournalMap = nil
        else W("GameMenu", {}, {}) end
    end
end
widgets = {}
swallow = 1
local wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "done" and wj.result.map_presses == 2, "swallowed M -> pressed again, map opened: "
    .. tostring(wj.status) .. " " .. tostring(wj.error) .. " presses=" .. tostring(wj.result and wj.result.map_presses))
ok(widgets.JournalMap == nil, "ui.waypoints closed the map it opened")
widgets, mapKeys = {}, {}
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "done" and wj.result.map_presses == 1 and #mapKeys == 2, "normal open: one M, one ESCAPE ("
    .. table.concat(mapKeys, ",") .. ")")
-- a stray ESCAPE left the game menu up: M does nothing there, so close it first
widgets, mapKeys = {}, {}
W("GameMenu", {}, {})
local _inj = Ext.Input.InjectKeyPress
Ext.Input.InjectKeyPress = function(k) if k == "M" and widgets.GameMenu then mapKeys[#mapKeys + 1] = k return end _inj(k) end
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "done" and mapKeys[1] == "ESCAPE" and mapKeys[2] == "M", "game menu closed, then M: " .. table.concat(mapKeys, ","))
Ext.Input.InjectKeyPress = _inj
-- check_all S11 (19:11:17 / 19:56:53): the ESCAPE that closes the map was swallowed and ui.waypoints said done with the
-- map still up (S068 started on it). Done only once it is gone: one more ESCAPE after SV.MAP_CLOSE_MS.
do
    local escSwallow = 1
    Ext.Input.InjectKeyPress = function(k)
        if k == "ESCAPE" and escSwallow > 0 then mapKeys[#mapKeys + 1] = k escSwallow = escSwallow - 1 return end
        _inj(k)
    end
    widgets, mapKeys = {}, {}
    local t0 = clock
    wj = run(ops["ui.waypoints"]({}).job, 9000)
    ok(wj.status == "done" and widgets.JournalMap == nil and table.concat(mapKeys, ",") == "M,ESCAPE,ESCAPE"
        and wj.result.map_escapes == 2 and not wj.result.map_open and clock - t0 >= BG3AI.Services.MAP_CLOSE_MS,
        "swallowed ESCAPE: pressed again, done once the map closed: " .. tostring(wj.status) .. " keys="
        .. table.concat(mapKeys, ","))
    -- it never closes: two ESCAPEs (never a 3rd: that one would open the game menu), done with map_open
    escSwallow, widgets, mapKeys = 99, {}, {}
    wj = run(ops["ui.waypoints"]({}).job, 9000)
    ok(wj.status == "done" and wj.result.map_open == true and #wj.result.waypoints >= 0
        and table.concat(mapKeys, ",") == "M,ESCAPE,ESCAPE" and widgets.GameMenu == nil,
        "map stays up: 2 ESCAPEs, done with map_open: " .. tostring(wj.status) .. " keys=" .. table.concat(mapKeys, ","))
    Ext.Input.InjectKeyPress = _inj
    widgets, mapKeys = {}, {}
end
-- the map never opens: 3 presses, then a failure that says what is on screen
widgets, mapKeys = {}, {}
swallow = 99
W("Tooltip", {}, {})
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "failed" and #mapKeys == 5 and tostring(wj.error):find("pressed M,M,M,M,M over 6%.0s")
    and tostring(wj.error):find("Tooltip"), "never opens: " .. tostring(wj.error))
swallow = 0
-- S055 06:46:20: the Act 3 map shows ~2.5 s after M; the 1.2 s re-press closed it again ("pressed M,M,M,M,M").
-- A JournalMap that appeared after our press but isn't visible yet is opening: wait, one M only.
do
    widgets, mapKeys = {}, {}
    local shownAt
    local _inj2 = Ext.Input.InjectKeyPress
    Ext.Input.InjectKeyPress = function(k)
        mapKeys[#mapKeys + 1] = k
        if k == "M" then
            if widgets.JournalMap then widgets.JournalMap = nil shownAt = nil
            else W("JournalMap", {}, {}).visible = false shownAt = clock + 2500 end
        elseif k == "ESCAPE" and widgets.JournalMap then widgets.JournalMap = nil end
    end
    local _wid = BG3AI.UI.widget
    BG3AI.UI.widget = function(n)
        local w = widgets[n]
        if w and n == "JournalMap" and shownAt and clock >= shownAt then w.visible = true end
        return w
    end
    local sj = run(ops["ui.waypoints"]({}).job, 8000)
    ok(sj.status == "done" and sj.result.map_presses == 1 and mapKeys[1] == "M" and mapKeys[2] ~= "M",
        "slow map: one M, waited for it: " .. tostring(sj.status) .. " " .. tostring(sj.error) .. " keys="
        .. table.concat(mapKeys, ","))
    BG3AI.UI.widget = _wid
    Ext.Input.InjectKeyPress = _inj2
    widgets = {}
end
-- a question box up instead of the map (vis live: a rest prompt during fast_travel): reported, not answered
widgets, mapKeys = {}, {}
swallow = 99
local restAnswered = false
W("Dialog_box", { "Long Rest", "Do you want to rest at camp?" }, { { "Yes", nil, function() restAnswered = true end },
    { "No", nil, function() end } })
wj = run(ops["ui.waypoints"]({}).job, 6000)
ok(wj.status == "failed" and wj.result.unanswered and tostring(wj.error):find("not answered", 1, true)
    and not restAnswered, "box instead of the map: reported, not answered: " .. tostring(wj.error))
widgets = {}
swallow = 0
-- travelVeto: rest/camp questions are never answered by fast_travel; travel questions are
ok(BG3AI.Services.travelVeto("Do you want to end the day?") ~= nil and BG3AI.Services.travelVeto("Long Rest") ~= nil,
    "veto: rest questions")
ok(BG3AI.Services.travelVeto("Travelling here will leave the region. Continue?") == nil
    and BG3AI.Services.travelVeto("[ForceUpdate]") == nil
    and BG3AI.Services.travelVeto("Leave the forest? Your interest awaits") == nil, "veto: travel/unreadable questions pass")
W("Dialog_box", { "Long Rest", "Do you want to rest?" }, { { "Yes", nil, function() restAnswered = true end },
    { "No", nil, function() end } })
local vst = {}
local vc, vres, verr = BG3AI.Services.handleConfirm(vst, {}, nil, BG3AI.Services.travelVeto)
ok(vc == "failed" and vres.unanswered and not restAnswered and tostring(verr):find("decide with confirm", 1, true),
    "handleConfirm with the veto leaves a rest question: " .. tostring(verr))
widgets = {}
-- fast_travel through the same path: swallowed press, then the waypoint is found and pressed
widgets, mapKeys = {}, {}
swallow = 1
local _byName, _content, _findAll = BG3AI.UI.byName, BG3AI.UI.content, BG3AI.UI.findAll
local btn = { name = "WaypointEntryButton" }
BG3AI.UI.byName = function(w, n) if n == "Waypoints" then return { list = true } end return _byName(w, n) end
BG3AI.UI.content = function() return { { t = "Wilderness" }, { n = "WaypointEntryButton", b = "The Risen Road" } } end
BG3AI.UI.findAll = function(el, pred) if el.list then return { btn } end return _findAll(el, pred) end
local executed
local _exec = BG3AI.UI.execute
BG3AI.UI.execute = function() executed = true return true end
local ftj2 = jobs[ops["ui.fast_travel"]({ waypoint = "Risen Road" }).job]
for _ = 1, 60 do clock = clock + 50 ftj2.on_tick(ftj2) if executed then break end end
ok(executed and ftj2.status == "running", "fast_travel pressed the waypoint after a swallowed M: "
    .. tostring(ftj2.status) .. " " .. tostring(ftj2.error))

-- right after a load TravelError reads "Unavailable" for a few s (live 2026-10-10): wait, no M meanwhile,
-- then travel; other errors and a lasting Unavailable still fail
local travelErr = "Unavailable"
local _path = BG3AI.UI.path
BG3AI.UI.path = function(dc, p) if p == "CurrentPlayer.TravelError" then return travelErr end return _path(dc, p) end
widgets, mapKeys, executed, swallow = {}, {}, false, 0
local ftj3 = jobs[ops["ui.fast_travel"]({ waypoint = "Risen Road" }).job]
for _ = 1, 40 do clock = clock + 100 ftj3.on_tick(ftj3) end
ok(ftj3.status == "running" and #mapKeys == 0, "Unavailable: waits without pressing M (" .. table.concat(mapKeys, ",") .. ")")
travelErr = nil
for _ = 1, 60 do clock = clock + 50 ftj3.on_tick(ftj3) if executed then break end end
ok(executed and mapKeys[1] == "M", "Unavailable cleared after 4 s: map opened, waypoint pressed")
travelErr, mapKeys = "Unavailable", {}
widgets = {}
local ftj4 = jobs[ops["ui.fast_travel"]({ waypoint = "Risen Road" }).job]
for _ = 1, 100 do clock = clock + 100 ftj4.on_tick(ftj4) if ftj4.status ~= "running" then break end end
ok(ftj4.status == "failed" and tostring(ftj4.error):find("Unavailable for 8 s") and #mapKeys == 0,
    "lasting Unavailable fails after 8 s: " .. tostring(ftj4.error))
travelErr = "Location"
local okl, el = pcall(ops["ui.fast_travel"], { waypoint = "Risen Road" })
ok(not okl and tostring(el):find("can't fast travel now %(Location%)"), "other travel errors fail at once: " .. tostring(el))
BG3AI.UI.path = _path
BG3AI.UI.byName, BG3AI.UI.content, BG3AI.UI.findAll, BG3AI.UI.execute = _byName, _content, _findAll, _exec

-- night camp (live 2026-10-10 11:1x): IsAtNightCamp + TravelError Unavailable for hours after a long rest whose
-- night never ended. fast_travel says so at once; long_rest at night goes to sleep (CampTravel) and waits for the
-- night to end; sleep_only refuses a fresh rest screen.
local night = true
inCamp = true
local _path2 = BG3AI.UI.path
BG3AI.UI.path = function(dc, p)
    if p == "CurrentPlayer.IsAtNightCamp" then return night end
    if p == "CurrentPlayer.TravelError" then return night and "Unavailable" or nil end
    return _path2(dc, p)
end
local okn, en = pcall(ops["ui.fast_travel"], { waypoint = "Risen Road" })
ok(not okn and tostring(en):find("night at camp", 1, true), "night camp: fast_travel says why at once: " .. tostring(en))
local tb = ops["ui.travel_block"]({})
ok(tb.night_camp == true and tb.error == "Unavailable", "travel_block reports night_camp")

local _cmd = BG3AI.UI.command
local sleeps = 0
local sleepEnds = true
BG3AI.UI.command = function(dc, name, param, dry)
    if name == "CampTravel" and not dry then
        sleeps = sleeps + 1
        if sleepEnds then night = false end
        return true
    end
    return _cmd(dc, name, param, dry)
end
clock = clock + 60000
widgets = {}
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
ok(sleeps == 1 and j.status == "done" and j.result.night_ended and j.result.rest == "night_finished",
    "night camp: long_rest goes to sleep and ends the night: " .. tostring(j.status) .. " " .. tostring(j.error))

-- the press does nothing: one retry after 4 s, then a failure that says travel stays blocked
clock, night, sleeps, sleepEnds = clock + 60000, true, 0, false
widgets = {}
r = ops["ui.long_rest"]({})
j = run(r.job, 40000)
ok(sleeps == 2 and j.status == "failed" and tostring(j.error):find("didn't end", 1, true)
    and tostring(j.error):find("blocked", 1, true), "night that won't end: 2 presses, then fails: " .. sleeps .. " "
    .. tostring(j.status) .. " " .. tostring(j.error))

-- a camp scene at night: done, and the note says to sleep after it
clock, night, sleeps = clock + 60000, true, 0
widgets = {}
BG3AI.UI.command = function(dc, name, param, dry)
    if name == "CampTravel" and not dry then sleeps = sleeps + 1 W("Dialogue", {}, {}) return true end
    return _cmd(dc, name, param, dry)
end
r = ops["ui.long_rest"]({})
j = run(r.job, 3000)
ok(j.status == "done" and j.result.camp_scene and j.result.night_open and tostring(j.result.note):find("long_rest again"),
    "night camp scene: done with a note: " .. tostring(j.status) .. " " .. tostring(j.result and j.result.note))

-- sleep_only (the bridge before travelling): the rest screen that opens at night is rested through, partial allowed
clock, sleeps = clock + 60000, 0
widgets = {}
local restCmds = {}
BG3AI.UI.command = function(dc, name, param, dry)
    if name == "CampTravel" and not dry then sleeps = sleeps + 1 W("MakeCamp", {}, {}).DataContext = { dc = "MakeCamp" } return true end
    if dc and dc.dc == "MakeCamp" then
        restCmds[#restCmds + 1] = name
        if name == "ProcessRestCommand" then widgets.MakeCamp = nil night = false end
        return true
    end
    return _cmd(dc, name, param, dry)
end
r = ops["ui.long_rest"]({ sleep_only = true })
j = run(r.job, 30000)
ok(j.status == "done" and restCmds[1] == "AutoSelectCommand" and restCmds[#restCmds] == "ProcessRestCommand",
    "sleep_only: rests from the night rest screen: " .. tostring(j.status) .. " " .. tostring(j.error) .. " "
    .. table.concat(restCmds, ","))
BG3AI.UI.command, BG3AI.UI.path = _cmd, _path2

-- ui.roll: a start command sent while the roll panel is still opening is dropped (live 2026-10-10 lockpick:
-- WaitForStart for 20 s, timeout). The job sends it again until the roll starts.
do
    local roll = { RollState = "WaitForStart", starts = 0, accept_after = 2 }
    widgets = { ActiveRoll = { name = "ActiveRoll", visible = true, DataContext = roll } }
    local _c, _p = BG3AI.UI.command, BG3AI.UI.prop
    BG3AI.UI.prop = function(e, k) if e == roll then return roll[k] end return _p(e, k) end
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then
            if dry then return true end
            roll.starts = roll.starts + 1
            if roll.starts > roll.accept_after then roll.RollState = "ResultReady" end   -- the first ones are dropped
            return true
        end
        return _c(dc, name, param, dry)
    end
    BG3AI.UI.widgetMap = BG3AI.UI.widgetMap or function() return { ActiveRoll = { el = widgets.ActiveRoll } } end
    BG3AI.UIState.roll = BG3AI.UIState.roll or function() return { phase = "result" } end
    local jr = run(ops["ui.roll"]({}).job, 10000)
    ok(jr.status == "done" and roll.starts == 3 and jr.result.retriggered == 2,
        "ui.roll re-sends the start until the roll starts: " .. tostring(jr.status) .. " starts=" .. roll.starts)
    -- an accepted first command: no re-send
    roll.RollState, roll.starts, roll.accept_after = "WaitForStart", 0, 0
    jr = run(ops["ui.roll"]({}).job, 10000)
    ok(jr.status == "done" and roll.starts == 1 and jr.result.retriggered == nil, "ui.roll: one start when it takes")
    ok(type(jr.result.phases) == "string" and jr.result.phases:find("ResultReady@", 1, true) ~= nil,
        "ui.roll: the state timeline rides along for metrics: " .. tostring(jr.result.phases))

    -- Inspiration reroll (S013-S026: 7/7 returned the first failed roll). RollAgainCommand leaves ResultReady a
    -- few frames later for WaitForReRoll, where the dice wait to be thrown again (like WaitForStart).
    local again = 0
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "RollAgainCommand" then
            if dry then return true end
            again = again + 1
            roll.pending = 3   -- ticks until the game switches state
            return true
        end
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then
            if dry then return true end
            roll.starts = roll.starts + 1
            if roll.RollState == "WaitForReRoll" then roll.RollState = "StartRoll" roll.pending = 4 end
            return true
        end
        return _c(dc, name, param, dry)
    end
    BG3AI.UI.prop = function(e, k)
        if e == roll then
            if k == "RollState" and roll.pending then
                roll.pending = roll.pending - 1
                if roll.pending <= 0 then
                    roll.pending = nil
                    roll.RollState = (roll.RollState == "ResultReady") and "WaitForReRoll" or "ResultReady"
                end
            end
            return roll[k]
        end
        return _p(e, k)
    end
    roll.RollState, roll.starts = "ResultReady", 0
    jr = run(ops["ui.roll"]({ reroll = true }).job, 10000)
    ok(jr.status == "done" and again == 1 and roll.starts == 1 and jr.result.rerolled == true
        and roll.RollState == "ResultReady" and (jr.result.phases or ""):find("WaitForReRoll", 1, true) ~= nil,
        "ui.roll reroll: waits out the old result, throws the dice again, returns the new result: "
        .. tostring(jr.status) .. " starts=" .. roll.starts .. " " .. tostring(jr.result.phases))
    -- the game ignores the reroll (state never leaves ResultReady): an honest reroll_ignored, no endless wait
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "RollAgainCommand" then return true end
        return _c(dc, name, param, dry)
    end
    roll.RollState, roll.pending = "ResultReady", nil
    jr = run(ops["ui.roll"]({ reroll = true }).job, 10000)
    ok(jr.status == "done" and jr.result.reroll_ignored == true, "ui.roll reroll ignored by the game: reported")
    -- entering at WaitForReRoll (a reroll pressed earlier) throws the dice like WaitForStart
    roll.RollState, roll.starts = "WaitForReRoll", 0
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then
            if dry then return true end
            roll.starts = roll.starts + 1
            roll.RollState = "ResultReady"
            return true
        end
        return _c(dc, name, param, dry)
    end
    jr = run(ops["ui.roll"]({}).job, 10000)
    ok(jr.status == "done" and roll.starts == 1 and roll.RollState == "ResultReady", "ui.roll at WaitForReRoll rolls")

    -- boosts by name (S024: roll_check(boosts="Guidance") failed "attempt to concatenate a nil value")
    local guidance = { Name = "Guidance" }
    local _path = BG3AI.UI.path
    BG3AI.UI.path = function(o, p)
        if o == roll and p == "BoostModifierList.1" then return guidance end
        if o == roll and p:match("^BoostModifierList%.") then return nil end
        return _path(o, p)
    end
    local _p2 = BG3AI.UI.prop
    BG3AI.UI.prop = function(e, k) if e == guidance then return guidance[k] end return _p2(e, k) end
    local addedWith
    BG3AI.UI.command = function(dc, name, param, dry)
        if dc == roll and name == "AddModifierCommand" then addedWith = param return true end
        if dc == roll and name == "UserTriggeredNextPhaseCommand" then roll.RollState = "ResultReady" return true end
        return _c(dc, name, param, dry)
    end
    roll.RollState = "WaitForStart"
    jr = run(ops["ui.roll"]({ boosts = { "guidance" } }).job, 10000)
    ok(addedWith == guidance and jr.status == "done", "ui.roll boosts by name: Guidance added")
    roll.RollState = "WaitForStart"
    local okb, errb = pcall(ops["ui.roll"], { boosts = { "Bardic Inspiration" } })
    ok(not okb and tostring(errb):find("offered: Guidance", 1, true) ~= nil,
        "ui.roll unknown boost: names what is offered: " .. tostring(errb))
    BG3AI.UI.path = _path
    BG3AI.UI.command, BG3AI.UI.prop = _c, _p
    widgets = {}
end

-- Stack-split box (S028 15:51-15:52): confirm() answers it (amount sets TargetCount), the ops' automatic
-- answers never do, and long_rest cancels a leftover one before pressing CampTravel.
do
    widgets = {}
    local data = { ItemCount = 3, TargetCount = 1, Item = { Name = "Supply Pack" } }
    data.SetProperty = function(self, k, v) self[k] = v end
    local pressed = {}
    local split = W("SplitItem", {}, {
        { "Cancel", nil, function() pressed[#pressed + 1] = "Cancel" widgets.SplitItem = nil end },
        { "", "ConfirmSplitButton", function() pressed[#pressed + 1] = "Confirm" widgets.SplitItem = nil end } })
    split.DataContext = { dc = "SplitItem" }
    local _path, _prop = BG3AI.UI.path, BG3AI.UI.prop
    BG3AI.UI.path = function(dc, p)
        if type(dc) == "table" and dc.dc == "SplitItem" and p == "CurrentPlayer.UIData.SplitItemData" then return data end
        return _path(dc, p)
    end
    BG3AI.UI.prop = function(e, k)
        if e == data or e == data.Item then return e[k] end
        return _prop(e, k)
    end
    ok(A.confirmBox() == nil, "split box: not a question box (handleConfirm never confirms a quantity)")
    local sb = A.splitBox()
    ok(sb and sb.max == 3 and sb.title == "Supply Pack" and #sb.buttons == 2 and sb.buttons[2].label == "Confirm",
        "split box read: Supply Pack 1/3, Cancel/Confirm")
    local okr, err = pcall(ops["ui.confirm"], { answer = "yes", amount = 5 })
    ok(not okr and tostring(err):find("out of range", 1, true) ~= nil, "split amount out of range refused: " .. tostring(err))
    -- bg3le 0011: the amount's set refused (it never ran) raises busy (the op answers busy), nothing pressed
    data.SetProperty = function() error("bg3le: UI busy (frame lock not taken since a timeout); retry", 0) end
    okr, err = pcall(ops["ui.confirm"], { answer = "yes", amount = 2 })
    ok(not okr and tostring(err):find("^BUSY ") ~= nil and #pressed == 0 and data.TargetCount == 1,
        "split amount set refused as busy: raises busy, nothing pressed: " .. tostring(err))
    data.SetProperty = function(self, k, v) self[k] = v end
    local r = ops["ui.confirm"]({ answer = "yes", amount = 2 })
    local j = run(r.job, 4000)
    ok(data.TargetCount == 2 and pressed[1] == "Confirm" and j.status == "done",
        "confirm(yes, amount=2) on the split box: " .. tostring(data.TargetCount) .. " " .. tostring(pressed[1]))

    -- a stale one (no Trade screen) before long_rest: cancelled, CampTravel pressed after it closed
    pressed = {}
    W("SplitItem", {}, {
        { "Cancel", nil, function() pressed[#pressed + 1] = "Cancel" widgets.SplitItem = nil end },
        { "", "ConfirmSplitButton", function() pressed[#pressed + 1] = "Confirm" end } }).DataContext = { dc = "SplitItem" }
    W("Trade", {}, {})
    ok(A.cancelStaleSplit() == nil and #pressed == 0, "split box under an open trade: left alone")
    widgets.Trade = nil
    commands = {}
    local okl, rl = pcall(ops["ui.long_rest"], {})
    ok(okl and pressed[1] == "Cancel" and #commands == 0,
        "long_rest: a leftover split box is cancelled, CampTravel not pressed under it: " .. tostring(rl)
        .. " pressed=" .. tostring(pressed[1]) .. " commands=" .. #commands)
    if okl and rl and rl.job then
        for _ = 1, 12 do clock = clock + 50 jobs[rl.job].on_tick(jobs[rl.job]) end
        ok(commands[1] == "CampTravel", "long_rest: CampTravel pressed once the split box closed")
    end
    BG3AI.UI.path, BG3AI.UI.prop = _path, _prop
    widgets = {}
end

-- The Long Rest button greyed out (EndTheDayState Disabled): refused at once, CampTravel not pressed (S028 15:53).
do
    widgets = {}
    local _path = BG3AI.UI.path
    local state = "Disabled"
    BG3AI.UI.path = function(dc, p)
        if p == "CurrentPlayer.EndTheDayState" then return state end
        return _path(dc, p)
    end
    commands = {}
    local okd, errd = pcall(ops["ui.long_rest"], {})
    ok(not okd and tostring(errd):find("greyed out", 1, true) ~= nil and #commands == 0,
        "long_rest with the button greyed out: refused, no CampTravel: " .. tostring(errd))
    state = "Enabled"
    local oke, re = pcall(ops["ui.long_rest"], {})
    ok(oke and commands[1] == "CampTravel", "long_rest with the button enabled: CampTravel pressed")
    BG3AI.UI.path = _path
    widgets = {}
end
