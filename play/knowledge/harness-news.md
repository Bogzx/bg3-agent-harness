# Harness news (operator-written; the agent reads, never edits)

<!--
Changes to the tools/bridge/mod that make older notes or lessons wrong. The session-start hook
(tools/memory/session.py) injects the entries newer than the previous session's start, at most 6.
One line each: "- YYYY-MM-DD HH:MM <what changed; which old lesson it retires>". Newest last.
-->

- 2026-10-10 01:30 "character is in a conversation" with no dialogue open was a stale-dialogue harness bug, now fixed; it is NOT a tutorial popup, and dismiss_popups is not the fix (retires the S002 lesson).
- 2026-10-10 01:30 level_up now offers spells, cantrips and invocations (Gale and Wyll leveled in S010); pass the picks from party-build.md.
- 2026-10-10 01:30 Weapon attacks via combat_turn/use_ability work (the S001 "range 0.0 m" failure is gone since S002); don't click enemies on screen.
- 2026-10-10 01:53 Memory review: objectives/npcs/quests/locations/strategy/party-build rebuilt from your session logs; keep them current (lint warns when stale).
- 2026-10-10 03:16 A level up mid-play is now said within ~1 min on any result (LEVEL UP pending); NOTES WINDOWs add a PARTY PLAN line listing missing party-build.md plan lines (next 2 levels each) and an uncovered role. party-build.md needs `## Party composition` and `## Level-up plans` sections (memory-template format).
- 2026-10-10 09:45 Resources: an ALERTS line (low HP + short rests left, supplies, gold, Encumbered, better gear, partial rest); long_rest refuses a partial rest unless partial=true; get_inventory filter=food|junk|gear shows supply values/junk/upgrades; trade wares now have names (the "price=26 count=2" bug) and supply=N; new trade(sell="junk"), manage_inventory(op="camp", item="junk") and equip_best(); short_rest shows HP before→after. Retires "Send to Camp via I → right-click" (S023).
- 2026-10-10 10:05 New `note(text)`: appends `- HH:MM [tag] …` lines to your session log instantly (no Read/Edit); travel_to/queue_actions/do_actions take `note="…"` so a note rides on the action. Use it instead of Editing the session log. Character line of sight for los_spot steps is now stricter (centre rays): fewer "stepped 1m to see it" then no LOS.
