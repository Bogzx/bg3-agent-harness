#!/bin/bash
#
# Builds libbg3sdl.so for the game's runtime (Steam Runtime 3 "sniper", glibc 2.31).
#
# * SDL headers: SDL2 2.30.6 (the version of the game's bundled bin/libSDL2.so, "SDL-release-2.30.6"),
#   downloaded once into build/bg3sdl/ and checked against a pinned sha256. Headers only, nothing is
#   linked: the shim finds SDL at run time.
# * Compiler: the bg3le builder image (tools/bg3le-builder, clang) against the sniper SDK sysroot that
#   tools/build_bg3le.sh unpacked into build/bg3le/sniper/sysroot, so the library asks for nothing newer
#   than glibc 2.31. The script verifies that.
# * Also builds build/bg3sdl/sdltest (a host-side test program linked against the game's own libSDL2.so).
#
# Outputs: build/bg3sdl/libbg3sdl.so, build/bg3sdl/sdltest
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO/native/bg3sdl"
OUT="$REPO/build/bg3sdl"
SYSROOT="$REPO/build/bg3le/sniper/sysroot"
IMAGE="bg3ai/bg3le-builder:trixie"
GAME_BIN="${BG3AI_GAME_DIR:-$HOME/.local/share/Steam/steamapps/common/Baldurs Gate 3}/bin"
SDL_VER=2.30.6
SDL_SHA256=c6ef64ca18a19d13df6eb22df9aff19fb0db65610a74cc81dae33a82235cacd4
SDL_INC="$OUT/SDL2-$SDL_VER/include"
GLIBC_MAX=2.31

mkdir -p "$OUT/dl"
if [ ! -f "$SDL_INC/SDL.h" ]; then
    tarball="$OUT/dl/SDL2-$SDL_VER.tar.gz"
    [ -f "$tarball" ] || curl -fsSL -o "$tarball" \
        "https://github.com/libsdl-org/SDL/releases/download/release-$SDL_VER/SDL2-$SDL_VER.tar.gz"
    echo "$SDL_SHA256  $tarball" | sha256sum -c --quiet
    tar -xzf "$tarball" -C "$OUT" "SDL2-$SDL_VER/include"
fi

[ -d "$SYSROOT/usr/include" ] || { echo "no sniper sysroot at $SYSROOT; run tools/build_bg3le.sh first" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -q -t "$IMAGE" "$REPO/tools/bg3le-builder" >/dev/null

CFLAGS="-std=gnu11 -O2 -g -fPIC -fvisibility=hidden -Wall -Wextra -Werror -Wno-unused-parameter -D_REENTRANT"
docker run --rm --user "$(id -u):$(id -g)" -v "$REPO:$REPO" -w "$SRC" "$IMAGE" \
    clang --target=x86_64-linux-gnu --sysroot="$SYSROOT" -fuse-ld=lld $CFLAGS -I"$SDL_INC" \
        -shared -Wl,-soname,libbg3sdl.so -Wl,--no-undefined -Wl,-z,defs \
        -o "$OUT/libbg3sdl.so" bg3sdl.c -lpthread -ldl

LIB="$OUT/libbg3sdl.so"
newest="$(objdump -T "$LIB" | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sed 's/GLIBC_//' | sort -V | tail -1)"
echo "libbg3sdl.so: newest glibc symbol version $newest (limit $GLIBC_MAX)"
if [ "$(printf '%s\n%s\n' "$newest" "$GLIBC_MAX" | sort -V | tail -1)" != "$GLIBC_MAX" ]; then
    echo "ERROR: needs glibc $newest > $GLIBC_MAX" >&2; exit 1
fi
echo "exports: $(nm -D --defined-only "$LIB" | awk '$2=="T"{print $3}' | tr '\n' ' ')"

# Test program: host gcc, linked against the game's bundled SDL so the shim is tested against the same build.
if [ -f "$GAME_BIN/libSDL2.so" ]; then
    gcc -std=gnu11 -O1 -g -Wall -Wextra -I"$SDL_INC" -o "$OUT/sdltest" "$SRC/test/sdltest.c" \
        -L"$GAME_BIN" -Wl,-rpath,"$GAME_BIN" -l:libSDL2.so
    echo "built $OUT/sdltest (against $GAME_BIN/libSDL2.so)"
fi
