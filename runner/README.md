# runner/: unattended play

`play.py` is the supervisor that lets the agent play for hours: one Claude Code session (~1 h) after
another, with the game kept alive and the memory committed between sessions. Stdlib only. It uses
`bridge/src/bg3ai/luadebug.py` to talk to the game and `tools/memory/` for the memory.

## Quick start

```bash
python3 tools/memory/new_playthrough.py ilse          # once: playthroughs/ilse/memory (git repo)
python3 runner/play.py run --playthrough ilse --hours 8 --model opus
python3 runner/play.py status                         # from another terminal
python3 runner/play.py stop                           # finish the current session, then exit
```

Ctrl-C once does the same as `stop`; a second Ctrl-C interrupts the running session (SIGINT to Claude).
`run --dry-run` prints the next session's plan (save to load, session id, the claude command) and changes
nothing.

Options: `--session-minutes 60` (wrap-up starts 3 min before the end), `--hours N` (total
budget; no new session starts with less than 20 min left), `--model`, `--save-owner NAME`,
`--take-over` (see Safety), `--until MILESTONE`, `--character-brief TEXT [--roleplay strict|light]
[--difficulty Explorer|Balanced|Tactician]` (both below).

`--save-owner` (or `$BG3AI_SAVE_OWNER`) is the player character's name, which starts every save folder
of the playthrough: `Claude` for `Claude-<id>__<label>`. Without it, the runner learns the owner from the
first `Sxxx-` save (a new game: the name the agent gave its character) and keeps it in `runner.json`. The
`<id>` differs per save, so two playthroughs with one character name can't be told apart by folder: give each
playthrough its own name. For `claude-run-1`:

```bash
python3 runner/play.py run --playthrough claude-run-1 --save-owner Claude --hours 8
```

**Character brief (phase 0).** `run --character-brief "a pious, short-tempered dwarf cleric who distrusts mind
flayers"` writes `playthroughs/<pt>/profile.json` (`{"character": {"brief", "name"?, "constraints"?, "roleplay"},
"difficulty"?}`; roleplay defaults to strict, difficulty to Explorer). A new game of that playthrough then starts
with a `PHASE 0, CHARACTER CREATION` kickoff: the agent follows `knowledge/playbooks/character-creation.md`, designs
the build and persona from the brief, names the character itself (unless `character.name` or `--save-owner` sets
one) and saves `S001-start`; the runner adopts that name as the save owner (`runner.json`, `character.name`).
Without a brief, a new game is named after `--save-owner` as before.

**Stop at a milestone.** `run --until nautiloid_escaped` (ids in `bridge/src/bg3ai/milestones.py`: `crash_site`,
`beach`, ...) checks the progress poller's seen-state every poll; when the milestone is reached it stops claude
(SIGINT; the fix-notes pass closes the memory), the runner quicksaves if needed and appends a run record to
`runtime/sessions/<pt>/runs.jsonl` (wall/play minutes, sessions, cost, turns and tokens from claude's result events,
game crashes/hangs, agent hangs, recoveries, setup failures, agent reloads), then exits. A playthrough already past
it doesn't play. (`--runs N`, the next playthrough and a new game, isn't there yet: loop the command in a shell.)

## What one session does

1. **Game up.** If there's no listener on 127.0.0.1:9998, start `runner/launch_game.sh` in the background
   (its output goes to `runtime/launch-*.out`). With a save to load it passes `--load <save folder>`.
   Once worker io's `ui.menu` op exists, the runner instead waits for the client state `Menu`
   (pressing SPACE past the splash screen) and loads through `ui.menu {action:"load"}`. Then it waits for
   `Running` and a server `sys.ping`.
2. **Which save.** It takes the newest of: the save named on HANDOFF.md's `Save to load:` line,
   `Sxxx-...` saves by this playthrough's save owner (the character prefix of the save folder, recorded
   the first time), and quicksaves the runner itself made. Nothing else is ever loaded. With no save yet
   (a new playthrough) the game is left at the main menu.
