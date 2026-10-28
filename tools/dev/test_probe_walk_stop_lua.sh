#!/usr/bin/env bash
# Offline test for tools/dev/probe_walk_stop.py's server sampler (its INSTALL Lua), Lua 5.3 and 5.4 (Docker): the
# cadence of its deep reads (N17: a per-tick deep read froze the game 5.9-7.5 s a tick).
set -euo pipefail
cd "$(dirname "$0")/../.."
T=$(mktemp -d)
trap 'rm -f "$T/install.lua"; rmdir "$T"' EXIT
python3 -c "import sys; sys.path.insert(0, 'tools/dev'); import probe_walk_stop as p; print(p.INSTALL)" > "$T/install.lua"
for img in nickblah/lua:5.3 nickblah/lua:5.4; do
  echo "== $img"
  docker run --rm -v "$PWD":/repo:ro -v "$T":/t:ro -w /repo "$img" sh -c "
    luac -p /t/install.lua || exit 1
    lua tools/dev/test_probe_walk_stop.lua /t/install.lua"
done
