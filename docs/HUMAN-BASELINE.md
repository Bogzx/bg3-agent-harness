# Human baseline

How the owner plays the benchmark scenarios through the normal game, what to record, and how the attempts become
the leaderboard row "human (owner)". The reader is `bg3bench.human` (packages/bench), the file format is
`packages/bench/schema/human.v1.schema.json`.

The row is a reference point. The owner knows BG3 and may have seen these scenes before, so it shows what an
experienced player does with the same start, goal and cap. It is scored exactly like a model's headline: first try,
no reload, inside the scenario's cap.

## Before a session

- Play on the machine and game build the models use (dev2, Steam in Offline Mode). Note the game version from the
  main menu.
- Settings as the scenario says (`game_settings` in `packages/bench/scenarios/<name>.json`): Balanced, Karmic Dice
  off. Leave everything else at your normal settings and write down anything unusual in `notes`.
- The dice seed (`game_settings.seed`) is not applied to human attempts: it needs the mod's setup call, which only
  the runner makes. The model runs played so far didn't have it either (the play.py hook is still to come).
- Never play from a playthrough save. Use the scenario's registered start save `SCN-<name>`
  (`<store>/<name>/`, registered with `python -m bg3bench.authoring register`; the store lives outside the repo).
  Copy the save folder from the store into the game's `Savegames/Story` folder (the store copy stays untouched).
  `nautiloid` starts from a new game instead.
- Read the scenario's start note (`start.note`: where, objective, party, journal). That is all a model gets about
  the situation, so read nothing else about the scene first (no wiki, no guides).

## One attempt

1. Load `SCN-<name>` (for `nautiloid`: start a new game and make the character its profile describes, a half-elf fighter named Tav).
2. Start a stopwatch when you can first move the party after the load (for `nautiloid`: after character creation,
   when you first control Tav on the ship). Don't stop it for menus, the ESC menu, or thinking: the game keeps
   running in real time for the models too.
3. Play toward the goal (table below). The goal is met at the moment the game shows it; note the stopwatch time then
   (`t_pass_s`).
4. The attempt ends when the goal is met, when you give up, or at the cap (`caps.run_hours`), whichever comes first.
   Note the stopwatch time (`time_s`). Playing past the cap is fine, but the attempt then counts as failed.
5. A reload is any load of a save during the attempt (a quickload after a bad fight counts). Write down how many.
   An attempt with a reload is recorded but doesn't count as a headline success.
6. If the attempt was not a fair try (the game crashed, the wrong save was loaded, you were interrupted), record it
   anyway with `void` set to the reason. Void attempts count as invalid runs, like a model's runner error.
7. For a choice scenario, record what you picked at each decision (`picks`: the scene and the option text).

Every attempt goes in the file, failed ones too, numbered 1, 2, 3, ... per scenario in the order played. The reader
refuses gaps and repeats, so a failed attempt can't be left out. Three attempts per scenario matches the models'
`runs: 3`; more is fine.

| scenario | cap | the goal is met when |
|---|---|---|
| `nautiloid` | 4 h | the party escapes the nautiloid (the crash landing on the beach) |
| `act1_gold_1000` | 45 min | the party has 1000 gold or more |
| `fight_act1_windmill` | 1 h | the windmill fight is won |
| `fight_act2_house_of_healing` | 1 h | the surgery fight is won |
| `fight_act2_balthazar` | 1.5 h | the Balthazar fight is won |
| `choice_act1_karlach` | 30 min | a third companion joins the party, or a fight is won |
| `choice_act1_minthara` | 30 min | a fight is won, or the party is back in the Emerald Grove (or the Hollow) |
| `fight_act1_grove_gate` | 30 min | the goblin raid at the Grove gate is won |
| `fight_act1_dror_ragzlin` | 1 h | the throne-room fight is won |
| `fight_act1_gith_patrol` | 45 min | the githyanki patrol fight is won (holdout) |
| `fight_act2_moonrise_assault` | 1 h | the Moonrise main-floor fight is won |
| `fight_act2_ketheric` | 1.5 h | the journal closes "Defeat Ketheric Thorm" |
| `fight_act3_sarevok` | 1 h | the tribunal fight is won |
| `fight_act3_cazador` | 1.5 h | the Cazador fight is won (holdout) |
| `act1_level_3` | 1 h | every party member is level 3 or more |
| `act1_recruit_3` | 1 h | three companions travel with the avatar |
| `act1_goblin_camp_no_fight` | 45 min | the party is inside the Shattered Sanctum and no combat started on the way |
| `act1_get_item` | 45 min | the party holds the Idol of Silvanus (holdout) |
| `choice_act1_arabella` | 30 min | the journal closes "Save Arabella", or a fight is won |
| `choice_act2_nightsong` | 30 min | the journal closes "The Chosen of Shar" or "Daughter of Darkness", or a fight is won |
| `choice_act3_wyll_pact` | 30 min | the journal closes the Grand Duke's quest, or a fight is won (holdout) |
| `crash_site_grove` | 1 h | the party reaches the Emerald Grove |
| `act2_last_light` | 1 h | the party reaches the Last Light Inn (holdout) |

