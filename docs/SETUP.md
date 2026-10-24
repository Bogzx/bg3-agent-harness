# Setting up a machine

How to get from a fresh Linux box to a running game the agent can play. The README's quick start is the short
version; this file adds the checks and the gotchas. Tested on Debian 13 (KDE Plasma on Wayland with Xwayland),
first with an AMD RX 6900 XT (Mesa RADV) and then with an NVIDIA RTX 3050 4 GB under PRIME offload.

## 1. Steam and the game

Install Baldur's Gate 3 through Steam, the **native Linux build** (Larian's Vulkan port, `bin/bg3`), not the
Windows build under Proton. Steam installs **Steam Linux Runtime 3.0 (sniper)** with it.

Check the game version with `python3 tools/steam_paths.py buildid`. The scripts find the install through
`$BG3_DIR`, else Steam's `libraryfolders.vdf` (any library), else `~/.local/share/Steam`. Everything here was
built and tested on buildid **25605617** (v4.76.31.656, Patch 8 Hotfix 10). On another build bg3le may need its
addresses re-resolved: it logs `WARNING: resolve:` lines on a mismatch. [GAME-UPDATES.md](GAME-UPDATES.md) is the procedure for
moving to a new build.

If the same Steam account plays on another machine, keep Steam in offline mode on this one (an online launch
here kicks the other session). `touch ~/.config/bg3ai/require-steam-offline` (or `BG3AI_REQUIRE_STEAM_OFFLINE=1`)
makes the launcher refuse to start the game while Steam is online.

## 2. Packages

- `git`, `docker.io` (add your user to the `docker` group, then log in again), `python3`, `gcc`.
- `mesa-vulkan-drivers` **with the screenshot layer**: check that
  `/usr/share/vulkan/explicit_layer.d/VkLayer_MESA_screenshot.json` exists. Debian 13 needs Mesa from
  trixie-backports (26.x) for it. The layer works on NVIDIA too, under PRIME offload; check it without the game
  with `tools/dev/check_screenshot_layer.sh` (it runs vkcube).
- `uv` (https://docs.astral.sh/uv/).
- An MCP client for the agent. The runner drives Claude Code (`claude`) by default; `--client codex|gemini` are
  wired but not tested live.

On hybrid NVIDIA laptops `runner/launch_game.sh` turns on PRIME offload by itself (`BG3AI_PRIME=0` turns it off).

## 3. Clone and build

```bash
git clone <this repo> && cd bg3-agent-harness
git submodule update --init --recursive
tools/build_bg3le.sh                 # ~10-20 min the first time (Docker, ~10 GB RAM) -> build/bg3le/dist/libbg3le.so
native/bg3sdl/build.sh               # -> build/bg3sdl/libbg3sdl.so (the input shim)
(cd bridge && uv sync)
```

`tools/build_bg3le.sh` applies every patch in `patches/bg3le/` (crash fixes for this game build).
`python3 tools/bg3le_buildinfo.py` says whether `build/bg3le/dist` has all of them, and `runner/launch_game.sh`
refuses a stale build (exit 4) unless `BG3AI_ALLOW_STALE_BG3LE=1`.

Offline tests:

```bash
(cd bridge && uv run python -m unittest discover -s tests -p 'test_*.py')
python3 -m unittest runner/test_play.py
python3 -m unittest discover -s tools/memory -p 'test_*.py'
python3 -m unittest discover -s runner/dashboard -p 'test_*.py'
uv run --with lz4 --with zstandard python -m unittest tools/test_pak.py
tools/vis/test_lua.sh                # and tools/dev/test_*.sh: Lua suites, run in Docker
```

## 4. First launch

Steam must be running. `tools/dev/first_launch.sh` does one supervised launch and checks every layer in order
(install, builds, display, bg3le's Lua debug server, the mod, the GPU, the game's cgroup, one screenshot) and stops
at the first failure with the reason. With `-- --load <save>` (or `-- --continue`) it says READY only once the
game's own load of that save has finished, so don't load it again after READY. By hand:

```bash
runner/launch_game.sh
cd bridge && PYTHONPATH=src python3 -m bg3ai.luadebug 'return Mods.BG3AI.BG3AI.rpc([[{"op":"sys.info"}]])'
uv run --project bridge python tools/dev/smoke.py     # once a game is loaded: every read tool answers
```

The game needs a display. From a shell without one (ssh, a terminal multiplexer started outside the desktop
session) the runner finds the desktop's Xwayland itself (`bg3harness/display_env.py`); `DISPLAY` and `XAUTHORITY`
in the environment win.

The window layout doesn't matter: click coordinates come from the input shim's view of the game window.

## 5. A playthrough

```bash
python3 tools/memory/new_playthrough.py my-run          # playthroughs/my-run/memory (its own git repo) + play/memory
BG3AI_SAVE_PREFIX=Claude- python3 runner/play.py run --playthrough my-run --save-owner Claude --session-minutes 60
uv run --project bridge python runner/dashboard/serve.py --port 8765    # http://localhost:8765
```

`runner/README.md` has every runner option; `docs/BENCHMARK.md` has the scenario and series workflow.

## 6. Rules learned the hard way

- One game instance per machine. Don't hot-reload the mod (`Ext.Debug.Reset()`) or send live test actions during
  an agent session: a reset wipes the jobs in flight.
- The native `bg3` ignores SIGTERM. Stop it with SIGKILL after ~10 s (the launcher clears the
  `ModCrashSanityCheck` it leaves behind).
- Your own saves are safe: save and load are gated in the mod, the bridge (it only touches saves named with
  `BG3AI_SAVE_PREFIX`) and the runner (`--save-owner`).
- **Never write engine system arrays or structs through unverified Script Extender mappings** (`Ext.System.*`,
  component fields through `Ext.Entity`). The field offsets can be wrong for this game build: reads then return
  quiet garbage, but writes corrupt the game's heap and crash minutes later in unrelated code. On 2026-10-10 two
  pushes into `Ext.System.ServerLockpicking.PickLockRequests` (mapped 8 bytes off) overwrote the game's buffer
  pointer, and the game's own next lockpick request freed it (SIGSEGV). Before any such write, prove the layout
  on this build; prefer the game's own input route (clicks, keys, Osiris calls).
