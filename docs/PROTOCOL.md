# BG3AI mod ↔ bridge protocol

Read this before you touch `mod/` or `bridge/`. See `docs/ARCHITECTURE.md` for the big picture and
`research/` for the reasoning.

## Transport

The bridge talks to the game through the Script Extender's LuaDebug TCP server (bg3le: 127.0.0.1:9998).
`bridge/src/bg3ai/luadebug.py` is the client. Each call evaluates exactly one expression in the
**server** context (Osiris, authoritative ECS; only exists once a save is loaded) or the **client**
context (Noesis UI, input, client ECS; always exists, including at the main menu):

```lua
return Mods.BG3AI.BG3AI.rpc([==[{"op":"state.status","args":{}}]==])
```

The reply is a JSON string, either `{"ok":true,"result":<any>}` or `{"ok":false,"error":"..."}`.
Mods run in their own environment table, so our global is `Mods.BG3AI.BG3AI`, not `_G.BG3AI`.

Ops must return in well under 1 s. bg3le gives up after 10 s, and a slow op stalls the game thread.
Anything that takes game time (walking, a spell resolving, a dialogue line) **starts a job** and returns
`{"job": <id>}`. The bridge then polls `job.poll {id}` until `status ~= "running"`.

`Game.eval` (`bridge/src/bg3ai/game.py`) is the only place the bridge sends Lua. Besides the single call above,
`Game.multi` batches several ops into one eval (`local R=Mods.BG3AI.BG3AI.rpc return '['..R(...)..','..R(...)..']'`).

**Recording and replay.** With `BG3AI_RPC_RECORD=<file>` in the bridge's env, every eval and its answer is appended
as one JSONL line (`bridge/src/bg3ai/rpc_record.py`): `{v, t, ms, ctx, calls:[{op,args}], replies:[...]}`, with
absolute paths scrubbed. The fake mod (`packages/harness/src/bg3harness/testing/fakemod.py`) speaks the same
LuaDebug protocol, decodes both eval shapes with the recorder's own decoder, and answers each op from a scripted game
model or from recorded files (`packages/harness/tests/fixtures/rpc/`); an op nobody answers gets the mod's own
`unknown op` refusal. Point a bridge at it with `BG3LE_DEBUG_PORT=<port>`. Use it for offline tests of any change
to an op the bridge calls.

## Live development

The game is usually running with bg3le and our mod loaded from `mod/` (via `BG3LE_MOD_PATH`, a loose
folder: no `.pak` and no modsettings change). A game the runner (`runner/play.py`) launched loads it from
`runtime/mod-live/` instead: a copy of `mod/` the runner refreshes only at a session boundary, so a merge into main
mid-session never reaches the session's game (S090). Edit `mod/`; the runner copies it at the next session start.

```bash
cd bridge
PYTHONPATH=src python3 -m bg3ai.luadebug            'return 1+1'       # client context
PYTHONPATH=src python3 -m bg3ai.luadebug --server   'return Osi.GetHostCharacter()'
PYTHONPATH=src python3 -m bg3ai.luadebug 'Ext.Debug.Reset() return 1'  # HOT RELOAD both contexts after editing Lua
```

Prototype a snippet in the eval first, then move it into the module and hot reload.

Rules while developing against a real playthrough's save (yours or an agent's):
- **No saving** (quick, auto or named), **no long or short rests**, **no area transitions or fast
  travel**, **no story dialogue choices**, and **no attacks or spells on NPCs**. Reads are always fine.
  Short walks with party members are fine. Opening a door or container is OK if nothing is taken.
- Don't restart or kill a game someone else (or a running agent session) is using.
- Wrap risky Noesis access in `pcall`. bg3le has no `Ext.UI.Defer`, so keep Noesis access read-mostly
  and short.

## Fairness contract (hard rule)

Everything an op returns must be something a human player could see on screen right now, or remember
having seen. See `research/02-visibility.md`, especially §4.3, the never-expose list.
- Server state goes through `Server/Visibility.lua`, then a **whitelisting serializer**. Never return a raw
  component dump.
- Names come from `Osi.GetDisplayName` → `Ext.Loca`, never template or internal names.
- Entity ids: return the **GUID string** (`uuid`). The bridge maps it to short handles (`e12`), and the
  agent never sees GUIDs.
- Events pushed with `RPC.push` must be visible facts (combat log, toasts, journal updates, what the
  party sees), never story flags or hidden rolls.

