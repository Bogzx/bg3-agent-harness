# bg3play: the playthrough app

Plays the whole game for people to watch: a launcher to pick the client, model, persona and difficulty, a live view
of the screen and the agent, the memory, a timeline of choices and fights, recording, a stream overlay and replay.
It reads the runner's files (`runtime/`, `playthroughs/`) and plays sessions through bg3harness `run_session`
(`--session-runner legacy`: `runner/play.py run`); it never imports the benchmark.

Version: `pyproject.toml` (0.1.0).

Install, flags, settings, screens, where runs and recordings live, and what needs the game: [docs/APP.md](../../docs/APP.md).

```bash
cd packages/playthrough/web && npm ci && npm run build      # once: the web app into web/dist
cd ../../..                                                 # back to the repo root
uv run --project packages/playthrough bg3play-serve --runtime runtime --playthroughs playthroughs
# open http://127.0.0.1:8780/
```

The shots below are from a real recorded run (playthrough `e2e-naut-claude`, 2026-10-22: Claude Code on Opus 5.5,
one 15-minute session through the Nautiloid), served offline from its copied `runtime/`.

Watch, after the session ended (the game capture shows only while a game runs):

![Watch](docs/watch.webp)

Replay, 7 minutes in: the recording next to the tool calls at that moment:

![Replay](docs/replay.webp)

Timeline: the session's fights (choices come from `decisions.md`, which this run left empty):

![Timeline](docs/timeline.webp)

Launcher, on a server started without `--supervise`:

![Launcher](docs/launcher.webp)

## Tests

```bash
cd packages/playthrough
uv run pytest -q tests                         # API, store, supervisor, recorder (fake ffmpeg), config
cd web && npm ci && npm run check && npm test  # svelte-check, tsc, vitest
cd .. && uv run pytest -q tests -m e2e         # Playwright against the built app (npm run build first)
```

## Status (2026-10-21)

Built and tested offline against a recorded fixture run. The recorder was checked live once with the game running;
the supervisor and the Launcher have not started a real session yet.