The choice scenarios' goal only ends the attempt; the picks are compared with the models', never scored. The table
says in words what each scenario's `goal` checks in game state; the scenario file is the definition.

## The results file

One JSON file per player, kept outside the repo until a release (for example `runtime/human/owner.json`):

```json
{
  "schema": 1,
  "player": "owner",
  "game_version": "4.1.1.6758295",
  "attempts": [
    {"scenario": "fight_act1_windmill", "attempt": 1, "date": "2026-10-23", "passed": true,
     "time_s": 1260, "t_pass_s": 1245, "reloads": 0, "void": null},
    {"scenario": "fight_act1_windmill", "attempt": 2, "date": "2026-10-23", "passed": true,
     "time_s": 1710, "t_pass_s": 1702, "reloads": 1, "void": null, "notes": "reloaded after a wipe"},
    {"scenario": "fight_act1_windmill", "attempt": 3, "date": "2026-10-23", "passed": false,
     "time_s": 410, "t_pass_s": null, "reloads": 0, "void": "the game crashed"},
    {"scenario": "choice_act1_karlach", "attempt": 1, "date": "2026-10-24", "passed": true,
     "time_s": 640, "t_pass_s": 610, "reloads": 0, "void": null,
     "picks": [{"scene": "Anders at the tollhouse", "pick": "Agree to find Karlach"}]}
  ]
}
```

| key | what |
|---|---|
| `player` | short lowercase id; the leaderboard row is `human (<player>)` |
| `game_version` | the game build from the main menu |
| `scenario`, `attempt` | the scenario name and the attempt number (1..n per scenario, in the order played) |
| `date` | the day it was played, `YYYY-MM-DD` |
| `passed` | the goal was met, as you saw it |
| `time_s` | stopwatch seconds from the load to the end of the attempt |
| `t_pass_s` | stopwatch seconds when the goal was met; `null` unless `passed` |
| `reloads` | loads during the attempt |
| `void` | `null`, or why the attempt was not a fair try |
| `picks` | choice scenarios only: `[{scene, pick}]` |
| `notes` | optional free text |

Check a file:

```bash
cd packages/bench
uv run python -c "
import sys; from pathlib import Path
from bg3bench import human, scenario
sc = {p.stem: scenario.load(p) for p in Path('scenarios').glob('*.json')}
human.load(sys.argv[1], sc); print('ok')" ../../runtime/human/owner.json
```

## How it is scored

`human.pairs(data, scenarios, bench_version)` turns each attempt into one run for `results.build_results`, next to
the models' runs:

- passed = `passed`, no reload, `t_pass_s` within the cap, and not void. Everything else is a failed run (a void one
  is invalid and left out of the statistics).
- the run id is `human-<player>-<scenario>-aNN`; model `human (<player>)`, no condition, no cost, zero tokens, no
  thinking time, wall time = `time_s`.
- a choice attempt carries its picks into the release's choice table, with no persona fit score (the judge scores
  models' explanations; a human attempt has none).

The leaderboard then shows the row like any other: k/n with the Wilson interval, pass^k, median wall time and time
to goal; the cost columns show "–" and the row has no point on the success-vs-cost plot. The human runs have no
`runs/<run_id>/` folder, so their manifest links on the page lead nowhere until publishing ships the human file
(`publish.py` doesn't yet).

Time comparisons are rough: a model's wall time includes its own latency, and in the fights a human's turn takes
seconds where a model's takes minutes. Compare success rates first.
