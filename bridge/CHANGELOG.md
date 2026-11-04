# bg3ai bridge: tool interface changes

Every change to what a client sees (tools/list, server instructions, tool result wording a model acts on) gets an
entry here, and the golden dump (tests/golden/tools_list.json) is regenerated with
`cd bridge && uv run python tests/test_tools_golden.py --update` in the same commit. The package version stays
0.1.0 until uv.lock can be relocked.

## 2026-11-04: in a fight EVENTS shows the whole batch; casts and party rows are never cut

- While a fight is open (or the drained batch has fight events), a result's EVENTS shows every row of the batch, up
  to 200 after the folds; out of combat it stays the newest 25. REVIEW-14: 264 of launch-v1's 283 cut results were
  inside a fight (median 14 rows cut, p90 51, max 150), the enemies' turns the agent could not see.
- On top of spell_info, every `cast` row and every row whose target or roller is a party member is shown even when
  the cap cuts the rows around it. A folded row (status group, zero-damage run) is kept when any of its rows would be.
  `(+N earlier events)` counts only the rows not shown. No tool or tool description changed.

## 2026-11-04: the EVENTS tail keeps spell_info; back-to-back zero-damage rows share one

- A `spell_info` line is shown even when it falls before the 25 newest events: it comes once per spell per fight, so
  a cut one never came back. w11 L4: one end_turn + wait_for (44.6 s) showed `(+56 earlier events)` and Balthazar's
  Cloudkill spell_info was among them. `(+N earlier events)` counts only what is not shown.
- Back-to-back `damage ... amount=0` rows that differ only in their target share one row, each target with its count:
  `damage source={e7 name=Balthazar} type= amount=0 x9 on 5: Apostate Defender e9, Apostate Defender e12 x2, ...`.
  Only consecutive rows: a damage event names no spell, and a miss is a zero row next to its cast, so the order is
  unchanged. A single zero row reads as before.

## 2026-11-04: walk_toward goes where the engine's path ends; a short stop at its own point says so

- `walk_toward(direction=...)` walks to where the engine's path to the farthest walkable sample ends (within 2.0 m of
  it), not to the sample itself. launch-v1: 113 direction walks stopped 2-5.4 m short of the sample, at the edge of the
  walkable ground, and failed "could not get closer to the destination".
- When a walk to walk_toward's own point (a direction, or an objective's way) still stops short, the failure reads
  `stopped 3.3 m short of the spot walk_toward picked (the farthest walkable point toward N): the walkable ground ends
  about there; walked 6.2 m`. A point the agent named (move_to, walk_toward target=) keeps "could not get closer to
  the destination". The 2.0 m arrival tolerance is unchanged.

## 2026-11-04: trade counts items already on the table as moved

- `trade(offer=<ids>, confirm=true)` after `trade(offer=<the same ids>)` no longer fails "not confirmed: some items
  couldn't be moved": an item this trade already put on its own side of the table is not moved again (a second move
  would take it back off) and is listed `ALREADY ON THE TABLE: e83 Scroll of Revivify, ...`. An id on no side still
  fails the confirm (NOT MOVED). launch-v1: 67 such failures in 19 runs (haiku act3_gold_max r2 seq 16-21).

## 2026-11-04: screenshot waits where the running game writes

- `screenshot` looks for the PNG in the folder the game's capture layer writes to (the output_dir in the running
  game's VK_LAYER_MESA_SCREENSHOT_CONFIG), before BG3AI_SHOTS_DIR or runtime/shots. v010 window 9: a bridge from
  another checkout waited 5 s in its own folder while the game wrote each PNG elsewhere in 0.5 s. Its timeout now
  names the file it waited for: `game did not present a frame in time (minimized or loading?): no <path>`.

## 2026-11-04: react(use=true) clicks a lone reaction row instead of pressing its command

- Live window 7: pressing the lone row's command (InterruptReactCommand) left Karlach's Reckless Attack prompt up and
  the row unchecked (TIMEOUT twice). The row's parameter is a PassThrough multi-binding (decision, interruptor), which
  bg3le most likely reads as nil (as with DonateBtn), and Lua can't build it. A lone row not yet chosen now goes to the
  same hover-checked mouse click as prompts with one row per level (`needs_click`); a row already chosen is still
  accepted by command, and several interruptors still use their rows and Accept.

## 2026-11-04: react(use=true) on a lone reaction row takes the reaction

