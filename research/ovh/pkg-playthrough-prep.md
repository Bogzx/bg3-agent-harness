# bg3play inventory (prep for the per-package phase)

First inventory of `packages/playthrough` (bg3play), the starting point of the playthrough team's analyze / E2E /
improve pass (user, 2026-10-22: per-package phase; found and app are that team). Taken 2026-10-22 on `overhaul`
49e76dc. Numbers are `wc -l` and the commands below; nothing here is a judgement yet beyond "Known gaps".

## Package

- `packages/playthrough/pyproject.toml`: `bg3play` 0.1.0, Python >= 3.12, depends only on `bg3-agent-harness`
  (workspace). It also imports `bg3ai` (the bridge) without declaring it (see gaps). Dev group: pytest, playwright
  1.64.0, jsonschema.
- Entry point: `bg3play-serve = bg3play.server:main` (the HTTP server, the only console script).
- Other ways in: `python -m bg3play.memory.{session,lint,new_playthrough,snapshots,keeper}` (the memory CLIs;
  tools/memory/*.py are 1-line re-export shims); `runner/legacy_playthrough.py` imports
  `bg3play.runner.playthrough.PlaythroughSide` (what `runner/play.py run` drives); `runner/dashboard/serve.py`
  imports `bg3play.server`; `runner/memory_keeper.py` re-exports `bg3play.memory.keeper`.
- Import-linter: bg3play may import bg3harness and bg3ai; bg3harness/bg3ai may not import bg3play; bg3bench and
  bg3play are independent (pyproject `[tool.importlinter]`, 2 contracts kept).
- Size: src 45 .py files, 10 173 lines; largest `memory/keeper.py` 968 (near the 1000 cap), `memory/session.py`
  823, `live.py` 661, `server.py` 621, `recorder.py` 618.

## Modules (src/bg3play)

| Module | Lines | Role (module docstring) |
|---|---:|---|
| server.py | 621 | stdlib HTTP server: `/api/v1/*` JSON, SSE `/api/v1/stream`, screen MJPEG/JPG, video byte ranges, serves web/dist; `main()` wires store, hub, supervisor, recorder, position sampler |
| api.py | 167 | v1 payload builders over the RunStore; every payload carries `"v"` |
| store.py | 308 | the run store: one reader over `runtime/` and `playthroughs/` |
| parse.py | 330 | pure parsers of the files a run leaves |
| hub.py | 196 | SSE hub: file changes -> numbered events, Last-Event-ID resume |
| tail.py | 60 | byte-offset tailing (runner/dashboard/feeds.py re-exports it) |
| live.py | 661 | the live views the retired runner/dashboard showed (session clock, game status, memory, RAM) |
| dashboard.py | 174 | the retired dashboard's routes, served by the app |
| game.py | 154 | live game view: state.status, visible events, screen frames |
| positions.py | 189 | party positions sampler -> positions.jsonl (breadcrumb map) |
| recorder.py | 618 | ffmpeg recorder: rolling MP4 segments + MJPEG tee |
| replay.py | 103 | Replay screen data: events on one wall-clock axis + covering video |
| story.py | 232 | Story screen from the memory |
| bench.py | 62 | reads a bench run's manifest/result (files only, no bg3bench import) |
| config.py | 309 | `playthroughs/<pt>/playthrough.toml`, the one file the app edits |
| client_profiles.py | 65 | profiles/*.toml via bg3harness.profiles |
| supervisor.py | 297 | start/stop/resume a multi-session playthrough (SessionRunner protocol) |
| keeper_pass.py | 67 | the supervisor's Keeper (memory keeper between sessions) |
| jsontypes.py | 21 | JSON type guards |
| adapters/harness_runner.py | 483 | SessionRunner over bg3harness.session.run_session |
| adapters/agent_process.py | 141 | one agent process for run_session's hooks |
| adapters/launched_game.py | 138 | a game the app launches/relaunches |
| adapters/legacy_cli.py | 185 | TEMPORARY SessionRunner over `runner/play.py run` (`--session-runner legacy`) |
| adapters/legacy_paths.py | 24 | TEMPORARY: bridge/src and runner/ on sys.path (boundary blocker) |
| adapters/bridge_status.py | 69 | TEMPORARY: state.status straight from the mod's RPC |
| adapters/session_cost.py | 47 | session cost from events.v1 |
| memory/keeper.py | 968 | memory keeper (`claude -p` pass between sessions) |
| memory/keeper_files.py | 317 | keeper's write rules on the memory dir (sys.path blocker) |
| memory/keeper_spend.py | 74 | keeper spend for paid clients |
| memory/session.py | 823 | memory session lifecycle (bridge memtools uses it via tools/memory) |
| memory/lint.py | 477 | memory lint |
| memory/memlib.py | 316 | shared memory helpers (stdlib) |
| memory/new_playthrough.py | 97 | new playthrough from memory-template/ |
| memory/snapshots.py | 392 | memory/snapshots/*.md from live game state (sys.path blocker) |
| runner/playthrough.py | 22 | PlaythroughSide: what bg3harness.runner.core.SessionRunner calls |
| runner/loop.py | 220 | the continuous `play.py run` loop |
| runner/session_side.py | 215 | one playthrough session |
| runner/keeper_side.py | 191 | between sessions: plan-line check, keeper, review |
| runner/memory_side.py | 175 | memory session start/stop |
| runner/memory_session.py | 49 | pure parts of the memory session |
| runner/prompts.py | 100 | kickoff and notes prompts |

## HTTP API (server.py `App.route` + handler)

`/api/v1/status`, `/api/v1/playthroughs[/<pt>/...]` (incl. `/story`), `/api/v1/recorder`, `/api/v1/supervisor`,
`/api/v1/video/index`, `/api/v1/stream` (SSE), `/api/v1/screenshot`, `/api/v1/live/screen` (+ legacy
`/api/screen`), `/api/v1/screen.mjpg|.jpg`, the dashboard routes (dashboard.py), static web/dist. Payloads are
versioned (`"v"`), but there is no JSON Schema for them yet (TARGET-ARCHITECTURE asks for versioned contracts with
schemas; check during analysis).

## Web app (packages/playthrough/web)

Svelte 5.57 + Vite 8 + TypeScript 5.9, vitest 5. `npm run check` = svelte-check --fail-on-warnings + tsc.
- `src/App.svelte` (147, router), `Overlay.svelte` (119, stream overlay), `main.ts`, `app.css`.
- screens/: Watch 255, Stats 296, Replay 223, Launch 173, Memory 172, Bench 158, Timeline 123, Story 109,
  Breadcrumb 51.
- lib/: types.ts 572 (hand-written API types; nothing checks them against the server's payloads), api.ts 88,
  watch/bench/memory/stats/timeline/breadcrumb/launcher/replay/route/overlay/format .ts, each but format.ts and
  api.ts with a `.test.ts`.
- Built output web/dist is gitignored; CI builds it in the e2e job.

## Tests

- Python: 471 offline tests (`uv run pytest -q -m "not live and not e2e" packages/playthrough/tests`: 471 passed,
  14 deselected, 60 s). Largest: memory/test_session 64, memory/test_keeper 48, memory/test_lint 41, test_config 40,
  test_live 28, test_store 25, test_recorder 22 (fake ffmpeg in tests/bin), memory/test_snapshots 20,
  test_supervisor 18, test_server 18, test_harness_runner 17.
- e2e (Playwright, `-m e2e`, needs `npm run build`): 14 tests. tests/e2e/test_web.py 11 (Watch, Memory, Stats,
  Story, Timeline, Launcher x3, Overlay, Replay) against a recorded fixture run (tests/fixtures/run, claude-run-2);
  tests/e2e/test_playthrough_offline.py 3 (two sessions through run_session with the keeper between, the API shows
  both, Watch/Timeline/Replay screens).
- Web: 41 vitest cases in 10 files under web/src/lib. No component tests for the .svelte screens (only e2e).
- Also exercised from outside: runner/test_play*.py and runner/test_scenarios.py import bg3play.runner.prompts /
  session_side; runner/dashboard/test_serve.py starts bg3play.server.

### Coverage per module (line+branch, package tests only)

`uv run pytest -q -m "not live and not e2e" packages/playthrough/tests --cov=packages/playthrough/src --cov-branch`:
TOTAL 86.7 %. The CI gate (`[tool.bg3ci.coverage-min]` "packages/playthrough/src" = 89.5) measures the whole
offline run, where runner/ tests add coverage of bg3play.runner.

| Module | Cover | Module | Cover |
|---|---:|---|---:|
| runner/loop.py | **5.6** | memory/session.py | 90.1 |
| runner/session_side.py | **10.2** | memory/keeper_spend.py | 90.0 |
| runner/keeper_side.py | **17.3** | memory/snapshots.py | 90.4 |
| runner/memory_side.py | **18.5** | memory/keeper.py | 91.7 |
| adapters/harness_runner.py | 87.2 | recorder.py | 92.5 |
| server.py | 92.7 | memory/keeper_files.py | 92.9 |
| live.py | 93.4 | store.py | 94.1 |
| lint.py | 94.2 | launched_game.py | 94.3 |
| legacy_cli.py | 94.5 | dashboard.py | 94.9 |
| hub.py | 95.2 | new_playthrough.py | 95.3 |
| parse.py | 96.0 | tail.py | 96.1 |
| memlib.py | 96.3 | agent_process.py | 96.8 |
| story.py | 96.9 | session_cost.py | 97.3 |
| replay.py | 97.8 | config.py | 98.3 |
| positions.py | 98.7 | game.py | 99.1 |
| supervisor.py | 99.6 | prompts.py | 95.0 |

100 %: api, bench, client_profiles, jsontypes, keeper_pass, bridge_status, legacy_paths, memory_session,
runner/playthrough.

## CI jobs touching bg3play (.github/workflows/ci.yml)

- Lint and types: ruff check/format, legacy lint lists, pyright (strict on packages/), import-linter, boundary scan
  (tools/ci/test_boundaries.py), 1000-line cap.
- pytest (offline): its 471 tests + the runner tests that import it, coverage gate 89.5.
- e2e: builds web/dist, installs chromium + ffmpeg, runs the 14 e2e tests (with the bench offline pipeline).
- TypeScript: npm ci, tsc --noEmit, svelte-check, vitest in packages/playthrough/web.

## Known gaps (to start the analysis from)

1. Boundary blockers (the scan's 3 bg3play hits): adapters/legacy_paths.py:18, memory/keeper_files.py:22,
   memory/snapshots.py:33 put bridge/src or runner/ on sys.path; app r15 is removing them. keeper_files.py:28 still
   runs tools/memory/session.py by path.
2. `bg3ai` is imported but not declared in pyproject dependencies.
3. bg3play.runner (loop, session_side, keeper_side, memory_side: 811 lines) is 5-19 % covered by its own tests; it
   was moved from runner/legacy_playthrough.py in D.2 and is tested only through runner/test_play*.py. Needs tests
   in the package (fake game, fake clients).
4. Three TEMPORARY adapters (legacy_cli, legacy_paths, bridge_status) are marked "remove in Phase D".
5. memory/keeper.py is 968 lines, just under the cap: split before it grows.
6. API payloads have no JSON Schema and web/src/lib/types.ts is hand-kept in sync with them.
7. No component tests for the Svelte screens; screens are covered only by the 14 Playwright tests.
8. Live status (README, 2026-10-21): built and tested offline; the recorder ran live once; the supervisor and the
   Launcher have never started a real session. That is the first E2E target for the team.
9. pyright error in tests/test_playthrough_runner.py:78 (merge_refused partially unknown) keeps the CI lint job red.
