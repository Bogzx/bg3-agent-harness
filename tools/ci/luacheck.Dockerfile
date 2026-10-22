# luacheck for tools/ci/lua_lint.py, pinned: the base image by tag and digest, each rock by version. The luarocks
# image has no C compiler and luafilesystem (a luacheck dependency) is a C module, so gcc comes in for the build.
FROM nickblah/lua:5.4.9-luarocks@sha256:c9ccaeb9589bb0725f926713a1ad42ca2c877bfa6e1f0a1e6d7efc58d564640c
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/* \
 && luarocks install argparse 0.7.1 \
 && luarocks install luafilesystem 1.9.0 \
 && luarocks install luacheck 1.2.0 \
 && luacheck --version
