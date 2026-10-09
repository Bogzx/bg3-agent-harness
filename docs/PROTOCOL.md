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

## Live development

The game is usually running with bg3le and our mod loaded from `mod/` (via `BG3LE_MOD_PATH`, a loose
folder: no `.pak` and no modsettings change).

```bash
cd bridge
PYTHONPATH=src python3 -m bg3ai.luadebug            'return 1+1'       # client context
PYTHONPATH=src python3 -m bg3ai.luadebug --server   'return Osi.GetHostCharacter()'
PYTHONPATH=src python3 -m bg3ai.luadebug 'Ext.Debug.Reset() return 1'  # HOT RELOAD both contexts after editing Lua
```

Prototype a snippet in the eval first, then move it into the module and hot reload.

Rules while the user's own save is loaded (currently a real playthrough in Act 2):
- **No saving** (quick, auto or named), **no long or short rests**, **no area transitions or fast
  travel**, **no story dialogue choices**, and **no attacks or spells on NPCs**. Reads are always fine.
  Short walks with party members are fine. Opening a door or container is OK if nothing is taken.
- Don't restart or kill the game. Ask the orchestrator.
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

### State (`Server/Visibility.lua`, `Server/State.lua`; owner: worker **vis**)
| op | ctx | args | result (sketch) |
|---|---|---|---|
| `state.status` | S | – | `{level, region, subregion, active:uuid, party:[{uuid,name,hp,max_hp,level,classes,pos,statuses,downed,in_combat}], in_combat, turn_based, in_dialogue, gold, combat:{round,current}?}` |
| `state.character` | S | `{uuid, sections?}` | Party: full sheet (abilities, saves, skills, AC, resources incl. spell slots/action/bonus/movement/reaction, passives, conditions, equipment, concentration, XP). Others: the Examine-panel projection only |
| `state.actions` | S | `{uuid}` | Party member's usable actions as the hotbar shows them: `[{spell, name, level?, cost:{action,bonus,movement,slot}, range?, target:"entity"/"point"/"self"/"aoe", available:true/false, reason?}]` |
| `state.nearby` | S | `{radius=25, kinds?}` | Visibility-filtered `{characters:[{uuid,name,relation,pos,dist,hp?,max_hp?,statuses,in_combat,can_talk,is_trader}], items:[{uuid,name,kind(door/container/item/lever/ladder/trap/waypoint/book),pos,dist,locked?,opened?,owned?,trap?}], remembered:[...]}` |
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

### Navigation (`Server/Navigation.lua`; owner: **vis**)
| op | ctx | args | result (sketch) |
|---|---|---|---|
| `nav.path` | S | `{char?, pos \| target}` | `{reachable, length_m, nodes, needs_jump?, within_movement?, movement_left, approach_pos?}` or `{reachable:false, straight_m, error_cause}`; points > 80 m → `{estimate:true, straight_m}` |
| `nav.reach_toward` | S | `{char?, dir:N\|NE\|…\|NW \| pos \| target, max_m=30, step=2}` | Farthest walkable point that way with a direct path (path ≤ max(1.5×straight, straight+2 m)): `{reachable, pos, path_m, straight_m, dir, needs_jump?, samples, ms}`. Target must be visible |
| `nav.markers` | S | – | Active quest objective markers (what the map shows): `{markers:[{label, quest, objective, kind, level?, here?, pos?, dist?, dir?}], error?, ms}`; `here` = on the current level with a position; sorted here-first, nearest-first. Data: generated `Server/Data/JournalMarkers.lua` |
| `nav.explored` | S | `{last=10}` | Party breadcrumbs: `{level, cells, cell_m, area_m2, bbox, subregion, recent:[{pos, subregion?, near?, near_m?}]}` |
| `nav.frontier` | S | `{radius=30, limit=25}` | Perceived leads `{leads:[{kind, uuid, name, dist, dir, pos, unvisited?, locked?, relation?, waypoint?}], more?, ms}` |
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

