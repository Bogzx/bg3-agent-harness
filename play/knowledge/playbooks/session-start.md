# Session start (target: ≤ 1 minute to the first game action)

**When:** at the start of every play session, right after the memory injection.

**Then play until the `⏱` line says WRAP UP NOW.** The minutes it shows include the 3-minute wrap-up;
the "Nm of play" part is all yours. Don't run `session-wrapup` early.

The hook has already opened `memory/sessions/Sxxx.md` and shown you PLAYTHROUGH.md and HANDOFF.md:
that is enough to start. Don't read other memory files up front. Read one only when the game sends you
to it (e.g. objectives.md before choosing a new goal, npcs.md before talking to someone you've met).

## 1. Get the game into the handoff state (one or two calls)
- `mcp__bg3__get_status` (full name; the runner may already have loaded the save: then don't reload).
- At the main menu, or somewhere other than HANDOFF says: `load_game(save="<Save to load>")` (fall back
  to the newest `S<prev>-*` save, then `"latest"`).
- **Brand-new playthrough** (PLAYTHROUGH.md says "Character: (not created yet)" and `list_saves` has
  none): `new_game` with the difficulty from `knowledge/setup.md`, then `create_character` with that
  build, **named exactly as the operator configured (default "Claude")**. Record the build in
  PLAYTHROUGH.md and party-build.md, then play the prologue.
- Dialogue or combat in progress: handle it first (the `dialogue` or `combat` playbook).

## 2. Start moving, then write the goal
Issue the first action from HANDOFF "Next steps" #1 (e.g. `travel_to`). **While it runs**, make one
Edit of `memory/sessions/Sxxx.md` that replaces only the `Goal: (write one line …)` line with one concrete
sentence (`Goal: <sentence>`, nothing else in that Edit). Log lines always go at the **end** of the file,
under `## Log` (S002 and S010 put them above it, next to the Goal). Then `save_game(label="Sxxx-start")`
at the next calm moment and log `- HH:MM [save] Sxxx-start at <region>`.

If the injection says **STALE NOTES** with objectives.md in it: during the first long walk, rewrite
objectives.md from HANDOFF.md (Main goal, why, next step; drop finished goals). One Write, ≤ 15 lines.

## 3. Reconcile lazily
No reconciliation pass. When live state contradicts a note during play (party, quest, item, place), live
state wins: fix the note and log `- HH:MM [discrepancy] <note said> → <game shows>`, written while the
game works, like every other note.

Extra cases:
- The injection says the previous session ended WITHOUT wrap-up: its HANDOFF may be stale, so trust
  `get_status` and read that session's log (`memory/sessions/S<prev>.md`) during your first walk.
- It says COMPACTION DUE: run `compaction` during the first long walk or at the first calm moment.
- A hypothesis is older than 3 sessions (lint warns): test or close it this session.
