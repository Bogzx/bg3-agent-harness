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
- **Movement is async.** `move_to` and `walk_toward` return at once with `started j12, ETA 3s`; `travel_to` (a
  marker, objective or waypoint, as chained walks) returns at once with the trip as the first entry of the
  character's queue. A `job_finished`/`queue_done` event in a later result says it arrived. A fast failure (no path,
  a hazard ahead) still comes back in the first result. Out of combat the server ignores `wait=true` on walks and
  says so.
- **`wait_for`** blocks until its target: `my_turn`, `idle`, a job (`j12`), `queue`, or an event kind. It returns
  early on a prompt. `wait()` passes game time.
- **`queue_actions`** runs steps back to back with no round trip per step: walk → open → loot → walk on. Another
  `queue_actions` call appends; right after a `move_to` it waits for that walk before its first step. The queue
  stops by itself on combat, a conversation, a reaction/roll prompt, a new hostile in view, low HP, or a failed step;
  the `queue_interrupted` event says why and what was left. `cancel_queue` stops it. Any direct action for the same
  character replaces the queue's current step.
- **`do_actions`** is the blocking form: a short chain of steps in one call. Steps can carry `"if"`/`"unless"`
  conditions (`in_combat`, `hp_below:50`, `item_present:e12`, `target_visible:e7`, `my_turn`, `in_dialogue`). The
  result ends with `CHANGES` (what moved, HP, items, who appeared).
- **`combat_turn`** plays a whole combat turn in one call: move, action, bonus action, extra attacks, `end_turn`.
  Each slot lists fallbacks (melee → ranged → Dash → Disengage), and every step is re-checked (still that
  character's turn, target still visible and alive). It returns right after ending the turn with who acts next; its
  result ends with `NOW <who>'s turn: plan it; also <b>, <c>`, and one call takes a list of plans:
  `[{"who":"Karlach","plan":{…}},{"who":"Gale","plan":{…}}]`. A combat move that ends out of reach says
  `stopped short of ≤Nm: <why>`.
- **Macros** do a chore in one call and stop at the first interrupt: `loot_area` (visible bodies and unlocked
  containers nearby), `dialogue_advance` (filler lines only; it stops at every real choice), `travel_to`, `regroup`,
  `read_all` (new books and notes in your bags, with their text), and `rest_if_needed`.

**Results.** Each action reports `done`, `FAILED — <reason>`, `INTERRUPTED — <what happened>`, `TIMEOUT`, or
`started jN` (async), followed by EVENTS. What a result already carries:
- a walk's end (`wait_for` on it, a queue's result): `LEADS` (unvisited things in view) and `WAYS walkable`;
- `talk_to`: the opening lines and the first real choice;
- an action that starts a fight: `COMBAT STARTED` with the turn order and your actions;
- EVENTS: items gained, gold, statuses, damage, deaths, approvals, level-ups.
A result may end with `RUNNING …` (still walking or queued), `NEXT: …` (what can be done while it runs) and
`PACE: …` (a wait came before there was nothing else to do).

**Dialogue and cutscenes.** Cutscenes are skipped automatically and spoken lines are fast-forwarded; each line still
arrives as a `dialogue_line` event (speaker: text). Opening and in-between filler is clicked for you: `talk_to` and
`choose_dialogue` results show the next real choice directly. Answers are never picked for you.
