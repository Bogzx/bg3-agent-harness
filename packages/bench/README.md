# bg3bench: the BG3 benchmark

Measures models playing Baldur's Gate 3 through the harness. It reads what the harness writes (the event log,
`facts.jsonl`, run records) and never reaches into harness internals. It imports neither the harness's Python code
nor the playthrough app; import-linter checks that.

Version: `pyproject.toml` (0.1.0). A results release is the tag `bench-vX.Y` with this package at `X.Y.0`
([CHANGELOG.md](../../CHANGELOG.md)).

## What is here

| Path | What |
|---|---|
| `scenarios/*.json` | scenario files v2: start, goal, caps, game settings, dice seed, timing, conditions |
| `scenarios/quizzes/` | closed-book quizzes per scenario, for contamination checks |
| `schema/` | JSON Schemas: scenario v2, goal, run manifest, run result, release results, roll log, human results |
| `docs/SCENARIOS-AUTHORING.md` | the checklist for adding a scenario, from start save to baselines |
| `src/bg3bench/scenario.py` | load and validate scenario files, and convert the v1 files in `scenarios/` at the root |
| `src/bg3bench/scenario_v1.py` | the v1 format the legacy runner (`play.py run --scenario`) still plays; `scenarios/*.json` here is canonical, the root `scenarios/` is legacy and closed (its README) |
| `predicates.py`, `graders.py`, `facts.py` | goals over `facts.jsonl`: state, ever and never leaves, progress curve |
| `methods.py` | how a sandbox goal was reached (trade, pickpocket, loot, quest): reported, never graded |
| `stats.py`, `luck.py` | Wilson intervals, pass^k, two-level bootstrap, paired differences, the luck covariate |
| `manifest.py`, `results.py` | run manifest, run result and release `results.json` builders |
| `conditions.py`, `contamination.py` | condition ids (`rules`, `rules+coaching@<hash8>`, `+wiki`, ...), quizzes, holdouts |
| `matrix.py`, `matrix_cli.py`, `matrices/` | the matrix runner: models x scenarios x conditions x repeats, resumable, under a global budget; its command line and matrix files |
| `series.py`, `resets.py`, `pins.py`, `records.py` | a series' runs: naming, runs.jsonl records, frozen caps/judge/client/sandbox, per-run resets and the FAIR-2 knowledge lock, version pins, the runs table and summary (moved from runner/runs.py, which re-exports them) |
| `preflight.py` | can this machine run the benchmark (CLI: `python3 tools/bench_preflight.py`) |
| `adapters/` | temporary: the matrix runs jobs through `runner/play.py` until the harness has `run_session` |
| `baselines/` | do-nothing and seeded random agents that speak MCP to the bg3 server |
| `fairness_probes.py` | every bg3 tool has a hidden-fact probe or a written exemption (`fairness_probes.json`) |
| `human.py` | the human baseline: reads a player's results file and adds the `human (<player>)` row ([docs/HUMAN-BASELINE.md](../../docs/HUMAN-BASELINE.md)) |
| `choices.py` | the choices grader: a choice scenario's picks from the facts' choice events (compared, never scored) and, given a judge, their fit to the character brief |
| `authoring.py` | the scenario authoring steps: register a start save, draft the file, attach the oracle, run the baselines ([docs/SCENARIOS-AUTHORING.md](docs/SCENARIOS-AUTHORING.md)) |
| `seriesview.py` | the series view: one row per run of a `play.py run --runs N` series, its spread and frozen settings, and the live run |
| `report.py`, `site/`, `export_inspect.py`, `publish.py` | series report, leaderboard page, Inspect `.eval`, release upload (dry run only) |

## Commands

