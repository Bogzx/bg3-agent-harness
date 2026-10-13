# play/ — working directory of the PLAYING agent

Operator notes (the agent reads CLAUDE.md, not this file).

1. Create a playthrough: `python3 tools/memory/new_playthrough.py <name>`. This creates
   `playthroughs/<name>/memory` (its own git repo) and points `play/memory` at it.
2. Start the agent in `play/` with the runner (`runner/play.py run --playthrough <name>`), which passes
   `claude -p --strict-mcp-config --mcp-config play/.mcp.json --tools ToolSearch --disable-slash-commands
   --exclude-dynamic-system-prompt-sections --setting-sources project,local`. `--tools ToolSearch` removes every
   built-in tool: the agent has only the bg3 MCP tools. There are no skills: the procedures are playbooks in
   `knowledge/playbooks/` (read-only), and CLAUDE.md says when to read each one.

   The harness is the bg3 MCP server's, so any MCP client gets the same one (`bridge/src/bg3ai/memtools.py`):
   - **Memory tools** `memory_list/read/search/write/edit/append` (+ `note` for the session log) reach only
     `memory/` (read-write) and `knowledge/` (read-only). Refused: paths outside them, `..`, symlinks, hidden and
     git files (`.git`, `.gitattributes`, `.session.json`), `snapshots/`, `markers.json`, rewriting a session log,
     and changing CHARACTER.md once committed other than appending development lines. Every write's result ends
     with the lint errors about that file.
   - **Session start.** The runner opens `sessions/Sxxx.md` (`session.py start`) and puts the briefing
     (CHARACTER.md, PLAYTHROUGH.md, HANDOFF.md, last game state, harness news) at the top of its kickoff; it sets
     `BG3AI_SESSION` so the server never opens a second one. Without the runner, the first tool call opens the
     session and its result starts with the briefing; `session_start` shows it again (after a compaction).
   - **Clock.** Runner sessions: the ⏱ line in results (`BG3AI_SESSION_DEADLINE`, `BG3AI_SESSION_WRAPUP`).
     Manual sessions: a wrap-up reminder after 50 min (`BG3_WRAP_MINUTES`, `BG3_SESSION_MINUTES`).
   - **Pace guard.** `move_to`/`walk_toward` ignore `wait=true` outside combat (travel_to never blocks) and say
     so: walks stay async so the character moves while the model thinks.
   - **Session end.** `end_session` (the wrap-up's last step) makes the old Stop hook's check: before wrap-up time
     "keep playing"; HANDOFF.md not written for this session → write it; lint errors → the report; else close
     and commit `Sxxx`. It gives up after 4 blocked attempts. Autonomous sessions (`BG3AI_SESSION_DEADLINE`)
     refuse early ends before the runner's wrap-up time (`BG3AI_SESSION_WRAPUP`, else 3 min before the
     deadline), even with HANDOFF.md written. When claude ends its turn anyway, the runner makes the same check
     and resumes the conversation with the reason.
   - `.claude/settings.json` is only defence in depth: it allows `mcp__bg3__*` and denies the built-in tools
     again. No hooks.
3. Operator tools (the agent has no shell): `python3 tools/memory/lint.py play/memory`,
   `python3 tools/memory/session.py status|end [--human-override] --memory-dir play/memory`.
4. Optional: put operator instructions (difficulty, reload policy changes) in
   `knowledge/operator.md`.

`.mcp.json` launches `uv run --project ../bridge bg3ai-mcp` (server name `bg3`). It has a 110 s tool
timeout, which keeps calls under Claude Code's 2-minute auto-background threshold. A client in a container
reaches the same server over HTTP instead: `tools/container/README.md`.
