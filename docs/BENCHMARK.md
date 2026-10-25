# Benchmark protocol: comparing models on BG3

For someone who wants to measure a model (or a client) playing Baldur's Gate 3 through this harness and compare it
with other models. This file is the protocol; the details live in the files it links. Status as of 2026-10-16:
**only the Nautiloid series has run live** (3/3 clean, Claude Opus); everything marked *(not live-tested)* is built
and unit-tested but has not played a real run yet.

To run your own model, start with [QUICKSTART.md](QUICKSTART.md). What to do when the game updates is in
[GAME-UPDATES.md](GAME-UPDATES.md).

The plan for the multi-model suite (pi + OpenRouter client, fight / sandbox-goal / choice / exploration families,
scoring and phases) is [BENCHMARK-PLAN.md](BENCHMARK-PLAN.md).

## 1. Requirements

- **Own BG3 on Steam**, installed through Steam (the harness never touches Steam's files, and there is no other
  way in: the preflight checks for Steam's `appmanifest_1086940.acf`).
- **The native Linux build** (`bin/bg3`, Vulkan), not the Windows build under Proton, plus Steam Linux Runtime 3.0
  (sniper). bg3le, the mod loader we build from `third_party/bg3le`, hooks the native binary only.
- **Hardware**: the game runs in its own cgroup capped at 10 GB RAM (`BG3AI_GAME_MEMORY_MAX`; it normally peaks at
  ~6.3 GB RSS), so 16 GB RAM in total; a 4 GB+ GPU (tested: NVIDIA RTX 3050, 4 GB, PRIME offload). Building
  bg3le needs ~10 GB RAM once. ~160 GB disk for the game.
- **Setup**: [docs/SETUP.md](SETUP.md) (machine setup), then `tools/build_bg3le.sh`, then
  `tools/dev/first_launch.sh` (one supervised launch that checks every layer).
- **The preflight**, read-only, before every series:

  ```bash
  python3 tools/bench_preflight.py [--series NAME]    # PASS/WARN/FAIL per line, exit 1 on any FAIL
  ```

  It checks: installed through Steam, fully, no update pending; the native ELF binary and sniper; the buildid
  equals the pinned build (and, with `--series`, the series' frozen one); Steam offline and the game's
  `AutoUpdateBehavior` (WARN when Steam could change the build mid-series); a Steam client running; bg3le built with
  every `patches/bg3le` patch; the mod in place; the `claude` CLI; docker (only for `--sandbox container`); free
  RAM vs the cap; VRAM.

## 2. What is frozen

A comparison only holds when all of these are equal across the models compared. Report them with the results.

| What | Value / where | Enforced by |
|---|---|---|
| Game build | Steam buildid `25605617` (Patch 8 Hotfix 10; `tools/dev/first_launch.sh` `WANT_BUILD`) | preflight; series pins |
| bg3le, mod, harness code | `series.json` `pins`: bg3le sha256 + source, mod tree sha1, repo commit + dirty diff ([runner/README.md](../runner/README.md), "Fixed per series") | the runner refuses a run whose pins differ (`--allow-version-drift` records it) |
| Harness, bench and suite versions | `pins.versions`: `bg3harness.__version__`, `bg3bench.__version__` and the suite `bench-vX.Y` (from each package's pyproject; [CHANGELOG.md](../CHANGELOG.md) says when each bumps) | a harness or suite change drifts the pins like the repo commit; a release holds one suite version (`results.build_results`); `packages/bench/tests/test_suite.py` fails when scenarios, graders or conditions change without a suite version |
| Steam updates | Steam in offline mode for the whole series | preflight WARN; the runner refuses to launch with Steam online where the machine requires offline (`~/.config/bg3ai/require-steam-offline` or `BG3AI_REQUIRE_STEAM_OFFLINE=1`) |
| Scenarios | `scenarios/<name>.json`: start, `until` milestone, caps, profile, resets ([scenarios/README.md](../scenarios/README.md)) | `series.json` `scenario.digest` + the start save's sha1 |
| Bench scenarios | `packages/bench/scenarios/<name>.json` (v2: task, scored_by, start, goal or `until`, caps, brief, game settings, dice seed) | `series.json` `scenario.digest`; the suite version (`suite.json`) |
| Condition | the id (`rules`, `rules+coaching@<hash8>`, `+wiki`, `+persona:x`, `+reloads:N`; `bg3bench.conditions`): the tools it removes, the harness prompt condition it plays (`rules` or `rules+coaching`) and what a session run can't play exactly (claude's coached `play/CLAUDE.md`, a coaching hash, a persona, a reload cap) | the matrix refuses a gapped job unless `allow_gaps`; its result is `pilot` and never published |
| What a bench run sees | no playthrough memory (memory tools off, an empty per-run memory dir), no wiki and no `load_game` unless its condition adds them | `LiveRunHost` (BG3AI_TOOLS_OFF, BG3AI_MEMORY_DIR); `packages/harness/tests/test_run_host.py` dumps what a pi run is served |
| Caps | per scenario (`run_hours`, `max_cost`, `max_tokens`) | `series.json` `caps` (`--allow-cap-change` records a change) |
| Persona judge model | `claude-sonnet-5-5` (`runner/judge.py`, `$BG3AI_JUDGE_MODEL`); never the model under test | `series.json`; `--allow-judge-change` records it |
| Latency reference speeds | 100 output tok/s, 0.75 s TTFT (`bg3harness/model_time.py` `REF_OUT_TOK_S`, `REF_TTFT_S`) | constants, recorded in each record's `model_time.ref` |
| Keeper model | `same` as the play model (or `--no-keeper` for every model) | recorded per run |
| Agent-facing text | `play/CLAUDE.md`, playbooks, tool descriptions: tag one commit and don't edit it during a comparison | repo pin |

Known bias: the agent-facing text was tuned on Claude's failures. Read
[docs/BENCHMARK-BIAS.md](BENCHMARK-BIAS.md) before claiming a cross-model result; its "What a fair cross-model
benchmark should do" list is the bar. On the `overhaul` branch the text is split into rules and coaching with the
anecdotes removed (`packages/harness/prompts/`), but no live run has used the split text yet.

### What a bench run is told (decision, user 2026-10-22)

- The kickoff leads with the scenario's task and how it is scored, in plain words (scenario v2 `task` and
  `scored_by`, e.g. nautiloid: "Escape the Nautiloid as fast as you can ... Nothing else counts toward the score").
  The text says what the grader checks and nothing it doesn't: the score is pass/fail on the goal; time, downs,
  deaths and cost are reported next to it, never added to it. `tests/test_scenario_set.py` checks every scenario's
  task against its goal and that the kickoff opens with both lines.
- The run ends once it is decided (an `until` milestone, or a goal scenario's live verdict: passed, or a fired
  "never" that leaves the goal unreachable) or at the scenario's time cap, and the kickoff says so with the cap in
  minutes. It no longer says "keep playing until WRAP UP NOW".
- The character brief is neutral and the roleplay `light` in every scenario but the choice scenes: a persona with
  values ("protects the weak, keeps promises") pulled live7's speed run into optional content (rescuing
  Shadowheart on the Nautiloid). Choice scenes keep their own neutral brief, since their picks are judged against it.
