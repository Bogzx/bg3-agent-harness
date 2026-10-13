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
`--take-over` (see Safety), `--until MILESTONE [--runs N] [--run-hours H]`, `--character-brief TEXT [--roleplay
strict|light] [--difficulty Explorer|Balanced|Tactician]` (all below). `play.py runs --playthrough PT [--failures]`
prints a series' run table.

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
it doesn't play. Every way an `--until` run ends writes a record (not a `stop`, which leaves the run to resume),
and `--run-hours H` caps one run: past it the run ends as a timeout.

**Benchmark series (`--runs N`).** The launch bar is 3 unattended Nautiloid runs in a row:

```bash
python3 runner/play.py run --playthrough naut --until nautiloid_escaped --runs 3 --run-hours 4 \
    --character-brief "a pious, short-tempered dwarf cleric who distrusts mind flayers"
python3 runner/play.py runs --playthrough naut [--failures]    # the table and "N clean in a row"
```

Run 1 is the playthrough itself (`naut`), run k is `naut-r<k>`. Each run gets a fresh memory from
`memory-template/` (`tools/memory/new_playthrough.py`, created by the runner) and the same `profile.json`: the
series' brief is frozen in `runtime/sessions/naut/series.json` when it starts, so the name run 1's agent chose isn't
forced on run 2. Between runs the runner stops the game it launched; the next run launches it again to the main
menu and plays a new game (phase 0 with a brief, else the `--save-owner` name). `--hours` caps the whole series,
`--run-hours` each run. All records go to `runtime/sessions/naut/runs.jsonl` (`series`, `run`). Re-running the same
command resumes the series at the first run without a record (also after the runner's self-restart). The series
stops early on `stop`/Ctrl-C, a stale bg3le build or a runner error; it goes on after a timeout or a give-up.

What a run doesn't inherit (reset before its first session, listed in the record's `reset`):

| State | Reset |
|---|---|
| saves (every run's character may have one name) | `runner.json` `saves_after` = the run's start: the runner and the MCP server (`BG3AI_SAVES_AFTER`, `list_saves`/`load_game`) ignore older saves |
| the navigation per-level kill list `Script Extender/BG3AI/nav_guard.json` (NAV-3) and the breadcrumb mirrors `nav_<host uuid>.json` | moved into `runtime/sessions/<run>/reset/` (`$BG3AI_SE_DIR` overrides the Script Extender dir) |
| the mod's Lua state (kill list in memory, warm-up counters, guards) | the game process is stopped between runs |
| `play/knowledge/` (agent-writable outside playbooks/, FAIR-2) | snapshotted at the run start; after the run every file the agent's Edit/Write calls changed is put back (its version kept in `runtime/sessions/<run>/knowledge_edits/`); other changes (an operator's) are reported, not reverted; the record's `knowledge` lists both |
| memory, `runner.json` history, progress seen-state, session logs | per playthrough already (a new playthrough per run) |
| travel_to dead ends | per MCP server process (per session) already |

Kept on purpose: `runtime/levelup_clicks.json` (UI click calibration), the wiki cache, `bg3ai/config.json`
(the runner sets it every session), `runtime/actions.jsonl` (read by time window).

**Run record** (`runs.jsonl`, one line per run): `outcome` (`reached` | `timeout` | `gave_up` | `runner_error`),
`why`, `clean`, counts and cost as above, `failures` (each `{t, kind, class, detail, n?}`), `operator` (each
`{t, what}`). Classes (`runner/runs.py`):

