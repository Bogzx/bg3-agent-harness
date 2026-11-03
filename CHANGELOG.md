# Changelog

The repo holds three packages (see [docs/TARGET-ARCHITECTURE.md](docs/TARGET-ARCHITECTURE.md)), each with its own
version:

| Package | Version lives in | Now |
|---|---|---|
| harness (mod, bg3 MCP server, runner, clients) | `packages/harness/pyproject.toml` (`bg3-agent-harness`; `bg3harness.__version__`) | 0.3.0 |
| benchmark | `packages/bench/pyproject.toml` (`bg3bench`; `bg3bench.__version__`) | 0.10.0 |
| benchmark suite | derived: `bench-vX.Y` of bg3bench `X.Y.Z` (`bg3bench.suite`), its files pinned in `packages/bench/suite.json` | bench-v0.10 |
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
  camp leave and fast_travel from camp with a conversation screen up refuse at once, a camp leave the game
  doesn't answer says what the game showed at the press, get_status no longer shows a
  notification's "[ForceUpdate]" placeholder, fast_travel from camp under a conversation screen says so instead of
  "(Condition)", get_status doesn't wait 3 s per read on a re-chain the game refused, and act.chain fails 0.6 s after
  an attach the game didn't take instead of at 3 s). bench-v0.9's release cites it.
- 0.3.0 (2026-11-02): what the agent sees and can do changed again after bench-v0.9's graded fight re-run (walks
  out of combat block by default, the minimap block, the PASSED line, a walk ends a batch, roll_check's reroll, trade's SOLD list, known ground to 40 m,
  required camp talks named, a misread variant list refused). bench-v0.10's release cites it; the list is under
  bench-v0.10 below.

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
  bench-v0.10 (bg3bench 0.10.0, harness 0.3.0, 2026-11-02): `releases/bench-v0.9.json` is frozen as it was for the
  fights-v09 re-run (a test pins its sha256). The scenarios and graders are bench-v0.9's; results.py adds the
  optional `recovered_launches` to result.json (below; no score effect), so bench-v0.10 pins its own suite digest.
  Its own release file (`releases/bench-v0.10.json`: mod tree 24376dc8, the same 81 bg3 tools, new prompt hashes for
  pi, claude and pi without screenshots) and start save keys (`saves/bench-v0.10/`, the same save files). Its
  conditions also pin the served tool definitions, `tools.defs_sha256` (release v1, optional; release.defs_digest:
  each served tool's tools/list entry, whole, canonical JSON), and C02 rejects a bundle whose `bg3-tools.json` hashes
  otherwise; bench-v0.8 and v0.9's releases have no such field and keep checking names only. What changed for the
  agent (bridge/CHANGELOG.md, 2026-11-02, and docs/FAIRNESS.md):
  - Walks out of combat return at their end: `move_to`, `walk_toward`, `travel_to` (and `route(go=true)`, a far
    `move_to`) default to `wait=true` and return what `wait_for` on the walk returned, with its early stops, CHANGES
    and the minimap block; a walk still going after 90 s returns its progress and the id `wait_for` keeps waiting
    on. `wait=false` keeps the old pipelining.
  - travel_to and regroup name what the trip passed: a PASSED line before CHANGES (what came into view at a leg's
    end and is out of view at the trip's end, not in the agent's last view; nearest first, up to 6).
  - Out of combat a batch ends with its first walk step: `do_actions` runs (`queue_actions` queues) the steps up to
    and with the first `move_to`/`walk_toward`/`travel_to`/`jump_to` and lists the rest as not run (not queued);
    batches without a walk, or with the walk last, are unchanged.
  - Every walk end out of combat and every `look` carry the minimap block (OBJECTIVES with distance, direction and
    height, WAYS, the 3 nearest unvisited LEADS), also after a walk that stopped short.
  - `roll_check` returns a failed roll with the roll screen open; a reroll is its own call, `roll_check(reroll=true)`.
    `reroll_on_fail` is removed (it spent Inspiration before the failure was seen).
  - `trade(sell="junk")` names every item it sold (SOLD, by name with count and gold) and counts what the junk rule
    kept back (KEPT).
  - Known ground (FAIRNESS.md): ground within 40 m of the leader with a clear eye ray (was the observer's Sight,
    ~18.5 m), and ground up to 15 m below an edge at up to 60 degrees (was 4 m, 45 degrees). route, travel_to,
    walk_toward, path_preview, WAYS, jump solvers, move_to/jump_to's "not in view" and hazard names follow it.
    Characters and items keep the Sight stat.
  - At night in camp, `long_rest`, camp leave and `fast_travel` from camp refuse before pressing anything while the
    camp story still wants a talk, and name who (the "!" over them; someone out of sight as "someone not in view"),
    instead of failing 30 s later. At night camp, get_status and look show the camp "!" over the characters in view
    in their TALK line. `long_rest` (and the sleep press of camp leave and fast_travel) refuses at once while the
    game menu is open.
  - `use_ability` on a spell container lists the variants that read as a spell's stats entry and says how many
    entries didn't (after one more read); with none that reads it refuses without names. The raw text is never shown.
  Metric definition change: a tool call counts as async only when its own background job is still running when the
  call returns (bridge metrics_record.py). A blocking walk's job starts and ends inside the call, and a walk that
  ended in the 0.3 s settle window no longer counts. Async % and the latency figures built on it are not 1:1 with
  bench-v0.9 or bench-v0.8 runs.
  The behaviour card shifts with blocking walks too, with no change in play: the `wait_for` calls that followed walks
  are gone and a walk's time is inside its own call, so calls per minute, the read share and the think gaps (tool
  result to next call) are not 1:1 with earlier suites. `moves.refused` now reads a walk's own lines only (not its
  EVENTS, CHANGES, PASSED or minimap block: a companion's queue failing during the walk is not the walk's refusal),
  and a walk's own result now carries its outcome, which earlier suites reported in the `wait_for` after it.
  Invalid-run rule: a `launch_failed` before the run's run_start that relaunched the scenario's own start save is
  the run's note (run record `recovered_launches`: t, attempt, save, error), not a harness failure; a launch failure
  after run_start, or for a new-game start, still voids the run (bg3harness runs.recovered_launch). Records written
  before it keep their failures, so re-grading a bench-v0.9 record doesn't change its validity. result.json
  discloses such launches in the optional `recovered_launches` (result v1, [{t, attempt, save, error}]; absent when
  none).
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
