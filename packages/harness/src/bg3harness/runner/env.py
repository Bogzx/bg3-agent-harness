# pyright: basic
"""The runner's paths, timings and log (moved from runner/play.py, Phase D.3): the one place tests patch them.

Every runner module reads these as renv.<NAME> at call time, so `mock.patch.object(renv, "RUNTIME", tmp)` reaches all
of them. SAVE_DIR and SE_BG3AI_DIR come from bg3harness.steam_paths ($BG3AI_SAVE_DIR / $BG3AI_SE_DIR, else the Larian
profile) once, at import.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from bg3harness import clients, steam_paths

REPO = Path(__file__).resolve().parents[5]

PLAY_DIR = REPO / "play"
PLAYTHROUGHS = Path(os.environ.get("BG3AI_PLAYTHROUGHS", REPO / "playthroughs"))
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
LAUNCH_SH = REPO / "runner" / "launch_game.sh"
CHECK_ALL = REPO / "tools" / "dev" / "check_all.py"
CHECK_PLANS = REPO / "tools" / "dev" / "check_plans.py"   # party-build.md plan lines vs the game's level-up offers
CHECK_PLANS_TIMEOUT = 90
SAVE_DIR = steam_paths.saves_dir()   # $BG3AI_SAVE_DIR, else under $BG3AI_PROFILE_DIR / the default profile
MEMORY_TEMPLATE = REPO / "memory-template"
SCENARIOS = Path(os.environ.get("BG3AI_SCENARIOS", REPO / "scenarios"))   # scenarios/<name>.json (run --scenario)
KNOWLEDGE_DIR = PLAY_DIR / "knowledge"   # agent-writable outside playbooks/ (FAIR-2): snapshot + restore per --runs run
KNOWLEDGE_LOCK = RUNTIME / "knowledge-lock.json"   # its files are read-only during a --runs series (FAIR-2)
# The mod's Ext.IO files (nav_guard_<host uuid>.json, older nav_guard.json: the per-level navigation kill list; nav_<host uuid>.json: breadcrumb mirrors)
SE_BG3AI_DIR = steam_paths.se_dir() / "BG3AI"   # $BG3AI_SE_DIR, else <profile>/Script Extender

HARD_STOP_GRACE = 10 * 60      # deadline + this → SIGINT
SIGINT_GRACE = 30              # then SIGTERM
SIGTERM_GRACE = 15             # then SIGKILL
FIX_SESSION_MINUTES = 10

MIN_SESSION_MINUTES = 20       # shortest session: --session-minutes below it is refused, and no session
                               # starts with less than this left in --hours (start ≤1 min + wrap-up 3 min)

WRAPUP_MINUTES = 3             # wrap-up phase at the end of a session (a third of it, for short ones): notes are
                               # written during play, so wrap-up is save + HANDOFF + Now line (S001/S002: ~35 s)
EARLY_EXIT_MINUTES = 5         # claude exiting cleanly with more than this left = an early exit (logged)
HEALTH_INTERVAL = 30           # seconds between liveness evals during a session
HEALTH_RETRY_INTERVAL = 10     # ... and after a failed one (a hang is declared within ~2 min, see detect_budget_s)
HEALTH_FAILURES = 3            # consecutive failed evals → hung
HEALTH_EVAL_TIMEOUT = 8        # a liveness eval slower than this counts as failed

AGENT_SILENT_MINUTES = float(os.environ.get("BG3AI_AGENT_SILENT_MIN", "4"))  # no claude output this long → hung
                               # (bridge tools cap their waits at ~60 s; a model turn is seconds to ~1 min)
CHECK_ALL_INTERVAL = 30 * 60   # tools/dev/check_all.py between sessions, at most this often (it takes ~25 s)
CHECK_ALL_TIMEOUT = 240        # its own budget is 120 s plus the bridge import; past this it's killed
REVIEW_TIMEOUT = 120           # review_session.py takes ~2 s on a 23-min session (claude-run-2 S065); killed past this
STEAM_RETRY_S = 120            # Steam not offline: refuse to launch, retry this often (never give up)
# Party ready checks (long_rest's "end the day?", region fast travel) stop working in a long-lived game process (se's
# discriminator 2026-10-10 18:07-18:18, research/impl-se.md §6: the 12:48 process failed from S028 on, fresh processes
# work with or without the mod). Between sessions the runner relaunches a game older than this (0 = never by age), or
# one whose last session logged a ready-check failure, and loads the playthrough's save (choose_save).
GAME_MAX_AGE_H = float(os.environ.get("BG3AI_GAME_MAX_AGE_H", "2"))
# The game's RAM grows ~0.9 GB/h (S082: 5754 → 7403 MB RSS in ~2 h, then ramwatch's KILL_ANON_SWAP_MB kill mid-save):
# a session never starts with a game already holding this much RAM+swap (anon incl. swapped out; 0 = no RAM check).
RELAUNCH_RAM_MB = int(os.environ.get("BG3AI_RELAUNCH_RAM_MB", "7000"))
READY_FAIL_RE = re.compile(r"\b(?:ready_failed|end_day_failed)\b(?::\w+)?")
JUDGE_TIMEOUT_S = 15 * 60      # the persona judge's whole budget after a run (two chunks, a retry each, fit)
KILLED_EXITS = (-2, -9, -15, 130, 137, 143)   # claude ended by a signal (SIGINT/KILL/TERM, raw or as 128+n)


def classify_api_failure(texts: list[str], rejected: bool) -> str | None:
    """RUN-7: "limit" (a usage/rate limit, an overload or a server error: wait and retry, never give up), "auth"
    (a key, credit or model problem: the quick-failure stop stays), or None (no API failure). texts: the stream's
    api_error event texts; rejected: a rate_limit event said rejected."""
    if any(API_AUTH_RE.search(t or "") for t in texts):
        return "auth"
    return "limit" if texts or rejected else None


def api_backoff_s(resets_at: float | None, now: float, repeat: int) -> float:
    """How long to wait after an API limit: until its reset time (+slack, at most API_RESET_MAX_S) when the client
    said, else API_BACKOFF_MIN_S doubling per repeat (repeat 1 = the first), at most API_BACKOFF_MAX_S."""
    if resets_at and resets_at > now:
        return min(resets_at - now + API_RESET_SLACK_S, API_RESET_MAX_S)
    return float(min(API_BACKOFF_MIN_S * 2 ** max(repeat - 1, 0), API_BACKOFF_MAX_S))


def mcp_problem(mcp: list[str] | None, strict: bool) -> str | None:
    """RUN-8: why the play process's bg3 MCP server isn't usable, from its init event's "name:status" list, or None.
    strict (claude, whose init lists every server): a missing bg3 counts too; other clients only report a failure."""
    if mcp is None:
        return None
    for m in mcp:
        name, _, status = str(m).partition(":")
        if name == "bg3":
            return None if status == "connected" else f"the bg3 MCP server is {status or 'unknown'}"
    return "the bg3 MCP server is missing from the init event" if strict else None


def exit_reason(reason: str, returncode: int | None) -> str:
    """'killed' when claude died of a signal the runner didn't send (S027: an outside SIGTERM at 3 min was taken
    for a clean exit, so the end lint demanded a handoff and a fix-notes session ran); else `reason`."""
    return "killed" if reason == "exited" and returncode in KILLED_EXITS else reason


STUCK_SESSIONS = 2


def quick_failure(res) -> bool:
    """The client failed at once: it exited within a minute, or the context overflowed before any tool call (the
    kickoff alone is too big: every new session would overflow the same way)."""
    return ((res.ended - res.started < 60 and res.reason == "exited")
            or (res.reason == "context_overflow" and res.tool_calls == 0))


MAX_QUICK_FAILURES = 3         # claude exiting within a minute this many times in a row → give up (auth/config only:
                               # a usage/rate limit or an overload backs off and never gives up, RUN-7)
API_BACKOFF_MIN_S = 60         # RUN-7: the first wait after a limit/overload with no reset time; doubles per repeat
API_BACKOFF_MAX_S = 3600       # ... up to this; a known reset time is waited for (+API_RESET_SLACK_S) up to 6 h
API_RESET_SLACK_S = 60
API_RESET_MAX_S = 6 * 3600
MOD_GONE_S = 180               # RUN-8: Mods.BG3AI missing this long while the game answers → a hang (recover)
# RUN-7: an API failure the runner waits out (usage/rate limit, overload, server errors) vs one a human must fix
API_AUTH_RE = re.compile(r"invalid api key|authentication|credit balance|unknown model|not_found_error|"
                         r"model.{0,40}(?:not found|does not exist|unavailable)|\b401\b|\b403\b", re.I)
MAX_RESUMES = 3                # game crashes per session that resume the same claude conversation
RESUME_MIN_MINUTES = 8         # less than this left after a crash: end the session, the next one picks up
MAX_SETUP_FAILURES = 5         # game launch/load failing this many times in a row → give up
CRASH_LOOP_FAULTS = 6          # game crashes/hangs + setup failures within CRASH_LOOP_WINDOW_S of one run → stop: an
CRASH_LOOP_WINDOW_S = 3600     # unattended run never burns hours (and money) relaunching a game that keeps dying
COST_DAY_S = 24 * 3600         # run --max-cost-per-day: the trailing window
COST_PAUSE_CHECK_S = 600       # ... while it is over the cap, the runner waits and looks again this often
STATUS_LINE = "runner.status.txt"   # runtime/: one line for the orchestrator (`cat`, or `play.py status`)
HEAT_PAUSE_CHECK_S = 15        # heat pause (ramwatch.heat_limits, runtime/thermal.log): look again this often
HEAT_MENU_WAIT_S = 2.0         # the ESC game menu opens/closes in ~0.3 s (bridge server._press_escape)
LOAD_START_TIMEOUT = 60        # ui.menu load → the game leaves the menu, else the load never started
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Flags for every playing session. Keep the cacheable prefix (system prompt + tool schemas) byte-identical
# across sessions: per-machine sections (cwd, git status, env info) move into the first user message, and the
# operator's user-level settings (hooks, plugins) stay out of the playing agent.
# No skills at all (bundled, synced or project): the playing agent's procedures are read-only playbooks in
# play/knowledge/playbooks/, which play/CLAUDE.md says when to read.
# No built-in tools but ToolSearch (it loads the deferred MCP tools' schemas): files, memory, the session's start and
# end and the sandbox are the bg3 MCP server's (bridge/src/bg3ai/memtools.py), so the agent has only mcp__bg3__*
# and no client hook is needed for correctness. play/.claude/settings.json denies the rest again (defence in depth).
# The flags live in runner/clients.py (ClaudeClient builds the command).
PLAY_TOOLS, CLAUDE_FLAGS = clients.PLAY_TOOLS, clients.CLAUDE_FLAGS

MAX_STOP_RESUMES = 6           # claude ended its turn but the stop check says "keep playing"/"finish the wrap-up":
                               # resume it with the reason at most this often per session (was the Stop hook's block)


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} runner: {msg}"
    print(line, flush=True)
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME / "runner.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
