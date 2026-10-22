# Overhaul plan: ordered implementation

The ordered task list for the overhaul on branch `overhaul`. It merges the five research reports of 2026-10-21
(`research/arch/{harness,pi-client,bench,app,game}.md`, local) into one sequence with one owner per file. The target
is [TARGET-ARCHITECTURE.md](TARGET-ARCHITECTURE.md); the benchmark design is [BENCHMARK-PLAN.md](BENCHMARK-PLAN.md).
Status of each item is tracked in the local `docs/BACKLOG.md` ("Target architecture + benchmark track").

## End state

1. **Harness** (`bridge/`, `mod/`, `native/`, `patches/`, `third_party/`, `packages/harness` = `bg3harness`): any
   client (Claude Code, Codex, Gemini CLI, pi with an endpoint + model + key) plays through one
   `run_session(spec, hooks)`, with server-side tool control, a normalized event log, a facts stream and the
   think-pause. Rules text only.
2. **Benchmark** (`packages/bench` = `bg3bench`): scenarios, graders over `facts.jsonl`, stats, manifests, baselines,
   fairness probes, Inspect export, a static leaderboard. Imports only the harness.
3. **Playthrough app** (`packages/playthrough` = `bg3play`, frontend in `packages/playthrough/web`): a supervisor
   over `run_session`, memory and keeper, coaching, a TypeScript UI with live view, recording, timeline, memory
   browser, overlay. Imports only the harness.
4. uv workspace, ruff, pyright, import-linter, pytest tiers, CI on every push, a 1000-line cap with a shrinking
   legacy list.
5. The stress-test round passes on every BG3 situation; then one PR replaces `main` with `overhaul`.

## Decisions taken in this plan (change them here if you disagree)

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Package names and places | `packages/harness` (`bg3harness`), `packages/bench` (`bg3bench`), `packages/playthrough` (`bg3play`); `bridge/` stays a separate workspace member (`bg3ai`) and belongs to the harness | the reports used three different spellings; `bridge/` moving would break play/.mcp.json, the container and every doc |
| D2 | How pi gets the rules | the harness assembles the system text from files and the pi extension sets it verbatim (`BG3AI_SERVE_RULES=0` for pi) | pi's built-in MCP never passes server instructions to the model; a file is easy to hash into the manifest |
| D3 | Where prompt texts live | all prompt texts are data files in the harness: `prompts/rules/` and `prompts/coaching/`. The harness default is rules only; the app adds coaching; the benchmark's "rules + coaching" condition picks the same files | the benchmark must not import the app, and a separate copy would drift |
| D4 | Persona/choice judge | decision extraction and a generic "LLM judge over events" go to the harness; the benchmark's choices grader and the app's persona score both call it | both packages need it and neither may import the other |
| D5 | Benchmark session length | one session per run, no memory keeper, no resume; a context overflow ends the run as a recorded failure | the plan's "crash = invalid rerun" and no compaction |
| D6 | Wire format for the ranked track | every model, Claude included, through OpenRouter's chat-completions API via pi; Claude through Claude Code is a labelled condition | one routing and caching path for all models |
| D7 | Time in exploration and sandbox scoring | REVISED 2026-10-21 after live probe L1: the ESC menu does NOT pause BG3 (NPCs walk, buffs tick, GameTime runs 1:1; research/ovh/live1.md §1), and no mod API pauses real time. No think-pause. Score on wall time, report each model's thinking time and latency next to it; exploration and sandbox results are labelled real-time; forced turn-based mode stays a possible later condition | there is no way to stop the world from the mod |
| D8 | Goals a do-nothing agent could pass ("no fight", "survive 30 min") | they must be combined with a positive goal | the baseline must fail every scenario |
| D9 | Dice | Karmic Dice off in every scenario (readable: RollManager.RollModeType); every roll logged out of the agent's sight; a per-run luck number published; and seeded dice: probe L3/L4 showed every load reseeds from the wall clock, and writing the ServerRollStreams component after a load gives the same roll sequence, so each run of a scenario starts from the same seed (same start, not the same run: rolls diverge once the runs do) | common random numbers at the start reduce variance |
| D10 | Lint on legacy code | ruff and pyright strict on `packages/`; legacy files get a per-file ignore list that only shrinks; no repo-wide reformat commit | a repo-wide reformat would bury every later diff |
| D11 | `runner/play.py` edits | one owner (the harness worker) takes all `play.py` edits as patches from the others | four tracks need small edits there |
| D12 | Who solves a scenario for the oracle run | one successful run by a strong model through Claude Code, kept as the reference trajectory; a human run where no model succeeds; scripted solves only for trivial scenarios | scripting a solve per scenario does not scale to 20-30 scenarios |
| D13 | `main` during the overhaul | frozen: only critical fixes, each also cherry-picked onto `overhaul` | keeps the final rebase small |
| D14 | Codex and Gemini CLI | kept, with one live smoke run each in Phase C; a client that fails it is marked unsupported in the docs, not fixed in this overhaul | they have never played a live session |
| D15 | Tool interface changes for non-Claude models | allowed only as one change for every model (e.g. real JSON arrays instead of JSON inside strings), recorded in the harness version; never per model | fairness |