- A reaction prompt with one interruptor and one row (Reckless Attack after a natural 1, v010 live window 6) has its
  Accept button collapsed; a player answers by clicking the row. `react(use=true)` now presses that row's own command
  (InterruptReactCommand with the row's parameter) instead of InterruptAcceptPlayerCommand, which closed the prompt
  without the reaction. A row already chosen (checked) is accepted as before; prompts with one row per level still go
  to the hover-checked click; several interruptors still use their rows and Accept.

## 2026-11-03: ui_action can't change the difficulty, its settings or the game options

- `ui_action` refuses a command that would change the difficulty (UpdateDifficultyCommand,
  UpdateCustomDifficultyCommand), its custom settings such as Karmic Dice, the ruleset or the game options, by its
  name, target or parameter, or by the settings page being open (NewGameSettings, GameOptions; close/back/cancel
  pass): `ui_action UpdateDifficultyCommand refused: it changes difficulty; the scenario fixes the difficulty, its
  settings (Karmic Dice ...) and the game options for the run`. Every attempt is a referee ring record
  (`rule_change`, facts v1 compatible extension). Why: REVIEW-5 F8, the scenario pins them and the escape hatch
  reached them mid-run.

## 2026-11-03: flee's greyed-out reason from the game's flee state

- `flee` on a greyed Flee button names the game's reason when the HUD's own tooltip fields are empty (v010 live window
  5: FleeBlockCause empty, FleeDistance -1/-1, so it said `the game gives no reason`): ui.flee_probe {char} reads the
  fleeing character's eoc::FleeCapabilityComponent (Flags: EnemyTooClose, CantMove, Downed, Incapacitated, Region,
  Condition, Summon, PartyFollower; CurrentFleeDistance / FleeDistance): `Flee is greyed out: Enemy too close!
  (1.4/27 m)`.

## 2026-11-03: dialogue rows pair with their answers by the number shown; flee says why the game won't

