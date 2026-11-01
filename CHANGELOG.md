# Changelog

The repo holds three packages (see [docs/TARGET-ARCHITECTURE.md](docs/TARGET-ARCHITECTURE.md)), each with its own
version:

| Package | Version lives in | Now |
|---|---|---|
| harness (mod, bg3 MCP server, runner, clients) | `packages/harness/pyproject.toml` (`bg3-agent-harness`; `bg3harness.__version__`) | 0.2.0 |
| benchmark | `packages/bench/pyproject.toml` (`bg3bench`; `bg3bench.__version__`) | 0.9.0 |
| benchmark suite | derived: `bench-vX.Y` of bg3bench `X.Y.Z` (`bg3bench.suite`), its files pinned in `packages/bench/suite.json` | bench-v0.9 |
| playthrough app | `packages/playthrough/pyproject.toml` (`bg3play`) | 0.1.0 |

The pyproject is the only place a version is written; `__version__` reads it (from the source tree, else from the
installed package).

How versions and tags work:

- A benchmark release is a git tag `bench-vX.Y` on the commit it was run from, and `bg3bench`'s version is `X.Y.0`
  at that commit. Before 1.0, anything that changes a score bumps Y: the scenario set or a scenario file, a grader,
  the conditions, the statistics, the invalid-run rule. Fixes that change no score bump only the patch number and
  get no new tag. `packages/bench/suite.json` pins a digest of those files (scenarios, graders, predicates, the
  choice grader, conditions, statistics, `results.py`) per suite version, and `tests/test_suite.py` fails when they
  change without one: bump the minor version and pin the new digest under the new `bench-vX.Y`, or, for a change
  that moves no score, append the digest to the current version's list. Entries are never edited.
- The harness version bumps its minor number for any change to what the agent sees or can do (tools, tool
  descriptions, rules text, fairness filters), and its patch number for fixes that change none of that.
- Every result cites the versions: each run record's `pins.versions` (harness, bench, suite), the run manifest's
  `versions`, each release run's `versions`, the release's `suite` and each leaderboard row's harness versions,
  plus the pins (game build, bg3le, mod tree, repo commit). A release holds runs of one suite version only (and its
  `bench_tag` must be that version), so results from different `bench-v` tags are never on one leaderboard.

Dates are when the work landed on the `overhaul` branch.

## Unreleased (overhaul, 2026-10-21)

The overhaul branch, from `main` at 471ee66. Nothing here has been tagged yet, and none of it has run in the live
game unless it says so.

### Plans and layout

- Target architecture (harness, benchmark, playthrough app with one-way dependencies), the multi-model benchmark
  plan and the ordered overhaul plan with decisions D1-D15.
- The repo root is a uv workspace (`bridge` + `packages/*`) with one `uv.lock`.
- Root ruff, pyright, pytest and coverage config: strict on new code, shrink-only lists for legacy code.
- CI on GitHub-hosted runners: lint, types, the dependency rule (report mode), the 1000-line cap, offline pytest
  with coverage, the Lua Docker suites and the TypeScript checks.

### Harness

- Every play process writes `Sxxx.events.jsonl`, one normalized event log for all clients; triage, the judge,
  model time, fairness flags and run totals read sessions through it.
- The bg3 server switches tools off per session (`BG3AI_TOOLS`, `BG3AI_TOOLS_OFF`) and refuses `note=` when memory
  is off.
- The agent's text is split into rules (how the game, the tools, fairness and the clock work) and coaching, with
  the session anecdotes taken out. `assemble(condition, client)` builds the text with its sha256; `play/CLAUDE.md`
  is generated from it, and the server gives the assembled rules to non-Claude clients.
- pi 1.3.0 is pinned in `tools/pi`, with `bg3.ts` (bg3 tools as sequential `mcp__bg3__` tools, the system text
  verbatim, a billing entry per response), an offline conformance suite against a stub model and a stub MCP
  server, and a real-call check through one endpoint (DeepSeek, $0.0088).
- Client profiles: one TOML format for claude, codex, gemini and pi (`profiles/*.toml`), with pi's `models.json`
  generated from them. `PiClient` runs pi from a profile and treats an exit 0 after an API error as a failure.
