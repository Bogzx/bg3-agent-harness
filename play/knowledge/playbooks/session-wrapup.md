# Session wrap-up (target: ≤ 8 minutes)

**When:** Run only when the clock line in tool results says **WRAP UP NOW** (10 min before the end, a third of a short session; or a SESSION CLOCK reminder in manual sessions) or the operator asks you to stop. Never earlier: "⏱ 14m left · 4m of play" means play 4 more minutes. Escaping real danger is play, not wrap-up. Reaches a safe state, saves, writes HANDOFF.md, trims memory to budget, then ends the turn so the Stop hook lints and commits the memory.

## 1. Reach a safe state (≤ 3 min)
- **In combat:** finish it if you can win within a few turns. Otherwise keep playing it. Never save-quit
  mid-fight on purpose. If the clock hits the hard limit mid-combat, save anyway (if the game allows)
  and write the situation precisely in HANDOFF.
- **In dialogue:** finish the conversation (the `dialogue` playbook rules still apply).
- Then don't start anything new: no travel, no new conversation, no long rest unless it was the plan.

## 2. Save
`save_game(label="Sxxx-end")`. Check the result says `done`, then log
`- HH:MM [save] Sxxx-end at <region>`. Note where you are for the handoff with `get_status`.

## 3. Update curated files (only what changed this session)
Go through your session log top to bottom and make sure each event is reflected:
- objectives.md: completed goals removed (they're in the log); new ones added with a *why*; blocked
  ones explained.
- decisions.md: every irreversible choice this session has an entry with the consequences you saw.
- npcs.md / locations.md / quests.md: new names, leads, dead ends, all tagged `[seen|told|wiki|guess Sxxx]`.
- hypotheses.md: close the ones you resolved (one line on how), and add new ones with a test.
- strategy.md: at most 3 new lessons. Merge duplicates. Promote one to PLAYTHROUGH "Top lessons" if it
  matters more than the current ones.
- PLAYTHROUGH.md: update the `Now:` line (act, region, `Last session: Sxxx`, `Save: Sxxx-end`), party
  lines and standing commitments.

## 4. Write HANDOFF.md (rewrite the whole file)
```
# Handoff Sxxx → Syyy  (YYYY-MM-DD HH:MM, in-game day N if known)
Save to load: "Sxxx-end" — <where the party stands; in or out of combat; anything open>

## Doing right now
<1–3 lines: the immediate task and its state>

## Next steps
1. <concrete next action, with tool-level detail if useful>
2. …
3. …

## Watch out
- <dangers, approval risks, resources low, timers> [tag]

## Unresolved
- <open threads and hypotheses to test, with IDs>

## Session stats
Actions ~N · combats N (won/lost) · downs N · reloads N · new quests N · files edited: …

## Discrepancies found this session
- <from session-start reconciliation, or "none">
```
Required by lint: the heading with *this* session's number, the `Save to load:` line, and the
`## Doing right now` and `## Next steps` sections. Stay ≤ 60 lines and ≤ 3500 characters.

## 5. End your turn: the Stop hook lints and commits
You have no shell. When everything above is done, **end your turn** with a one-line summary. The Stop
hook then:
- lints the memory (wrap-up phase) and, if it passes, closes the session and runs
  `git add -A && git commit -m "Sxxx"`. You'll see "Memory for Sxxx committed". Then stop; take no
  further game actions.
- if it fails, blocks the stop and sends you the lint report. Fix each error, then end your turn
  again:
  - **handoff:** the heading must name this session; `Save to load:`, `## Doing right now` and
    `## Next steps` must be present.
  - **budget:** condense; move detail out of always-loaded files; archive finished regions or NPCs.
  - **provenance:** add the missing tag to each listed line.
  - **deletion:** you removed lines from decisions.md or npcs.md. The report quotes them. Put them
    back, condensed under `## Archive`. Only if condensing is truly intended, add an
    `ARCHIVE: <why>` line.
  - **append-only:** you changed an old session log line. The report quotes the original lines;
    restore them exactly.

After 4 failed attempts the hook gives up and leaves the session open for the operator. Avoid that by
fixing the errors the edit-time lint hook already reported during the session.
