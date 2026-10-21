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
| `schema/` | JSON Schemas: scenario v2, goal, run manifest, run result, release results, roll log |
| `docs/SCENARIOS-AUTHORING.md` | the checklist for adding a scenario, from start save to baselines |
| `src/bg3bench/scenario.py` | load and validate scenario files, and convert the v1 files in `scenarios/` at the root |
| `src/bg3bench/scenario_v1.py` | the v1 format the legacy runner (`play.py run --scenario`) still plays; `scenarios/*.json` here is canonical, the root `scenarios/` is legacy and closed (its README) |
| `predicates.py`, `graders.py`, `facts.py` | goals over `facts.jsonl`: state, ever and never leaves, progress curve |
| `methods.py` | how a sandbox goal was reached (trade, pickpocket, loot, quest): reported, never graded |
| `stats.py`, `luck.py` | Wilson intervals, pass^k, two-level bootstrap, paired differences, the luck covariate |
| `manifest.py`, `results.py` | run manifest, run result and release `results.json` builders |
| `conditions.py`, `contamination.py` | condition ids (`rules`, `rules+coaching@<hash8>`, `+wiki`, ...), quizzes, holdouts |
| `matrix.py` | the matrix runner: models x scenarios x conditions x repeats, resumable, under a global budget |
| `series.py`, `resets.py`, `pins.py`, `records.py` | a series' runs: naming, runs.jsonl records, frozen caps/judge/client/sandbox, per-run resets and the FAIR-2 knowledge lock, version pins, the runs table and summary (moved from runner/runs.py, which re-exports them) |
| `preflight.py` | can this machine run the benchmark (CLI: `python3 tools/bench_preflight.py`) |
| `adapters/` | temporary: the matrix runs jobs through `runner/play.py` until the harness has `run_session` |
| `baselines/` | do-nothing and seeded random agents that speak MCP to the bg3 server |
| `fairness_probes.py` | every bg3 tool has a hidden-fact probe or a written exemption (`fairness_probes.json`) |
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

The matrix runner has no command line yet. It is used from Python (`bg3bench.matrix`) with the play.py adapter;
one job is one `runner/play.py run --scenario ...` call ([docs/QUICKSTART.md](../../docs/QUICKSTART.md)).

## Tests

```bash
cd packages/bench
uv run pytest -q tests --cov=bg3bench
uv run ruff check src tests
```

The tests are offline and deterministic. `test_probes.py` lists the bridge's tools through `uv run` in `bridge/`.
Tests that need an optional extra (inspect-ai, mcp, scipy) are skipped when it is not installed.

## Status (2026-10-21)

Built and tested offline. Only `nautiloid` has run live, and that was through the older v1 series runner. The other
scenarios need their start saves, an oracle run and both baselines before they count.
