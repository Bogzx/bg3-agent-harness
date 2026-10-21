## Efficiency (hard rule)
- Read what the engine can tell you as text, and do what it can do with an action tool. For a screen no tool covers,
  use `ui_dump` (text) then `ui_action`, before considering `screenshot` + `click`.
- **Don't know the way** (no path, the goal on another floor, fire in the way, a ladder)? Ask `route` instead of
  taking a screenshot.
- Write names, not ids, in your notes.
- **Trust the result; don't re-read.** Every call is a ~2 s round trip, and repeated reads of what the last result
  said add up fast. Each result already carries what you need:
  - after a walk's end, `LEADS` + `WAYS walkable` → no `get_frontier` / `get_nearby` / `look` after arriving.
  - `route(..., go=true)` checks and starts the trip → no `route` then `travel_to`.
  - `talk_to`: the opening lines and the first real choice → no `get_dialogue`.
  - `COMBAT STARTED` → no `get_combat_state`. After `combat_turn`'s `NOW <who>'s turn: plan it; also <b>, <c>`, plan
    them all in ONE `combat_turn` with a list.
  - EVENTS: items, gold, statuses, damage, deaths, approvals, level-ups → no `get_inventory` / `get_party` /
    `get_status` to check them.
  - `note()` and `note="…"` on travel_to/queue_actions/do_actions → no `memory_edit` of the session log mid-walk.
  Re-read only when a result says something failed, or a decision needs a fact no result showed.
- `screenshot`/`zoom` are **only** for:
  - real ambiguity the text can't resolve
  - visual puzzles and spatial layouts the text doesn't describe
  - UI the tools don't model, and only after `ui_dump` didn't explain it
- **Never screenshot "just to check."** `zoom` beats a new full screenshot when you only need one detail.
- `click`/`press_key` are a last resort for unmodelled UI. Log each use as `[note] harness: needed click for <what>`.
- **Think while acting: never let the character stand still while you think.** Use a walk to read, plan, or write
  notes; `wait_for` only when your next step really depends on the arrival. Queue the next few steps with
  `queue_actions` and observe (`look`, `get_nearby`) and plan the next batch while they run.
- **Targets:** outside combat, at most ~2 s between your actions; never more than 10 s, except for a real dialogue
  decision. Play at the pace of a skilled human, or faster.
- Don't call `get_status` between every action; the deltas already tell you what changed. Use `look(full=true)` only
  when you've lost track (after a load, a long break, confusion).
