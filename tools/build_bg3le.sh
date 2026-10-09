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
# name order) to the submodule's working tree, unless they are already there, so re-running is safe;
# the submodule's commit never changes. `--unpatch` reverts them.
#
# Usage: tools/build_bg3le.sh            # incremental
#        tools/build_bg3le.sh --clean    # wipe build/bg3le first
#        tools/build_bg3le.sh --patch    # apply patches/bg3le and exit (no build)
#        tools/build_bg3le.sh --unpatch  # revert patches/bg3le from the submodule and exit
# BG3LE_SRC overrides the source tree (tools/test_bg3le_patches.py uses it on a scratch copy).
# The build refuses to run while bg3 does (RAM), unless BG3LE_BUILD_WHILE_GAME=1.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${BG3LE_SRC:-$REPO/third_party/bg3le}"
OUT="$REPO/build/bg3le"
IMAGE="bg3ai/bg3le-builder:trixie"
PATCHES="$REPO/patches/bg3le"

[ -f "$SRC/CMakeLists.txt" ] || { echo "submodule missing: git submodule update --init --recursive" >&2; exit 1; }

shopt -s nullglob
patches=("$PATCHES"/*.patch)

# The files the series touches, and what they look like with every patch applied to the pinned
# commit (built in a scratch copy). The submodule's copies must match either the commit (apply the
# series) or that result (already applied); anything else is a stop, since it means the submodule
# moved or someone edited it by hand. (git apply cannot check overlapping patches as a series.)
touched=()
if [ ${#patches[@]} -gt 0 ]; then
    mapfile -t touched < <(sed -n 's|^+++ b/||p' "${patches[@]}" | sort -u)
fi
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
if [ ${#touched[@]} -gt 0 ]; then
    git -C "$SRC" archive HEAD -- "${touched[@]}" | tar -x -C "$scratch"
    git -C "$scratch" init -q
    for p in "${patches[@]}"; do
        git -C "$scratch" apply "$p" || { echo "patch does not apply to the pinned commit: $p" >&2; exit 1; }
    done
fi
patched_state() {  # 0 = all applied, 1 = pristine, 2 = something else
    local f same=1 clean=1
    for f in "${touched[@]}"; do
        cmp -s "$SRC/$f" "$scratch/$f" || same=0
    done
    git -C "$SRC" diff --quiet HEAD -- "${touched[@]}" || clean=0
    if [ $same = 1 ]; then return 0; elif [ $clean = 1 ]; then return 1; else return 2; fi
}

if [ ${#touched[@]} -gt 0 ]; then
    state=0; patched_state || state=$?
    if [ "${1:-}" = "--unpatch" ]; then
        case $state in
            0) git -C "$SRC" checkout -- "${touched[@]}"; echo "reverted ${#patches[@]} patch(es)" ;;
            1) echo "not patched" ;;
            *) echo "refusing to revert: $SRC has edits that are not patches/bg3le" >&2; exit 1 ;;
        esac
        exit 0
    fi
    case $state in
        0) echo "patches already applied: ${#patches[@]}" ;;
        1) for p in "${patches[@]}"; do git -C "$SRC" apply "$p"; echo "patch applied: $(basename "$p")"; done ;;
        *) echo "patch does not apply to $SRC (pinned commit changed, or local edits):" >&2
           git -C "$SRC" diff --stat HEAD -- "${touched[@]}" >&2
           exit 1 ;;
    esac
elif [ "${1:-}" = "--unpatch" ]; then
    exit 0
fi
if [ "${1:-}" = "--patch" ]; then exit 0; fi

# The build needs most of the RAM a running game uses (15 GB machines): not both at once.
if pgrep -x bg3 >/dev/null && [ "${BG3LE_BUILD_WHILE_GAME:-0}" != 1 ]; then
    echo "bg3 is running; not building (set BG3LE_BUILD_WHILE_GAME=1 to override)" >&2
    exit 1
fi

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
