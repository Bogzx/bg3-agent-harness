## How the game and the tools work

**Reading.** Anything the engine can tell you comes as text: `look`, `get_status`, `get_party`, `get_nearby`,
`get_frontier`, `get_actions`, `get_dialogue`, `get_combat_state`, `get_journal`, `get_map`, `get_inventory`,
`look_at`, `path_preview`, `route`.
- `route(objective="o1")` (or `target=`/x,y,z) answers the way as text: floors above/below, the walk in legs with
  climbs and jumps, the closest walkable spot, which ladder or door helps, and a NEXT step. `route(..., go=true)`
  checks the way and starts the trip.
- Observations are **deltas**: after the first call, `look`/`get_status`/`get_map` return only what changed since
  your last look (moves, HP, conditions, who appeared/left/turned hostile, items, mode, objectives). `look()` is the
  cheap glance; `full=true` gives everything again.
- Targets are short ids from the observation tools (`e12`) or party names. Ids last one session.

**Acting.** Anything the engine can do has an action tool: `move_to`, `walk_toward`, `interact`, `talk_to`, `loot`,
`use_ability`, `end_turn`, `choose_dialogue`, `roll_check`, `react`, `fast_travel`, `manage_inventory`,
`short_rest`/`long_rest`, `camp`, `level_up`, `trade`, and so on. For a screen no tool covers, `ui_action` acts on it.
- **A walk returns at its end.** `move_to`, `walk_toward` and `travel_to` (a marker, objective or waypoint, as
  chained walks) return when the walk is over, arrived or stopped, with what changed and the minimap block:
  `OBJECTIVES` (distance, direction, height), `WAYS` and the nearest unvisited `LEADS`. A prompt, a conversation or
  a fight that starts ends the wait early and says so. A trip still going after ~90 s returns its progress, and
  `wait_for` keeps waiting. Out of combat `wait=false` returns at once instead (`started j12, ETA 3s`, or the trip as
  the first entry of the character's queue); a `job_finished`/`queue_done` event in a later result says it arrived.
  In combat a walk returns when it is over, whatever `wait` says.
- **`wait_for`** blocks until its target: `my_turn`, `idle`, a job (`j12`), `queue`, or an event kind. It returns
  early on a prompt. `wait()` passes game time.
- **`queue_actions`** runs steps back to back with no round trip per step: open → loot → pick up → walk on. A walk
  step (`move_to`, `walk_toward`, `travel_to`, `jump_to`) is the last step a batch takes: the steps after the first
  one are not queued, and the result lists them (after one with an `"if"`/`"unless"` they are queued and run only if
  it is skipped). Another `queue_actions` call appends; right after a `move_to` it waits for that walk before its first step. The queue
  stops by itself on combat, a conversation, a reaction/roll prompt, a new hostile in view, low HP, or a failed step;
  the `queue_interrupted` event says why and what was left. `cancel_queue` stops it. Any direct action for the same
  character replaces the queue's current step. In combat `queue_actions` queues nothing.
- **`do_actions`** is the blocking form: a short chain of steps in one call, ending at its first walk step as
  above. Steps can carry `"if"`/`"unless"`
  conditions (`in_combat`, `hp_below:50`, `item_present:e12`, `target_visible:e7`, `my_turn`, `in_dialogue`). The
  result ends with `CHANGES` (what moved, HP, items, who appeared). In combat `do_actions` runs no steps.
- **In combat each call is one action**: `use_ability`, `move_to`, `walk_toward`, `jump_to` or `react`. It returns
  when the action is over, with the character's resources left (`after={…}`) and `CHANGES` (HP, deaths, moves,
  statuses). `end_turn` ends a member's turn and waits through the other combatants' turns until a party member can
  act; its result names who acts now, with that member's actions (`YOUR ACTIONS`) and the other combatants' turns
  in EVENTS. `wait_for("my_turn")` returns with them too.
- **Macros** do a chore in one call and stop at the first interrupt: `loot_area` (visible bodies and unlocked
  containers nearby), `dialogue_advance` (filler lines only; it stops at every real choice), `travel_to`, `regroup`,
  `read_all` (new books and notes in your bags, with their text), and `rest_if_needed`.

**Results.** Each action reports `done`, `FAILED — <reason>`, `INTERRUPTED — <what happened>`, `TIMEOUT`, or
`started jN` (async), followed by EVENTS. What a result already carries:
- a walk's end (`wait_for` on it, a queue's result): `LEADS` (unvisited things in view) and `WAYS walkable`;
- `talk_to`: the opening lines and the first real choice;
- an action that starts a fight: `COMBAT STARTED` with the turn order and your actions;
- EVENTS: items gained, gold, statuses, damage, deaths, approvals, level-ups.
A result may end with `RUNNING …` (still walking or queued).

**Dialogue and cutscenes.** Cutscenes are skipped automatically and spoken lines are fast-forwarded; each line still
arrives as a `dialogue_line` event (speaker: text). Opening and in-between filler is clicked for you: `talk_to` and
`choose_dialogue` results show the next real choice directly. Answers are never picked for you.
