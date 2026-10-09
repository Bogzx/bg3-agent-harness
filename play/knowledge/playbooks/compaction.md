# Memory compaction (target: ≤ 15 minutes, done before playing)

**When:** Use when the session-start injection says COMPACTION DUE (every ~10 sessions), or when lint reports budget errors you can't fix by small trims. Condenses old session logs into a chapter file and prunes curated memory without losing facts.

Session logs are never deleted or edited. Compaction **summarises** them into a chapter and **prunes**
the curated files. Facts from decisions.md and npcs.md are archived, never deleted.

## 1. Write the chapter
- The sessions to cover are those after the last chapter file and before the current session. The
  injection names the last compacted session; `Glob` with path `memory` and pattern `chapters/*.md` or
  `sessions/S*.md` lists the files.
- Read those `sessions/Sxxx.md` logs in order.
- Create `memory/chapters/C<nn>_S<first>-S<last>_<slug>.md`, e.g.
  `C03_S021-S030_act1-underdark.md`. The `S<first>-S<last>` part is required: tools use it to know what
  has been compacted.
- Content (≤ 150 lines):
  ```
  # Chapter 3 (S021–S030): Act 1, Underdark and Grymforge
  ## Story so far
  <10–20 lines: what happened, in order>
  ## Key decisions
  - D-11 … (see decisions.md)
  ## Lessons from this chapter
  - …
  ## Loose ends carried forward
  - …
  ```
- Write only what the logs and notes support. Don't add details from memory.

## 2. Prune the curated files
- **locations.md:** collapse finished regions to ≤ 3 lines ("done; merchant X; nothing left except Y").
- **quests.md:** finished quests → one line each under Done.
- **npcs.md:** NPCs who are dead, gone, or irrelevant for the rest of the game → move their entries
  under `## Archive`, condensed to 1–2 lines with their tags kept. If you condense more than 10 lines,
  add a line `ARCHIVE: condensed <who> during compaction C<nn>`.
- **decisions.md:** keep every decision. Old ones may be condensed into `## Archive` the same way, with
  an `ARCHIVE:` line.
- **hypotheses.md:** remove Closed entries whose lesson is already in strategy.md; drop stale open ones
  (note "dropped" in the log).
- **strategy.md:** merge duplicates, delete lessons that turned out wrong (log why), keep ≤ 150 lines.
- **PLAYTHROUGH.md:** refresh the index lines, standing commitments, and top 3 lessons.

## 3. Check and log
- The lint hook reports problems after each edit. Fix every error it shows.
- Log `- HH:MM [note] compaction: wrote chapters/C<nn>_…; archived N NPCs; collapsed N regions`.
- Continue with normal play. The commit happens at wrap-up.
