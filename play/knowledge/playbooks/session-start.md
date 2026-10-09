# Session start (target: ≤ 5 minutes)

**When:** Run at the start of every play session, right after the memory injection. Loads or verifies the save, reconciles memory against live game state, writes the session goal. Use before taking any game action.

**Then play until the `⏱` line says WRAP UP NOW.** The minutes it shows include the wrap-up time; the
"Nm of play" part is yours to play, however short the session is. Don't run `session-wrapup` early.

The hook has already opened `memory/sessions/Sxxx.md` and shown you PLAYTHROUGH.md and HANDOFF.md.
`Sxxx` below is the session number from the injection header.

## 1. Read
- `memory/objectives.md` (the whole file) and `memory/hypotheses.md` (the Open section).
- If the injection said the previous session ended WITHOUT wrap-up: also read that session's log
  (`memory/sessions/S<prev>.md`). Treat its uncommitted notes as unverified.
- If it said COMPACTION DUE: finish this playbook, then run `compaction` before playing.

## 2. Get the game into the handoff state
0. **Brand-new playthrough** (PLAYTHROUGH.md says "Character: (not created yet)" and there are no saves
   in `list_saves`): if you're at the main menu, run `new_game` with the difficulty from
   `knowledge/setup.md`, then `create_character` with the build from setup.md, **named exactly as the
   operator configured (default "Claude")**: save folders are matched by that name. If character
   creation is already open or done, continue from there. Record the build in PLAYTHROUGH.md and
   party-build.md, then play the prologue.
1. Call `get_status`.
2. If the game is at the main menu, or somewhere other than HANDOFF says: `list_saves()`, then
   `load_game(save="<folder>")` for the save named under "Save to load:" (fall back to the newest
   `S<prev>-*` save, then `load_game(save="latest")`). Log the save you loaded.
3. If a dialogue or combat is in progress, deal with it first (`dialogue` or `combat` playbook), then
   come back here.

## 3. Reconcile memory against live state
Call `get_party()`, `get_journal()`, `get_inventory(filter="<key item words>")` and `get_map()`.
Four reads, no more. `memory/snapshots/` shows what they said at the end of the last session, which
helps spot changes. Compare them with:

| Memory claim | Check against |
|---|---|
| HANDOFF "Doing right now" and location | `get_status` region and position |
| PLAYTHROUGH party lines (who is active, who is at camp) | `get_party` |
| objectives.md (each goal's quest still active?) | `get_journal` |
| inventory-notes.md "Keep" items (still owned? who carries them?) | `get_inventory` |
| party-build.md levels or plans vs actual level | `get_party` / `get_character` |
| locations.md waypoint list | `get_map` (discovered waypoints) |

For **every mismatch**:
- Fix the curated note. Live state wins.
- Append `- HH:MM [discrepancy] <what the note said> → <what the game shows>` to the session log.
- If the mismatch shows a habit (e.g. you keep forgetting who holds key items), add a lesson to
  strategy.md.

Don't hunt for tiny differences in numbers. Notes shouldn't contain stats anyway; if they do, delete
those stats.

## 4. Commit to a goal
- Pick the session goal: normally HANDOFF "Next steps" #1, unless reconciliation changed the picture.
- Replace the `Goal:` line at the top of `memory/sessions/Sxxx.md` (the only line in a session log you
  may rewrite) with one concrete sentence, e.g.
  `Goal: free the gnome at the windmill, then reach the Underdark entrance via the Blighted Village well.`
- `save_game(label="Sxxx-start")`.
- Log `- HH:MM [save] Sxxx-start at <region>`.

## 5. Open hypotheses
If a hypothesis is older than 3 sessions (lint warns about it), plan to test or close it this session,
and say so in the goal line.

Now play (core loop in CLAUDE.md).
