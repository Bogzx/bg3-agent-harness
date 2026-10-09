# play/ — working directory of the PLAYING agent

Operator notes (the agent reads CLAUDE.md, not this file).

1. Create a playthrough: `python3 tools/memory/new_playthrough.py <name>`. This creates
   `playthroughs/<name>/memory` (its own git repo) and points `play/memory` at it.
2. Start Claude Code in `play/` with the runner's flags (`runner/play.py` passes them itself):
   `claude --disable-slash-commands --exclude-dynamic-system-prompt-sections --setting-sources project,local`.
   There are no skills: the procedures are playbooks in `knowledge/playbooks/` (read-only for the agent),
   and CLAUDE.md says when to read each one. Hooks (`.claude/settings.json`) do the following:
   - SessionStart: `session.py hook` opens `sessions/Sxxx.md` and injects PLAYTHROUGH.md and
     HANDOFF.md. On resume or compact it re-injects them without opening a new session.
   - PreToolUse: `fairness_guard.py` allows only the `mcp__bg3__*` tools, todos, and file
     tools under `memory/` and `knowledge/` (writes are refused under `knowledge/playbooks/`). Everything else, including Bash, Monitor, web and
     subagents, is blocked. `pace_guard.py` (move_to/walk_toward/travel_to) drops `wait=true` when the
     bridge's activity sampler (`$BG3AI_METRICS_PATH`) freshly shows no combat and no dialogue, and
     tells Claude why: walks stay async so the character moves while the model thinks.
   - PostToolUse: on bg3 tools, a wrap-up reminder after 50 min (`BG3_WRAP_MINUTES`,
     `BG3_SESSION_MINUTES`). On Edit/Write, lint errors for the edited memory file.
   - Stop: once wrap-up is due (or, in manual sessions, HANDOFF.md names the current session), it lints.
     Autonomous sessions (`BG3AI_SESSION_DEADLINE`) get "keep playing" blocks before the runner's
     wrap-up time (`BG3AI_SESSION_WRAPUP`, else 3 min before the deadline),
     even with HANDOFF.md written. On errors it blocks
     the stop and feeds Claude the report; on success it commits `Sxxx`. It gives up after 4 blocks.
   - SessionEnd: commits if wrap-up was finished; otherwise it leaves the session open, and the next
     start flags it.
3. Operator tools (the agent has no shell): `python3 tools/memory/lint.py play/memory`,
   `python3 tools/memory/session.py status|end [--human-override] --memory-dir play/memory`.
4. Optional: put operator instructions (difficulty, reload policy changes) in
   `knowledge/operator.md`.

`.mcp.json` launches `uv run --project ../bridge bg3ai-mcp` (server name `bg3`). It has a 110 s tool
timeout, which keeps calls under Claude Code's 2-minute auto-background threshold.
