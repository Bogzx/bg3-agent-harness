#!/usr/bin/env bash
# Syntax-check the save driver / ui.state code and run its offline tests in Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Client/UIActions.lua $L/Client/UIState.lua || exit 1
    lua tools/dev/test_save.lua $L"
done