## Efficiency contract

1. **Engine first.** The agent works from engine data, not pixels. Ops return compact JSON with no nulls
   or empty arrays where avoidable, and positions rounded to 0.1 m. The bridge renders text from it.
   Screenshots exist only as an explicit opt-in tool.
2. **Multi-action planning.** One observation must be enough to plan several actions. State includes
   positions, distances, line of sight, ranges, costs, remaining action economy and options. Actions
   settle on engine events and post-conditions, never fixed sleeps, and return the visible outcome (hit
   or miss and damage, new position, remaining movement and resources). The bridge's `do_actions` runs
   a whole sequence per call and stops only on interrupts.
3. **Latency budget.** An RPC round trip is ~30 ms. `state.status` <20 ms, `state.nearby` <50 ms and
   `ui.state` <30 ms of game-thread time.

## Op catalogue

Context: **S** = server, **C** = client. `uuid` = entity GUID string. `pos` = `[x,y,z]`.

### Core (`Shared/RPC.lua`, done)
| op | ctx | args | result |
|---|---|---|---|
| `sys.ping` | S,C | – | `{context,t,version}` |
| `sys.info` | S,C | – | `{context,version,game_version,game_state,ops[],event_seq}` |
| `events.since` | S,C | `{seq,limit}` | `{latest, events:[{seq,t,kind,data}]}` |
| `job.poll` / `job.cancel` | S,C | `{id}` | `{id,kind,status,result,error,elapsed_ms}` |
| `job.notify` | S,C | `{id}` | same view; a still-running job announces its end as `job_finished` (a waited call that outlived its wait) |

Each module below may span several files: the first file loads the rest at its end with `Ext.Require` (the list is
in [ARCHITECTURE.md](ARCHITECTURE.md), "The mod"). Ops are registered wherever their code lives, so grep for
`RPC.register("<op>"` to find one.

### State (`Server/Visibility.lua` + `VisibilityObjects.lua`, `Server/State.lua` + `StateWorld.lua`, `StateOps.lua`; owner: worker **vis**)
| op | ctx | args | result (sketch) |
|---|---|---|---|
| `state.status` | S | – | `{level, region, subregion, active:uuid, party:[{uuid,name,hp,max_hp,level,classes,pos,statuses,downed,in_combat}], in_combat, turn_based, in_dialogue, gold, combat:{round,current}?}` |
| `state.character` | S | `{uuid, sections?}` | Party: full sheet (abilities, saves, skills, AC, resources incl. spell slots/action/bonus/movement/reaction, passives, conditions, equipment, concentration, XP). Others: the Examine-panel projection only |
| `state.actions` | S | `{uuid}` | Party member's usable actions as the hotbar shows them: `[{spell, name, level?, cost:{action,bonus,movement,slot}, range?, target:"entity"/"point"/"self"/"aoe", available:true/false, reason?}]` |
| `state.nearby` | S | `{radius=25, kinds?}` | Visibility-filtered `{characters:[{uuid,name,relation,pos,dist,hp?,max_hp?,statuses,in_combat,can_talk,can_trade? (not hostile, and the Trader tag, traded with, or its dialogue offered Trade this session)}], items:[{uuid,name,kind(door/container/item/lever/ladder/trap/waypoint/book),pos,dist,locked?,opened?,owned?,trap?}], remembered:[...]}` |
| `state.inventory` | S | `{uuid?, filter?}` | Party inventory: `[{uuid,name,count,type,rarity,equipped_slot?,weight,value,usable?,owner}]`. Also the contents of the container the party currently has open |
| `state.combat` | S | – | `{round, current, order:[{uuid?,name,relation,hp_pct?,is_current,acted}]}` (visible combatants only) |
| `state.journal` | S | – | Journal quests: `[{title, category, steps:[text], done?}]` (unlocked steps only) |
| `state.map` | S | – | `{level, region, subregion, waypoints:[{id,name}]}` (unlocked only) |
| `state.entity` | S | `{uuid}` | Examine-panel projection of one visible entity |