## Phase A: foundations (sequential, one worker, offline)

| # | Task | Done when |
|---|---|---|
| A1 | uv workspace root (virtual; members `bridge`, `packages/*`), one `uv.lock` | `uv sync --locked` from the root; the MCP server still starts from `play/.mcp.json` and the container smoke passes; offline suite green |
| A2 | Root tool config: ruff, pyright, pytest with `unit`/`integration`/`e2e`/`live` markers (existing unittest tests run under pytest), coverage per package | `uv run pytest` runs the whole offline suite |
| A3 | 1000-line cap check + legacy allowlist of the 25 over-cap files | test fails on a new over-cap file or a grown listed file |
| A4 | Dependency rule: import-linter contracts plus a boundary test (no `sys.path` edits, no cross-package paths), in report mode until Phase D | runs in CI and prints violations |
| A5 | CI workflow (GitHub-hosted): sync, ruff, pyright, import-linter, line cap, pytest, Lua Docker suites, TypeScript checks (once there is TypeScript) | green on `overhaul` |

## Phase B: parallel offline tracks

Each track is one worker in its own worktree, branching from `overhaul` and merging back by PR. No track needs the
game or a paid API.

**B-harness** (owns `runner/play.py`, `runner/*` until moved, `packages/harness`)
1. Server-side tool control: `BG3AI_TOOLS` in the bridge (`capabilities.py`), unknown names fail loudly, tool-count
   check in the init event.
2. Normalized event log: `Event` with an envelope (`v`, `t`, `seq`, `src`), `Sxxx.events.jsonl` written by every
   client, one `load_events()` reader with a fallback for old raw streams.
3. Port the analyzers (triage, judge extraction, model time, fairness flags) to `load_events`, with parity tests on
   recorded sessions.
4. Create `packages/harness`; move the leaf modules (clients, sandbox, display env, ramwatch, host tuning,
   fairness, triage, model time, events) behind one-line shims.
5. `GameController`: launch, save/load, OOM and crash handling out of `play.py`.
6. `run_session(spec, hooks)`: lift `run_claude` and the resume loop out of `play.py`; `play.py run` behaves the
   same; a fake client and a fake game drive it end to end in tests.
7. Facts stream writer (`facts.jsonl` v1, poller reusing `progress.read_game`), wired at the poll site.
8. Wire in the other tracks' `play.py` patches (pi client flags, prompt assembly, think-pause switch).
9. Facts for the choices family: every dialogue choice (scene, options shown, option picked) and the agent's
   stated reason from the event log, in `facts.jsonl`.
10. Game settings readout: resolution, UI scale, language, subtitles, camera, difficulty and Karmic Dice read at
    session start and written to the manifest; a run whose settings differ from the scenario's is refused.
11. Container sandbox for pi: OpenRouter in the egress allowlist, the key passed by env, pi and Node in the image;
    the existing offline sandbox smoke extended to pi.
12. Spend safety: a per-run dollar cap enforced from the billed cost as it streams (kill at the cap, run marked
    `cap_hit`), a global budget file for a whole matrix, and a refusal to start without a cap.

**B-tests** (owns `tests/` at the root, `packages/*/tests/fakes/`, fixtures)
1. Fake mod: a TCP server that speaks the mod's Lua-eval protocol and replays recorded replies by request, with a
   scripted game state for the scenarios used in tests (gold changes, a fight, a dialogue, an area change).
2. Fixture recorder: a bridge option that records every Lua request/reply pair of a live session to a fixture
   file (used in Phase C; small, scrubbed of paths).
3. Offline end-to-end test: scenario file → `run_session` → pi with a scripted stub model → real bridge → fake
   mod → `facts.jsonl` → grader → `result.json` → stats → leaderboard build; runs in CI in a few minutes.
4. Playwright test of the app against the same run's output; coverage thresholds per package in CI.

