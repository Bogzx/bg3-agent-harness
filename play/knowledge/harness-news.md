# Harness news (operator-written; the agent reads, never edits)

<!--
Changes to the tools/bridge/mod that make older notes or lessons wrong. The session-start hook
(tools/memory/session.py) injects the entries newer than the previous session's start, at most 6.
One line each: "- YYYY-MM-DD HH:MM <what changed; which old lesson it retires>". Newest last.
-->

- 2026-10-10 01:30 "character is in a conversation" with no dialogue open was a stale-dialogue harness bug, now fixed; it is NOT a tutorial popup, and dismiss_popups is not the fix (retires the S002 lesson).
- 2026-10-10 01:30 level_up now offers spells, cantrips and invocations (Gale and Wyll leveled in S010); pass the picks from party-build.md.
- 2026-10-10 01:30 Weapon attacks via combat_turn/use_ability work (the S001 "range 0.0 m" failure is gone since S002); don't click enemies on screen.
