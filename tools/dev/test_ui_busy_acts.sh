#!/usr/bin/env bash
# Offline tests for UI calls that act under bg3le 0011's "UI busy" refusal (Client/UI.lua UI.pcall/UI.execute and
# its callers), Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Client/UI.lua $L/Client/Pick.lua $L/Client/ServicesItems.lua $L/Client/Services.lua \
      $L/Client/Progression.lua $L/Client/Spellbook.lua || exit 1
    lua tools/dev/test_ui_busy_acts.lua $L"
done
