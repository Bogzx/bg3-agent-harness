## Play like a human clicking ahead (pipelining)
A skilled player clicks the next destination, then thinks while the character walks. Do the same:
**issue the next steps, then observe and plan while they run.** Your character should never stand still while you
think. The patterns that cover most of the game outside combat:
1. **Go there, then act there:** `travel_to(objective="o1")` returns at once. Append what to do on arrival right
   away: `queue_actions('[{"tool":"interact","target":"e15"},{"tool":"loot"}]')`. While it runs: `look`, read the
   events, write your notes, plan the next batch.
2. **A sweep in one call:** `queue_actions('[{"tool":"move_to","target":"e7"},{"tool":"interact","target":"e7"},{"tool":"loot"},{"tool":"pick_up","item":"e9"},{"tool":"walk_toward","direction":"W"}]')`.
   Before it ends, append the next batch. Each result's EVENTS shows `queue_done`, or `queue_interrupted` with why
   and what was left: handle that first.
3. **Needing the outcome is not a reason to block.** Even when the next decision depends on the arrival (what's
   behind the door, did the route get through), issue the move async, **then do the work that doesn't depend on
   it** (`note("…")` the last event, `path_preview`/`get_frontier` for the next leg, `look_at` something in view, a
   `wiki_page` you'll need), and only then `wait_for("queue")` / `wait_for("j12")`. A fast failure comes back in the
   first result, so async costs you nothing.
4. **Wiki lookups ride on a walk.** Before `wiki_search`/`wiki_page`, start a walk or queue (toward the lead you're
   checking, the next loot, a regroup), then read the wiki while it runs.

**Never pass `wait=true` to `move_to`, `walk_toward` or `travel_to`**, not even after a failed or stopped walk, and
not because an earlier call in this session did: one blocking walk invites the next, and the character stands still
while you think. Also don't: call `wait()` to pass time (use `wait_for` with a target: `my_turn`, `idle`, a job, an
event kind); write long notes between two actions instead of during a walk; call `interact`/`move_to` directly on a
character whose queue is running (a direct action supersedes the queue: append instead, or `cancel_queue` on
purpose); make one tool call per step of a plan you already know. In combat use `combat_turn`; in dialogue,
`dialogue_advance` for filler. Follow a result's `RUNNING …` (append, don't wait), `NEXT: …` and `PACE: …`. After
`stopped short of ≤Nm`, plan from the distance it gives, not from the move being `done`.
Results carry two pace hints: `NEXT: …` after a walk that still runs (what can be done while it runs) and `PACE: …`
(a wait came before there was nothing else to do).
