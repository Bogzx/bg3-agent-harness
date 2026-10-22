# Game updates

The harness is tied to one BG3 build. bg3le finds the engine's functions by address, the mod reads UI and engine
state whose layout Larian can change, and benchmark runs only compare when they played on the same build. So the
build is pinned, the machine stays offline so Steam can't change it, and a new build is accepted only after the
checks below pass on it.

The pinned build today: Steam buildid **25605617** (v4.76.31.656, Patch 8 Hotfix 10).

## Where the pin lives

| What | Where |
|---|---|
| the pinned buildid | `WANT_BUILD` in `tools/dev/first_launch.sh`; `tools/bench_preflight.py` reads it from there (its `PINNED_BUILD` is only a fallback) |
| the installed buildid | `AppState.buildid` in Steam's `appmanifest_1086940.acf`: `python3 tools/steam_paths.py buildid` prints it (`manifest` prints the file it reads) |
| the check of one against the other | `python3 tools/ci/game_build.py` (below) |
| the build a series played on | `pins.game_build` in `runtime/sessions/<series>/series.json`, and in every run's manifest |
| the docs that name it | README.md, docs/SETUP.md, docs/BENCHMARK.md, this file |

## Staying on the pinned build

- Steam in offline mode on the machine that runs the harness. With
  `touch ~/.config/bg3ai/require-steam-offline` (or `BG3AI_REQUIRE_STEAM_OFFLINE=1`) the launcher refuses to start
  the game while Steam is online. If the same account plays on another machine, offline mode also keeps the two
  from kicking each other.
- Automatic updates for BG3 set to "Only update this game when I launch it" in Steam (Properties > Updates).
  The preflight warns when the game's `AutoUpdateBehavior` lets Steam update it in the background.
- Launch only through the runner (`runner/launch_game.sh` or `runner/play.py run`), never from Steam's Play
  button while online.
- Check before every series: `python3 tools/bench_preflight.py [--series NAME]`. Its `install` line warns about
  a pending update and its `build` line fails when the installed buildid is not the pinned one (or, with
  `--series`, not the series' frozen one).

## The build check

`python3 tools/ci/game_build.py [--manifest PATH] [--pin-file PATH]` compares the installed buildid with the pin and
prints one line. It only reads two files (the appmanifest that `tools/steam_paths.py` finds, and `WANT_BUILD` in
`tools/dev/first_launch.sh`); it never starts or talks to Steam. Unlike the preflight it has no fallback pin: a
`WANT_BUILD` line it can't read is an answer of its own.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | the installed build is the pin, no update pending | nothing |
| 1 | the installed build is not the pin | a new build is installed: run the procedure below before any run or series |
| 2 | the installed build is the pin, but Steam has an update pending (`StateFlags` UpdateRequired, or a `TargetBuildID` that differs) | keep Steam offline; update only on purpose, as below |
| 3 | can't tell: no manifest, no `buildid` in it, or no `WANT_BUILD` pin | fix the install or the pin file |
| 64 | bad arguments | |

Its tests run on fixture manifests: `uv run pytest tools/ci/test_game_build.py`.

A series never continues across a build change: the runner refuses a run whose pins differ from the series' first
run. `--allow-version-drift` overrides that and records the drift, and such a run is not comparable.

## When a new build comes out

Don't update the harness machine first. Do it on purpose, in this order, and move the pin only when every step
passes.

1. Finish or stop the running series. A series' results stay tied to the old pin.
2. Update the game. Take Steam online, let it update BG3, and go back to offline mode. Note the new buildid
   (`python3 tools/steam_paths.py buildid`; `python3 tools/ci/game_build.py` now exits 1) and the game version
   from the patch notes.
3. Check that bg3le still resolves. Rebuild it if its patches changed (`tools/build_bg3le.sh`), then run
   `tools/dev/first_launch.sh -- --continue`. Its `install` step only warns about the new buildid; the `health`
   step fails on any `WARNING: resolve:` line in bg3le's log. A failed resolve means bg3le's addresses need
   re-resolving for the new build, upstream or in `patches/bg3le/`, before anything else.
4. Run the conformance checks.
   - Offline first: `python3 tools/dev/acceptance.py --check` (tools, RPC ops, Lua snippets and saves still match
     the harness), the Lua Docker suites (`bash tools/ci/lua_suites.sh`) and the offline suite.
   - Then live: `python3 tools/dev/acceptance.py` with the game running (about 28 minutes for every section;
     `--list` shows them, `--section NAME` runs one). Every section must pass as it did on the old build.
5. Check the fake-mod fixtures. The integration tests replay Lua replies recorded on the pinned build
   (`packages/harness/tests/fixtures/rpc/*_live4.jsonl`: trade, dialogue, pickpocket, load, area; replayed by
   `packages/harness/tests/test_fixtures_live4.py` through the fake mod, `bg3harness.testing`). Record the same
   situations again on the new build with the fixture recorder (`BG3AI_RPC_RECORD=<file>` on the bridge,
   `bridge/src/bg3ai/rpc_record.py`; research/ovh/live4.md §1 has the sessions) and compare them with the old
   files: a reply whose shape changed means the mod or the bridge must change, and the fixture is replaced in the
   same commit. `uv run pytest packages/harness/tests/test_fixtures_live4.py` must pass on the new files.
6. Run the stress-test matrix (docs/OVERHAUL-PLAN.md Phase F; `tools/stress/matrix.toml`): every BG3 situation
   (game lifecycle, movement, combat, dialogue, trade, stealth, inventory, party, puzzles, UI states, special
   forms) from its start save, with a result for each row. `uv run python tools/stress/run.py --check` (offline)
   first, then `uv run python tools/stress/run.py` (results in `runtime/stress/results.json`, docs/STRESS-TEST.md
   rendered from them). Compare with the last run on the old build; every new failure is fixed or written down
   before the pin moves.
7. Check the benchmark's start saves. Load every scenario's start save on the new build and check that it
   starts where its scenario file says. Then run each scenario's oracle run and both baselines again: the oracle
   must still pass and the baselines must still fail. A start save that no longer works is made again, and the
   scenario gets a new name, because a series freezes the save's sha1.
8. Move the pin in one commit: `WANT_BUILD` in `tools/dev/first_launch.sh` (after it `tools/ci/game_build.py`
   exits 0 again), `PINNED_BUILD` in `packages/bench/src/bg3bench/preflight.py` (its fallback), the buildid and
   game version in README.md, docs/SETUP.md, docs/BENCHMARK.md and this file, a CHANGELOG entry, and any bg3le
   patch or mod fix the checks needed. That commit bumps the harness version, and the next benchmark release gets a new
   `bench-v` tag: results from before and after the move are not compared on one leaderboard.

If a step fails and can't be fixed soon, stay on the old build. Steam has no supported way back to an older build, so
before updating, keep a copy of the game folder or a second machine on the pinned build.
