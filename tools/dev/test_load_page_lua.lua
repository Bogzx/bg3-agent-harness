-- Offline tests for Client/UIActionsMenu.lua's load page wait (A.loadGroupsSettled): live N8 step 9, the first Load
-- page open after a launch listed only '' and 'Claude' at 3 s and ui.menu load failed "no playthrough named 'Vessa
-- Thornquill'"; the same load 0.8 s later found it. The wait now ends on a settled, fully named list, or at a
-- cold-open cap counted from the page opening and sized from what the open stage left of the 30 s job. MENU.load's
-- tick is driven below through stubbed UI calls on a fake Load page.
--   lua tools/dev/test_load_page_lua.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

-- the fake game: clock (ms), a loading screen up until loadingUntil, the Load page opening 200 ms after the command
local clock, loadingUntil, pageOpensAt = 0, -1, nil
local groups = {}   -- the page's Expanders: {Type, hdr, shownAt, props = {IsExpanded}, kids = save rows}
local tick          -- MENU.load's job check, captured by the waitJob stub
local registered = {}
local function el(t) t.props = t.props or {} t.SetProperty = function(self, k, v) self.props[k] = v end return t end
local page = el({ Type = "LoadGame" })
Ext = { Utils = { MonotonicTime = function() return clock end, Print = function() end, PrintWarning = function() end,
    GetGameState = function() return "Running" end }, Input = { InjectKeyPress = function() end } }
