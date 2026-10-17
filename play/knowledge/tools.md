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
- `get_party()` → each member's HP, AC, level, conditions, resources (slots, actions), concentration, and each caster's `prepared n/max: …` spells.
  `get_party(camp=true)` → the companions waiting at camp: class, level, pending level up, best skills, weapons/armour.
- `get_character(who, sections="")` → full sheet (party) or Examine view (others); e.g. `sections="resources,equipment"`.
- `get_actions(who)` → usable actions/spells/items with costs and availability (ability ids for `use_ability`).
- `get_nearby(radius=25, kinds="")` → visible characters and objects with ids, distance, relation, locked/opened.
- `get_frontier()` → leads to explore: doors, ladders, `<exit>`s (hatches, trapdoors, stairs, gates, passages: listed even where you have walked) and unvisited things, with direction; a `WAYS walkable: N 20m+, NE 20m+ (6m up), E 8m | blocked: W SW` line (how far the ground is walkable straight out in each direction; `blocked` = not even 8 m: a wall, cliff or water). Use it for "which way out / where's the hatch" instead of a screenshot. Plus `HAZARD Fire 3m W (2x4m)` lines for harmful patches in view (nearest edge, direction, E-W x N-S size). `look` shows the same lines when the patches in view change.
- `get_explored()` → your own breadcrumbs in this area (explored extent, recent named places).
- `path_preview(target | x,y,z, who)` → walking distance, reachable?, needs a jump?, fits remaining movement?
- `route(objective | target | x,y,z, who, connectors=false, go=false)` → (`go=true`: check and go in one call: it starts travel_to the goal when a path exists, else to the closest walkable spot) the way to a goal as text, instead of a screenshot: distance and height (`9.2m above you`), `FLOORS` here and at the goal, the walk in legs (`1. 8m NW`, `2. CLIMB 9.2m up to (x,y,z)`, `3. 6m E`) with fire on it, or `NO WALKABLE PATH` + the `CLOSEST walkable spot` + `LADDERS/DOORS in view` ranked by how much they help (`climbs 9.4m up [interact to climb]`), and one `NEXT:` step. Far goals (80–400 m) get the engine's whole route too, or `NO WALKABLE PATH from here` when this side has none; then also `CLOSEST YOU HAVE BEEN`, `WAYPOINTS (unlocked, nearest the goal first)`, and `BLOCKED BEFORE (this session)` (spots where your trips toward it stopped; a ladder whose top is one says `LEADS TO A DEAD END`). After two stops at one spot, `NEXT:` says the side is closed: come from another side (a waypoint, the roads, the wiki), don't climb back up. `get_frontier` ladders (also vines/roots) say `climbs 9m up to (x,y,z) (unexplored)`: where the climb ends; leads on another floor say `(9m up)`. The WAYS line ends with `| jump: E gap 3.2m from (x,y,z) to (x,y,z) (1m down)` where the walk stops at a gap a jump crosses: walk to the first point, `jump_to` the second.
- `look_at(target)` → the Examine panel for a visible character or object.
- `get_inventory(who, filter="")` → equipment, notable items, junk summary, the OPEN container's contents, `CAMP SUPPLIES n/40 carried`, `BETTER GEAR IN BAGS` and `JUNK n items ~Xg`; filter: equipped | consumables | food (supply=N per item) | junk (what sell=junk sells) | gear (upgrades) | all | words.
- **ALERTS** line (get_status, and look/get_status when it changes): low HP out of combat with the fix (`short rests left N: short_rest`, or `long rest needs 40 supplies, have X`), camp supplies < 40, gold < 200, Encumbered, better gear in bags (`equip_best`), last long rest PARTIAL. Act on it before the next fight or puzzle.
- `get_journal(quest="")` → active quests with latest entry; `quest="<words>"` → all entries of one quest.
- `get_layout(radius=12, who, to="")` → the walls, openings and floor around you as a text map, N up, you = `@` (what the overhead camera shows): mazes, rooms, ledges, chasms. Marks the party (`1`-`3`), characters (`E` hostile, `n` other), doors `D`, ladders `H`, levers/buttons `L`, containers `C`. radius 4-20 m. In a trial room with a floor mosaic it also prints the mosaic and, standing at its start, the `MOSAIC PATH` along its lines to the far end (`to="x,z"` or an id: the line nearest that instead) as one `do_actions` of straight `move_to` legs. Use it for "where are the walls / which way through this room" instead of a screenshot.
- `get_map(full=false)` → (after the first call: only new waypoints/objectives, unless you changed area) region, unlocked waypoints, all active quest objectives (`OBJECTIVE o1 <label> 16m W [quest]`, or "(another area)"), your markers. `get_status()` shows the objectives in this area (max 3).
- `get_events()` → never blocks: everything since your last call, plus `RUNNING` jobs/queues (what's still walking, what's queued).
- `wiki_search(query, limit=5)` / `wiki_page(title, section="", max_chars=2500)` → bg3.wiki; tag it `[wiki]`. A whole page is cut at max_chars and lists its sections: ask for the one you need with `section=`. For a puzzle, `recall(query)` (what you read) and `get_nearby(kinds="props")` come first.
- `screenshot(scale=0.5)` / `zoom(x, y, w, h)` → pixels: only when text can't answer (zoom crops the last screenshot; fractions 0-1).
- `ui_dump(text_only=true)` → text and buttons of the current UI screen; cheaper than a screenshot.
- `session_clock()` → time left in the session.