### Events (`Server/Events.lua`; owner: **vis**)
`RPC.push(kind, data)` for visible facts, with kinds such as `combat_started`, `combat_ended`,
`turn_started{uuid}`, `turn_ended`, `round_started`, `damage{source?,target,amount,type}`,
`status_applied/removed` (visible statuses only), `died`, `downed`, `dialogue_started{speakers}`,
`dialogue_ended`, `roll_result{roller,skill,dc?,total,success}`, `quest_update{title}`, `xp`,
`level_up_available{uuid}`, `item_added{owner,name,count}`, `gold_changed`, `level_changed{level}`,
`teleported`, `rest`, `approval{npc,delta}` (as the toast shows), `used{char,item}`, `opened{item}`,
`spotted{uuid}` (a hidden thing revealed to the party), `cast{caster,spell,target?}` for visible
casters, and `moved_done{uuid,move_id}`.

### Navigation (`Server/Navigation.lua` + `NavigationReach.lua`, `NavigationTerrain.lua`, `NavigationRoute.lua`, `NavigationOps.lua`, `NavigationDoors.lua`; owner: **vis**)
| op | ctx | args | result (sketch) |
|---|---|---|---|
| `nav.path` | S | `{char?, pos \| target}` | `{reachable, length_m, nodes, needs_jump?, within_movement?, movement_left, approach_pos?}` or `{reachable:false, straight_m, error_cause}`; points > 80 m → `{estimate:true, straight_m}`; a pos the party hasn't seen or walked (`N.known`) → `{unknown_goal:true, straight_m, dir, dy}` |
| `nav.reach_toward` | S | `{char?, dir:N\|NE\|…\|NW \| pos \| target, max_m=30, step=2}` | Farthest walkable point that way with a direct path (path ≤ max(1.5×straight, straight+2 m)): `{reachable, pos, path_m, straight_m, dir, needs_jump?, samples, ms}`. Target must be visible |
| `nav.approach` | S | `{char?, target \| pos, within=1.5, budget_m?, avoid=true}` | Walk along the engine's real route until within `within` m (straight) of it, capped by budget_m and, in combat, the movement left − 0.2; stops 1.2 m before a harmful surface when avoid: `{reachable, pos, walk_m, need_m?, route_m, end_m, limit: in_range\|movement\|hazard\|route_end, hazard?, movement_left?, ms}` or `{reachable=false, reason}`. Used by combat_turn's move |
| `nav.markers` | S | – | Active quest objective markers (what the map shows): `{markers:[{label, quest, objective, kind, level?, here?, pos?, dist?, dir?}], error?, ms}`; `here` = on the current level with a position; sorted here-first, nearest-first. Data: generated `Server/Data/JournalMarkers.lua` |
| `nav.explored` | S | `{last=10}` | Party breadcrumbs: `{level, cells, cell_m, area_m2, bbox, subregion, recent:[{pos, subregion?, near?, near_m?}]}` |
| `nav.frontier` | S | `{radius=30, limit=25}` | Perceived leads `{leads:[{kind, uuid, name, dist, dir, pos, dy? (another floor, \|dy\|≥1.5), unvisited?, locked?, relation?, waypoint?, height_m?+leads? (ladders: AI-bounds height, up\|down)}], more?, ms}` |
| `nav.route` | S | `{char?, pos \| target, connectors=false}` | The way to a goal (bridge `route` tool, walk_toward failure hint): `{straight_m, dir, dy, unknown_goal? (a pos not seen or walked: nothing else probed), reachable, length_m?, needs_jump?, legs?[{to, at_m, m, dir, dy, link?}], links?[{at_m, from, to, dy, m, kind=climb\|drop\|jump}], exposure?, straight_hazard?, closest?{pos, path_m, to_goal_m, dy_to_goal, via, legs?}, connectors?[{uuid, name, kind=ladder\|door, dist, dir, dy, path_m?\|unreachable?, height_m?, leads?, far_dy?, to_goal_m?, helps?, locked?}], standing_in?, too_far?, door_link?{uuid, name, door, to, door_m, dir, to_goal_m}, ms}`. door_link (NavigationDoors.lua): no walk from here, and a door the party used before took it far toward the goal. Connectors: visible ladders/doors ≤30 m, listed when there is no path (or connectors=true). Target must be visible |
| `nav.set_subregion` | S | `{name}` | Tells breadcrumbs the minimap subregion (the bridge sends it from `ui.state`) |

Directions are 8-point compass from the active character, assuming +X = east, +Z = north (unverified).
The bridge renders markers as `OBJECTIVE o1 <label> 16m W [quest]` (get_status: this area, max 3;
get_map: all) and exposes `walk_toward(direction | objective | target)` = `nav.reach_toward` → `act.move_to{pos}`.