3. **Claude.** It runs `claude -p <kickoff> --model M --output-format stream-json --verbose --strict-mcp-config
   --mcp-config play/.mcp.json --session-id <uuid>` in `play/` with:
   - `BG3AI_SESSION_DEADLINE`: epoch seconds, for the MCP session clock
   - `BG3_SESSION_MINUTES` / `BG3_WRAP_MINUTES`: for the memory hooks
   - `BG3AI_PLAYTHROUGH`
   - `BG3AI_SAVE_OWNER` / `BG3AI_SAVE_PREFIX`: the save owner, and `$BG3AI_SAVE_PREFIX` or else `<owner>-`; both
     empty for a new game whose character isn't named yet (the server takes the name create_character types)
   - `BG3AI_SAVE_GATES`: `1`/`0` when the runner opened/closed the save/load gates for this game (absent when
     it couldn't tell); the MCP server follows it

   The play/ hooks open the memory session (SessionStart) and lint and commit it (Stop). The runner does
   not call `session.py start` itself, since that would open two sessions. Output goes to
   `runtime/sessions/<playthrough>/Sxxx.jsonl` (raw) and `Sxxx.log` (readable: `[say]`, `[tool]`,
   `[result]`, `[end]`).
4. **Deadline.** At deadline + 10 min Claude gets SIGINT, then SIGTERM 30 s later, then SIGKILL. If Claude
   exits on its own more than 5 min before the deadline (not a crash, hang or hard stop), the runner logs
   `Sxxx: early exit: … min left`, records `early_exit_min` in the history, and starts the next session at once.
5. **After.**
   - If no new `Sxxx-` save appeared during the session, the runner quicksaves (`ui.menu quicksave`, or an
     injected F5) and records the new folder.
   - If the memory session is still open, it runs `session.py end`. On lint failure it resumes the same
     Claude conversation (`--resume`, so no new memory session) for a 10-minute "fix your notes" pass,
     then tries `end` again. If that still fails, it leaves the session open and tells the next session.

## Recovery

- **Stopping the game:** the native `bg3` ignores SIGTERM. The runner sends SIGTERM to the launcher's
  process group and to every `pgrep -x bg3` process, then SIGKILL after 3 s. It relaunches only once no
  `bg3` process remains and port 9998 is free.
- **Crash or hang:** the game process or port disappears, or 3 consecutive liveness evals fail at 30-s
  intervals. The runner stops Claude, kills the game, relaunches it (`launch_game.sh --load`), and reloads
  the playthrough's latest save (newest `Sxxx-`/runner quicksave of the save owner; never anyone else's).
  If at least 8 min of the session are left, it then **resumes the same Claude conversation**
  (`claude --resume`, transcript `Sxxx-r1.*`) with a note on the crash and the save, up to 3 times per
  session; the deadline doesn't move. Otherwise the session ends and the next kickoff carries the note.
  `runner.json` history records `resumes`. Offline test with a fake game and fake claude:
  `python3 runner/test_crash.py` (~30 s; crash, hang, too-little-time).
- **Game Over** (`ui.state` screen `game_over`, once that op exists): the kickoff tells the agent to load.
  If the next session starts at Game Over too, the runner reloads the latest save itself.
- **Stuck detector:** progress is a signature of level/region/subregion, party levels and XP, and
  journal titles and step counts (server `state.status` + `state.journal`). After 2 consecutive sessions
  with no change, the next kickoff carries a "change approach" directive. Until those ops exist, progress
  is unknown and the detector stays quiet.
- **Refused load:** before a `ui.menu` load of its own save the runner opens the save/load gates (a fresh
  profile has them off); if the mod still refuses (or the eval fails) it relaunches the game with
  `--load <folder>` instead. Any mod refusal during setup is a setup failure (relaunch, retry), never fatal.
- **Give-ups:** Claude exiting within a minute 3 times in a row (auth or config problems), or 5
  consecutive game setup failures, stops the runner. A stale bg3le build (`tools/bg3le_buildinfo.py`: the
  dist lacks a `patches/bg3le` patch, or `launch_game.sh` exits 4) stops it before any launch: run
  `tools/build_bg3le.sh` (`BG3AI_ALLOW_STALE_BG3LE=1` launches anyway). Every exit logs
  `runner stopped (<why>)`; an unexpected error also logs its traceback. Check `runtime/runner.log`.

