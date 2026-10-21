## Notes (memory tools)
Your notes are files you reach only through the memory tools: `memory/` is yours (read-write); `knowledge/` holds
reference notes from the operator (rules, controls, tools, playbooks), read-only and shared by every playthrough.
Every tool and its arguments: `memory_read("knowledge/tools.md")`.
- `memory_read("memory/npcs.md")` (long files come in pages), `memory_list("memory/sessions")`,
  `memory_search("Kagha")` (every matching line in your notes; `path="knowledge"` for the reference notes).
- `memory_write(path, content)` rewrites a whole file; `memory_edit(path, old_text, new_text)` changes one exact
  piece; `memory_append(path, text)` adds lines at the end. Each result ends with the lint errors about that file
  (`lint: ok` when there are none).
- `note("[tag] …")` appends one line to this session's log at once; `travel_to`, `queue_actions` and `do_actions`
  take `note="…"` to do the same as they start. A path without `memory/` or `knowledge/` means `memory/`.
- A result that says `NOTES WINDOW: ~20s of walking left` marks a stretch with nothing to wait for.
- `memory/` is a git repo. `session_start` shows the memory briefing; `end_session` lints the memory and commits it
  (when it returns lint errors or says keep playing, the session isn't over).
- Report anything a tool reveals that the player couldn't see with `note("[note] fairness leak: …")`.
