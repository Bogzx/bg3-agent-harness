# bg3 tools cheat-sheet (called as `mcp__bg3__<name>`)

Targets (`target`, `npc`, `item`, `who`) are short ids from the observation tools (`e12`) or a party
member's name. Ids last one session only: write names in notes. `who` defaults to the active
character. Job results are `done`, `FAILED — reason`, `INTERRUPTED — reason`, `TIMEOUT`, or `started
j12, ETA 3s` (async: a `job_finished` event follows; `wait_for("j12")` to block), followed by EVENTS.
Lines a result may end with:
- `RUNNING j3 move_to ~2s left | queue Claude: travel_to m1 (31m left, ETA ~7s) then interact …` → what is
  still walking or queued right now. Don't wait on it: append with `queue_actions`, then plan or write notes.
- `NEXT: queue_actions what to do on arrival, then look/plan/notes while it runs; …` → after a walk that is
  still running: the order to work in (shown the first few times).
- `PACE: you waited right after issuing…` → on a `wait_for` called <1.5 s after starting something: next
  time do independent work first and `wait_for` last.

## Observe
- `look(full=false)` → **what changed since your last look**, a few lines: `PARTY moved 12m W`, `e4 Gale HP 8->3/12, +Burning`,
  `+ e66 Imp hostile HP 9/9 7m` (appeared), `~ e11 Chest unlocked, opened`, `GONE e14 …` (left view/range),
  `ITEM + e69 Potion x2 (Claude)`, `GOLD 40->65`, `MODE …`/`TURN …`, `QUEST …`, `OBJECTIVE + …`, and any new dialogue/roll/popup.
  "No change since your last look." is a real answer. `full=true`, the first call and a new area give the full status + nearby view.
- `get_status(full=false)` → where you are, party HP/conditions, mode, what's on screen, objectives, recent events. Start here.
  After the first call it shows only changes (like `look`, without nearby/items); `full=true` for everything.
- `get_party()` → each member's HP, AC, level, conditions, resources (slots, actions), concentration.
- `get_character(who, sections="")` → full sheet (party) or Examine view (others); e.g. `sections="resources,equipment"`.
- `get_actions(who)` → usable actions/spells/items with costs and availability (ability ids for `use_ability`).
- `get_nearby(radius=25, kinds="")` → visible characters and objects with ids, distance, relation, locked/opened.
- `get_frontier()` → leads to explore: doors, ladders, `<exit>`s (hatches, trapdoors, stairs, gates, passages: listed even where you have walked) and unvisited things, with direction; a `WAYS walkable: N 20m+, NE 20m+ (6m up), E 8m | blocked: W SW` line (how far the ground is walkable straight out in each direction; `blocked` = not even 8 m: a wall, cliff or water). Use it for "which way out / where's the hatch" instead of a screenshot. Plus `HAZARD Fire 3m W (2x4m)` lines for harmful patches in view (nearest edge, direction, E-W x N-S size). `look` shows the same lines when the patches in view change.
- `get_explored()` → your own breadcrumbs in this area (explored extent, recent named places).
- `path_preview(target | x,y,z, who)` → walking distance, reachable?, needs a jump?, fits remaining movement?
- `route(objective | target | x,y,z, who, connectors=false)` → the way to a goal as text, instead of a screenshot: distance and height (`9.2m above you`), `FLOORS` here and at the goal, the walk in legs (`1. 8m NW`, `2. CLIMB 9.2m up to (x,y,z)`, `3. 6m E`) with fire on it, or `NO WALKABLE PATH` + the `CLOSEST walkable spot` + `LADDERS/DOORS in view` ranked by how much they help (`climbs 9.4m up [interact to climb]`), and one `NEXT:` step. Far goals (80–400 m) get the engine's whole route too, or `NO WALKABLE PATH from here` when this side has none; then also `CLOSEST YOU HAVE BEEN`, `WAYPOINTS (unlocked, nearest the goal first)`, and `BLOCKED BEFORE (this session)` (spots where your trips toward it stopped; a ladder whose top is one says `LEADS TO A DEAD END`). After two stops at one spot, `NEXT:` says the side is closed: come from another side (a waypoint, the roads, the wiki), don't climb back up. `get_frontier` ladders also say `climbs 9m up/down`; leads on another floor say `(9m up)`.
- `look_at(target)` → the Examine panel for a visible character or object.
- `get_inventory(who, filter="")` → equipment, notable items, junk summary, and the OPEN container's contents; filter: equipped | consumables | all | words.
- `get_journal(quest="")` → active quests with latest entry; `quest="<words>"` → all entries of one quest.
- `get_map(full=false)` → (after the first call: only new waypoints/objectives, unless you changed area) region, unlocked waypoints, all active quest objectives (`OBJECTIVE o1 <label> 16m W [quest]`, or "(another area)"), your markers. `get_status()` shows the objectives in this area (max 3).
- `get_events()` → never blocks: everything since your last call, plus `RUNNING` jobs/queues (what's still walking, what's queued).
- `wiki_search(query, limit=5)` / `wiki_page(title, section="", max_chars=5000)` → bg3.wiki; tag it `[wiki]`.
- `screenshot(scale=0.5)` / `zoom(x, y, w, h)` → pixels: only when text can't answer (zoom crops the last screenshot; fractions 0-1).
- `ui_dump(text_only=true)` → text and buttons of the current UI screen; cheaper than a screenshot.
- `session_clock()` → time left in the session.