### Dev (never exposed as agent tools)
| op | ctx | args | result |
|---|---|---|---|
| `dev.fairness_probe` | S | `{radius=30}` | Per-rule counts only: characters `{shown, off_stage_or_script_hidden, invisible, sneaking, not_perceived}`, items `{shown, hidden_or_invisible, carried, undiscovered_trap, secret_door, scenery, not_perceived}` |
| `dev.perception_probe` | S | `{radius=30, reset?}` | Large-object (door/ladder/≥2 m tall or ≥1 m radius) perception: `{counts:{large, by_sight, by_ray, not_perceived, filtered, ray_shown_traps, ray_shown_secret_doors}, ray_only:[{uuid,name,kind,dist,height,radius}], stats, calibrated, ray_broken}`. `ray_shown_*` must be 0. `reset` re-enables a ray rule disabled by an error |
| `state.timings` | S | – | Per-op latency in ms |

### Actions (`Server/Guard.lua` + `GuardGate.lua`, `GuardSpells.lua`, `GuardCast.lua`; `Server/Actions.lua` + `ActionsMove.lua`, `ActionsInteract.lua`, `ActionsCast.lua`, `ActionsParty.lua`, `ActionsClick.lua`; owner: worker **act**)
All take `{char: uuid, ...}` and return `{job}`. A job finishes `done` (with a short result), `failed`
(with the reason a player would see), `interrupted` (with what happened, e.g. combat started or dialogue
began), or `timeout`.
`act.move_to{char, target:uuid | pos:[x,y,z], run=true}` · `act.interact{char, target}` (doors, levers,
containers, corpses, books, ladders, waypoints) · `act.pickup{char, item}` · `act.talk{char, npc}` ·
`act.cast{char, spell, target?:uuid, pos?, targets?:[], upcast?, ui?}` (attacks, spells, Jump, Shove, Dash,
Hide, Throw, consumables; at most 2 targets, or with `ui=true` up to 10: the hotbar route, where the job checks the
cast as usual, issues no order and only watches CastSpell/CastedSpell while the bridge clicks the hotbar; the game
charges it, the guard adds nothing; no pos/upcast; no start in 15 s = failed; a two-step spell (audit ACT-9) takes
one pick: Revivify the body, Dimension Door `pos` = the landing + `target` = who goes (default the caster), the bridge
clicks the pick and the ground, `bg3ai/twostep.py`; the result says `revived` / `teleported` + `landed_m_from_target`) · `act.end_turn{char}` · `act.equip{char,item}` / `act.unequip` ·
`act.transfer{item, to:uuid|"camp"}` · `act.select{char}` · `act.chain{char, leader}` / `act.unchain{char}` ·
`act.sneak{char, on}` · `act.turn_based{on}` · `act.go_to_camp{}`.
`Guard.lua` enforces visibility, legality, combat gating and the cheat blocklist
(`research/03-actions.md` §7).

**Reply shape.** Every `act.*` call returns `{job, eta_s?, route_m?}` at once. When the job already
settled during the call (a refusal, or a `move_to` that is already there) the reply also carries
`{status, result, error}`, so the bridge doesn't need a `job.poll`. `eta_s` is a rough walk estimate (run 4 m/s).
`notify:true` in any `act.*` call makes the job push `job_finished {job:"j12", op, status, error?, dist?, pos?}`
when it ends. The bridge sets it for async (`wait=false`) actions, so any later tool result shows the end.

**Movement.** Entity targets are walked to a verified reachable point next to them (`G.approach_point`: rings
of 1.0/1.5/2.2 m, each proven by `BG3AI.Nav`). It never uses `CharacterMoveTo`. Approaches re-plan when the
target moves more than 2 m or is out of reach on arrival, up to 4 times. Jump distance is capped
(`G.jump_max`: Strength/2 m, ×3 Enhance Leap; UNVERIFIED formula).

| op | ctx | args | result |
|---|---|---|---|
| `act.watch` | S | `{radius=30}` | One cheap read for the bridge's action queue: `{party:[{uuid,pos,hp,max_hp,downed?}], combat, dialogue, seen:[{uuid,name,hostile?,dead?}]}`. `seen` is visibility-filtered, as in `state.nearby` |
| `act.note` | S | `{kind:"queue_*", data}` | Pushes the bridge's queue reports into the event stream. Only `queue_*` kinds are accepted |
| `act.target_state` | S | `{uuid}` | `{visible, held?, locked?, opened?, trap_armed?, dead?}`; `held` = carried by the party |

