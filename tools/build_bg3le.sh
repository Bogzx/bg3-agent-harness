#!/bin/bash
#
# Builds the portable libbg3le.so from third_party/bg3le (pinned submodule) with bg3le's own
# tools/build-sniper.sh, inside a throwaway toolchain container (tools/bg3le-builder).
#
# Outputs (gitignored):
#   build/bg3le/sniper/bg3le/libbg3le.so   the LD_PRELOAD library (Lua builtins are compiled into it)
#   build/bg3le/sniper/...                 sysroot, libc++, abseil, protobuf (cached; delete to rebuild)
#   build/bg3le/dist/libbg3le.so           copy of the library that runner/launch_game.sh uses
#   build/bg3le/dist/bg3lua                bg3le's console/debugger client (Python script)
#   build/bg3le/dist/BUILDINFO             commit, clang version, glibc check
# Fetched third-party sources land in third_party/bg3le/external/third_party/ (gitignored by bg3le).
#
# Usage: tools/build_bg3le.sh            # incremental
#        tools/build_bg3le.sh --clean    # wipe build/bg3le first
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/third_party/bg3le"
OUT="$REPO/build/bg3le"
IMAGE="bg3ai/bg3le-builder:trixie"

if [ "${1:-}" = "--clean" ]; then rm -rf "$OUT"; fi
mkdir -p "$OUT/sniper" "$OUT/dist" "$OUT/home"

[ -f "$SRC/CMakeLists.txt" ] || { echo "submodule missing: git submodule update --init --recursive" >&2; exit 1; }

docker build -q -t "$IMAGE" "$REPO/tools/bg3le-builder" >/dev/null

# Same absolute path inside as outside, so CMake caches and the printed paths stay valid on the host.
# Runs as the calling user (files stay ours); the docker group lets build-sniper.sh drive the host daemon.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c %g /var/run/docker.sock)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO:$REPO" \
    -w "$SRC" \
    -e HOME="$OUT/home" \
    -e BG3LE_SNIPER_BUILD="$OUT/sniper" \
    -e BG3LE_VERSION="$(git -C "$SRC" describe --tags --always 2>/dev/null || git -C "$SRC" rev-parse --short HEAD)" \
    "$IMAGE" \
    bash -c 'git config --global --add safe.directory "*" && tools/build-sniper.sh'

LIB="$OUT/sniper/bg3le/libbg3le.so"
install -m 0755 "$LIB" "$OUT/dist/libbg3le.so"
install -m 0755 "$SRC/client/bg3lua" "$OUT/dist/bg3lua"
{
    echo "bg3le commit: $(git -C "$SRC" rev-parse HEAD)"
    echo "built: $(date -Is)"
    echo "clang: $(docker run --rm "$IMAGE" clang --version | head -1)"
    echo "sha256: $(sha256sum "$LIB" | cut -d' ' -f1)"
    echo "newest glibc: $(objdump -T "$LIB" | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -V | tail -1)"
    echo "needed: $(readelf -d "$LIB" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | tr '\n' ' ')"
} > "$OUT/dist/BUILDINFO"
cat "$OUT/dist/BUILDINFO"