- These changed what is measured: suite bench-v0.6 (bg3bench 0.6.0).

### Maximise scenarios (decision, user 2026-10-25)

- "The goal for the gold runs is to reach as much gold as possible, not only 1000": a scenario may set
  `maximise: {"fact": "gold"}` instead of a goal. It has no pass: the run plays its whole time cap (an agent that
  ends its turn early is resumed with the time left), and its score is the fact's gain, its value at run_end minus
  its value at the run's first snapshot (`graders.measure`). result.json has `score` {fact, start, end, gain, peak}
  and `passed` null; the series report shows the gain per run and the median, lowest and highest gain; pass rates,
  the release stats and the site leaderboard leave such runs out (the site has no score table yet).
- A target is never turned into a maximum: `act1_gold_1000` keeps its 1000-gold target and its name;
  `act1_gold_max` plays its start save without one, and `act3_gold_max` is its Act 3 twin (same task, same 45 min).
- Suite bench-v0.7 (bg3bench 0.7.0).
- bench-v0.8 (bg3bench 0.8.0): the save scenarios' brief says half-orc (every start save's avatar is one, it said
  half-elf), a save-start run plays under the scenario's character name (the runner renames the avatar at the
  run's start and the mod again after every load; the saves themselves still say "Claude"), and `act3_gold_max`
  has its start save.

## 3. Running a series

```bash
python3 tools/bench_preflight.py                                        # everything PASS (WARNs read)
python3 runner/play.py scenarios                                        # which scenarios are ready
python3 runner/play.py run --scenario nautiloid --runs 5 --model opus --playthrough naut-opus
python3 runner/play.py runs --playthrough naut-opus [--failures]        # the table and the spread
```