- Answer rows pair with the live answer VMs by the number each row shows (the game's AnswerTextPrefix, AnswerIdx + 1)
  when the other pairings fail, so a list with a wrapped row is answered with SelectAnswerCommand and keeps its tags.
  v010 live window 4: Lae'zel's cage "Get rid of them." (6 answers, the 4th "[PALADIN] [DECEPTION] ..." on two lines)
  was counted as 7 rows from the first row's height, went out without VMs, and the row's button press was ignored
  twice. An answer the game ignores for 3 s with no readable BoundEvent gets the number key it shows (1-9).
- `flee`: when the game has the Flee button greyed out (CanFlee false), it fails at once with the reason the button's
  tooltip shows a player: `FAILED: the game won't flee now (Flee is greyed out: Enemy too close! (10.6/27 m))`. No
  command is run and no cursor search is made. Each Flee command name runs once (window 4: one name on 20 pages, a
  2 s wait for the question after each; with the 48-spot cursor search the call took 48 s).

## 2026-11-03: an answer the game ignores is pressed with its number key; a scrolled answer list is the same node

- `choose_dialogue` (and every tool that answers through ui.dialogue_choose): when the game still shows the same
  answers 3 s after the answer's SelectAnswerCommand (or its button press), the mod presses the answer's own bound
  key once (BoundEvent `UISelectSlot<n>` -> number key n) and the result says so: `note=the game ignored the
  answer's command for 3.0 s (same answers still shown): pressed its key 3 instead`. An answer with no number key
  bound is not pressed again; the note says the game has not taken it.
- The settle after an answer (and after a roll's Continue) compares the node by speaker, line, the list's whole
  length and the text at each answer number both reads show, not by the first visible row: a list wheeled during
  the wait (the bridge's ui.answer_list) no longer ends the job `done` on the unanswered node.
- Why (ops/logs/dialogue-click.md): v010 live windows 1 and 3 and launch-v1: 51 of 1186 choices, all on two
  nodes (Lae'zel's cage "Get rid of them." 33, Gimblebock 18), returned `still running` after 20 s with the same
  answers back; the number key took the same answer at once.

## 2026-11-03: clouds, surfaces and concentration in combat output; drop links; a busy End Turn press

- `get_combat_state` lists the `HAZARD` lines `look` prints (nav.hazards: the harmful clouds and surfaces a party
  member sees within 12 m of the selected character), every call. The turn block that ends `end_turn` and
  `wait_for('my_turn')` and the result of a combat action (`use_ability`, `move_to`, `walk_toward`, `jump_to`,
  `react`) add them when the patches in view changed since they were last shown (one appeared, went or moved over
  2 m; `HAZARD none in view now` when the last one went), the same delta `look` keeps. The read rides in the eval
  those calls already make (state.status / state.actions / the CHANGES read): no extra round trip.
- state.nearby rows carry `concentration` (the creature's spell, as state.combat rows already did); get_nearby shows
  `concentrating on Cloudkill`, and CHANGES says `~ e7 Balthazar concentrating on Cloudkill` /
  `no longer concentrating on Cloudkill` (and on a creature that comes into view). Party rows in get_status show
  their own concentration the same way.
- A walk down a drop link of its own route (the engine's path steps off a ledge; the walker falls straight down in
  one tick) is no longer failed as `engine teleported the character (not allowed)`: Navigation N.project put a point
  on a vertical route segment at its top, so the landing was "off the route" by the drop's height.
- `ui.command` refused by the busy UI at its first call answers "refused before its first UI call" again (its
  confirm-box fallback made a second refused call, which read as "met it after a UI call ran"), so the bridge asks it
  again after the cooldown: end_turn's HUD End Turn no longer fails at once with `bg3le: UI busy`.
- Why (ops/logs/act2-fights-analysis.md, ops/logs/combat-info.md): in all 11 lost launch-v1 Balthazar runs Cloudkill
  sat on the party and the agent learnt of it only as `+Cloudkill` on whoever it had hit; v09 Moonrise lost turns to
  the teleport and busy failures.

## 2026-11-03: a walk there ends in its talk or use on arrival; do_actions' walk end has the minimap block; a walk a conversation ended says so

- `talk_to` / `interact` on a target past reach or out of sight walk there first (travel_to, blocking) and then talk
  or use it in the same call, its result in that result (`e7 is 50m away: walking there first; arrived, then
  interact e7`). A walk there that stops short (a hostile in view, damage, a prompt) does nothing more and says
  `interact e7: not run (the walk there stopped before arriving)`; it was queued after the trip and ran where the
  walk had stopped. A walk still going at the ~90 s walk budget queues the talk/use for the arrival, as before. The
  two tools' descriptions say so. review-v010-final #1.
- `do_actions` that ends with its walk step ends with the minimap block (OBJECTIVES, WAYS, LEADS) as every other walk
  end; so does `wait_for('queue')` when the steps after a conditional walk are listed as not run. review-v010-final #4.
- A blocking walk whose wait a dialogue screen ended starts with the walk's own result when the conversation ended
  the walk (`move_to: INTERRUPTED — a conversation started`); it said `wait_for('j1') keeps waiting for it` for a
  walk that was over. A conversation that doesn't stop the walk (someone else's) still gives the id to wait on.
  review-v010-final #6.

## 2026-11-03: travel_to doesn't take along a member who is far away at the start

- The leader's `travel_to` no longer re-chains or waits for a member more than 24 m away when the trip starts
  (party.FOLLOW_M). Its first lines say so once: `not with you: e1 Shadowheart 77m (unchained, stays there)` (a
  chained one: `(not waited for)`). Members within 24 m are re-chained and waited for as before.
- No straggler check after the leg that arrives: the trip ends `arrived` and its queued steps run.
- A straggler's walk back gets time for the gap: max(30 s, gap / 2 m/s + 10 s), at most 60 s (party.walk_back_s);
  it was a flat 30 s.
- The stop gives how the walk back ended instead of a guessed cause: `e1 Shadowheart 57m behind can't keep up — its
  walk back: no walkable ground leads further toward it (57m left); route(who=...) for their way, regroup(), or
  stay(who) to leave them on purpose` (was `... can't keep up (stuck on a ladder, gap or door?): ...`).
- Why (ops/logs/keep-up-v010.md): the bench saves start with Shadowheart 77-86 m off; the trip re-chained her, her
  walk back didn't fit in 30 s and the leader's trip stopped at once, in 10 of 171 launch-v1 runs and every bp-live
  v0.10 probe walk from those saves. A trip that still stops on a straggler stays as it was (owner decision).

## 2026-11-03: a walk step its condition skips doesn't end the batch

- `do_actions` / `queue_actions`: a walk step with `if`/`unless` no longer drops the steps after it before anything
  runs. They run when the condition skips the walk; when the walk runs, they are not run and each says so:
  `3. interact e12: not run (out of combat a batch ends with its first walk step, here step 1 move_to 5,0,0)`. The
  first walk step without a condition still drops the rest at once, as before. Steps a later `queue_actions` call
  appends are not affected. Docstrings, rules text (game-and-tools.md) and play/knowledge/tools.md say so.
- review-v010-human-gap #5.

## 2026-11-02: out of combat a batch ends with its first walk step

- `do_actions` and `queue_actions` (and `do_actions(wait=false)`): a walk step (`move_to`, `walk_toward`,
  `travel_to`, `jump_to`) is the last step a batch takes. The steps after the first walk step are dropped before
  anything runs; the steps up to and with the walk run (or are queued) as before. The result lists the dropped ones:
  `not run: interact e12, loot (out of combat a batch ends with its first walk step, here step 2 move_to e12)`
  (`not queued: ...` for queue_actions). Batches without a walk step, or with the walk last, are unchanged
  (inventory chores, looting sweeps of interact/loot/pick_up, dialogue chains). A `queue_actions` call made while a
  walk or trip runs still appends after it (a separate call, after travel_to/move_to/walk_toward wait=false).
- Chosen over refusing the whole batch: the steps after the walk never run blind either way, and this way the call
  still does the part up to the walk instead of costing a round trip and a resend.
- Docstrings: do_actions' example no longer starts with a move_to (interact walks to its target itself). Rules text
  (game-and-tools.md) and coaching/pace.md's sweep example say the same.
- Human-gap research §4: 76 deepseek do_actions batches were walk_toward chains (three headings committed before the
  first one's end was seen).

## 2026-11-02: travel_to and regroup name what they passed

- A trip's result (travel_to; regroup for its walkers' trips) has a PASSED line before CHANGES: what came into view
  at a leg's end during the trip and is out of view at its end, nearest first from the walker's end spot, up to 6,
  then `+N more`: `PASSED (seen on the way, out of view now): e12 Chest <container> 46m W | e7 Goblin hostile 28m SW
  (5m down)`. Characters with their relation (and `dead`), leads with their kind (and `locked`/`trap`/`story`), as
  CHANGES shows them; scenery and clutter are left out, and so is anything the agent's last view already had (CHANGES
  says GONE for those). No line when nothing passed.
- The read: state.nearby (radius 25, centred on the walker: its new `center` argument) in the check each finished
  leg already makes, the same round trip (one op more in its server eval). A mod without `center` centres it on the
  selected character.
- Human-gap research §1e: CHANGES diffed the trip's start against its end only.

## 2026-11-02: required camp talks are named at night; no sleep press under the game menu; a misread variant list is refused

- `long_rest`, camp leave and `fast_travel` from camp, at night in camp while the camp story still wants a talk
  (the "!" over a character), refuse before pressing anything and say who: "long_rest: FAILED — not pressed: the
  night at camp can't end yet: the game still wants a talk with e4 Shadowheart first (the '!' over them); going to
  sleep only brings up its 'talks left' notice until then, and a talk with them clears it" (camp leave /
  fast_travel: "FAILED: nothing was pressed: ... Until the night ends, travel from camp is blocked"). Someone out
  of sight is said as "someone not in view", neither named nor counted (the game's notice doesn't count them
  either). Until now the sleep press got the game's "talks left" notice and the call failed 30 s later naming only
  widgets (launch-v1 Haiku act3_gold_max: 46 failed long rests). At night camp, `get_status` and `look` add only
  what the screen shows to their TALK line, the camp '!' over the characters in view ("TALK: night at camp: the
  '!' over e4 Shadowheart"), once while it is unchanged; nothing when none of them is in view.
- `long_rest`, and the sleep press of camp leave and `fast_travel` from camp, refuse at once while the game menu is
  open: "the game menu is open: close it first (press_key ESCAPE), then long_rest" (launch-v1 Haiku act3_gold_max
  r1 pressed sleep twice under the pause menu its own click had opened).
- `use_ability` on a spell container lists only the variants that read as a spell's stats entry and says how many
  entries didn't ("choose a variant: Target_LayOnHands_Cure, ... (1 more entry of its variant list did not read as
  spell names)"), after one more read; with none that reads, "choose a variant (this action's variant list did not
  read as spell names)". The raw text is never shown (fights-v09 Balthazar r3 showed a level mesh name as Lay On
  Hands' variant).

## 2026-11-02: walks out of combat return at their end (wait=true is the default)

- `move_to`, `walk_toward` and `travel_to` default to `wait=true` and, out of combat, return at the walk's end with
  the walk's own result: what `wait_for(<job>)` / `wait_for('queue:<who>')` returned until now (the same early
  stops: a prompt, a dialogue or a fight starting ends the wait and says so), with CHANGES and the minimap block.
  The `started j12` line is not shown when the walk ended. `route(go=true)` and a far `move_to` (via travel_to)
  block the same way.
