# 01b: bg3le in practice: build, launch, mod loading

Follows the decision in `01-runtime.md`: native Linux build plus bg3le (pinned) as the primary runtime, with
official bg3se under Proton as the fallback. Everything here comes from the pinned source
(`third_party/bg3le` @ `3728a68`, v0.3.6) and from this machine. **Nothing here has been tested against the
running game yet**; items that depend on a live run are marked **UNVERIFIED**.

## 1. Source pin

```
third_party/bg3le            -> https://github.com/lenonk/bg3le @ 3728a68 (Release v0.3.6, 2026-10-08)
third_party/bg3le/client     -> https://github.com/lenonk/bg3lua @ c6901fa (bg3le's nested submodule: console/debugger client)
```
Set up with `git submodule add https://github.com/lenonk/bg3le third_party/bg3le`, then `git checkout 3728a68`
and `git submodule update --init --recursive`. To bump the pin, check out another commit in the submodule and
commit the superproject. Read `CHANGELOG.md` first; the debug-server protocol and the plugin ABI are the parts we
depend on.

## 2. Build

The host has no clang, cmake, ninja or bsdtar, so `tools/build_bg3le.sh` runs bg3le's own
`tools/build-sniper.sh` inside a throwaway toolchain image (`tools/bg3le-builder/Dockerfile`: Debian trixie with
clang 19, cmake, ninja, libc++, bsdtar and the docker CLI). The host's docker socket is mounted in, because
build-sniper.sh pulls and exports the Steam Runtime sniper SDK image itself. The container runs as the calling
user, so every file stays owned by us.

```
tools/build_bg3le.sh            # incremental; first run takes a while (sysroot, LLVM sparse clone, libc++, abseil, protobuf)
tools/build_bg3le.sh --clean    # wipe build/bg3le and start again
```

What build-sniper.sh does, in order (each step is cached):
1. If `external/third_party/` is missing, runs `tools/fetch-externals.sh`. This fetches Noesis 3.1.7 headers from
   bg3se-updates.norbyte.dev, plus glm, imgui, Norbyte's lua fork, rapidjson, tinycrypt, Vulkan-Headers and
   optick, and builds host abseil (20260817.0) and protobuf (v36.1). These land in the **submodule's** gitignored
   `external/third_party/`.
2. Pulls `registry.gitlab.steamos.cloud/steamrt/sniper/sdk:latest` and unpacks it as a sysroot (glibc 2.31).
3. Sparse-clones `llvm-project` at the tag matching the builder's clang (`llvmorg-19.1.7`) and builds static
   libc++/libc++abi against the sysroot, with `-D_LIBCPP_ABI_VARIANT_INDEX_TYPE_OPTIMIZATION` (the game's
   `std::variant` layout).
4. Builds abseil and protobuf-lite against that libc++.
5. Builds `libbg3le.so`, then **fails if it asks for any glibc symbol newer than 2.31**.

Outputs (all under the gitignored `build/bg3le/`):

| Artifact | Path | What it is |
|---|---|---|
| **LD_PRELOAD library** | `build/bg3le/dist/libbg3le.so` (copied from `build/bg3le/sniper/bg3le/libbg3le.so`) | The whole extender. libc++, abseil, protobuf, Lua and **all Lua builtins (bg3le's prelude, the `Ext` library, the Events library, mod loader)** are compiled in. The builtins are raw string literals in `src/lua_host.cpp`, so there are **no separate Lua files to deploy**. |
| Console/debugger client | `build/bg3le/dist/bg3lua` | Python script from the `client/` submodule; it speaks the same LuaDebug protocol as our `bridge/src/bg3ai/luadebug.py` |
| Build record | `build/bg3le/dist/BUILDINFO` | commit, clang version, sha256, newest glibc version, NEEDED libraries |
| Caches | `build/bg3le/sniper/{sysroot,llvm-project,libcxx,abseil,protobuf-build,bg3le}` | delete one directory to redo that step |
| Build log | `build/bg3le/build.log` | |

Not built (not needed): bg3le's `install.py` and `installer/bg3le-launch` (we do not touch Steam's config), the
host-glibc `build/` variant, and the developer tools (`modsettings`, `pakfix`, `memgrep`; the CMake targets
exist if we ever want them).

