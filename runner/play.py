#!/usr/bin/env python3
"""Autonomous session runner: keeps an AI playing BG3 for hours, one ~1 h Claude Code session at a time.

    runner/play.py run --playthrough NAME [--hours N] [--session-minutes 60] [--model opus] [--effort medium]
    runner/play.py status [--playthrough NAME]
    runner/play.py stop                  # finish the current session, then exit
    runner/play.py bench run --matrix M.toml   # bg3bench's matrix, each job one run_session on this game
    runner/play.py run --playthrough NAME --until nautiloid_escaped --runs 3 [--run-hours 4]   # benchmark series
    runner/play.py runs --playthrough NAME [--failures]   # its runs: outcome, clean, failures by class
    runner/play.py judge --playthrough NAME [--run K]     # (re-)score runs' in-character decisions (runner.judging)

Per session: make sure the game is up with this playthrough's save loaded, open the memory session (session.py
start; its briefing leads the kickoff), launch `claude -p` in play/ with only the bg3 MCP tools (the server owns
memory, the sandbox and end_session; after claude exits the stop check may resume it with "keep playing" or
"finish the wrap-up", and lints and commits the memory when it may end; no client hooks), stream its output
to runtime/sessions/<playthrough>/Sxxx.{jsonl,log}, enforce the deadline, then make sure a save exists,
close the memory session (with a short "fix your notes" pass if lint fails), and update the stuck
detector. Game crashes and hangs are recovered by relaunching and reloading the last save; a claude process
that prints nothing for AGENT_SILENT_MINUTES is killed and resumed. Every game launch first requires Steam to be
offline (steamui_login.txt). Faults, recoveries and runner start/stop go to runner_events.jsonl (dashboard).

Safety: the runner only ever loads saves that belong to this playthrough (label `Sxxx-...`, optionally
behind $BG3AI_SAVE_PREFIX, and the save owner recorded for the playthrough). It refuses to take over a
game it didn't start that is already in a loaded save, unless --take-over is given.
"""

from __future__ import annotations

import argparse
import shlex
import sys

import _harness  # noqa: F401  (packages/harness/src and bridge/src on sys.path)
import legacy_cli as lcli
from bg3ai import milestones
from bg3harness import clients, sandbox
from bg3harness.game import controller
from bg3harness.runner import env as renv
from bg3harness.runner import judging
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate

SAVETIME_CMDS = {"run", "status", "scenarios"}  # they list saves (controller.list_saves), as do bench run/resume