```bash
# series report (Markdown + HTML) from a series' run folder
PYTHONPATH=src python3 -m bg3bench.report <runs_dir> <out_dir> [--series NAME]

# static leaderboard page from a release's results.json
PYTHONPATH=src python3 -m bg3bench.site.build <results.json> <out.html> [--families JSON]

# the files a release would upload, written to a folder instead (holdout scenarios are refused)
PYTHONPATH=src python3 -m bg3bench.publish <release_dir> --tag bench-v0.1 --hf-repo OWNER/NAME --dry-run <out_dir>
```

From the repo root:

```bash
# the runner's file for a v2 scenario (v1 shape + game_settings, timing); a goal that isn't one milestone is refused
uv run --project packages/bench python -m bg3bench.scenario resolve packages/bench/scenarios/nautiloid.json > resolved.json
# the matrix runner; one job is one bg3harness run_session (adapters/session_run.py, no resume: a crash is an invalid
# run, rerun), with the harness's RunHost passed to matrix_cli.main(host=...); --runner legacy (until Phase D) plays
# each job as one runner/play.py run --scenario-file <resolved> ... call instead
uv run --project packages/bench python -m bg3bench.matrix plan --matrix packages/bench/matrices/<file>.toml
uv run --project packages/bench python -m bg3bench.matrix run|resume|status --matrix <file>.toml --runner legacy

# the series view on http://127.0.0.1:8766/series, with the live row (the recorded rows only: python -m bg3bench.seriesview)
python3 runner/dashboard/seriesview.py

# register a start save for a scenario (copies it, runs the save inspector, writes registration.json)
uv run --project packages/bench python -m bg3bench.authoring register <save folder> <store> <name>
```

`plan` is a dry run: the jobs, the budget reserve, and what play.py can't play exactly yet for each condition. A
paid job needs `runtime/budget.json` (`{"cap": ..., "spent": ...}`); without it `run` stops before its first job.
The format of a matrix file is in `matrix_cli.py`'s docstring; `matrices/pilot-2026-10-22.toml` is an example.

The choice scenarios' result has the picks (`choices`), and the leaderboard adds a table of who picked what at each
scene. The fit score needs a judge: `bg3harness.judge` takes any client profile (Claude through `claude -p`, or a pi
profile's endpoint, which needs a cost cap), and a release refuses a judge that is also a contestant.

## Tests

```bash
cd packages/bench
uv run pytest -q tests --cov=bg3bench
uv run ruff check src tests
```

The tests are offline and deterministic. `test_probes.py` lists the bridge's tools through `uv run` in `bridge/`.
Tests that need an optional extra (inspect-ai, mcp, scipy) are skipped when it is not installed.

Two tests (marker `e2e`, run by default; `-m "not e2e"` skips them) play the whole pipeline with no game and no key,
through the real bridge and the fake mod (`bg3harness.testing.fakemod`, a TCP server that answers the bridge's mod
calls from a scripted game):

- `test_e2e_offline.py`: a scenario file, pi with a scripted stub model, the bridge, the fake mod, `facts.jsonl`,
  the grader, `result.json`, the stats, the report and the leaderboard. Needs `tools/pi/node_modules`
  (`cd tools/pi && npm ci --ignore-scripts`), skipped without it.
- `test_baseline_validity.py`: the do-nothing and random baselines play every scenario through the matrix runner
  from the scenario's start hosted by the fake mod, and every grader must fail them. `choice_act1_karlach` is a
  strict xfail until the harness keeps the avatar flag in `facts.jsonl`.

## Status (2026-10-22)

Built and tested offline. Only `nautiloid` has run live, through `play.py run --scenario`, which still reads the v1
file in the root `scenarios/`: Claude Opus 5.5 through Claude Code and DeepSeek Flash through pi, one run each
(research/ovh/live3.md). 17 of the 27 save starts have a registered start save (research/ovh/live6.md §2); the
other ten say why not (`start.not_ready` or no save) and the matrix skips them. Each still needs an oracle run and
both baselines before it counts. The human baseline protocol is [docs/HUMAN-BASELINE.md](../../docs/HUMAN-BASELINE.md).
