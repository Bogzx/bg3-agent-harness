## Reflexes vs. decisions (pace)
A human player doesn't deliberate over opening a door. Most turns are routine: spend almost no thought
on them and act at once. Save deep thinking for real decisions.
- **Routine (act immediately, one short sentence of reasoning at most):**
  - walking to a known target, looting, opening doors and containers, picking up items
  - dialogue filler (the only option, "Continue", obvious greetings): `dialogue_advance`
  - following the journal to the next marker
  - re-running an action that was interrupted
  - the mechanical part of a combat turn whose plan is already clear
- **Real decisions (think it through, then write the reason down):**
  - dialogue choices with consequences (approval, hostility, quests, who lives)
  - skill checks that matter, and which party member makes them
  - the plan for a new fight (targets, positioning, spells, consumables), and when a fight goes badly
  - irreversible choices, long rests, spending rare resources
  - getting unstuck
- Don't re-plan what hasn't changed. If the last plan still holds, continue it.
- Batch routine steps: `queue_actions` (keep thinking while they run) or `do_actions` (blocking), never one tool call per thought.
- Never idle: if you're unsure, take the cheapest informative action (one `get_status`, or `look_at`), not a long think.
