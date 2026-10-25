# Authoring a benchmark scenario

A scenario is a fixed start (a new game or a start save), a goal read from game state, caps and game settings
(`schema/scenario.v2.schema.json`, checked by `bg3bench.scenario.validate`). This page is the checklist for adding
one. The code for each step is in `bg3bench.authoring`. It is tested offline; the steps that need the game are
marked **live**.

## Checklist

1. **Pick the family and the goal.** One of `fight`, `sandbox`, `choice`, `exploration`, `puzzle`
   (docs/BENCHMARK-PLAN.md). Write the goal in the predicate language (`bg3bench.predicates`, e.g.
   `{"gold": {"gte": 1000}}`) or use `until: <milestone>`. A goal a do-nothing agent could pass is refused (D8). Add a
   positive conjunct, such as an area, a level or gold. For "as much as you can" use `maximise: {"fact": "gold"}`
   instead of a goal: no pass, the run plays its whole cap and scores the fact's gain (`graders.measure`); never
   dress a maximum up as a high target, or a target as a maximum.
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
   objective=..., brief=..., run_hours=..., goal=..., seed=...)` returns a validated v2 scenario. `seed` is the
   dice seed the mod writes after every load (D9), so every run starts from the same rolls; the manifest records it.
   `timing` follows the family: exploration and sandbox are `realtime` (the world runs while the model thinks;
   there is no think-pause, D7, and results show thinking time next to wall time), fights default to `turn_based`.
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
   `bg3bench.adapters.legacy_cli.LegacyCli` (it needs a `baseline` client registered in `bg3harness/clients.py`, see
   below). The rule: each baseline passes in under 5% of at least 5 valid runs (`out["ok"]`). A scenario that fails
   it gets a stronger goal, not more runs.
8. **Write the quiz** (`quizzes/<name>.json`, `bg3bench.contamination`): closed-book questions that only prior
   knowledge answers. Models take it with no tools, and the score is reported next to the pass rate.
9. **Decide on holdout.** Set `holdout: true` for the private set. Holdout scenarios are run, never published:
   `results.build_results(..., holdout=contamination.holdout_names(scenarios))`.

## Running the baselines from the legacy runner

`bg3bench.baselines.client.BaselineClient` implements the runner's `Client` protocol (`bg3harness/clients.py` `Client`)
with model slugs `baseline:donothing` and `baseline:random:<seed>`. To play one through `runner/play.py run
--client baseline`, the owner of `bg3harness/clients.py` registers it, passing the runner's `Event` class:

```python
from bg3bench.baselines.client import BaselineClient