- **harness** (ours): `game_crash`, `game_hang`, `game_oom`, `setup_failed`, `launch_failed`, `launch_gave_up`,
  `load_refused`, `recovery_failed`, `bg3le_stale`, `steam_refused`, `oom_save_refused` (runner_events.jsonl);
  `mcp_failed` (claude's init shows the bg3 server not connected), `claude_api` (API/usage/overload errors),
  `claude_quick_exit` (claude exited within a minute), `tool_exception` (an uncaught bridge exception, "Error
  executing tool"), `tool_timeout` (an MCP timeout or a `TIMEOUT` step), `fairness_leak` (runner/fairness.py LEAK in a
  game-tool result: the agent saw what a player can't), `gave_up` (the runner gave up), `runner_error`.
- **agent**: `stall` (the progress poller's stuck record: no story progress for 10+ min while acting), `party_wipe`
  (Game Over), `reload` (every load_game it called), `early_exit`, `fairness_block` (the sandbox guard blocked its
  file call), `timeout` (the run's time ran out; `unknown` when harness failures hit the run).
- **unknown**: `agent_hang` (claude silent for minutes: a slow turn or a stalled API), `claude_killed` (claude
  ended by a signal from outside the runner).

A game-tool `FAILED:`/`NOT READY`/`REFUSED` result is the game refusing the agent's call, not a failure.
**Operator actions**: a second `runner_start` in the run (restarted by hand), a self-restart on changed code, a
stop, `--take-over`. `clean` = reached, no harness failure, no operator action.

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
3. **Claude.** It first opens the memory session itself (`session.py start`: `sessions/Sxxx.md`; its output, the
   memory briefing, leads the kickoff). Then it runs `claude -p <kickoff> --model M --output-format stream-json
   --verbose --strict-mcp-config --mcp-config play/.mcp.json --tools ToolSearch --session-id <uuid>` in `play/`.
   `--tools ToolSearch` removes every built-in tool (files, shell, web, agents): the agent has only `mcp__bg3__*`,
   whose memory tools are sandboxed by the server (bridge/src/bg3ai/memtools.py). Env:
   - `BG3AI_SESSION_DEADLINE`: epoch seconds, for the MCP session clock
   - `BG3AI_SESSION`: the session the runner opened; the MCP server attaches to it and never opens one
   - `BG3_SESSION_MINUTES` / `BG3_WRAP_MINUTES`: for session.py's clock
   - `BG3AI_PLAYTHROUGH`
   - `BG3AI_SAVE_OWNER` / `BG3AI_SAVE_PREFIX`: the save owner, and `$BG3AI_SAVE_PREFIX` or else `<owner>-`; both
     empty for a new game whose character isn't named yet (the server takes the name create_character types)
   - `BG3AI_SAVE_GATES`: `1`/`0` when the runner opened/closed the save/load gates for this game (absent when
     it couldn't tell); the MCP server follows it

   There are no client hooks. When claude ends its turn, the runner makes the stop check (`session.py stop`, the
   old Stop hook): before wrap-up time, or with HANDOFF.md not written for this session, or with lint errors, it
   resumes the same conversation with the reason (`Sxxx-c1`, at most `MAX_STOP_RESUMES`); when the session may
   end, it lints and commits the memory. The agent's own `end_session` tool makes the same check from inside the
   session, so a well-behaved agent never needs the resume. Then `session.py session-end` (the old SessionEnd
   hook). Output goes to
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
  the MCP clock line and the stop check as `BG3AI_SESSION_WRAPUP`, so both switch at the same moment.

## Files

| Path | What |
|---|---|
| `runtime/runner.log` | runner decisions, one line each |
| `runtime/runner.status.json` | live status for `play.py status` |
| `runtime/sessions/<pt>/runner.json` | save owner, runner quicksaves, per-session history and progress signatures |
| `runtime/sessions/<pt>/runs.jsonl` | one record per `--until` run (a `--runs` series: all its runs, in the base's dir) |
| `runtime/sessions/<pt>/series.json` | a `--runs` series: its frozen profile, N, the milestone |
| `runtime/sessions/<run>/reset/`, `knowledge_snapshot.json`, `knowledge_edits/` | a `--runs` run's moved nav files, knowledge/ snapshot, the agent's knowledge/ edits that were put back |
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

After each play session the runner starts `runner/memory_keeper.py`: a `claude -p` pass (the session's play model,
effort low, ≤5 min; `--keeper-model <model>` overrides `same`, `--keeper-effort/--keeper-minutes`, off with
`--no-keeper`; the model is recorded in the session history, keeper_history.jsonl and runs.jsonl) that updates
objectives/quests/npcs/locations/party-build/... from the session logs and HANDOFF.md and writes the chapter
when compaction is due. It runs in a thread during the next session's prep (game check, mod reload, check_all:
none write memory) and is joined before the next player starts, so it never races the player's edits.
Sandbox: `--restricted --tools Read,Edit,Write,Glob,Grep`, cwd = the memory dir, knowledge read-only, no MCP,
HANDOFF.md and sessions/ denied (and restored if touched). The runner lints afterwards: new errors get one
resumed fix try, else all keeper edits roll back. Clean edits are committed as `Sxxx memory-keeper: <files>`
(staleness counters reset). Log line `Sxxx memory keeper: <status> in N s; ...`, `memory_keeper` event in
runner_events.jsonl, details in keeper_history.jsonl, transcript `Sxxx-keeper.jsonl`.