**B-pi** (owns `tools/pi/`, `bg3harness/profiles.py`, the pi parts of `bg3harness/clients.py`)
1. Pinned pi 1.3.0 in `tools/pi/` (package.json + lockfile, `npm ci --ignore-scripts`).
2. `bg3.ts` extension: registers the bg3 tools as sequential, sets the system text verbatim, records OpenRouter's
   billed `usage.cost` and served provider per response.
3. Client profiles: one TOML format for all four clients (client, base URL, model, key variable, provider pin,
   reasoning effort); generates pi's `models.json`.
4. Conformance suite with a stub model and a stub MCP server (23 checks: tool list, sequential calls, no
   compaction, images, clock line, malformed args, abort, resume, routing fields, billing, retries, timeouts).
5. `PiClient` + parser with recorded fixture streams; guards for "exit 0 after an API error" and the overflow loop.

**B-bench** (owns `packages/bench`)
1. Stats: Wilson interval, pass^k, two-level bootstrap, paired differences, luck covariate (pure Python).
2. Goal predicates and graders over `facts.jsonl` (state / ever / never; gold, item, level, companions, combat
   outcome, area, quest, survive), tested on synthetic and recorded timelines.
3. Sandbox method attribution (sold, quest, looted, stole, pickpocketed, killed a trader): reported, never graded.
4. Manifest and result schemas (JSON Schema, versioned), `results.json` per release.
5. Inspect `.eval` export (optional extra, pinned).
6. Static leaderboard page with the success-vs-cost plot.
7. Baseline clients: do-nothing and random, through the same client interface.
8. Fairness probe table: every tool has a probe or a written exemption.
9. Scenario schema additions: `game_settings` (Karmic Dice, difficulty, resolution, UI scale, language), `think_pause`,
   goal predicates, `requires` (`vision`, `min_context`) so a model that can't meet them is reported as not
   applicable instead of failing, `difficulty` as a variant dimension, `family`, `holdout`.
10. Matrix runner: models × scenarios × conditions × repeats as one queue that runs unattended overnight: one game at
    a time, resumes after a crash of the runner itself, reruns invalid runs, honours the RAM and thermal guards,
    stops at the global budget, and writes progress the dashboard shows. Conditions include reasoning effort,
    persona (neutral, evil, ...), wiki on/off and rules vs rules + coaching.
11. Contamination tooling: the closed-book quiz per scenario (questions, answer key, scoring, run through the same
    client profile), wiki off by default, the private holdout flag kept out of published results.
12. Scenario authoring kit: a checklist and a tool that registers a start save (copy, inspect, sha, neutral-name
    check), drafts the scenario file, records the oracle trajectory (D12) and runs both baselines against it.

**B-prompts** (owns `packages/harness/prompts/`, lint tests; hands diffs to the owners of `play/CLAUDE.md`,
`bridge/src/bg3ai/rules.py`, the kickoff text)
1. Split `play/CLAUDE.md`, `rules.py`, the kickoff and `PACE_PREFLIGHT` into rules and coaching files.
2. `assemble(condition) -> (text, sha256, parts)`; lint tests keep session anecdotes, client names and unexposed
   tool names out of the rules.

**B-mod** (owns new Lua files and `bridge/src/bg3ai/pause.py`)
1. Referee ring (`Referee.lua`): crimes, theft, pickpocket, trade, kills, combat membership; never reachable by an
   agent tool (fairness test).
2. Hidden roll log (`RollLog.lua`).
3. Seeded dice: a setup RPC (behind the scenario-setup switch, never an agent tool) that writes ServerRollStreams
   seeds after every load, re-applied by the harness after each load; manifest records the seed. (The think-pause
   is dropped: D7.)
4. Rename perturbation cleanup: Ext.Loca renames survive a save load, so the setup RPC records and undoes them, or
   the game restarts between runs.
5. Save inspector (`tools/saves/inspect.py`): lists what a save embeds, run on copies only.

**B-app** (owns `packages/playthrough`)
1. Config file (`playthrough.toml`) replacing `profile.json`, with migration.
2. Run store reader and SSE hub over today's files.
3. Recorder: ffmpeg + NVENC to rolling MP4 segments, on by default, with a RAM/thermal guard.
4. Web app P0 (TypeScript + Vite + Svelte): watch screen, memory browser, timeline of choices and fights, cost and
   latency, built against a recorded run.
5. Supervisor: start/stop/resume through `run_session` (a thin adapter over `play.py` until B-harness step 6).