CLIENTS["baseline"] = lambda **kw: BaselineClient(event=Event, **kw)
```

The agent processes run `uv run --project packages/bench --extra baselines python -m bg3bench.baselines.<agent>`.
They launch the bg3 server from `play/.mcp.json` and read the deadline from `BG3AI_SESSION_DEADLINE`.

## The first set (packages/bench/scenarios/, 2026-10-21)

Seven v2 files, each with its closed-book quiz in `scenarios/quizzes/<name>.json` (tests/test_scenario_set.py checks
the whole set): `nautiloid` (exploration, new game, converted from `scenarios/nautiloid.json`), `act1_gold_1000`
(sandbox, 45 min, method attribution on), three fights picked from claude-run-2 (`fight_act1_windmill`, an Act 1
ambush; `fight_act2_house_of_healing`, mid-size; `fight_act2_balthazar`, a boss) and two choice scenes from its
decisions.md (`choice_act1_karlach`, `choice_act1_minthara`, neutral brief). Every save start has `save: null` and
`start.needs_save` naming the exact game point: make the save there with an avatar named Tav, register it (step 3)
and draft it (step 4) with the same seed. The draft drops `needs_save`. Delete the placeholder by hand before
`write_draft`, which never overwrites; that is safe because no series can run on a file that has no save. A choice scene's goal only ends the run; the choice
itself is read from the trajectory.

## Round 11 (2026-10-22): 23 scenarios

Sixteen more, the same conventions (tests/test_scenario_set.py checks the set's shape, the holdout, the resolve split
and the quizzes; tests/test_baseline_validity.py hosts each start in the fake mod and fails both baselines on it):

- fights (10 in all, every act): `fight_act1_grove_gate` (allies, the first big fight), `fight_act1_dror_ragzlin`
  (a boss in a crowd), `fight_act1_gith_patrol` (small and elite), `fight_act2_moonrise_assault` (large, with the
  Harpers), `fight_act2_ketheric` (a boss; the goal is the journal's "Defeat Ketheric Thorm", so a phase change can't
  end the run), `fight_act3_sarevok` (a boss), `fight_act3_cazador` (a boss in a crowd);
- sandbox goals: `act1_level_3` (`party_level` gte 3, the lowest member), `act1_recruit_3` (`companions` gte 3),
  `act1_goblin_camp_no_fight` (the Shattered Sanctum reached and no combat: the area is the D8 positive conjunct),
  `act1_get_item` (`item` Idol of Silvanus);
- choices: `choice_act1_arabella`, `choice_act2_nightsong`, `choice_act3_wyll_pact` (neutral brief; the goal is a
  journal quest closing or a won fight, from the quest titles claude-run-2 recorded);
- exploration: `crash_site_grove` (milestone `grove`), `act2_last_light` (milestone `last_light`). With `nautiloid`
  they are the only ones `scenario.resolve` accepts today (one milestone).

Holdout (run, never published): `fight_act1_gith_patrol`, `fight_act3_cazador`, `act1_get_item`,
`choice_act3_wyll_pact`, `act2_last_light` (5 of 23, one per family but puzzle). Seeds 1008-1023. Every new file says
`requires: {vision: false, min_context: null}`: nothing in the set reads the screen, and the context floor waits for a
pilot's peak. The saves to make are in research/ovh/bench.md "Start saves to make".

## Round 20 (2026-10-22): Tactician variants, 28 scenarios

Five fights get a harder twin at Tactician: `fight_act1_windmill_tactician`, `fight_act1_dror_ragzlin_tactician`,
`fight_act1_gith_patrol_tactician` (holdout, like its base), `fight_act2_balthazar_tactician`,
`fight_act3_sarevok_tactician`. That makes 15 fights (the plan's 10-15) and 28 scenarios; the holdout is 6 of 28.
A variant is its base with `game_settings.difficulty` "Tactician", its own seed (1024-1028), " (Tactician)" in the
title, a first line in `about` and the difficulty in `needs_save`; goal, caps, party, brief and the quiz's questions
are the base's (tests/test_scenario_set.py test_a_tactician_variant_is_its_base_at_tactician). Same caps on purpose:
the difficulty is the only thing that differs, so the pair measures it.

How the difficulty is set: it lives in the save (the Ruleset component), and the mod only reads it (`scn.difficulty`
refuses a write: the game sets it through its Options flow). So each variant needs its own start save: load the
base's start moment, switch the game options' difficulty from Balanced to Tactician, then save as SCN-<name>. The
run's settings check compares `game_settings.difficulty` with the game's readout and refuses a contradiction, but the
harness readout doesn't read `scn.difficulty` yet (bg3harness/game_settings.py: always null), so today a wrong save
is only logged as `settings_unverified`.

No Honour variants: BG3 can't switch a running game to Honour (it is picked at a new game, with one save slot and
no reloads), so an Honour fight needs an Honour playthrough to that point, and the bench's save starts and reruns
from a start save don't fit its single save. The plan's `honour_survive_30m` stays unwritten for the same reason.

## Round 21 (2026-10-22): the start saves as made

The live worker made the start saves (research/ovh/live6.md §2; store `~/bg3-start-saves/<scenario>/`). Each made
save is the scenario's `start.save` (its `Tav-<id>__SCN-<name>` folder) and `start.save_sha1` (the store's
`registration.json` `lsv_sha1`), and `needs_save` is gone. Where a save holds something else than the file said, the
file follows the save: `act1_gold_1000`'s party levels (Tav 2, Shadowheart 2, Gale 1), Wyll in
`choice_act1_arabella`'s party (level 1), Wyll at level 3 in `choice_act1_karlach`, `act1_level_3`'s Shadowheart
79 m away, `fight_act2_balthazar`'s start past the bridge, ~11 m from Balthazar (its twin's note too); the rest
(a pending level-up, no long rest in `fight_act1_gith_patrol`) is in `about`.

A start that can't be run yet says why in `start.not_ready`; a save start with no save is not ready either
(`scenario.not_ready`). The matrix gives such a scenario no jobs and `bench plan`/`run` list it as "skipped, not
ready: <why>". Not ready today: `fight_act2_house_of_healing` (not made: needs a hand-played approach),
`fight_act2_ketheric`, `fight_act3_sarevok`, `choice_act3_wyll_pact`, `fight_act3_cazador` (made, but the save
inspector refused them on false positives: once a fixed inspector registers the same save, delete `not_ready`) and
the five Tactician twins, whose `needs_save` now names their base's start save to switch to Tactician and resave.

## Round 22 (2026-10-25): maximise runs

Two sandbox runs with no target (scenario `maximise`, suite bench-v0.7): `act1_gold_max` plays act1_gold_1000's start
save (its folder and sha1, so the two scenarios share one save) and `act3_gold_max` is its Act 3 twin, with the same
task and the same 45 minutes so the acts compare. act1_gold_1000 keeps its 1000-gold target. `act3_gold_max` has no
start save yet (`needs_save`). Its source is claude-run-2's last `S047-end` (folder `Claude-011126099__S047-end`, lsv
sha1 97bd737ecf85, 2026-10-11 03:00), where the party stands at the Lower City Central Wall plaza 2 m from Mohan with
410 gold, everyone level 7 at full HP and Gale at camp. Make it as live6 made the others: copy that folder to a
`PROBE-` copy, launch it with `BG3AI_SCENARIO_SETUP=1`, set Balanced in the game menu, and change nothing else. Then
run `uv run --project bridge python tools/saves/make_start_save.py act3_gold_max --owner Tav`. Finally set
`start.save` / `start.save_sha1` from the store's registration.json, drop `needs_save`, and re-pin the suite digest
(a patch bump: the score rule is unchanged).

## Round 23 (2026-10-25): the avatar's race and name

Live made `act3_gold_max`'s start save (research/fix/live.md "Bench B1"; registered in suite bench-v0.8 with the
start the live worker read: Gale level 6 at camp, the avatar with a level-up to 8 pending, max HP halved by the
Explorer -> Balanced switch). Two things it found hold for every start save:

- The avatar is a half-orc (the inspector's `Race` HalfOrc, Paladin/Devotion, in all 22 stored saves), so every save
  scenario's `profile.character.brief` says half-orc paladin. A new save scenario's brief must name the race and
  class the inspector reads.
- A loaded `Tav-…__SCN-*` save names its avatar "Claude" again (CustomName, CharacterCreationStats.Name and the
  DisplayName text): make_start_save's rename only reached the folder's owner. The runner now renames the avatar
  to `profile.character.name` at a save-start run's start (`scn.rename_avatar`, re-applied by the mod after every
  load), so no start save has to be made again. make_start_save still renames for the folder name and undoes it
  after saving.