## 3. Launch: `runner/launch_game.sh`

How bg3le gets in when installed normally: `install.py` writes `"<root>/bin/bg3le-launch" %command%` into
Steam's launch options (`localconfig.vdf`). Steam's command is reaper → `SteamLinuxRuntime_sniper/_v2-entry-point`
→ the game. The wrapper finds the `*/bin/bg3` argument and re-runs itself inside the container with `--inject`,
so `LD_PRELOAD` is set only for `bin/bg3` (gamescope and other wrappers break with it).

What we do instead, following bg3le's developer harness `run-native.sh`:
```
cd "<steamapps>/common/Baldurs Gate 3"
"<steamapps>/common/SteamLinuxRuntime_sniper/run" -- env SteamAppId=1086940 SteamGameId=1086940 \
    LD_PRELOAD=<repo>/build/bg3le/dist/libbg3le.so BG3LE_DEBUG=1 BG3LE_DEBUG_PORT=9998 BG3LE_CONSOLE=0 \
    BG3LE_LOG=<runtime>/logs/bg3le-<ts>.log [BG3LE_MOD_PATH=...] \
    VK_LOADER_LAYERS_ENABLE=VK_LAYER_MESA_screenshot \
    'VK_LAYER_MESA_SCREENSHOT_CONFIG=comms;control=bg3ai-shot;output_dir=<runtime>/shots;log_type=error' \
    ./bin/bg3 [-loadSaveGame <save> | -continueGame] [extra args]
```
* `SteamLinuxRuntime_sniper/run` is the same container entry point Steam uses (`toolmanifest.vdf`: `/_v2-entry-point
  --verb=%verb% --`). The runtime installed here is `sniper_platform_3.0.20260805.254768`. `env` runs **inside**
  the container, so the preload and the other variables reach only the game. `$HOME` (and so the repo and the
  runtime dir) is visible in the container by default.
* **The Steam app id.** `run-native.sh` writes `steam_appid.txt` into the game directory. Without it,
  libsteam_api "asks Steam to launch it properly", Steam starts a second copy without our preload, and the first
  exits with "command line was forwarded". We must not write into the Steam install, so the launcher passes
  `SteamAppId=1086940` and `SteamGameId=1086940`, the variables Steam sets for games it launches. **UNVERIFIED**
  that this alone satisfies BG3's Steam API restart check. The launcher detects an exit within 10 s, prints the
  log and explains the `steam_appid.txt` fallback; writing that file needs the user's OK. Steam must be running.
* The **debug server stays on**: `BG3LE_DEBUG=1` (its default anyway), port 9998 (`BG3LE_DEBUG_PORT`). The
  launcher refuses to start if 9998 is already listening. `BG3LE_CONSOLE=0` stops bg3le auto-opening a terminal
  running `bg3lua`; it would only do so with `CreateConsole` in a `ScriptExtenderSettings.json`, but we pin it off.
* **`ModCrashSanityCheck`.** Its exact path is `~/.local/share/Larian Studios/Baldur's Gate 3/ModCrashSanityCheck`,
  in the profile root, not under `PlayerProfiles/` (`src/preload.cpp: cleanup_sanity_check()`). The launcher
  removes exactly that directory before launch. bg3le also `rmdir`s it at startup, but only if it is empty.
* **Logs.** Game stdout and stderr go to `runtime/logs/game-<ts>.log` (the first lines record the exact
  command). bg3le's own log goes to `runtime/logs/bg3le-<ts>.log.<pid>`, one per process in the chain that loads
  it; the game's is the one that mentions `COsiris`.
* The script stays in the foreground until the game exits and writes `runtime/game.pid`. Run it with `&` or under
  a supervisor. `--dry-run` prints the command without launching.
* **`-loadSaveGame` argument format: UNVERIFIED.** The flag exists in the binary (`strings bin/bg3`:
  `-loadSaveGame`, `-continueGame`). Saves live in `PlayerProfiles/Public/Savegames/Story/<folder>/` with folder
  names like `pibooool-52412617951__AutoSave_33`. Try the folder name first, then the full path to the folder or
  its `.lsv`. `--continue` (`-continueGame`) is the safe option for the first test.