**Bridge-side scheduling (`bridge/src/bg3ai/actq.py`, no mod op).**
- **Async tools.** `move_to` and `walk_toward` are async by default; other actions take `wait=false`.
- **`wait_for(target, timeout_s)`.** `target` is a job id, `queue[:who]`, `idle`, `my_turn`, or event kinds.
- **`queue_actions`.** A per-character FIFO, run by a worker thread through the same tools, so the guard
  checks every step.
- **`do_actions`.** Steps take optional `if`/`unless` conditions.
- **Interrupts and events.** The default interrupts are combat start, conversation start, a reaction/roll/popup
  on screen, a new hostile in `act.watch`, HP below 35%, `teleport_blocked`, and any failed step. They cancel
  the running job (`job.cancel`) and drop the rest, and are reported as `queue_interrupted {who, why, done,
  left, last}` or `queue_done {who, done, skipped?}`.
- **Connection.** Interrupt checks use their own LuaDebug connection: `act.watch` + `events.since` every
  0.3 s, and `ui.state` every 1 s.
- **`combat_turn`** (`bridge/src/bg3ai/turn.py`). One call per combat turn: slots move/action/bonus/extra, each
  a list of alternatives (any batchable tool) tried in order. Before each step the bridge re-reads
  `state.combat` (still our turn?) and `act.target_state` (target visible, not dead). A move that exceeds the
  movement left becomes a partial walk via `nav.approach` (route-following; `nav.reach_toward` on an old mod); `within` stops at a range, and a move that ends out of range says why. Reaction prompts
  stop the turn unless the plan's `on_reaction` answers them (`ui.react`). `end_turn` runs without waiting;
  the result names the next combatants from the visible turn order.
- **Macros** (`bridge/src/bg3ai/macros.py`): `loot_area` (`state.nearby` corpses + unlocked, unowned,
  untrapped containers), `dialogue_advance` (`ui.dialogue_choose` only for a continue prompt or one
  untagged, non-committing option), `travel_to` (chained `nav.reach_toward` legs ≤30 m), `regroup`
  (parallel per-character queues), `read_all` (carried books by name, text from the `book` screen), and
  `rest_if_needed`. All are bridge-side, use only player-visible ops, and run their steps through the
  guarded tools with the default interrupts.

### UI (`Client/UI.lua`, `Client/UIState.lua`, `Client/UIActions.lua` + `UIActionsPrompts.lua`, `UIActionsMenu.lua`; owner: worker **io**)
The helper API for other client modules is documented at the top of `Client/UI.lua`.
Every action op accepts `dry=true`, which only runs `CanExecute`. Ops marked *job* return `{job}` and settle at the next
decision point, so batched actions can chain.

