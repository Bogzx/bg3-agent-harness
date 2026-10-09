# Toolchain for third_party/bg3le/tools/build-sniper.sh.
#
# The host has no clang/cmake/ninja/bsdtar, so the whole build runs in here. The game-facing library is still built
# against the Steam Runtime 3 (sniper) SDK sysroot that build-sniper.sh unpacks; this image only supplies the
# compiler. build-sniper.sh fetches libc++ sources matching *this* clang's version and builds them against the
# sysroot, so the clang version here is what ends up statically linked into libbg3le.so.
#
# The docker CLI is included because build-sniper.sh pulls and exports the sniper SDK image itself; the host's
# docker socket is mounted in by tools/build_bg3le.sh.
FROM debian:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
        clang lld llvm cmake ninja-build make git curl ca-certificates libarchive-tools \
        libc++-dev libc++abi-dev python3 file binutils docker-cli \
    && rm -rf /var/lib/apt/lists/*
