#!/usr/bin/env bash
# Offline fairness tests (audit 2026-10-10) for Navigation.lua + Visibility.lua (test_vis_lua.lua: V22), in Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Server/Navigation*.lua $L/Server/Visibility.lua $L/Server/State.lua || exit 1
    lua tools/dev/test_fairness_lua.lua $L && lua tools/dev/test_vis_lua.lua $L"
done