**Busy UI answer.** During a rest, travel or load transition a Noesis call can wait out bg3le's frame lock; after one
such call (≥ 40 ms, or bg3le's own refusal) every UI read raises at once for 200 ms (`Client/UI.lua` UI.busy). A client
op that met it answers `{ok:false, error:"bg3le: UI busy: the game's UI is busy (...); <mark>; retry in a moment"}`
(a job it started is finished `interrupted`). `<mark>` says whether anything ran (`Shared/RPC.lua`):

| mark | meaning | the bridge (`bridge/src/bg3ai/busywait.py`, `Game.rpc`) |
|---|---|---|
| `refused before its first UI call, nothing was done` (RPC.BUSY_BEFORE) | the op's first UI call was refused at the gate and that ended the op: no read, no press | asks **any** client op again, presses included (e.g. `ui.dialogue_choose` right after a screen change) |
| `met it after a UI call ran` (RPC.BUSY_AFTER) | a read or a press may have happened before the answer | asks again only the reads and target-state ops in `RETRY_OPS` (`ui.state`, `ui.selected`, `ui.trade`, `ui.reactions`, `ui.hotbar_slot`, `ui.cast_state`); any other op's caller gets the busy answer (asking again could press twice) |

The bridge asks every 0.25 s for up to 5 s; then the busy answer stands, with the wait appended. Server ops never
answer busy.

| op | ctx | args | result |
|---|---|---|---|
| `ui.state` | C | `{full?}` | `{screen, page?, blocking?, panels_open?, region?, subtitle?:{speaker,text}, notifications?:[text], gates?, ms}` plus one of the blocks below. `screen` ∈ `main_menu, loading, game, dialogue, roll, reaction, popup, tutorial, container, trade, level_up, character_creation, rest, book, reward, learn_spells, tadpole_tree, save_load, game_menu, options, map, game_over`. `blocking` = a modal blocks world actions. `gates` only appears when a gate is on or with `full` |
| ↳ `dialogue` | | | `{speaker, text, options?:[{i, text, tags?:["PERSUASION",...], off?}], continue?, listening?, can_trade?}` |
| ↳ `roll` | | | `{phase: intro/ready/rolling/result/reroll, skill, context?, dc? (only if shown), advantage?, modifiers?, advantages?, boosts?:[{name,value?,off?}], selected?, inspiration?, result?, success?}` |
| ↳ `reaction` | | | `{prompt, options?:[{i, who, name, decision}], pending?}` |
| ↳ `popup` / `tutorial` | | | `{title?, text?, buttons?:[label]}` (also used for game_menu/main_menu/book/reward/options) |
| ↳ `container` | | | `{slots?:[{i, kind, rarity?, n?, stolen?, gold?}], text?}`. There are no item names client-side: use `state.inventory` |
| `ui.dialogue_choose` | C | `{i \| 0/"continue", text?, skip?, dry?}` | *job* → `{screen, lines:[{speaker,text}], dialogue?, ended?}` (settles on new options, a continue prompt, another modal, or the end). An option outside the rendered rows is refused `… (needs_scroll)` unless the list's extent pairs rows with answer VMs by position; `text` = the option's text, checked against the row before pressing (audit UI-4) |
| `ui.roll` | C | `{boosts?:[i], reroll?, dry?}` | ready: add boosts and roll, *job* → `{roll}`. result: reroll (Inspiration) or continue, *job* like dialogue |
| `ui.react` | C | `{use:bool, which?:i, dry?}` | *job* → `{pending?, reaction?}` |
| `ui.loot` | C | `{all:true \| slots:[i] \| close:true, dry?}` | Take all / single slots (indexes from `container.slots`) / close. *job* waits for the panel to close. For a single item by uuid use `act.pickup` |
| `ui.menu` | C | `{action: open_menu/close_menu/continue/load/quickload/quicksave/save/save_confirm, name?, dry?}` | `save {name}` → *job* `{saved, name}`: opens the save page, opens "new save" (RETURN = UIAccept), sets the name from Lua (no typing), accepts (RETURN), confirms an overwrite prompt, waits for saving to finish and closes the menu. If an injected key doesn't land, the job fails with `{needs_key:"RETURN"/"ESCAPE", stage}`: send that key through the input shim, then `save_confirm {name?}` continues. Save actions need the save gate, load actions the load gate |
| `ui.config` | C | `{save_enabled?, load_enabled?, autoskip?}` | `{gates:{save,load}, autoskip}`. AutoSkip (default on) skips movies (ESC) and voiced dialogue lines without answers (`SelectorSkipCommand`), and pushes each line as the event `dialogue_line`; the runner sends each session's `autoskip` (the run's setting). `autoskip` lives in memory only (a reload or a new game process starts on again); the gates are persisted in the extender data dir `bg3ai/config.json`, read at bootstrap. **Both gates default to false.** Turn them on only when the AI's own playthrough is loaded |
| `ui.settings` | C | `{show_tutorials?, subtitles?, display_speaker?, skip_larian_signup?, skip_splash?, dialogue_wait?, auto_continue?, hold_to_end_turn?, camera_shake?, closeup_dialog_camera?, camera_auto_rotate?, combat_feed?, text_background?, north_facing_minimap?, mute_unfocused?, list?}` | `{settings, errors?}`. Engine switches, persisted in our config and re-applied on start. Works at the main menu and in game |
| `ui.reactions` | C | `{char, set?:{<name\|id>: "never"/"always"/"ask"}, dry?}` | `{char, reactions:[{id,name,mode}]}`. Selected character only (`act.select` first) |
| `ui.dismiss` | C | `{dry?}` | `{closed:[{page,how,title?}], decisions?, failed?}`. Closes tutorials, info boxes, rewards, book/examine and sign-up. Never closes decisions (`popup.decision=true` in `ui.state`; use `ui.press`) |
| `ui.key` | C | `{key, mods?, hold_ms?}` | SDL scancode labels (`ESCAPE`, `SPACE`, `1`), modifier labels (`LSHIFT`, ...). `hold_ms` → *job*. Refused: `F5`/`F8` (quicksave/quickload) and any key but ESCAPE while the Save/Load page is open (UIActions.SAVELOAD_*, audit UI-3) |
| `ui.command` | C | `{widget \| "*", command, target?, param?, dry?}` | Runs `DC.<target>.<command>(param)`. `target`/`param` are dotted paths from the widget's DataContext (numbers index collections). `"*"` = any DC (shared base: `EndTurn`, …). Refused unless dry: save/load/delete-save commands (Quick*, *SaveGame*, *LoadGame*, ContinueGame, Overwrite) and anything but close/back on the Save/Load page: `ui.menu` does those (audit UI-3) |
| `ui.press` | C | `{button, widget?, nth?, dry?}` | Clicks a visible button by its dump `n=` name or a label substring, on `widget` or, if omitted, any visible page. Uses the button's Command + CommandParameter, or, for string-parameter buttons with a BoundEvent, the bound key (UIAccept→RETURN, UICancel/ToggleInGameMenu→ESCAPE). Result `{page, via:"command"/"key:RETURN"}` |
| `ui.dump` | C | `{root?, depth?, text_only?, max?, hidden?}` | `{widgets:[{w, dc, items:[{t} \| {b,n,off?}]}], ms}`: visible text and buttons per page |
| `ui.widgets` / `ui.props` | C | – / `{widget, path?, commands?}` | Open pages with DC types / a VM's values, sub-objects and commands (dev) |

