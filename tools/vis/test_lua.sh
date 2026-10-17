#!/usr/bin/env bash
# Syntax-check the server Lua and run the offline vis tests in Lua 5.3 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
docker run --rm -v "$PWD":/repo:ro -w /repo nickblah/lua:5.3 sh -c '
  for f in mod/Mods/BG3AI/ScriptExtender/Lua/*/*.lua mod/Mods/BG3AI/ScriptExtender/Lua/Server/Data/*.lua; do
    luac -p "$f" || exit 1
  done
  lua tools/vis/test_lua_vis.lua mod/Mods/BG3AI/ScriptExtender/Lua || exit 1
  lua tools/vis/test_route_lua.lua mod/Mods/BG3AI/ScriptExtender/Lua || exit 1
  lua tools/vis/test_read_cost_lua.lua mod/Mods/BG3AI/ScriptExtender/Lua || exit 1  # NAV-6: was outside the suite
  lua tools/vis/test_inv_cost_lua.lua mod/Mods/BG3AI/ScriptExtender/Lua'  # V12: state.inventory stats reads
