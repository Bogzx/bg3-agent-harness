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
