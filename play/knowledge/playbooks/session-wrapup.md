# Session wrap-up (target: ≤ 3 minutes)

**When:** only when the clock line in tool results says **WRAP UP NOW** (3 min before the end; or a
SESSION CLOCK reminder in manual sessions) or the operator asks you to stop. Never earlier: "⏱ 5m left ·
2m of play" means play 2 more minutes. Being stuck is not a reason (run `stuck`, section 0). Escaping
danger is play. Don't save `Sxxx-end` or rewrite HANDOFF.md before WRAP UP NOW.

Your notes were written during play (CLAUDE.md, Memory protocol), so wrap-up is just the following.
No reading through files, no tidying.

## 1. Safe state (≤ 1 min)
- **Combat:** finish the current round. If the fight goes on, save anyway (if the game allows) and write
  the situation precisely in HANDOFF.
- **Dialogue:** finish the conversation (the `dialogue` playbook rules still apply).
- Start nothing new.

## 2. Save
`save_game(label="Sxxx-end")` (returns at once; `list_saves` then shows it, and a later result says SAVED). Save `Sxxx-end` once, not again after every note.

## 3. Write (one call each, while nothing else runs)
- **HANDOFF.md:** rewrite the whole file with one Write, using the template below.
- **PLAYTHROUGH.md:** the `Now:` line (act, region, `Last session: Sxxx`, `Save: Sxxx-end`), plus party
  lines only if they changed.
- **objectives.md** if a goal finished or changed and you haven't noted it yet, and in any case when it
  is 3 sessions old (lint then refuses the wrap-up until it is current). Same for decisions.md
  (irreversible choices this session need an entry).
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
Lint requires the heading with *this* session's number, the `Save to load:` line, and the
`## Doing right now` and `## Next steps` sections. Stay ≤ 60 lines and ≤ 3500 characters.

## 4. End your turn
End your turn with a one-line summary. The Stop hook lints the memory and commits it ("Memory for Sxxx
committed"). Then stop and take no further game actions. If it sends back a lint report instead, fix
each error and end your turn again:
- **handoff:** the heading must name this session; `Save to load:`, `## Doing right now` and
  `## Next steps` must be there.
- **budget:** condense; move detail out of always-loaded files.
- **provenance:** add the missing tag (`[seen Sxxx]`, `[told Sxxx]`, `[wiki]`, `[guess]`) to each listed line.
- **deletion:** you removed lines from decisions.md or npcs.md. Put the quoted lines back, condensed
  under `## Archive`, or add an `ARCHIVE: <why>` line if condensing is intended.
- **append-only:** you changed an old session-log line. Restore the quoted original exactly.

After 4 failed attempts the hook gives up and leaves the session open for the operator.
