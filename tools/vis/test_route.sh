#!/usr/bin/env bash
# Offline tests for nav.route (route ahead), the frontier ladder fields and the journal cache, in Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Server/Navigation.lua $L/Server/State.lua || exit 1
    lua tools/vis/test_route_lua.lua $L"
done
