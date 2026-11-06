#!/usr/bin/env bash
# Offline tests for two teams in one fight (test_arena_lua.lua: server; test_arena_lua_client.lua: client), in Lua 5.3
# and 5.4 (Docker).
set -euo pipefail
cd "$(dirname "$0")/../.."
L=mod/Mods/BG3AI/ScriptExtender/Lua
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -w /repo "$img" sh -c "
    luac -p $L/Shared/RPC.lua $L/Shared/Arena.lua $L/Server/Arena.lua $L/Server/Guard.lua $L/Server/Events.lua \
      $L/Client/UIState.lua $L/Client/UIStateArena.lua $L/Client/UIActionsPrompts.lua $L/Client/UISettings.lua \
      $L/Client/ArenaCamera.lua || exit 1
    lua tools/dev/test_arena_lua.lua $L && lua tools/dev/test_arena_lua_client.lua $L"
done