- A walk or trip still going 90 s after the call started (plumbing.WALK_BUDGET_S, under the clients' 110 s MCP
  timeout) returns its progress (`still waiting for j12 after 90s`, the RUNNING line with metres/ETA left) and
  `wait_for('j12') keeps waiting for it` (`queue:<who>` for a trip), never a tool timeout.
- `wait=false` keeps the old behaviour (returns within ~1 s, `started j12, ETA 3s`, NEXT hints for pipelining).
  The pace-guard note ("wait=true ignored outside combat") and travel_to's "(wait=true ignored ...)" are gone.
- Metrics: a call's `async` flag now needs its own background job still running when it returned (a blocking walk's
  job starts and ends inside the call); a walk that ended in the 0.3 s settle window no longer counts as async.
- Human-gap research: 74-89% of launch-v1's out-of-combat walks were followed at once by wait_for (2,866 calls,
  ~93 min of model think, ~$22).

## 2026-11-02: every walk end and every look carry the minimap block

- Out of combat, every walk end (wait_for on a walk or trip, a walk that ends in the settle window, a blocking
  walk, jump_to, travel_to's settle, regroup, a fast travel's arrival) and every `look` end with three lines, always
  (not only when something changed), arrived or stopped short:
  `OBJECTIVES o1 Escape the ship 16m W (3.4m down) [Escape the Nautiloid] | o2 ... | +1 more (get_map lists all)`
  (up to 3, here first, nearest first; another area's marker says `(another area)`; `none on the map`), the
  `WAYS walkable:` line, and `LEADS (unvisited; get_frontier lists all):` with the 3 nearest unvisited leads (was
  the first 5 unvisited, and only after a walk that ended `done`).
- One renderer (bridge/src/bg3ai/minimap.py). Its frontier read (`nav.frontier ways_cached`) moved into the CHANGES
  read's eval, so a walk end no longer makes a separate nav.frontier round trip (launch-v1: median 56 ms, p90 96 ms
  each); look and the other walk ends read it in their existing eval. A busy spot's block is under 300 tokens.
