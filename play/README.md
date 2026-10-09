# play/ — working directory of the PLAYING agent

Operator notes (the agent reads CLAUDE.md, not this file).

1. Create a playthrough: `python3 tools/memory/new_playthrough.py <name>`. This creates
   `playthroughs/<name>/memory` (its own git repo) and points `play/memory` at it.
2. Start Claude Code in `play/`. Hooks (`.claude/settings.json`) do the following:
   - SessionStart: `session.py hook` opens `sessions/Sxxx.md` and injects PLAYTHROUGH.md and
     HANDOFF.md. On resume or compact it re-injects them without opening a new session.
   - PreToolUse: `fairness_guard.py` allows only the `mcp__bg3__*` tools, skills, todos, and file
     tools under `memory/` and `knowledge/`. Everything else, including Bash, Monitor, web and
     subagents, is blocked.
   - PostToolUse: on bg3 tools, a wrap-up reminder after 50 min (`BG3_WRAP_MINUTES`,
     `BG3_SESSION_MINUTES`). On Edit/Write, lint errors for the edited memory file.
   - Stop: once wrap-up is due or HANDOFF.md names the current session, it lints. On errors it blocks
     the stop and feeds Claude the report; on success it commits `Sxxx`. It gives up after 4 blocks.
   - SessionEnd: commits if wrap-up was finished; otherwise it leaves the session open, and the next
     start flags it.
3. Operator tools (the agent has no shell): `python3 tools/memory/lint.py play/memory`,
   `python3 tools/memory/session.py status|end [--human-override] --memory-dir play/memory`.
4. Optional: put operator instructions (difficulty, reload policy changes) in
   `knowledge/operator.md`.

`.mcp.json` launches `uv run --project ../bridge bg3ai-mcp` (server name `bg3`). It has a 110 s tool
timeout, which keeps calls under Claude Code's 2-minute auto-background threshold.