- pi runs in the container sandbox, with egress to OpenRouter and DeepSeek only.
- The mod keeps a hidden referee log (trades, pickpockets, crimes, kills, loot, fights) and a log of every roll,
  both read by the harness and never by the agent.
- Scenario setup RPCs (dice seed, Karmic Dice, renames, undo), refused unless the game runs with
  `BG3AI_SCENARIO_SETUP=1`; the seed and renames are applied again after every load.
- A save inspector (`tools/saves/inspect.py`) that lists what a save embeds and flags anything personal.
- The harness version is `packages/harness/pyproject.toml`'s, exposed as `bg3harness.__version__`.
- 0.2.0 (2026-11-01): the bg3 tools' results changed after the launch runs (bridge/CHANGELOG.md, 2026-10-30:
  get_character shows status names only; a give the game refuses, an item fixed in place, a busy camp UI and a
  short rest the game ignores fail at once with the reason instead of timing out; 2026-11-01: use_ability's
  opener says when a conversation is open after the hit, camp leave in a night camp talk says nothing was pressed,
  get_status doesn't show the minimap's pre-trip subregion after a fast travel or camp trip as where the party is,
  a camp leave the game doesn't answer says what the game showed at the press, get_status no longer shows a
  notification's "[ForceUpdate]" placeholder). bench-v0.9's release cites it.

### Benchmark (`packages/bench`)

- Statistics: Wilson intervals, pass^k, a two-level bootstrap, paired differences and the luck covariate.
- Goal predicates and graders over `facts.jsonl` v1, the do-nothing check and the progress curve.
- Sandbox method attribution (trade, pickpocket, loot, quest), reported beside the verdict and never graded.
- Run manifest, run result and release results as versioned JSON Schemas with builders. Every manifest and every run in
  `results.json` cites the harness, bench and suite versions (the harness one is null when neither the runner nor
  the record's pins give it).
- Versions: `bg3bench.__version__` from the pyproject, the suite version `bench-vX.Y` derived from it with its files
  pinned in `suite.json`, and the versions in every run record's pins (a series refuses a run of another harness or
  suite version unless `--allow-version-drift`). A release refuses runs of two suite versions or of a suite other
  than its `bench_tag`; the leaderboard shows the suite and each row's harness versions.
- Scenario schema v2 (family, holdout, goal or until, game settings, dice seed, realtime or turn-based timing,
  requires, conditions) and a loader that converts the v1 files. The think-pause is gone: the ESC menu does not
  pause BG3 (D7).
