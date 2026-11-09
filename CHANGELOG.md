# Changelog

The repo holds three packages (see [docs/TARGET-ARCHITECTURE.md](docs/TARGET-ARCHITECTURE.md)), each with its own
version:

| Package | Version lives in | Now |
|---|---|---|
| harness (mod, bg3 MCP server, runner, clients) | `packages/harness/pyproject.toml` (`bg3-agent-harness`; `bg3harness.__version__`) | 0.18.0 |
| benchmark | `packages/bench/pyproject.toml` (`bg3bench`; `bg3bench.__version__`) | 0.11.0 |
| benchmark suite | derived: `bench-vX.Y` of bg3bench `X.Y.Z` (`bg3bench.suite`), its files pinned in `packages/bench/suite.json` | bench-v0.11 |
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

- The bench recording's encoder no longer defaults to `h264_nvenc` (on an AMD machine without NVENC, every recorded run died
  at recorder start). It is `BG3AI_BENCH_RECORD_ENCODER`, else the machine's `~/.config/bg3ai/record-encoder`, which
  `bg3harness.doctor` writes once with the first of `h264_nvenc` and `libx264` that encodes a test frame. With
  neither set, `play.py bench` refuses before any run and names the fix.
  The run record and its manifest (`record_encoder`, manifest v1, optional) name the encoder the recording used;
  null when the run wasn't recorded.
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
  out of combat block by default, the minimap block, the PASSED line, a walk ends a batch, roll_check's reroll, trade's SOLD list, known ground to 16 m,
  required camp talks named, a misread variant list refused). bench-v0.10's release cites it; the list is under
  bench-v0.10 below.
- 0.4.0 (2026-11-05): what a maximise run's agent is told changed: the kickoff states the end rule (a stretch with
  no acting call since a restart ends the run) and names the served tools that are acting calls, and the resume
  message no longer says the run goes on until its time cap. It also covers the bg3 tool changes of bench-v0.11
  (bridge/CHANGELOG.md, 2026-11-04 night and 2026-11-05 entries). bench-v0.11's release cited it until 0.5.0.
- 0.5.0 (2026-11-07): what the agent sees changed: `rules/arena.md` on who answers a reaction prompt and when one is
  declined, and in normal play too, `react` with use=true says when its prompt closed and its observers' resources
  hadn't dropped, and a swing the game took the cost of and then cancelled no longer says the game refused it. The
  served tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.6.0.
- 0.6.0 (2026-11-07): what the agent sees changed: `react`'s served definition no longer says Do Not React has never
  closed a prompt with one row per level (it closes one in a match; outside one it's unread), and a cast paused for a reaction no
  longer promises that it goes on or that its outcome shows in the events (a Counterspell stops it). An order the pause
  caught before it started says what 4 live reads showed, that it didn't run after the answer and its action stayed,
  with no promise. The bench prompts don't change. bench-v0.11's release cited it until 0.7.0.
- 0.7.0 (2026-11-07): what the agent can do changed: an order for a character whose last cast is still resolving after
  a reaction is refused, as one sent 1 s after a Counterspell ran and got two casts for one action. It lifts when the
  cast lands, the game takes its cost, or 10 s after any prompt closes. A cast countered with no prompt holds the same
  way and says so. Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.8.0.
- 0.8.0 (2026-11-07): what the agent sees changed: an order sent after the game charged a held attack and before it
  landed took that landing as its own and read `done, no_damage, unrolled=`, then its real swing went. It now waits
  for its own swing and reports that. It also wrote a false `cast_unrolled` to the run's facts, which no longer
  happens. Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.9.0.
- 0.9.0 (2026-11-07): what the agent sees and can do changed, in `level_up`: it closes the page the game opens after a
  finish even past that page's intro; it won't finish without the plan's skills, expertise and passives even when the
  game calls the page complete; `plan.skills` reaches the expertise group; a held name no later group can take comes
  back as `already_held`; `set.<group>_off` names picks that left the page; a refusal names a skill group short of its
  max, where it said nothing was open. The read-back's refusal has no live read yet. Tool definitions and the bench
  prompts don't change. bench-v0.11's release cited it until 0.10.0.
- 0.10.0 (2026-11-07): what the agent sees changed: a raw id of a container spell the character doesn't hold reads
  `the character does not have that action`, where it read `choose a variant:` with variants that were then refused.
  So does a raw id of a call-allies container not held. For an order behind a held cast, a hit or status before its own
  CastSpell no longer counts in its result: a held arrow's late hit had read `damage=[6; 6]` where the order's own was
  6; read live, part 5a. An Osiris-ordered cast that is countered pays every cost but the four turn resources, on a
  prompt or with none, as a hotbar cast did for its slot; it had paid the action alone (a30-read). Read live for a
  spell slot only. A swing a Riposte answered reads as a miss, `done ... no_damage`, when every die on its own cast
  read below its dc and none is a natural 20; otherwise its text keeps the true words and drops `what came of it shows
  in the events`. A weapon attack with no Action among its costs no longer says `extra_attack` in its result, which
  Frenzied Strike did; not read live. Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.11.0.
- 0.11.0 (2026-11-07): what the agent sees changed: every status whose boost frees an Action counts as the free attack,
  in the guard, the action list and a Canceled swing's cost. An L12 Fighter had its second and third swings refused
  `not enough action`, with EXTRA_ATTACK_2 on and read by nothing; it now makes its 3 attacks, read live. An unarmed
  attack and a throw count as the game's check does. A weapon action with an Action cost takes the free attack, read
  live on L12. A hover preview's CastSpellFailed no longer ends the order, which had read `the game refused the cast (no
  reason given)`. Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.12.0.
- 0.12.0 (2026-11-07): what the agent sees changed: words only, no order runs or stops that didn't. Until a held weapon
  attack has landed, a weapon attack with no Action and no free status reads `not enough action yet: ...`, and the list
  gives it `last cast still resolving`. Before, they read `not enough action` and `no action` while a free attack was coming.
  Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.13.0.
- 0.13.0 (2026-11-07): what the agent sees changed: a character whose turn the game runs itself reads so, and nothing
  a client sends reaches the game for it. Live (Goading Attack, in a match and in normal play) the game's AI cast,
  spent and ended such a turn with no order, the HUD read `Can't control: Goaded`, a player's End Turn click did
  nothing and the hotbar cells read can_use false. The mod reads the game's LoseControlComponent by entity (present),
  which lasts as long as the status (present on every sample under all 5 statuses read), not its LoseControl bool
  (false under Command: Approach and Confusion while the AI ran) nor the stats' flag. In combat only: out of combat
  nothing changes, as every read was of a combat turn. Every such status counts, Compelled Duel too, where an order
  once ran but a player's hotbar is grey. The action list, the guard, end_turn, stop and an order that never
  started now say `can't control: Goaded, the game runs its turn`, or `the game runs its turn` when a player can't see
  the status on the character (Minor Illusion shows no HUD bar and is hidden from the portrait); is_my_turn is false,
  the state.combat row reads `game_turn`, not `ended`, get_status reads `turn=e1 (can't control: Goaded, the game runs
  its turn)`, the wait no longer says `now e1's turn` for it, and the side's
  own casts in its own open turn read `cast e1 Red Ash (game's turn): ...` (a reaction the side answers on another's
  turn isn't tagged). Before, the list offered actions, orders failed three
  ways (one naming an unrelated status as the cause) or cut the game's cast, and end_turn ended a turn a player can't
  (6 of 6, 2.15-2.18 s later). The mod's re-sent end for a dropped group end skips such a member. In a match the runner
  gives that phase no cap and no forfeit count and writes `arena_control_lost`, and one that hangs is the game's stall;
  the grade lists `lost_control` and `valid` doesn't read it. Read live: the component under 5 statuses (live.md "## Goaded read 2"); the fix itself not yet. Known, not
  changed: a member both stunned and the game's gets both row flags, and the bridge's end for a passing row is refused
  in the same words (`did not end`); a task of ours queued before the status (a timed-out walk) can't be purged while
  it's on, and whether the game's AI clears it is unread. Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.14.0.
- 0.14.0 (2026-11-08): what the agent sees changed: an order's result in combat states a free attack. Its `after={...}`
  reads `Extra Attack: free` beside `action=` while the game's free-attack status (any of Guard's
  EXTRA_ATTACK_STATUSES) is on at that read, the words get_actions tags the attack with. With none on, in a held
  weapon attack's landing gap at Action 0, it reads `last cast still resolving`, the list's reason there. Otherwise
  nothing: absence claims nothing either way. Before, a swing's result read `action=0` alone, and in the L12 S6 rerun
  7 free attacks went unused by pM's count (3 attacks per action), inferred, all after the turn's last paid swing with
  no list read in between (extra-attack-read.md).
  A Canceled swing's status comes 43-137 ms after its return (pK's reads), so its result stays as before. Tool
  definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.14.1.
