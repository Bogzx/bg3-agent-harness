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
# Our fixes to bg3le live in patches/bg3le/*.patch (git diffs against the pinned commit, applied in
# name order). Each is applied to the submodule's working tree unless it is already there, so
# re-running is safe; the submodule's commit never changes. `--unpatch` reverts them.
#
# Usage: tools/build_bg3le.sh            # incremental
#        tools/build_bg3le.sh --clean    # wipe build/bg3le first
#        tools/build_bg3le.sh --patch    # apply patches/bg3le and exit (no build)
#        tools/build_bg3le.sh --unpatch  # revert patches/bg3le from the submodule and exit
# BG3LE_SRC overrides the source tree (tools/test_bg3le_patches.py uses it on a scratch copy).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${BG3LE_SRC:-$REPO/third_party/bg3le}"
OUT="$REPO/build/bg3le"
IMAGE="bg3ai/bg3le-builder:trixie"
PATCHES="$REPO/patches/bg3le"

[ -f "$SRC/CMakeLists.txt" ] || { echo "submodule missing: git submodule update --init --recursive" >&2; exit 1; }

shopt -s nullglob
patches=("$PATCHES"/*.patch)

if [ "${1:-}" = "--unpatch" ]; then
    for ((i = ${#patches[@]} - 1; i >= 0; i--)); do
        p="${patches[$i]}"
        if git -C "$SRC" apply --check -R "$p" 2>/dev/null; then
            git -C "$SRC" apply -R "$p" && echo "reverted $(basename "$p")"
        fi
    done
    exit 0
fi

# Applied, already applied, or a stop: a patch that fits neither way means the submodule moved or
# was edited by hand.
for p in "${patches[@]}"; do
    if git -C "$SRC" apply --check -R "$p" 2>/dev/null; then
        echo "patch already applied: $(basename "$p")"
    elif git -C "$SRC" apply --check "$p" 2>/dev/null; then
        git -C "$SRC" apply "$p"
        echo "patch applied: $(basename "$p")"
    else
        echo "patch does not apply to $SRC (pinned commit changed, or local edits): $p" >&2
        git -C "$SRC" apply --check "$p" >&2 || true
        exit 1
    fi
done
if [ "${1:-}" = "--patch" ]; then exit 0; fi

if [ "${1:-}" = "--clean" ]; then rm -rf "$OUT"; fi
mkdir -p "$OUT/sniper" "$OUT/dist" "$OUT/home"

docker build -q -t "$IMAGE" "$REPO/tools/bg3le-builder" >/dev/null

# Compile jobs are capped (BG3LE_JOBS, default 6): each clang job can take 1-2 GB, and nproc+2 jobs
# could run a 16 GB machine out of RAM. Docker itself costs almost nothing on Linux (no VM).
# Same absolute path inside as outside, so CMake caches and the printed paths stay valid on the host.
# Runs as the calling user (files stay ours); the docker group lets build-sniper.sh drive the host daemon.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c %g /var/run/docker.sock)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO:$REPO" \
    -w "$SRC" \
    -e HOME="$OUT/home" \
    -e CMAKE_BUILD_PARALLEL_LEVEL="${BG3LE_JOBS:-6}" \
    -e BG3LE_SNIPER_BUILD="$OUT/sniper" \
    -e BG3LE_VERSION="$(git -C "$SRC" describe --tags --always 2>/dev/null || git -C "$SRC" rev-parse --short HEAD)" \
    "$IMAGE" \
    bash -c 'git config --global --add safe.directory "*" && tools/build-sniper.sh'

LIB="$OUT/sniper/bg3le/libbg3le.so"
install -m 0755 "$LIB" "$OUT/dist/libbg3le.so"
install -m 0755 "$SRC/client/bg3lua" "$OUT/dist/bg3lua"
{
    echo "bg3le commit: $(git -C "$SRC" rev-parse HEAD)"
    for p in "${patches[@]}"; do echo "patch: $(basename "$p") $(sha256sum "$p" | cut -c1-16)"; done
    echo "built: $(date -Is)"
    echo "clang: $(docker run --rm "$IMAGE" clang --version | head -1)"
    echo "sha256: $(sha256sum "$LIB" | cut -d' ' -f1)"
    echo "newest glibc: $(objdump -T "$LIB" | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -V | tail -1)"
    echo "needed: $(readelf -d "$LIB" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | tr '\n' ' ')"
} > "$OUT/dist/BUILDINFO"
cat "$OUT/dist/BUILDINFO"
