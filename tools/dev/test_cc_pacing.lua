-- Offline test: cc_apply's pacing in Client/Progression.lua (P.CC_STATE_WAITS) over a fake character creation
-- page whose tab lists render their labels over time and whose dependent lists (subraces after a race pick)
-- rebuild after a delay, with a fake clock (no game). Shapes from the naut series (2026-10-13 stage_ms: 450 ms
-- per tab, 300 ms per pick, 6.1 s per call). Run: tools/dev/test_cc_pacing.sh
local L = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local fails = 0
local function check(cond, what) if not cond then fails = fails + 1 print("FAIL " .. what) else print("ok   " .. what) end end

local now = 0
local function obj(type_, props, onset)
    local o = { _t = type_, _p = props or {}, _onset = onset }
    function o.GetProperty(self, k) return self._p[k] end
    function o.SetProperty(self, k, v)
        if self._onset then self._onset(self, k, v) else self._p[k] = v end
    end
    return o
end

-- Game knobs per case: RENDER_MS = time from a tab's selection until all its items are rendered (item i of n at
-- i/n of it, in list order: "Half-Elf" renders before "Elf"); REBUILD_MS = time from a race pick until
-- SelectableSubRaces shows that race's subraces (the old race's list until then).
local G = { RENDER_MS = 120, REBUILD_MS = 60 }

local function vm(name) return obj("ls.VM", {}), name end  -- VMs carry no readable name: labels come from rendering
local NAMES = {}
local function vms(list_)
    local out = {}
    for i, n in ipairs(list_) do
        local o = vm(n)
        NAMES[o] = n
        out[i] = o
    end
    return out
end

local RACES = vms({ "Human", "Half-Elf", "Elf" })
local SUB = { ["Human"] = {}, ["Half-Elf"] = vms({ "High Half-Elf", "Wood Half-Elf", "Drow Half-Elf" }),
              ["Elf"] = vms({ "High Elf", "Wood Elf" }) }
local CLASSES = vms({ "Wizard", "Fighter" })
local BACKGROUNDS = vms({ "Sage", "Soldier" })

local CUSTOM = obj("ls.VM", { IsCustom = true, Name = "Custom" })
local dc, page
local widget = obj("ls.UIWidget", {})
local function reset()
    page = { subFor = "Human", subAt = 0, shown = nil, shownAt = 0, commands = {} }
    dc = obj("ls.DCCharacterCreation", { SelectedRace = RACES[1], SelectableRaces = RACES, SelectableClasses = CLASSES,
        SelectedClass = CLASSES[1], SelectableBackgrounds = BACKGROUNDS, SelectedBackground = BACKGROUNDS[1],
        SelectableOrigins = { CUSTOM }, SelectedOrigin = CUSTOM, SelectableSubClasses = {}, SelectableDeities = {}, IsCharacterComplete = true },
        function(self, k, v)
            self._p[k] = v
            if k == "SelectedRace" then page.pending, page.pendingAt = NAMES[v], now end
        end)
    setmetatable(dc._p, { __index = function(_, k)
        if k == "SelectableSubRaces" then
            if page.pending and now - page.pendingAt >= G.REBUILD_MS then page.subFor, page.pending = page.pending, nil end
            return SUB[page.subFor]
        end
    end })
    widget.DataContext = dc
    BG3AI.Progression.rendered, BG3AI.Progression.renderedKey = nil, nil
end

local TAB_LIST = { raceTab = "SelectableRaces", subRaceTab = "SelectableSubRaces", classTab = "SelectableClasses",
                   backgroundTab = "SelectableBackgrounds" }
local tabs = {}
for name in pairs({ originTab = 1, raceTab = 1, subRaceTab = 1, classTab = 1, subClassTab = 1, deityTab = 1,
                    backgroundTab = 1, abilityTab = 1 }) do
    tabs[name] = obj("ListBoxItem", { Visibility = "Visible" }, function(self, k, v)
        if k == "IsSelected" and v then page.shown, page.shownAt = name, now end
    end)
end