- 0.14.1 (2026-11-08): what the agent sees doesn't change. A mirror kit fails on a passive held twice only when the kit
  added it: a twin the game's own class rows gave one twice (a Ranger's UnlockedSpellSlotLevel1 at levels 2 and 3)
  holds it as its reference does, and team 2's first S4 failed on it (live 2026-11-08). bench-v0.11's release cited it
  until 0.15.0.
- 0.15.0 (2026-11-08): what the agent sees changed: a cast the game never started is never sent again by the mod. In
  combat, a cast with no cast state and no step at 3 s after a hit on the caster, a shown status ending on it, or an
  enemy's reaction cast in the same seconds used to be purged and ordered once more by the mod (the hit route since
  2026-10-17, the reaction route since 2026-10-18; a player's dropped order isn't clicked again for them). Now it fails
  as the game left it, naming what the mod read and when: `the game never started it (3 s, no cast, no step): a hit on
  the caster 1.0 s before the order`, `...: someone's reaction cast 0.4 s after the order`. The hold while an enemy's
  reaction plays (the game may start the cast after it, at most 8 s), the countered route (`... the game took the
  action: most likely countered`) and the held-cast routes are unchanged. The result no longer reads `twice: ordered
  again after ...`. Owner, D346: "make it like a real player would". In a match, a reaction prompt its owner leaves
  open is declined after 45 s from its first read on screen, not the arena's turn cap, as a player who lets it pass
  (rules/arena.md states it; the prompts' digest moves). The wait comes off that side's next turns' model-time caps, at
  most half a cap each (`arena_turn_charged`), so unanswered prompts alone never end a turn; the rest carries over. A
  phase capped only because of the charge (model time under the full cap) is told the time it had, is marked
  `charged_cap` on `arena_turn_forced`, counts toward no forfeit and breaks the run in the runner and the grader, so
  only a side's own full cap can forfeit (owner, 2026-11-08). While the runner's end at a cap runs, a new order for
  that character is refused (`this character's turn is being ended at <the cap>`): live, its own next `move_to`
  replaced the end, and it walked and swung after the cap. The runner stops polling that end once it reaches any final
  status; it waited 22 s on an `interrupted` one, with no cap, hold or stall check. A
  side's waits are budgeted at a sixth of the wall cap: past it (`arena_reaction_budget_spent`, once) its prompts are
  declined at their first read, so unanswered prompts can't use up a match. The side reads an `arena_notice` event for
  each decline and each charge (`reaction: Sentinel (Red Ash) unanswered for 46 s: declined; 46 s comes off your next
  turns`); the other side reads what it read before. `arena_reaction_declined` gains `waited_s` and `over_budget`; the
  grade gains `reactions_unanswered` {A, B: declined, over_budget, waited_s, charged_s, budget_spent}, and `valid`
  doesn't read it. No match on record has ended on the wall cap this way: it is the owner's rule for the public bench,
  bounded by arithmetic (D188, D346, ops wall-cap-design.md). Tool definitions don't change. bench-v0.11's release
  cited it until 0.16.0.
- 0.16.0 (2026-11-08): what the agent sees changed: combat events name what dealt a hit. A `damage` event from a visible
  source says `by <name>`: the object that dealt it when it isn't its owner, the item that left the thrower during that
  Throw or the spell of the cast with the hit's story action, else, for a hit with storyActionID 0, the item of the
  source's own Throw while it is open or under 1 s after it landed. The game's
  `damageCause` names nothing (read live: Attack, InventoryThrow, SurfaceCreate). A Throw's `cast` says
  the item (`Throw of Bomb`). Nothing is named for an unseen source. Owner, 2026-11-08, after a wipe by eight bare Force
  hits (windmill, S001): "fix what the agent is told". Read live: the blast's hits carry storyActionID 0, 10 ms before the Throw's CastedSpell on one load and 224 ms after it on the next.
  Also `set_party sneak_on`: a Hide the game doesn't carry out fails with what was read (the game's refusal, never
  started, began but didn't land, cast but not sneaking) instead of a bare job timeout, which voided a run as a harness
  failure (v011-pilot act1_gold_max, S001). Why the game dropped those two orders is not read.
  Also the session clock: a run resumed during a fight hold keeps its real end (the fight's end plus its wrap-up), not
  the held ceiling, which gave S001 about 10 min of play it wasn't due; and a bench run isn't resumed with less than 8
  min left when its agent ends its turn (the runner's log says so). Owner rulings, 2026-11-08.
  Tool definitions and the bench prompts don't change. bench-v0.11's release cited it until 0.17.0.
- 0.17.0 (2026-11-09): what the agent sees changed: a maximise run's resume text says the run ends at the Game Over
  screen, no longer at a party wipe (bench-v0.11's third digest moved that rule). Tool definitions and the first prompt
  don't change. bench-v0.11's release cited it until 0.17.1.
- 0.17.1 (2026-11-09): the referee ring writes `party_down` when a party member goes down, for the losses statistic
  only. The 15 s poll misses a down the game undoes inside one interval: `deaths.downs` undercounted (35 downs in the
  debriefs against 21 records over six v0.11 pilot sessions). `downed`, `revived` and the snapshots stay the poll's and
  a run's verdict and grade read only those, so a down the game undoes at once can't end a run. No way up is recorded:
  the game reports up before a death and before the Downed status lands (fight_act1_windmill at 678.9). What the agent
  sees doesn't change. bench-v0.11's release cited it until 0.18.0.
- 0.18.0 (2026-11-09): what the agent sees changed: an arena agent restarted mid-fight is told, facts only, where it
  was told only "The fight is on.": after a client fault, that its client stopped and was restarted, the game kept
  running, and an order sent before it stopped may have run (live: one did); and the round and whose turn it is,
  naming only its own side's character, else "the other side's turn". A stalled stream is infrastructure, not play (owner, 2026-11-09; the v0.11 pilot's L12 match was lost
  by forfeit to one stalled DeepSeek response): a phase capped with its side's stream silent through a whole
  `turn_cap_s` of model time, or in progress when its side's client failed, is forced as before but counts toward no
  forfeit and breaks the run (`arena_turn_forced`'s `excused`, `stream_silent` or `client_fault`). A second fault is
  still `client_fault`, invalid. Limits: a stall that starts inside what would be a side's third forced phase in a row
  doesn't fill a whole cap, and still forfeits; a client that writes nothing during a response (`claude -p` writes
  whole messages) reads as silent through a single response longer than the cap, while pi streams deltas. The turn the
  fault interrupted keeps the model time it had used. An arena side's short ids (e12) now survive its restart, kept in
  `<side>.handles.json` (`BG3AI_HANDLES_PATH`, set by the arena only): a new process renumbered them, so an id from the
  conversation could name another character. A playthrough's resume still renumbers; out of scope here.
  bench-v0.11's release cited it until 0.19.0.
- 0.19.0 (2026-11-09): what the agent sees changed: end_turn's wait and wait_for('my_turn') stop when the game is
  waiting on a party member at 0 HP without the Downed status, whose open turn only an end moves on (state.combat
  `zero_hp_turn`), and say so: `now e2's turn`, then `e2 Gale is at 0 HP without the Downed status (no death saving
  throws): the game kept its turn open`, with no action block. The row also reads `zero_hp_turn`. They left that
  member out as downed and ran their caps (windmill 01:39:19: 81 s, then 15 s, one of 223 end_turn calls in the v0.11
  pilot's 19 sessions). A turn under the Downed status still passes by itself, enemy turns and the other side's are
  waited out as before, and a match is unchanged: its phase tick ends that turn itself. A Lockpick row pressed with no
  roll that the game answered with a failed Use of the door by the actor, who stayed put, reads `the game ran a plain
  Use of it, which failed; no lockpicking started; the actor did not move` (read live: 16 of 16 no-rolls), no longer
  `nothing happened`; when the game walked the actor, the last clause gives the distance (`the actor moved 1.4 m`), and
  with no failed Use read it is `no roll came; the actor moved 1.4 m`. The game's own notices during the wait now follow
  as its words (`the game said "Can't reach this", "Can't reach"`, on all 32 no-roll tries read and none of the 26 that
  rolled); why it can't reach depends on where another party member stands, and that is still not known. A walked
  actor's `after=` gives where it ended. A `react` answered while the next reaction prompt comes up no longer reports `TIMEOUT` after 10 s;
  it comes back done, with that prompt. The answer had landed: the game's popup is down 30-82 ms between two prompts,
  under the 80 ms between the job's checks, so the next prompt read as the same one at the same event count (the v0.11
  pilot's L12 match; 3 of 5 declines with a next prompt in a live read). The job now holds the event it answered and
  compares it by identity (`==`, stable over 2666 frames in that read and false on all 3 next prompts; a prompt's text
  can't tell two saves that rolled the same). A chosen row's decline reads the same way, so another prompt at the same
  count is no longer declined by mistake. Not read in a live match on this code. An arena side's tool timeouts and
  tool exceptions are now counted after the match, over all its processes (`arena_tool_faults {side, n, faults}`): one
  per side is tolerated and recorded, and a side's second makes the match invalid, rerun and never rated (`arena_end`'s
  `invalid: "tool_faults"`; its reason and winner stay what happened). Our tools failing is infrastructure, not the
  side's play, and a real timeout can decide a match. The bench still voids a run at the first. Tool definitions don't
  change. bench-v0.11's release cites it.
- The runner's relaunch point between sessions follows the game's memory cap (the scope's, else
  `BG3AI_GAME_MEMORY_MAX`): 1200 MiB under ramwatch's anon+swap kill to the nearest 100, at most
  `BG3AI_RELAUNCH_RAM_MB`. 6500 at 8G (the flat 7000 left about 45 minutes of growth under the 7692 kill), 7000 at
  10G as before. The relaunch reason names the kill the game really runs under, not always 9000.
- The game's swap cap defaults to 4G under a `BG3AI_GAME_MEMORY_MAX` below 10G (`ramwatch.default_swap_max`): the v0.10
  pilot's act3_gold_max runs went past 2 GB of swap at 8G. 10G keeps 2G, and `BG3AI_GAME_SWAP_MAX` always wins.
- An arena match isolates the mod's nav files and scenario.json as a bench run does: moved into `<run_dir>/reset/` before the
  game starts and back on every way out, the match's own kept in `reset/run/`. It used to overwrite the avatar's nav mirror.

- Casts the harness starts now roll their dice. A cast an Osiris `UseSpell` or
  `UseSpellAtPosition` starts carries IgnoreSpellRolls (live 2026-11-03: its options also name IgnoreHasSpell,
  IgnoreCastChecks, IgnoreTargetChecks and AvoidDangerousAuras), so the game skipped its spell roll. Every attack the
  harness started hit without a d20 (30 of 30 arena attacks; 8 of 8 at AC 35 in the Balthazar save), and Sacred Flame
  never rolled a save (8 of 8 did full damage). The game AI's casts and the hotbar click route rolled (the hotbar
  missed 9 of 9 at AC 35). After each order (`use_ability` casts, the cast after a walk into range, sneak's Hide), the
  mod drops IgnoreSpellRolls from the cast's `SpellCastState.CastOptions` when the cast entity is made and reads it
  back (live, by hand: 6 of 6 attacks missed at AC 35, and 2 of 6 Sacred Flame saves were made). When the write
  doesn't take, or the cast ends with no cast state seen, the cast fails and says it went without its roll. Live
  check through `use_ability` (tools/dev/check_cast_rolls.py, 2026-11-03, PASS): 3 hits in 21 weapon attacks and 0
  of 6 Eldritch Blast beams at AC 35, 1 Sacred Flame save made in 12, and every cast drew its spell die. The harness
  has cast through Osiris `UseSpell` since the mod's first commit (043d06a4, 2026-10-09), so attacks and saving
  throws cast by the harness never rolled in any run before this: party attacks could not miss and party save
  spells always landed (launch-v1: 368 of 368 harness weapon attacks dealt damage). bench-v0.10 results are not
  comparable with bench-v0.8 or v0.9 results for any scenario with a fight.
- Casts the harness starts are now checked by the game's own rules, as a player's are. The same cast-state write
  also drops IgnoreCastChecks and IgnoreTargetChecks; IgnoreHasSpell stays, because dropping it refused every cast.
  Before this, a harness cast skipped the spell's Requirements, RequirementConditions and TargetConditions.
  Window 7 showed Rage cast out of combat and Jump cast while Ensnared; with IgnoreCastChecks dropped, the game
  refused Rage out of combat by itself. The guard still checks the caster's readable rules before the order
  (Server/GuardRules.lua: the Requirements Combat/!Combat/!Immobile and the RequirementConditions terms, which the
  game skips: window 8, Hide while Flanked), so the agent gets a plain refusal and loses no action. TargetConditions
  are left to the game: with the guard off it refused all four targets they rule out (window 9: Sleep at 48 HP,
  Healing Word on an undead, a swing at a corpse, Help on a healthy ally; InvalidTarget, nothing spent), so the
  guard no longer refuses on them, and a term it misreads can't refuse a legal cast. If IgnoreTargetChecks stays set
  on a cast (the engine then won't check its target), the guard reads the target conditions at the cast state and,
  when one reads false, stops the cast (fail closed) with "<spell> can't target that: <fact> (the game wouldn't check
  the target ...): stopped"; a cast that goes through anyway says so in `target_unchecked`. When the game refuses a cast, the result now gives the game's own reason
  (`SpellCastOutcome.Result`, e.g. "it can't be cast now (its requirements aren't met) (the game's CantCast)")
  instead of "no reason given", when the cast entity has one. At a ground point, the game's target reasons
  (MissingTarget, TooFewTargets, InvalidTarget) read "it can't be cast at that spot", because they can depend on
  hidden creatures there. An InvalidTarget at a creature adds the guard's reading of the spell's TargetConditions:
  the terms not read as met, in plain words, each marked "(not read)" or with the fact that fails it (window 8:
  Sneak Attack "needs ... the caster has advantage on a melee weapon attack (not read) or there is an ally of the
  caster ... within 1.5 m of the target"). The old guesses after a refusal ("probably just outside the engine's
  reach: step closer", "usually the shot's path is blocked ... try another target") are gone; they sent a Sneak
  Attack at 1.0 m into the same refusal twice. The other refusals and failures in the mod's actions and guard state
  facts only: the advice after them ("move closer first", "travel_to that spot first", "climb it yourself first",
  "pick a landing ...", "Read a Scroll of Revivify ...", "Never jump there again ...") is gone. A tool option is
  still named as what it does ("force=true casts anyway", "trespass=true walks in anyway", "its variants (which=)"),
  and facts about the game and the tools stay ("a Scroll of Revivify through use_ability gives both picks ...;
  Withers revives at camp", "it takes target=<its id>", "loot_corpse does", "interact on <ladder> climbs it").
  A status in a spell's conditions reads with the game's name: "the caster has the Flanked status" (it said "the
  caster has flanked"). A check flag that stays set is recorded: the done result carries `unchecked`, and the
  referee gets a `cast_unchecked` row with the flags, any rule terms the guard couldn't read for that cast, and,
  when IgnoreTargetChecks stayed set, the guard's reading of the target conditions at that target.
- Melee reach is now measured with the creatures' Move bounds (`AIBounds.Move`, 0.25 m for a medium creature), not
  their Stand footprint (0.5 m). One measure (`G.melee_reach`) serves the guard's range check, move_to's stop and its
  already-there test, and the walk into range before a cast. Window 8 (the engine checking): a 1.5 m greataxe swing
  was refused from 2.1 m centre to centre (10 of 10) and cast from 1.4 m. The guard had allowed 2.5 m, and
  `move_to target` said already_there at 2.1 m, so the agent could not close in. A plain `move_to target` now stops
  inside the mover's own melee reach. Window 9 measured the edge: the engine cast from 2.0 m and refused 2.05 m,
  which fits the Move radii (1.5 + 0.249 + 0.249 = 1.998 m); the Hit radii (1.9 m) were 0.1 m too strict. A guard refusal on a Shout's caster names "the caster" (it said "the target
  has flanked" for Hide).
- The guard's rule check reads the condition terms the launch-v1 casts use that it left unread before.
  - From the game's own helper scripts (Shared `CommonConditions.khn`): HasHelpableCondition (Help),
    IsCrowdControlled, MobileShootingCasterCondition, CanUseWeaponActions, HasHPMoreThan (Sleep).
  - Read directly: Player, Locked (Knock), HasPassive (Sculpt Spells).
  - IsProficientWithEquippedWeapon: the character's Proficiency flags against the equipped weapons'
    ProficiencyGroup flags.
  - IsImmuneToStatus: refused only when the immunity is listed; otherwise it stays unknown.
  - A condition ending in ';' (Grease) now reads.
  - A status-group term (`HasStatus('SG_...')`) never read as off before (a Lua `x and false or nil` slip), so it
    was never refused; it now reads.
  - Replaying the 199 launch-v1 casts gives the same 3 refusals as before (Prayer of Healing in combat) and no new
    one.
- A move in combat (`move_to`, `walk_toward`, `jump_to`) answers only after the casts its walk set off from the
  mover's foes have resolved, at most 8 s after the move ended, and its result names them under REACTIONS (who,
  what, the damage to the mover). Arena window 8, seed 7 m1: `move_to` answered "done, hp 20/20" 0.6 s before the
  opportunity attack it provoked landed (critical, 13), and the agent read it a call later as an ordinary enemy
  cast. Window 9 checked it live: the opportunity attack's cast starts only after the walk arrives (+0.33 s,
  +0.93 s), while the reactor's ReactionInterruptUsed fires before (-0.63 s, -1.30 s); so a foe's interrupt during
  the move holds it until that foe's cast starts (at most 4 s after the interrupt), then the cast holds it.
- A character knocked down (StatusType KNOCKED_DOWN or SG_Prone: PRONE, PRONE_GREASE, ...) during its own combat turn
  can't walk or cast through the harness, as a player can't: the game keeps it down until the status goes at its next
  turn start (Shared Status_KNOCKED_DOWN.txt PRONE: RemoveEvents OnTurn, RemoveConditions half its movement). w10 L6
  ST-P5(d): a harness move_to stood a prone character up at no cost beyond the metres walked. The guard refuses both
  before any order ("cannot move while Prone (the status keeps it in place this turn)", "cannot act while Prone (it
  did not stand up this turn: standing needs half its movement)"), and the movement figure shows 0. A knocked-down
  status without that condition gives its own effect, read from its RemoveEvents/RemoveConditions. Outside its own
  turn nothing changes.
  In a match, where every member's turn starts in the round's burst before any phase, the mod stands a prone member up
  at its own phase start for half its movement when it has that much (REVIEW-12 (a)); it takes that half itself unless
  the status's removal already did. The arena rules state the house rule for durations (an N-turn status lasts the rest
  of its round plus N-1 rounds) and the prone rule. Not run live: whether the game's removal of the status spends the
  half itself is w10 ST-P5(f) (`Ar.PRONE_DEDUCT` switches the mod's half off).
- `runner/launch_game.sh` checks the game build (`tools/ci/game_build.py` against `WANT_BUILD`): another build is
  refused, exit 6, also with `--dry-run`, unless `BG3AI_ALLOW_OTHER_BUILD=1`; when it can't tell, or an update is
  pending, it warns. `--no-bg3le` skips it. When the game exits the launcher prints the game log and the bg3le log
  paths and, after a nonzero exit or a logged crash, up to 8 `crash:`/`WARNING: resolve` lines per bg3le log; the
  exit status stays the game's. Issue #101: a crash at a save load's end with no build, bg3le info or logs to go
  on. The bug report template asks for `tools/bg3le_buildinfo.py`, `launch_game.sh --dry-run` and those logs.
- The runner reads the launcher's exit 6 as a refusal, not a game death: `GameBuildRefused` (a `LaunchBlocked`, so the
  run loop retries like a Steam or display refusal and the bench records no attempt), the log line, event and alert
  say "launcher refused: game build X, pinned Y" from the launcher's refusal line. Before, it counted as a launch
  that died and was retried until the crash-loop stop.
- bg3le patch 0013: bg3se's globals (`gStaticSymbols`, `gExtender`) are created at the end of bg3le's own setup
  (the first Osiris callback, once the engine heap is installed), not only by the ImGui overlay from bg3le's
  `vkCreateInstance` export. Issue #101: where the overlay never built (`BG3LE_IMGUI=0`, or Vulkan calls that
  don't reach bg3le's exports), the first entity read of a still-pending component crashed at the first server
  tick after Running (SIGSEGV at 0xa0 in `ThreadRegistry::RequestThreadIndex`); reproduced with `BG3LE_IMGUI=0`.
  bg3le with 0001-0013: sha256 e84d6044c057f3d8e3c4d8ce2259bac944fe873a18f6d5603d891a98e0be0f95, pinned by
  `releases/bench-v0.11.json` (was b7ecfd06, untagged and ungraded); `releases/bench-v0.10.json` is unchanged.
- bg3le patches 0014 and 0015, for a game crash in `Noesis::bg3le_ui::l_get`: a call through a held object that was
  already on the allocator's free list. 0014: `push_object` no longer raises the count of an object whose count is 0
  (its `OnDestroy` is running): it returns nil. `object_arg` holds a reference for the whole C call, and refuses a dying
  object with the existing "Noesis object is gone". A reference that would be the last goes to the UI thread's queue.
  0015: the crash log names the running Ext.UI call: its function, string argument, object, class, and the object's
  first word and count. Tested offline by `tools/test_bg3le_ui_refs.py`, which also requires the crash on the series
  without the fix. What freed the object in that crash is not known. bg3le with 0001-0015: sha256
  498efbceca3144922a77809be8a31412cf947e18f569828a31ffdbce8a0a6085, pinned by `releases/bench-v0.11.json` (was
  e84d6044); the v0.8-v0.10 releases are unchanged.
- bg3le patch 0016, for a wrong stats pool: on 14 of 685 kept launches the string pool scan took another array instead
  of the real pool at +32 from the rarity run (the 1512-entry array at -384 on 13, the 15754-entry array at -576 on 1),
  so GUID, Float and TranslatedString attributes had no pool, "attributes available" was logged anyway, and FixedString
  writes went into the engine's array. A candidate now needs the guid, float and translated pools after it (all 1333
  good scans in the logs have them, all 29 bad scans don't), and the log names the dice string that matched. With no candidate, attributes stay unavailable. Why the dice
  test passes on that array is not known. Tested offline by `tools/test_bg3le_stats_pool.py`, which also requires the
  wrong pick on the series without the fix. The runner reads the same from the launch's bg3le log
  (`LaunchWatch.stats_problem`): a game at the menu or at Running with a pool missing, or with no scan logged, is killed
  and refused with `StatsPoolsMissing`, with no relaunch. A crash found in that read counts as a game death. bg3le with 0001-0016: sha256
  d21546c06f5f307e6c8bd1f68ca3bf70bf95fb23ca54b809405ce4e8d1c16de9, pinned by `releases/bench-v0.11.json` (was
  498efbce), with its code_sha1; the v0.8-v0.10 releases are unchanged and keep the bug.
- Operator messages (the playthrough app's operator chat): with `BG3AI_OPERATOR_DIR` set, the bg3 server adds each
  pending message from `operator.jsonl` there to the next tool result the client gets, once, as `OPERATOR MESSAGE
  (from the person running this playthrough, sent HH:MM:SS): <text>`, and appends `{id, t, session, tool}` to
  `operator.delivered.jsonl`; a metrics record `{"type": "operator", "t", "tool", "ids"}` names the ids. Only the
  MCP layer takes them: a tool another tool calls and a batch step never do, so a batch carries them in its own end. Without the variable nothing is opened or added. No tool, description or rules text changes,
  so the served tool definitions and their digests stay the same. A bench run never has it: `LiveRunHost` drops
  every `BG3AI_OPERATOR_*` from the env a run gets, whatever the shell that started the bench exports.
  The harness version stays: the block appears only with the playthrough's option on. A session's `latency.json`
  has `operator_messages`: how many message ids its `operator` metrics records carried (0 without the option).
- The play `claude` gets `play/.claude/settings.json` through `--settings` (the container sandbox already did).
  Issue #101: Claude Code ignores a project's `permissions.allow` while its folder isn't trusted ("Ignoring 1
  permissions.allow entry from .claude/settings.json: this workspace has not been trusted"), so in a clone nobody
  had opened interactively every `mcp__bg3__*` call was refused and the session ended at once. The allow, the deny
  list and `--tools ToolSearch` are the same as before; the bg3 tools served don't change.
- 4v4 arenas with built teams, at levels 2, 5 and 12. A build file (`packages/bench/builds/`) generates its arena; both
  sides of a pair are copies given the same stats and kit. A rated arena compares
  each pair with a reference: a real hireling respecced and levelled through the game's own pages to that build
  (`tools/dev/arena_reference.py`, `probe_route_h.py`) and read live. `arena.setup` fails on any difference that isn't
  listed, row by row, in the build's `reference.accepted`, and on a host outside the tier's XP window
  (`mirror.party_xp`). Only a draft arena may lack references. Rated: `builds-l2-4v4-v1` (Fighter, Rogue, Barbarian,
  Ranger) and `builds-l5-4v4-v1` (Fighter, Bard, Wizard, Rogue). Drafts with no references that can't be played:
  `builds-l12-4v4-v1` and `builds-l5-4v4-team2-v1`. Every builds arena has a 90 s turn cap and can set its members'
  gap along the line (`location.line_gap`, default 1.5 m; these four use 1.25 m), with the fairness limits unchanged.
- The kit gives a copy its spell abilities, and the upcast rows a character's book gets from the game, up to its highest
  slot (`UnlockSpell(X,AddChildren,..)`). It waits out the copy's rebuild, so no passive lands twice, and fails when a
  part didn't land. `tools/dev/arena_scan.py` walks what every build can reach (items, class rows, kit, race, passives)
  over a checked-in stat graph, `tools/dev/arena_stat_graph.json`, of stats entry ids, their references and what each
  does against the arena's bar. `test_arena_scan.py` fails on a knock-down or side-changing spell. Chromatic Orb and
  the Quarterstaff (it unlocks Topple) left the Wizard builds, and a Wizard twin holds only its prepared spells, so
  what a reference learned beyond them (5 spells at level 5, with their upcasts) is accepted, row by row.
- Respec and level-up, which the references drive through the mod's character pages: a respec now applies its point
  buy, skills, expertise, passives, spell picks and equipment selector, and reads every pick back. Normal play's
  level-up changes too: `ui.level_up` waits up to 15 s for the intro (was 5 s; live, a Wizard's 5 s wasn't enough and
  the page stayed without panels), and it returns to the overview after the subclass and deity tabs, as it did after
  the feat tab.
- Reaction prompts in a match. A prompt belongs to the side of the character it asks: the HUD stays on a living team
  member so the prompt shows, and only that side sees it and answers it, by a row click where the game needs one. The
  rules text says so, in place of "a match never makes" that click. While the game is paused for a reaction, a side's
  orders and ends are refused and nothing is purged, and an order the pause caught reads INTERRUPTED. The runner reads
  for a hold before a turn cap, doesn't count time held, and declines a prompt left open past the cap
  (`arena_reaction_declined`). A stall while the game is held, or a hold or decline nobody can clear, ends the match as a
  fault, never as the acting side's forfeit. Known and open: an Extra Attack order sent after a decline, before the held
  attack lands, can answer `done` with `unrolled=` though its own swing then goes, and a refused or cancelled order's die
  is recorded on that caster's next attack. No grader reads either.
- Also in normal play: a cast that lands after a pause pays for itself, as the resource audit now waits for the cast's
  CastedSpell (a released Healing Word healed with its slot unspent). `react` with use=true says so when its prompt
  closed and its observers' resources hadn't dropped 4 s later. A swing the game took the cost of and then cancelled no
  longer says the game refused it.
- S6: `packages/bench/matrices/arena-s6-{l2,l5,l12}.toml`, one DeepSeek self-match per tier under one $1 budget file,
  graded and never rated, with no release. The L12 matrix plans no match while its arena is a draft. A matrix queue the
  operator closed (an `operator_stop` line) is refused before anything is charged or played.
- The served tool definitions don't change in this round, but match rules and `react`'s results do, so the harness goes
  to 0.5.0. `releases/bench-v0.11.json` moves that version, `mod_sha1` and `code_sha1`; its game and bg3le pins don't
  change.
- Also in normal play: `level_up` closes the page the game opens after a finish even when that page's intro has already
  ended. It used to keep such a page for the next call, and that page could lack the next level's selectors for good: a
  Battle Master's level 7 stopped with its manoeuvres "not offered", and reopened by hand the same page offered them. A
  page that ignores the close is still reported as `page_still_open`, and a page the game opens later than the first look
  is still carried, so a kept page remains possible. Each such level now takes a reopen: live, a 5.5-7.2 s intro on top of
  the close. The `closed` stage allows 12 s, so a long Finishing and a close don't read as a failure after the level applied.
- `level_up` reads the plan's skills, expertise and passives back, and won't finish without them even when the game calls
  the page complete. A level's expertise group can open full of the game's own picks, and the plan's toggles don't all land: a
  Rogue's level 6 opened on Deception and Sleight of Hand, and toggling Acrobatics then Athletics left Sleight of Hand and
  Athletics, with `done` and `complete`, where the plan said Acrobatics. How the game moved them isn't read. Now, only for a
  pick that didn't land, the group's unnamed picks come off as far as the picks lacking exceed its free room, never one the
  game shows as locked. `set.<group>_off` names the rows still listed that were selected before the toggles and aren't after
  them, including one the game took, and it's still there on the bridge's rerun after spell clicks. `plan.skills` now reaches the expertise group: a row the game shows disabled and
  unselected no longer stops the search for a name, so a name no group offers is reported as `not offered`. That search is
  shared with character creation's `plan.skills`. On a level-up, a `plan.skills` name a group holds as an earlier pick moves
  on to a later group that can take it, so naming a proficient skill at a Bard's level 3 takes an expertise pick; if none
  can, nothing changes and the result's `already_held` names it. Character creation still stops at the held row. A level-up refused as not complete now names a skill group short of its max (`3 class skills (plan.skills)`), where it said the page showed nothing open. A plan's skill or passive the page doesn't offer is now a failure, no longer `done`.
- A kit's subclass spell with no ability on its row or class reads the stats' ability, as a character's does (a Thief's
  Shout_SupremeSneak read None on a twin, Intelligence on the reference).
- `builds-l12-4v4-v1` and its build file aren't drafts any more, so its S6 matrix plans its one match.
- A kit gives a race's rows above level 1 up to its level, as a real character of that race has them: at level 5 a Drow
  twin holds Faerie Fire and Darkness beside Dancing Lights. Only `builds-l5-4v4-team2-v1`'s Ranger moves, and it's still
  a draft. Read live (2026-11-08): a twin casts each once, and a second try on a later turn is refused as on cooldown
  until a long rest, as a character's.
- `builds-l5-4v4-team2-v1`'s Barbarian (Wildheart) and Bard (Valor) become a Fighter (Champion) and a Bard (Lore): a
  twin can't lose a passive its class rows gave (`RemovePassive` does nothing to one, live 2026-11-07), and every
  Barbarian subclass and Valor remove one at level 3. Swords and Glamour fail the knock-down scan. Still a draft.
- `make_arena_save` refuses a build whose subclass rows remove a passive, naming the row: no call takes one off a
  twin.
- `builds-l5-4v4-team2-v1` has its four references, read live from respecced hirelings, and accepts by exact value only
  the rows L5's first team accepts (each build's weapon action DC; the Wizard's unprepared spells, book rows and Arcane
  Recovery upcasts). Its S4 and a scripted S5 passed (2026-11-08). It stays a draft until the owner rules on its
  classes and on rating it; its draft reason now says so.
- The arena scan (`tools/dev/arena_scan.py`, its graph and `test_arena_scan`) refuses a build that can apply a status
  whose flags (inheritance resolved) hold `LoseControl`, naming the way from the build's pick to the status (owner,
  2026-11-08: the game AI doesn't play in the arena). 45 statuses carry it. Of the played builds only the L5 and L12
  Battle Master reached one, Goaded by Goading Attack, whose holders lost their turns to the game's AI. The flag alone
  doesn't prove that: one Compelled Duel turn under it stayed the player's, and all 3 Goaded turns on record were twins.
- `builds-l5-4v4-v1` and `builds-l12-4v4-v1`'s Battle Master takes Sweeping Attack in place of Goading Attack: its own
  attack with an area damage roll, no status, no ally's action and no dropped weapon (Commander's Strike and
  Manoeuvring Attack hand an ally an action, Disarming Attack leaves a weapon on the ground; Trip, Pushing and
  Menacing were already out). The Fighter's references were read again and both arenas' S4 passed (2026-11-08), so
  neither file is a draft. Not read live: a cast of Sweeping Attack, and a match on either arena since the change.

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
  Its own release file (`releases/bench-v0.10.json`: mod tree 6ec901b5, the same 81 bg3 tools, new prompt hashes for
  pi, claude and pi without screenshots) and start save keys (`saves/bench-v0.10/`, the same save files). Its
  conditions also pin the served tool definitions, `tools.defs_sha256` (release v1, optional; release.defs_digest:
  each served tool's tools/list entry, whole, canonical JSON), and C02 rejects a bundle whose `bg3-tools.json` hashes
  otherwise; bench-v0.8 and v0.9's releases have no such field and keep checking names only. What changed for the
  agent (bridge/CHANGELOG.md, 2026-11-02, and docs/FAIRNESS.md):
  - Walks out of combat return at their end: `move_to`, `walk_toward`, `travel_to` (and a far `move_to`) default
    to `wait=true`, `route(go=true)` always blocks (it has no `wait`), and they return what `wait_for` on the walk
    returned, with its early stops, CHANGES and the minimap block; a walk still going after 90 s returns its
    progress and the id `wait_for` keeps waiting on. `wait=false` keeps the old pipelining.
  - travel_to and regroup name what the trip passed: a PASSED line before CHANGES (what came into view at a leg's
    end and is out of view at the trip's end, not in the agent's last view; nearest first, up to 6).
  - The leader's `travel_to` doesn't re-chain or wait for a member more than 24 m away at the trip's start (said
    once: `not with you: e1 Shadowheart 77m (unchained, stays there)`; one already chained stays chained, so the
    game's follow can still bring it), checks for stragglers only after legs that don't arrive, gives a
    straggler's walk back 30-60 s for the gap (was a flat 30 s), and a stop on a straggler says how its walk back
    ended (was a guessed "stuck on a ladder, gap or door?").
  - Out of combat a batch ends with its first walk step: `do_actions` runs (`queue_actions` queues) the steps up to
    and with the first `move_to`/`walk_toward`/`travel_to`/`jump_to` and lists the rest as not run (not queued);
    batches without a walk, or with the walk last, are unchanged. A walk step with `if`/`unless` ends the batch only
    when it runs (the steps after it then say "not run").
  - Every walk end out of combat and every `look` carry the minimap block (OBJECTIVES with distance, direction and
    height, WAYS, the 3 nearest unvisited LEADS), also after a walk that stopped short.
  - `roll_check` returns a failed roll with the roll screen open; a reroll is its own call, `roll_check(reroll=true)`.
    `reroll_on_fail` is removed (it spent Inspiration before the failure was seen). A reroll on a success is refused
    and presses nothing.
  - `trade(sell="junk")` names every item it sold (SOLD, by name with count and gold) and counts what the junk rule
    kept back (KEPT).
  - Known ground (FAIRNESS.md) from the camera: ground within 16 m of the leader (what the default camera shows at
    16:9, measured live; owner D448; earlier bench-v0.10 builds used 40 m) or within a member's own Sight (~18.5 m)
    with a clear eye ray, and ground up to 15 m below an
    edge at up to 41 degrees, the camera's measured pitch (was 4 m, 45 degrees). route, travel_to,
    walk_toward, path_preview, WAYS, jump solvers, move_to/jump_to's "not in view" and hazard names follow it.
    Characters and items keep the Sight stat.
  - At night in camp, `long_rest`, camp leave and `fast_travel` from camp refuse before pressing anything while the
    camp story still wants a talk, and name who (the "!" over them; someone out of sight as "someone not in view"),
    instead of failing 30 s later. At night camp, get_status and look show the camp "!" over the characters in view
    in their TALK line. `long_rest` (and the sleep press of camp leave and fast_travel) refuses at once while the
    game menu is open.
  - `use_ability` on a spell container lists the variants that read as a spell's stats entry and says how many
    entries didn't (after one more read); with none that reads it refuses without names. The raw text is never shown.
  - `talk_to` / `interact` on a target past reach or out of sight walk there and then talk or use it in the same
    call; a walk there that stops short runs no step and says so (`not run (the walk there stopped before
    arriving)`); one still walking at the ~90 s budget queues it for the arrival. `do_actions` ending at its walk step
    carries the minimap block too, and a blocking walk that its walker's own conversation ended says it ended. A
    travel_to inside another call (route go=true, a far move_to, a walk there) counts its budget from that call's
    start.
  - Combat output lists the harmful clouds and surfaces a party member sees within 12 m (`HAZARD` lines, as `look`):
    `get_combat_state` every call; the turn block and a combat action's result when the patches in view changed.
    Nearby rows, party rows and CHANGES say who starts or stops concentrating (`concentrating on Cloudkill`).
  - An answer the game ignores for 3 s is pressed once with its bound number key, and the result says so (`note=`);
    the settle after an answer compares the whole node, so a list scrolled during the wait isn't taken as answered.
    Answer rows pair with the game's answers by the answer index each row carries (its AnswerIdx; window 5: the
    number a row shows read only '.'), so a list with a wrapped row is answered with its tags (window 4: Lae'zel's
    cage counted 7 rows of 6 and the press was ignored); with no readable bound key, the ignored answer gets the
    number key it shows.
  - `flee` fails at once with the reason the greyed-out Flee button's tooltip shows (`FAILED: the game won't flee
    now (Flee is greyed out: Enemy too close! (10.6/27 m))`), with no command or cursor search, and runs each Flee
    command name once (window 4: 48 s before failing). When the tooltip's own fields are empty, the reason comes from
    the game's flee state (FleeCapability: the flags in words, nearest enemy / distance needed: `Enemy too close!
    (2.3/27 m)`; window 5 said `the game gives no reason`).
  - Attacks and saving throws cast by the harness roll their dice (above, under Harness): before, they never
    rolled, so bench-v0.10 results are not comparable with bench-v0.8 or v0.9 results for scenarios with fights.
    rolls.jsonl gains a row per attack-roll cast with its `cast_roll` (d20, total, DC; a saving throw's goes to the
    saver's row), kept out of `natural`/`dc` so the luck covariate is unchanged; a death or concentration save has no
    skill. A cast that went through without its roll is done with `unrolled`, and the referee records `cast_unrolled`.
  - Harness casts are checked by the game's own rules, and melee reach is measured with the creatures' Hit bounds
    (both above, under Harness): a cast the game refuses says the game's reason, a check flag that stays set gives
    `unchecked` and a `cast_unchecked` referee record. facts v1 has the `cast_unrolled` and `cast_unchecked` events
    (compatible extension), so F03 accepts bundles that have them. A cast whose cast state was made counts as
    started; one the game holds paused for a reaction is reported interrupted (paused), not purged as never started.
  - `react(use=true)` on a prompt with one interruptor and one row (Reckless Attack after a natural 1) clicks the row,
    as a player does (window 6: Accept closed it without the reaction; window 7: the row's command left it up).
  - `ui_action` refuses commands that would change the difficulty, its settings (Karmic Dice ...), the ruleset or the
    game options; every attempt is a `rule_change` referee record (facts v1 compatible extension; REVIEW-5 F8).
  - The game's mouse is the harness's from a run's start (`gio.take_input`): the operator's pointer no longer reaches
    the game or edge-scrolls the camera (window 7).
  - A fall down a drop link of the walk's own route is no longer failed as an engine teleport, and `end_turn`'s End
    Turn press refused by the busy UI is asked again after the cooldown instead of failing at once. Any fall of a walk
    (at least 1.5 m down, no longer across than down, up to 8 m, not onto the walk's end) goes on as the walk
    (`walk_fell` event); the engine's no-path snap across to the walk's end is still blocked. A busy answer that came
    after the End Turn press ran checks whether the turn moved on, as after any press, before failing.
  - An enemy's spell on first sight in a fight (cast in view, or a row showing it concentrating on it) gives a
    `spell_info` line with what its tooltip shows: the game's description with its numbers filled in, then level,
    concentration, range, area, save or attack, damage and duration from its stats (act2-fights-analysis fix 4).
    Its damage uses the caster's numbers where the tooltip's number can be rebuilt: `LevelMapValue(...)` at the
    caster's level. A weapon formula is not resolved (the off-hand rule, versatile weapons and the caster's own damage
    boosts change it; REVIEW-13), so it is left out like any part that stays a formula (one log line). No
    spell_info for an action every party member has in its own spellbook and that isn't a spell (Main Hand Attack,
    Ranged Attack, Jump, Dash), and a Shout no longer fails it (w10 L2).
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
  Evidence video and panel: a blocking walk's timeline entry shows the walk's outcome, not its start line; results the
  bridge reports as failed (FAILED/REFUSED lines, bg3harness triage.failed_lines) are marked as fails; the local
  panel's results list shows a run's recovered launches.
  What a valid bundle is (REVIEW-4, 2026-11-03; bench-v0.10 and later: the job validates a suite at its released
  commit, so bench-v0.8 and v0.9 submissions keep their rules):
  - P02 also requires the code the release pins: release pins `code_sha1` (optional in release v1;
    bg3harness.pins.code_digest, the git blobs at HEAD of bridge/src, runner, packages/harness/src, play, tools/pi
    and uv.lock, tests left out) and the run's `pins.repo.code_sha1` must equal it. packages/harness/src and uv.lock
    are dirty-checked now too. The release is rebuilt whenever that code changes (the release test says so).
  - New check C04 (reject, panel and job): every process's session_start and init name the manifest's model slug,
    and the effort the usage/result events say was served is the declared `reasoning_effort`. launch-v1's 36
    DeepSeek runs declared `medium` and pi served `high` (the DeepSeek profiles' thinking_level_map has no medium
    and pi clamps up), so the three DeepSeek pi profiles (deepseek-flash, deepseek-v4-pro, deepseek-v4.1-flash-pi)
    now declare `effort = "high"`: their bench-v0.10 rows read high, and launch-v1's DeepSeek row (declared medium)
    is not 1:1 with them by label, only by what was served.
  - X03 rejects a save start whose first load mark is not the release's start save folder.
  - F03 also rejects a bundle.json whose `attempt.cell` is not `<scenario>/<model slug>/<condition>` of its own
    manifest (X07 picks its reference runs by the cell). All 146 launch-v1 bundles pass.
  - results: a leaderboard row is (client, model, reasoning effort, condition); runs, groups and the ranking's
    models carry `effort` (results v1, optional). The suite digest is re-pinned under bench-v0.10 (no score moves
    for runs of one effort per model).
  bench-v0.11 (bg3bench 0.11.0, 2026-11-05): `releases/bench-v0.10.json` is frozen as merged and graded (the
  v0.10 pilot; a test pins its sha256). A maximise run's kickoff and the gold_max scenarios no longer say the run
  always plays all its minutes, so a maximise score can move: bench-v0.11 pins its own suite digest. Its own release
  file (`releases/bench-v0.11.json`) pins the night follow-up's mod tree, tool definitions and served prompts, and
  its start save keys are `saves/bench-v0.11/` (the same save files as bench-v0.10). What changed for the agent is
  in bridge/CHANGELOG.md (2026-11-04 night and 2026-11-05 entries). The five Tactician twins and
  `fight_act3_sarevok` got their start saves before the suite was tagged: new scenarios to run, no existing score
  moves. `fight_act1_gith_patrol_tactician` is a holdout, so its save stays out of saves.json and the release.
  A maximise run its end rule ended before the time cap is recorded as `ended_by_agent` (run record, facts' run_end,
  result.v1 still, the render's ENDED BY AGENT ending), not `timeout`; its score is computed as before, and older
  runs keep `timeout`. results.py lists the new outcome. Both changes share bench-v0.11's second digest (no score
  moves; the version stays 0.11.0, untagged and ungraded, as bench-v0.10's second digest did).
  A maximise run now ends and scores only at the game's Game Over screen (graders.measure, and the live verdict of a
  run with no goal). It used to stop when every real member read as dead or downed, which the facts see only if a read
  lands in that moment, while the prompt says "A party wipe (the Game Over screen) ends it sooner". In the v0.11
  pilot's two gold runs a defeat sent the party to prison with no Game Over, and one missing `downed` record decided
  the score. Goal runs keep the wipe rule. This can move a maximise score, so by the rule above it would be a new
  minor; it is bench-v0.11's third digest instead because v0.11 is untagged and has no published results. bench-v0.8's
  published scores and results-144.json don't change.
  A run its agent left with time on the clock and no cap hit is recorded `agent_exited` (run record, facts' run_end,
  result.v1 still, the render's ENDED BY AGENT ending), not `timeout`; older runs keep `timeout`. The v0.11 pilot's two
  gold runs read `timeout` at 39.1 and 42.5 of 45 minutes. results.py lists the new outcome: bench-v0.11's fourth
  digest, no score moves.
  `deaths.downs` (facts.party_losses) also counts the referee ring's `party_down`, one per member until a read shows
  them up, with the poll's read of that same down not counted again. `wipes`, `State`, every goal and the score stay
  on the poll: bench-v0.11's fifth digest, no score moves. Known gap, ruled 2026-11-09: a capture the poll never saw
  as a whole-party down still reads as a won combat in a `combats_won` goal, as before (none on record). Reading the
  ring there would fail at grade a run the live verdict had ended as passed, and a last member's down and straight up
  just before a win would grade the win as a defeat.
  The arena grader's forfeit streak skips a forced turn carrying `excused`, as it does `charged_cap`: the runner and
  the grader read the same run. `arena.py` isn't a suite file, so no digest; no match on record carries it.
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

- The grade card shows a maximise run's score and how it ended (`+517 gold (ended by agent)`), not "fail": such a run
  has no pass or fail.
- Config in `playthrough.toml`, migrated from `profile.json`.
- A run store, a v1 JSON API and an SSE stream that resumes from `Last-Event-ID`.
- A Svelte web app: Watch, Memory, Timeline, Launcher, Overlay (transparent 1920x1080 for streaming) and Replay.
- A recorder: ffmpeg writes the game into rolling MP4 segments with a disk cap and a RAM and heat guard, plus a
  live MJPEG view.
- Party positions sampled into `positions.jsonl`, shown as a breadcrumb plot.
- A supervisor that starts, stops and resumes a multi-session playthrough, runs the keeper between sessions, nudges
  after two sessions without progress and stops after three crashes in a row.
- Playwright tests of every screen against a fixture run, off by default.
- Operator messages: `[operator] messages = true` in `playthrough.toml` (Launch field `operator_messages`, off by
  default) lets `POST /api/v1/playthroughs/<pt>/messages` (`{"text": ...}`, the same write rules as start/stop) queue
  a message for the agent: 201 `{id, t, status}`, 409 when the option is off or the dir is a bench run's, 400 for an
  empty or over-1000-character text, 429 for more than one per 5 s or 10 still queued. The runner gives the bg3
  server `BG3AI_OPERATOR_DIR` (`runtime/sessions/<pt>`) only for such a playthrough; at every agent start (a new
  session or a restart) a delivered message its session's stream log never showed is queued again (a `redeliver`
  row). The Watch snapshot carries
  `operator: {enabled, messages}`, the SSE stream an `operator` event per new row (with `playthrough`), Replay
  `operator` events at the sent and delivered times, and each session's `supervisor.json` row, `/sessions` row and
  Timeline session `operator: {enabled, sent, delivered}` (null when the option was off).
- The operator messages' dir reaches a session only from the runner, for a playthrough with the option on: the
  `harness` runner's agent and the `legacy` runner's `play.py run` drop a `BG3AI_OPERATOR_*` the app's shell
  exports, and the `legacy` runner passes `BG3AI_OPERATOR_DIR` to `play.py` (its bg3 server delivers; no restart
  redelivery there). The memory keeper's client and bg3 server never get one (`keeper.keeper_env`).

## Before the overhaul (`main` up to 471ee66)

claude-run-2 finished the game (2026-10-09 to 2026-10-18), and three Nautiloid series ran 9 of 9 to the goal. The
history is in git.
