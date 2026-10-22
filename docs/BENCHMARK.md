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
| Steam updates | Steam in offline mode for the whole series | preflight WARN; the runner refuses to launch with Steam online where the machine requires offline (`~/.config/bg3ai/require-steam-offline` or `BG3AI_REQUIRE_STEAM_OFFLINE=1`) |
| Scenarios | `scenarios/<name>.json`: start, `until` milestone, caps, profile, resets ([scenarios/README.md](../scenarios/README.md)) | `series.json` `scenario.digest` + the start save's sha1 |
| Caps | per scenario (`run_hours`, `max_cost`, `max_tokens`) | `series.json` `caps` (`--allow-cap-change` records a change) |
| Persona judge model | `claude-sonnet-5-5` (`runner/judge.py`, `$BG3AI_JUDGE_MODEL`); never the model under test | `series.json`; `--allow-judge-change` records it |
| Latency reference speeds | 100 output tok/s, 0.75 s TTFT (`bg3harness/model_time.py` `REF_OUT_TOK_S`, `REF_TTFT_S`) | constants, recorded in each record's `model_time.ref` |
| Keeper model | `same` as the play model (or `--no-keeper` for every model) | recorded per run |
| Agent-facing text | `play/CLAUDE.md`, playbooks, tool descriptions: tag one commit and don't edit it during a comparison | repo pin |

Known bias: the agent-facing text was tuned on Claude's failures. Read
[docs/BENCHMARK-BIAS.md](BENCHMARK-BIAS.md) before claiming a cross-model result; its "What a fair cross-model
benchmark should do" list is the bar. On the `overhaul` branch the text is split into rules and coaching with the
anecdotes removed (`packages/harness/prompts/`), but no live run has used the split text yet.

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

- `outcome` (`reached` | `timeout` | `over_budget` | `gave_up` | `runner_error`), `cap`, `clean`
- `time_to_milestone_s`, `wall_min`, `play_min`, `sessions`, `turns`
- `cost_usd`, `tokens` (input, output, cache read, cache write)
- `failures` (each with its class, below), `operator`, `deaths`, `fairness_flags`
- `latency` (idle median/p90, gaps over 10 s, bridge read round trip) and `model_time` (harness vs model time, the
  model part normalised to the reference speeds) *(model_time in records: not live-tested)*
- `persona` (the judge's 0-10 adherence to CHARACTER.md) *(not live-tested)*
- `pins`, `caps`, `scenario`, `model`, `effort`, `keeper_model`, `version_drift`, `cap_changes`

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
  (`operator` non-empty) never counts.

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

## Not live-tested yet

- Every scenario but `nautiloid`: start saves are not made yet, and loading one in a series has never run.
- `model_time` and `persona` in run records (both unit-tested on real session files).
- Any non-Claude client in a series (section 3).
- `tools/bench_preflight.py` on a machine other than the test machine (unit-tested with fixture Steam trees; its
  `AutoUpdateBehavior` meanings come from community documentation, not Valve's: fairly sure of 0 and 1, less of 2).
- The container firewall on a real host, and a real `--sandbox container` session (offline tests and a Docker-only
  smoke without game or credentials pass: `runner/test_sandbox.py`, `tools/container/sandbox_smoke.py`).