- One series per model and scenario: give each its own `--playthrough` name (the series name; default the
  scenario's). Re-running the same command resumes at the first run without a record.
- The runner launches the game, plays sessions until the milestone or a cap, records the run, stops the game,
  resets per-run state (saves, nav files, `play/knowledge/` edits, memory) and starts the next run
  ([runner/README.md](../runner/README.md), "Benchmark series").
- `--judge` (on by default with `--runs`/`--scenario`) scores each run's in-character decisions afterwards;
  `play.py judge --playthrough NAME` (re-)scores later *(not live-tested)*.
- Don't commit, edit code or update anything between the runs of a series: the pins refuse it.

**Scenarios today**: `nautiloid` is ready (new game → Nautiloid escaped). `crash_site_grove`, `act1_underdark` and
`act2_last_light` need a start save made once by hand ([scenarios/README.md](../scenarios/README.md), "Making a
start save"); loading a start save in a series is *not live-tested*.

**Models other than Claude.** `--client claude|codex|gemini` picks the CLI that plays (`bg3harness/clients.py`:
Claude: the same command as before, verified offline; Codex: untested live, its CodexClient command passes an
offline check against codex-cli 0.157.1, a stub Responses-API model and the stub MCP server
(`tools/codex/conformance/`); Gemini: **unsupported** (D14: the Gemini CLI is not installed or logged in here, never
run)); a series keeps its first client (series.json). A non-Claude series is *not live-tested yet*.

**The container sandbox (step 3).** `--sandbox container` runs the client in a container that reaches only the bg3
MCP server (served over http on the host with the session's env) and, through an egress proxy, its own model API:
no repo, game files, saves, memory or other host ports inside
([tools/container/README.md](../tools/container/README.md), "The runner"). Use it for any client you don't want to
trust with the host. It needs, once per boot, `sudo tools/container/firewall.sh up` (the runner refuses to start
without the nft table, with that hint; `--sandbox-unfirewalled` is for dev runs and is recorded), docker, and the
client's credentials in the runner's env (`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`). Run records carry `"sandbox": "container"`. The keeper runs in the container too.

```bash
sudo tools/container/firewall.sh up && tools/container/firewall.sh check          # OK: only the bg3 server
python3 runner/play.py run --scenario nautiloid --runs 5 --client codex --model gpt-5 --sandbox container \
    --playthrough naut-gpt5
```

## 4. A run record

One line per run in `runtime/sessions/<series>/runs.jsonl`; the fields are listed in
[runner/README.md](../runner/README.md), "Run record". The ones a comparison uses:

- `outcome` (`reached` | `failed` | `timeout` | `over_budget` | `gave_up` | `runner_error`; `failed`: a goal run that its live
  verdict ended on a fired "never"), `cap`, `clean`
- `time_to_milestone_s`, `wall_min`, `play_min`, `sessions`, `turns`
- `cost_usd`, `tokens` (input, output, cache read, cache write)
- `failures` (each with its class, below), `operator`, `deaths`, `fairness_flags`
- `latency` (idle median/p90, gaps over 10 s, bridge read round trip) and `model_time` (harness vs model time, the
  model part normalised to the reference speeds) *(model_time in records: not live-tested)*
- `persona` (the judge's 0-10 adherence to CHARACTER.md) *(not live-tested)*
- `pins` (with `versions`: harness, bench, suite), `caps`, `scenario`, `model`, `effort`, `keeper_model`,
  `version_drift`, `cap_changes`

## 5. Reading `play.py runs`

The table has one row per run (outcome, clean, time to the milestone, cost, tokens, idle median, failures by
class), then the series summary: n, reached, and n/min/max/mean/stddev for wall time, time to the milestone
(reached runs only), cost, tokens and the idle median, plus any version drift or cap change between runs.

Compare models on: the reached rate, then time to the milestone and cost (mean **and** spread: with 3-5 runs, two
models whose ranges overlap are not distinguished), the idle median (live feel), persona, deaths. Use
`model_time`'s adjusted model time, not raw wall time, when providers differ in API speed.

## 6. Fairness and failure classes

- **Fairness**: the agent sees only what a player sees and acts through the game's own actions and input. A run
  whose `fairness_flags.leak` is non-zero saw something a player can't: that is a harness failure (`fairness_leak`),
  and the run doesn't count for the model either way. `fairness_block` (the sandbox stopped the agent's file call)
  counts against the agent.
- **Failure classes** (`runner/runs.py`, listed in [runner/README.md](../runner/README.md)): **harness** (ours:
  crashes, hangs, OOM, launch/setup, stale bg3le, MCP not connected, API errors, tool exceptions/timeouts, leaks),
  **agent** (stalls, wipes, reloads, early exits, timeouts and caps), **unknown** (agent hangs, claude killed from
  outside; timeouts and caps also become unknown when harness failures hit the run).
- **Which runs count**: a run with a harness failure measures us, not the model. Report it, re-run it (a new run in
  the same series), and compare models on `clean` runs plus agent-class failures. A run with an operator action
  (`operator` non-empty) never counts, nor does one an endpoint served outside its profile's `[routing].only`
  (`off_route`: the manifest's `model.served` lists the providers and pi's thinking levels after its clamp).

## 7. Budget and machine plan

Per model and scenario: **3-5 runs** (3 shows whether a result repeats, 5 gives a usable spread).

Measured: the Nautiloid series `naut` (`runtime/sessions/naut/runs.jsonl`, Opus, effort medium, 3 runs, 1 session
each): **$4.03-4.80 (mean $4.34) and 11.0-12.8 min wall (mean 12.1) per run**, ~1 min between runs for the game
restart. claude-run-2 (Opus, 24 h of sessions, $487) cost ~$20 per hour of play; `naut` ~$21.5 per wall hour.

Estimated (not measured) for the later scenarios, from claude-run-2's pace at ~$22/h:

| Scenario | Per run | 3 runs | 5 runs | Basis |
|---|---|---|---|---|
| `nautiloid` | $4.3, 12 min | $13, 40 min | $22, 65 min | measured (`naut`) |
| `crash_site_grove` | ~$15, ~50 min | ~$45, 2.5 h | ~$75, 4.2 h | claude-run-2: crash site → grove ~40 min of play |
| `act2_last_light` | ~$18, ~60 min | ~$55, 3 h | ~$90, 5 h | claude-run-2: Shadow-Cursed Lands → Last Light ~50 min |
| `act1_underdark` | ≤ $75, ≤ 3 h | ≤ $225, ≤ 9 h | ≤ $375, ≤ 15 h | the caps only: claude-run-2 never went there |

So one model, all four scenarios at 3 runs: ~$115 and ~6 h without the Underdark, up to ~$340 and ~15 h with it
at its caps; plus the judge (a few Sonnet calls per run, cost unmeasured). Assumptions: Opus at Claude Code's
reported cost, prompt caching as in `naut` (cache reads are ~98% of its tokens); wall time = play time + ~10 min
per run for launch, load and wrap-up; claude-run-2's pace was one run of a then-buggier harness, so later
scenarios may be faster; a slower or more expensive model scales time and cost by its own speed and price, and the
caps (`run_hours`, `max_cost`) bound what a failing run can spend.

**Machines**: one machine runs one game, so one series at a time (one runner per checkout: `runtime/runner.lock`;
one debug port 9998; the 10 GB game cgroup). Series run back to back on a machine; models or scenarios run in
parallel only on separate machines, each with its own Steam install at the same buildid, all in offline mode (one
Steam account online on two machines kicks the other off). A machine left unattended for a day does about one
model's full set (Underdark at its caps) or 4-5 models' Nautiloid series.

### Variance pilot

`packages/bench/matrices/variance-pilot.toml` (BENCHMARK-PLAN.md phase 2, not run): DeepSeek V4.1 Flash through pi
and Opus 5.5 through Claude Code (account 2's subscription), 10 scenarios (two of each family but fights, four
fights including `fight_act1_windmill` and its Tactician twin; no holdout), condition `rules`, 6 runs each. Two of
the ten have no start save yet (`fight_act1_windmill_tactician`, `fight_act2_house_of_healing`: `start.not_ready`),
so the plan skips them: 96 runs, 48 paid. Six runs per cell give each scenario's pass rate to about ±0.3 (a Wilson
95% interval at 3/6 is 19-81%, at 0/6 0-39%), enough to see which scenarios are near 0 or 1 for both models (no
spread: fewer runs) and which sit in between (the ones that need 5 or more). The run count per scenario is fixed
from these rates before any published series.

Cost bound: $0.25 per DeepSeek run (the longest measured flash run, live4's nautiloid, billed $0.19), $12.00 for all
48; Claude Code runs cost no USD but use the subscription's weekly limit. The caps of the eight scenarios that run
add up to 10.25 game hours per model and repeat, 123 hours for all 96 runs if none ends early (`nautiloid` ends at
its milestone). Jobs run repeat-major, so stop after repeat 1 (8 paid runs, at most $2.00) and check whether any run
hit the $0.25 cap before going on. Check the plan with
`uv run python runner/play.py bench plan --matrix packages/bench/matrices/variance-pilot.toml`
(packages/bench/tests/test_matrix_cli.py test_the_variance_pilot_plan checks its output). It plays with `--runner session` (the
default; the legacy runner refuses every goal that isn't one milestone) and needs the save scenarios' start saves and the owner's go-ahead for the spend.

## Not live-tested yet

- Every scenario but `nautiloid` in a bench run: 17 start saves are made and registered (research/ovh/live6.md §2),
  but no run has loaded one yet.
- A Tactician scenario: its save is not made, and the harness doesn't read the game's difficulty yet.
- `model_time` and `persona` in run records (both unit-tested on real session files).
- Any non-Claude client in a series (section 3).
- `tools/bench_preflight.py` on a machine other than the test machine (unit-tested with fixture Steam trees; its
  `AutoUpdateBehavior` meanings come from community documentation, not Valve's: fairly sure of 0 and 1, less of 2).
- The container firewall on a real host, and a real `--sandbox container` session (offline tests and a Docker-only
  smoke without game or credentials pass: `runner/test_sandbox.py`, `tools/container/sandbox_smoke.py`).
