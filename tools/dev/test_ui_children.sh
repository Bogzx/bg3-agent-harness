#!/usr/bin/env bash
# Offline tests for Client/UI.lua's Panel child reads, Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Client/UI.lua || exit 1
    lua tools/dev/test_ui_children.lua $L"
done