- `look(full=true)` no longer prints its separate OBJECTIVE lines: the block carries them.

## 2026-11-02: trade sell="junk" names what it sold

- The result lists every item sold, grouped by name with the count and the gold at the shown prices, most gold
  first (`SOLD Goblin Scimitar x4 12 g | Skull x2 4 g`), and counts what the junk rule kept back of the items on
  the trade screen by kind (`KEPT (not junk) on this trade screen: 12 consumables, 4 equipped, 1 story`). The
  sale's own item events stay out of EVENTS (the list replaces them). launch-v1's 36 sell=junk calls sold 430
  items for ~4,000 g, and their results named none of them.

## 2026-11-02: roll_check stops on a failed roll; a reroll is its own call

- A failed roll with Inspiration left returns with the roll screen still open (the roll line shows DC, die, bonus
  and Inspiration left) and says how to go on: `roll_check(reroll=true)` rolls again for 1 Inspiration,
  `roll_check()` continues with the failure. A success still continues in the same call; a failure with no
  Inspiration left, or a reroll the game ignored, continues too (nothing to choose).
- `reroll_on_fail` is removed (not refused): it rerolled before the failure was seen (human-gap research: 15 of
  launch-v1's Inspiration points were spent that way). `reroll` replaces it and acts on the result on screen.

## 2026-11-02: the action list prints costs and ranges as the game states them

- A cost's named resources read in words, after the action economy and the spell slot, in one fixed order:
  `[A+Channel Divinity 1]` (was `[A+other:{'ChannelDivinity': 1}]`), `[A+slot L1]` (was `[A+slot L1]` or
  `[slot L1+A]`, by run). The NOW line names those resources the same way (`Channel Divinity 1/1`).
- A range the mod can't put in metres is left out: Throw read `Throw [A] ThrownObjectRangem point`, now
  `Throw [A] point` (the game works the throw range out per object). The mod's state.actions sends `range` only in
  metres (a number, or the wielded weapon's range).

## 2026-11-02: in combat each call is one action; combat_turn is removed

- combat_turn is gone from tools/list (94 tools), with its plan parsing, fallbacks, retargeting, automatic bonus
  heal, own-summon-first turns, list-of-plans chains and background end (fight-flow research, D291: in launch-v1
  the agents played fights as whole turns planned from one read; the owner's rule is one action, observe, then the
  next). Summons are played like any party member.
- do_actions and queue_actions run no steps in combat: "REFUSED: in combat <tool> runs no steps: each call is one
  action (use_ability, move_to, walk_toward, jump_to, react, end_turn)". `combat` is no longer an `ignore=` kind, so
  a batch or queue started out of combat stops when a fight starts.
- In combat use_ability, move_to, walk_toward, jump_to and react end with a CHANGES block (other combatants' HP,
  deaths, moves, statuses since the agent's last look). That costs one act.watch read per call of these tools, also
  out of combat.
- In combat a walk (move_to, walk_toward) returns at its end whatever `wait` says.
- end_turn and wait_for('my_turn') hand over with the acting member's turn block as get_combat_state shows it
  (reach line, `YOUR ACTIONS (eN)` and the action list): one state.actions read more.
- Texts: the server instructions say "In combat each call is one action (...)"; an attack that starts a fight says
  `COMBAT: the attack started a fight (round N): get_combat_state shows the turn order`; SHARED TURN says
  `(who=<member> on each action, end_turn for each)`; end_turn on an ended member says "its turn already ended (the
  rest of its turn group is still acting)"; camp in a fight "a fight is on: finish it first, then camp()".
- Compaction keeps its combat_turn rules for recorded runs (golden replay, obs_size).

## 2026-11-01: fast_travel from camp names the conversation screen; get_status stops re-sending a refused re-chain

- fast_travel from camp with a conversation screen up fails "FAILED: a conversation is open, nothing was pressed;
  the game doesn't answer the camp button while a conversation screen is up" instead of "can't fast travel now
  (Condition)" (live-v09c 1c: ui.fast_travel's dry run reads TravelError before IsInCamp, so under the dream's
  narrator screen it refused "(Condition)" and the leave-camp-first check never ran).
- get_status/look re-chain an unchained member once per spot: when the game didn't attach it (act.chain failed or
  ran out its 3 s), it is sent again only after the member or the leader moved 5 m (live-v09c c2: 3 reads at 3.1 s
  each with "Shadowheart 99m, unchained"; 76 of 245 logged re-chains timed out, all 78 m or more apart). The
  PARTY APART line is unchanged.