## Act (exploration)
- `move_to(target | x,y,z, who, run=true, wait=false)` → walk there (next to an entity; refused if there's no walkable path). **Returns at once** with a job id + ETA (a fast failure comes back at once too). Don't pass `wait=true`: plan or take notes during the walk, then `wait_for` if you must. Stops early on combat or dialogue.
- `walk_toward(direction | objective | target, max_m=30, who, run=true, wait=false)` → async like `move_to`; walk as far as the ground allows (direct path, stopping short of harmful surfaces) toward N/NE/…/NW, an objective (`o1` or words from its name; this area only) or a visible id. For "head toward the quest marker" when no door or item leads there.
- `interact(target, who, wait=true)` → door, lever, chest, corpse (opens loot), book, ladder, waypoint; walks there first.
- `pick_up(item, who, wait=true)` → take an item lying on the ground (walks there first).
- `loot(take="all", who)` → take from the open container or corpse: "all" or comma-separated ids.
- `jump_to(x, y, z, who, force=false)` → jump (costs movement plus a bonus action in combat; max distance ≈ Strength/2 m, but jumps over ~4.5 m have landed short live). A landing in fire/acid/… or on a wall (or a long jump that may fall short into one) is moved to the nearest clear spot in reach, and the result says so; with none near, it is refused. `force=true` jumps exactly there.
- `set_party(action, who, leader)` → select | chain | unchain | sneak_on | sneak_off | turn_based_on | turn_based_off.
- `fast_travel(waypoint)` → to an unlocked waypoint (name or id); not in combat.
- `camp(action="go")` → go | leave camp.
- `place_marker(label, target | x,y,z)` / `delete_marker(marker_id)` → your own persistent map markers.
- `wait(seconds=3)` → let game time pass. Prefer `wait_for`, which returns as soon as the thing happens.

## Combat
- `get_combat_state()` → turn order (visible), whose turn, round, the active character's remaining economy.
- `use_ability(ability, target | x,y,z, who, upcast=0, targets="")` → attack, spell, item or action from `get_actions`; `targets` = comma list for multi-target spells.
- `ability_preview(ability, target, who)` → hit chance or save DC, damage, range, line of sight.
- `react(use, which="")` → answer a reaction prompt (Shield, Counterspell, opportunity attack).
- `end_turn(who, wait=true)` → end the turn; blocks through enemy turns until a party member can act, combat ends, or a reaction/roll prompt needs you (`wait=false` + `wait_for("my_turn")` to plan meanwhile).
- `combat_turn(plan, who, end=true, wait_next=false, ignore="hostile", wait_turn=30)` → **a whole turn in one call** (see below). Returns at once after ending the turn with `NEXT …, then <ally> (yours)`, or `NOW <ally>'s turn` plus their actions: plan that turn while the enemies act and **send it at once**: a plan sent before the turn comes waits for it (up to `wait_turn` s; stops on a prompt), so no `wait_for("my_turn")` is needed. Party members with equal initiative share one turn (any order; each needs its own `combat_turn(who=…)` or `end_turn(who=…)`).
- `end_combat_report(what_worked, what_to_change, next_goal)` → required after every fight; goes to your session log.

### `combat_turn` plans
JSON with slots run in `order` (default `move,action,bonus,extra`); each slot is a list of alternatives
tried in order until one works:
- `move`: `"e12"` (walk next to it), `{"target":"e12","within":18}` (stop once within 18 m: ranged/spell
  range; no walk if already there), or `[x,y,z]`. If the walk doesn't fit the movement left, it walks as far
  as it does (`"partial":false` turns that off). A failed move doesn't end the turn: the fallbacks decide.
  It follows the real route (stairs, detours) and keeps out of fire on the way (`"avoid":false` walks through).
  A move that ends out of reach says so and why: `now 9.9m from e12 — stopped short of ≤1.5m: out of
  movement (9m left, the route needs 18.5m)` / `kept out of Fire on the route 4m ahead` / `no walkable
  route gets closer than 7m (raised/blocked …)`. If then every action alternative is out of range, it
  tries the ranged weapon attack itself (`action (auto fallback): …`), else Dash + walk on (out of movement only).
- **Retarget:** when every alternative of a slot missed its target (dead, out of range, no line of sight),
  the plan's own targeted abilities are tried on the nearest other visible hostile in reach
  (`action (auto fallback): Target_SacredFlame e15: done`; also for `extra` after a kill). `"retarget":false` turns it off.
- **Downed ally:** no `bonus` in the plan and an ally at 0 HP within range: Healing Word on them if this
  character has it (`bonus (auto: ally downed): …`). `"auto_bonus":false` turns it off.
- `action`, `bonus`: an ability id (`"Shout_Dash"`), or `{"ability","target" | "x","y","z","upcast","targets","if","unless","then_move"}`,
  or `{"tool":<any do_actions tool>,...}`. `"then_move":true` (Dash) walks the move again after it.
- `extra`: a list of more slots, e.g. the Extra Attack: `[[{"ability":"Target_MainHandAttack","target":"e12"}]]`.
- `reactions`: `{"Divine Smite":"always"}` sets reaction prompts before the turn. `on_reaction`: `"use"` or
  `"skip"` answers reaction prompts that pop up mid-turn (default: stop and let you answer).
- Before every step: still your turn? target still visible and alive? (if not: the next alternative). It stops,
  without ending the turn, on HP below 35%, a prompt, a blocked teleport, or the turn being taken away.
```json
{"move":{"target":"e12","within":1.5},
 "action":[{"ability":"Target_MainHandAttack","target":"e12"},
           {"ability":"Projectile_MainHandAttack","target":"e12"},
           {"ability":"Shout_Dash","then_move":true},
           "Shout_Disengage"],
 "bonus":{"ability":"Shout_SecondWind","if":"hp_below:50"}}
```
Result: one line per slot (`action: Target_MainHandAttack e12: FAILED — target is out of range … → Projectile_MainHandAttack e12: done`), `end_turn: done`, `NOW` (HP, action, bonus, movement), `CHANGES`, `NEXT`.

## Dialogue
- `talk_to(npc, who)` → walk up (following them if they move) and start a conversation (`who` = the speaker).
- `get_dialogue()` → current line and numbered options with check tags. A tagged option shows the check and your speaker's bonus (`← Persuasion check: e2 +5 (party best e1 +6 …)`). Or the roll, reaction or popup on screen.
- `choose_dialogue(option)` → pick answer N; clicks through the filler after it and returns the next real choice, roll or the end.
- `roll_check(boosts="", reroll_on_fail=false)` → resolve the roll on screen (boosts as offered, e.g. "Guidance").
- Cutscenes are auto-skipped; their lines arrive as `dialogue_line` events. Answers are never auto-picked.

## Items
- `manage_inventory(op, item, who, to)` → op: equip | unequip | use (drink/read/eat) | give (`to`=name) | camp (to the camp chest).
- `trade(npc="", offer="", request="", confirm=false)` → `npc` opens the barter; offer and request are comma lists of ids; `confirm=true` closes the deal.

## Progression
- `short_rest()` → 2 per long rest. `long_rest()` → at camp (`camp("go")` first); handle camp scenes as dialogue.
- `get_level_up_options(who)` → what the level-up screen offers.
- `level_up(who, plan?)` → one call per character level. No plan: uses the line `- <Name> L<n>: {json}` in memory/party-build.md. Plan JSON keys: `subclass`, `feat`, `abilities` (`{"Wisdom":2}` or `{"Wisdom":1,"Constitution":1}`), `spells`, `cantrips`, `skills`, `passives`, `class` (multiclass only). A subclass level fails and lists the choices until `subclass` is given; so do spells/cantrips to learn and passives (invocations, fighting styles): the error says `can pick: …`. A feat level defaults to +2 main ability. Spell picks are real clicks the tool does itself (a few seconds). get_status/look show `LEVEL UP: <who> (<level>)` while one is pending (out of combat).
- `new_game(difficulty="Explorer")` → from the main menu: Explorer | Balanced | Tactician, then character creation.
- `character_creation_options()` → what the creation screen offers now.
- `create_character(plan)` → JSON, e.g. `{"race":"Half-Orc","class":"Paladin","subclass":"Oath of Devotion","background":"Soldier","abilities":"recommended","name":"Claude"}` or `{"origin":"Lae'zel"}`.

## Think while acting
- `queue_actions(actions, who, replace=false, ignore="")` → append steps to that character's queue and **return at once**; they run back to back while you keep observing and planning. Same steps as `do_actions`. The end arrives as a `queue_done` / `queue_interrupted {why, left}` event; `wait_for("queue")` blocks until then. Each character has its own queue (two can act at once).
- `cancel_queue(who="")` → stop one character's queue (or all): cancels the running step, drops the rest.
- `wait_for(target="idle", timeout_s=15)` → block only until it happens: a job id (`j12`), `queue` / `queue:<who>`, `idle`, `my_turn`, or event kinds (`combat_started,dialogue_started`). Returns early if a reaction/roll/dialogue/popup appears. Ends with CHANGES.
- `do_actions(actions, wait=true, ignore="")` → JSON list of steps `{"tool": name, ...args}` run in order in ONE call. Allowed: move_to, walk_toward, travel_to, interact, pick_up, loot, loot_corpse, talk_to, jump_to, set_party, use_ability, end_turn, choose_dialogue, roll_check, react, manage_inventory, wait, press_key, place_marker, camp, short_rest.
  - Per step `"if"` / `"unless"`: `in_combat`, `in_dialogue`, `my_turn`, `hp_below:<pct>`, `item_present:<id>`, `target_visible:<id>` (a list = all must hold); a failed condition skips the step.
  - Stops (drops the rest, says why and what was left) on: combat or a conversation starting, a reaction/roll/popup, a new hostile in view, HP below 35%, any damage outside combat, stepping into a harmful surface (fire, acid, ...), a blocked teleport, a failed step. `ignore="hostile,hp"` turns some off (combat, dialogue, prompt, hostile, hp, damage, surface, teleport).
  - Returns each step's outcome, `NOW` (HP, action, bonus, movement) and `CHANGES` (moves, HP, items, who appeared, mode). `wait=false` queues instead.

## Macros (one call each; they stop at the first interrupt and say what was left)
- `loot_area(radius=12, who, corpses=true, containers=true, opened=false)` → walk to every VISIBLE corpse and unlocked container in range (nearest-first route), open it and take all. Skips locked, owned (stealing), trapped and already-opened ones (`opened=true` includes those) and what it already looted this session. Out of combat.
- `dialogue_advance(max_lines=12)` → click through filler only: a Continue prompt, or one untagged option that commits to nothing. Stops and shows the dialogue at a real choice (2+ options, a [SKILL]/class tag, "attack/give/pay/leave…"), a roll, or the end. Never picks an answer. `talk_to`, `choose_dialogue` and any result where a conversation opened already do this, so you rarely call it.
- `travel_to(marker | objective | waypoint | target | x,y,z, who, wait=false)` → **returns at once**: the trip runs as the first entry of the character's queue, so `queue_actions` appends what to do on arrival (`queue_done`/`queue_interrupted` event at the end; `wait_for("queue")` lists each leg; don't pass `wait=true`). Destination: your marker (`m1` or label words), an objective here (`o1`), a visible id, a point, or a waypoint (= `fast_travel`). Chains walks of ≤30 m along walkable ground, around fire/acid/other harmful surfaces you can see; stops on arrival (≤3 m), when nothing leads further (then `get_frontier`, a door, a jump), after 2 legs without progress, or an interrupt (any damage). Stepping into a surface: it steps out, forward if it can, and goes on. When no clear way leads on it walks around patches, or takes the **shortest crossing** and says what it costs (`crossing 1.5m of Fire (~1-4 dmg + Burning)`), unless HP is too low for the worst case (then heal first). `avoid_hazards`: `auto` (default), `strict` (never cross), `cross` (cross even at low HP; never lava/chasms or >6 m). Out of combat. `fast`: `auto` (default) fast travels first when a trip is ≥120 m and an unlocked waypoint here is much nearer the goal (`fast travel to X first (25m from it; you were 463m away)`); `never` walks. A trip that stops twice at the same blocked spot says `tried 2x: blocked … Don't try this approach again` with another way.
- `regroup(leader, near_m=6, chain=true)` → members farther than `near_m` from the leader walk back, all at once, in travel_to legs (any distance), then everyone is chained to the leader. Downed members and members on another level are listed, not moved.
- Party stays together by default: get_status/look print `PARTY APART: e1 Lae'zel 34m, e3 Gale other level → regroup()` and re-chain a companion that drifted off the group; a companion's own queue re-chains it when it ends; the leader's travel_to waits for (then walks back) a chained member who falls >12 m behind, and stops with `can't keep up` if it can't.
- `stay(who, on=true)` → leave a companion on purpose (plate, door, lookout): no re-chain, no waiting; `stay(who, on=false)` or `regroup` brings it back.
- `read_all(who, max_items=6)` → read every book, note and letter in the bags not read yet (remembered across sessions) and return their text. Out of combat.
- `rest_if_needed(threshold=50, allow_long=false)` → short rest if anyone is downed or ≤ threshold % HP; with `allow_long=true`, camp + long rest when no short rest is left. HP only: judge spell slots yourself.