local UI = {
    widget = function(n) if n == "LoadGame" and pageOpensAt and clock >= pageOpensAt then return page end end,
    widgets = function() return clock < loadingUntil and { { name = "root" } } or {} end,
    isVisible = function() return true end,
    typeName = function(e) return type(e) == "table" and e.Type or nil end,
    prop = function(e, k) return e and e.props[k] end,
    pcall = function(f, ...) return pcall(f, ...) end,
    allText = function(e) return clock >= (e.namedAt or 0) and e.hdr or nil end,
    dc = function() return {} end, get = function() return nil end, anyDC = function() return {} end,
    command = function(_, cmd) if cmd == "OpenLoadGameDialog" then pageOpensAt = clock + 200 end return true end,
    find = function() return nil end,
}
-- groups listed now; a group's save rows show 200 ms after it is expanded
function UI.findAll(e, pred, _)
    local out = {}
    local kids = e == page and groups or (e.props.IsExpanded and clock >= e.props.listsAt and e.kids or {})
    for _, k in ipairs(kids) do
        if (k.shownAt or 0) <= clock and pred(k) then out[#out + 1] = k end
    end
    return out
end
local function group(hdr, shownAt, nSaves, namedAt)
    local g = el({ Type = "Expander", hdr = hdr, shownAt = shownAt, namedAt = namedAt, kids = {} })
    g.SetProperty = function(self, k, v)
        self.props[k] = v
        if k == "IsExpanded" and v then self.props.listsAt = clock + 200 end
    end
    for _ = 1, nSaves or 0 do
        g.kids[#g.kids + 1] = el({ Type = "ls.LSListBoxItem", props = { DataContext = { Type = "ls.VMSavegame" } } })
    end
    return g
end
BG3AI = { RPC = { register = function(n, f) registered[n] = f end, now_ms = function() return clock end }, UI = UI,
    UIActions = { inner = { check = function() end,
        try = function(f, ...) local okc, r = pcall(f, ...) if okc then return r end end,
        waitJob = function(_, _, fn) tick = fn return {} end } } }
dofile(LUA .. "/Client/UIActionsMenu.lua")
local A = BG3AI.UIActions
A.LOAD_ENABLED = true
local JOB_MS = 30000 -- ui.menu.load's job timeout
-- run one load from t = 0, ticking every 80 ms until it ends or the job's own timeout -> {status, res, job, t (end)}
local function runLoad(args)
    clock, pageOpensAt = 0, nil
    registered["ui.menu"](args)
    local job = {}
    while clock <= JOB_MS do
        local status, res = tick(job)
        if status then return { status = status, res = res, job = job, t = clock } end
        -- a save row was selected: the select stage worked
        if job.picked then return { status = "picked", job = job, t = clock } end
        clock = clock + 80
    end
    return { status = "timeout", job = job, t = clock }
end
local settled = A.loadGroupsSettled

-- N8: '' and 'Claude' listed 3.5 s after the page opened: a group is still being named, keep waiting
local t = {}
ok(settled(t, { "", "Claude" }, 0, 0) == false, "just opened: wait")
ok(settled(t, { "", "Claude" }, 3500, 0) == false, "3.5 s, an unnamed group listed: wait (main gave up at 3 s)")
-- every group named and the list unchanged for the settle time: the playthrough isn't there
t = {}
ok(settled(t, { "Claude", "Marek Dunhallow" }, 100, 0) == false, "named list just seen")
ok(settled(t, { "Claude", "Marek Dunhallow" }, 100 + A.LOAD_GROUPS_SETTLE_MS, 0) == false, "settle time not passed")
ok(settled(t, { "Claude", "Marek Dunhallow" }, 101 + A.LOAD_GROUPS_SETTLE_MS, 0) == true, "settled: give up")
-- a list still growing restarts the settle clock
t = {}
settled(t, { "Claude" }, 0, 0)
ok(settled(t, { "Claude", "Tav" }, 2000, 0) == false, "the list changed at 2 s")
ok(settled(t, { "Claude", "Tav" }, 4500, 0) == false, "2.5 s after the change: wait")
ok(settled(t, { "Claude", "Tav" }, 5001, 0) == true, "3 s after the last change: give up")
-- the cold-open cap: an unnamed group (or nothing at all) never holds the job past LOAD_GROUPS_MAX_MS
t = {}
ok(settled(t, { "" }, A.LOAD_GROUPS_MAX_MS, 0) == false, "unnamed at the cap: wait")
ok(settled(t, { "" }, A.LOAD_GROUPS_MAX_MS + 1, 0) == true, "unnamed past the cap: give up")
t = {}
ok(settled(t, {}, 10000, 0) == false, "nothing listed yet: wait")
ok(settled(t, {}, A.LOAD_GROUPS_MAX_MS + 1, 0) == true, "nothing listed past the cap: give up")

-- review of PR #36 (1): N8's page listed '' and 'Claude', the '' named 'Vessa Thornquill' 3.8 s after the page opened
-- (collapsed): its saves get 3 s from when it is found, not from the page opening (it failed "no saves listed" on the
-- tick it was expanded)
loadingUntil = -1
groups = { group("Claude", 0, 5), group("Vessa Thornquill", 0, 3, 4000) }
local r = runLoad({ action = "load", name = "VessaThornquill-1234__S003", owner = "Vessa Thornquill" })
ok(r.status == "picked" and r.job.picked == 1, "group found at 3.8 s: a save is selected (got " .. r.status
    .. (r.res and (": " .. tostring(r.res.error)) or "") .. ")")
-- a found group whose saves never list still fails, 3 s after it was found
groups = { group("Claude", 0, 5), group("Vessa Thornquill", 0, 0, 4000) }
r = runLoad({ action = "load", name = "x", owner = "Vessa Thornquill" })
ok(r.status == "failed" and tostring(r.res.error):find("no saves listed", 1, true) and r.t > 7000 and r.t < 7200,
    "found group with no saves: fails 3 s after it was found (" .. r.t .. " ms)")

-- (2): another group expanded is collapsed at 1.5 s; an absent playthrough with a group never named still ends at the
-- cap counted from the page opening (200 ms), and "listed for" is measured from there too
groups = { group("Claude", 0, 5), group("", 0, 0) }
groups[1].props.IsExpanded, groups[1].props.listsAt = true, 0
r = runLoad({ action = "load", name = "x", owner = "Marek Dunhallow" })
ok(r.status == "failed" and tostring(r.res.error):find("no playthrough named", 1, true) ~= nil,
    "absent: the facts error")
ok(r.t - 200 <= A.LOAD_GROUPS_MAX_MS + 80, "absent, collapse at 1.5 s: fails at the cap from the page opening ("
    .. (r.t - 200) .. " ms after it)")
local listed = r.res and tonumber(tostring(r.res.error):match("listed for ([%d.]+)s"))
ok(listed and math.abs(listed * 1000 - (r.t - 200)) < 100, "'listed for' counts from the page opening ("
    .. tostring(listed) .. " s)")

-- (3): a loading screen held the open stage 20 s: an absent playthrough still ends with the facts error inside the
-- 30 s job, not as its timeout
loadingUntil = 20000
groups = { group("", 0, 0) }
r = runLoad({ action = "load", name = "x", owner = "Marek Dunhallow" })
ok(r.status == "failed" and tostring(r.res.error):find("no playthrough named", 1, true) ~= nil,
    "20 s loading screen, absent: the facts error (got " .. r.status .. ")")
ok(r.t <= JOB_MS - 1000, "... before the job's 30 s timeout (" .. r.t .. " ms)")
-- the worst open stage (LOADING_WAIT_MS + the 4 s page timer) still leaves the cap inside the job
local cap = A.loadGroupsCap or function() return A.LOAD_GROUPS_MAX_MS + 1500 end -- branch: 15 s after the collapse
ok(29000 + math.max(0, cap(29000)) <= JOB_MS - 1000, "cap after a 29 s open stays in the job")
ok(cap(0) == A.LOAD_GROUPS_MAX_MS, "a quick open keeps the full cap")

print(string.format("%d/%d passed", pass, pass + fail))
if fail > 0 then os.exit(1) end
