# Session start (target: ≤ 1 minute to the first game action)

**When:** every play session, right after the memory injection (PLAYTHROUGH.md + HANDOFF.md; none →
`session_start`). Then play until the `⏱` line says WRAP UP NOW. That is enough to start: don't read other
memory files up front; `memory_read` one when the game sends you to it (objectives.md before choosing a new
goal, npcs.md before talking to someone you've met).

## 1. Get the game into the handoff state (one or two calls)
- `mcp__bg3__get_status` (full name; the runner may already have loaded the save: then don't reload).
- At the main menu, or somewhere other than HANDOFF says: `load_game(save="<Save to load>")` (fall back
  to the newest `S<prev>-*` save, then `"latest"`).
- **Brand-new playthrough** (PLAYTHROUGH.md says "Character: (not created yet)" and `list_saves` has
  none): with a character brief in the kickoff (CHARACTER CREATION), run the `character-creation` playbook
  now. Without one: `new_game` with the difficulty from `knowledge/setup.md` (Explorer), then
  `create_character` with that default build, **named exactly as the kickoff says** (no name in the kickoff:
  choose a distinctive one; it becomes the playthrough's save owner). Record the build in
  PLAYTHROUGH.md and party-build.md, then play the prologue.
- Dialogue or combat in progress: handle it first (the `dialogue` or `combat` playbook).
- The injection has a **snapshots/gear.md** block, or get_status shows `ALERTS … better gear in bags`:
  `equip_best()` (one call, out of combat). ALERTS low HP → `short_rest` before the first fight.

## 2. Start moving, then write the goal
Issue the first action from HANDOFF "Next steps" #1 (e.g. `travel_to`). **While it runs**, make one
`memory_edit` of `memory/sessions/Sxxx.md` replacing only the `Goal: (write one line …)` line with
`Goal: <one concrete sentence>`. Every other log line goes through `note("[tag] …")` (or `note=` on a walk).
Then `save_game(label="Sxxx-start")` at the next calm moment and log `- HH:MM [save] Sxxx-start at <region>`.

If the injection says **STALE NOTES** with objectives.md in it: during the first long walk, rewrite
objectives.md from HANDOFF.md (Main goal, why, next step; drop finished goals). One `memory_write`, ≤ 15 lines.

## 3. Reconcile lazily
No reconciliation pass. When live state contradicts a note (party, quest, item, place), live state wins: fix
the note and log `[discrepancy] <note said> → <game shows>` while the game works.

Extra cases:
- The injection says the previous session ended WITHOUT wrap-up: its HANDOFF may be stale, so trust
  `get_status`; HANDOFF's last section `## From the logs (memory keeper)` already has that log's last state
  and next step. Don't read old `sessions/Sxxx.md` logs during play (the keeper copies what you need into
  HANDOFF, objectives.md and quests.md between sessions); read `S<prev>.md` only if that section is missing.
- It says COMPACTION DUE (only when no memory keeper runs): run `compaction` during the first long walk.
- A hypothesis is older than 3 sessions (lint warns): test or close it this session.
