# Exploration

**When:** out of combat and dialogue: where to go, moving, looting, locks/traps, camp and rests, inventory, trading.

## Pick where to go
1. Current objective (objectives.md Main first; a Side objective if it's in this region). The game's own
   quest markers appear as `OBJECTIVE o1 <label> 16m W` lines in `get_status`/`get_map`.
2. `get_frontier()` lists the leads you can see: doors, ladders, stairs, exits and unvisited things, with
   distance and direction. It's the main tool for "where next?". `get_explored()` shows where the party
   has already been in this area.
3. `get_journal()` for what the game says to do (`get_journal(quest="<words>")` shows a quest's full
   history). `get_map()` lists waypoints and your own markers.
4. In-region leads from locations.md ("Unexplored: …").
5. A puzzle (paintings, statues, plates, a door with no handle, a password)? First what you have SEEN:
   `recall("<words>")` (every book/note/letter you opened + the journal: the order, the password, the hint
   is usually there) and `get_nearby(kinds="props")` (the puzzle pieces' names and visible states, e.g.
   `[lit]`). Only then the wiki.
6. Need the wiki (a quest, a puzzle, an item)? Start a walk or queue first (toward the likeliest lead),
   then `wiki_search` → `wiki_page(title, section="<the part you need>")` while it runs (a whole page is cut
   at 2500 chars and lists its sections); never stand still to read.
Don't try to uncover every corner. Once `get_frontier` shows nothing new that matters, move on and mark
the region done in locations.md.

## Moving
- `move_to(target="e14")` or `move_to(x=…, y=…, z=…)` uses the game's own pathfinding; the chained party
  follows. `path_preview(target=…)` first, if you're unsure it's reachable (it also says whether a
  jump is needed). No lead in view but an `OBJECTIVE` line points somewhere? `walk_toward(objective="o1")`
  (or `direction="W"`) walks as far that way as the ground allows; then look again. Results:
  - `done`: arrived.
  - `INTERRUPTED — combat …` → `combat` playbook. `INTERRUPTED — dialogue …` → `dialogue` playbook.
  - `INTERRUPTED — <other>` (something spotted, a trap revealed): `get_nearby` to see what appeared.
  - `FAILED — <reason>` / `TIMEOUT`: check `get_nearby` for the obstacle (closed door, gap). Try
    `interact` on the door, `jump_to(x, y, z)`, another point, or another route. One `screenshot` only
    if the text doesn't explain the block.
  - `started jN` / `still running`: keep going (plan, note, look); `wait_for("jN")` only when out of work.
  - The same `used <item>` event repeating, or a character ignoring orders: a harness loop → the
    `stuck` playbook, section 0.
- Long distances: `travel_to(marker="m1" | objective="o1")` chains walks there and returns at once;
  append the arrival steps with `queue_actions` and plan meanwhile (it stops on an interrupt or when
  nothing leads further); `fast_travel(waypoint="<name>")` between unlocked
  waypoints (never in combat). `regroup()` brings stragglers back.
- A climb or a door on the way: one call, `queue_actions('[{"tool":"interact","target":"e57"},{"tool":"travel_to","objective":"o2"}]')`
  (a REFUSED travel_to names the ladder), not interact, wait, then travel_to (naut2: ~4 s of standing per climb).
- An arrival result already holds CHANGES, LEADS and, for an objective trip, where that objective is now
  ("o1 … moved on: now 67m W"): act on it; a `get_map`/`get_status` right after only answers "No change".
- Sneaking: `set_party(action="sneak_on")` near enemies you want to avoid or ambush, `sneak_off` after.
- Keep the party chained (`set_party(action="chain", who=…, leader=…)`) except when positioning on
  purpose.

## Interacting and looting
- `interact(target)`: doors, levers, books, ladders, waypoints, containers and corpses (opens the loot
  window). `pick_up(item)` for things lying on the ground. `look_at(target)` shows the Examine panel.
- **Read books, letters and notes**: they hold passwords, clues and quest starts. Write key facts into
  npcs, locations or quests, tagged `[seen]`.
- After `interact` opens a container: `get_inventory()` lists its contents. Then `loot(take="all")`, or
  `loot(take="<id>,<id>")` for picks. Take gold, potions, scrolls, arrows, throwables, camp supplies,
  keys, books and letters, and gear better than what's equipped. Skip junk in a hurry.
- **Loot a whole area** with `loot_area()` (visible bodies and unlocked, unowned containers nearby,
  nearest-first, take all). For a hand-picked sweep, one `queue_actions`/`do_actions` call:
  interact corpse 1 → loot → interact chest → loot → pick_up the item on the floor.
- `read_all()` reads every new book, note and letter in your bags and returns their text.
- Locked: try a found key first, then lockpicking (the best Sleight of Hand character via `who=`), then
  another entrance. Log failures as dead ends. Lockpicking needs Thieves' Tools in that character's own
  bags: `lockpick` says at once who carries them, or that nobody does (then force it, Knock, another way in,
  or buy tools from a trader). A right-click menu or a screenshot won't get past a missing tool.
- **Traps** that `get_nearby` or `get_frontier` shows (revealed by a Perception success): walk around
  them, or disarm with a trap disarm kit and the best Sleight of Hand character. Never step on one.
- Hidden things are only revealed by the game's own Perception rolls. Keep a high-Perception member at
  the front. Don't conclude something is "not there" until a perceptive character has stood close and
  `get_nearby` still doesn't list it.
- A place worth returning to → `place_marker(label="…", target="e14")`, plus a locations.md line.

## Resting and camp
- `short_rest()` (2 per long rest) when someone is hurt or out of short-rest features.
- **Long rest:** `camp(action="go")`, then `long_rest()`. It uses camp supplies, restores everything,
  and advances some story timers `[wiki]`. First check objectives for urgent items (someone in danger,
  "before nightfall" wording) and don't rest while one is pending. Afterwards, handle camp scenes as
  dialogues (`get_dialogue`), then `camp(action="leave")`.
- **Camp supplies:** a full long rest needs 40 (fewer = a partial rest: half HP and slots; S025-S026 lost two
  rests and ~10 min hunting food). `get_inventory(filter="food")` shows supply per item.
- At camp, talk to companions (`talk_to`) to change the active party or hear their stories. Use the camp
  chest and camp traders. For screens without a dedicated tool, `ui_dump` then `ui_action`.

## Resources and economy (S025-S026: two partial rests, 34/88 HP into a puzzle, 93 g left, junk-full bags)
- **HP:** below 50 % out of combat, `short_rest` before any fight or puzzle (2 per long rest; the ALERTS
  line says how many are left). No short rest left → long rest (with 40+ supplies) or potions.
- **Supplies:** keep 80+ so every rest is full (`long_rest` refuses a partial one unless `partial=true`). Pick
  up food in passing (bread, apples, wine…: free supplies); buy it (`trade` wares marked `supply=N`) below 80.
- **Gold:** keep a 200 g reserve. Before paying a big sum (a key, a bribe), try the dialogue's Persuasion/
  Intimidation/Deception option when the odds are good, or pickpocket/steal it if the oath allows and
  nobody watches (fair game; save first). Paying 1000 of 1093 g (S026) left nothing for food.
- **Sell junk at every trader:** `trade(sell="junk")` right after the trade screen opens (one call);
  Encumbered with no trader near → `manage_inventory(op="camp", item="junk")`.
- **Gear:** at session start and after loot, `equip_best()` (the gear check from the last session is in
  the start context: snapshots/gear.md). Never leave a rare shield/armour in a bag.

## Inventory and trading (only when it matters)
- `get_inventory(filter="equipped" | "consumables" | "<words>")` keeps reads small.
- Equip upgrades right away: `manage_inventory(op="equip", item="e31", who="Lae'zel")`. Move items with
  `op="give", to="<name>"`, store with `op="camp"`, drink, read or eat with `op="use"`.
- Selling and buying: `trade(npc="e8")` opens the barter. `get_inventory()` lists both sides; then
  `trade(offer="<ids>", request="<ids>")` and `trade(confirm=true)`.
- Note keepers and their purpose in inventory-notes.md.

## Transitions
- **Before leaving a region for good**, especially when the game warns about a point of no return:
  `save_game(label="Sxxx-pre-<slug>")`, re-read objectives.md and locations.md for unfinished items
  there, and decide explicitly. Make it a decisions.md entry if you skip something.
- On entering a new region, add a locations.md section with the waypoint name once discovered.
