#!/usr/bin/env bash
# Offline tests of Shared/RPC.lua's event ring (push, events.since at and past EVENT_CAP), Lua 5.3 and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Shared/RPC.lua || exit 1
    lua tools/dev/test_rpc_events.lua $L"
done
