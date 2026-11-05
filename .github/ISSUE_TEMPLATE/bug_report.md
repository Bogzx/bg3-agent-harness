---
name: Bug report
about: Something in the mod, bridge or runner doesn't work
labels: bug
---

### What you ran

The command or tool call, and the playthrough or scenario.

### What happened

The error, output or behaviour. Paste the relevant lines from `runtime/sessions/<run>/` or the game log.

### What you expected

### Setup

- Game build (`python3 tools/steam_paths.py buildid`):
- Repo commit (`git rev-parse --short HEAD`):
- Distro, GPU and driver:
- MCP client and model:
- bg3le build (`python3 tools/bg3le_buildinfo.py` output):
- Launch command (`runner/launch_game.sh --dry-run` output, with the flags you used):

### Logs

For a crash or a game that won't start, attach the game log and the bg3le logs of that launch:
`runtime/logs/game-<stamp>.log` and `runtime/logs/bg3le-<stamp>.log.<pid>` (the launcher prints their paths when
the game exits). The lines that matter most:

    grep -a -E 'crash:|WARNING: resolve' runtime/logs/bg3le-<stamp>.log.*