Hotbar cast route (`Client/Hotbar.lua`, worker **se**, audit ACT-8; bridge `bg3ai/hotbar.py hotbar_cast`, wired into
use_ability for more than two targets but off: `HOTBAR_CAST_ENABLED = False`). Built from HotBar.xaml offline; the guesses G1-G10 are listed in both files and
checked by acceptance se "hotbar widget tree (ACT-8, read-only)" / "hotbar cast: Bless on 3 allies (ACT-8)".

| op | ctx | args | result |
|---|---|---|---|
| `ui.hotbar_slot` | C | `{spell, char?, tabs?}` | `{found, deck, index, slot, cols, rows, order, can_use, container?, upcast?, shown_deck, rendered, cell:{w,h}, over, at?:{deck,index,slot,spell,order,cols,rows}, over_hud, deck_button, deck_button_over?, selected?, why?}`: the selected character's cell for a spell id (index 1-based, column-first grid), `over` = the cursor is on it, `at` = the cell under the cursor now |
| `ui.cast_state` | C | `{char?}` | `{armed, spell?, remaining?, can_force, counter, targeting?, preview?, upcast_open?, cast?:{spell, phase, picked, index, previewing}}`: the spell in hand, the cursor's target counter, the tick button, the client cast entity |
| `ui.cast_confirm` | C | `{}` | `{done}`: the multi-target tick button (`ForceCastCommand`), only while armed and enabled |
| `ui.cast_cancel` | C | `{}` | `{done, was_armed}`: `CancelTaskCommand`, the bridge's last resort after ESCAPE and a right click |
| `ui.ground_check` | C | `{pos?, tol?=1.0, avoid?:[uuid], no_chars?}` | `{world?, d_m?, near?, hover?, on_avoid, cursor?, overlay?, warning?, at?}`: a ground click's proof before it is made (act, ACT-9 two-step route, `Client/Pick.lua`): the cursor's world point and its flat distance to `pos`, a character under it, the cursor shape (`ecl::CursorControl`: `Cast` / `CastWarning`, GUESS T1) |

`ui.screen_pos` (`Client/Pick.lua`) also takes `{pos:[x,y,z]}` (a ground point) instead of `uuid`, and returns `world`
(the point projected).

Widget names are the XAML root `x:Name`, e.g. `ContainerWidget`, `Dialog_box` (MessageBox), `ModalTutorial`,
`NotificationWidget`, `JournalQuest`, `DMSettings`. Character creation, new game and level-up live in `Client/Progression.lua`,
`ProgressionCC.lua` and `ProgressionLevelUp.lua` (worker **se**). Trade, rests, fast travel and waypoints live in
`Client/Services.lua`, `ServicesItems.lua`, `ServicesRest.lua` and `ServicesWaypoints.lua` (worker **kb**).