## Safety

- The runner refuses to use a game that is already in a save it didn't load (e.g. your own playthrough).
  Quit to the menu or close the game first, or pass `--take-over`.
- It only kills a game it launched, one at the main menu, or any game under `--take-over`.
- Only one runner at a time (`runtime/runner.lock`).
- `BG3AI_SAVE_PREFIX` marks this playthrough's saves. The runner accepts it as a folder prefix (`Claude-`:
  the character name plus the game's dash, what the MCP server and docs/HANDOFF.md use; the agent's
  `Sxxx-` labels stay plain) or as a label prefix (`pt1-S010-end`). It is optional: without it the runner
  passes `<owner>-`. Use `--save-owner` for the character name; new games name the character with it
  (create_character types and checks it before "Venture forth").
- `--session-minutes` below 20 is refused (`--allow-short-sessions` for harness tests): start-up (≤1 min)
  and wrap-up (3 min) need room. Wrap-up starts 3 min before the end (a third of a tiny session), passed to
  the MCP clock line and the Stop hook as `BG3AI_SESSION_WRAPUP`, so both switch at the same moment.

## Files

| Path | What |
|---|---|
| `runtime/runner.log` | runner decisions, one line each |
| `runtime/runner.status.json` | live status for `play.py status` |
| `runtime/sessions/<pt>/runner.json` | save owner, runner quicksaves, per-session history and progress signatures |
| `runtime/sessions/<pt>/runs.jsonl` | one record per `--until` run |
| `playthroughs/<pt>/profile.json` | the character brief, roleplay mode, difficulty (`--character-brief`) |
| `runtime/sessions/<pt>/Sxxx.{jsonl,log}` | Claude's stream (raw and readable); `Sxxx-fix.*` for fix-notes passes |
| `runtime/STOP` | created by `play.py stop` |

Tests (pure logic only, nothing is launched): `python3 runner/test_play.py`. Crash recovery end to end with
stubs (no game, no claude, temp dirs only): `python3 runner/test_crash.py`.

Dashboard: `/api/goals` + the **Goal scorecard** card (every session's goal metrics side by side with
pass/fail, a trend sparkline, best value and pass rate; goal bars in `runner/dashboard/feeds.py` `GOALS`) and a
**milestones** line (`bridge/src/bg3ai/milestones.py`: Nautiloid escaped, beach, first camp, … Act 3), also in
Story progress.

Not verified yet: the exact `ui.menu` load argument (the runner passes the save folder name as `name`),
`InjectKeyPress("F5")` as the quicksave fallback, and the stream-json event shapes beyond what
`format_stream_event` handles. A real end-to-end run needs a game the runner may own.

## Memory keeper (between sessions)

After each play session the runner starts `runner/memory_keeper.py`: a `claude -p` pass (sonnet, effort low,
≤5 min; `--keeper-model/--keeper-effort/--keeper-minutes`, off with `--no-memory-keeper`) that updates
objectives/quests/npcs/locations/party-build/... from the session logs and HANDOFF.md and writes the chapter
when compaction is due. It runs in a thread during the next session's prep (game check, mod reload, check_all:
none write memory) and is joined before the next player starts, so it never races the player's edits.
Sandbox: `--restricted --tools Read,Edit,Write,Glob,Grep`, cwd = the memory dir, knowledge read-only, no MCP,
HANDOFF.md and sessions/ denied (and restored if touched). The runner lints afterwards: new errors get one
resumed fix try, else all keeper edits roll back. Clean edits are committed as `Sxxx memory-keeper: <files>`
(staleness counters reset). Log line `Sxxx memory keeper: <status> in N s; ...`, `memory_keeper` event in
runner_events.jsonl, details in keeper_history.jsonl, transcript `Sxxx-keeper.jsonl`.