## Meta
- `save_game(label="")` → named save (`Sxxx-<slug>`); empty = quicksave. Background: returns at once, SAVED / SAVE FAILED on a later result; queued if the game can't save now (conversation). `list_saves(limit=10)`. `load_game(save="latest")` → folder name from `list_saves`.
- `confirm(answer="yes")` → answer an open Yes/No (OK/Cancel…) box: `"yes"`, `"no"`, a button label or its number (`POPUP decision … → answer with confirm`). `long_rest`/`short_rest`/`fast_travel` answer their own "End the day?"-type questions and list them (`long_rest(confirm=false)` stops at the first instead).
- `ui_action(command, widget="", param="")` → run a UI command that `ui_dump` lists (screens with no dedicated tool).
- `press_key(key)` / `click(x, y, button="left")` → last resort (SDL key names; fractions of the last screenshot). Log each use.

## Worked `do_actions` examples

**A combat turn** (after one `get_combat_state` + `get_actions`; ids come from those reads; check the
move with `path_preview` first if it's tight):
```json
[{"tool":"move_to","target":"e12"},
 {"tool":"use_ability","ability":"Target_MainHandAttack","target":"e12"},
 {"tool":"use_ability","ability":"Shout_SecondWind"},
 {"tool":"end_turn"}]
```
If a reaction prompt or a downed target stops it, read the result, then re-plan only what's left.
Conditional variant: `{"tool":"use_ability","ability":"Shout_SecondWind","if":"hp_below:50"}` heals only when hurt.

**A combat turn** is better done with `combat_turn` (fallbacks, re-checks, returns at once with who's next).

**A looting sweep**: `loot_area()` does it in one call. For a hand-picked set, queue it (ids from `get_nearby`); send it with
`queue_actions`, then plan the next leg while it runs; `queue_done` tells you it finished:
```json
[{"tool":"interact","target":"e21"},{"tool":"loot","take":"all"},
 {"tool":"interact","target":"e22"},{"tool":"loot","take":"all"},
 {"tool":"interact","target":"e30"},{"tool":"loot","take":"all"},
 {"tool":"pick_up","item":"e33"}]
```
For picky looting, `interact` alone, then `get_inventory()` to see the container, then `loot(take="<ids>")`.

**A dialogue chain**: `dialogue_advance()` clicks the filler for you. To chain your own picks (only the obvious ones; it stops at a roll or a new choice set):
```json
[{"tool":"choose_dialogue","option":1},
 {"tool":"choose_dialogue","option":1},
 {"tool":"choose_dialogue","option":3}]
```
Then `get_dialogue()` and decide the real choice yourself.

**Exploring at human pace** (no idle gaps): `queue_actions([{"tool":"walk_toward","objective":"o1"},
{"tool":"interact","target":"e40","if":"target_visible:e40"}])`, then right away `look` / `get_frontier`
and plan what's next. When `queue_done` or `job_finished` shows up, the next plan is already ready.
