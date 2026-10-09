#!/bin/bash
# Regenerates Server/Data/JournalMarkers.lua from the installed game (read-only).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
data="$HOME/.local/share/Steam/steamapps/common/Baldurs Gate 3/Data"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
docker run --rm -v "$data":/data:ro -v "$here":/tools:ro -v "$tmp":/out python:3.12-slim sh -c \
  "pip install -q lz4 zstandard >/dev/null 2>&1; for p in Gustav.pak GustavX.pak Shared.pak Patch8_HotFix10.pak; do python -I /tools/pak.py /data/\$p 'Story/Journal/(Markers/|objective_prototypes)' /out >/dev/null; done"
mkdir -p "$repo/mod/Mods/BG3AI/ScriptExtender/Lua/Server/Data"
python3 -I "$here/gen_marker_index.py" "$tmp" "$repo/mod/Mods/BG3AI/ScriptExtender/Lua/Server/Data/JournalMarkers.lua"