## Act (exploration)
- `move_to(target | x,y,z, who, run=true, wait=false)` → walk there (next to an entity; refused if there's no walkable path). **Returns at once** with a job id + ETA (a fast failure comes back at once too). Don't pass `wait=true`: plan or take notes during the walk, then `wait_for` if you must. Stops early on combat or dialogue.
- `walk_toward(direction | objective | target, max_m=30, who, run=true, wait=false)` → async like `move_to`; walk as far as the ground allows (direct path, stopping short of harmful surfaces) toward N/NE/…/NW, an objective (`o1` or words from its name; this area only) or a visible id. For "head toward the quest marker" when no door or item leads there. A winding walk (furniture, switchbacks) counts and says "winding"; a dead direction names the directions that do walk ("Walkable instead: SW 18m …").
- `interact(target, who, wait=true)` → door, lever, chest, corpse (opens loot), book, ladder, waypoint; walks there first.
- `pick_up(item, who, wait=true)` → take an item lying on the ground (walks there first).
- `loot_corpse(corpse, who)` → walk to a visible dead body and open its loot window; then `loot` takes from it.
- `loot(take="all", who)` → take from the open container or corpse: "all" or comma-separated ids.
- `jump_to(x, y, z, who, force=false)` → jump (costs movement plus a bonus action in combat; max distance ≈ Strength/2 m, but jumps over ~4.5 m have landed short live). A landing in fire/acid/… or on a wall (or a long jump that may fall short into one) is moved to the nearest clear spot in reach, and the result says so; with none near, it is refused. A refusal (blocked landing, too far, no floor at that height) carries the way that works when there is one: "It is walkable: move_to …" or "move_to <stand> (walk Nm), then jump_to <landing>": do exactly that. `force=true` jumps exactly there.
- `set_party(action, who, leader)` → select | chain | unchain | sneak_on | sneak_off | turn_based_on | turn_based_off.
- `fast_travel(waypoint)` → to an unlocked waypoint (name or id); not in combat. From camp it leaves camp first; if a long rest's night is still running (night at camp blocks all travel) it goes to sleep first and says so.
- `feed_gale(item)` → in the donation window Gale's dialogue opens ("Give Gale a magic artefact"; screen=donate): picks the magic item and presses Donate. item: name or handle from get_inventory.
- `insert_item(item=None, into=None, who=None)` → an "Insert Item" panel (pedestal, plaque, socket; screen=insert): puts the item in the slot (what a drag does) and presses Insert. `into=<object>` opens its panel first (interact). Without item it lists the slot and the items the panel can take (the opener's bag): `INSERT panel Riddle of the Night: slot(s) empty … items it can take: e53 Teachings of Loss: …`. The opener defaults to the item's carrier. Story pedestals can punish a wrong item (a trap): choose by the riddle. Result `inserted … (panel closed)`, or `FAILED — the panel refused … (reason)`. No hand drags or clicks.
- `camp(action="go")` → go | leave camp.
- `swap_party(into="Halsin", out="Gale")` → one call from anywhere out of combat: camp, the companion's join
  answer (and the full-party answer that sends `out` to camp), leave camp, everyone chained. `out` is needed
  with 4 members (never your own character). A camp scene or an unknown answer stops it with the dialogue
  shown: answer, then call it again. The newcomer's pending level up shows as `LEVEL UP` right after.
  get_status/look print a `PARTY:` line when a swap pays off (a camp companion tied to this region or an
  active quest, a missing role or skill) — the line is the harness's suggestion, the choice is yours.
- `lockpick(target, who)` / `disarm(trap, who)` → pick a visible lock (Thieves' Tools) / disarm a DISCOVERED trap (Trap Disarm Toolkit): opens the roll, then `roll_check`. Your best Sleight of Hand member (`who`) gets the best odds.
- `set_reactions(who, settings="")` → list or change a member's reaction prompts (`{"Shield": "always", "Opportunity Attack": "ask"}`).
- `dismiss_popups()` → close info popups (tutorials, reward/book panels); decision boxes are never closed (answer them with `confirm`).
- `click_on(target, button="left")` → last resort before `click`: verified-hover click on a visible entity no other tool covers. Log each use.
- `place_marker(label, target | x,y,z)` / `delete_marker(marker_id)` → your own persistent map markers.
- `wait(seconds=3)` → let game time pass. Prefer `wait_for`, which returns as soon as the thing happens.
- Never `wait_for("combat_started")` (or dialogue_started) to ask "did that start a fight?": the action's own
  result already says so (mode line, CHANGES, EVENTS), and with nothing running the answer is "nothing is running
  that would cause combat_started" (24 wasted round trips S023-S026). Just make your next move.

## Combat
- `get_combat_state()` → turn order (visible), whose turn, round, the active character's remaining economy.
- `use_ability(ability, target | x,y,z, who, upcast=0, targets="")` → attack, spell, item or action from `get_actions`; `targets` = comma list for multi-target spells. `ability` = the name as the action list shows it (`"Fire Bolt"`, any case) or an id; lists show an id only where two actions share a name (then pass that id). Out of combat at a creature it says whether the hit started a fight: `COMBAT: the attack started a fight …` or `combat: NOT started — e24 Shadow took 3 damage but no fight began` (dormant/unaware: it won't act until combat starts). A scroll you carry works by name too (`"Scroll of Invisibility"`): out of combat the tool reads it and clicks the target (default: `who`); never screenshot + click_on for it.
- `ability_preview(ability, target, who)` → hit chance or save DC, damage, range, line of sight.
- `react(use, which="")` → answer a reaction prompt (Shield, Counterspell, opportunity attack).
- `end_turn(who, wait=true)` → end the turn; blocks through enemy turns until a party member can act, combat ends, or a reaction/roll prompt needs you (`wait=false` + `wait_for("my_turn")` to plan meanwhile).
- `combat_turn(plan, who, end=true, wait_next=false, ignore="hostile", wait_turn=30)` → **a whole turn in one call** (see below). Returns at once after ending the turn with `NEXT …, then <ally> (yours)`, or `NOW <ally>'s turn` plus their actions: plan that turn while the enemies act and **send it at once**: a plan sent before the turn comes waits for it (up to `wait_turn` s; stops on a prompt), so no `wait_for("my_turn")` is needed. Party members with equal initiative share one turn (any order): plan them all in one call with a list, `plan='[{"who":"Karlach","plan":{…}},{"who":"Gale","plan":{…}}]'` (a later member's plan waits for their turn like a sent-ahead plan, ≤80 s in all; it stops and lists who was NOT played when a turn is interrupted or a turn doesn't come in time). The action that starts a fight already shows `COMBAT STARTED` with the turn order and your actions: no `get_combat_state` after it.
- `end_combat_report(what_worked, what_to_change, next_goal)` → required after every fight; goes to your session log.
- **Fight debrief (automatic):** the result that sees a fight end carries `FIGHT DEBRIEF (<clean|costly|near-wipe|wipe>, N rounds) vs …`: enemies and what they used on you, damage dealt/taken per member, who went down to whom, your abilities' hits/misses/targets, damage types that did little, surrounded/high ground, potions/scrolls used and carried unused. It is also logged (`[fight]`) and kept in memory/fights.jsonl. A `load_game` during a fight (or ≤10 min after a bad one) adds `LAST ATTEMPT: … — plan a different approach: …` (also on the next get_status); the retried fight's first combat state / combat_turn repeats the key facts once (`RETRY of the fight …`), and a fight vs enemies a past debrief knows opens with `LAST TIME vs …`. `WARNING: …` lines flag a repeated pattern (the same member down first again, a damage type that did little last time too). After a bad fight or reload: read it, change the plan, note one lesson (`note(tag="lesson")`).

### `combat_turn` plans
JSON with slots run in `order` (default `move,action,bonus,extra`); each slot is a list of alternatives
tried in order until one works:
- `move`: `"e12"` (walk next to it), `{"target":"e12","within":18}` (stop once within 18 m: ranged/spell
  range; no walk if already there), or `[x,y,z]`. If the walk doesn't fit the movement left, it walks as far
  as it does (`"partial":false` turns that off). A failed move doesn't end the turn: the fallbacks decide.
  It follows the real route (stairs, detours) and keeps out of fire on the way (`"avoid":false` walks through).
  A move that ends out of reach says so and why: `now 9.9m from e12 — stopped short of ≤1.5m: out of
  movement (9m left, the route needs 18.5m)` / `kept out of Fire on the route 4m ahead` / `no walkable
  route gets closer than 7m (raised/blocked …)`. A melee move that ends clearly out of reach adds
  `— melee is out of reach this turn` and the melee attacks on that target are skipped (no wasted try): the
  plan's ranged alternative runs, else the ranged weapon attack itself (`action (auto fallback): …`), else
  Dash + walk on (out of movement only). A route that needs ≈ all your movement often ends ~1.5 m short:
  plan a ranged alternative or a Dash for it.
- **Lost move target:** the move's target died or went out of sight (Shadow Blend, killed by an ally first):
  `target: e24 Shadow is out of sight → the plan now aims at the nearest visible hostile e70 Wraith (12m)`,
  and it walks on if movement is left. Nobody in sight: its attacks and a walk-on Dash are skipped. Only a
  hostile move target is re-aimed: a move to an ally (a dead one you revive) stays on that ally.
- **Retarget:** when every alternative of a slot missed its target (dead, out of range, no line of sight),
  the plan's own targeted abilities are tried on the nearest other visible hostile in reach
  (`action (auto fallback): Target_SacredFlame e15: done`; also for `extra` after a kill). `"retarget":false` turns it off.
  Fallbacks keep to the planned target's side: an ability aimed at an ally is never re-aimed at a hostile (a heal
  may go to another downed party member in reach), the ranged-attack fallback only hits a hostile target, and a
  revive (Revivify, its scroll) is never re-aimed; a dead target is no miss for a revive.
- **Melee refused without a reason** (`the game refused the cast (no reason given): a melee swing from 2.1 m …`):
  with movement left it steps within 1 m of the target once and swings again (`stepped closer for the swing`).
- **`who`:** required when `NOW` lists more than one of your characters (a shared turn). Left out there, the
  plan goes to the only one acting whose actions cover every ability it names, else it is refused (`give
  who=`). A plan sent ahead without `who` waits for the character selected when it was sent (or the only one
  of yours with its abilities), never for whichever party turn comes first.
- **Downed ally:** no `bonus` in the plan and an ally at 0 HP within range: Healing Word on them if this
  character has it (`bonus (auto: ally downed): …`). `"auto_bonus":false` turns it off.
- `action`, `bonus`: an ability name as listed (`"Dash"`) or id (`"Shout_Dash"`), or `{"ability","target" | "x","y","z","upcast","targets","if","unless","then_move"}`,
  or `{"tool":<any do_actions tool>,...}`. `"then_move":true` walks the move again after it; a Dash does that by itself (`"then_move":false` to just Dash).
- `extra`: a list of more slots, e.g. the Extra Attack: `[[{"ability":"Target_MainHandAttack","target":"e12"}]]`.
  After the first weapon attack (a Main Hand Attack, Divine Smite, Reckless Attack) the next one costs no action,
  as in the game; get_actions shows it as `[Extra Attack: free]`. Weapon actions (Lacerate, Cleave) and spells don't.
- Summons (Spiritual Weapon) attack with their own action, e.g. "Greataxe Slash" (a bonus action). A plain
  `Target_MainHandAttack` in a summon's plan is mapped to it.
- `reactions`: `{"Divine Smite":"always"}` sets reaction prompts before the turn. `on_reaction`: `"use"` or
  `"skip"` answers reaction prompts that pop up mid-turn, and the ones that come while a sent-ahead plan
  waits for its turn (an enemy's attack → Shield Blow). Default: stop, name the prompt, and let you answer.
- `targets` with 3+ names (Bless): the cast takes 2, so it goes on the first two and says who was left out.
  Spells with no higher-level form here (Guiding Bolt) ignore `upcast` and say so.
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
  get_status/look say `TALK: e5 Wyll wants to talk (the '!' over them)` when a companion (in the party, or at camp while you are there) or a nearby NPC shows the game's "!" (rows: `wants to talk(!)`): talk_to them; no screenshot needed.
- `pickpocket(npc, who, take="", min_chance=0)` → sneak up and open the pickpocket window; the result lists their pockets with the game's own odds: `POCKETS …: e40 Gold x12 81% DC8 | e41 Dagger 2g 54% DC12 …` (best odds first). `take="<name or id>"` steals it (also in the first call), `min_chance=60` takes nothing below 60 %; the result says `stole …` or `CAUGHT? … failed` with the roll vs DC. A failed steal is a crime if they notice: sneak, stay unseen, save first. `who=` steals with that member (it takes control of them first; the odds are theirs); `odds ?` = the game hadn't shown that item's odds yet (list again). `pickpocket(npc, leave=true)` closes the window.
- `get_dialogue()` → current line and numbered options with check tags. A tagged option shows the check and your speaker's bonus (`← Persuasion check: e2 +5 (party best e1 +6 …)`). Or the roll, reaction or popup on screen.
- `choose_dialogue(option)` → pick answer N; clicks through the filler after it and returns the next real choice, roll or the end.
- `roll_check(boosts="", reroll_on_fail=false)` → resolve the roll on screen (boosts as offered, e.g. "Guidance").
- Cutscenes are auto-skipped; their lines arrive as `dialogue_line` events. Answers are never auto-picked.

## Items
- `manage_inventory(op, item, who, to)` → op: equip | unequip | use (drink/read/eat) | give (`to`=name) | camp (to the camp chest; `item="junk"` sends all junk at once, fixes Encumbered).
- `equip_best(who="")` → equips every better item sitting in party bags (AC, weapon damage/enchantment, rarity; proficiency-aware; moves it from another member's bag first). Out of combat. The result ends with `MAIN HANDS` (each member's weapon with its type/damage, e.g. `Syringe (+1 dagger 1d4+1 uncommon)`: odd names are real weapons). `get_inventory(filter="gear")` previews.
- `trade(npc="", offer="", request="", confirm=false, sell="", keep="", full=false)` → `npc` opens the barter itself (it talks to them and picks the trade answer, e.g. 'Let me see your wares.'; when the conversation offers none yet it stops there for you to choose); offer and request are comma lists of ids; `confirm=true` closes the deal (it balances with the trader's gold first and refuses a deal that would donate your items for nothing). Opening / `trade()` lists the wares grouped (`food` with supply and a total, `potions/scrolls/ammo`, `gear` best rarity first, `other`; 10 per group, `full=true` for all) and your best-priced items; offer/request/confirm show only the `TABLE` and the balance. Wares marked `supply=N` are food. `sell="junk"` sells all junk in one call (never equipped/story/consumables/food/keys/tools/magic gear; `keep="e12,e15"` spares items), up to the trader's gold.

## Progression
- `short_rest()` → 2 per long rest (party-wide); restores HP and short-rest features. Result: `HP Claude 34→62/88, … · short rests left 1/2`.
- `rest_if_needed(threshold=50, allow_long=false)` → short rest only if someone is downed or ≤ threshold % HP (see below).
- `long_rest(confirm=true, partial=false)` → at camp (`camp("go")` first); handle camp scenes as dialogue. With < 40 supplies it STOPS before resting (a partial rest = half HP and slots): get food first, or `partial=true` to rest anyway; the rest screen stays open (`press_key("escape")`). At night camp (a rest whose night didn't end: camp leave/fast travel blocked) `long_rest()` goes to sleep and ends the night; after a night camp scene, call it again.
- `get_level_up_options(who)` → what the level-up screen offers.
- `level_up(who, plan?)` → one call per character level. No plan: uses the line `- <Name> L<n>: {json}` in memory/party-build.md. Plan JSON keys: `subclass`, `feat`, `abilities` (`{"Wisdom":2}`, `{"Wisdom":1,"Constitution":1}` or `["Wisdom","Constitution"]`; half feats 1 point), `spells`, `cantrips`, `skills`, `passives` (also a feat's own choice), `class` (multiclass only). A subclass level fails and lists the choices until `subclass` is given; so do spells/cantrips to learn and passives (invocations, fighting styles): the error says `can pick: …`. A feat level defaults to +2 main ability. Spell and cantrip picks are real clicks the tool does itself (a few seconds); never drive the level-up screen by hand. Several members: one level_up per member; calls in the same message are run one after another (a page the game opens by itself for the next member is closed and reopened properly). get_status/look show `LEVEL UP: <who> (<level>)` while one is pending (out of combat); other results say `LEVEL UP pending` within about a minute of the XP. A NOTES WINDOW may add `PARTY PLAN: no plan line yet for …; party lacks …`: write those in party-build.md while walking.
- `prepare_spells(who, add=[...], remove=[...])` → change a cleric's/paladin's/druid's/wizard's PREPARED spells, out of combat (after a long rest is the usual time): only prepared spells cast, and `get_actions` lists only those. Names or ids, a list or a comma string; removals go first, so a swap fits the limit. It opens the spellbook, clicks the spells and closes it (a few seconds), then ends with `PREPARED <who> n/max: …; always: …` (domain/oath spells are always prepared). Refused in combat, over the limit (it names what is prepared now: remove some in the same call), for a spell not in the character's book, and for sorcerers/warlocks/bards/rangers (their spells change only at `level_up`). `get_party` shows each caster's `prepared n/max` line; `get_status` a `PREPARED` line on a full look and after a change.
- `new_game(difficulty="Explorer")` → from the main menu: Explorer | Balanced | Tactician, then character creation.
- `character_creation_options()` → what the creation screen offers now.
- `create_character(plan)` → JSON with your build, e.g. `{"race":"Elf","subrace":"High Elf","class":"Wizard","subclass":"School of Evocation","background":"Sage","abilities":"recommended","name":"Ilse"}` or `{"origin":"Lae'zel"}`. Applies the plan, types the name on the naming page, presses "Venture forth" and checks the name in the world. It returns within ~95 s: if the world is still loading it says so (the name is set; check get_status later, don't call it again). "still applying the plan" → call it again. `name` (≤20 chars) is required for a custom character unless the playthrough has a save owner (then it must be that name; leaving it out uses it). A naming failure never finishes: fix the cause and call it again. From then on your saves are this character's (`list_saves`/`load_game` work in the same session).

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
- Party stays together by default: get_status/look print `PARTY APART: e1 Lae'zel 34m, e3 Gale other level → regroup()` and re-chain a companion that drifted off the group; a companion's own queue re-chains it when it ends; the leader's travel_to waits for (then walks back) a chained member who falls >12 m behind, and stops with `can't keep up` if it can't. Summons (Spiritual Weapon, familiar, Mage Hand) are party rows tagged `summon of e1`: allies you can act with in combat, never counted apart, re-chained, waited for or regrouped.
- `stay(who, on=true)` → leave a companion on purpose (plate, door, lookout): no re-chain, no waiting; `stay(who, on=false)` or `regroup` brings it back.
- `read_all(who, max_items=6)` → read every book, note and letter in the bags not read yet (remembered across sessions) and return their text. Out of combat.
- `recall(query="")` → search everything the party has READ (book/note/letter panels you opened anywhere, read_all) and the journal steps, by words: `recall("mausoleum painting order")` → `READ <title>: …snippet…` / `JOURNAL <quest>: <step>`. Empty query lists the titles read. Use it at a puzzle before the wiki: it is what you yourself saw (fair, no spoilers); nothing matches → read the books/notes nearby first.
- `rest_if_needed(threshold=50, allow_long=false)` → short rest if anyone is downed or ≤ threshold % HP; with `allow_long=true`, camp + long rest when no short rest is left. HP only: judge spell slots yourself.

## Meta
- `note(text, tag="note")` → appends `- HH:MM [tag] text` line(s) to your session log **at once** (no
  memory_read/memory_edit of the file). One line per line of text; start a line with its own `[quest]`/`[npc]`/`[decision]`… tag.
  `travel_to`, `queue_actions` and `do_actions` take `note="…"` too: the note rides on the action, zero extra
  turns. The memory keeper folds the log into the curated files between sessions; edit those yourself only
  for plan changes you need this session.
- Memory files (your notes; the only file access you have). Paths are `memory/<file>` (read-write) or
  `knowledge/<file>` (read-only reference notes and playbooks); a bare name means `memory/`. Hidden and git files,
  symlinks, `snapshots/` (generated) and `markers.json` (place_marker) are out of bounds; session logs are
  append-only; CHARACTER.md takes only appended development lines once committed.
  - `memory_list(path="memory")` → the files of a folder with line counts.
  - `memory_read(path, start_line=1, max_lines=400)` → a file's text, in pages for long files.
  - `memory_search(query, path="memory", regex=false)` → every matching line as `file:line: text`.
  - `memory_write(path, content)` → create or rewrite a whole `.md` file. `memory_edit(path, old_text, new_text,
    replace_all=false)` → replace exact text (must match once). `memory_append(path, text)` → add lines at the end.
    Each ends with `lint: ok` or the lint errors about that file: fix them.
- `session_start()` → your memory briefing (CHARACTER.md, PLAYTHROUGH.md, HANDOFF.md, last game state). Your
  first message normally has it already; call it if it doesn't, and after a context compaction.
- `end_session()` → the last wrap-up step: checks wrap-up time, HANDOFF.md for this session and lint, then commits
  the memory. `NOT ENDED. …` says what to do first (keep playing, write the handoff, fix lint).
- `save_game(label="")` → named save (`Sxxx-<slug>`); empty = quicksave. Background: returns at once, SAVED / SAVE FAILED on a later result; queued if the game can't save now (conversation). `list_saves(limit=10)` → this playthrough's saves (the character's folders), `<- latest` marks what "latest" loads. `load_game(save="latest")` → folder name from `list_saves`; "latest" = your newest `Sxxx-` save or save_game quicksave, never a `LIVETEST-*`, AutoSave or someone else's QuickSave. Refused (FAILED) when the runner closed the save/load gates for this game (not your character).
- `confirm(answer="yes")` → answer an open Yes/No (OK/Cancel…) box: `"yes"`, `"no"`, a button label or its number (`POPUP decision … → answer with confirm`). `long_rest`/`short_rest`/`fast_travel` answer their own "End the day?"-type questions and list them (`long_rest(confirm=false)` stops at the first instead).
- `ui_action(command, widget="", param="")` → run a UI command that `ui_dump` lists (screens with no dedicated tool); save/load commands (Quick*, LoadGame, Continue, the Save/Load pages) are refused: use `save_game` / `load_game`.
- `press_key(key)` / `click(x, y, button="left")` → last resort (SDL key names; fractions of the last screenshot). Log each use. `press_key("escape")` with nothing open is refused (it would open the game menu); tools close the panels they open. F5 runs `save_game()`; F8 (quickload) is refused: `load_game`.

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