-- The rendered list items of the shown tab: item i of n once (now - shownAt) >= RENDER_MS * i / n.
local function renderedItems()
    local coll = page.shown and TAB_LIST[page.shown] and dc:GetProperty(TAB_LIST[page.shown]) or nil
    if not coll then return {} end
    local out = {}
    for i, it in ipairs(coll) do
        if now - page.shownAt >= G.RENDER_MS * i / #coll then
            out[#out + 1] = obj("ListBoxItem", { DataContext = it, _tb = obj("TextBlock", { _text = NAMES[it] }) })
        end
    end
    return out
end

BG3AI = {}
local handlers, jobs = {}, {}
BG3AI.RPC = {
    register = function(n, f) handlers[n] = f end,
    now_ms = function() return now end,
    start_job = function(kind) local j = { id = #jobs + 1, kind = kind } jobs[#jobs + 1] = j return j end,
    finish_job = function(j, st, res, err) j.status, j.result, j.error = st, res, err end,
}
BG3AI.UI = {
    prop = function(o, k) if o == nil then return nil end return o:GetProperty(k) end,
    props = function(o) return o and o._p or {} end,
    typeName = function(o) return type(o) == "table" and o._t or nil end,
    text = function(o) return o and o._p and o._p._text or nil end,
    clean = function(s) return s end,
    widgets = function() return { { name = "CharacterCreation", el = widget } } end,
    widget = function() return widget end,
    isVisible = function() return true end,
    cachedByName = function(_, n) return tabs[n] end,
    byName = function() return nil end,
    command = function(o, n) page.commands[#page.commands + 1] = n return true end,
    items = function(coll)
        local i = 0
        return function()
            i = i + 1
            if type(coll) == "table" and coll[i] ~= nil then return i, coll[i] end
            return nil
        end
    end,
    count = function(coll) return type(coll) == "table" and #coll or 0 end,
    path = function() return nil end,
    findAll = function(root, pred)
        if root == widget then return renderedItems() end
        if root and root._p and root._p._tb then return { root._p._tb } end
        return {}
    end,
}
dofile(L .. "/Client/Progression.lua")
local P = BG3AI.Progression

local TICK = 16  -- ms per game tick (60 fps)
local function apply(plan, extra)
    reset()
    local args = { plan = plan, finish = false }
    for k, v in pairs(extra or {}) do args[k] = v end
    local t0 = now
    local res = handlers["ui.cc_apply"](args)
    local j = jobs[res.job]
    for _ = 1, 4000 do
        if j.status then break end
        now = now + TICK
        j.on_tick(j)
    end
    return j, now - t0
end

local PLAN = { race = "Elf", subrace = "High Elf", class = "Fighter", background = "Soldier", abilities = "recommended" }

-- 1. state waits (default): same picks as the fixed waits, a fraction of the time
local j, ms = apply(PLAN)
check(j.status == "done", "state: done (" .. tostring(j.error) .. ")")
check(j.result.pacing == "state", "state: result.pacing = state")
check(j.result.set.race == "Elf" and j.result.set.subrace == "High Elf" and j.result.set.class == "Fighter"
    and j.result.set.background == "Soldier", "state: race/subrace/class/background as planned")
check(#j.result.problems == 0, "state: no problems (" .. table.concat(j.result.problems, "; ") .. ")")
check(ms < 2500, "state: cc_apply " .. ms .. " ms < 2500 (labels render in 120 ms here; live unknown, capped at 450)")
print("     stages: " .. j.result._stage_ms)
local fast_ms = ms

-- 2. args.paced (BG3AI_CC_PACED=1): the old fixed waits, same result
j, ms = apply(PLAN, { paced = true })
check(j.status == "done" and j.result.pacing == "fixed", "paced: done, pacing = fixed")
check(j.result.set.race == "Elf" and j.result.set.subrace == "High Elf", "paced: same picks")
check(ms >= 8 * 450 + 6 * 300, "paced: >= the recorded fixed waits (" .. ms .. " ms)")
check(fast_ms * 2.5 < ms, "state waits cut cc_apply by > 2.5x (" .. fast_ms .. " vs " .. ms .. " ms)")
local tab = j.result._stage_ms:match("race=(%d+)")
check(tab and tonumber(tab) >= 450, "paced: race stage holds the 450 ms tab wait (" .. tostring(tab) .. ")")

-- 3. P.CC_STATE_WAITS = false: the fixed waits too
P.CC_STATE_WAITS = false
j, ms = apply(PLAN)
check(j.result.pacing == "fixed" and ms >= 8 * 450, "CC_STATE_WAITS=false: fixed waits (" .. ms .. " ms)")
P.CC_STATE_WAITS = true

-- 4. a half-rendered race list: "Half-Elf" is labelled before "Elf"; a pick then would substring-match "Elf"
--    to "Half-Elf". The state wait holds until the whole list is labelled.
G.RENDER_MS = 300
j, ms = apply(PLAN)
check(j.status == "done" and j.result.set.race == "Elf", "half-rendered list: waits, picks Elf (not Half-Elf): "
    .. tostring(j.result.set.race))
check(ms < 3000, "half-rendered list: still faster than fixed (" .. ms .. " ms)")
G.RENDER_MS = 120

-- 5. the subrace list rebuilds 250 ms after the race pick (beyond the 100 ms settle): the subrace pick waits for
--    it instead of failing on the old race's list
G.REBUILD_MS = 250
j, ms = apply({ race = "Half-Elf", subrace = "Wood Half-Elf", class = "Fighter" })
check(j.status == "done" and j.result.set.subrace == "Wood Half-Elf", "slow dependent list: subrace waited for ("
    .. tostring(j.error or j.result.set.subrace) .. ")")
G.REBUILD_MS = 60

-- 6. labels never render within the cap: falls back like the fixed waits (the pick with what's there) and stops
--    within the old delays, no hang
G.RENDER_MS = 100000
local t = now
j, ms = apply({ race = "Elf" })
check(j.status == "failed" and tostring(j.error):find("no 'Elf'"), "no labels: fails like before (" .. tostring(j.error) .. ")")
check(ms < 450 + 300 + 200, "no labels: gives up at the old delays (" .. ms .. " ms)")
G.RENDER_MS = 120

-- 7. a wrong name fails (after the cap), as before
j, ms = apply({ race = "Gnome" })
check(j.status == "failed" and tostring(j.error):find("no 'Gnome'"), "unknown race: fails")

-- 8. no subraces (Human): the empty list passes after one settle, not the cap
j, ms = apply({ race = "Human", class = "Fighter" })
local sub = tonumber(j.result._stage_ms:match("subrace=(%d+)") or "9999")
check(j.status == "done" and sub <= 300, "empty subrace list: " .. sub .. " ms (<= 300: the old pick wait since the race pick)")

-- 9. a plan that leaves the subrace to the page's default: the old race's (empty) list is not taken as final
--    while the race pick is fresh; the rebuilt list's first subrace is defaulted
G.REBUILD_MS = 200
j, ms = apply({ race = "Elf", class = "Fighter" })
check(j.status == "done" and j.result.set.subrace == "High Elf" and (j.result.defaulted or {})[1] == "subrace",
    "default subrace after a slow rebuild: " .. tostring(j.result.set.subrace))
G.REBUILD_MS = 60

print(fails == 0 and "ALL OK" or (fails .. " FAILED"))
os.exit(fails == 0 and 0 or 1)
