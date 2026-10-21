## Memory protocol
The harness commits `memory/` once per session (at `end_session`). `memory_write` is for whole files (HANDOFF.md at
wrap-up), `memory_edit` for one exact piece, `memory_append` and `note` for log lines.

Files and their jobs:

| File | Holds | Write style |
|---|---|---|
| CHARACTER.md | who you play: name, build, look, voice, backstory, values, flaws, goals, won't do, how they decide | written once at character creation; then only append to `## Character development` |
| PLAYTHROUGH.md | index, "you are here", party roles, commitments, top lessons | rewrite, ≤120 lines |
| HANDOFF.md | baton for the next session | rewrite at wrap-up, ≤60 lines |
| objectives.md | goal stack with a *why* per goal | rewrite |
| hypotheses.md | beliefs under test, each with a test | rewrite; close or drop |
| strategy.md | lessons, playbook, harness quirks | rewrite, ≤150 lines |
| lessons.md | fight lessons `- WHEN <trigger>: <failed> → <instead> (Sxxx)`; the harness shows matching ones at a fight start (`LESSONS:`) | the keeper distills your `[lesson]`/`[fight]` notes; ≤60 |
| decisions.md | irreversible choices, why, consequences seen | append; never delete |
| npcs.md / locations.md / quests.md | facts and leads by name, region, quest | edit; archive, don't delete |
| party-build.md / inventory-notes.md | build *intentions*, item *judgments* | rewrite |
| sessions/Sxxx.md | this session's log | **append-only**, one line per event |
| snapshots/ | generated from live state | **never edit** |
| markers.json | map markers | only via `place_marker`/`delete_marker` |

What you learn goes in `memory/` (strategy.md, lessons), never in `knowledge/`.

**Never hand-copy what the game tracks** (HP, stats, inventory, gold, journal text, waypoints). Query
it, or read `snapshots/`. Notes hold what the game doesn't: intentions, judgments, promises, suspicions,
plans, reasons and lessons.

**Write notes while the game works, never between two game actions.** Note-writing is slow (a memory_edit
turn costs 5–15 s), so do it while the party walks, a queue runs or enemies take their turn: issue
`travel_to` / `queue_actions` / `end_turn` first, then the memory_edit, then `wait_for`. A result that says
`NOTES WINDOW: ~20s of walking left` is that moment: write the notes now. Log lines go through
`note("[tag] …")` (instant, no memory_read/memory_edit) or ride on the walk itself: `travel_to(…, note="[quest] …")`;
edit a curated file only when the plan changes (the memory keeper folds the log in between sessions);
a note may lag the event by a few minutes, but not past the next walk or enemy turn. Write a note after
any of these:
- a quest update
- a decision
- meeting a named NPC
- a promise
- a down or death
- a solved puzzle
- a new waypoint
- a hypothesis confirmed or refuted
- a reload
- a new lock, trap or lead

**Provenance tags** are required on fact bullets in npcs, locations, quests, decisions and hypotheses:
- `[seen Sxxx]` you observed it in this game
- `[told Sxxx]` a character said it (they may lie)
- `[wiki]` general knowledge (your own or `wiki_search`/`wiki_page`), not yet confirmed in this game
- `[guess]` your inference

A `[wiki]` or `[guess]` item is never the sole reason for a Main objective. Put it in hypotheses.md
with a test.

**Live state beats notes.** On a mismatch, fix the note and log a `[discrepancy]` line.

Never delete facts from decisions.md or npcs.md. Move them to `## Archive`, or add an `ARCHIVE: <why>`
line when condensing. Fix the lint errors a memory tool's result shows when they appear.
