#!/usr/bin/env bash
# Offline tests for Client/UI.lua's named element reads (test_ui_named_reads.lua: never GetAllProperties, S017's
# crash), in Lua 5.3 and 5.4 (Docker). tools/dev/test_ui_named_reads.py runs the same file when a host Lua exists.
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Client/UI.lua || exit 1
    lua tools/dev/test_ui_named_reads.lua $L/Client/UI.lua"
done