* **Screenshot layer.** The host has `/usr/share/vulkan/explicit_layer.d/VkLayer_MESA_screenshot.json`
  (`library_path: libVkLayer_MESA_screenshot.so`). pressure-vessel normally imports host Vulkan layers into the
  container, but **UNVERIFIED** that this explicit layer and its library dependencies make it in. If the
  `bg3ai-shot` socket never appears, check the pressure-vessel log (`PRESSURE_VESSEL_VERBOSE=1` via `--env`). The
  control socket is an abstract unix socket; the container shares the host network namespace, so the bridge can
  reach it.
* Optional knobs from run-native.sh, passed with `--env`: `SDL_VIDEODRIVER=wayland` (native Wayland surface;
  bg3le's author found it steadier. Default is whatever SDL picks, usually XWayland), `MANGOHUD=1`. Headless runs
  through `gamescope --backend headless` are possible but out of scope.

## 4. Talking to it: `bridge/src/bg3ai/luadebug.py`

A plain-socket client with one reader thread. Field numbers are taken from `LuaDebug.proto` (identical in bg3se
main and in bg3le's vendored copy) and checked against `src/debug_server.cpp`; the module docstring lists them.
```
PYTHONPATH=bridge/src python -m bg3ai.luadebug "return Ext.Utils.GameVersion()"       # client context
PYTHONPATH=bridge/src python -m bg3ai.luadebug --server "return Osi.GetHostCharacter()" # needs a loaded save
```
* Version negotiation: we offer 4 (bg3le). bg3le accepts anything. bg3se answers 5 and disconnects on a
  mismatch, so the client reconnects with the advertised version.
* On connect the client sends an empty `DbgUpdateSettings` (break on error off), and it auto-sends `DbgContinue`
  if a breakpoint is ever reported. Both matter only for bg3se; we must never leave the game paused.
* Errors: a Lua error raises `LuaDebugError(<lua message>)`; a bg3se refusal (`BkResult`) raises
  `LuaDebugError("EVAL_ENGINE_NOT_READY")`; no answer raises `TimeoutError`. bg3le gives up after 10 s on its side
  with "timed out waiting for the story thread; is the game paused or still loading?". The default client timeout
  is 15 s.
* Results are strings. bg3le returns every value `tostring()`'d and tab-joined. Return JSON from Lua
  (`return Ext.Json.Stringify(x)`) for anything structured.
* Tested offline only, against fake bg3le-style and bg3se-style servers (16 checks: negotiation, both contexts,
  errors, refusals, timeouts and recovery, 20 concurrent evals, output capture, breakpoint auto-continue).

## 5. Loading our own SE mod on bg3le

**Loose folders are supported, and they are the right development path.** No `.pak` and no modsettings.lsx
entry is needed (`src/lua_host.cpp`, "mod loading", around line 14630):

* **Roots**: every directory in `BG3LE_MOD_PATH` (`:`-separated), then `<ExtenderRoot>/mods`, where
  `ExtenderRoot` is the parent of the directory holding `libbg3le.so`. For us that is `build/bg3le/mods`, which
  is unused. `runner/launch_game.sh` sets `BG3LE_MOD_PATH` to `--mod-path`, else `$BG3AI_MOD_PATH`, else
  `<repo>/mod` if it exists.
* **Layout** under each root:
  ```
  <root>/Mods/<Folder>/meta.lsx                         # optional for bg3le: only the UUID is read (ModuleUUID, PersistentVars key)
  <root>/Mods/<Folder>/ScriptExtender/Config.json       # REQUIRED: {"RequiredVersion": 32, "ModTable": "BG3AI", "FeatureFlags": ["Lua"]}
  <root>/Mods/<Folder>/ScriptExtender/Lua/BootstrapServer.lua   # runs in the server context
  <root>/Mods/<Folder>/ScriptExtender/Lua/BootstrapClient.lua   # runs in the client context
  ```
  This is the same tree a `.pak` contains, so the folder can be packed unchanged for bg3se later. Without
  `Config.json` or a `ModTable` the mod is skipped with a warning in the bg3le log.
* **Order and overrides**: loose roots load **before** packed mods, and mods are deduplicated by `ModTable`, so a
  loose copy wins over an installed `.pak` with the same `ModTable`. Loose mods "load regardless: they are a
  development convenience and never appear in modsettings.lsx at all". They provide **scripts only**. The engine
  does not see them, so stats, localization, story goals or UI assets in a loose folder are **not** loaded; those
  would need a real `.pak` in the profile's `Mods/` plus a modsettings entry. Our mod is pure Lua, so this is fine.
* **Environment**: as upstream, each mod's globals live in `Mods.<ModTable>` (writing `function Foo()` defines
  `Mods.BG3AI.Foo`), `Ext.Require`/`require` resolve against the mod's own `Lua/` directory, and `ModuleUUID` is
  set. **Consequence for IPC:** a debugger eval runs in the real global scope, so the harness must call
  `Mods.BG3AI.<fn>`, or the mod must deliberately publish one entry point (`_G.BG3AI = ...`; check that the
  sandbox's `__newindex` allows it).
* **When scripts run**: the client bootstraps run when the game leaves module loading (before the main menu); the
  server bootstraps run once Osiris is bound, at session load. Then `SessionLoading`, PersistentVars restore and
  `SessionLoaded` fire.
* **Hot reload**: `Ext.Debug.Reset()` (or `reset` in bg3lua) rebuilds both Lua contexts and re-reads the loose
  files, which gives an edit, reset, test loop without restarting the game (README: "sessions and
  `Ext.Debug.Reset()` rebuild both"). **UNVERIFIED** that loose files are re-read rather than cached; test it.
* **Logs**: bootstrap loading prints `Loading bootstrap script: Mods/<Folder>/ScriptExtender/Lua/<file>` to the bg3le
  log and to debug output (field 8 in our client).

### modsettings.lsx (only for packed mods)
* Lives at `~/.local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/modsettings.lsx`. Paks go in
  `~/.local/share/Larian Studios/Baldur's Gate 3/Mods/` (the engine ignores paks in `<install>/Data/Mods`;
  bg3le `reference/MOD-LOADING.md`).
* bg3le does not edit it. It reads it and also loads scripts for paks enabled there but missing from the engine's
  order. Options are the in-game mod manager, hand-editing a `<node id="ModuleShortDesc">` entry (UUID, Folder,
  Name, Version64, MD5), or building bg3le's `modsettings` tool (`tools/modsettings.cpp`), which generates a
  load order from a directory of paks by reading each pak's `meta.lsx`.
* Gotchas from MOD-LOADING.md: **loading a save replaces the load order with the save's own module list** (it
  rewrote modsettings.lsx down to `GustavX` when a mod-free save was loaded); **one malformed pak stops the engine
  loading any mod**; and the sanity-check marker disables all mods (handled by the launcher).
* For the **bg3se/Proton fallback**, the same `Mods/<Folder>/` tree must be packed into a `.pak` and enabled in
  that prefix's modsettings.lsx. bg3se's documented developer path puts the loose `ScriptExtender\` plus
  `meta.lsx` under `<install>\Data\Mods\<ModName>` (`Docs/Debugger.md`). **UNVERIFIED** whether that also needs a
  load-order entry.

## 6. First live test checklist (for the orchestrator)

1. `tools/build_bg3le.sh` must have completed and `build/bg3le/dist/BUILDINFO` must exist.
2. Steam running. Then: `runner/launch_game.sh --dry-run`, then `runner/launch_game.sh --continue &`.
3. If the game exits within 10 s with "forwarded": the SteamAppId route failed (see §3).
4. When the main menu appears (the client context exists), run
   `PYTHONPATH=bridge/src python -m bg3ai.luadebug --output "return Ext.Utils.GameVersion()"`. Expect
   `v4.76.31.656`.
5. After the save loads, run `... --server "return Osi.GetHostCharacter()"`.
6. Check `runtime/logs/bg3le-*.log.*` for `WARNING: resolve:` lines (engine targets not found in this build).
7. Check that `runtime/shots/` receives a frame when `bridge/src/bg3ai/screen.py` requests one.
