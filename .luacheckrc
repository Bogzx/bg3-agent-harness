-- luacheck config for the mod and its offline tests (tools/ci/lua_lint.py runs it; warnings that predate the lint
-- are in tools/ci/luacheck_baseline.json, which only shrinks).
-- Lua 5.3 names only: the mod runs in bg3le's Lua 5.4, the tests in 5.3 and 5.4, and 5.4 adds nothing the code uses.
std = "lua53"
max_line_length = 120
-- Script Extender globals. Ext is writable: the mod sets engine fields through it (Scenario's RollModeType).
-- PersistentVars is the SE's per-save table, which the mod assigns.
globals = { "BG3AI", "Ext", "PersistentVars" }
read_globals = { "Osi", "Mods" }
-- `_name` marks an argument a callback signature needs but the body doesn't use.
ignore = { "212/_.*" }

files["mod/Mods/BG3AI/ScriptExtender/Lua/Server/Data"] = { max_line_length = false } -- generated tables

-- The tests define their stubs and helpers as top-level globals and their part files (dofile'd by the main test)
-- read them, so a global set at a test file's top level is defined for every test file. lua_lint.py checks the
-- tests in their own luacheck run, so these globals never count as defined for the mod. The tests stub os too.
files["tools/dev"] = { allow_defined_top = true, globals = { "os" } }
