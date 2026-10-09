# bg3-ai: an AI that plays Baldur's Gate 3

A mod plus a harness that let an LLM agent (Claude Code) play **Baldur's Gate 3** end to end:

* it **sees** what the player sees: fair, visibility-filtered game state as compact text, plus the real
  screen on demand;
* it **acts** like a player: the game's own UI commands, engine actions, and input as a fallback;
* it **remembers**: a git-tracked Markdown memory carried across ~1-hour sessions;
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

Manual play session: `cd play && claude` (the `bg3` MCP server, rules, skills and hooks are configured there).

## Rules the system enforces

* **Fairness.** The mod filters state server-side with the engine's own perception queries and projects
  only fields the in-game UI shows. The playing agent has no shell and no web access, and can only read or
  write its `memory/` and `knowledge/` folders.
* **Efficiency.** State and actions go through the engine as text. Screenshots are opt-in. GUIDs become
  short handles (`e12`).