### Actions (`Server/Guard.lua`, `Server/Actions.lua`; owner: worker **act**)
All take `{char: uuid, ...}` and return `{job}`. A job finishes `done` (with a short result), `failed`
(with the reason a player would see), `interrupted` (with what happened, e.g. combat started or dialogue
began), or `timeout`.
`act.move_to{char, target:uuid | pos:[x,y,z], run=true}` · `act.interact{char, target}` (doors, levers,
containers, corpses, books, ladders, waypoints) · `act.pickup{char, item}` · `act.talk{char, npc}` ·
`act.cast{char, spell, target?:uuid, pos?, targets?:[], upcast?}` (attacks, spells, Jump, Shove, Dash,
Hide, Throw, consumables) · `act.end_turn{char}` · `act.equip{char,item}` / `act.unequip` ·
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
  movement left becomes a partial walk via `nav.reach_toward`; `within` stops at a range. Reaction prompts
  stop the turn unless the plan's `on_reaction` answers them (`ui.react`). `end_turn` runs without waiting;
  the result names the next combatants from the visible turn order.
- **Macros** (`bridge/src/bg3ai/macros.py`): `loot_area` (`state.nearby` corpses + unlocked, unowned,
  untrapped containers), `dialogue_advance` (`ui.dialogue_choose` only for a continue prompt or one
  untagged, non-committing option), `travel_to` (chained `nav.reach_toward` legs ≤30 m), `regroup`
  (parallel per-character queues), `read_all` (carried books by name, text from the `book` screen), and
  `rest_if_needed`. All are bridge-side, use only player-visible ops, and run their steps through the
  guarded tools with the default interrupts.

### UI (`Client/UI.lua`, `Client/UIState.lua`, `Client/UIActions.lua`; owner: worker **io**)
Details and live-test status are in `research/impl-io.md`. The helper API for other client modules is documented at the top of `Client/UI.lua`.
Every action op accepts `dry=true`, which only runs `CanExecute`. Ops marked *job* return `{job}` and settle at the next
decision point, so batched actions can chain.