bg3le limitations (verified 2026-10-09):
- `TextBlock` text: use `:ToString()`, because `GetProperty("Text")` is nil. `GetProperty` logs unknown names, so use `GetAllProperties` for optional ones.
- `ICommand:Execute(arg)` only takes a Noesis object or nil. A boxed string parameter (e.g. GameMenu "Resume" →
  `"CloseGameMenu"`, `CustomEvent "..."`) can't be passed, so use `ui.key`. Raising a routed `Click` doesn't run commands.
- `ls.VMItem` and `ls.Character` have no `Name`, `ls.EntityRef` has no fields, and `ls.VMWaypoint(Group)` has no properties. Names and uuids
  come from rendered text or the server.
- Never cache element proxies across calls, because re-templated elements are freed. Cache child-index paths instead (`UI.cachedByName`).
- `Ext.Debug.Reset()` clears the client job table: poll errors with "no such job" after another worker reloads.
- Verified working: `Ext.UI.GetRoot()`, `VisualChild`, `GetProperty`, `GetAllProperties`, `At`/`Count`,
  `ICommand:CanExecute/Execute` (`gui::DCMainMenu.ContinueGameCommand` loaded a save), `Ext.Input.InjectKeyPress`,
  `Ext.IO.SaveFile/LoadFile`, `Ext.Debug.Reset()`.

### Harness only (`Server/Referee.lua`, `Server/RollLog.lua`, `Server/Scenario.lua`)
These ops are for the harness and never for the agent: no MCP tool calls them, nothing in them calls `RPC.push`, and
`bridge/tests/test_referee.py` checks the bridge's top-level modules and tool names for them. The harness reads the
rings through `bridge/src/bg3ai/referee.py` (`RingReader`, which turns the mod's monotonic `t` into epoch seconds)
and calls `scn.*` through `packages/harness/src/bg3harness/game_settings.py` and `tools/saves/make_start_save.py`.

| op | ctx | args | result |
|---|---|---|---|
| `ref.since` | S | `{seq, limit=500}` | `{boot, now, gt, latest, first, events:[{seq, t, gt, ev, ...}]}`: referee records after `seq` in the facts v1 event shapes (`packages/harness/schemas/facts.v1.schema.json`). `first` is the oldest seq kept (cap 4000): a cursor below `first - 1` lost records. A new `boot` (a load rebuilt the Lua state) restarts `seq` at 1 |
| `ref.snapshot` | S | – | `{boot, now, gt, latest, fired, never, registered, trade?, pickpocket?, combats}`: which listeners fired at least once, the open trade or pickpocket, the combats seen |
| `roll.since` | S | `{seq, limit=500}` | same cursor contract; one record per die roll: `{seq, t, gt, srcs, roller, party, success, natural, result, dc, stream, streams, ...}` |
| `roll.snapshot` | S | – | the roll streams now (`num_rolls`, `seed`, Karmic debts), `mode` (RollModeType), `base_seed`, which sources and hooks fired |
| `scn.seed` | S | `{seed}` (integer 0..2000000000) | writes the dice seed now and records it; the mod writes it again after every load (`LevelGameplayStarted`) |
| `scn.karmic` | S | `{set?: bool}` | reads Karmic Dice, or turns it off/on and reads it back |
| `scn.difficulty` | S | – | reads the difficulty; a `set` is refused (the game's Options flow owns it) |
| `scn.rename_avatar` | S | `{name}` | the host avatar's name (1-40 letters, digits, spaces, `_`, `-`) in CustomName, CharacterCreationStats.Name and its DisplayName text, read back; recorded, so the mod writes it again after every load of that avatar; returns `{uuid, original, before, name, handle, display, cc}` |
| `scn.rename` | S | `{renames:[{uuid, name}]}` | changes display names through `Ext.Loca`; originals are recorded so `scn.undo` restores them after a load |
| `scn.undo` | S | – | removes the seed record and restores the renames, Karmic Dice and the avatar's name |
| `scn.status` | S | – | `{seed, applied, reapplied, renames, avatar}` |

Every `scn.*` op fails with `scenario setup is off: the game was not started with BG3AI_SCENARIO_SETUP=1` unless
the game process has that variable. The runner sets it only for a run with a seed.

## Input shim (native, owner: worker **se**)
`native/bg3sdl/`: an `LD_PRELOAD` SDL2 shim for mouse and keyboard injection into the game only, controlled
over the abstract socket `\0bg3ai-input`. It's the fallback for world clicks and anything without a command.
Python client: `bridge/src/bg3ai/input.py`.