def savetime_refused(argv: list[str]) -> bool:
    """Every save choice goes by SaveTime (game.saves): a Python that can't read it is refused, in one line."""
    if (why := controller.savetime_problem()) is None:
        return False
    print(f"runner/play.py refused: {why}. Run it with the repo's Python 3.14: uv run python runner/play.py "
          f"{shlex.join(argv)}", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["bench"]:  # bg3bench's matrix CLI (its own arguments) with the live RunHost
        if argv[1:2] in (["run"], ["resume"]) and savetime_refused(argv):
            return 2
        return lcli.cmd_bench(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play sessions until stopped")
    r.add_argument(
        "--playthrough", help="the playthrough (a series: its base name); default with --scenario: the scenario's name"
    )
    r.add_argument(
        "--scenario",
        metavar="NAME",
        help="play scenarios/<NAME>.json as a --runs series: its start (new game or start save), --until "
        "milestone, caps and profile, frozen in series.json; a flag that contradicts it is refused",
    )
    r.add_argument(
        "--scenario-file",
        metavar="PATH",
        help="like --scenario, from a resolved scenario file (bg3harness/scenario_file.py: the v1 shape plus "
        "game_settings difficulty/karmic_dice, checked against the game, and timing), e.g. the bench's "
        "`python -m bg3bench.scenario resolve <v2>`",
    )
    r.add_argument(
        "--hours",
        type=float,
        help="total wall-clock budget (default, or 0: until stopped or the game "
        "ends; every run stops at the end credits / epilogue, milestone game_end)",
    )
    r.add_argument(
        "--session-minutes",
        type=float,
        default=60,
        help='per-session override, live: session-config.json {"minutes": 60}, re-read before each session',
    )
    r.add_argument(
        "--allow-short-sessions",
        action="store_true",
        help=f"allow --session-minutes below {renv.MIN_SESSION_MINUTES} (harness tests only)",
    )
    r.add_argument("--model", help="model for play sessions (claude --model; default opus, or the --profile's)")
    r.add_argument(
        "--profile",
        metavar="TOML",
        help="profiles/*.toml: client, model and effort (pi: endpoint and "
        "model definition; needs --max-cost and --no-keeper, the API key from the env or the repo .env)",
    )
    r.add_argument(
        "--effort",
        choices=renv.EFFORTS,
        help="effort for play sessions (claude --effort; default: "
        "Claude Code's). Per-session override: runtime/sessions/<pt>/session-config.json "
        '{"model": ..., "effort": ...}, re-read before each session',
    )
    r.add_argument("--fix-model", help="model for the fix-notes pass (default: --model)")
    r.add_argument("--fix-effort", choices=renv.EFFORTS, default="low", help="effort for the fix-notes pass")
    r.add_argument(
        "--no-keeper",
        "--no-memory-keeper",
        dest="no_memory_keeper",
        action="store_true",
        help="skip the memory-keeper pass between sessions (runner/memory_keeper.py)",
    )
    r.add_argument(
        "--keeper-model",
        default="same",
        help="model for the memory-keeper pass; 'same' (default) = the session's play model",
    )
    r.add_argument("--keeper-effort", choices=renv.EFFORTS, default="low", help="effort for the memory-keeper pass")
    r.add_argument("--keeper-minutes", type=float, default=5.0, help="time cap of the memory-keeper pass")
    r.add_argument("--fallback-model", help="claude --fallback-model (used when the main model is overloaded)")
    r.add_argument(
        "--autocompact",
        type=int,
        default=None,
        help=f"claude --autocompact <tokens> for play sessions ({rstate.AUTOCOMPACT_MIN}-{rstate.AUTOCOMPACT_MAX}); "
        'default: Claude Code\'s own window. session-config.json "autocompact" overrides it',
    )
    r.add_argument(
        "--save-owner",
        help="character name that prefixes this playthrough's save folders "
        "('Claude' for Claude-<id>__<label>); default: learned from the first save, or $BG3AI_SAVE_OWNER",
    )
    r.add_argument(
        "--until",
        choices=[m[0] for m in milestones.MILESTONES],
        metavar="MILESTONE",
        help="stop at this story milestone (bridge/src/bg3ai/milestones.py, e.g. nautiloid_escaped, "
        "crash_site) and append a run record to runtime/sessions/<pt>/runs.jsonl",
    )
    r.add_argument(
        "--runs",
        type=int,
        metavar="N",
        help="with --until: play N runs in a row, each a new playthrough and a new game: <pt>, <pt>-r2, "
        "... (fresh memory, the same profile.json, per-run reset); records in "
        "runtime/sessions/<pt>/runs.jsonl, --hours caps the whole series. Re-running resumes it",
    )
    r.add_argument(
        "--run-hours",
        type=float,
        metavar="H",
        help="with --until: cap one run at H hours; then it ends as a timeout (a failure) and the next starts",
    )
    r.add_argument(
        "--max-cost",
        type=float,
        metavar="USD",
        help="cap one run's play cost (without --until: this runner's run); past it the run ends "
        "over_budget (a failure). Required for a paid client (pi), cut to what runtime/budget.json "
        "{cap, spent} has left. A series keeps its first run's caps (series.json)",
    )
    r.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help="a paid client (pi): create the global budget runtime/budget.json {cap: USD, spent: 0} when "
        "there is none (a paid run refuses to start without one); one with another cap is refused "
        "(edit the file to change it)",
    )
    r.add_argument(
        "--max-cost-per-day",
        type=float,
        metavar="USD",
        help="long runs: pause (no new session) while the play agent's spend in the trailing 24 h is at "
        "least USD; it plays again once old sessions leave the window (checked between sessions)",
    )
    r.add_argument(
        "--max-tokens",
        type=int,
        metavar="N",
        help="with --until: cap one run's claude tokens (input + output + cache reads + cache writes)",
    )
    r.add_argument(
        "--allow-cap-change",
        action="store_true",
        help="--runs: let --run-hours/--max-cost/--max-tokens differ from series.json (recorded)",
    )
    r.add_argument(
        "--judge",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with --until: after each run, score its in-character decisions against CHARACTER.md "
        "(bg3harness.runner.judging; the judge's cost is recorded apart, never capped). Default: on with --runs "
        "and --scenario, off for a single run",
    )
    r.add_argument(
        "--judge-model",
        help="the judge's model (default $BG3AI_JUDGE_MODEL or "
        f"{judging.DEFAULT_MODEL}); never the model under test. A series keeps its first one (series.json)",
    )
    r.add_argument(
        "--allow-judge-change",
        action="store_true",
        help="--runs: let --judge-model / $BG3AI_JUDGE_MODEL differ from series.json (recorded)",
    )
    r.add_argument(
        "--client",
        choices=sorted(clients.CLIENTS),
        help="the CLI that plays (bg3harness.clients; default claude). A series keeps its first one "
        "(series.json); codex and gemini are offline-tested only",
    )
    r.add_argument(
        "--sandbox",
        choices=sandbox.MODES,
        default="none",
        help="container: the client plays from a container that reaches only the bg3 MCP server (served "
        "over http on the host) and its model API (bg3harness.sandbox, tools/container/README.md); "
        "refuses without the host firewall (sudo tools/container/firewall.sh up). Recorded; a series "
        "keeps its first mode (series.json) and refuses a run in another",
    )
    r.add_argument(
        "--sandbox-unfirewalled",
        action="store_true",
        help="--sandbox container without the host firewall (dev only: the container then reaches every "
        "host port bound to all interfaces); recorded as sandbox_unfirewalled",
    )
    r.add_argument(
        "--allow-sandbox-change",
        action="store_true",
        help="--runs: let --sandbox / --sandbox-unfirewalled differ from series.json (recorded)",
    )
    r.add_argument(
        "--allow-client-change", action="store_true", help="--runs: let --client differ from series.json (recorded)"
    )
    r.add_argument(
        "--allow-version-drift",
        action="store_true",
        help="--runs: play a run although the game build, bg3le, the mod or the repo differ from the "
        "series' first run (series.json pins); the drift is recorded",
    )
    r.add_argument(
        "--character-brief",
        metavar="TEXT",
        help="who the character is, in your words: written to playthroughs/<pt>/profile.json; a new game "
        "then starts with a character-creation phase that designs the build and persona from it",
    )
    r.add_argument(
        "--roleplay",
        choices=rpolicy.ROLEPLAY_MODES,
        help="how strictly the agent plays its character (profile.json; default strict)",
    )
    r.add_argument("--difficulty", choices=rpolicy.DIFFICULTIES, help="difficulty of a new game (profile.json)")
    r.add_argument(
        "--seed",
        type=int,
        metavar="N",
        help="dice seed of the run (D9): the game gets BG3AI_SCENARIO_SETUP=1, the mod's scn.seed once "
        "at the run's start (re-applied after every load), scn.undo at its end",
    )
    r.add_argument(
        "--no-autoskip",
        dest="no_autoskip",
        action="store_true",
        help="let movies, cinematics and voiced lines play (default: skipped; $BG3AI_AUTOSKIP=0 does the same; a "
        "scenario's game_settings.autoskip = false turns it off too)",
    )
    r.add_argument(
        "--take-over", action="store_true", help="allow using a game that is already in a save the runner didn't load"
    )
    r.add_argument(
        "--keep-game",
        action="store_true",
        help="--until / --runs: leave the harness game running when the run (or the series) ends; by "
        "default it is stopped like between runs",
    )
    r.add_argument(
        "--restart-run",
        action="store_true",
        help="--runs: start the next run over even when it has finished play sessions (its sessions, "
        "memory and saves record move aside to *.restarted-<time>). Without it a run with no finished "
        "session whose runner died is restarted anyway; one with finished sessions continues",
    )
    r.add_argument(
        "--plan",
        action="store_true",
        help="print the series.json a series would freeze, its next "
        "run and the first session's command, then exit: no lock, no game, no files written",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="prepare the first session and print the claude "
        "command instead of running it (still starts the game and loads the save)",
    )
    s = sub.add_parser("status", help="summary of the runner, game and last session")
    s.add_argument("--playthrough")
    sub.add_parser("stop", help="finish the current session, then exit")
    rs = sub.add_parser("runs", help="table of a series' --until runs: outcome, clean, failures by class")
    rs.add_argument("--playthrough", required=True, help="the series (run --runs) or playthrough (run --until)")
    rs.add_argument("--failures", action="store_true", help="also list every run's failures")
    rs.add_argument(
        "--backfill",
        action="store_true",
        help="first add the metrics (latency, deaths, fairness "
        "flags, story, time to milestone, model_time's API floor) old records lack, from their files; "
        "present values stay",
    )
    rs.add_argument(
        "--vs",
        metavar="SERIES",
        help="compare with another series (means; flags an API floor "
        "difference instead of reading it as a model-time regression)",
    )
    sub.add_parser("scenarios", help="list scenarios/*.json: ready, or needs a start save")
    j = sub.add_parser("judge", help="(re-)score runs' in-character decisions into their runs.jsonl records")
    j.add_argument("--playthrough", required=True, help="the series (run --runs) or playthrough (run --until)")
    j.add_argument("--run", type=int, help="this run only (default: every run without a persona score)")
    j.add_argument("--all", action="store_true", help="re-judge every run, scored ones too")
    j.add_argument("--judge-model", help="default: series.json's, else $BG3AI_JUDGE_MODEL or " + judging.DEFAULT_MODEL)
    j.add_argument("--allow-judge-change", action="store_true", help="judge with another model than series.json's")
    j.add_argument("--timeout", type=float, default=renv.JUDGE_TIMEOUT_S, help="seconds per run (default %(default)s)")
    a = ap.parse_args(argv)
    if a.cmd in SAVETIME_CMDS and savetime_refused(argv):
        return 2
    return {
        "run": lcli.cmd_run,
        "status": lcli.cmd_status,
        "stop": lcli.cmd_stop,
        "runs": lcli.cmd_runs,
        "scenarios": lcli.cmd_scenarios,
        "judge": lcli.cmd_judge,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
