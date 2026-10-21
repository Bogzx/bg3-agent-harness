## Core loop
1. **Observe** once: `look` (a delta), plus the one mode-specific tool you need.
2. **Decide:** a short plan tied to an objective in `memory/objectives.md`.
3. **Act** with the highest-level tool that fits: a macro for a chore (`loot_area`, `travel_to`,
   `dialogue_advance`, …), `combat_turn` for a combat turn, `queue_actions` for other routine multi-step
   plans (then go straight back to step 1 while they run), `do_actions` for a short blocking chain.
4. **Read the delta.** Handle interrupts (`queue_interrupted`, combat, dialogue) first.
5. **Record** noteworthy events during the next walk, queue or enemy turn, not between actions (see Memory).

Never wait for, watch or screenshot a cutscene: read the `dialogue_line` events.

Don't call `session_clock` or guess the time: **play until the `⏱` line itself says WRAP UP NOW**, and keep playing
the story (the next door, descent, talk or fight) until then, not loot, inventory or waiting. Nothing about the
wrap-up (no `Sxxx-end` save, no HANDOFF edit, no end_session, no "running out of time" plans) before it.
**A session never ends mid-fight**, so reloading, fleeing or rushing a fight is never about time. When the line says
**"no story progress for N min"**, run the `stuck` playbook now.
