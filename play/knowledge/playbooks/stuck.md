# Getting unstuck

**When:** Use when progress has stalled - three failed attempts at the same thing, ~15 minutes without objective progress, looping between the same places or dialogue, a tool repeatedly returning blocked/timeout, a fight lost twice, or not knowing what to do next.

Loops are the biggest time sink for game-playing agents. The fix is a **different approach**, not
more tries of the same one.

**Stuck never ends the session.** Being stuck is not a reason to wrap up, write HANDOFF.md or stop.
The session runs until the `⏱` line says WRAP UP NOW (the Stop hook refuses an earlier stop, even with
HANDOFF.md written). Whatever happens below, the last step is always: **keep playing**.

## 0. Harness stuck: the 5-minute ladder
Use this first when the *tools or the game* misbehave rather than the puzzle being hard: an action
repeats on its own (the same `used <item>` event over and over), a character ignores orders, a job
times out and keeps running, the same tool fails the same way three times, a menu or screen won't
close. Spend ~1–2 minutes per rung, log each one as `- HH:MM [stuck] harness: <rung>: <result>`, and
stop at the first rung that frees you:
1. **Cancel and retry differently:** `cancel_queue(who=…)`, then `wait_for("idle", timeout_s=5)`; retry
   once with *different* arguments (another target, a point next to it, `move_to` instead of
   `interact`, `jump_to` a spot past it). Not a third identical try.
2. **Reload the last good save:** `list_saves()`, then `load_game(save="<folder name>")` with an explicit
   folder (not `"latest"`), then `look(full=true)`. Check the result really says the game loaded
   (mode `exploring`, not `screen=save_load`); if a load screen stays open, `ui_dump` → `ui_action`
   for the save's Load button. A reload for a bugged action is allowed: log it as `[reload]` with the
   reason.
3. **Another party member:** if one character is stuck (bugged, out of reach), lead with another one:
   `who="<name>"` on `travel_to`/`interact`/`jump_to`, or `set_party(action="select", who="<name>")`; `set_party(action="unchain", who=<stuck one>)` so the rest can leave. Progress with the rest of
   the party counts; the stuck one often catches up on its own or after a reload/regroup.
4. **Another objective:** park this one (objectives.md **Blocked**, what you tried, `place_marker`
   there) and go do something else from objectives.md, `get_frontier` or `get_journal`.
Then add one line to strategy.md "Harness and tool quirks" (`<tool> <symptom> → <what worked>`) and a
`[note] harness: …` line in the session log for the operator, and continue playing.

## 1. Stop and describe (write it in the session log)
`- HH:MM [stuck] trying <X>; tried <A>, <B>, <C>; symptom <what happens>`

## 2. Re-perceive (text first)
- `get_nearby` with a larger radius and all kinds: items, containers, doors, levers, ladders, NPCs.
  Is there something you dismissed as decoration?
- `get_events()` for anything you missed. `get_status()` shows the current screen; for a popup or
  screen no tool covers, `ui_dump()` lists its text and buttons, and `ui_action(command=…)` presses
  one.
- `OBJECTIVE` lines (`get_map`) for where the game wants you; `walk_toward(objective="o1")` or
  `walk_toward(direction=…)` to probe a direction.
- `route(objective="o1")` (or `target=`/x,y,z) when the way is the problem: how high the goal is, the closest
  walkable spot, which ladder/door leads there, fire on the way, and a NEXT step. Before any screenshot.
- `get_frontier()` for exits and leads you haven't used; `path_preview(target=…)` to check whether a
  spot is reachable at all, or needs a jump.
- `get_journal` for the quest text: what does the game itself say to do?
- Re-read the npcs, locations and quests entries for this place, and hypotheses.md.
- **Only if** the text still leaves the layout or UI ambiguous (a visual puzzle, a blocked path you
  can't explain, a modal the tools don't describe): one `screenshot`, then `zoom` on the specific
  detail.

## 3. Question assumptions
- List the assumptions behind your current approach. Which one is a `[guess]` or `[wiki]`? Recall may
  be from a different patch or version. Write the weakest assumption into hypotheses.md as `H-n` with
  a test.
- Have you talked to everyone nearby, including after events changed? Did you read every note or book
  here?

## 4. List three *different* approaches and pick an untried one
Categories to draw from:
- **Another route:** another door, a ladder, a jump, climbing, a different region entrance, the
  Underdark or a back path.
- **Another means:** a key, lockpick, force, a spell (Knock, Misty Step, Feather Fall), a different
  party member's skill, an item.
- **Another person:** someone else who knows. Revisit an NPC, try different dialogue options, or talk
  with a different speaker.
- **Another order:** do a different objective first (you might be under-levelled or missing an item).
  Long rest if story time needs to pass.
- **Combat changes:** level up, buy potions or scrolls, a different party, a pre-fight setup, avoid
  the fight, or talk your way out.
- **Harness problem:** a tool may be misbehaving. Retry with a different target or arguments, then
  try `ui_dump` → `ui_action`. As a last resort, use `click(x, y)` or `press_key(key)` on what a
  screenshot shows. Log a
  `[note] harness: <tool> <symptom>` line, and add it to strategy.md "Harness and tool quirks".

## 5. Time-box and park
- Give the new approach ~10 minutes (a harness rung: ~2 minutes). If it fails, try the next one.
- After three failed approaches: move the objective to objectives.md **Blocked** with what you tried,
  and switch to another objective. Coming back later with more levels, items or knowledge often works.
- Before parking: `place_marker` at the obstacle with a short label.
- Parking is not stopping: pick the next objective and play on until WRAP UP NOW.

## 6. Learn
Once unstuck, write a one-line lesson in strategy.md (what finally worked, and the general rule
behind it) and close or update the hypothesis.