- act.chain (mod ActionsParty.lua): an attach the game doesn't take fails once the member is still not in the
  leader's group 0.6 s after the order ("the game didn't attach <who> to <leader>'s group: still apart 0.6 s after
  the order (<n> m away)"), not after the job's 3 s timeout. The game gives no refusal of its own (the Osiris call
  returns, nothing is logged); the 60 logged re-chains that took showed InSamePartyGroup within 182 ms of the order.
  The first get_status after such a split, regroup's and set_party's chains wait 0.6 s instead of 3 s.

## 2026-11-01: camp leave with a conversation screen up refuses at once; no "[ForceUpdate]" notice

- camp leave / fast_travel from camp: with a conversation screen up on the client (ui.travel_block dialogue), they
  fail at once with "FAILED: a conversation is open, nothing was pressed; the game doesn't answer the camp button
  while a conversation screen is up" and press nothing (live-v09b: right after the 1st long rest's dream, the
  rest's narrator screen still up, the server's conversation check let the press through; it went unanswered for
  13.5 s and failed "cannot travel right now" with no reason).
- A press the game still doesn't answer ("cannot travel right now": act.leave_camp's wait ran out) gets on its
  FAILED line " The game didn't answer the camp button: the party stayed at camp. When it was pressed: <facts>.
  After the wait: <facts>." (facts from ui.travel_block: night at camp, the travel buttons greyed out
  (<TravelError>), the HUD hidden) instead of the block read only after the wait.
- get_status NOTICE (ui.state notifications, mod UIState.lua): a TextBlock whose Text is the formatter's
  "[ForceUpdate]" placeholder reads its inlines; with none it is left out (live-v09b: "NOTICE [ForceUpdate]" after
  a camp leave).

## 2026-11-01: use_ability's opener, camp leave in a night camp talk and WHERE after a fast travel say what is true