- The v2 scenario set, each scenario with a closed-book quiz. Suite versions so far: bench-v0.1, the first set
  (`nautiloid`, a 45-minute `act1_gold_1000` sandbox, fights and choice scenes from claude-run-2's game points);
  bench-v0.2, five fights with a Tactician twin (28 scenarios, 15 fights); bench-v0.3, the start saves made in the
  live game on 2026-10-22 (research/ovh/live6.md §2) and the start notes corrected to what those saves hold. 17 of
  the 27 save starts are ready. Four saves are made but not registered (the save inspector refused them on false
  positives), `fight_act2_house_of_healing` has none yet and the Tactician twins wait for theirs. The matrix skips
  a scenario that isn't ready and lists it with the reason (`start.not_ready`, or no start save). bench-v0.4: a
  run an endpoint served outside its profile's `[routing].only` is invalid (`off_route`) and rerun; the manifest's
  `model.served` lists the providers and pi's thinking levels after its clamp.
  bench-v0.5: a run of a seeded scenario whose seed the game didn't apply is invalid (`unseeded`) and rerun, and its
  manifest's `seed` is the one the game applied (null when none).
  bench-v0.6: every scenario tells the agent its task and how it is scored (`task`, `scored_by`, first in the
  kickoff), a goal run ends once its live verdict decides it (or at the time cap, which the kickoff names; no "play
  until WRAP UP NOW"), and every scenario but the choice scenes plays a neutral brief with light roleplay
  (docs/BENCHMARK.md §2).
  bench-v0.7: maximise scenarios (scenario v2 `maximise: {fact}`): no goal and no pass, the run plays its whole
  time cap and scores the fact's gain over it (`graders.measure`; result.json `score`, `passed` null, left out of
  every pass rate). `act1_gold_max` (act1_gold_1000's start save, 45 min) and its Act 3 twin `act3_gold_max` (its
  start save not made yet); act1_gold_1000 keeps its target (user, 2026-10-25).
  bench-v0.8: every start save's avatar is a half-orc paladin, so the save scenarios' character brief says so (it
  said half-elf); a save-start run renames the avatar to the scenario's character (the saves still load as
  "Claude", live B1). `act3_gold_max` has its start save (`Tav-30251267957__SCN-act3_gold_max`): Gale level 6 at
  camp, the avatar with a level-up to 8 pending, max HP halved by the Explorer -> Balanced switch. A party wipe
  (every party member dead or downed: the Game Over screen) ends every run: a goal not passed by then fails
  (`graders.verdict` "wiped", a `{"wipe": true}` violation), a maximise run scores its gain up to the wipe; a run
  whose game loaded a save that is not its start save or its own is invalid (`foreign_save`). Pinned into the
  unreleased bench-v0.8 (diverse-2026-10-25 plays it; its earlier records can be regraded). A fight scenario passes
  only when its own fight is won: its goal is `{"combats_won": {"gte": 1, "enemy": re}}`, a won combat in which a
  character outside the party whose name matches the regex died (`kill` or `died` events, `facts.State.won_kills`);
  `{"combats_won": {"gte": 1}}` passed any won combat (diverse-2026-10-25 Haiku x `fight_act1_windmill` killed a
  sleeping Scrawny Bugbear in 1.1 min). `facts.py` is a suite file now.
  bench-v0.9: the launch runs were graded under bench-v0.8 against `releases/bench-v0.8.json`, which is frozen
  now (a test pins its sha256). bench-v0.9 has the same scenario and grading files (suite.json `shared` pairs the
  two versions, so they may pin one digest) and its own release file and start save keys (`saves/bench-v0.9/`);
  the mod and bg3 tool changes after the launch are pinned in bench-v0.9's release, never in bench-v0.8's.
- Matrix runner: a models x scenarios x conditions x repeats queue that resumes after a crash, reruns invalid runs
  and stops at the global budget. It drives `runner/play.py` through a temporary adapter, with a condition table
  that maps each condition to play.py flags and lists what play.py still lacks.
- Baseline agents (do nothing, seeded random) that speak MCP to the bg3 server.
- Fairness probe table: every bg3 tool has a hidden-fact probe or a written exemption.
- Contamination tools: the quiz format and scoring, condition ids with the wiki and reloads off by default, and
  holdout scenarios kept out of published results.
- Scenario authoring kit: register a start save with hashes and a neutral-name check, draft the v2 file, attach
  the oracle trajectory, run both baselines.
- Outputs: a series report (Markdown and one HTML page), a static leaderboard with a success-vs-cost plot per
  family, an Inspect AI `.eval` export, and a publish pipeline for GitHub release assets and a Hugging Face dataset
  (dry run only; holdout runs are refused).

### Playthrough app (`packages/playthrough`)

- Config in `playthrough.toml`, migrated from `profile.json`.
- A run store, a v1 JSON API and an SSE stream that resumes from `Last-Event-ID`.
- A Svelte web app: Watch, Memory, Timeline, Launcher, Overlay (transparent 1920x1080 for streaming) and Replay.
- A recorder: ffmpeg writes the game into rolling MP4 segments with a disk cap and a RAM and heat guard, plus a
  live MJPEG view.
- Party positions sampled into `positions.jsonl`, shown as a breadcrumb plot.
- A supervisor that starts, stops and resumes a multi-session playthrough, runs the keeper between sessions, nudges
  after two sessions without progress and stops after three crashes in a row.
- Playwright tests of every screen against a fixture run, off by default.

## Before the overhaul (`main` up to 471ee66)

claude-run-2 finished the game (2026-10-09 to 2026-10-18), and three Nautiloid series ran 9 of 9 to the goal. The
history is in git.
