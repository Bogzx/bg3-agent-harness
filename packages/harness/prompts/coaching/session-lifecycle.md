## Session lifecycle
- **Start:** the harness opens `memory/sessions/Sxxx.md`, and your first message starts with the memory
  briefing: CHARACTER.md (once it exists), PLAYTHROUGH.md and HANDOFF.md. If it doesn't, call
  `session_start` before anything else. Run the `session-start` playbook before playing. If the briefing says
  COMPACTION DUE, run `compaction` next.
- **Wrap-up (3 min):** only when the `⏱` line says **WRAP UP NOW** or a SESSION CLOCK reminder
  appears. **Don't start wrap-up before that**, and don't save `Sxxx-end` or rewrite HANDOFF.md early:
  autonomous sessions block early stops. Nothing needs to fit in the session: the game keeps running between
  sessions and the next one starts about a minute later
  from where the party stands, so start the boss fight, descent or trip. A fight on at WRAP UP holds the session
  until it ends (WRAP UP NOW comes after it). Never idle with get_status/wiki
  reads. Retreating from real danger is play, not wrap-up. Because notes were written during
  play, wrap-up is short: finish the current fight round or conversation → `save_game("Sxxx-end")` →
  rewrite HANDOFF.md (one `memory_write`) → update PLAYTHROUGH.md's `Now:` line → `end_session`. Details
  (the HANDOFF template, lint fixes) are in the `session-wrapup` playbook. If `end_session` returns lint errors
  (or says keep playing), do that and call it again. End your turn once it reports the commit.
- **After context compaction:** call `session_start` (your memory briefing again), re-read
  `memory/sessions/Sxxx.md` and objectives.md with `memory_read`, then call `get_status`.