- use_ability out of combat at a creature: when a conversation is open after the hit (live-v09: the game's "You
  picked a fight" scene, with combat on 0.3 s after the result), the result says "combat: not started — a
  conversation is open, opened by the hit" (without ", opened by the hit" when one was open before the cast)
  instead of "combat: NOT started — … no fight began (dormant or unaware …)".
- camp leave / fast_travel at night camp during a camp conversation fail at once with "FAILED: a conversation is
  open, nothing was pressed; at night in camp, leaving first ends the night (the party sleeps: a long rest)" instead
  of long_rest's "… then long_rest".
- get_status after a teleport (fast_travel, travel_to's fast-travel leg, camp go/leave): while the minimap's
  subregion line (WHERE's subregion) still shows the name it had when the trip left, WHERE leaves it out and a line
  says "SUBREGION not updated since <the trip to <waypoint> | the trip to camp | leaving camp>: the minimap still
  shows '<name>', as it did before" (live-v09: "WHERE Blighted Village" 0.6 s after arriving at Roadside Cliffs).
  Nothing waits for the line.

## 2026-10-30: a give the game refuses fails; get_character shows status names only; a busy UI reads as busy

- manage_inventory op=give: when the game leaves the item with the giver (no error, no move: a story item), the
  give fails after its 8 s wait with "the game kept it with <giver>: it can't be handed over" instead of
  "TIMEOUT — timed out" (mod act.transfer on_timeout).
- get_character shows each status by the name the UI shows, without the engine's status id, on a party member's
  sheet and on an examined character, as look_at already did. It served ids like DIFFICULT_TERRAIN_DEEPWATER.
- camp leave / fast_travel at camp: when the game's UI is busy (bg3le refuses the read of whether a long rest's night
  still blocks travel) they fail at once with "FAILED: can't read whether a long rest's night at camp still blocks
  travel: bg3le: UI busy …" instead of reading it as no night and failing later "cannot travel right now".
- pick_up on an item the game can't pick up (fixed in place, listed `<usable>` like a wall torch) fails at once with
  "that can't be picked up: it is fixed in place (interact uses it)" instead of "TIMEOUT — timed out" after 40 s.
- With a dead member that still holds the game's ClientControl (its corpse once selected), the selected member is
  the living one set_party selected: get_status/look's active member and PARTY APART, fast_travel's LEFT BEHIND
  and "already stands at the waypoint" answer, and nav reads measured from the corpse before. A fast_travel to the
  waypoint the selected member stood on then timed out after 66 s instead of answering at once. fast_travel also
  watches the living holder for the arrival, so a trip that moved it no longer waits on the corpse.
- short_rest in combat, or while any party member is in a conversation (also one that isn't selected), fails at once
  with "not pressed: in combat" / "a party member is in a conversation" instead of "TIMEOUT — timed out" after 15 s:
  the game ignored the press there while the selected member's rest button still showed as available.

## 2026-10-30: do_actions stops at a 90 s time budget

- do_actions (the blocking call) stops before its next step once it has run 90 s (`actq.BATCH_BUDGET_S`), and a
  wait step is cut to the time left ("wait: waited 6.0s of 25s (time budget)"). The result ends
  "STOPPED: time budget (90 s per call), N steps left; not run: …". The clients give up on an MCP request after
  110 s: six 30 s waits failed the call as "MCP request timed out" while the bridge ran the batch on for 70 s more.
  queue_actions (do_actions wait=false) has no budget: it returns at once and runs while the agent plays on. Its
  description names the budget.

## 2026-10-24: feed_gale closes the donation window and checks Gale took the item

- feed_gale donates through the window's DonateItemsCommand, clicks the window's X (Gale absorbs the item when it
  closes) and says done only when the item left the party and Gale's Arcane Hunger is gone. Live D10 it said
  "done — donated=e132 window_open" with nothing given. Its description says it donates and closes the window.

## 2026-10-22: served rules follow the served tools; no screenshots for a text-only model

- In the rules condition the served rules (instructions, first briefing, `rules` prompt, bg3://rules, pi's system
  text) leave out the rules part of a tool group the session switched off (BG3AI_TOOLS / BG3AI_TOOLS_OFF): with
  screenshots off they still described `screenshot` and `zoom`. rules+coaching keeps the whole text (it needs every
  group).
- A pi profile whose model has no image input runs with the screenshots group off (bg3harness profiles.tools_off,
  PiClient's server env): it was told "(see attached image)" and given no picture.

## 2026-10-22: coaching nudges only in a coaching condition

- The `PACE: you waited right after issuing…` and `NOTES WINDOW: ~Ns of walking left…` (with its PARTY PLAN line)
  result nudges are coaching: served only when `$BG3AI_PROMPT_CONDITION` is `rules+coaching` (rules.coaching).
  Rules-only sessions (the default, benchmark ranked runs) get neither. Playthroughs set rules+coaching (runner;
  play/.mcp.json for Claude Code).

## 2026-10-22: live4 §2/§5 fixes (pickpocket, summon attack, flee, trade table)

- pickpocket: a window it didn't open is closed and then waited for (≤2 s) before this NPC's is opened; the open
  sent right after the close went with it ("FAILED — no pickpocket window is open" 2/2 after another process left
  the window open). A window that stays open says so.
- pickpocket(take=...): the POCKETS line after a steal is read again (it still listed the stolen Gold x15).
- combat_turn / use_ability: a guessed Target_MainHandAttack for a character with no weapon attack becomes its
  Unarmed Strike (a summoned Cambion: "the character does not have that action").
- flee's "enemy too close" names the enemy from the fight's turn order ("e7 e7" before).
- The trade table says "the trader pays you Ng on confirm" for an overpay their gold covers (confirm presses the
  game's Balance; "you overpay 30g" was followed by +30 gold), and what is still overpaid when it doesn't.

## 2026-10-22: benchmark neutrality audit (docs/BENCHMARK-BIAS.md status table)

- The server instructions say what the tools do, not how to play well: "Pipeline like a human clicking ahead",
  "keep planning while the character walks; wait_for only when ..." and "Use screenshot only when ..." are gone
  (coaching/pace.md and efficiency.md have them for the coaching condition). Every client and condition got them.
- level_up's and create_character's `plan` and set_reactions' `settings` take a JSON object as well as the same
  JSON as a string (D15); their schemas said `string` while the descriptions said "plan: JSON like {...}".
- combat_turn's and wiki_page's descriptions lost their session stories (S023-S026, S018-S027).
- The `rules` prompt and bg3://rules resource no longer describe themselves as "play/CLAUDE.md, the rules Claude
  Code loads".
- The server-side stop refusal names only the tools the session has: with the memory group off it no longer says
  "call end_session" or "HANDOFF.md", with saves off it no longer names save_game.

## 2026-10-22: run 1 result wording (live3, pi + DeepSeek Flash)

- A do_actions/queue_actions/combat_turn step whose tool can't be a step (look, loot_area, ...) is refused with the
  list of tools a step can be, and "call other tools on their own".
- insert_item on an object that answers with a conversation (the Nautiloid's rune console) says "using it opened a
  conversation instead" when the conversation comes up after the interact returns too; it said "no Insert Item
  panel opened ... not every pedestal takes items" while the conversation offered the insert.

## 2026-10-22: D15 tool interface review (one interface for every model)

- do_actions and queue_actions take `actions` as a JSON array (or the `{"actions": [...]}` object), and combat_turn
  takes `plan` as a JSON object or array of member plans. The JSON string form still works. The schemas used to say
  `string` only, so a model that follows a schema literally had to escape JSON inside a string.
- The server instructions are built from the served tool set (capabilities.py): with the memory group off they no
  longer name memory_* tools, knowledge/, session_start or end_session, and with screenshots off they no longer
  mention screenshot.
- Wording for any client, not only Claude Code: "end your turn" became "stop (no more tool calls)" in the
  instructions, end_session's description and its results. note's description says memory_read/memory_edit
  instead of Claude Code's file Read/Edit.
- server.INSTRUCTIONS and server.RULES exist again (tools/pi/dump_server.py reads them; the server.py split had
  dropped them, so `dump_server.py --system` failed with AttributeError).
- Size (all 94 tools, chars; ~chars/4 tokens): tools/list wire form 68,499 -> 69,023 (+524, the anyOf schemas),
  descriptions 35,909 -> 36,010 (+101), instructions 1,398 -> 1,410. With BG3AI_TOOLS=game (74 tools) the
  instructions are 869 chars.

## 2026-10-22: pickpocket fixes (research/ovh/live2.md §1)

- pickpocket(take=...) clicks Steal for real: the mod selects the item and reads its odds without pressing
  (ui.pickpocket min_chance=101), then hoverclick.py moves the cursor until the mod's new ui.steal_probe
  (Client/PickpocketOps.lua) says it is over StealBtn, clicks, and reads the roll. The old Command press never
  started a steal (14 live tries). min_chance is checked in the bridge. A window that closes right after the click
  reads "CAUGHT?".
- pickpocket(npc) closes a pickpocket window this server didn't open for that NPC before opening theirs (a stale
  window of another NPC was listed as this NPC's pockets).
- The description and the pockets listing say the shown odds assume nobody sees the thief.

## 2026-10-22: flee tool (research/ovh/live2.md §2)

- New tool `flee(who)`: the HUD's Flee for one character. Refused while a hostile the character can see is closer
  than 27 m (the button's tooltip rule; state.combat lists only seen combatants). Otherwise it selects the
  character, runs the HUD's flee command when a page has one (Client/FleeOps.lua ui.flee_probe / ui.flee), or clicks
  the Flee button hover-checked (hoverclick.py), answers "Do you want to flee from combat?" with Yes (boxclick.py)
  and checks the character left the fight. The flee command and button names were not read live yet: FleeOps finds
  them by "flee" in their names. The tool count goes from 94 to 95.
