#!/usr/bin/env bash
# Syntax-check Shared/Mem.lua + RPC.lua and run the offline memory-guard tests in Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Shared/Mem.lua $L/Shared/RPC.lua || exit 1
    lua tools/dev/test_mem.lua $L"
done
