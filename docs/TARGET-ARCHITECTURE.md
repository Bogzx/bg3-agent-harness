# Target architecture: harness, benchmark, playthrough

Decided 2026-10-21 (user): the repo is reorganised into three packages with one-way dependencies. They live in this
repo first; each moves to its own repo once its API stops changing (the playthrough app first, the benchmark after
`bench-v1`). This file is the target; the research for how to get there is in `research/arch/` (in progress).

```
            ┌──────────────────────┐      ┌──────────────────────────┐
            │  2. benchmark        │      │  3. playthrough app       │
            │  scenarios, graders, │      │  UI, memory, keeper,      │
            │  stats, leaderboard  │      │  coaching, long runs      │
            └──────────┬───────────┘      └────────────┬─────────────┘
                       │  run_session(...)              │
                       ▼                                ▼
            ┌────────────────────────────────────────────────────────┐
            │  1. harness: game environment + agent clients           │
            │  mod, bridge MCP server, launch/reset/recovery, clients │
            └────────────────────────────────────────────────────────┘
```

The benchmark and the playthrough app import only the harness's public API, never each other. The harness imports
neither.

## 1. Harness (`bg3-agent-harness`)

Everything needed to put any agent in the game, with nothing tuned to one model.

- **Game environment:** the Lua mod (`mod/`), the bridge MCP server (`bridge/`), the screenshot layer and input
  shim (`native/`, `patches/`), bg3le (`third_party/`), game launch, save load, reset and crash recovery, the
  container sandbox (`tools/container/`), version pins.
- **Clients:** Claude Code, Codex CLI, Gemini CLI, and a **pi client** that works with just an endpoint, a model and
  an API key (OpenRouter, DeepSeek, any OpenAI-compatible endpoint). One `Client` interface, one normalized event
  log for all of them.
- **Rules text:** how the game, the tools and fairness work. No coaching, no session anecdotes, no client-specific
  wording.
- **State facts:** what the mod can read (area, quests, gold, inventory, level, HP, combat state). The harness
  reports facts; it does not decide what counts as winning.
- **Capabilities that callers switch on or off:** memory tools, wiki tools, `load_game`, screenshots.
- **The session API**, the single entry point for both other packages:

  ```
  run_session(client, model, system_text, tool_allowlist, start_state, deadline, caps) -> events + run record
  ```

## 2. Benchmark

Measures models on the harness. Depends only on the harness.

- Scenarios (start save or new game, goal, caps, brief), in four families: fights, sandbox goals
  (`act1_gold_1000` first), choices, exploration and puzzles. See [BENCHMARK-PLAN.md](BENCHMARK-PLAN.md).
- Graders: predicates over the harness's state facts (gold ≥ N, item held, hostiles dead, no fight, area reached).
- The series runner, per-run manifests, pins, the invalid-run rule (crash = rerun).
- Statistics (Wilson intervals, pass^k, bootstrap), reports, the success-vs-cost plot, the leaderboard.
- Validity kit: oracle runs, do-nothing and random baselines, per-tool fairness regression tests.
- Conditions: the ranked track runs every model through the pi client with the rules text only. Claude Code, Codex
  and Gemini CLI are a labelled "bring your own agent" condition; "rules + coaching" is another labelled condition.
- Releases are tagged (`bench-v0.1`, ...); every result cites a tag. Results, trajectories and videos are published
  outside git (release assets or a dataset), not committed.

## 3. Playthrough app

Plays the whole game for people to watch and use, with everything the agent needs for long runs. Depends only on
the harness.

