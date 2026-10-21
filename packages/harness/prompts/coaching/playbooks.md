## Playbooks (read on demand)
Procedures live in `knowledge/playbooks/<name>.md`. They are read-only, and you have no skills or slash commands.
**"Run the X playbook"** (or "the X skill" in an older message) means: `memory_read("knowledge/playbooks/X.md")`
**once** when its moment comes, then follow it. Don't re-read one that is still in your context. Don't
read them all up front.

| Moment | Playbook |
|---|---|
| session start, right after the memory injection, before any game action | `session-start` |
| a new game whose kickoff says CHARACTER CREATION with a character brief (before `new_game`) | `character-creation` |
| the injection says COMPACTION DUE (after session-start) | `compaction` |
| combat starts or is about to start (`INTERRUPTED — combat`, hostiles, you plan to attack) | `combat` |
| a conversation opens or you're about to start one with a named NPC | `dialogue` |
| out of combat and dialogue: where to go, looting, locks and traps, camp, trading | `exploration` |
| a `LEVEL UP:` line (level up at the next out-of-combat moment, `level_up(who)` from your party-build.md plan), a `PARTY PLAN` line (write the plan lines / composition while walking), companion choice, spell preparation, a build revision | `party-and-leveling` |
| the stuck criteria below | `stuck` |
| the `⏱` line says WRAP UP, or a SESSION CLOCK reminder | `session-wrapup` |
| the `⏱` line says "no story progress for N min" | `stuck` |
