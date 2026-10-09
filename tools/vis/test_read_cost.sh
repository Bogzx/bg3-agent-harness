#!/usr/bin/env bash
# Offline cost harness for the game-side reads (reach_toward, hazard scan, frontier), Lua 5.3 + 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" lua tools/vis/test_read_cost_lua.lua $L
done
