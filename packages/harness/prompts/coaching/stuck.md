## Stuck procedure
You are stuck if: three attempts at the same thing have failed, ~15 minutes have passed without
objective progress (the `⏱` line says "no story progress for N min"), or the same places or
conversation keep repeating. Then **stop repeating**:
1. Run the `stuck` playbook.
2. List three *different* approaches.
3. Pick an untried one.
4. Write it in hypotheses.md with a test.
5. If it still fails, park the objective under Blocked and work on another.

Persistence pays off only when the approach changes.

**Stuck never ends a session.** A loop, a bugged action or a blocked route is a reason to change
approach, never to wrap up early or stop: the session runs until the `⏱` line says WRAP UP NOW, and
`end_session` (and the runner, if you end your turn anyway) refuses an early stop even when HANDOFF.md is
already written. For a **harness bug**
(an action that loops, ignores orders, or a tool that keeps failing the same way), the `stuck`
playbook's harness ladder takes ~5 minutes: cancel and retry differently → reload the last good save →
continue with another party member → another objective. Then keep playing.
