# bg3-ai: an AI that plays Baldur's Gate 3

A mod plus a harness that let an LLM agent (any MCP client; the runner drives Claude Code by default, `--client
codex|gemini` are wired too) play **Baldur's Gate 3** end to end:

* it **sees** what the player sees: fair, visibility-filtered game state as compact text, plus the real
  screen on demand;
* it **acts** like a player: the game's own UI commands, engine actions, and input as a fallback;
* it **remembers**: a git-tracked Markdown memory carried across sessions of up to an hour (`--session-minutes`, default 60);
* it **runs unattended**: a supervisor launches the game, runs sessions, recovers from crashes, and
  shows a live dashboard.

It runs on the **native Linux** build of BG3 (Vulkan, Steam), using
[bg3le](https://github.com/lenonk/bg3le), a native-Linux port of Norbyte's Script Extender.

Start here: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The mod↔bridge contract is
[docs/PROTOCOL.md](docs/PROTOCOL.md). The research behind every decision is in [research/](research/).

## Quick start

```bash
git submodule update --init third_party/bg3le
tools/build_bg3le.sh                         # builds build/bg3le/dist/libbg3le.so (Docker, sniper toolchain)
runner/launch_game.sh --continue             # native BG3 + bg3le + our mod + capture layer (Steam must run)
cd bridge && PYTHONPATH=src python3 -m bg3ai.luadebug 'return Mods.BG3AI.BG3AI.rpc([[{"op":"sys.info"}]])'

python3 tools/memory/new_playthrough.py claude-run-1      # memory repo + play/memory symlink
python3 runner/play.py run --playthrough claude-run-1     # autonomous sessions
python3 runner/dashboard/serve.py --port 8765             # watch it play: http://localhost:8765
```

Manual play session: `cd play && claude --tools ToolSearch` (the `bg3` MCP server and the rules are configured
there; memory, the sandbox and the session's start and end are bg3 tools, so any MCP client works the same way).
A client in a container reaches the server over HTTP: `tools/container/README.md`.

## Rules the system enforces

* **Fairness.** The mod filters state server-side with the engine's own perception queries and projects
  only fields the in-game UI shows. The playing agent has no shell and no web access (bg3.wiki only through the
  rate-limited wiki tool). It reads and writes its `memory/` and only reads `knowledge/`, through the MCP server's
  memory tools, which enforce that sandbox.
* **Efficiency.** State and actions go through the engine as text. Screenshots are opt-in. GUIDs become
  short handles (`e12`).

## Machine configuration

Nothing is tied to one machine; every path has an env override and a default found at run time:

| What | Env | Default |
|---|---|---|
| game install | `BG3_DIR` | the Steam library listing app 1086940 (`tools/steam_paths.py game`) |
| Steam's sniper runtime | `BG3AI_SNIPER_DIR` | its library per `libraryfolders.vdf`, else next to the game |
| Steam root | `STEAM_ROOT` | `~/.steam/steam`, `~/.steam/debian-installation`, `~/.local/share/Steam`, Flatpak |
| Larian profile | `BG3AI_PROFILE_DIR` | `~/.local/share/Larian Studios/Baldur's Gate 3` (`steam_paths.py profile`) |
| saves / Script Extender files | `BG3AI_SAVE_DIR` / `BG3AI_SE_DIR` | `<profile>/PlayerProfiles/Public/Savegames/Story` / `<profile>/Script Extender` |
| runtime logs, shots, metrics | `BG3AI_RUNTIME` | `<repo>/runtime` |
| playthroughs (memory repos) | `BG3AI_PLAYTHROUGHS` | `<repo>/playthroughs` |
| NVIDIA PRIME offload | `BG3AI_PRIME=1\|0` | auto: on when an NVIDIA GPU and its Vulkan ICD are present |
| Steam must be offline | `BG3AI_REQUIRE_STEAM_OFFLINE=1\|0` | off, or on when `~/.config/bg3ai/require-steam-offline` exists |

Host requirements the code assumes: Linux with systemd user sessions (`systemd-run --user --scope` for the game's
cgroup, `journalctl --user` for OOM reasons; both degrade to a warning without them), Mesa's
`VK_LAYER_MESA_screenshot`, Docker (bg3le build, Lua test suites). `runner/launch_game.sh` lists every knob.
Operator scripts for the dev machines (`tools/orchestrator/`) are untracked and not needed to run anything.