**B-split** (after B-harness step 1, owns `bridge/src/bg3ai/server.py`)
1. Split `server.py` (11,152 lines) into tool-group modules under the cap, behaviour unchanged, tests green.
2. Then the other over-cap files, largest first, each by its package's owner.
3. Tool interface review for non-Claude models (D15): batch tools take real JSON arrays instead of JSON inside a
   string; descriptions lose Claude Code assumptions (ToolSearch, deferred tools, turn wording); tool count and
   description length measured; any change shipped for every model and recorded in the harness version.

## Phase C: first live window (needs the game, one sitting, runner stopped)

Ask before it starts. Every check has exact steps in the research reports.
- `run_session` live: one 20-minute session and one forced crash recovery.
- DONE 2026-10-21 (research/ovh/live1.md): menu pause does not freeze the world (D7 revised); Karmic readable; reloads
  reseed, seeds writable (D9); referee event arities captured; x11grab capture works; renames survive loads; map
  X/Y = world x/z.
- Dice: do reloads reroll; can the mod set the seed; is Karmic Dice readable.
- Referee events: which exist on the current build, and their arguments (trade, pickpocket, loot, trader kill,
  flee).
- Recording: ffmpeg captures the BG3 window under Xwayland; NVENC cost while the game runs.
- Map coordinates; loot generation timing; renames.
- Record golden fixtures for the fake mod (B-tests 2): a fight, a trade, a dialogue choice, an area change, a
  load.
- Codex and Gemini CLI: one short smoke run each (D14).
- Then pi real-call checks per model family (about $1 in total, asked first): billed cost, served provider,
  images, cache hits, effort.

## Phase D: the move

1. Benchmark code out of `play.py`/`runs.py` into `packages/bench` (series on `run_session`, no resume, invalid ⇒
   rerun).
2. Playthrough code (runner loop, kickoff, memory, keeper, dashboard) into `packages/playthrough`.
3. import-linter enforcing, shims removed, `runner/play.py` becomes a dispatch-only CLI.
4. Second live window: one Nautiloid benchmark run and one playthrough session.

## Phase E: benchmark content

1. Neutral start saves (generic name, standard build), checked by the save inspector.
2. First scenarios: `nautiloid`, `act1_gold_1000`, 3 fights, 2 choice scenes; oracle run and baselines for each.
3. Nautiloid pilot on pi: 5 models × 3 runs (~$40-60, asked first).
4. Claude Code baseline on the new harness: the same scenarios and conditions, so the harness effect (Claude
   through Claude Code vs through pi) is measured on one harness version.
5. Human baseline: the owner plays each scenario once or twice through the normal game (time, success, attempts),
   published as its own leaderboard row.
6. Then the variance pilot and the rest of the suite, per BENCHMARK-PLAN.md phases 2-3, including harder
   difficulty variants of the fights.

## Phase F: stress-test round

The coverage matrix (`docs/STRESS-TEST.md`): every BG3 situation (game lifecycle, movement, combat, dialogue,
trade, stealth, inventory, party, puzzles, UI states, special forms) across acts, with a start save, a live check
and a result, through Claude Code and pi. Every failure becomes a backlog item, gets fixed and is re-run. The saves
and recorded replies become fixtures for the integration tests.

## Phase F2: docs, releases and the launch gate

1. Versioning: harness and benchmark versions, `bench-vX` tags, a changelog; every result cites both versions.
2. Docs: README rewritten for the three packages; a quickstart "test your own model" (one profile file, one
   command); app docs; standalone MCP server docs for people bringing their own client; contributing.
3. Game-update procedure: stay offline, pin the buildid, and on a new build rerun the conformance suite, the fake-
   mod fixtures check and the stress-test matrix before moving the pin.
4. Launch gate (nothing public before all are settled by the owner): Larian's AI clause, publishing start saves,
   the benchmark's name, and a scrub of personal paths and keys from anything published.

## Phase G: replace main

Rebase `overhaul` onto `main`, CI green, one PR `overhaul` → `main` (rebase merge), then add the CI jobs as required
checks to the `protect-main` ruleset.

## Who works when

| When | Workers |
|---|---|
| Phase A | 1 (foundations) |
| Phase B | up to 5 at once: harness, pi, bench, app, tests; prompts, mod and split as the others free up |
| Phases C, D.4, E, F | the live worker, one game at a time |
| Phase F2 | 1 (docs), the owner for the launch gate |

## Not planned (optional, later)

- Publishing claude-run-2's data (decisions, memory, transcripts).
- Streaming to Twitch or YouTube with narration.
- One model per companion (multi-agent party).
- A decision model fine-tuned on our logs for latency and cost.
- An open track for outside harness submissions.
- Windows: a non-goal; the harness runs on the native Linux build.
