# Session wrap-up (target: ≤ 3 minutes)

**When:** only when the `⏱` line says **WRAP UP NOW** (or a SESSION CLOCK reminder, or the operator asks).
"⏱ 5m left · 2m of play" means play 2 more minutes; stuck is not a reason (run `stuck`); escaping danger is
play. One exception: with "5m of play" or less and a point of no return next (a descent, a boss, a long trip, a
new region), wrap up now (`end_session` accepts it). Never kill time with get_status/wiki reads (S036 idled
~2.5 min): wrap up, or do reversible useful work (sell/stash junk, equip_best, level plans, loot nearby, notes).
**Do it once.** If `end_session` or the runner says "Keep playing", play on: no second `Sxxx-end` save unless the
party moved, no HANDOFF rewrite (one `memory_edit` of a changed line is fine). No reading through files, no tidying.

## 1. Safe state (≤ 1 min)
- **Combat:** finish the current round. If the fight goes on, save anyway (if the game allows) and write the
  situation precisely in HANDOFF.
- **Dialogue:** finish the conversation (`dialogue` playbook rules apply). Start nothing new.

## 2. Save
`save_game(label="Sxxx-end")` once. A later result says SAVED ("queued" with a dialogue on screen is fine: it
saves as soon as it can). No `list_saves` check, no re-save after notes (S025 saved 4 times, checked 3 times).

## 3. Write (one call each)
- **HANDOFF.md:** one `memory_write("memory/HANDOFF.md", …)` with the template below.
- **PLAYTHROUGH.md:** the `Now:` line (act, region, `Last session: Sxxx`, `Save: Sxxx-end`); party lines only if
  they changed.
- **objectives.md** if a goal finished or changed and isn't noted yet, and in any case when it is 3 sessions
  old (lint refuses the wrap-up until it is current). decisions.md: an entry per irreversible choice this session.
- One log line: `- HH:MM [save] Sxxx-end at <region>`.

```
# Handoff Sxxx → Syyy  (YYYY-MM-DD HH:MM)
Save to load: "Sxxx-end" — <where the party stands; in or out of combat; anything open>

## Doing right now
<1–3 lines: the immediate task and its state>

## Next steps
1. <concrete next action, with tool-level detail if useful>
2. …

## Watch out
- <dangers, resources low, harness quirks seen this session> [tag]
```
≤ 60 lines and ≤ 3500 characters.

## 4. end_session, then end your turn
`end_session` lints and commits ("Memory for Sxxx committed … Session over: end your turn now"); then end your
turn with a one-line summary, no more game actions. A lint report instead: fix each error, call it again:
- **handoff:** the heading names this session; `Save to load:`, `## Doing right now`, `## Next steps` present.
- **budget:** condense; move detail out of always-loaded files.
- **provenance:** add the missing tag (`[seen Sxxx]`, `[told Sxxx]`, `[wiki]`, `[guess]`) to each listed line.
- **deletion:** lines removed from decisions.md or npcs.md: put them back, condensed under `## Archive`, or add
  an `ARCHIVE: <why>` line.
- **append-only:** an old session-log line changed: restore the quoted original exactly.

After 4 failed attempts it gives up and leaves the session open for the operator.
