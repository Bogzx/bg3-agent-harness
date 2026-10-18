# bg3-ai: an AI that plays Baldur's Gate 3

A mod and a harness that let an LLM agent play **Baldur's Gate 3** on the native Linux build. The mod runs inside
the game through [bg3le](https://github.com/lenonk/bg3le) (a Linux port of Norbyte's Script Extender) and gives
the agent only what a human player could see, as compact text, plus the real screen when it asks. The agent acts
through the game's own UI commands, engine actions and mouse/keyboard input, keeps a Markdown memory between
sessions of up to an hour, and is supervised by a runner that launches the game, recovers from crashes and shows
a live dashboard. The agent is an MCP client: the runner drives Claude Code by default.

**Status: work in progress.** It plays the whole game, but it is a research harness, not a polished product.
Setup assumes Linux, Steam and some patience, and things break when the game updates.

## What it has done

- **A full playthrough.** The playthrough `claude-run-2` went from a new game to the epilogue between 2026-10-09
  and 2026-10-18: 96 numbered sessions (99 counting restarts), about 39 hours of play and about $710 of model cost
  (Claude Opus 5.5 at medium effort, including the memory-keeper calls), on Explorer difficulty, mostly
  unattended. The harness was fixed and improved while the run went on, so this is a demonstration that the
  whole game can be finished through it, not a controlled measurement.
- **The Nautiloid series.** Nine scripted runs of the opening chapter (new game until the party escapes the
  Nautiloid) in three series of three, on 2026-10-13, 2026-10-16/17 and 2026-10-17: 9 of 9 reached the goal. The
  mean cost per run fell from $4.34 in the first series to $1.46 in the third as the harness got better (9 to 13
  minutes of wall time per run throughout).

## Known limits

- Explorer, the easiest difficulty.
- Many fights were won only after reloading and retrying.
- About 12% of tool calls in claude-run-2 returned a failure or a refusal (some only for part of a batch of
  actions). The agent usually recovered, but it cost time.
- Until 2026-10-17 a harness bug let the agent cast spells its characters had not prepared. It is fixed, but the
  run before that point had the advantage.
- Only Claude has played it. The Codex and Gemini wiring exists and has not played a live session.
- The agent-facing text (the rules, playbooks and tool descriptions) was tuned on Claude's failures, so it is not
  a neutral benchmark yet: see [docs/BENCHMARK-BIAS.md](docs/BENCHMARK-BIAS.md).

## Goals

Near term: make this a benchmark other agents and models can be measured on.

- A small suite of short, cheap scenarios, for example a boss fight, the invisible-floor Faith-Leap trial and an
  exploration leg, each scored on success, attempts, cost and time; the full playthrough stays an occasional
  showcase. The series runner, run records and pinned builds already exist
  ([docs/BENCHMARK.md](docs/BENCHMARK.md), [scenarios/README.md](scenarios/README.md)); only the Nautiloid
  scenario has been run.
- Any MCP client, not only Claude Code (`--client codex|gemini` are wired, untested live).
- A container sandbox that lets the playing client reach only the game's MCP server and its own model API
  ([tools/container/README.md](tools/container/README.md); built, not yet run on a real host with the firewall).
- Split the prompts into rules (how the game, the tools and fairness work) and coaching (advice written after one
  model's mistakes), and take Claude's session anecdotes out of the agent's text.

Longer term:

- Roleplay. The agent should play a character, not just win: each playthrough gets a character brief that the
  agent turns into a build and a persona at character creation (kept in the memory's `CHARACTER.md`), and its
  choices are meant to follow it. A judge model can score how well a run's decisions fit the persona
  (`runner/judge.py`, not yet run live).
- Harder difficulties, other characters and classes, and held-out starts that the prompts were never tuned on.
- A live feel: the agent should play at the pace of a skilled human watching the screen. Latency (idle time
  between actions, long gaps, round trips) is measured every session.
- Something good to watch: a stream overlay built on the dashboard's feeds.
- Publishing the playthrough data (session transcripts, metrics, memory).

## How it works

```
 Claude Code / any MCP client  --MCP-->  bridge (Python MCP server "bg3")  --Lua eval over TCP-->  mod (Lua, in the game via bg3le)
                                           |-- screenshots: Mesa's Vulkan screenshot layer
                                           |-- mouse/keys: an SDL2 LD_PRELOAD shim (game window only)
 runner/play.py: launches the game, runs sessions, recovers, records metrics; runner/dashboard: live view
```

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the design and why each choice was made.
- [docs/FAIRNESS.md](docs/FAIRNESS.md): what the agent may see and how the mod filters state with the engine's own
  perception checks. The playing agent has no shell and no web access except a rate-limited bg3.wiki tool.
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the mod/bridge contract.
- [play/CLAUDE.md](play/CLAUDE.md): the playing agent's rules; [play/knowledge/](play/knowledge/) its playbooks.
- [research/](research/): the research notes behind the design.

## Requirements

- Baldur's Gate 3 bought on Steam and installed through it, the **native Linux build** (not the Windows build
  under Proton). Built and tested on Steam buildid 25605617 (Patch 8 Hotfix 10); other builds may need bg3le
  re-resolved.
- Linux with systemd user sessions, Mesa's `VK_LAYER_MESA_screenshot` Vulkan layer, Docker (to build bg3le and
  run the Lua tests), Python 3 and [uv](https://docs.astral.sh/uv/).
- 16 GB RAM and a 4 GB+ GPU (tested on an NVIDIA RTX 3050 laptop GPU under PRIME offload, and on an AMD RX 6900 XT).
- An MCP client for the agent, by default Claude Code, and the API budget to run it.

## Quick start

Full setup and checks: [docs/SETUP.md](docs/SETUP.md).

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
A client in a container reaches the server over HTTP: [tools/container/README.md](tools/container/README.md).
Runner options: [runner/README.md](runner/README.md).

### Machine configuration

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

The game runs in its own systemd user scope with a memory cap (`systemd-run --user --scope`), and OOM reasons come
from `journalctl --user`; both degrade to a warning without systemd. `runner/launch_game.sh` lists every knob.

## Credits

- [bg3le](https://github.com/lenonk/bg3le) by lenonk (MIT), the native-Linux port of
  [Norbyte's Script Extender](https://github.com/Norbyte/bg3se). It is a git submodule in `third_party/bg3le`,
  built with our crash-fix patches from `patches/bg3le/`.
- [bg3.wiki](https://bg3.wiki) for the wiki the agent can look things up in.
- Baldur's Gate 3 is made and owned by Larian Studios. Nothing from the game is included here; you need your own
  copy on Steam.

## License

MIT, see [LICENSE](LICENSE). `third_party/bg3le` keeps its own MIT license.
