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
`--take-over` (see Safety), `--until MILESTONE [--runs N] [--run-hours H] [--max-cost USD] [--max-tokens N]
[--allow-version-drift] [--allow-cap-change] [--judge|--no-judge] [--judge-model M] [--allow-judge-change]`,
`--character-brief TEXT [--roleplay strict|light] [--difficulty
Explorer|Balanced|Tactician]`, `--scenario NAME` (all below). `play.py runs --playthrough PT [--failures]` prints a
series' run table and its summary; `play.py judge --playthrough PT [--run K]` (re-)scores runs' persona into their
records; `play.py scenarios` lists `scenarios/*.json` and whether each is ready.

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
it doesn't play. Every way an `--until` run ends writes a record (not a `stop`, which leaves the run to resume).

**Caps.** `--run-hours H` caps one run's wall time: past it the run ends as a `timeout`. `--max-cost USD` and
`--max-tokens N` cap its claude cost and tokens (every claude process of the run: play, resumes, fix-notes, memory
keeper; tokens = input + output + cache reads + cache writes). They are checked while claude streams (the run's
finished processes from their result events plus the live stream's per-message usage; claude reports cost only in
result events, so the live cost is the live tokens at the run's cost per token so far) and between sessions (the
keeper's cost too). Past one, claude is stopped like at a hard stop (session reason `cap`, no keeper pass after it)
and the run ends `over_budget`; the record's `cap` says which (`time` | `cost` | `tokens`).

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
`--run-hours`/`--max-cost`/`--max-tokens` each run. All records go to `runtime/sessions/naut/runs.jsonl` (`series`,
`run`). Re-running the same
command resumes the series at the first run without a record (also after the runner's self-restart). The series
stops early on `stop`/Ctrl-C, a stale bg3le build or a runner error; it goes on after a timeout, a cap or a give-up.

**Fixed per series** (`series.json`, written by the first run; a series from before these fields gets them from its
next run):
- `caps` (`run_hours`, `max_cost`, `max_tokens`): later runs take them from series.json; a cap flag left out uses the
  frozen value, a different value is refused (exit 2) unless `--allow-cap-change` (recorded in `cap_changes`).
- `pins`, the versions every run plays with (`runs.version_pins`): `game_build` (the Steam buildid from
  `appmanifest_1086940.acf`, `tools/steam_paths.py buildid`; bg3le was verified on 25605617, see
  `tools/dev/first_launch.sh`), `bg3le` (`source`: `git describe --tags --dirty` of third_party/bg3le, `local_changes`
  = `-dirty`; `commit`, `sha256` and the patch count from `build/bg3le/dist/BUILDINFO`), `mod` (meta.lsx `version`,
  `sha1` over the mod/ tree) and `repo` (`commit`, `dirty` and a `diff` hash over the code paths: runner, bridge, mod,
  play, tools, patches, memory-template). Before a run that hasn't started, the runner compares `game_build`,
  `bg3le.sha256`, `bg3le.source`, `mod.sha1` and `repo.commit/dirty/diff` with the first run's and refuses a
  difference (exit 2, `REFUSING run k: versions differ ...`, then `runner stopped`) unless `--allow-version-drift`
  (then recorded in `version_drift`). A run already under way (the runner's self-restart) keeps the pins it started
  with. So: commit, or at least don't edit code, between the runs of a series.
- `judge_model`, the persona judge's model (below): `--judge-model` / `$BG3AI_JUDGE_MODEL` left out takes it; a
  different one is refused (exit 2) unless `--allow-judge-change` (recorded in `judge_change`). Frozen by the first
  judged run, or by the first `play.py judge` of a series from before it.
- `sandbox`, `sandbox_unfirewalled` (`--sandbox`, `--sandbox-unfirewalled`): written by the first run of a new series
  only; a series from before these keys played without a sandbox (`none`). A run in another mode is refused before
  any docker work (exit 2, `REFUSING the run: the series' sandbox is fixed …`, then `runner stopped (sandbox mode
  differs …)`) unless `--allow-sandbox-change` (recorded in `sandbox_change`). Unlike the client, a left-out
  `--sandbox` means `none`, never the frozen container: pass the flags the refusal names.

**Persona judge** (`runner/judge.py`). After a run's record is written (any outcome), `--judge` scores the run's
in-character decisions (dialogue choices with their stated reasons, `[decision]` notes, CHARACTER.md development)
against the playthrough's CHARACTER.md with a fixed judge model (`claude -p`, no tools, never the model under test:
`run` refuses that pairing) and merges `persona` into the record in place: `{status (ok | judge_failed |
no_decisions | no_character), score 0-10, judge_model, decisions, dropped, summary, cost_usd, out_of_character?,
error?}`. On by default for `--runs`/`--scenario` series, off for a single `--until` run. It has a 15-min budget
(`JUDGE_TIMEOUT_S`), a second Ctrl-C cuts it short, and a failure is only a `judge_failed` persona, never a failed
run. Its cost is `persona.cost_usd`, never in the run's `cost_usd` nor its `--max-cost` cap. `play.py judge
--playthrough PT --run K` re-judges one record (for runs from before the judge, e.g. naut 1-3); without `--run` every
record without a score (`--all`: scored ones too); it also fills a missing `model_time`.

**Scenarios (`--scenario NAME`).** A fixed start point as a file, `scenarios/<name>.json` (format, the drafts and
how to make a start save: `scenarios/README.md`): the start (a new game, or a start save), the `--until` milestone,
the caps, the profile and the per-run resets. `run --scenario NAME [--runs N] [--playthrough SERIES]` plays it as a
`--runs` series (N and the series name default to the scenario's), refused unless `play.py scenarios` shows it
`ready`. Flags that contradict it are refused (exit 2): `--until`, `--character-brief`, `--roleplay`,
`--difficulty`, `--save-owner` must be left out or equal; the caps follow the series' caps rule (a different value
needs `--allow-cap-change`). series.json gets `scenario` (`name`, `digest`, `start_save_sha1`, `spec`): a later run
is refused when the scenario file or the start save's `.lsv` changed, or the series is another scenario's. Every run
gets the scenario's profile.json. A save start also gets: the save owner = `profile.character.name` (it must be the
save's character); the start save as a candidate of the save choice although it is older than the run
(`Runner.ours`, so the first session loads it and the agent's first save supersedes it; `BG3AI_START_SAVE` for the
MCP server); a fresh memory whose `HANDOFF.md` is the harness's neutral start note (`runs.start_note`, a pure
function of the scenario's fixed `start.note`; never another playthrough's memory); and a `SCENARIO START` note in
the first kickoff (`scenario_start_note`). Run records carry `scenario`.

What a run doesn't inherit (reset before its first session, listed in the record's `reset`):

| State | Reset |
|---|---|
| saves (every run's character may have one name) | `runner.json` `saves_after` = the run's start: the runner and the MCP server (`BG3AI_SAVES_AFTER`, `list_saves`/`load_game`) ignore older saves (a scenario's start save excepted, in the runner) |
| the navigation per-level kill list `Script Extender/BG3AI/nav_guard.json` (NAV-3) and the breadcrumb mirrors `nav_<host uuid>.json` | moved into `runtime/sessions/<run>/reset/` (`$BG3AI_SE_DIR` overrides the Script Extender dir) |
| the mod's Lua state (kill list in memory, warm-up counters, guards) | the game process is stopped between runs |
| `play/knowledge/` (read-only to the agent's tools; FAIR-2) | its files are `chmod a-w` for the whole series (original modes in `runtime/knowledge-lock.json`, restored when the runner exits; an execv'd successor keeps the lock; any later non-series `run` undoes a stale one; `BG3AI_KNOWLEDGE_LOCK=0` skips it): an operator edit mid-series fails loudly instead of skewing the series. Also snapshotted at the run start; after the run every file the agent's Edit/Write calls changed is put back (its version kept in `runtime/sessions/<run>/knowledge_edits/`); other changes (an operator's) are reported, not reverted; the record's `knowledge` lists both |
| memory, `runner.json` history, progress seen-state, session logs | per playthrough already (a new playthrough per run); a scenario's save start adds its start note as HANDOFF.md |
| travel_to dead ends | per MCP server process (per session) already |

Kept on purpose: `runtime/levelup_clicks.json` (UI click calibration), the wiki cache, `bg3ai/config.json`
(the runner sets it every session), `runtime/actions.jsonl` (read by time window).

**Run record** (`runs.jsonl`, one line per run): `outcome` (`reached` | `timeout` | `over_budget` | `gave_up` |
`runner_error`), `cap` (`time` | `cost` | `tokens` when a cap ended it), `why`, `clean`, counts, cost and `tokens`
as above, `failures` (each `{t, kind, class, detail, n?}`), `operator` (each `{t, what}`), `caps`, `pins`,
`version_drift`, `cap_changes`, `persona` and `judge_change` (above), and the benchmark metrics (`runs.run_metrics`, pure functions over the run's files):

| Field | What | From |
|---|---|---|
| `time_to_milestone_s` | wall seconds from the run's start to the milestone; null if not reached | the milestone's time |
| `deaths` | `{fights, downs, deaths, wipes}`: party members downed / dead per fight, Game Overs | `Sxxx.fights.jsonl` (the fight debriefs' `party[].downed/died`), `party_wipe` failures. Downs/deaths outside a fight the debrief saw (falls, traps out of combat) aren't observable |
| `fairness_flags` | `{count, leak, check, blocks, kinds: {rule: n}}` | runner/fairness.py over the play streams; `blocks`: the sandbox guard's blocks of the agent's file calls |
| `latency` | `{sessions, idle_median_s, idle_p90_s, gaps_over_10s, read_rt_median_ms}` | the play sessions' `Sxxx.latency.json`; over several sessions each value is the sample-weighted median of the per-session values (exact for one session); null without latency files |
| `story` | `{points, events, sessions, by_kind}` gained in the run | `progress_history.jsonl` |
| `milestones_reached` | milestones reached in the playthrough at the end | `bg3ai.milestones.status` |
| `model_time` | the run's model vs harness time: `model_s`, `harness_s`, the wall split, idle-gap attribution, TTFT, output speed, and `adjusted_model_s` (model time at the reference API speed `ref`, so providers/hours compare) | `runner/model_time.py` over the run's play streams, turns and metrics (its constant field lists left out) |

`play.py runs` prints the per-run table (now with time to the milestone, tokens, the idle median, the persona score
(`fail` / `n/d` no decisions / `n/c` no character when unscored) and the API-adjusted model minutes) and a series
summary: n, reached, and n/min/max/mean/stddev (sample) of wall time, time to the milestone (reached runs), cost,
tokens, the idle median, the persona score, the adjusted model minutes and the judge's cost; plus any version drift,
caps and judge models that differ between runs. Records from before a field
show `-` (time to the milestone is derived from `reached_at - started`).

Classes (`runner/runs.py`):

- **harness** (ours): `game_crash`, `game_hang`, `game_oom`, `setup_failed`, `launch_failed`, `launch_gave_up`,
  `load_refused`, `recovery_failed`, `bg3le_stale`, `steam_refused`, `oom_save_refused` (runner_events.jsonl);
  `mcp_failed` (claude's init shows the bg3 server not connected), `claude_api` (API/usage/overload errors),
  `claude_quick_exit` (claude exited within a minute), `tool_exception` (an uncaught bridge exception, "Error
  executing tool"), `tool_timeout` (an MCP timeout or a `TIMEOUT` step), `fairness_leak` (runner/fairness.py LEAK in a
  game-tool result: the agent saw what a player can't), `gave_up` (the runner gave up), `runner_error`.
- **agent**: `stall` (the progress poller's stuck record: no story progress for 10+ min while acting), `party_wipe`
  (Game Over), `reload` (every load_game it called), `early_exit`, `fairness_block` (the sandbox guard blocked its
  file call), `timeout` (the run's time ran out), `cost_cap` / `tokens_cap` (the run's cost or token cap); the last
  three are `unknown` when harness failures hit the run.
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
   (a new playthrough) the game is left at the main menu. The `Save to load:` save wins over newer ones (the
   agent's decision, e.g. a reload before a lost fight) unless a save was written after the session that wrote
   HANDOFF (its `# Handoff Sxxx` heading) ended: a crashed session's or a runner quicksave's later play. The choice
   and its reason are in the runner log (`save choice:`), `run --plan` and the kickoff (S16, 2026-10-18).
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

- **Mod snapshot (S24):** the runner's game loads the mod from `runtime/mod-live/` (`launch_game.sh --mod-path`),
  a copy of `mod/` synced in place at each session start, before the relaunch check and the hot reload, also for a
  relaunch mid-session. A merge into main mid-session therefore never reaches the session's game (S090 loaded new Lua
  next to its old bridge). The start line names the commit (`S090: claude starting (…; code 1328586c7)`); when main
  moves during a session, the runner logs `main moved since the session's mod snapshot (a -> b)` (event
  `main_moved`): an MCP server started after that (a resume) runs b's bridge next to a's mod. A game that loads the
  mod from elsewhere (launched before this, or by a live window) is relaunched at the next session start.
- **Relaunch skip:** a relaunch between sessions loads the newest save, so it is skipped when that save is older than
  the last session's start (it would lose that session's play), unless the session ended on `load_game` of exactly
  that save with nothing after it but reads, notes/memory, saves and refused calls (actions.jsonl; S25: S091 and
  S092 ended on `load_game S090-end`, and the S24 relaunch was skipped at the boundary).
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
  consecutive game setup failures (a bg3 MCP server that fails at init counts as one, and the session stops
  at once), stops the runner. A usage/rate limit, an overload or a server error never gives up: the runner
  waits for the limit's reset time (else 1, 2, 4… min up to 1 h) and goes on (event `api_limit`). A game
  that answers while the BG3AI mod is gone for 3 min is a hang (recover, resume). A stale bg3le build (`tools/bg3le_buildinfo.py`: the
  dist lacks a `patches/bg3le` patch, or `launch_game.sh` exits 4) stops it before any launch: run
  `tools/build_bg3le.sh` (`BG3AI_ALLOW_STALE_BG3LE=1` launches anyway). Every exit logs
  `runner stopped (<why>)`; an unexpected error also logs its traceback. Check `runtime/runner.log`.