| op | ctx | args | result |
|---|---|---|---|
| `ui.state` | C | `{full?}` | `{screen, page?, blocking?, panels_open?, region?, subtitle?:{speaker,text}, notifications?:[text], gates?, ms}` plus one of the blocks below. `screen` ∈ `main_menu, loading, game, dialogue, roll, reaction, popup, tutorial, container, trade, level_up, character_creation, rest, book, reward, learn_spells, tadpole_tree, save_load, game_menu, options, map, game_over`. `blocking` = a modal blocks world actions. `gates` only appears when a gate is on or with `full` |
| ↳ `dialogue` | | | `{speaker, text, options?:[{i, text, tags?:["PERSUASION",...], off?}], continue?, listening?, can_trade?}` |
| ↳ `roll` | | | `{phase: intro/ready/rolling/result/reroll, skill, context?, dc? (only if shown), advantage?, modifiers?, advantages?, boosts?:[{name,value?,off?}], selected?, inspiration?, result?, success?}` |
| ↳ `reaction` | | | `{prompt, options?:[{i, who, name, decision}], pending?}` |
| ↳ `popup` / `tutorial` | | | `{title?, text?, buttons?:[label]}` (also used for game_menu/main_menu/book/reward/options) |
| ↳ `container` | | | `{slots?:[{i, kind, rarity?, n?, stolen?, gold?}], text?}`. There are no item names client-side: use `state.inventory` |
| `ui.dialogue_choose` | C | `{i \| 0/"continue", skip?, dry?}` | *job* → `{screen, lines:[{speaker,text}], dialogue?, ended?}` (settles on new options, a continue prompt, another modal, or the end) |
| `ui.roll` | C | `{boosts?:[i], reroll?, dry?}` | ready: add boosts and roll, *job* → `{roll}`. result: reroll (Inspiration) or continue, *job* like dialogue |
| `ui.react` | C | `{use:bool, which?:i, dry?}` | *job* → `{pending?, reaction?}` |
| `ui.loot` | C | `{all:true \| slots:[i] \| close:true, dry?}` | Take all / single slots (indexes from `container.slots`) / close. *job* waits for the panel to close. For a single item by uuid use `act.pickup` |
| `ui.menu` | C | `{action: open_menu/close_menu/continue/load/quickload/quicksave/save/save_confirm, name?, dry?}` | `save {name}` → *job* `{saved, name}`: opens the save page, opens "new save" (RETURN = UIAccept), sets the name from Lua (no typing), accepts (RETURN), confirms an overwrite prompt, waits for saving to finish and closes the menu. If an injected key doesn't land, the job fails with `{needs_key:"RETURN"/"ESCAPE", stage}`: send that key through the input shim, then `save_confirm {name?}` continues. Save actions need the save gate, load actions the load gate |
| `ui.config` | C | `{save_enabled?, load_enabled?, autoskip?}` | `{gates:{save,load}, autoskip}`. AutoSkip (default on) skips movies (ESC) and voiced dialogue lines without answers (`SelectorSkipCommand`), and pushes each line as the event `dialogue_line`. Persisted in the extender data dir `bg3ai/config.json`, read at bootstrap. **Both default to false.** Turn them on only when the AI's own playthrough is loaded |
| `ui.settings` | C | `{show_tutorials?, subtitles?, display_speaker?, skip_larian_signup?, skip_splash?, dialogue_wait?, auto_continue?, hold_to_end_turn?, camera_shake?, closeup_dialog_camera?, camera_auto_rotate?, combat_feed?, text_background?, north_facing_minimap?, mute_unfocused?, list?}` | `{settings, errors?}`. Engine switches, persisted in our config and re-applied on start. Works at the main menu and in game |
| `ui.reactions` | C | `{char, set?:{<name\|id>: "never"/"always"/"ask"}, dry?}` | `{char, reactions:[{id,name,mode}]}`. Selected character only (`act.select` first) |
| `ui.dismiss` | C | `{dry?}` | `{closed:[{page,how,title?}], decisions?, failed?}`. Closes tutorials, info boxes, rewards, book/examine and sign-up. Never closes decisions (`popup.decision=true` in `ui.state`; use `ui.press`) |
| `ui.key` | C | `{key, mods?, hold_ms?}` | SDL scancode labels (`ESCAPE`, `SPACE`, `F5`, `1`), modifier labels (`LSHIFT`, ...). `hold_ms` → *job* |
| `ui.command` | C | `{widget \| "*", command, target?, param?, dry?}` | Runs `DC.<target>.<command>(param)`. `target`/`param` are dotted paths from the widget's DataContext (numbers index collections). `"*"` = any DC (shared base: `QuickSaveCommand`, `EndTurn`, …) |
| `ui.press` | C | `{button, widget?, nth?, dry?}` | Clicks a visible button by its dump `n=` name or a label substring, on `widget` or, if omitted, any visible page. Uses the button's Command + CommandParameter, or, for string-parameter buttons with a BoundEvent, the bound key (UIAccept→RETURN, UICancel/ToggleInGameMenu→ESCAPE). Result `{page, via:"command"/"key:RETURN"}` |
| `ui.dump` | C | `{root?, depth?, text_only?, max?, hidden?}` | `{widgets:[{w, dc, items:[{t} \| {b,n,off?}]}], ms}`: visible text and buttons per page |
| `ui.widgets` / `ui.props` | C | – / `{widget, path?, commands?}` | Open pages with DC types / a VM's values, sub-objects and commands (dev) |

Widget names are the XAML root `x:Name`, e.g. `ContainerWidget`, `Dialog_box` (MessageBox), `ModalTutorial`,
`NotificationWidget`, `JournalQuest`, `DMSettings`. Character creation, new game and level-up live in `Client/Progression.lua`
(worker **se**). Trade, rests, fast travel and waypoints live in `Client/Services.lua` (worker **kb**).

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

## Input shim (native, owner: worker **se**)
`native/bg3sdl/`: an `LD_PRELOAD` SDL2 shim for mouse and keyboard injection into the game only, controlled
over the abstract socket `\0bg3ai-input`. It's the fallback for world clicks and anything without a command.
Python client: `bridge/src/bg3ai/input.py`.
