# bg3 tools cheat-sheet (56 tools, called as `mcp__bg3__<name>`)

Targets (`target`, `npc`, `item`, `who`) are short ids from the observation tools (`e12`) or a party
member's name. Ids last one session only: write names in notes. `who` defaults to the active
character. Job results are `done`, `FAILED — reason`, `INTERRUPTED — reason`, `TIMEOUT`, or `still
running` (→ `wait`), followed by EVENTS.

## Observe
- `get_status()` → where you are, party HP/conditions, mode, what's on screen, recent events. Start here.
- `get_party()` → each member's HP, AC, level, conditions, resources (slots, actions), concentration.
- `get_character(who, sections="")` → full sheet (party) or Examine view (others); e.g. `sections="resources,equipment"`.
- `get_actions(who)` → usable actions/spells/items with costs and availability (ability ids for `use_ability`).
- `get_nearby(radius=25, kinds="")` → visible characters and objects with ids, distance, relation, locked/opened.
- `get_frontier()` → leads to explore: doors, ladders, stairs, exits and unvisited things, with direction.
- `get_explored()` → your own breadcrumbs in this area (explored extent, recent named places).
- `path_preview(target | x,y,z, who)` → walking distance, reachable?, needs a jump?, fits remaining movement?
- `look_at(target)` → the Examine panel for a visible character or object.
- `get_inventory(who, filter="")` → equipment, notable items, junk summary, and the OPEN container's contents; filter: equipped | consumables | all | words.
- `get_journal(quest="")` → active quests with latest entry; `quest="<words>"` → all entries of one quest.
- `get_map()` → region, unlocked waypoints, your markers.
- `get_events()` → everything since your last call.
- `wiki_search(query, limit=5)` / `wiki_page(title, section="", max_chars=5000)` → bg3.wiki; tag it `[wiki]`.
- `screenshot(scale=0.5)` / `zoom(x, y, w, h)` → pixels: only when text can't answer (zoom crops the last screenshot; fractions 0-1).
- `ui_dump(text_only=true)` → text and buttons of the current UI screen; cheaper than a screenshot.
- `session_clock()` → time left in the session.

## Act (exploration)
- `move_to(target | x,y,z, who, run=true)` → walk there; stops early on combat or dialogue.
- `interact(target, who)` → door, lever, chest, corpse (opens loot), book, ladder, waypoint; walks there first.
- `pick_up(item, who)` → take an item lying on the ground.
- `loot(take="all", who)` → take from the open container or corpse: "all" or comma-separated ids.
- `jump_to(x, y, z, who)` → jump (costs movement plus a bonus action in combat).
- `set_party(action, who, leader)` → select | chain | unchain | sneak_on | sneak_off | turn_based_on | turn_based_off.
- `fast_travel(waypoint)` → to an unlocked waypoint (name or id); not in combat.
- `camp(action="go")` → go | leave camp.
- `place_marker(label, target | x,y,z)` / `delete_marker(marker_id)` → your own persistent map markers.
- `wait(seconds=3)` → let game time pass; continue a `still running` action.

## Combat
- `get_combat_state()` → turn order (visible), whose turn, round, the active character's remaining economy.
- `use_ability(ability, target | x,y,z, who, upcast=0, targets="")` → attack, spell, item or action from `get_actions`; `targets` = comma list for multi-target spells.
- `ability_preview(ability, target, who)` → hit chance or save DC, damage, range, line of sight.
- `react(use, which="")` → answer a reaction prompt (Shield, Counterspell, opportunity attack).
- `end_turn(who)` → end the turn; blocks through enemy turns until a party member can act or combat ends.
- `end_combat_report(what_worked, what_to_change, next_goal)` → required after every fight; goes to your session log.

## Dialogue
- `talk_to(npc, who)` → walk up and start a conversation (`who` = the speaker).
- `get_dialogue()` → current line and numbered options with check tags, or the roll, reaction or popup on screen.
- `choose_dialogue(option)` → pick answer N; returns the next line and options.
- `roll_check(boosts="", reroll_on_fail=false)` → resolve the roll on screen (boosts as offered, e.g. "Guidance").
- Cutscenes are auto-skipped; their lines arrive as `dialogue_line` events. Answers are never auto-picked.

## Items
- `manage_inventory(op, item, who, to)` → op: equip | unequip | use (drink/read/eat) | give (`to`=name) | camp (to the camp chest).
- `trade(npc="", offer="", request="", confirm=false)` → `npc` opens the barter; offer and request are comma lists of ids; `confirm=true` closes the deal.

## Progression
- `short_rest()` → 2 per long rest. `long_rest()` → at camp (`camp("go")` first); handle camp scenes as dialogue.
- `get_level_up_options(who)` → what the level-up screen offers.
- `level_up(who, plan)` → plan JSON, e.g. `{"feat":"Ability Improvement","abilities":{"Wisdom":2},"spells":["Bless"]}`; omitted fields take the defaults.
- `new_game(difficulty="Explorer")` → from the main menu: Explorer | Balanced | Tactician, then character creation.
- `character_creation_options()` → what the creation screen offers now.
- `create_character(plan)` → JSON, e.g. `{"race":"Half-Orc","class":"Paladin","subclass":"Oath of Devotion","background":"Soldier","abilities":"recommended","name":"Claude"}` or `{"origin":"Lae'zel"}`.

## Meta
- `do_actions(actions)` → JSON list of steps `{"tool": name, ...args}` run in order in ONE call; stops at the first failure or interrupt. Allowed: move_to, interact, pick_up, loot, talk_to, jump_to, set_party, use_ability, end_turn, choose_dialogue, roll_check, react, manage_inventory, wait, press_key, place_marker, camp, short_rest.
- `save_game(label="")` → named save (`Sxxx-<slug>`); empty = quicksave. `list_saves(limit=10)`. `load_game(save="latest")` → folder name from `list_saves`.
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

**A looting sweep** (after a fight; corpse and chest ids from `get_nearby`):
```json
[{"tool":"interact","target":"e21"},{"tool":"loot","take":"all"},
 {"tool":"interact","target":"e22"},{"tool":"loot","take":"all"},
 {"tool":"interact","target":"e30"},{"tool":"loot","take":"all"},
 {"tool":"pick_up","item":"e33"}]
```
For picky looting, `interact` alone, then `get_inventory()` to see the container, then `loot(take="<ids>")`.

**A dialogue chain** (only the obvious picks; it stops at a roll or a new choice set):
```json
[{"tool":"choose_dialogue","option":1},
 {"tool":"choose_dialogue","option":1},
 {"tool":"choose_dialogue","option":3}]
```
Then `get_dialogue()` and decide the real choice yourself.
