-- Offline tests for Client/UIActionsMenu.lua's load page wait (A.loadGroupsSettled): live N8 step 9, the first Load
-- page open after a launch listed only '' and 'Claude' at 3 s and ui.menu load failed "no playthrough named 'Vessa
-- Thornquill'"; the same load 0.8 s later found it. The wait now ends on a settled, fully named list, or at a
-- cold-open cap.
--   lua tools/dev/test_load_page_lua.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

Ext = { Utils = { MonotonicTime = function() return 0 end, Print = function() end, PrintWarning = function() end } }
BG3AI = { RPC = { register = function() end, now_ms = function() return 0 end }, UI = {},
    UIActions = { inner = { check = function() end, try = function() end, waitJob = function() end } } }
dofile(LUA .. "/Client/UIActionsMenu.lua")
local A = BG3AI.UIActions
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
ok(A.LOAD_GROUPS_MAX_MS < 30000 - 10000, "the cap leaves the 30 s job time to select and load")

print(string.format("%d/%d passed", pass, pass + fail))
if fail > 0 then os.exit(1) end