## Safety

- The runner refuses to use a game that is already in a save it didn't load (e.g. your own playthrough).
  Quit to the menu or close the game first, or pass `--take-over`.
- It only ever signals this harness's bg3 processes: ones `runner/launch_game.sh` started (the
  `BG3AI_GAME_STAMP` env, the `bg3ai-game-*.scope` cgroup, or bg3le mapped in), and the launcher's process
  group only while its leader is provably that launch (command line + start time). A BG3 you launched from
  Steam is never killed; while one runs the runner refuses to launch its own (event `foreign_game`) and retries.
- `--sandbox container` plays from a container that reaches only the bg3 MCP server (over http on the host, one
  server per play process) and the client's model API through an egress proxy; the memory keeper runs there too.
  It refuses to start without the host firewall (`sudo tools/container/firewall.sh up`; `--sandbox-unfirewalled`
  for dev runs, recorded). runner/sandbox.py, tools/container/README.md ("The runner").
- Only one runner at a time (`runtime/runner.lock`, an flock; never delete the file:
  `tools/orchestrator/runner_lib.sh` waits for the lock instead).
- Steam offline is required only on a machine that sets it: `~/.config/bg3ai/require-steam-offline` (its
  text is the reason; the test machine has it) or `BG3AI_REQUIRE_STEAM_OFFLINE=1` (`=0` overrides the file). Elsewhere an
  online Steam is fine. `python3 tools/steam_paths.py launch-ok` shows the decision.
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
| `runtime/sessions/<pt>/series.json` | a `--runs` series: its frozen profile, N, the milestone, version pins, caps and judge model (and the scenario) |
| `scenarios/<name>.json` | `run --scenario` start points (`scenarios/README.md`) |
| `runtime/sessions/<run>/reset/`, `knowledge_snapshot.json`, `knowledge_edits/` | a `--runs` run's moved nav files, knowledge/ snapshot, the agent's knowledge/ edits that were put back |
| `playthroughs/<pt>/profile.json` | the character brief, roleplay mode, difficulty (`--character-brief`) |
| `runtime/sessions/<pt>/Sxxx.{jsonl,log}` | Claude's stream (raw and readable); `Sxxx-fix.*` for fix-notes passes |
| `runtime/STOP` | created by `play.py stop` |

Tests (pure logic only, nothing is launched): `python3 runner/test_play.py`, `runner/test_runs.py`,
`runner/test_scenarios.py`. Crash recovery end to end with
stubs (no game, no claude, temp dirs only): `python3 runner/test_crash.py`.

Dashboard: **`/series`** (`/api/series[?playthrough=BASE]`, `runner/dashboard/seriesview.py`), the benchmark series
view: per run outcome, minutes to the milestone, cost, tokens, H/A/U failures, persona, idle median/p90, gaps > 10 s,
read round trip, the model_time harness/model/overlap bar with the top harness tools, sandbox; the series.json
settings; the spread (the streak line and the stats of `play.py runs`); and a live row for the run in progress
(runner.status.json `series`/`run_index`: elapsed, cost so far incl. the running stream, the current session's live
latency). Every 10 s; default: the running series, else the newest runs.jsonl.
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
