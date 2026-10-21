# Authoring a benchmark scenario

A scenario is a fixed start (a new game or a start save), a goal read from game state, caps and game settings
(`schema/scenario.v2.schema.json`, checked by `bg3bench.scenario.validate`). This page is the checklist for adding
one. The code for each step is in `bg3bench.authoring`. It is tested offline; the steps that need the game are
marked **live**.

## Checklist

1. **Pick the family and the goal.** One of `fight`, `sandbox`, `choice`, `exploration`, `puzzle`
   (docs/BENCHMARK-PLAN.md). Write the goal in the predicate language (`bg3bench.predicates`, e.g.
   `{"gold": {"gte": 1000}}`) or use `until: <milestone>`. A goal a do-nothing agent could pass is refused (D8). Add a
   positive conjunct, such as an area, a level or gold.
2. **Make the start save (live).** Use a neutral character: one plain capitalised name (`Tav`, `Ana`), never a
   model, client, person or playthrough name, and a standard build. Turn Karmic Dice off (D9), stand out of combat
   with no dialogue open, and save with the label `SCN-<name>`. Never use one of a playthrough's `Sxxx-` saves.
3. **Register it.** `authoring.register_save(<save folder>, <store>, <name>, inspector=..., now=time.time)` copies
   the folder (the original is never touched), refuses a non-neutral name or a wrong label, runs the save inspector
   (`tools/saves/inspect.py`, B-mod) on the copy, and writes `<store>/<name>/registration.json` with the `.lsv` sha1
   the runner checks and a sha256 per file. If the inspector flags something (a Steam ID, a user path), the copy is
   kept for you to look at and registration fails. Whether start saves may be published is still open
   (research/arch/bench.md, open question 3), so keep the store out of the repo until that is decided.
4. **Draft the scenario file.** `authoring.draft_scenario(registration, title=..., family=..., where=...,
   objective=..., brief=..., run_hours=..., goal=...)` returns a validated v2 scenario.
   `authoring.write_draft(sc, "scenarios")` writes `scenarios/<name>.json` and refuses to overwrite one: a series
   freezes the file's digest, so an edit means a new name. The start note holds neutral game facts only (where,
   objective, party, journal), never a playthrough's history.
5. **Set the caps** from one pilot run. For a sandbox goal, calibrate the target so the strongest model reaches it
   about half the time within the cap. `max_cost` is also the matrix runner's budget reserve for each run.
6. **Record the oracle trajectory (live, D12).** One successful run by a strong model through Claude Code, a human
   run where no model succeeds, or a scripted solve for a trivial scenario. Then call
   `authoring.attach_oracle("scenarios/oracle", sc, <trajectory file>, kind=..., run_id=..., passed=True,
   t_pass_s=...)`. It writes `scenarios/oracle/<name>.json` with the trajectory's sha256 and the scenario digest it
   was made on. This proves the goal is reachable with the tools and caps a model gets, and that the grader fires.
7. **Run both baselines (live).** `authoring.run_baselines(runtime/matrix/baselines-<name>, <name>, run_one,
   now=time.time, sleep=time.sleep, guard=...)` runs do-nothing 5 times and the random agent with seeds 1-5 through
   the matrix runner, on a budget of $0, so a baseline that costs money stops at once. `run_one` today is
   `bg3bench.adapters.legacy_cli.LegacyCli` (it needs a `baseline` client registered in `runner/clients.py`, see
   below). The rule: each baseline passes in under 5% of at least 5 valid runs (`out["ok"]`). A scenario that fails
   it gets a stronger goal, not more runs.
8. **Write the quiz** (`quizzes/<name>.json`, `bg3bench.contamination`): closed-book questions that only prior
   knowledge answers. Models take it with no tools, and the score is reported next to the pass rate.
9. **Decide on holdout.** Set `holdout: true` for the private set. Holdout scenarios are run, never published:
   `results.build_results(..., holdout=contamination.holdout_names(scenarios))`.

## Running the baselines from the legacy runner

`bg3bench.baselines.client.BaselineClient` implements the runner's `Client` protocol (`runner/clients.py:80-90`)
with model slugs `baseline:donothing` and `baseline:random:<seed>`. To play one through `runner/play.py run
--client baseline`, the owner of `runner/clients.py` registers it, passing the runner's `Event` class:

```python
from bg3bench.baselines.client import BaselineClient
CLIENTS["baseline"] = lambda **kw: BaselineClient(event=Event, **kw)
```

The agent processes run `uv run --project packages/bench --extra baselines python -m bg3bench.baselines.<agent>`.
They launch the bg3 server from `play/.mcp.json` and read the deadline from `BG3AI_SESSION_DEADLINE`.
