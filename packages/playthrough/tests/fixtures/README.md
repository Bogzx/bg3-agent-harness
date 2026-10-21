# Test fixtures

`run/` is a recorded playthrough, trimmed and scrubbed: claude-run-2 sessions S094-S096 (the final fight and the
epilogue), as the runner, bridge and memory left them on 2026-10-18.

- `run/runtime/`: `runner.status.json` (rewritten: the S096 session just ended), `actions.jsonl` (260 of S096's tool
  calls, results cut at 1500 characters), `sessions/claude-run-2/` (runner.json history, S094-S095 logs cut to their
  header and last 25 lines, S096's to its first 260 and last 30, lines cut at 400 characters; latency, progress and
  fight files as they were), `shots/shot_000001.jpg` (an ffmpeg `testsrc2` frame, not a game capture).
- `run/runtime/sessions/claude-run-2/positions.jsonl`: SYNTHETIC party positions in S096's window (a walk on
  END_Main, a 300 s gap, more END_Main, then EPI_Main_A), written by `positions.Sampler` from made-up state.status
  dicts; the real game wasn't sampled for it.
- `run/playthroughs/claude-run-2/memory/`: a subset of the memory files, no git history (tests that need commits
  copy it and `git init` it).

Scrubbed: repo and home paths (`<repo>`, `~`), session UUIDs (zeros), the numeric save-owner id. The memory text
mentions only game content.