- A good interface: pick model and client, persona or roleplay brief, difficulty; start, stop, resume; watch live
  (the screen, the agent's reasoning, party, map, current goal); browse the memory; a timeline of choices and fights;
  cost and latency. Today's dashboard (`runner/dashboard/`) is the starting point.
- The agent's long-run kit: coaching and playbooks, the memory template, the memory keeper, lessons, persona and
  judge, session handoff, multi-session resume and nudges.
- Playthrough data (`playthroughs/`), analysis miners (`tools/logs/`), stream overlay.

## Where today's code goes

| Today | Goes to |
|---|---|
| `mod/`, `bridge/`, `native/`, `patches/`, `third_party/`, `tools/container/` | harness |
| `bg3harness/clients.py`, launch, recovery, sandbox and pin code in `runner/play.py` / `runner/runs.py` | harness |
| rules parts of `play/CLAUDE.md`, `bridge/src/bg3ai/rules.py` | harness |
| `bridge/src/bg3ai/milestones.py` | facts to the harness, the milestone list and predicates to benchmark / playthrough |
| `scenarios/`, series code in `runner/play.py` / `runner/runs.py`, `tools/bench_preflight.py` | benchmark |
| coaching parts of `play/CLAUDE.md`, playbooks, `memory-template/`, `runner/memory_keeper.py`, `runner/judge.py`, resume and nudge code in `runner/play.py`, `runner/dashboard/`, `tools/logs/`, `playthroughs/` | playthrough app |

`runner/play.py` (about 5,500 lines) holds parts of all three and is the main piece of work. It is split in steps
behind the session API, not in one move. Directories are not renamed for the sake of it.

## Engineering standards (user, 2026-10-21)

Clean architecture, uv and current best practices, and extensive tests at every level, run in a CI pipeline.
Where this conflicts with how the code is today, the code moves toward this; new code starts here.

### Architecture

- **Pure core, adapters at the edges.** Scenario and event models, graders, statistics, prompt assembly and config
  parsing are pure functions over data, with no I/O, subprocesses, clocks or globals. Everything that touches the
  outside world (the game, MCP, the model clients, the filesystem, HTTP, time) sits behind a small `Protocol` and is
  passed in, so tests can swap in a fake.
- **Contracts are data with versions.** The normalized event log, `facts.jsonl`, the run manifest, `result.json`
  and the client profile each have a `v` field and a JSON Schema in the repo, validated in tests. The benchmark and
  the app read these files; they never reach into harness internals.
- **The dependency rule is enforced** by import-linter contracts plus a test that bans `sys.path` edits and
  cross-package file paths.
- **Small modules with one job.** The two monoliths are split over time behind stable interfaces:
  `runner/play.py` (~5,500 lines) along the package lines above, and `bridge/src/bg3ai/server.py` (~11,000 lines)
  into tool-group modules. No new code goes into either.
- **1000-line cap per file** (user, 2026-10-21) for every source and test file (Python, TypeScript, Lua, shell).
  A CI test enforces it. The 25 tracked files above the cap on 2026-10-21 (largest: `bridge/src/bg3ai/server.py`
  11,152, `runner/play.py` 5,575, `tools/dev/test_act_lua.lua` 4,154, `Server/Navigation.lua` 4,114) are listed in
  a legacy allowlist with their current length. A listed file may only shrink, the list may only get shorter, and
  no new file joins it. A pure data table (such as `Server/Data/JournalMarkers.lua`) may be exempt, with its reason
  written in the allowlist. The target is an empty list.
- **Explicit state, no hidden globals.** Dependencies are passed in (constructor or function arguments), and
  configuration comes from files and environment variables read in one place.
- **Errors are not swallowed.** No retry or fallback that hides a bug; unknown cost is an error, not 0; a crash is
  a recorded invalid run.

### Tooling

- **uv workspace** at the repo root with one committed `uv.lock`; CI and scripts use `uv sync --locked` and
  `uv run`. Python ≥ 3.12, `src/` layout per package, dependencies declared per package, dev tools in
  `[dependency-groups]`.
- **ruff** for lint and format (one config at the root), **pyright** for types (strict on the new packages,
  basic on legacy code until it is touched), **import-linter** for the dependency rule.
- **pytest** as the runner (existing `unittest` tests keep working under it), with markers `unit`, `integration`,
  `e2e`, `live`. Coverage is measured per package; the floors are in the root `pyproject.toml`
  (`[tool.bg3ci.coverage-min]`, checked by `tools/ci/coverage_gate.py`) and only go up.
- TypeScript (pi extension, app frontend): `npm ci`, `tsc --noEmit`, eslint, vitest; Playwright for browser
  tests. Lua: the existing Docker suites (`tools/dev/test_*.sh`).
- Pinned versions everywhere: pi via `tools/pi/package.json` + lockfile, bg3le by submodule, the game by buildid.
- **The offline suite** is three commands from the repo root, the same ones CI runs: `uv run pytest -m "not live
  and not e2e"` (bridge, runner, tools, every package; about 10 minutes on dev2), `uv run pytest -m "e2e and not
  live"` (the web app in Playwright after `npm run build` in `packages/playthrough/web`, and the bench pipeline
  through pi after `npm ci` in `tools/pi`; its own CI job) and `bash tools/ci/lua_suites.sh` (every
  `tools/dev/test_*.sh` and `tools/vis/test_*.sh` under Docker; known-broken suites are listed in it with the
  reason). TypeScript is checked per package (`npm ci --ignore-scripts`, `tsc --noEmit`, `npm run check`, `npm test`)
  in `packages/playthrough/web` and `tools/pi`. Tests that need something a fresh clone lacks (a local file, the
  web build) are skipped with the reason by the root `conftest.py`, never silently. These replace
  `tools/orchestrator/suite_wt.sh`'s Python part.
- Secrets only in gitignored `.env` files; tests never need a real key.

### Tests

Every change ships with tests, and every bug fix with a test that fails without it. Tests are deterministic: fake
clocks instead of sleeps, no timing races, no network.

| Level | What it covers | Fakes used | Where it runs |
|---|---|---|---|
| **Unit** | pure functions: graders, stats (against known values), event parsing per client, prompt assembly, schema validation, config, milestone predicates | none | CI, every push; seconds |
| **Integration** | one real component against fakes: the bridge MCP server against a fake mod (a TCP endpoint replaying recorded Lua replies); each client (pi, Claude Code, Codex, Gemini) against a stub MCP server and a scripted model (pi's offline `faux` provider, fake CLI streams); `run_session` with a fake client and a fake game process (deadline, wrap-up, kill, crash ⇒ invalid, caps); graders on recorded `facts.jsonl` from real runs; the app API on recorded run data; the Lua mod in the Docker suites | stub MCP, fake mod, faux model, recorded fixtures | CI, every push; minutes |
| **E2E, offline** | the whole pipeline with no game and no paid model: scenario file → `run_session` → pi with a scripted faux model → real bridge → fake mod → `facts.jsonl` → grader → `result.json` → stats → report and leaderboard build; plus the app in a browser (Playwright) against the same run's output | fake mod, faux model | CI, every push; a few minutes |
| **E2E, live** (later, not in the target CI for now) | real BG3 on dev2: a scripted oracle agent through a short scenario; the existing `tools/dev/acceptance.py` checks | none | by hand under `runtime/game.lock` for now; the stress-test round (BACKLOG) covers it |

- **Golden fixtures:** real sessions recorded once (raw client streams, Lua replies, `facts.jsonl`) and checked in
  small, so parsers, graders and the fake mod are tested against what the game actually produces.
- **Contract tests** on every versioned schema: old fixtures must still parse, or the version is bumped on purpose.
- **Fairness tests:** for each tool, a query about something known to be hidden in a fixture must be refused.
- **Validity tests:** the do-nothing and random baselines must fail every scenario's grader.

### CI pipeline (part of the target)

GitHub Actions on GitHub-hosted runners, on every push and pull request, and a pull request merges only when it is
green: `uv sync --locked` → ruff (lint + format check) → pyright → import-linter → the 1000-line cap check →
pytest `unit` + `integration` + `e2e` (offline) with coverage → Lua Docker suites → TypeScript checks (tsc,
eslint, vitest) → Playwright. No job needs the game, a GPU or an API key.

**Branch protection on `main`** (user, 2026-10-21), as a GitHub ruleset: changes land only through pull requests;
the CI checks above are required and must pass on the latest commit; no force pushes; no deletion; linear history.
Work happens on branches (one per worker or task) and is merged by PR. Active since 2026-10-21 as ruleset
`protect-main` (no bypass, 0 approvals, squash or rebase merge); the CI checks are added as required once CI
exists.

Live E2E is left out of CI for now. If it is added later, it runs only on a self-hosted dev2 runner, triggered by
hand or on a schedule from `main` and never from pull requests: the repo is public, and a fork's pull request must
not be able to run code on a machine with the game and keys.

## Rules for getting there

- **The whole overhaul happens on the long-lived branch `overhaul`** (user, 2026-10-21), created from `main` at
  471ee66, in its own worktree (`/data/Repos/bg3-ai-overhaul`), so the main checkout that the game and the runner
  load from stays on `main`. Each task gets its own branch from `overhaul` and is merged into `overhaul` by PR.
  When the overhaul is done, one PR merges `overhaul` into `main` (rebase merge), which makes `main` equal to the
  overhaul without a force push. If `main` gets commits in the meantime, `overhaul` is rebased onto `main` before
  that PR.

- One step at a time, with the offline suite green after each; no big-bang move.
- New code goes straight to its target package.
- Splitting the rules from the coaching comes early: the fair benchmark needs it.
- The dependency rule is checked by a test, not by convention.
