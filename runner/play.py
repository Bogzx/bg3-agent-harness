#!/usr/bin/env python3
"""Autonomous session runner: keeps an AI playing BG3 for hours, one ~1 h Claude Code session at a time.

    runner/play.py run --playthrough NAME [--hours N] [--session-minutes 60] [--model opus] [--effort medium]
    runner/play.py status [--playthrough NAME]
    runner/play.py stop                  # finish the current session, then exit
    runner/play.py run --playthrough NAME --until nautiloid_escaped --runs 3 [--run-hours 4]   # benchmark series
    runner/play.py runs --playthrough NAME [--failures]   # its runs: outcome, clean, failures by class
    runner/play.py judge --playthrough NAME [--run K]     # (re-)score runs' in-character decisions (runner/judge.py)

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
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import types
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bridge" / "src"))
sys.path.insert(0, str(REPO / "tools" / "memory"))
from bg3ai import metrics as latency  # noqa: E402  (stdlib only; doesn't import the MCP server)
from bg3ai import progress as story  # noqa: E402  (stdlib only: story progress per hour, stuck signal)
from bg3ai import milestones  # noqa: E402  (stdlib only: story milestones, for run --until)
from bg3ai.luadebug import LuaDebugError  # noqa: E402  (stdlib only: the game refused or failed an eval)
sys.path.insert(0, str(REPO / "runner"))
import triage  # noqa: E402  (runner/triage.py: post-session Sxxx.triage.md)
import ramwatch  # noqa: E402  (runner/ramwatch.py: RAM watchdog, RSS spikes with the last tool calls)
import runs  # noqa: E402  (runner/runs.py: run --runs naming, per-run resets, failure tagging, the runs table)
import judge  # noqa: E402  (runner/judge.py: the persona judge, a run record's `persona`)
import clients  # noqa: E402  (runner/clients.py: the CLI that plays, --client claude|codex|gemini)
import display_env  # noqa: E402  (runner/display_env.py: DISPLAY/XAUTHORITY/... when a shell lacks them)
import sandbox  # noqa: E402  (runner/sandbox.py: run --sandbox container)
import model_time  # noqa: E402  (runner/model_time.py: model vs harness time, a run record's `model_time`)
sys.path.insert(0, str(REPO / "tools"))
import steam_paths  # noqa: E402  (stdlib: Steam's offline state, the game's install and Larian profile paths)
import bg3le_buildinfo  # noqa: E402  (stdlib: is build/bg3le/dist built with every patches/bg3le patch?)

PLAY_DIR = REPO / "play"
PLAYTHROUGHS = Path(os.environ.get("BG3AI_PLAYTHROUGHS", REPO / "playthroughs"))
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
SESSION_PY = REPO / "tools" / "memory" / "session.py"
LAUNCH_SH = REPO / "runner" / "launch_game.sh"
CHECK_ALL = REPO / "tools" / "dev" / "check_all.py"
REVIEW_PY = REPO / "tools" / "logs" / "review_session.py"
CHECK_PLANS = REPO / "tools" / "dev" / "check_plans.py"   # party-build.md plan lines vs the game's level-up offers
CHECK_PLANS_TIMEOUT = 90
SAVE_DIR = steam_paths.saves_dir()   # $BG3AI_SAVE_DIR, else under $BG3AI_PROFILE_DIR / the default profile
DEBUG_HOST = "127.0.0.1"
DEBUG_PORT = int(os.environ.get("BG3LE_DEBUG_PORT", "9998"))
SAVE_PREFIX = os.environ.get("BG3AI_SAVE_PREFIX", "")
MEMORY_TEMPLATE = REPO / "memory-template"
SCENARIOS = Path(os.environ.get("BG3AI_SCENARIOS", REPO / "scenarios"))   # scenarios/<name>.json (run --scenario)
KNOWLEDGE_DIR = PLAY_DIR / "knowledge"   # agent-writable outside playbooks/ (FAIR-2): snapshot + restore per --runs run
KNOWLEDGE_LOCK = RUNTIME / "knowledge-lock.json"   # its files are read-only during a --runs series (FAIR-2)
# The mod's Ext.IO files (nav_guard_<host uuid>.json, older nav_guard.json: the per-level navigation kill list; nav_<host uuid>.json: breadcrumb mirrors)
SE_BG3AI_DIR = steam_paths.se_dir() / "BG3AI"   # $BG3AI_SE_DIR, else <profile>/Script Extender

HARD_STOP_GRACE = 10 * 60      # deadline + this → SIGINT
SIGINT_GRACE = 30              # then SIGTERM
SIGTERM_GRACE = 15             # then SIGKILL
KILL_GRACE = 3                 # the game ignores SIGTERM (S024: still up after 10 s): SIGKILL it after this many s
KILL_CONFIRM = 30              # then wait this long for the processes and port 9998 to go away
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
EVAL_TIMEOUT = 15
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
GAME_FAULTS = ("crash", "hang")
FAULTS = GAME_FAULTS + ("agent_hang",)
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
LAUNCH_ATTEMPTS = 3            # a launch that dies before the save plays is retried; the 3rd try loads the previous
LAUNCH_BACKOFF = (15, 45)      # good save; then the runner stops (alert). Seconds to wait before the 2nd and 3rd try
MENU_STATES = ("Menu", "LoadMenu")   # client game states at the main menu or its splash ("Press any key")
SPLASH_LOADMENU_S = 10         # LoadMenu this long without the main menu = the splash waiting for a key
MENU_TIMEOUT = 180             # debug port up → main menu (splash pressed past), else wait_menu fails
MENU_POLL = 3                  # seconds between menu/splash checks
LOAD_START_TIMEOUT = 60        # ui.menu load → the game leaves the menu, else the load never started
STALE_EXIT_WITHIN_S = 30       # launch_game.sh exiting STALE_EXIT this soon after the launch = its stale-bg3le refusal
PORT_DEAD_S = 60               # bg3 up this long with port 9998 refusing = dead (bg3le listens ~1 s after start)
BG3_APP_ENV = b"SteamAppId=1086940"
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


# ======================================================================== pure logic (unit-tested)

def session_id(n: int) -> str:
    return f"S{n:03d}"


def predict_session_number(existing: list[int], current: int | None) -> int:
    """The number session.py's `start` will give the next session (max of logs and state, plus one)."""
    return max(existing + [current or 0]) + 1


def wrapup_minutes(session_minutes: float) -> float:
    """Length of the wrap-up phase: 3 min, but at most a third of the session."""
    return min(WRAPUP_MINUTES, session_minutes / 3)


def wrap_minutes(session_minutes: float) -> float:
    """Minutes into the session when WRAP UP starts (the clock line and the memory hooks)."""
    return session_minutes - wrapup_minutes(session_minutes)


def early_exit_minutes(reason: str, started: float, ended: float, session_minutes: float,
                       threshold: float = EARLY_EXIT_MINUTES) -> float | None:
    """Minutes left when claude exited on its own (not a crash, hang, hard stop or abort) more than
    `threshold` minutes before the deadline; else None. The runner then starts the next session at once
    (claude-run-2: S001 used 14 of 30 min, S002 12.6 of 20)."""
    if reason != "exited":
        return None
    left = (started + session_minutes * 60 - ended) / 60
    return round(left, 1) if left > threshold else None


# Early exits are kept (decided 2026-10-11 from S040-S046): a session ending 6-7 min early at a milestone costs only
# the boundary (previous claude end → next claude start: 29-86 s, median ~45 s, mostly the memory keeper's 25-51 s),
# which a full-length session pays too: play stays ~97 % of the wall clock either way, while bouncing a finished
# wrap-up turned into idle filler (S036). boundary_s is kept per session to watch that cost.
BOUNDARY_MAX_S = 15 * 60   # a longer gap is a runner stop, not a boundary


def boundary_s(history: list[dict], started: float) -> float | None:
    """Seconds from the previous play session's end to this one's claude start (None: first, or a runner stop)."""
    prev = next((h for h in reversed(history) if h.get("kind", "play") == "play" and h.get("ended")), None)
    if prev is None:
        return None
    gap = started - prev["ended"]
    return round(gap, 1) if 0 <= gap <= BOUNDARY_MAX_S else None


def detect_budget_s(kind: str) -> float:
    """Worst case from a fault to claude stopped and recovery starting, with the current constants.
    crash/port lost: next health check; game hang: HEALTH_FAILURES slow evals; agent hang: the silence limit.
    Then claude gets SIGINT, SIGTERM, SIGKILL (the main loop ticks every 2 s)."""
    probe = HEALTH_EVAL_TIMEOUT + 5  # eval timeout + LuaDebugClient connect timeout
    stop = SIGINT_GRACE + SIGTERM_GRACE + 2 * 3
    if kind == "crash":
        return HEALTH_INTERVAL + 1 + stop
    if kind == "hang":
        return HEALTH_INTERVAL + probe + (HEALTH_FAILURES - 1) * (HEALTH_RETRY_INTERVAL + probe) + stop
    if kind == "agent_hang":
        return AGENT_SILENT_MINUTES * 60 + 2 + stop
    raise ValueError(kind)


def deadlines(start: float, session_minutes: float, grace: float = HARD_STOP_GRACE) -> tuple[float, float]:
    """(soft deadline shown to the agent, hard stop when the runner interrupts Claude)."""
    deadline = start + session_minutes * 60
    return deadline, deadline + grace


# A session never ends mid-fight (S078): the bg3 MCP server holds the clock while a fight that was on at WRAP UP lasts,
# at most its FIGHT_HOLD_S (15 min) past the planned WRAP UP, and writes the moved times to $BG3AI_FIGHT_HOLD_PATH
# (Sxxx.hold.json). The runner moves its deadline and hard stop with it, never more than FIGHT_HOLD_S past its own.
FIGHT_HOLD_S = 15 * 60


def fight_hold_path(state_dir: Path, sid: str) -> Path:
    return state_dir / f"{sid}.hold.json"


def held_deadline(path: Path, deadline: float) -> float:
    """The session deadline with the MCP server's fight hold applied (bounded), or `deadline`."""
    try:
        held = float(json.loads(path.read_text()).get("deadline") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return deadline
    return max(deadline, min(held, deadline + FIGHT_HOLD_S))


def session_budget(now: float, run_end: float | None, session_minutes: float,
                   min_minutes: float = MIN_SESSION_MINUTES) -> float | None:
    """Minutes for the next session given the --hours budget; None when there's no room for one."""
    if run_end is None:
        return session_minutes
    left = (run_end - now) / 60
    if left < min_minutes:
        return None
    return min(session_minutes, left)


HANDOFF_SAVE_RE = re.compile(r"^Save to load:\s*(.+)$", re.M)


def parse_handoff_save(text: str) -> str | None:
    """The save label HANDOFF.md asks for ('Save to load: "S042-end" — ...'), or None."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    m = HANDOFF_SAVE_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    q = re.match(r'["“`\']([^"”`\']+)["”`\']', value)
    label = q.group(1) if q else re.split(r"\s+[—–-]\s+|\s+\(", value)[0]
    label = label.strip().strip("`")
    if not label or label.lower().startswith(("(none", "none", "n/a")):
        return None
    return label


SAVE_FOLDER_RE = re.compile(r"^(?P<owner>.+)-(?P<num>\d+)__(?P<label>.+)$")


@dataclass(frozen=True)
class Save:
    folder: str
    owner: str
    label: str
    mtime: float


def parse_save_folder(name: str, mtime: float = 0.0) -> Save | None:
    m = SAVE_FOLDER_RE.match(name)
    if not m:
        return None
    return Save(name, m.group("owner"), m.group("label"), mtime)


def is_ai_label(label: str, prefix: str = SAVE_PREFIX) -> bool:
    """Saves the agent makes are labelled Sxxx-<slug>, optionally behind the playthrough's prefix."""
    return re.match(rf"^{re.escape(prefix)}S\d{{3}}-", label) is not None


def is_ai_save(s: Save, prefix: str = SAVE_PREFIX) -> bool:
    """An `Sxxx-` save of this playthrough. $BG3AI_SAVE_PREFIX is used two ways: as a label prefix
    (`pt1-S010-end`) and, by the MCP server and docs/HANDOFF.md (`BG3AI_SAVE_PREFIX=Claude-`), as a folder
    prefix: the character's name plus the dash the game adds (`Claude-123__S001-end`). The agent's
    save_game never prefixes the label, so a folder-prefix match takes a plain `Sxxx-` label."""
    if is_ai_label(s.label, prefix):
        return True
    return bool(prefix) and s.folder.startswith(prefix) and is_ai_label(s.label, "")


def label_matches(label: str, wanted: str, prefix: str = SAVE_PREFIX) -> bool:
    return label == wanted or label == prefix + wanted


SAVE_WINDOW_SLACK_S = 120.0   # a save this close to one of the playthrough's play windows still counts as written in it


def _first_last_t(path: Path) -> tuple[float, float] | None:
    """The first and last "t" of a jsonl file (a Sxxx.turns.jsonl: the session's first and last stream event), reading
    only its head and tail; None when unreadable or without times."""
    try:
        with open(path, "rb") as f:
            head = f.readline()
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().splitlines()
    except OSError:
        return None
    ts = []
    for ln in [head] + list(reversed(tail)):
        try:
            t = json.loads(ln).get("t")
        except (ValueError, AttributeError):
            continue
        if isinstance(t, (int, float)):
            ts.append(float(t))
            if len(ts) == 2:
                break
    return (min(ts), max(ts)) if ts else None


def play_windows(history: list[dict] | None, state_dir: Path | None) -> list[tuple[float, float]]:
    """When this playthrough was being played: its play sessions' [started, ended] (runner.json history) and each
    Sxxx*.turns.jsonl's first..last event (a session the runner never recorded: it crashed mid-session). pick_save
    takes an `Sxxx-` save of the owner only when it was written in one of these: the owner (the character's name) is
    shared by other playthroughs (replay-hoh saved S027-end / S028-lastlight-prep as "Claude" on 10-15, after
    claude-run-2's S065-end), and a worker's savetest (S999-savetest) is no playthrough's."""
    out = [(float(h["started"]), float(h["ended"])) for h in history or []
           if h.get("kind") == "play" and isinstance(h.get("started"), (int, float))
           and isinstance(h.get("ended"), (int, float))]
    if state_dir is not None:
        try:
            files = sorted(Path(state_dir).glob("S*.turns.jsonl"))
        except OSError:
            files = []
        out += [w for w in (_first_last_t(f) for f in files) if w]
    return sorted(out)


def in_windows(t: float, windows: list[tuple[float, float]], slack: float = SAVE_WINDOW_SLACK_S) -> bool:
    return any(a - slack <= t <= b + slack for a, b in windows)


def played_elsewhere(t: float, windows: list[tuple[float, float]], slack: float = SAVE_WINDOW_SLACK_S) -> bool:
    """A save time after this playthrough began (its first play window) that falls in none of its windows: written
    while another playthrough (or a worker) had the game. Older saves are judged as before (the newest wins anyway)."""
    return bool(windows) and t > windows[0][0] - slack and not in_windows(t, windows, slack)


def save_candidates(saves: list[Save], handoff_label: str | None, owner: str | None,
                    runner_saves: set[str], prefix: str = SAVE_PREFIX,
                    windows: list[tuple[float, float]] | None = None) -> list[Save]:
    """The saves of this playthrough.

    Candidates: the folder HANDOFF names, any `Sxxx-` save of the playthrough's owner, and quicksaves the
    runner itself made for this playthrough (recorded by folder). Never anything else. With `windows`
    (play_windows: when this playthrough was played; empty for a new one), a labelled save counts only when it was
    written in one of them or before the first: another playthrough with the same name, or a worker, wrote the rest.
    """
    cands = []
    for s in saves:
        if s.folder in runner_saves:
            cands.append(s)
        elif is_autosave(s.label):
            continue  # AutoSave_N/QuickSave_N: the game's or a worker's test state (se 18:14 AutoSave_59), never ours
        elif owner is not None and s.owner != owner:
            continue
        elif played_elsewhere(s.mtime, windows or []):
            continue  # same owner, written while another playthrough (or a worker) played
        elif handoff_label and label_matches(s.label, handoff_label, prefix):
            cands.append(s)
        elif owner is not None and is_ai_save(s, prefix):
            cands.append(s)
    return cands


def pick_save(saves: list[Save], handoff_label: str | None, owner: str | None,
              runner_saves: set[str], prefix: str = SAVE_PREFIX,
              windows: list[tuple[float, float]] | None = None) -> Save | None:
    """The newest save of this playthrough (save_candidates)."""
    return max(save_candidates(saves, handoff_label, owner, runner_saves, prefix, windows),
               key=lambda s: s.mtime, default=None)


HANDOFF_SAVE_SLACK_S = 5.0   # a save this soon after the HANDOFF's session ended was still that session's


def parse_handoff_session(text: str) -> int | None:
    """The session that wrote HANDOFF.md: N of its '# Handoff SN → SN+1' heading (memlib.HANDOFF_RE), or None."""
    import memlib as M
    m = M.HANDOFF_RE.search(re.sub(r"<!--.*?-->", "", text, flags=re.S))
    return int(m.group(1)) if m else None


def session_ends(history: list[dict] | None, state_dir: Path | None) -> dict[int, float]:
    """When each play session ended: runner.json history "ended", or its Sxxx.turns.jsonl's last event (a session the
    runner never recorded: it crashed mid-session), whichever is later."""
    out: dict[int, float] = {}

    def put(sid: str, t: float) -> None:
        m = re.match(r"S(\d+)", sid or "")
        if m:
            n = int(m.group(1))
            out[n] = max(out.get(n, t), t)
    for h in history or []:
        if h.get("kind") == "play" and isinstance(h.get("ended"), (int, float)):
            put(str(h.get("session") or ""), float(h["ended"]))
    if state_dir is not None:
        try:
            files = sorted(Path(state_dir).glob("S*.turns.jsonl"))
        except OSError:
            files = []
        for f in files:
            w = _first_last_t(f)
            if w:
                put(f.name, w[1])
    return out


def pick_save_why(saves: list[Save], handoff_label: str | None, owner: str | None,
                  runner_saves: set[str], prefix: str = SAVE_PREFIX,
                  windows: list[tuple[float, float]] | None = None, handoff_session: int | None = None,
                  ends: dict[int, float] | None = None) -> tuple[Save | None, str]:
    """The save a session starts from, and why. HANDOFF's "Save to load" is the agent's decision: it wins over newer
    saves unless one was written after the session that wrote HANDOFF (`handoff_session`, its heading) ended (`ends`,
    session_ends): later play HANDOFF doesn't know about (a session that crashed before its wrap-up, a runner
    quicksave), or HANDOFF's session is unknown. S076 (L8): the newest was S075-fight, saved mid-fight on try 6,
    HANDOFF (S075's) named S075-end; S076's HANDOFF named S075-end again, its S076-end being a losing fight."""
    cands = save_candidates(saves, handoff_label, owner, runner_saves, prefix, windows)
    newest = max(cands, key=lambda s: s.mtime, default=None)
    if newest is None:
        return None, "no save of this playthrough"
    if not handoff_label:
        return newest, "the newest save (HANDOFF names no save)"
    named = max((s for s in cands if label_matches(s.label, handoff_label, prefix)), key=lambda s: s.mtime,
                default=None)
    if named is None:
        return newest, f"the newest save (HANDOFF names \"{handoff_label}\": no such save of this playthrough)"
    if named is newest:
        return named, "HANDOFF's \"Save to load\", also the newest save"
    sid = f"S{handoff_session:03d}" if handoff_session is not None else None
    end = (ends or {}).get(handoff_session) if handoff_session is not None else None
    if end is None:
        return newest, (f"the newest save (HANDOFF names \"{handoff_label}\" but "
                        + (f"{sid} never played here" if sid else "has no '# Handoff Sxxx' heading")
                        + ": can't tell whether it knew the newer saves)")
    if newest.mtime > end + HANDOFF_SAVE_SLACK_S:
        return newest, (f"the newest save (HANDOFF names \"{handoff_label}\", but \"{newest.label}\" was saved "
                        f"after {sid}, which wrote HANDOFF, ended)")
    return named, (f"HANDOFF's \"Save to load\" ({sid}'s choice), not the newer \"{newest.label}\" from the same "
                   "session")


def foreign_saves(saves: list[Save], owner: str | None, runner_saves: set[str], windows: list[tuple[float, float]],
                  prefix: str = SAVE_PREFIX) -> list[str]:
    """The owner's `Sxxx-` saves written outside this playthrough's play windows: another playthrough with the same
    character name (replay-hoh's S027-end / S028-lastlight-prep as "Claude"), a worker's S999-savetest. The MCP
    server's list_saves / load_game("latest") must not offer them (BG3AI_SAVES_EXCLUDE): before claude-run-2's first
    save after a resume, "latest" was replay-hoh's Act 2 save. [] for a new playthrough (no windows)."""
    if not windows or not owner:
        return []
    return sorted(s.folder for s in saves if s.owner == owner and s.folder not in runner_saves
                  and is_ai_save(s, prefix) and played_elsewhere(s.mtime, windows))


def saves_since(saves: list[Save], t: float, owner: str | None, prefix: str = SAVE_PREFIX) -> list[Save]:
    """AI-labelled saves written after t (by this playthrough's owner, when known)."""
    return sorted((s for s in saves if s.mtime >= t and is_ai_save(s, prefix)
                   and (owner is None or s.owner == owner)), key=lambda s: s.mtime)


def relaunch_reason(started: float | None, now: float, last_log: str | None,
                    max_age_h: float = GAME_MAX_AGE_H, session_s: float = 0.0, ram_mb: int | None = None,
                    ram_limit_mb: int = RELAUNCH_RAM_MB) -> str | None:
    """Why the game process should be relaunched before the next session, or None: the last session's log shows a
    party ready check failing (ready_failed / end_day_failed), the game already holds ram_limit_mb or more RAM+swap,
    or the process would be max_age_h or more old by the end of a session that may run session_s (its length plus
    the fight hold: S082 started at 1.9 h and ran to 2.3 h)."""
    m = READY_FAIL_RE.search(last_log or "")
    if m:
        return f"the last session logged {m.group(0)} (ready checks break in a long-lived game process)"
    if ram_mb is not None and ram_limit_mb > 0 and ram_mb >= ram_limit_mb:
        return (f"the game holds {ram_mb} MB RAM+swap (relaunched at {ram_limit_mb} MB: it grows ~0.9 GB/h and "
                f"ramwatch kills it at {ramwatch.KILL_ANON_SWAP_MB} MB)")
    if started and max_age_h > 0 and now - started + session_s >= max_age_h * 3600:
        age = f"{(now - started) / 3600:.1f} h old"
        if session_s:
            age += f" and the session may run {session_s / 60:.0f} min (to {(now - started + session_s) / 3600:.1f} h)"
        return f"the game process is {age} (relaunched at {max_age_h:g} h: ready checks break in a long-lived process)"
    return None


def game_ram_mb() -> int | None:
    """The running game's RAM+swap (RssAnon + VmSwap of all bg3 processes, ramwatch's kill measure), None if none."""
    pids = ramwatch.game_pids()
    return sum(ramwatch.anon_swap_mb(p) for p in pids) if pids else None


def is_autosave(label: str) -> bool:
    return re.match(r"(?i)^(auto|quick)save_\d+$", label) is not None


AGENT_SAVES_KEPT = 50


def agent_quicksaves(actions: list[dict], owner: str | None) -> list[str]:
    """Quicksave folders the MCP server's saver wrote (actions.jsonl "save_game.bg" records with ok and the folder
    found on disk, bridge saver.py), in order; only `owner`'s (None: none, an unknown owner proves nothing)."""
    out = []
    for a in actions:
        if a.get("tool") != "save_game.bg" or not a.get("ok") or not a.get("folder"):
            continue
        s = parse_save_folder(str(a["folder"]))
        if s and owner and s.owner.casefold() == owner.casefold() and is_autosave(s.label) and s.folder not in out:
            out.append(s.folder)
    return out


def foreign_use_since(save: Save, saves: list[Save], actions: list[dict], game_started: float | None,
                      runner_saves: set[str], prefix: str = SAVE_PREFIX) -> str | None:
    """Why the running game may no longer be at `save` (this playthrough's newest save), or None.

    --take-over plays on from the running game; that's only right if nobody else used it since. Workers' live tests
    load and save their own saves (LIVETEST-*) or older playthrough saves, and a relaunch by tools/dev/first_launch.sh
    --continue loads the newest save of any kind. Any of these after `save` was written → load `save` instead."""
    for s in saves:
        if (s.mtime > save.mtime and s.owner == save.owner and s.folder not in runner_saves
                and not is_autosave(s.label) and not is_ai_save(s, prefix)):
            return f"save '{s.label}' was written after {save.label}"
    for a in actions:
        if a.get("t", 0) <= save.mtime or a.get("tool") != "load_game":
            continue
        args = a.get("args") or {}
        m = re.search(r"loaded=(\S+)", str(a.get("result") or ""))
        got = m.group(1) if m else str(args.get("save") or args.get("label") or "")
        if got not in (save.label, save.folder):
            return f"load_game '{got}' ran after {save.label} was written"
    if game_started and game_started > save.mtime:
        return f"the game was relaunched after {save.label} was written (it loads its newest save, not ours)"
    return None


def mark_inflight_stuck(guard_file: Path) -> str | None:
    """After the runner killed a hung game: marks the mod's in-flight background search stuck, so the next start
    counts it as a strike on that level. The mod marks it from its own Tick, which runs on the server thread that
    froze (2026-10-13: a ways warm froze SCL_Main_A for 30 min; the marker stayed fresh, no strike, and the next load
    froze again). guard_file: the file, or the mod's dir (the newest nav_guard*.json: NAV-3 keeps one per
    playthrough, nav_guard_<host uuid>.json). The marker is "inflights"["ways warm"] (NAV-7, one per op) or an
    older file's "inflight". Returns the level, or None when there was nothing in flight."""
    try:
        if guard_file.is_dir():
            files = sorted(guard_file.glob("nav_guard*.json"), key=lambda p: p.stat().st_mtime)
            if not files:
                return None
            guard_file = files[-1]
        d = json.loads(guard_file.read_text())
        f = d.get("inflight") if isinstance(d, dict) else None
        many = d.get("inflights") if isinstance(d, dict) else None
        if isinstance(many, dict) and isinstance(many.get("ways warm"), dict):
            f = many["ways warm"]
        if not isinstance(f, dict) or not f.get("level") or f.get("stuck"):
            return None
        f["stuck"] = True
        guard_file.write_text(json.dumps(d))
    except (OSError, ValueError) as e:
        log(f"nav guard: couldn't mark the in-flight search stuck: {e}")
        return None
    log(f"nav guard: the game hung during {f.get('op')} on {f['level']}: marked stuck (a strike at the next start)")
    return f["level"]


def read_actions_since(t: float, path: Path | None = None, tail_bytes: int = 4_000_000) -> list[dict]:
    """runtime/actions.jsonl records (the MCP server's tool log) with t > `t`; reads only the file's tail."""
    path = path or RUNTIME / "actions.jsonl"
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            data = f.read()
    except OSError:
        return out
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("t", 0) > t:
            out.append(rec)
    return out


OOM_LINE = "killed by the OOM killer"
OOM_SAVE_LIMIT = 2             # OOM kills in a row right after loading the same save → the runner refuses that save
OOM_SPIKE_S = 180              # a ramwatch RSS spike this close before an OOM line in another unit = the game's OOM


def game_scope(stamp: str) -> str:
    """The systemd user scope runner/launch_game.sh runs the game in (its own cgroup, OOMPolicy=continue)."""
    return f"bg3ai-game-{stamp}.scope"


def find_game_oom(journal: str, stamp: str, spikes: list[dict]) -> str | None:
    """Why the game died of an OOM, from `journalctl --user -o short-unix` lines since its launch, or None. A kill in
    the game's own scope counts; one in another unit (a game launched before scopes, e.g. inside the multiplexer's unit) only
    with a ramwatch RSS spike (runtime/ram_spikes.jsonl) within OOM_SPIKE_S before it."""
    scope = game_scope(stamp)
    for line in journal.splitlines():
        if OOM_LINE not in line:
            continue
        head, _, rest = line.partition(" ")
        try:
            t = float(head)
        except ValueError:
            continue
        unit = rest.split(": ")[-2].split()[-1] if ": " in rest else "?"
        at = f"{datetime.fromtimestamp(t):%H:%M:%S}"
        if unit == scope:
            return f"the kernel OOM-killed the game at {at} (its scope {scope})"
        spike = next((sp for sp in spikes if 0 <= t - sp.get("t", 0) <= OOM_SPIKE_S), None)
        if spike:
            return (f"the kernel OOM-killed the game at {at} (in {unit}; its RSS went {spike['from_mb']} → "
                    f"{spike['to_mb']} MB in {spike['seconds']} s just before)")
    return None


def read_spikes(since: float, path: Path | None = None) -> list[dict]:
    """runtime/ram_spikes.jsonl records (runner/ramwatch.py) with t >= since."""
    out = []
    try:
        lines = (path or RUNTIME / "ram_spikes.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines[-200:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("t", 0) >= since:
            out.append(rec)
    return out


def user_journal(since: float) -> str:
    """systemd --user manager lines since `since` (where 'A process of this unit has been killed by the OOM killer'
    lands; the kernel log needs root here). '' when journalctl isn't usable."""
    try:
        r = subprocess.run(["journalctl", "--user", "--no-pager", "-q", "-o", "short-unix", "--since",
                            f"@{int(since)}", "_COMM=systemd"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout


def crash_tools_note(why: str, actions: list[dict], until: float, n: int = 3) -> str:
    """The kickoff warning after a game crash: the last n tool calls (runtime/actions.jsonl) up to `until`, so the
    next session doesn't walk straight back into it (S038: jump_to toward the Shadowfell ledge → OOM)."""
    last = [a for a in actions if a.get("t", 0) <= until + 1][-n:]
    if not last:
        return (f"RUNNER NOTE: the last session ended in a game crash ({why}). No tool calls were logged just "
                "before it; reconcile your notes with get_status.")
    calls = "; ".join(
        f"{datetime.fromtimestamp(a['t']):%H:%M:%S} {a.get('tool')} "
        f"{json.dumps(a.get('args') or {}, separators=(',', ':'))[:140]} → "
        f"{str(a.get('result') or '').splitlines()[0][:90] if a.get('result') else '?'}" for a in last)
    return (f"RUNNER NOTE: the last session ended in a game crash ({why}). The last {len(last)} tool calls before it: "
            f"{calls}. Treat the last one as the likely trigger: don't repeat it at the same spot (a jump: walk "
            "there via route/WAYS instead; a spell/item: another one), and reconcile your notes with get_status.")


def last_loaded_save(actions: list[dict], saves: list[Save], fallback: str | None) -> str | None:
    """The save the game process loaded last: the newest load_game in `actions` (actions.jsonl since its launch) that
    loaded (`loaded=<label>`), as its folder, else `fallback` (the runner's load at the launch; S082's OOM line named
    S078-start, loaded 2 h and two agent reloads before)."""
    for a in reversed(actions):
        m = re.search(r"loaded=(\S+)", str(a.get("result") or "")) if a.get("tool") == "load_game" else None
        if not m:
            continue
        arg = parse_save_folder(str((a.get("args") or {}).get("save") or ""))
        if arg and arg.label == m.group(1):
            return arg.folder
        same = [sv for sv in saves if sv.label == m.group(1) and sv.mtime <= a.get("t", 0)]
        return max(same, key=lambda sv: sv.mtime).folder if same else m.group(1)
    return fallback


def oom_streak_update(streak: dict | None, save: str | None, oom: str | None) -> dict | None:
    """The consecutive-OOM record after a game death: same save again → n+1, another save → 1, no OOM → reset."""
    if not oom or not save:
        return None
    n = streak["n"] + 1 if streak and streak.get("save") == save else 1
    return {"save": save, "n": n, "why": oom, "t": time.time()}


def game_started_at() -> float | None:
    """When the running game was launched (runtime/game.pid is written at each launch), or None."""
    try:
        return (RUNTIME / "game.pid").stat().st_mtime
    except OSError:
        return None


def progress_signature(status: dict | None, journal: list | dict | None) -> dict | None:
    """What counts as progress: level/region/subregion, party levels and XP, journal steps."""
    if status is None and journal is None:
        return None
    sig: dict = {}
    if status:
        sig["level"] = status.get("level")
        sig["region"] = status.get("region")
        sig["subregion"] = status.get("subregion")
        party = status.get("party") or []
        sig["party_levels"] = sorted(str(p.get("level")) for p in party if isinstance(p, dict))
        placed = [p for p in party if isinstance(p, dict) and isinstance(p.get("pos"), list) and len(p["pos"]) >= 3]
        if placed:
            sig["pos"] = [round(float(x), 1) for x in placed[0]["pos"][:3]]   # not compared as is: see session_progress
            # RUN-6: the party's order can change (a swap, the selection); state_drift compares one member by name
            sig["pos_name"] = placed[0].get("name")
            sig["party_pos"] = {str(p["name"]): [round(float(x), 1) for x in p["pos"][:3]] for p in placed
                                if p.get("name")}
        xp = [p.get("xp") for p in party if isinstance(p, dict) and p.get("xp") is not None]
        if xp:
            sig["xp"] = sum(xp)
    if journal is not None:
        quests = journal.get("quests", journal) if isinstance(journal, dict) else journal
        steps = 0
        titles = []
        for q in quests or []:
            if isinstance(q, dict):
                titles.append(f"{q.get('title')}|{q.get('done', False)}")
                steps += len(q.get("steps") or [])
        sig["journal"] = hashlib.sha1("\n".join(sorted(titles)).encode()).hexdigest()[:12]
        sig["journal_steps"] = steps
    return sig


POS_KEYS = ("pos", "pos_name", "party_pos")   # where the party stands: never a save-state change by itself


def made_progress(start_sig: dict | None, end_sig: dict | None) -> bool | None:
    """True/False, or None when it can't be told (a signature is missing). The position is left out: any step
    would count (session_progress counts a far move)."""
    if start_sig is None or end_sig is None:
        return None
    strip = lambda d: {k: v for k, v in d.items() if k not in POS_KEYS}  # noqa: E731
    return strip(start_sig) != strip(end_sig)


# A session also made progress when it scored this many story points (seen-sets only grow: a reload never scores
# twice) or ended this far from where it started on the same level. S040 descended the Shadowfell islands, met
# Nightsong and fought Balthazar (32 pts), wiped and reloaded: same save signature → "no progress" → CHANGE APPROACH.
PROGRESS_MIN_POINTS = 10
PROGRESS_MOVE_M = 60.0


def session_progress(start_sig: dict | None, end_sig: dict | None, points: int | None) -> tuple[bool | None, str]:
    """(progress, why): the save signature changed, or >= PROGRESS_MIN_POINTS story points, or a move of
    >= PROGRESS_MOVE_M; False only when it can be told; None when nothing is known (a crash, no reads)."""
    sig = made_progress(start_sig, end_sig)
    if sig:
        changed = [k for k in end_sig if k not in POS_KEYS and start_sig.get(k) != end_sig.get(k)]
        return True, "changed " + ", ".join(changed)
    if isinstance(points, (int, float)) and points >= PROGRESS_MIN_POINTS:
        return True, f"{points:g} story points"
    a, b = (start_sig or {}).get("pos"), (end_sig or {}).get("pos")
    if sig is False and a and b and math.dist(a, b) >= PROGRESS_MOVE_M:
        return True, f"moved {math.dist(a, b):.0f} m"
    if sig is None:
        return None, "unknown"
    return False, f"same save state, {points if points is not None else '?'} story points"


# A runner restart keeps a running game only if it still shows where the last session ended: between runs the
# orchestrator's acceptance tests load their own saves (S059 08:45 and S063 10:51 started on an Act-2 fixture /
# another save and the agent had to reload by itself). Party levels/XP are left out (summons and level-ups jitter).
DRIFT_KEYS = ("level", "journal", "journal_steps")
DRIFT_MOVE_M = 15.0


def state_drift(end_sig: dict | None, now_sig: dict | None) -> str | None:
    """Why the running game is not where the last session left it (end_sig), or None (same place, or unknown)."""
    if not end_sig or not now_sig:
        return None
    for k in DRIFT_KEYS:
        if k in end_sig and k in now_sig and end_sig[k] != now_sig[k]:
            return f"{k} {now_sig[k]} (the last session ended at {end_sig[k]})"
    a, b, who = end_sig.get("pos"), now_sig.get("pos"), "the party"
    name = end_sig.get("pos_name")
    if name and name in (now_sig.get("party_pos") or {}):   # the same member, whatever the party's order now
        b, who = now_sig["party_pos"][name], name
    if a and b and math.dist(a, b) > DRIFT_MOVE_M:
        return f"{who} stands {math.dist(a, b):.0f} m from where the last session ended"
    return None


def run_events(state_dir: Path, since: float) -> list[dict]:
    """runner_events.jsonl records at or after `since`."""
    out = []
    try:
        lines = (state_dir / "runner_events.jsonl").read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("t", 0) >= since:
            out.append(e)
    return out


def crash_loop(events: list[dict], now: float, run_started: float, n: int = CRASH_LOOP_FAULTS,
               window: float = CRASH_LOOP_WINDOW_S) -> str | None:
    """Why the run should stop as a crash loop (n game crashes/hangs or setup failures within `window` of this run;
    a runner (re)start by the operator counts from its start), or None."""
    since = max(now - window, run_started - 1)
    bad = [e for e in events if e.get("t", 0) >= since and (
        (e.get("kind") == "fault" and e.get("fault") in GAME_FAULTS) or e.get("kind") == "setup_failed")]
    if len(bad) < n:
        return None
    return f"crash loop: {len(bad)} game crashes/hangs/setup failures in {(now - min(e['t'] for e in bad)) / 60:.0f} min"


def day_cost(state_dir: Path, now: float) -> float:
    """The play agent's spend in the trailing COST_DAY_S (finished processes; run --max-cost-per-day)."""
    return runs.claude_totals(state_dir, now - COST_DAY_S)["cost_usd"]


def status_line(now: float, playthrough: str, state: str, run_started: float, sessions: int, seen: dict | None,
                spent_day: float, cost_cap: float | None, faults: int) -> str:
    """runtime/runner.status.txt: what the runner does, for the orchestrator, in one line."""
    ms = milestones.status(seen)
    story = (f"milestones {ms['reached']}/{ms['total']} (Act {ms['act']})"
             + (": THE GAME IS FINISHED" if ms["ended"] else f", next {ms['next'] or '-'}"))
    return (f"{datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S} {playthrough}: {state} | up {(now - run_started) / 3600:.1f} h, "
            f"{sessions} session(s) | {story} | ${spent_day:.2f} in 24 h"
            + (f" (cap ${cost_cap:g})" if cost_cap else "")
            + f" | {faults} game fault(s) in {CRASH_LOOP_WINDOW_S / 60:.0f} min (stop at {CRASH_LOOP_FAULTS})")


def story_points(summary_path: Path) -> int | None:
    """Story progress points of a finished session (Sxxx.progress.json), or None."""
    try:
        return json.loads(summary_path.read_text()).get("points")
    except (OSError, ValueError, AttributeError):
        return None


def until_reached(seen: dict | None, milestone: str) -> dict | None:
    """{id, label} when milestones.status(seen) has `milestone` reached (the progress seen-state), else None."""
    item = next((i for i in milestones.status(seen or {})["items"] if i["id"] == milestone), None)
    return {"id": item["id"], "label": item["label"]} if item and item["reached"] else None


def read_seen(state_dir: Path) -> dict | None:
    """The playthrough's persistent progress seen-state (bg3ai.progress Timeline), or None."""
    try:
        return json.loads((state_dir / "progress_state.json").read_text())
    except (OSError, ValueError):
        return None


def run_record(state_dir: Path, run_started: float, now: float, hit: dict, playthrough: str,
               history: list[dict], outcome: str | None = None, why: str = "", take_over: bool = False,
               cap: str | None = None) -> dict:
    """One runs.jsonl line for an --until run: wall time, sessions, claude cost and tokens (the stream's result
    events, the [end] lines), crashes/hangs and recoveries (runner_events.jsonl), agent reloads (actions.jsonl);
    the outcome (reached | timeout | over_budget | gave_up | runner_error) and the cap that ended it (time | cost |
    tokens), every failure tagged harness/agent/unknown (runner/runs.py), the operator actions, `clean` (reached, no
    harness failure, no operator action), and the benchmark metrics (runs.run_metrics: time to the milestone,
    deaths, fairness flags, latency, story points)."""
    outcome = outcome or ("reached" if hit.get("id") else "timeout")
    if outcome == "timeout":
        cap = "time"
    sessions = [h for h in history if h.get("kind") == "play" and (h.get("started") or 0) >= run_started]
    totals = runs.claude_totals(state_dir, run_started)
    cost, turns, tokens = totals["cost_usd"], totals["turns"], totals["tokens"]
    events = []
    try:
        for ln in (state_dir / "runner_events.jsonl").read_text().splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("t", 0) >= run_started - 1:   # event times are rounded to 0.1 s
                events.append(e)
    except OSError:
        pass
    actions = [a for a in read_actions_since(run_started) if a.get("t", now) <= now + 1]
    reloads = [a for a in actions if a.get("tool") == "load_game"]
    failures = runs.classify_run(state_dir, run_started, now, sessions, events, actions, outcome, why, cap)
    _, operator = runs.event_failures(events)
    if take_over:
        operator.append({"t": round(run_started, 1), "what": "--take-over (the run may have started from a game "
                                                              "it didn't set up)"})
    counts = runs.class_counts(failures)
    return {"t": round(now, 1), "playthrough": playthrough, "outcome": outcome, "why": why[:300],
            "clean": runs.is_clean(outcome, failures, operator),
            "harness_failures": counts[runs.HARNESS], "agent_failures": counts[runs.AGENT],
            "unknown_failures": counts[runs.UNKNOWN],
            "milestone": hit.get("id"), "label": hit.get("label"),
            "reached_in": hit.get("session"), "reached_at": hit.get("t"),
            "started": round(run_started, 1), "wall_min": round((now - run_started) / 60, 1),
            "sessions": len(sessions), "play_min": round(sum(h.get("minutes") or 0 for h in sessions), 1),
            "cost_usd": round(cost, 4), "turns": turns, "tokens": tokens,
            "crashes": sum(1 for e in events if e.get("kind") == "fault" and e.get("fault") in GAME_FAULTS),
            "agent_hangs": sum(1 for e in events if e.get("kind") == "fault" and e.get("fault") == "agent_hang"),
            "recoveries": sum(1 for e in events if e.get("kind") == "recovered"),
            "setup_failures": sum(1 for e in events if e.get("kind") == "setup_failed"),
            "reloads": len(reloads), "failures": failures, "operator": operator, "cap": cap,
            **runs.run_metrics(state_dir, run_started, hit.get("t"), failures),
            "milestones_reached": milestones.status(read_seen(state_dir))["reached"]}


def judge_record(state_dir: Path, rec: dict, model: str, timeout: float = JUDGE_TIMEOUT_S, client=None,
                 stop=lambda: False) -> dict:
    """A run record's `persona` (judge.persona_field of judge.judge_run) within `timeout` seconds overall; stop():
    give up now (a second operator signal). Never raises: a failed or late judge is a judge_failed persona. Its
    cost is persona.cost_usd, apart from the run's capped cost_usd (the judge runs in a temp dir, not state_dir)."""
    deadline = time.time() + timeout

    def call(c, prompt):
        left = deadline - time.time()
        if stop():
            raise RuntimeError("judge interrupted (operator signal)")
        if left < 1:
            raise RuntimeError(f"judge timed out ({timeout:g} s)")
        return judge.call_client(c, prompt, timeout=min(judge.TIMEOUT_S, left))
    try:
        res = judge.judge_run(state_dir, rec, client=client or judge.ClaudeCLI(model), call=call)
    except Exception as e:  # noqa: BLE001 - judge_run never raises; a judge never fails the run all the same
        res = {"status": judge.FAILED, "score": None, "judge_model": model, "error": f"{type(e).__name__}: {e}"}
    return judge.persona_field(res)


def keeper_model(option: str | None, play_model: str) -> str:
    """The memory keeper's model: --keeper-model, where "same" (the default) is the session's play model. A
    benchmark run then measures one model end to end, its own note-keeping included (BENCHMARK-BIAS.md)."""
    return play_model if not option or option == "same" else option


def is_stuck(history: list[dict], n: int = STUCK_SESSIONS) -> bool:
    """The last n finished sessions each ended with no progress (unknowns don't count as stuck)."""
    recent = [h for h in history if h.get("kind", "play") == "play"][-n:]
    return len(recent) == n and all(h.get("progress") is False for h in recent)


# A concrete pre-flight for pace, right in the kickoff (the most salient spot): in claude-run-2 S001 the
# agent read the CLAUDE.md pipelining rules, then blocked on every walk anyway (31 wait=true walks, 0% of
# actions pipelined, idle median 2.27 s) and wrapped up 16 min early when a ladder bugged out.
PACE_PREFLIGHT = """PACE PRE-FLIGHT (read before your first game action):
- Game tools are named mcp__bg3__<name> (e.g. mcp__bg3__get_status); the short names below are shorthand.
- The character must be busy while you think. A walk is: issue it, then do something that doesn't
  depend on it, then react to its result. For example:
    travel_to(objective="o1")                      -> started, returns at once
    queue_actions('[{"tool":"interact","target":"e15"}]')   -> what to do on arrival
    look() / get_frontier() / note("[tag] ...")      -> while it walks
    wait_for("queue")                              -> only now, if nothing else is left to do
- Never pass wait=true to move_to, walk_toward or travel_to, not even after a walk stopped or
  failed (failures come back at once anyway), and not because you did it earlier this session. Never
  wait() to pass time: wait_for("my_turn" | "idle" | a job id).
- Stuck (a loop, a bugged action, a route that keeps failing) never ends the session: follow
  knowledge/playbooks/stuck.md section 0 (cancel and retry differently, reload the last good save, lead
  with another party member, another objective) and keep playing until the ⏱ line says WRAP UP NOW."""


def build_kickoff(sid: str, playthrough: str, loaded_save: str | None, notes: list[str], briefing: str = "",
                  save_why: str = "") -> str:
    """The first message of a session. `briefing` is session.py start's memory injection (the runner opened the
    session): it leads the message, where the SessionStart hook used to put it. Without it (the runner couldn't open
    the session) the agent calls session_start. It names no deadline: every time shown before WRAP UP NOW was read
    as a clock to beat (S066-S082 wrapped up 7-13 min early)."""
    lines = [
        f"This is session {sid} of the BG3 playthrough '{playthrough}', started by the autonomous runner.",
        ("Your memory briefing (PLAYTHROUGH.md and HANDOFF.md) is above. " if briefing else
         "First call session_start: it shows your memory briefing (PLAYTHROUGH.md and HANDOFF.md). ")
        + "Run the session-start playbook (memory_read knowledge/playbooks/session-start.md), then "
        "play following CLAUDE.md. Your notes are files under memory/: use the memory_* tools and note.",
        "Keep playing until the ⏱ line in tool results says WRAP UP NOW: you'll be told when to wrap up. Then run "
        "the session-wrapup playbook; its last step is end_session, and you end your turn when that reports the "
        "commit. Nobody will answer questions: decide yourself and write down why.",
        PACE_PREFLIGHT,
    ]
    if briefing:
        lines.insert(0, briefing)
    if loaded_save:
        why = f" ({save_why})" if save_why else ""
        lines.append(f"The runner has already loaded save \"{loaded_save}\"{why}. Don't reload it; verify state "
                     "with get_status.")
    for note in notes:
        lines.append(note)
    return "\n\n".join(lines)


def resume_prompt(why: str, notes: list[str]) -> str:
    """The message that resumes a session's claude conversation after the runner recovered a crashed game."""
    lines = [f"RUNNER: the game {why}. The runner relaunched it and loaded the latest save; the same session "
             f"continues (you'll be told when to wrap up). Call get_status first: anything after that save is lost, so redo it "
             "and note the crash in the session log in one line. Then keep playing following CLAUDE.md."]
    return "\n\n".join(lines + notes)


def agent_resume_prompt(notes: list[str]) -> str:
    """The message that resumes a session whose claude process went silent and was stopped by the runner."""
    lines = [f"RUNNER: your previous turn produced no output for {AGENT_SILENT_MINUTES:g} min, so the runner stopped "
             f"and resumed you; the game kept running. Same session (you'll be told when to wrap up). Call "
             "get_status, then keep playing following CLAUDE.md; keep each step short (no tool call or think longer "
             "than a minute)."]
    return "\n\n".join(lines + notes)


def stop_resume_prompt(reason: str) -> str:
    """The message that resumes a session whose claude ended its turn while the stop check (session.py stop, the old
    Stop hook) still blocks: keep playing, finish the wrap-up, or fix lint errors."""
    return f"RUNNER: you ended your turn, but this session can't end yet. {reason}"


def new_game_note(owner: str) -> str:
    return (f"RUNNER NOTE: this is a new game. Name your character exactly \"{owner}\" (create_character plan "
            f"\"name\": \"{owner}\"; create_character types it and verifies it before Venture forth). Saves are "
            "matched to this playthrough by that name.")


ROLEPLAY_MODES = ("strict", "light")
DIFFICULTIES = ("Explorer", "Balanced", "Tactician")   # new_game's
ROLEPLAY_TEXT = {
    "strict": "Roleplay: strict. Play as this character for the whole game: dialogue, companions, moral choices and "
              "combat style follow CHARACTER.md; odds, safety and approval only break ties between in-character "
              "options.",
    "light": "Roleplay: light. Keep the persona in dialogue and big choices; odds, safety and approval weigh as much "
             "as the character does.",
}


def load_profile(path: Path) -> dict:
    """playthroughs/<pt>/profile.json: {"character": {"brief", "name"?, "constraints"?, "roleplay"}, "difficulty"?}.
    A missing or broken file, or a character without a brief, means today's flow (no character block)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    c = raw.get("character")
    if isinstance(c, dict) and isinstance(c.get("brief"), str) and c["brief"].strip():
        out["character"] = dict(c, roleplay=c.get("roleplay") if c.get("roleplay") in ROLEPLAY_MODES else "strict")
    else:
        out.pop("character", None)
    if out.get("difficulty") not in DIFFICULTIES:
        out.pop("difficulty", None)
    return out


def update_profile(profile: dict, brief: str | None = None, roleplay: str | None = None,
                   difficulty: str | None = None) -> dict:
    """The profile with run --character-brief/--roleplay/--difficulty applied; other keys are kept."""
    out = json.loads(json.dumps(profile))
    c = dict(out.get("character") or {})
    if brief and brief.strip():
        c["brief"] = brief.strip()
    if roleplay:
        c["roleplay"] = roleplay
    if c.get("brief"):
        c.setdefault("roleplay", "strict")
        out["character"] = c
    if difficulty:
        out["difficulty"] = difficulty
    return out


def character_creation_note(profile: dict, name: str | None) -> str:
    """Phase 0 (CHAR-1): the first session of a new game with a character brief designs and creates the character."""
    c = profile.get("character") or {}
    lines = [
        "RUNNER NOTE: PHASE 0, CHARACTER CREATION. This is a new game and the playthrough has a character brief. "
        "Before any play, memory_read knowledge/playbooks/character-creation.md and follow it in order: new_game at the "
        "difficulty below, design the build and the persona from the brief, write memory/CHARACTER.md and the build "
        "plan in party-build.md (during new_game's load), then create_character with the name. Its result is the "
        "first look (name/race/class as the game spells them, objectives, what's nearby) and queues the first save "
        "itself: the runner learns the character's name from it (if the result says it wasn't started: "
        "save_game(label=\"S001-start\")). Then make the first move at once, with no more reads, and write "
        "PLAYTHROUGH.md and the log while it runs. Then keep playing as that character following CLAUDE.md.",
        "Character brief (the operator's words):\n> " + c.get("brief", "").strip().replace("\n", "\n> "),
    ]
    if c.get("constraints"):
        lines.append(f"Constraints: {c['constraints']}")
    lines.append(ROLEPLAY_TEXT[c.get("roleplay") if c.get("roleplay") in ROLEPLAY_TEXT else "strict"])
    d = profile.get("difficulty")
    lines.append(f"Difficulty: new_game(\"{d or 'Explorer'}\")" + ("." if d else " (the default; the profile sets none)."))
    if name:
        lines.append(f"Name: exactly \"{name}\" (create_character plan \"name\"); saves are matched to this "
                     "playthrough by it.")
    else:
        lines.append("Name: yours to choose, to fit the brief (create_character plan \"name\"). Make it distinctive: "
                     "it becomes this playthrough's save owner.")
    return "\n".join(lines)


def scenario_start_note(scenario: str | None, profile: dict, loaded: str | None) -> str:
    """The first session of a scenario run that starts from a save: no new game, the character is the save's."""
    c = profile.get("character") or {}
    lines = [
        f"RUNNER NOTE: SCENARIO START ({scenario}). This run starts mid-game from a fixed save, not a new game: the "
        f"runner loaded \"{loaded or 'the start save'}\". The character"
        + (f" ({c['name']})" if c.get("name") else "") + " is the save's: name, race, class, level, party, gear and "
        "journal are what the game shows. Your memory is new; HANDOFF.md holds the harness's short start note. "
        "Before playing on: get_status and get_party, then write memory/CHARACTER.md (the format in "
        "knowledge/playbooks/character-creation.md; header lines from the game) with the persona from the brief "
        "below, the party in party-build.md and the Character line in PLAYTHROUGH.md. Never start a new game.",
    ]
    if c.get("brief"):
        lines.append("Character brief (the operator's words):\n> " + c["brief"].strip().replace("\n", "\n> "))
    if c.get("constraints"):
        lines.append(f"Constraints: {c['constraints']}")
    lines.append(ROLEPLAY_TEXT[c.get("roleplay") if c.get("roleplay") in ROLEPLAY_TEXT else "strict"])
    return "\n".join(lines)


def fix_notes_prompt(lint_report: str) -> str:
    return ("The session is over, but `session.py end` refused to commit your memory because lint "
            "failed. Do NOT play or touch the game. Only fix the memory files so lint passes (memory_read, "
            "memory_edit, memory_write: each result shows the file's remaining lint errors), then call end_session "
            f"and end your turn. You have {FIX_SESSION_MINUTES} minutes.\n\nLint report:\n{lint_report[-6000:]}")


STUCK_DIRECTIVE = (
    "RUNNER DIRECTIVE, CHANGE APPROACH: the last {n} sessions ended with no measurable progress "
    f"(same level, region, party levels/XP and journal, under {PROGRESS_MIN_POINTS} story points, no far move: "
    "{what}). What you've been doing isn't working. "
    "Before anything else: run the stuck playbook, write three approaches in hypotheses.md that are "
    "genuinely different from the ones in your recent session logs, and pick one you haven't tried. "
    "Consider parking the blocked objective and pursuing a different quest, region or main-story lead.")


def stuck_directive(history: list[dict], n: int = STUCK_SESSIONS) -> str:
    sig = next((h.get("end_sig") for h in reversed(history) if h.get("end_sig")), None) or {}
    what = ", ".join(f"{k}={v}" for k, v in sig.items() if k in ("level", "region", "subregion")) or "n/a"
    return STUCK_DIRECTIVE.format(n=n, what=what)


def _short(value, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def timing_event(ev: clients.Event) -> dict | None:
    """A non-claude client's Event in the stream-json shape bg3ai.metrics.StreamTiming reads (assistant text and
    tool calls, tool results, the process's result), so its turns.jsonl and latency report work the same; claude's
    raw events go to StreamTiming as they are. None: nothing StreamTiming times."""
    if ev.kind == "text":
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": ev.text}]}}
    if ev.kind == "tool_call":
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ev.call_id,
                                                              "name": ev.tool, "input": ev.args or {}}]}}
    if ev.kind == "tool_result":
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ev.call_id,
                                                         "content": ev.text, "is_error": ev.is_error}]}}
    if ev.kind == "result":
        return {"type": "result", "num_turns": ev.turns, "duration_api_ms": None,
                "usage": {"output_tokens": (ev.tokens or {}).get("output")}}
    return None


def gates_decision(char_name: str | None, owner: str | None, new_game: bool) -> bool | None:
    """Whether the save/load gates may be opened for the game that is up.

    True: a brand-new game, or the controlled character is the playthrough's save owner.
    False: a character is loaded and it's someone else (e.g. the user's own playthrough).
    None: can't tell (no name, or no owner known yet), so leave the gates alone.
    """
    if new_game:
        return True
    if not char_name or not owner:
        return None
    return char_name.strip().casefold() == owner.strip().casefold()


def save_identity(owner: str | None, env_prefix: str = SAVE_PREFIX) -> tuple[str, str]:
    """(BG3AI_SAVE_OWNER, BG3AI_SAVE_PREFIX) for the MCP server. The prefix is the runner's own $BG3AI_SAVE_PREFIX,
    else the owner plus the dash the game adds (`Claude-` for `Claude-<n>__<label>`). The <n> is per save, not per
    playthrough, so the folder name can't tell two playthroughs of one name apart. No owner yet (a new game before
    character creation): both empty, and the server adopts the name create_character typed."""
    owner = (owner or "").strip()
    return owner, env_prefix or (f"{owner}-" if owner else "")


def gates_env(allow: bool | None) -> dict:
    """BG3AI_SAVE_GATES for the MCP server: "1"/"0" = gates_decision opened/closed the save/load gates for this game
    (the server's _enable_save_gates follows it); absent when the runner couldn't tell (the server's own default)."""
    return {} if allow is None else {"BG3AI_SAVE_GATES": "1" if allow else "0"}


PLAY_SETTINGS = [
    # (op, args) applied before every session; cutscene autoskip is a user requirement.
    ("ui.config", {"autoskip": True}),
    ("ui.settings", {"show_tutorials": False}),
    ("ui.dismiss", {}),
]


def ui_strict_wanted(env) -> bool | None:
    """BG3AI_UI_STRICT: "1" on, "0" off, unset/anything else None (leave the game's mode alone; default off)."""
    return {"1": True, "0": False}.get(str(env.get("BG3AI_UI_STRICT", "")).strip())


def ui_strict_lua(on: bool) -> str:
    """Client Lua: set the strict mode, -> "<on> <refused> <lua timeouts> <ui timeouts>" or "missing"."""
    return ("local f = Ext._Internal and Ext._Internal.UiStrict if type(f) ~= 'function' then return 'missing' end "
            f"f({'true' if on else 'false'}) local s, n = f() local _, lt, ut = Ext._Internal.UiSerialize() "
            "return tostring(s) .. ' ' .. tostring(n) .. ' ' .. tostring(lt) .. ' ' .. tostring(ut)")


def ui_strict_text(reply: str) -> str:
    """ui_strict_lua's reply as one log line."""
    parts = str(reply).split()
    if len(parts) != 4 or parts[0] not in ("true", "false"):
        return f"not set ({reply!r}: the running bg3le lacks patch 0010?)"
    return (f"{'ON' if parts[0] == 'true' else 'off'} (refused {parts[1]}, lock timeouts lua {parts[2]} "
            f"ui {parts[3]})")


# The player's own created character (the avatar), not the selected one: Osi.GetHostCharacter() and the
# client's ClientControl follow the selection, so picking Lae'zel used to close the save/load gates.
# Server context. DB_Avatars first, then the Avatar component; the host character only as a last resort.
AVATAR_NAME_LUA = (
    'local function nm(g) local ok, n = pcall(function() return '
    'Ext.Loca.GetTranslatedString(Osi.GetDisplayName(g)) end) return ok and n or nil end '
    'local ok, rows = pcall(function() return Osi.DB_Avatars:Get(nil) end) '
    'if ok and rows and rows[1] and rows[1][1] then local n = nm(rows[1][1]) if n and n ~= "" then return n end end '
    'for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("Avatar") or {}) do '
    '  local dn = e.DisplayName '
    '  if dn then local n = Ext.Loca.GetTranslatedString(dn.NameKey.Handle.Handle) if n and n ~= "" then return n end end '
    'end '
    'return nm(Osi.GetHostCharacter()) or ""')

MOD_DIR = REPO / "mod"
MOD_READY_LUA = "return tostring(Mods.BG3AI ~= nil and Mods.BG3AI.BG3AI ~= nil and Mods.BG3AI.BG3AI.RPC ~= nil)"
RELOAD_TIMEOUT = 60
WAIT_REPORT_AFTER = 90         # a runner wait longer than this logs what it waits for ...
WAIT_REPORT_EVERY = 30         # ... every this many seconds


def mod_tree_hashes(root: Path = MOD_DIR) -> dict[str, str]:
    """{relative path: sha1} for every file under mod/ (the Lua tree the game loads)."""
    out = {}
    for p in sorted(root.rglob("*")) if root.is_dir() else []:
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def changed_files(old: dict | None, new: dict) -> list[str]:
    """Files added, removed or edited between two mod_tree_hashes; everything when old is unknown."""
    if old is None:
        return sorted(new)
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


# ---------------------------------------------------------------- self-update (between sessions)

BRIDGE_SRC = REPO / "bridge" / "src"
HANDOVER = RUNTIME / "runner-handover.json"   # state the runner hands to its own execv'd successor
HANDOVER_MAX_AGE = 300


def _sha1(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def loaded_code_files(repo: Path = REPO, modules=None) -> list[Path]:
    """Source files of the repo modules loaded in this process: runner/*.py plus what runs in-process from
    tools/ and bridge/src (metrics, progress, skipper, ...). Never a venv or site-packages."""
    out = set()
    for m in list((modules if modules is not None else sys.modules).values()):
        f = getattr(m, "__file__", None)
        if not f or not f.endswith(".py"):
            continue
        p = Path(f).resolve()
        if p.is_relative_to(repo) and not any(x in p.parts for x in (".venv", "site-packages", "__pycache__")):
            out.add(p)
    return sorted(out)


class CodeWatch:
    """Remembers each loaded repo module's hash when first seen (at start, or on a later lazy import) and
    reports the ones edited on disk since: that code is stale in this process."""

    def __init__(self, files=None):
        self.hashes: dict[Path, str | None] = {}
        self.track(files)

    def track(self, files=None) -> None:
        for p in loaded_code_files() if files is None else files:
            self.hashes.setdefault(p, _sha1(p))

    def changed(self, files=None) -> list[Path]:
        self.track(files)
        return [p for p, h in self.hashes.items() if _sha1(p) != h]


def tree_hashes(root: Path, pattern: str = "*.py") -> dict[str, str]:
    """{relative path: sha1} for the files matching pattern under root (the bridge's source tree)."""
    out = {}
    for p in sorted(root.rglob(pattern)) if root.is_dir() else []:
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def code_check_cmd(script: Path) -> list[str]:
    """Imports the new runner the way a restart would (argparse --help exits 0 after every top-level import)."""
    return [sys.executable, str(script), "run", "--help"]


def restart_argv() -> list[str]:
    """Same interpreter, flags and arguments (cwd is unchanged by execv, so relative paths still hold)."""
    return [sys.executable, *getattr(sys, "orig_argv", [sys.executable, *sys.argv])[1:]]


def write_handover(data: dict, path: Path | None = None) -> None:
    path = path or HANDOVER
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**data, "pid": os.getpid(), "t": time.time()}))
    tmp.replace(path)


def read_handover(path: Path | None = None, now: float | None = None) -> dict | None:
    """The handover the previous incarnation of this process wrote (same pid, fresh); always consumed."""
    path = path or HANDOVER
    try:
        return peek_handover(path, now)
    finally:
        path.unlink(missing_ok=True)


def peek_handover(path: Path | None = None, now: float | None = None) -> dict | None:
    """read_handover without consuming it: is this process the execv'd successor of a runner mid-run?"""
    try:
        data = json.loads((path or HANDOVER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("pid") != os.getpid():
        return None
    if (now or time.time()) - data.get("t", 0) > HANDOVER_MAX_AGE:
        return None
    return data


class RestartRunner(Exception):
    """Raised between sessions when the runner's own code changed on disk: cmd_run execs the new code."""


class WaitReporter:
    """Watchdog for long runner waits: past WAIT_REPORT_AFTER s, logs what it waits for every WAIT_REPORT_EVERY s
    (so a silent runner log always says why it's silent)."""

    def __init__(self, what: str, timeout: float | None, start: float | None = None, logf=None):
        self.what, self.timeout = what, timeout
        self.start = time.time() if start is None else start
        self.next = self.start + WAIT_REPORT_AFTER
        self.log = logf or log

    def tick(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now < self.next:
            return False
        left = f", {self.timeout - (now - self.start):.0f} s left" if self.timeout else ""
        self.log(f"still waiting for {self.what} ({now - self.start:.0f} s so far{left})")
        while self.next <= now:
            self.next += WAIT_REPORT_EVERY
        return True


class HealthTracker:
    """Counts consecutive failed liveness checks; `hung` once the limit is reached."""

    def __init__(self, limit: int = HEALTH_FAILURES):
        self.limit = limit
        self.failures = 0

    def record(self, ok: bool) -> None:
        self.failures = 0 if ok else self.failures + 1

    @property
    def hung(self) -> bool:
        return self.failures >= self.limit


# ======================================================================== game I/O

class RpcError(RuntimeError):
    """The mod refused an op or answered garbage. A RuntimeError: at a session boundary it is a setup failure
    (relaunch and retry), never the runner's death (RUN-3: a refused ui.menu load killed the run)."""


# What a session's setup or recovery may raise when the game or the mod misbehaves: retried, never fatal.
SETUP_ERRORS = (RuntimeError, OSError, TimeoutError, LuaDebugError)


class LaunchBlocked(RuntimeError):
    """The game can't be launched now for a reason an operator fixes outside the runner: never a give-up, the run
    loop retries every STEAM_RETRY_S."""


class ForeignGameRunning(LaunchBlocked):
    """A bg3 this harness didn't launch (a user's own BG3) is running: never signalled, and no second game beside it
    (RUN-4)."""


class SteamNotOffline(LaunchBlocked):
    """Steam isn't in offline mode: launching BG3 could kick the account's other machine off. Never launch."""


class NoDisplay(LaunchBlocked):
    """No usable display env (display_env.problem): the game would fall back to the iGPU and crash (L8). Never
    launch; the desktop session coming back fixes it."""


class GameDied(RuntimeError):
    """The game died while the runner waited on it (crash, crash reporter, bg3 gone, dead debug port, launcher exit)."""


class Bg3leStale(Exception):
    """The bg3le build lacks a patches/bg3le patch (UI-1): never launch it. Not a RuntimeError: a stale build never
    fixes itself, so no retry loop may swallow it; the runner stops until tools/build_bg3le.sh is run."""


class LaunchGaveUp(Exception):
    """LAUNCH_ATTEMPTS launches in a row died before the save played: the runner stops (not a RuntimeError, so no
    retry loop swallows it)."""


def steam_ready() -> tuple[bool, str]:
    """(ok to launch BG3?, why): a Steam client is running and, on a machine that requires it
    (steam_paths.offline_required: $BG3AI_REQUIRE_STEAM_OFFLINE=1 or ~/.config/bg3ai/require-steam-offline; machine
    2), Steam's last login was offline. Not required by default (RUN-11: a public user's runner launched never)."""
    required, req_why = steam_paths.offline_required()
    if not required:
        ok, why = True, f"Steam offline {req_why}"
    else:
        ok, why = steam_paths.steam_offline()
        why = f"{why} [offline required: {req_why}]"
    if not ok:
        return False, why
    r = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, text=True)
    if not r.stdout.strip():
        # libsteam_api would start Steam itself, maybe online
        return False, f"Steam isn't running ({why})"
    return True, why


def bg3le_problems() -> list[str]:
    """Why the bg3le build launch_game.sh would preload is stale ([] = current, or $BG3AI_ALLOW_STALE_BG3LE=1)."""
    if os.environ.get("BG3AI_ALLOW_STALE_BG3LE") == "1":
        return []
    lib = Path(os.environ.get("BG3AI_BG3LE_LIB", bg3le_buildinfo.DIST / "libbg3le.so"))
    return bg3le_buildinfo.problems(lib.parent)


def port_open(host: str = DEBUG_HOST, port: int = DEBUG_PORT, timeout: float = 1.0) -> bool:
    """The debug port answers. A handshake, not a bare connect-and-close: that leaked a bg3le client slot
    per probe ("client attached (371 connected)"); see luadebug.port_answers."""
    from bg3ai.luadebug import port_answers
    return port_answers(host, port, timeout)


def lua_eval(code: str, context: str = "client", timeout: float = EVAL_TIMEOUT) -> str:
    from bg3ai.luadebug import LuaDebugClient
    with LuaDebugClient(connect_timeout=5.0) as lua:
        return lua.evaluate(code, context=context, timeout=timeout)


def rpc(op: str, args: dict | None = None, context: str = "server", timeout: float = EVAL_TIMEOUT):
    payload = json.dumps({"op": op, "args": args or {}})
    if "]==]" in payload:
        raise ValueError("payload contains the Lua long-string terminator")
    raw = lua_eval(f"return Mods.BG3AI.BG3AI.rpc([==[{payload}]==])", context, timeout)
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RpcError(f"{op}: non-JSON reply {raw[:200]!r}") from e
    if not reply.get("ok"):
        raise RpcError(f"{op}: {reply.get('error')}")
    return reply.get("result")


def mod_loaded(timeout: float = EVAL_TIMEOUT) -> bool | None:
    """RUN-8: the BG3AI mod's RPC is loaded in the client context (MOD_READY_LUA); None when the eval itself fails
    (that is game_state's business, not a missing mod)."""
    try:
        return lua_eval(MOD_READY_LUA, "client", timeout).strip() == "true"
    except Exception:  # noqa: BLE001
        return None


def game_state(timeout: float = EVAL_TIMEOUT) -> str | None:
    """Ext.Utils.GetGameState() from the client context, or None if the game doesn't answer."""
    try:
        return lua_eval("return tostring(Ext.Utils.GetGameState())", "client", timeout).strip()
    except Exception:  # noqa: BLE001 - any failure means "no answer"
        return None


def available_ops(context: str) -> set[str]:
    try:
        return set((rpc("sys.info", context=context) or {}).get("ops") or [])
    except Exception:  # noqa: BLE001
        return set()


def host_character_name() -> str | None:
    """Display name of the party's avatar (the player's own created character), or None.

    Not the selected character: selecting a companion must neither close the save/load gates nor make
    --take-over think the running game belongs to someone else."""
    try:
        return lua_eval(AVATAR_NAME_LUA, context="server").strip() or None
    except Exception:  # noqa: BLE001
        return None


def server_ready() -> bool:
    try:
        rpc("sys.ping", context="server")
        return True
    except Exception:  # noqa: BLE001
        return False


def ui_state() -> dict | None:
    """The mod's ui.state (client), or None without the op or an answer."""
    if "ui.state" not in available_ops("client"):
        return None
    try:
        return rpc("ui.state", context="client") or {}
    except Exception:  # noqa: BLE001
        return None


def ui_screen() -> str | None:
    return (ui_state() or {}).get("screen")


def at_splash(st: dict | None) -> bool:
    """The "Press any key" splash before the main menu (ui.state shows it as a panel, not a screen)."""
    st = st or {}
    return st.get("page") == "SplashScreen" or "SplashScreen" in (st.get("panels_open") or [])


def press_menu_key() -> str:
    """One key past the splash: Enter through the input shim (a real SDL key, like a player), else the engine's
    InjectKeyPress. Returns which one worked."""
    try:
        from bg3ai.input import InputClient
        with InputClient(timeout=3.0) as inp:
            inp.key("ENTER")
        return "shim Enter"
    except Exception as e:  # noqa: BLE001
        log(f"shim key failed ({e}); using InjectKeyPress")
    lua_eval('Ext.Input.InjectKeyPress("SPACE") return 1')
    return "InjectKeyPress SPACE"


def read_progress() -> dict | None:
    ops = available_ops("server")
    status = journal = None
    try:
        if "state.status" in ops:
            status = rpc("state.status")
        if "state.journal" in ops:
            journal = rpc("state.journal")
    except Exception as e:  # noqa: BLE001
        log(f"progress read failed: {e}")
    return progress_signature(status, journal)


def list_saves() -> list[Save]:
    out = []
    try:
        entries = list(os.scandir(SAVE_DIR))
    except OSError:
        return out
    for e in entries:
        if not e.is_dir():
            continue
        lsv = runs.save_lsv(SAVE_DIR, e.name)   # <folder>/<label>.lsv
        try:
            mtime = lsv.stat().st_mtime if lsv is not None else e.stat().st_mtime
        except OSError:
            continue
        s = parse_save_folder(e.name, mtime)
        if s:
            out.append(s)
    return out


def load_menu_args(save: Save, saves: list[Save]) -> dict:
    """ui.menu load arguments: the Load page groups saves by owner, newest first, and the mod checks the
    selected save's date before loading (it can't read save titles there)."""
    same = sorted((s for s in saves if s.owner == save.owner), key=lambda s: s.mtime, reverse=True)
    args: dict = {"action": "load", "name": save.folder, "owner": save.owner}
    idx = next((i for i, s in enumerate(same, 1) if s.folder == save.folder), None)
    if idx is not None:
        t = time.localtime(save.mtime)
        args.update(index=idx, date={"y": t.tm_year, "m": t.tm_mon, "d": t.tm_mday, "H": t.tm_hour, "M": t.tm_min})
    return args


def all_bg3_pids() -> list[int]:
    """Every native game process on the machine (./bin/bg3), ours or a user's own; not the pressure-vessel wrapper."""
    r = subprocess.run(["pgrep", "-x", "bg3"], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


# A harness process (launch_game.sh's game, or the crash reporter it spawns) carries one of these (RUN-4): the marker
# env (BG3AI_GAME_STAMP, from 2026-10-16) or the screenshot layer's bg3ai control socket (every launch before it),
# its own cgroup scope (BG3AI_GAME_SCOPE, the default) or bg3le mapped in.
HARNESS_ENV = (b"BG3AI_GAME_STAMP=", b"control=bg3ai-shot")


def is_harness_pid(pid: int) -> bool:
    """The process was started by runner/launch_game.sh (this harness), not by a user from Steam."""
    proc = Path(f"/proc/{pid}")
    try:
        if "/bg3ai-game-" in (proc / "cgroup").read_text(errors="replace"):
            return True
    except OSError:
        pass
    try:
        env = (proc / "environ").read_bytes()
        if any(m in env for m in HARNESS_ENV):
            return True
    except OSError:
        pass
    try:
        with open(proc / "maps", "rb") as f:
            return any(b"libbg3le" in ln for ln in f)
    except OSError:
        return False


def pids_of_game() -> list[int]:
    """This harness's native game processes (all_bg3_pids that is_harness_pid): the ones the runner may wait on,
    count as its game, or kill. A user's own BG3 is never one of them (RUN-4)."""
    return [p for p in all_bg3_pids() if is_harness_pid(p)]


def foreign_bg3_pids() -> list[int]:
    """bg3 processes this harness didn't launch (a user's own BG3): the runner never signals them and never
    launches a second game beside one."""
    return [p for p in all_bg3_pids() if not is_harness_pid(p)]


def proc_start(pid: int) -> int | None:
    """The process's start time (clock ticks since boot, /proc/<pid>/stat field 22): with the pid, its identity
    (a pid reused after a reboot or a wrap has another start time)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_cmdline(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def crash_reporter_pids() -> list[int]:
    """BG3's LinuxCrashReporter processes. One starts after the game crashes and keeps the launcher alive (S014:
    the runner then waited 10 min for a dead game). Only reporters with the game's SteamAppId (or its install path)
    that this harness's game spawned (is_harness_pid): a user's own BG3 crash report is left alone (RUN-4)."""
    r = subprocess.run(["pgrep", "-f", "LinuxCrashReporter"], capture_output=True, text=True)
    out = []
    for p in r.stdout.split():
        try:
            env = Path(f"/proc/{p}/environ").read_bytes().split(b"\0")
            cmd = Path(f"/proc/{p}/cmdline").read_bytes()
        except OSError:
            continue
        if (BG3_APP_ENV in env or b"Baldurs Gate 3" in cmd) and is_harness_pid(int(p)):   # RUN-4: ours only
            out.append(int(p))
    return out


class LaunchWatch:
    """Fast crash detection for one launch, polled by Runner.wait_for(). Dead when: a bg3le log of this launch's stamp
    (runtime/logs/bg3le-<stamp>.log.<pid>) has a `crash:` line or was written by LinuxCrashReporter; a game crash
    reporter runs; bg3 was up and is gone; or bg3 is up but port 9998 has refused for PORT_DEAD_S s."""

    def __init__(self, stamp: str, logs: Path, started: float | None = None, port_dead_s: float = PORT_DEAD_S):
        self.stamp, self.logs, self.port_dead_s = stamp, logs, port_dead_s
        self.started = time.time() if started is None else started
        self.offsets: dict[str, int] = {}
        self.bg3_since: float | None = None
        self.port_ok_at: float | None = None

    def log_crash(self) -> str | None:
        """The first crash sign in this launch's bg3le logs; reads only what's new since the last call."""
        for f in sorted(self.logs.glob(f"bg3le-{self.stamp}.log*")):
            off = self.offsets.get(f.name, 0)
            try:
                with open(f, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read()
            except OSError:
                continue
            end = chunk.rfind(b"\n") + 1   # keep a partial last line for the next call
            self.offsets[f.name] = off + end
            for line in chunk[:end].splitlines():
                if b"] crash:" in line:
                    return line.split(b"] ", 1)[-1].decode(errors="replace").strip()[:200]
                if b"host is LinuxCrashReporter" in line:
                    return f"LinuxCrashReporter started ({f.name})"
        return None

    def check(self, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        why = self.log_crash()
        if why:
            return f"bg3le logged {why}"
        rep = crash_reporter_pids()
        if rep:
            return f"LinuxCrashReporter is running (pid {', '.join(map(str, rep))})"
        if pids_of_game():
            if self.bg3_since is None:
                self.bg3_since = now
        elif self.bg3_since is not None:
            return "the bg3 process is gone"
        if port_open():
            self.port_ok_at = now
        elif self.bg3_since is not None:
            since = max(self.bg3_since, self.port_ok_at or 0.0)
            if now - since > self.port_dead_s:
                return f"bg3 is up but debug port {DEBUG_PORT} has refused for {now - since:.0f} s"
        return None


# ======================================================================== check_all between sessions

def memory_lint_errors(report: str, skip: tuple[str, ...] = ()) -> list[str]:
    """The ERROR lines of a tools/memory/lint.py report ("ERROR  <check>  <message>"), minus the checks in `skip`."""
    out = []
    for line in report.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == "ERROR" and parts[1] not in skip:
            out.append(line.strip())
    return out


def until_stop_handoff_only(reason: str | None, until: bool, report: str) -> bool:
    """The session ended on the --until milestone and its end lint failed only on the handoff check (no wrap-up
    ran, by design): no fix-notes session. Any other ERROR (a real notes problem), or a report without ERROR lines
    (session.py itself failed), still gets one."""
    return (reason == "milestone" and until and bool(memory_lint_errors(report))
            and not memory_lint_errors(report, skip=("handoff",)))


def check_all_cmd(json_path: Path) -> list[str]:
    """check_all imports the MCP server (bridge deps): through uv's bridge env when uv is there."""
    uv = shutil.which("uv")
    py = [uv, "run", "--quiet", "--project", str(REPO / "bridge"), "python"] if uv else [sys.executable]
    return py + [str(CHECK_ALL), "--json", str(json_path)]


def check_plans_cmd(json_path: Path, mem: Path) -> list[str]:
    uv = shutil.which("uv")
    py = [uv, "run", "--quiet", "--project", str(REPO / "bridge"), "python"] if uv else [sys.executable]
    return py + [str(CHECK_PLANS), "--json", str(json_path), "--memory", str(mem)]


def plan_check_result(rows: list[dict]) -> tuple[str, list[dict]]:
    """check_plans rows → (log text, bad lines in the keeper's refused-line format {name, level, problems})."""
    bad = [r for r in rows if r.get("problems")]
    out = [{"name": r["name"], "level": r["level"],
            "problems": (f"check_plans between sessions (the game's offers): {'; '.join(r['problems'])} | stored line: "
                         f"{json.dumps(r.get('plan')) if r.get('plan') is not None else '(none)'} | offers: {r.get('offers')}")[:1500]}
           for r in bad]
    if not bad:
        return f"plan lines: {len(rows)} checked, all valid against the game's offers", out
    return (f"plan lines: {len(bad)} of {len(rows)} INVALID: "
            + "; ".join(f"{r['name']} L{r['level']}: {', '.join(r['problems'])}" for r in bad))[:600], out


def plan_note(bad: list[dict]) -> str:
    return ("RUNNER NOTE: these level-up plan lines in memory/party-build.md don't match what the game will offer; "
            "fix each line (picks only from the offers listed) before that level-up:\n"
            + "\n".join(f"- {b['name']} L{b['level']}: {b['problems']}" for b in bad))


def check_all_due(last: float | None, now: float, interval: float = CHECK_ALL_INTERVAL) -> bool:
    return last is None or now - last >= interval


def check_all_result(rc: int | None, out: str, report: dict | None) -> dict:
    """{ok, counts, line, rows, why} from check_all's exit code, stdout and --json report. ok=False: no verdict
    (refused, game unreachable, timed out or crashed), which doesn't count as a run for the 30-min spacing."""
    if report and isinstance(report.get("counts"), dict):
        c = {k: int(report["counts"].get(k, 0)) for k in ("PASS", "WARN", "FAIL", "SKIP")}
        line = f"check_all: {c['PASS']} pass, {c['WARN']} warn, {c['FAIL']} fail" + (
            f", {c['SKIP']} skip" if c["SKIP"] else "")
        return {"ok": True, "counts": c, "line": line, "rows": report.get("rows") or [], "why": None}
    why = {None: f"timed out after {CHECK_ALL_TIMEOUT} s", 2: "game/mod not reachable",
           3: "refused: a session is playing"}.get(rc)
    if why is None:
        tail = (out.strip().splitlines() or ["no output"])[-1][:160]
        why = f"exit {rc}: {tail}"
    return {"ok": False, "counts": None, "line": f"check_all: no verdict ({why})", "rows": [], "why": why}


def _cell(text, limit: int = 300) -> str:
    return str(text)[:limit].replace("|", "\\|").replace("\n", " ")


def check_all_markdown(res: dict, when: float, secs: float, label: str = "") -> str:
    """The section appended to the finished session's Sxxx.triage.md."""
    verdict = res["line"].removeprefix("check_all: ")
    head = f"\n## check_all {label}({datetime.fromtimestamp(when):%H:%M}, {secs:.0f} s): {verdict}\n"
    if not res["ok"]:
        return head
    out = [head, "| check | verdict | secs | detail |", "|---|---|---|---|"]
    for r in res["rows"]:
        mark = {"FAIL": "**FAIL**"}.get(r.get("verdict"), r.get("verdict"))
        out.append(f"| {r.get('name')} | {mark} | {r.get('secs', '')} | {_cell(r.get('detail', ''))} |")
        out += [f"| | | | {_cell(line, 150)} |" for line in (r.get("lines") or [])[1:4]]
    return "\n".join(out) + "\n"


# ======================================================================== persistent runner state

class RunnerState:
    """runtime/sessions/<playthrough>/runner.json: save owner, runner quicksaves, session history."""

    def __init__(self, playthrough: str):
        self.dir = RUNTIME / "sessions" / playthrough
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "runner.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("history", [])
        self.data.setdefault("runner_saves", [])

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def __getitem__(self, k):
        return self.data.get(k)

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()


def write_status(**fields) -> None:
    path = RUNTIME / "runner.status.json"
    try:
        cur = json.loads(path.read_text())
    except (OSError, ValueError):
        cur = {}
    cur.update(fields, updated=time.time())
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    tmp.replace(path)


# ======================================================================== the runner

@dataclass
class Options:
    playthrough: str
    hours: float | None
    session_minutes: float
    model: str
    take_over: bool = False
    dry_run: bool = False
    save_owner: str | None = None
    effort: str | None = None          # claude --effort; None = Claude Code's default
    fix_model: str | None = None       # the fix-notes pass (no game, small edits); None = same as play
    fix_effort: str | None = "low"
    fallback_model: str | None = None
    allow_short: bool = False          # --allow-short-sessions: session-config minutes below MIN_SESSION_MINUTES too
    keeper: bool = True                # memory-keeper pass between sessions (runner/memory_keeper.py)
    keeper_model: str = "same"         # "same" = the session's play model (a benchmark compares one model end to end)
    keeper_effort: str | None = "low"
    keeper_minutes: float = 5.0
    autocompact: int | None = None     # claude --autocompact <tokens> (play sessions); None = Claude Code's default
    until: str | None = None           # run --until <milestone id> (bg3ai.milestones): stop there, write runs.jsonl
    character_brief: str | None = None  # run --character-brief: written to profile.json (CHAR-1 phase 0)
    roleplay: str | None = None        # run --roleplay strict|light (profile.json character.roleplay)
    difficulty: str | None = None      # run --difficulty (profile.json difficulty, phase 0's new_game)
    run_hours: float | None = None     # run --run-hours: cap of one --until run (then it ends as a timeout)
    series: str | None = None          # run --runs: the series (the base playthrough); its runs.jsonl gets the records
    run_index: int | None = None       # run --runs: this run's number in the series (1 = the base playthrough)
    series_end: float | None = None    # run --runs --hours: the whole series' end (epoch s)
    max_cost: float | None = None      # run --max-cost USD: cost cap of one --until run (then it ends over_budget)
    max_tokens: int | None = None      # run --max-tokens N: token cap of one --until run (runs.token_total)
    pins: dict | None = None           # version pins this run plays with (runs.version_pins), recorded in runs.jsonl
    version_drift: list | None = None  # --allow-version-drift: the pins that differ from the series' first run
    cap_changes: list | None = None    # --allow-cap-change: the caps that differ from series.json
    scenario: str | None = None        # run --scenario NAME: scenarios/<name>.json (frozen in series.json)
    start_save: str | None = None      # its start save folder: loaded for the first session, kept out of saves_after
    judge: bool = False                # run --judge (default with --runs): score the run's persona after its record
    judge_model: str | None = None     # the judge's model (series.json "judge_model" for a series)
    judge_change: str | None = None    # --allow-judge-change: the judge model differs from series.json's
    client: str = "claude"             # run --client: the CLI that plays (runner/clients.py; frozen in series.json)
    client_change: str | None = None   # --allow-client-change: the client differs from series.json's
    sandbox_change: str | None = None  # --allow-sandbox-change: the sandbox mode differs from series.json's
    sandbox: str = "none"              # run --sandbox: none | container (runner/sandbox.py)
    box: sandbox.Sandbox | None = None  # --sandbox container: prepared once by cmd_run (firewall checked, proxy up)
    restart_run: bool = False          # run --runs --restart-run: start the next run over even after finished sessions
    keep_game: bool = False            # run --until/--runs --keep-game: leave the harness game up when the run/series ends
    max_cost_per_day: float | None = None  # run --max-cost-per-day USD: pause while the trailing 24 h spent this much


# claude --autocompact accepts 100k-1M tokens. Context-cost model (tools/logs/context_model.py, S021-S026): a
# compaction is a ~1-2 min summary stall (a >10 s gap) and a generic summary, so shorter sessions with our own
# handoff are the default lever; this is an opt-in safety net / A-B switch (e.g. 350000 with 30-min sessions).
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000


def make_client(opt: Options) -> clients.Client:
    """The play client: the bg3 stdio server of play/.mcp.json, or with --sandbox container the http one on the
    sandbox network's gateway (the client runs in the container)."""
    if opt.sandbox == "container":
        return clients.make_http(opt.client, sandbox.URL)
    return clients.make(opt.client)


def sandbox_server_env(base_env: dict, req: clients.Request) -> dict:
    """The env of the http bg3 server of one sandboxed play process: what the stdio server would get (the runner's
    env, play/.mcp.json's, the session's) with the rules served by the server (the container has no play/)."""
    return {**base_env, **(clients.bg3_server().get("env") or {}), **req.env, "BG3AI_SERVE_RULES": "1"}


def session_overrides(path: Path) -> dict:
    """{"model", "effort", "minutes"} from runtime/sessions/<pt>/session-config.json, re-read before every session
    so the operator can retune a long run without restarting it. Unknown keys and bad values are ignored."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    if isinstance(raw, dict):
        if isinstance(raw.get("model"), str) and raw["model"]:
            out["model"] = raw["model"]
        if raw.get("effort") in EFFORTS:
            out["effort"] = raw["effort"]
        m = raw.get("minutes")
        if isinstance(m, (int, float)) and not isinstance(m, bool) and 0 < m <= 24 * 60:
            out["minutes"] = float(m)
        ac = raw.get("autocompact")
        if isinstance(ac, int) and not isinstance(ac, bool) and AUTOCOMPACT_MIN <= ac <= AUTOCOMPACT_MAX:
            out["autocompact"] = ac
    return out


@dataclass
class SessionResult:
    sid: str
    claude_session: str
    exit_code: int | None
    reason: str          # exited | hard_stop | crash | hang | agent_hang | aborted | killed | milestone | dry_run
    #                      | cap | api_limit (RUN-7: usage/rate limit, overload) | mcp_failed (RUN-8)
    started: float
    ended: float
    notes: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    detail: str = ""     # what the runner saw, for a fault
    fault_at: float | None = None
    resets_at: float | None = None   # api_limit: when the client said the limit resets


class Runner:
    watch: LaunchWatch | None = None   # crash signs of the game the runner launched (see launch_game)
    api_limits = 0                     # RUN-7: API limits in a row (the back-off doubles per repeat)
    cap_hit: dict | None = None        # --max-cost/--max-tokens: {cap, cost, tokens, why} once the run reached one
    run_started: float | None = None   # --until: the run's start (claude_totals counts from there)
    _client: clients.Client | None = None

    def __init__(self, opt: Options):
        self.opt = opt
        self._client = make_client(opt)   # the CLI every play, resume and fix-notes process runs
        self.mem = PLAYTHROUGHS / opt.playthrough / "memory"
        self.state = RunnerState(opt.playthrough)
        # --sandbox container: this playthrough's HOME for the client in the container (its sessions to resume)
        self.box = replace(opt.box, home=self.state.dir / "sandbox-home") if opt.box else None
        self._sandbox_live: tuple | None = None   # (server, container) of the play process now running
        self.profile_path = PLAYTHROUGHS / opt.playthrough / "profile.json"
        self.profile = load_profile(self.profile_path)
        if opt.character_brief or opt.roleplay or opt.difficulty:
            self.set_profile(update_profile(self.profile, opt.character_brief, opt.roleplay, opt.difficulty))
        owner = opt.save_owner or os.environ.get("BG3AI_SAVE_OWNER")
        if owner and self.state["save_owner"] != owner:
            if self.state["save_owner"]:
                log(f"save owner for {opt.playthrough}: {self.state['save_owner']} -> {owner}")
            if not opt.dry_run:
                self.state["save_owner"] = owner
            else:
                self.state.data["save_owner"] = owner
        self.game_proc: subprocess.Popen | None = None
        self.stop_requested = False
        self.abort = False
        self.pending_notes: list[str] = []
        self.quick_failures = 0
        self.new_game = False
        self.gates: bool | None = None       # gates_decision for the game this session plays (BG3AI_SAVE_GATES)
        self.until_hit: dict | None = None   # the --until milestone, once the progress poller saw it reached
        self.cost_pause_since: float | None = None   # --max-cost-per-day: paused since (epoch s)
        self.last_triage: tuple[str, Path] | None = None  # (session, Sxxx.triage.md) of the last claude run
        self.code_watch = CodeWatch()         # this process's repo code, for the self-restart between sessions
        self.bridge_hashes: dict | None = None  # bridge/src as the last session's MCP server ran it
        self.handover = read_handover()       # set when this process is the execv'd successor of the runner
        self.keeper_thread: threading.Thread | None = None  # memory keeper overlapping the between-session prep
        if self.handover:
            self.pending_notes = list(self.handover.get("pending_notes") or [])
            self.quick_failures = int(self.handover.get("quick_failures") or 0)
            self.new_game = bool(self.handover.get("new_game"))
            self.bridge_hashes = self.handover.get("bridge_hashes")
            lt = self.handover.get("last_triage")
            self.last_triage = (lt[0], Path(lt[1])) if lt else None
            if self.handover.get("series_end"):
                self.opt.series_end = self.handover["series_end"]
        self.outcome: str | None = None      # how run() ended: reached|timeout|gave_up|runner_error|stopped (--until)
        self.fatal = False                   # run() ended on something a next --runs run can't fix (needs a human)
        g = self.state["game"] or {}
        # The game a previous run of the runner launched: keep watching its bg3le logs.
        self.watch = LaunchWatch(g["stamp"], RUNTIME / "logs", g.get("launched")) if g.get("stamp") else None

    def set_profile(self, profile: dict) -> None:
        """Write profile.json (not in a dry run) and use it."""
        if profile == self.profile:
            return
        if self.state["history"]:
            log(f"profile.json changed for {self.opt.playthrough}, which already played: the character brief only "
                "matters for a new game (phase 0)")
        self.profile = profile
        if self.opt.dry_run:
            return
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.profile_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(profile, indent=1, ensure_ascii=False) + "\n")
            tmp.replace(self.profile_path)
            log(f"profile written: {self.profile_path}")
        except OSError as e:
            log(f"profile write failed: {e}")

    def adopt_owner(self, owner: str) -> None:
        """I3: the character's name (from this playthrough's first Sxxx- save) becomes its save owner: runner.json,
        and profile.json's character.name; the MCP server's prefix follows (save_identity)."""
        if self.opt.dry_run:
            self.state.data["save_owner"] = owner
            return
        self.state["save_owner"] = owner
        log(f"save owner for {self.opt.playthrough}: {owner} (learned from its saves)")
        c = self.profile.get("character")
        if c and not c.get("name"):
            self.set_profile(dict(self.profile, character=dict(c, name=owner)))

    # ---------------------------------------------------------------- unattended-run events

    def event(self, kind: str, **fields) -> None:
        """runtime/sessions/<pt>/runner_events.jsonl: runner start/stop, faults, recoveries, refused launches
        (the dashboard's unattended hours and crash/recovery counts). Never breaks the runner."""
        try:
            with open(self.state.dir / "runner_events.jsonl", "a") as f:
                f.write(json.dumps({"t": round(time.time(), 1), "kind": kind, **fields}) + "\n")
        except OSError as e:
            log(f"event log failed: {e}")

    # ---------------------------------------------------------------- setup

    def check_memory(self) -> None:
        from new_playthrough import SetupError, link_play_memory
        if not self.mem.is_dir():
            raise SystemExit(f"no memory at {self.mem}; create it with: "
                             f"python3 tools/memory/new_playthrough.py {self.opt.playthrough}")
        link = PLAY_DIR / "memory"
        if not (link.is_symlink() and link.resolve() == self.mem.resolve()) and not self.opt.dry_run:
            try:
                prev = link_play_memory(PLAY_DIR, self.mem)
            except SetupError as e:
                raise SystemExit(str(e)) from e
            log(f"play/memory → {self.mem} (was {prev})")

    def memory_state(self) -> dict:
        import memlib as M
        return M.load_state(self.mem)

    def next_sid(self) -> str:
        import memlib as M
        reuse = M.reusable_session(self.mem)  # an empty killed session's id is reused by session.py start
        if reuse:
            return session_id(reuse)
        return session_id(predict_session_number(M.session_numbers(self.mem), self.memory_state().get("current")))

    # ---------------------------------------------------------------- game lifecycle

    def owned_group(self) -> int | None:
        """The launcher's process group from runner.json's game record, only while it is provably that launch
        (RUN-4): its leader is runtime launch_game.sh with the start time recorded at launch (records from before
        2026-10-16 have none: the command line alone). None after a reboot or a pid reuse: never signalled then."""
        g = self.state["game"] or {}
        pg = g.get("pgid")
        if not pg:
            return None
        try:   # after a self-restart (execv) the game is still our child: reap it if it exited, or it stays a zombie
            os.waitpid(pg, os.WNOHANG)
        except OSError:
            pass
        start = proc_start(pg)
        if start is None:   # the leader (launch_game.sh) is gone: the group is ours only if it holds a harness game
            return pg if any(_pgid(p) == pg for p in pids_of_game()) else None
        if b"launch_game.sh" not in proc_cmdline(pg):
            return None
        if g.get("pg_start") is not None and g["pg_start"] != start:
            return None
        return pg

    def owns_game(self) -> bool:
        """True if the running game is one this runner (or a previous run of it) launched."""
        if self.game_proc is not None and self.game_proc.poll() is None:
            return True
        pg = self.owned_group()
        if pg is None:
            return False
        try:
            os.killpg(pg, 0)
            return True
        except OSError:
            return False

    def game_alive(self) -> bool:
        if self.game_proc is not None and self.game_proc.poll() is not None:
            return False
        return port_open()

    def launch_game(self, load_folder: str | None) -> None:
        ok, why = steam_ready()
        write_status(steam_offline=ok, steam_why=why)
        if not ok:
            log(f"!!!!! REFUSING TO LAUNCH BG3: Steam not ready: {why}. Start Steam (in offline mode where offline "
                "is required: Steam > Go Offline...) and leave it running; the runner keeps retrying. !!!!!")
            self.event("steam_refused", why=why)
            raise SteamNotOffline(why)
        self.refuse_stale_bg3le(bg3le_problems())
        foreign = foreign_bg3_pids()
        if foreign:   # RUN-4: a user's own BG3 (Steam would hand a second launch to it, or two games fight)
            why = f"a BG3 this harness didn't launch is running (pid {', '.join(map(str, foreign))})"
            write_status(alert={"t": time.time(), "kind": "foreign_game", "msg": f"not launching: {why}"[:600]})
            log(f"!!!!! REFUSING TO LAUNCH BG3: {why}; it is never killed by the runner. Close it to let the runner "
                "launch its own game; the runner keeps retrying. !!!!!")
            self.event("foreign_game", pids=foreign)
            raise ForeignGameRunning(why)
        if port_open():
            raise RuntimeError(f"port {DEBUG_PORT} is still in use; can't launch another game")
        filled = display_env.fill(os.environ)   # Xwayland restarted since the runner started: its new -auth file
        if filled:
            log("display env refreshed from the desktop session: " + " ".join(f"{k}={v}" for k, v in filled.items()))
        no_display = display_env.problem(os.environ)
        if no_display:
            log(f"!!!!! REFUSING TO LAUNCH BG3: no usable display: {no_display}; the runner keeps retrying. !!!!!")
            self.event("display_refused", why=no_display)
            raise NoDisplay(no_display)
        args = [str(LAUNCH_SH)] + (["--load", load_folder] if load_folder else [])
        stamp = base = f"{datetime.now():%Y%m%d-%H%M%S}"   # launch_game.sh names its logs with it (BG3AI_LAUNCH_STAMP)
        n = 1
        while list((RUNTIME / "logs").glob(f"*-{stamp}.log*")):   # never another launch's logs (LaunchWatch)
            stamp, n = f"{base}-{n}", n + 1
        logf = open(RUNTIME / f"launch-{stamp}.out", "w")
        log(f"launching game: {' '.join(args)}")
        self.game_proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                          start_new_session=True, env={**os.environ, "BG3AI_LAUNCH_STAMP": stamp})
        logf.close()  # the game has its own copy of the descriptor
        now = time.time()
        self.watch = LaunchWatch(stamp, RUNTIME / "logs", now)
        self.state["game"] = {"pgid": self.game_proc.pid, "pg_start": proc_start(self.game_proc.pid), "launched": now,
                              "load": load_folder, "stamp": stamp}
        self.state["mod_hashes"] = mod_tree_hashes()  # a fresh game loads the mod as it is now
        write_status(game_pgid=self.game_proc.pid)

    def refuse_stale_bg3le(self, bad: list[str]) -> None:
        """A stale bg3le build (bg3le_problems, or launch_game.sh's exit 4): status, event, loud log, Bg3leStale."""
        write_status(bg3le_stale=bad or None)
        if not bad:
            return
        msg = "; ".join(bad)
        log(f"!!!!! REFUSING TO LAUNCH BG3: stale bg3le build ({msg}). Run {bg3le_buildinfo.REBUILD} (with the game "
            "closed), then restart the runner; BG3AI_ALLOW_STALE_BG3LE=1 launches it anyway. !!!!!")
        self.event("bg3le_stale", problems=bad)
        write_status(alert={"t": time.time(), "kind": "bg3le_stale",
                            "msg": f"runner stopped: stale bg3le build ({msg})"[:600]})
        raise Bg3leStale(msg)

    def kill_game(self) -> None:
        """Stop the game: SIGTERM, then SIGKILL after KILL_GRACE s (bg3 ignores SIGTERM), on both the
        launcher's process group (only while owned_group proves it is that launch) and this harness's bg3 processes
        (pids_of_game: never a user's own BG3, RUN-4); then require port 9998 to be free."""
        log("killing the game")
        groups = []
        if self.game_proc is not None and self.game_proc.poll() is None:
            groups.append(self.game_proc.pid)
        else:
            pg = self.owned_group()
            if pg:
                groups.append(pg)
            elif (self.state["game"] or {}).get("pgid"):
                log(f"not signalling process group {self.state['game']['pgid']}: not provably our launch any more "
                    "(a reboot or a reused pid)")
        foreign = foreign_bg3_pids()
        if foreign:
            log(f"leaving bg3 pid(s) {', '.join(map(str, foreign))} alone: not launched by this harness")

        def send(sig: int) -> None:
            for pgid in groups:
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            for pid in pids_of_game() + crash_reporter_pids():
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

        def gone() -> bool:
            return not pids_of_game() and not crash_reporter_pids() and not port_open()

        send(signal.SIGTERM)
        end = time.time() + KILL_GRACE
        while time.time() < end and not gone():
            time.sleep(1)
        if not gone():
            log(f"game still running {KILL_GRACE} s after SIGTERM; SIGKILL")
            send(signal.SIGKILL)
            end = time.time() + KILL_CONFIRM
            while time.time() < end and not gone():
                time.sleep(1)
        if self.game_proc is not None:
            try:
                self.game_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.game_proc = None
        self.watch = None
        self.state["game"] = None
        self.state["game_killed_at"] = time.time()
        if pids_of_game():
            raise RuntimeError(f"bg3 processes survive SIGKILL: {pids_of_game()}")
        if port_open():
            raise RuntimeError(f"port {DEBUG_PORT} is still in use after the game was killed")

    def wait_for(self, what: str, pred, timeout: float, interval: float = 2.0) -> bool:
        start = time.time()
        end = start + timeout
        report = WaitReporter(what, timeout, start)
        while time.time() < end:
            report.tick()
            if self.game_proc is not None and self.game_proc.poll() is not None:
                launched = (self.state["game"] or {}).get("launched") or 0
                if (self.game_proc.returncode == bg3le_buildinfo.STALE_EXIT
                        and time.time() - launched < STALE_EXIT_WITHIN_S):
                    # launch_game.sh's own stale-build refusal (the backstop of the check in launch_game)
                    self.refuse_stale_bg3le([f"runner/launch_game.sh exited {bg3le_buildinfo.STALE_EXIT} "
                                             f"(stale bg3le build, see {RUNTIME}/launch-*.out)"])
                raise GameDied(f"game exited (status {self.game_proc.returncode}) while waiting for {what}")
            if self.abort:
                raise RuntimeError(f"aborted while waiting for {what}")
            why = self.watch.check() if self.watch is not None else None
            if why:
                log(f"game died while waiting for {what}: {why}")
                raise GameDied(f"{why} while waiting for {what}")
            if pred():
                return True
            time.sleep(interval)
        log(f"timed out after {timeout:.0f} s waiting for {what}")
        return False

    def wait_menu(self) -> bool:
        """Up to the main menu, pressing past the "Press any key" splash. The client state there is Menu or LoadMenu
        (it flips between them on the splash): never a reason to wait for Running."""
        if not self.wait_for("debug port", port_open, 240):
            return False
        if not self.wait_for("client game state Menu", lambda: game_state() in MENU_STATES, 300):
            return False
        if "ui.state" not in available_ops("client"):
            for _ in range(2):  # without ui.state (no mod yet): two blind presses
                self.try_menu_key()
                time.sleep(MENU_POLL)
            return True
        loadmenu_since = None
        presses = 0

        def at_menu() -> bool:
            nonlocal loadmenu_since, presses
            state, st = game_state(), ui_state() or {}
            if state == "Menu" and st.get("screen") == "main_menu":
                return True
            loadmenu_since = (loadmenu_since or time.time()) if state == "LoadMenu" else None
            # Only on the splash: a key on the main menu itself would hit Continue (someone else's save, maybe).
            if (state == "Menu" and at_splash(st)) or (
                    loadmenu_since is not None and time.time() - loadmenu_since >= SPLASH_LOADMENU_S):
                presses += 1
                how = self.try_menu_key()
                log(f"splash screen (state {state}): pressed {how} ({presses})")
                loadmenu_since = None   # give the press time to act before the next one
            return False
        if not self.wait_for("the main menu", at_menu, MENU_TIMEOUT, MENU_POLL):
            return False
        return True

    def try_menu_key(self) -> str:
        try:
            return press_menu_key()
        except Exception as e:  # noqa: BLE001
            log(f"key press failed: {e}")
            return "nothing (failed)"

    def wait_running(self) -> bool:
        """After a load was started: Running, then the server context. A splash on the way gets a key press."""
        def running() -> bool:
            state = game_state()
            if state == "Running":
                return True
            if state == "Menu" and at_splash(ui_state()):
                log(f"splash screen while waiting for the load: pressed {self.try_menu_key()}")
            return False
        return (self.wait_for("game state Running", running, 600, MENU_POLL)
                and self.wait_for("server context", server_ready, 120, 3))

    def own_saves(self) -> list[Save]:
        """list_saves() minus saves older than this playthrough's run start (runner.json saves_after, set for a
        --runs run): a series reuses one character name, and run 2 must never load run 1's saves."""
        after = self.state["saves_after"]
        saves = list_saves()
        return [s for s in saves if s.mtime >= after or s.folder == self.opt.start_save] if after else saves

    def play_windows(self) -> list[tuple[float, float]]:
        return play_windows(self.state["history"], getattr(self.state, "dir", None))

    def ours(self) -> set[str]:
        """Save folders pick_save takes whatever their label: the runner's quicksaves, the agent's own quicksaves
        (agent_saves, RUN-6: S059's newest good state was one) and a scenario's start save (older than every save of
        the run, so the agent's first save supersedes it)."""
        return (set(self.state["runner_saves"] or []) | set(self.state["agent_saves"] or [])
                | ({self.opt.start_save} if self.opt.start_save else set()))

    def record_agent_saves(self, since: float) -> list[str]:
        """The quicksave folders the play session's MCP server wrote since `since` (its save_game.bg lines in
        actions.jsonl: the folder found on disk) → runner.json agent_saves (the last AGENT_SAVES_KEPT). Named
        Sxxx- saves need no record (pick_save takes them by label). Only this playthrough's owner's folders."""
        found = agent_quicksaves(read_actions_since(since), self.state["save_owner"] or self.opt.save_owner)
        if found:
            kept = [f for f in self.state["agent_saves"] or [] if f not in found] + found
            self.state["agent_saves"] = kept[-AGENT_SAVES_KEPT:]
        return found

    def choose_save(self) -> Save | None:
        handoff = self.mem / "HANDOFF.md"
        text = handoff.read_text() if handoff.exists() else ""
        saves = self.own_saves()
        save, why = pick_save_why(saves, parse_handoff_save(text), self.state["save_owner"], self.ours(),
                                  windows=self.play_windows(), handoff_session=parse_handoff_session(text),
                                  ends=session_ends(self.state["history"], getattr(self.state, "dir", None)))
        blocked = set(self.state["oom_blocked"] or [])
        if save and save.folder in blocked:
            first = save
            save = self.refuse_oom_save(save, saves, blocked)
            why = f"\"{first.label}\" is OOM-blocked ({why}): the newest other save"
        self.save_choice = (save.label, why) if save else None
        log(f"save choice: {save.folder if save else 'none'}: {why}")
        if save and not self.state["save_owner"] and not self.opt.dry_run:
            self.adopt_owner(save.owner)
        return save

    def loaded_why(self, loaded: str | None) -> str:
        """Why the runner loaded `loaded` (choose_save's reason), "" when it loaded another save (a crash fallback)."""
        choice = getattr(self, "save_choice", None)
        return choice[1] if loaded and choice and choice[0] == loaded else ""

    def refuse_oom_save(self, save: Save, saves: list[Save], blocked: set[str]) -> Save:
        """The newest save OOM-killed the game OOM_SAVE_LIMIT times in a row right after loading: load the newest
        other save of the playthrough (loudly), or stop the runner when there is none (a crash loop needs a human)."""
        alt = pick_save([s for s in saves if s.folder not in blocked], None, self.state["save_owner"], self.ours(),
                        windows=self.play_windows())
        streak = self.state["oom_streak"] or {}
        why = streak.get("why", "OOM")
        if alt is None:
            raise LaunchGaveUp(f"save {save.folder} OOM-killed the game {OOM_SAVE_LIMIT}× in a row after loading "
                               f"({why}) and there is no other save to load")
        msg = (f"REFUSING TO RESUME from {save.folder}: it OOM-killed the game {OOM_SAVE_LIMIT}× in a row right after "
               f"loading ({why}); loading the older {alt.folder} instead")
        log(f"!!!!! {msg} !!!!!")
        self.event("oom_save_refused", save=save.folder, instead=alt.folder, why=why[:300])
        write_status(alert={"t": time.time(), "kind": "oom_save_refused", "msg": msg[:600]})
        tools = streak.get("tools") or []
        self.pending_notes.append(
            f"RUNNER NOTE: the save \"{save.label}\" made the game run out of memory {OOM_SAVE_LIMIT} times in a row "
            f"right after loading (the kernel killed it); the runner loaded the older save \"{alt.label}\" instead. "
            "Progress after it is lost in the game: replay it, and fix HANDOFF.md/objectives. "
            + (f"The last tool calls before the memory blow-up were: {' | '.join(tools[-5:])}. Avoid repeating "
               "them exactly (another route or approach). " if tools else ""))
        return alt

    def note_game_gone(self, oom: str | None) -> None:
        """The game is down at a session start and the runner didn't kill it after the last session: it crashed at
        (or after) the end of that session. Warn the next session with the last tool calls before the end, once."""
        last = next((h for h in reversed(self.state["history"] or []) if h.get("kind") == "play"), None)
        if not last or not last.get("ended") or self.state["crash_noted"] == last.get("session"):
            return
        if (self.state["game_killed_at"] or 0) >= last["ended"] - 5:
            return   # killed on purpose (age relaunch, recovery) after that session
        self.state["crash_noted"] = last.get("session")
        why = f"{last.get('session')}: the game was gone at the next session start" + (f"; {oom}" if oom else "")
        log(f"game gone since {last.get('session')} ended: warning the next session with its last tool calls")
        self.pending_notes.append(crash_tools_note(why, read_actions_since(last["ended"] - 600), last["ended"]))

    def check_game_oom(self) -> str | None:
        """After the game died (recover(), or a runner start finding it gone): did the kernel OOM-kill it? Updates
        the consecutive-OOM streak for the save that game had loaded; at OOM_SAVE_LIMIT that save is blocked
        (choose_save refuses it). Checked once per launch. -> the OOM reason, or None."""
        g = self.state["game"] or {}
        stamp, launched = g.get("stamp"), g.get("launched")
        if not stamp or not launched or self.state["oom_checked"] == stamp:
            return None
        self.state["oom_checked"] = stamp
        oom = find_game_oom(user_journal(launched), stamp, read_spikes(launched))
        kills = [k for k in read_spikes(launched, RUNTIME / "ram_kills.jsonl") if k.get("kind") == "kill"]
        if not oom and kills:
            oom = (f"ramwatch killed the game at {datetime.fromtimestamp(kills[-1]['t']):%H:%M:%S} before the OOM "
                   f"killer could: {kills[-1].get('why', '?')}")
        acts = read_actions_since(launched)
        save = last_loaded_save(acts, self.own_saves(), self.state["loaded_save"])
        streak = oom_streak_update(self.state["oom_streak"], save, oom)
        if streak:
            spikes = read_spikes(launched)
            streak["tools"] = (spikes[-1].get("tools", []) if spikes else
                               ramwatch.last_tools(acts[-1]["t"], RUNTIME / "actions.jsonl") if acts else [])
        self.state["oom_streak"] = streak
        if not oom:
            return None
        log(f"!!!!! GAME OOM: {oom}; save loaded before it: {save} (OOM {streak['n'] if streak else 1} in a row) !!!!!")
        self.event("game_oom", why=oom[:300], save=save, streak=streak["n"] if streak else 1,
                   tools=(streak or {}).get("tools", [])[-8:])
        if streak and streak["n"] >= OOM_SAVE_LIMIT:
            self.state["oom_blocked"] = sorted(set(self.state["oom_blocked"] or []) | {save})
        return oom

    def foreign_use(self, save: Save) -> str | None:
        """foreign_use_since() for the running game; a relaunch counts only for a game this runner didn't start."""
        started = None if self.owns_game() else game_started_at()
        return foreign_use_since(save, list_saves(), read_actions_since(save.mtime), started, self.ours())

    def restart_drift(self) -> str | None:
        """state_drift() between the last play session's end and the running game (a runner restart only). Fails
        closed (RUN-6): a game whose progress can't be read is not proven to be where the session ended → reload."""
        last = next((h for h in reversed(self.state["history"] or []) if h.get("kind") == "play"), None)
        if not last or not last.get("end_sig"):
            return None
        now = read_progress()
        if now is None:
            return "its progress can't be read (state.status/state.journal), so it isn't known to be where it was"
        return state_drift(last["end_sig"], now)

    def load_save(self, save: Save | None, force: bool = False) -> str | None:
        """Bring the game to this playthrough's latest save (or the main menu for a new game). force: load it even
        when --take-over would play on from the running game (restart_drift found it elsewhere)."""
        ui_load = "ui.menu" in available_ops("client") if port_open() else False
        self.new_game = save is None
        if save is None:
            if port_open() and game_state() == "Running" and server_ready() and self.opt.take_over:
                # A new game already past character creation (e.g. started by an operator or a previous
                # session that ended before its first save): play on from here.
                host = host_character_name()
                owner = self.state.data.get("save_owner")
                if owner is None or (host and host.casefold() == owner.casefold()):
                    log(f"no save yet, but a new game is running (host '{host}'): playing on from here")
                    return None
                raise SystemExit(f"a game is running with host '{host}', not this playthrough's '{owner}'")
            log("no save for this playthrough yet: leaving the game at the main menu (new game)")
            if not port_open():
                self.launch_game(None)
            if not self.wait_menu():
                raise RuntimeError("the main menu never came up")
            owner = self.opt.save_owner or self.state["save_owner"]
            char = self.profile.get("character")
            if char:
                # CHAR-1 phase 0: the agent designs the character from the brief; a name only when one is given.
                self.pending_notes.append(character_creation_note(self.profile, char.get("name") or owner))
            elif owner:
                # Saves are matched to this playthrough by the character's name: a "Tav" breaks that.
                self.pending_notes.append(new_game_note(owner))
            return None
        if port_open() and game_state() == "Running" and server_ready() and self.opt.take_over and not force:
            # The operator says: take over the game as it is. If it's this playthrough's character, play on
            # from here instead of reloading (a reload only loses the minutes since the last save).
            host = host_character_name()
            owner = self.state.data.get("save_owner") or save.owner
            touched = self.foreign_use(save)
            if host and owner and host.casefold() == owner.casefold() and not touched:
                log(f"--take-over: the running game is '{host}' (this playthrough): playing on from here")
                return None
            if touched:
                log(f"--take-over: not playing on from the running game: {touched}")
        log(f"loading save {save.folder}")
        use_menu = port_open() and ui_load
        if use_menu:
            state = game_state()
            if state != "Running" and not self.wait_menu():   # splash included: press past it first
                raise RuntimeError(f"the main menu never came up (state {game_state()})")
            # The mod refuses a load while its persisted load gate is off (the default on a fresh profile). This
            # is this playthrough's own save, which is exactly when the gates may open; apply_play_settings
            # re-decides them by the loaded character's name.
            try:
                rpc("ui.config", {"save_enabled": True, "load_enabled": True}, context="client")
            except Exception as e:  # noqa: BLE001 - a refusal below falls back to a relaunch
                log(f"opening the save/load gates before the load failed: {e}")
            try:
                # ui.menu load works from the main menu and from in-game (it opens the load screen itself).
                rpc("ui.menu", load_menu_args(save, list_saves()), context="client", timeout=30)
            except (RpcError, LuaDebugError) as e:
                # Refused (gate, no such playthrough or date on the page) or the eval failed: restart the game
                # with -loadSaveGame, which needs neither the gate nor the Load page.
                log(f"ui.menu load of {save.folder} refused ({e}); relaunching the game with --load instead")
                self.event("load_refused", save=save.folder, error=str(e)[:300])
                use_menu = False
            if use_menu:
                # Wait for Running only once the load really started. From in-game the state stays Running until
                # the load begins (S002's agent started mid-load); from the menu a load that never starts is an
                # error, not a 10-minute wait.
                here = ("Running",) if state == "Running" else MENU_STATES
                if not self.wait_for("the load to start", lambda: game_state() not in here, LOAD_START_TIMEOUT,
                                     0.5):
                    raise RuntimeError(f"the load of {save.folder} never started (state {game_state()})")
        if not use_menu:
            if port_open():
                if not (self.owns_game() or self.opt.take_over or game_state() in MENU_STATES):
                    raise SystemExit("would have to restart a game the runner didn't launch; "
                                     "close it or pass --take-over")
                self.kill_game()
            save = self.launch_and_load(save)
            self.state["loaded_save"] = save.folder
            write_status(last_loaded=save.folder)
            return save.label
        if not self.wait_running():
            raise RuntimeError(f"save {save.folder} didn't finish loading")
        self.state["loaded_save"] = save.folder
        write_status(last_loaded=save.folder)
        return save.label

    def previous_good_save(self, failed: set[str]) -> Save | None:
        """The fallback when a save keeps crashing the game on load: the last save the runner loaded successfully,
        else this playthrough's newest other save. None = nothing else to try."""
        saves = [s for s in self.own_saves() if s.folder not in failed]
        good = self.state["loaded_save"]
        hit = next((s for s in saves if s.folder == good), None)
        return hit or pick_save(saves, None, self.state["save_owner"], self.ours(), windows=self.play_windows())

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self.abort:
            time.sleep(min(1.0, max(0.0, end - time.time())))

    def launch_and_load(self, save: Save) -> Save:
        """Launch the game with this save and wait until it plays; returns the save that loaded. A launch that dies on
        the way (LaunchWatch, launcher exit, load timeout) is killed and retried after LAUNCH_BACKOFF; after the
        second failure with previous_good_save(). LAUNCH_ATTEMPTS failures in a row → LaunchGaveUp (the runner stops:
        a crash loop needs a human, e.g. a bg3le fix)."""
        failed: list[str] = []
        errors: list[str] = []
        target = save
        for attempt in range(1, LAUNCH_ATTEMPTS + 1):
            try:
                self.launch_game(target.folder)
                if not self.wait_for("debug port", port_open, 240):
                    raise GameDied("the debug port never opened")
                if not self.wait_running():
                    raise GameDied(f"save {target.folder} didn't finish loading")
            except GameDied as e:
                failed.append(target.folder)
                errors.append(f"{target.folder}: {e}")
                log(f"launch {attempt}/{LAUNCH_ATTEMPTS} with {target.folder} failed: {e}")
                self.event("launch_failed", attempt=attempt, save=target.folder, error=str(e)[:300])
                self.kill_game()
                if attempt == LAUNCH_ATTEMPTS:
                    break
                if self.abort:
                    raise RuntimeError("aborted between launch attempts") from e
                if attempt >= 2:
                    prev = self.previous_good_save(set(failed))
                    if prev is not None:
                        target = prev
                wait = LAUNCH_BACKOFF[min(attempt, len(LAUNCH_BACKOFF)) - 1]
                log(f"retrying in {wait} s with {target.folder}")
                self.sleep(wait)
                continue
            if failed:
                log(f"game up after {len(failed)} failed launch(es): {target.folder} loaded")
                if target.folder != save.folder:
                    self.pending_notes.append(
                        f"RUNNER NOTE: the newest save \"{save.label}\" crashed the game on load {len(failed)} times; "
                        f"the runner loaded the older save \"{target.label}\" instead. Progress after it is lost "
                        "in the game: replay it, and fix HANDOFF.md/objectives if they assume the newer save.")
            return target
        raise LaunchGaveUp(f"{LAUNCH_ATTEMPTS} launches in a row died before the save played: " + "; ".join(errors))

    def give_up_launch(self, e: LaunchGaveUp) -> None:
        """The loud stop after LaunchGaveUp: runner log, runner_events (dashboard) and a status alert."""
        log(f"!!!!! GAME CRASH LOOP: {e}. The runner stops; fix the crash (bg3le log in runtime/logs), "
            "then restart the runner. !!!!!")
        self.event("launch_gave_up", error=str(e)[:600])
        write_status(alert={"t": time.time(), "kind": "launch_gave_up",
                            "msg": f"runner stopped: the game crashes on every launch ({e})"[:600]})

    def maybe_relaunch(self, first: bool, minutes: float = 0.0) -> str | None:
        """Between sessions: kill a game process that relaunch_reason() condemns for the next session (`minutes`
        long, plus the fight hold), so ensure_game() launches a fresh one with the newest save. Never kills when the relaunch can't happen (Steam not offline: the launch guard
        would refuse) or would lose play (the newest save is older than the last session's start). -> the reason."""
        if self.opt.dry_run or not port_open():
            return None
        if first and not (self.owns_game() or self.opt.take_over):
            return None  # ensure_game decides about a game the runner didn't launch
        hist = [h for h in self.state["history"] if h.get("kind") == "play"]
        last = hist[-1] if hist else None
        text = None
        if last:
            try:
                text = (self.state.dir / f"{last['session']}.log").read_text(errors="replace")
            except OSError:
                text = None
        g = self.state["game"] or {}
        started = g.get("launched") if self.owns_game() and g.get("launched") else game_started_at()
        ram = game_ram_mb()
        why = relaunch_reason(started, time.time(), text, session_s=minutes * 60 + FIGHT_HOLD_S if minutes else 0.0,
                              ram_mb=ram)
        if why is None:
            if started:
                log(f"game process {(time.time() - started) / 3600:.1f} h old, {ram if ram is not None else '?'} MB "
                    f"RAM+swap, no ready-check failure last session: kept (relaunch at {GAME_MAX_AGE_H:g} h by the "
                    f"session's end or {RELAUNCH_RAM_MB} MB)")
            return None
        save = self.choose_save()
        if save is None:
            log(f"game relaunch wanted ({why}) but this playthrough has no save to load: skipped")
            return None
        if last and save.mtime < last.get("started", 0):
            log(f"game relaunch wanted ({why}) but the newest save {save.label} is older than {last['session']}'s "
                "start: skipped (it would lose play)")
            return None
        ok, steam_why = steam_ready()
        if not ok:
            log(f"game relaunch wanted ({why}) but Steam is not offline ({steam_why}): keeping the running game")
            self.event("game_relaunch_skipped", why=why, steam=steam_why)
            return None
        log(f"relaunching the game: {why}; then loading {save.folder}")
        self.event("game_relaunch", why=why, save=save.folder)
        self.kill_game()
        self.state["loaded_save"] = None
        return why

    def ensure_game(self, first: bool) -> str | None:
        """Make sure the game runs with this playthrough loaded. Returns the loaded save label, if any."""
        state = game_state() if port_open() else None
        if state is not None and not self.owns_game():
            last = next((h for h in reversed(self.state["history"] or []) if h.get("kind") == "play"), {})
            started = game_started_at()
            if started and last.get("ended") and started > last["ended"]:
                self.note_game_gone(None)   # someone relaunched the game after the last session: it died then
        if state not in (None, *MENU_STATES, "Running"):
            # Mid-load or similar: settle at a menu or in-game; never wait for Running from the menu/splash.
            log(f"game is busy (state {state}); waiting for it to settle")
            self.wait_for("the game to settle", lambda: game_state() in (*MENU_STATES, "Running"), 600, 3)
            state = game_state()
        if state == "Running" and not server_ready():
            self.wait_for("server context", server_ready, 120, 3)
        owned = self.owns_game() and self.state["loaded_save"] is not None
        if state == "Running" and server_ready():
            if first and not owned and not self.opt.take_over:
                raise SystemExit("the game is running with a save loaded that the runner didn't load "
                                 "(maybe your own playthrough). Quit to the main menu, close the game, or "
                                 "pass --take-over.")
            drift = self.restart_drift() if first else None
            if drift:
                save = self.choose_save()
                log(f"runner restart: the running game moved since the last session ({drift}): "
                    f"loading {save.folder if save else 'nothing (no save)'}")
                self.event("restart_drift", why=drift, save=save.folder if save else None)
                if save is not None:
                    return self.load_save(save, force=True)
            if not first or owned:
                return None  # keep playing where the last session left off
        if port_open() and state is None:
            raise RuntimeError("debug port open but the game doesn't answer")
        if state is None:
            self.note_game_gone(self.check_game_oom())   # gone (e.g. 20:48: an OOM took the runner down with it)
        return self.load_save(self.choose_save())

    def apply_play_settings(self) -> None:
        """Best effort, never fatal: autoskip, no tutorials, dismiss popups, then the save/load gates."""
        self.gates = None
        for op, args in PLAY_SETTINGS:
            try:
                rpc(op, args, context="client")
            except Exception as e:  # noqa: BLE001
                log(f"play settings: {op} {args} failed: {e}")
        self.apply_ui_strict()
        name = None
        if not self.new_game:
            name = host_character_name()
            if name is None:
                log("play settings: couldn't read the avatar's name")
        owner = self.opt.save_owner or self.state["save_owner"]
        allow = self.gates = gates_decision(name, owner, self.new_game)
        if allow is None:
            log(f"save/load gates left unchanged (character {name!r}, save owner {owner!r})")
            return
        try:
            rpc("ui.config", {"save_enabled": allow, "load_enabled": allow}, context="client")
            log(f"save/load gates {'enabled' if allow else 'DISABLED'} "
                f"({'new game' if self.new_game else f'character {name!r}, save owner {owner!r}'})")
        except Exception as e:  # noqa: BLE001
            log(f"play settings: ui.config gates failed: {e}")

    def apply_ui_strict(self) -> None:
        """BG3AI_UI_STRICT=1/0: bg3le's strict frame lock (patch 0010) on/off in the running game, so a game kept
        across sessions follows the switch without a relaunch (launch_game.sh passes BG3LE_UI_STRICT=1 for the
        launch itself). Unset: left as it is. Logs the lock counters it reads back."""
        on = ui_strict_wanted(os.environ)
        if on is None:
            return
        try:
            log("ui strict: " + ui_strict_text(lua_eval(ui_strict_lua(on), "client")))
        except Exception as e:  # noqa: BLE001
            log(f"ui strict: setting {'on' if on else 'off'} failed: {e}")

    def hot_reload_mod(self) -> int:
        """Between sessions: if any file under mod/ changed since the game last loaded it, Ext.Debug.Reset() and
        wait for the mod's RPC table to come back. Returns the number of changed files (0 = nothing done)."""
        cur = mod_tree_hashes()
        changed = changed_files(self.state["mod_hashes"], cur)
        if not changed:
            return 0
        state = game_state() if port_open() else None
        if state not in ("Running", "Menu"):
            log(f"mod changed ({len(changed)} files) but the game is {state or 'not up'}; reload skipped")
            return 0
        ctxs = ["server", "client"] if state == "Running" else ["client"]  # no server context at the menu
        try:
            lua_eval("Ext.Debug.Reset() return 'reset'", ctxs[0], timeout=EVAL_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - the reset can drop the eval connection
            log(f"hot reload: Reset eval returned {e!r} (usually harmless)")

        def back() -> bool:
            try:
                return all(lua_eval(MOD_READY_LUA, c, timeout=5).strip() == "true" for c in ctxs)
            except Exception:  # noqa: BLE001
                return False
        time.sleep(1)
        if not self.wait_for("BG3AI mod after Ext.Debug.Reset()", back, RELOAD_TIMEOUT, 1):
            raise RuntimeError("the mod didn't come back after a hot reload")
        self.state["mod_hashes"] = cur
        shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
        log(f"mod hot-reloaded ({len(changed)} files changed: {shown})")
        return len(changed)

    def recover(self, why: str) -> None:
        log(f"recovering: {why}")
        heat = None
        if not self.game_alive():
            oom = self.check_game_oom()
            if oom:
                why = f"{why} (out of memory: {oom})"
            heat = self.heat_killed()
            if heat:
                why = f"{why} (the heat guard stopped it: {heat})"
        crashed_at = time.time()
        self.kill_game()
        if heat:
            self.heat_paused(after_kill=True)   # no relaunch into the same heat
        if why == "stopped responding":
            mark_inflight_stuck(SE_BG3AI_DIR)
        save = self.choose_save()
        label = self.load_save(save)
        self.pending_notes.append(
            f"RUNNER NOTE: the game {why} at {datetime.fromtimestamp(crashed_at):%H:%M}; the runner restarted it and "
            f"resumed from save \"{label or 'main menu'}\". Anything after that save is lost: reconcile your notes "
            "and the session log against get_status before continuing.")
        self.pending_notes.append(crash_tools_note(f"the game {why}", read_actions_since(crashed_at - 600), crashed_at))

    # ---------------------------------------------------------------- saves

    def ensure_save(self, since: float) -> None:
        owner = self.state["save_owner"]
        new = saves_since(list_saves(), since, owner)
        if new:
            if not owner:
                self.adopt_owner(new[-1].owner)
            log(f"session saved: {new[-1].folder}")
            return
        others = [s.folder for s in list_saves() if s.mtime >= since and is_ai_label(s.label, "")]
        if not self.game_alive() or not server_ready():
            log("no save this session and the game isn't ready for one")
            return
        if others:
            # Named saves exist but don't belong to this playthrough's owner/prefix: say why, then quicksave.
            log(f"session saves {others} don't match save owner {owner!r} / BG3AI_SAVE_PREFIX "
                f"{SAVE_PREFIX!r} (wrong character name?); quicksaving")
        else:
            log("the agent didn't save this session; quicksaving")
        before = {s.folder for s in list_saves()}
        t0 = time.time()
        try:
            if "ui.menu" in available_ops("client"):
                rpc("ui.menu", {"action": "quicksave"}, context="client", timeout=30)
            else:
                lua_eval('Ext.Input.InjectKeyPress("F5") return 1')  # default quicksave binding
        except Exception as e:  # noqa: BLE001
            log(f"quicksave request failed: {e}")
            return
        end = time.time() + 60
        while time.time() < end:
            fresh = [s for s in list_saves() if s.folder not in before and s.mtime >= t0 - 1]
            if fresh:
                s = max(fresh, key=lambda x: x.mtime)
                if owner and s.owner != owner:
                    log(f"quicksave {s.folder} has an unexpected owner; not recording it")
                    return
                self.state["runner_saves"] = self.state["runner_saves"] + [s.folder]
                log(f"quicksave written: {s.folder}")
                return
            time.sleep(2)
        log("quicksave didn't appear within 60 s")

    # ---------------------------------------------------------------- claude

    def model_for(self, tag: str = "") -> tuple[str, str | None]:
        """(model, effort) for a session: CLI options < session-config.json; the fix pass has its own."""
        if tag == "-fix":
            return self.opt.fix_model or self.opt.model, self.opt.fix_effort
        state = getattr(self, "state", None)
        o = session_overrides(state.dir / "session-config.json") if state else {}
        return o.get("model", self.opt.model), o.get("effort", self.opt.effort)

    @property
    def client(self) -> clients.Client:
        """The play CLI (--client), built once in __init__ (a bare Runner in a test builds it on first use)."""
        if self._client is None:
            self._client = make_client(self.opt)
        return self._client

    def play_request(self, prompt: str, session: str | None, resume: bool, tag: str = "", env: dict | None = None,
                     sid: str = "") -> clients.Request:
        """One play process for the client: model and effort (model_for), the fallback model, the autocompact
        window (play sessions only, not the fix pass), the session env and the file stem (sid + tag) that names a
        client's per-session config file."""
        model, effort = self.model_for(tag)
        state = getattr(self, "state", None)
        ac = None
        if tag != "-fix":
            ac = (session_overrides(state.dir / "session-config.json") if state else {}).get(
                "autocompact", self.opt.autocompact)
        sdir = state.dir if state else RUNTIME / "sessions" / self.opt.playthrough
        return clients.Request(prompt, model, sdir, session=session, resume=resume, effort=effort,
                               fallback_model=self.opt.fallback_model, autocompact=ac, env=dict(env or {}),
                               tag=sid + tag)

    def agent_cmd(self, req: clients.Request) -> list[str]:
        """The play process's command line (the client's; tests swap the binary here)."""
        return self.client.command(req)

    def save_env(self) -> dict:
        """The MCP server's save identity and gates: BG3AI_SAVE_OWNER/PREFIX (save_identity) and BG3AI_SAVE_GATES;
        a --runs run's BG3AI_SAVES_AFTER, a scenario's BG3AI_START_SAVE (older than the run, still its save), and
        BG3AI_SAVES_EXCLUDE: the owner's saves another playthrough or a worker wrote (foreign_saves, newline-separated)."""
        state = getattr(self, "state", None)
        owner, prefix = save_identity(self.opt.save_owner or (state["save_owner"] if state else None))
        after = state["saves_after"] if state else None   # a --runs run: the server's saves start at the run
        foreign = (foreign_saves(list_saves(), owner, self.ours(), self.play_windows(), prefix)
                   if state and owner and not after else [])
        return {"BG3AI_SAVE_OWNER": owner, "BG3AI_SAVE_PREFIX": prefix, **gates_env(getattr(self, "gates", None)),
                **({"BG3AI_SAVES_AFTER": str(int(after))} if after else {}),
                **({"BG3AI_SAVES_EXCLUDE": "\n".join(foreign)} if foreign else {}),
                **({"BG3AI_START_SAVE": start} if (start := getattr(self.opt, "start_save", None)) else {})}

    def run_claude(self, sid: str, prompt: str, minutes: float, claude_session: str,
                   resume: bool = False, tag: str = "", needs_game: bool = True) -> SessionResult:
        """needs_game=False (the fix-notes pass edits memory only): a game crash meanwhile doesn't stop it (S027-fix
        was killed 30 s in by the OOM kill of the game); the next session's setup relaunches the game."""
        start = time.time()
        deadline, hard_stop = deadlines(start, minutes)
        client = self.client
        session_env = dict(BG3AI_SESSION_DEADLINE=str(int(deadline)),
                           BG3AI_PLAYTHROUGH=self.opt.playthrough,
                           BG3_SESSION_MINUTES=str(minutes),
                           BG3_WRAP_MINUTES=str(wrap_minutes(minutes)),
                           # when the MCP clock line switches to WRAP UP (epoch seconds); play until then
                           BG3AI_SESSION_WRAPUP=str(int(start + wrap_minutes(minutes) * 60)))
        base_env = dict(os.environ)
        base_env.pop("BG3AI_SAVE_GATES", None)
        session_env.update(self.save_env())
        base = self.state.dir / f"{sid}{tag}"
        session_env["BG3AI_METRICS_PATH"] = f"{base}.metrics.jsonl"  # the bridge's per-tool latency records
        # the runner opened this memory session: the MCP server attaches, never opens one
        session_env["BG3AI_SESSION"] = sid
        session_env["BG3AI_FIGHT_HOLD_PATH"] = str(fight_hold_path(self.state.dir, sid))  # per session, not per -rN
        # the memory keeper compacts old logs between sessions: session.py then doesn't ask the player to
        session_env["BG3AI_KEEPER"] = "1" if self.opt.keeper and not self.opt.dry_run else "0"
        if not needs_game:
            session_env["BG3AI_FIX_NOTES"] = "1"   # the stop check lints + commits instead of "keep playing"; no game I/O
        # The runner's own BG3AI_*/BG3_* go to the MCP server too (claude passes its whole env on; codex and gemini
        # forward Request.env by name).
        session_env = {**{k: base_env[k] for k in clients.session_env_names(base_env)}, **session_env}
        # A client that names the conversation itself (codex, gemini) starts without an id: it comes in the
        # stream's init event. One that never got it can't resume: that process starts a new conversation.
        if not client.assigns_session and not resume:
            claude_session = None
        if resume and not claude_session:
            log(f"{sid}{tag}: no {client.name} session id to resume; starting a new conversation")
            resume = False
        req = self.play_request(prompt, claude_session, resume, tag, session_env, sid)
        written = client.write_config(req)
        # The playthrough has its own memory system; Claude Code's auto-memory would add a second one (and a
        # per-machine path in the prompt): CLAUDE_CODE_DISABLE_AUTO_MEMORY is ClaudeClient.env's.
        env = clients.process_env(client, req, base_env)
        cmd = self.agent_cmd(req)
        box, cname = self.box, None
        if box is not None:   # --sandbox container: the client in the container, the bg3 server over http here
            cname = box.container_name(sid + tag)
            cmd = box.run_argv(cname, cmd, client.env(req), written)
        model, effort = self.model_for(tag)
        write_status(playthrough=self.opt.playthrough, session=sid + tag, claude_session=claude_session,
                     model=model, effort=effort or "default",
                     started=start, deadline=deadline, hard_stop=hard_stop, log=str(base) + ".log",
                     metrics=f"{base}.metrics.jsonl", turns=f"{base}.turns.jsonl",
                     progress=f"{base}.progress.jsonl", session_ended=None)
        self.write_status_line(f"playing {sid}{tag} (until {datetime.fromtimestamp(deadline):%H:%M})")
        log(f"{sid}{tag}: {client.name} starting ({model}, effort {effort or 'default'}; deadline "
            f"{datetime.fromtimestamp(deadline):%H:%M}, hard stop {datetime.fromtimestamp(hard_stop):%H:%M})")
        if self.opt.dry_run:
            print(json.dumps({"cmd": cmd, "cwd": str(PLAY_DIR), "deadline": deadline}, indent=1))
            return SessionResult(sid, claude_session, 0, "dry_run", start, start, model=model, effort=effort)

        jsonl = open(f"{base}.jsonl", "a")
        human = open(f"{base}.log", "a", buffering=1)
        human.write(f"===== {sid}{tag} {datetime.now():%Y-%m-%d %H:%M:%S} {client.name} session "
                    f"{claude_session or '(new)'}\n")
        human.write("[prompt] " + _short(prompt, 4000) + "\n")
        if box is not None:
            try:
                self._sandbox_live = (box.start_server(clients.bg3_server(), sandbox_server_env(base_env, req),
                                                       PLAY_DIR, Path(f"{base}.mcp-http.log")), cname)
            except sandbox.SandboxError as e:   # RUN-8's path: a setup failure, the agent would have no tools
                detail = f"sandbox: {e}"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {detail}\n")
                human.close()
                jsonl.close()
                log(f"{sid}{tag}: {detail} (a setup failure)")
                self.event("mcp_failed", session=sid + tag, detail=detail[:300])
                return SessionResult(sid, claude_session, None, "mcp_failed", start, time.time(), model=model,
                                     effort=effort, detail=detail)
        try:
            proc = subprocess.Popen(cmd, cwd=PLAY_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True, text=True, bufsize=1)
        except OSError:
            self.sandbox_cleanup()
            human.close()
            jsonl.close()
            raise

        timing = latency.StreamTiming(Path(f"{base}.turns.jsonl"))
        last_output = [time.time()]  # the agent-hang watchdog: any stream line counts as alive
        meter = None   # --max-cost/--max-tokens: the run's cost and tokens as this stream goes (runs.UsageMeter)
        if self.has_usage_caps() and self.run_started is not None:
            done = runs.claude_totals(self.state.dir, self.run_started)
            meter = runs.UsageMeter(done["cost_usd"], runs.token_total(done["tokens"]))
        seen_session = [claude_session]   # the conversation id: assigned up front, or the stream's init event's
        api = {"texts": [], "rejected": False, "resets_at": None, "mcp": None}   # RUN-7/RUN-8, from the stream

        def pump():
            for line in proc.stdout:
                last_output[0] = time.time()
                jsonl.write(line)
                jsonl.flush()
                stamp = datetime.now().strftime("%H:%M:%S")
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    if line.strip():
                        human.write(f"{stamp} [raw] {line.rstrip()[:500]}\n")
                    continue
                try:
                    events = client.parse(line)
                except Exception as e:  # noqa: BLE001 - a line the parser chokes on is logged, never fatal
                    human.write(f"{stamp} [raw] {type(e).__name__}: {line.rstrip()[:400]}\n")
                    continue
                for e in events:
                    for text in clients.format_event(e):
                        human.write(f"{stamp} {text}\n")
                    if e.kind == "init" and e.session_id and not seen_session[0]:
                        seen_session[0] = e.session_id
                        write_status(claude_session=e.session_id)
                    if e.kind == "init" and api["mcp"] is None:
                        api["mcp"] = mcp_problem(e.mcp, client.name == "claude") or ""
                    if e.api_error and e.kind in ("text", "error", "result") and e.text:
                        api["texts"].append(e.text[:300])
                    if e.kind == "rate_limit":
                        api["resets_at"] = e.resets_at or api["resets_at"]
                        api["rejected"] = api["rejected"] or e.api_error
                try:
                    # bg3ai.metrics reads claude's stream-json; other clients' events are fed in that shape
                    for tev in ([ev] if client.name == "claude" else [timing_event(e) for e in events]):
                        if tev is not None:
                            timing.on_event(tev)
                    if meter is not None:
                        for e in events:
                            meter.on_event(e)
                except Exception:  # noqa: BLE001 - metrics never break a session
                    pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        poll_stop = threading.Event()
        poller = threading.Thread(target=self.progress_loop, args=(sid + tag, base, start, poll_stop, human),
                                  name="progress", daemon=True)
        poller.start()

        health = HealthTracker()
        next_health = time.time() + HEALTH_INTERVAL
        mod_gone_since = None   # RUN-8: the game answers but Mods.BG3AI is gone (a Lua reset, a mod load error)
        reason = "exited"
        detail, fault_at = "", None
        signals_sent = []
        hold, held_until, planned_stop = fight_hold_path(self.state.dir, sid), deadline, hard_stop
        while proc.poll() is None:
            time.sleep(2)
            now = time.time()
            if reason == "exited" and not signals_sent and needs_game and held_deadline(hold, deadline) != held_until:
                # the cap while the fight lasts, then the fight's end + the wrap-up; never before the planned stop
                held_until = held_deadline(hold, deadline)
                hard_stop = max(planned_stop, held_until + HARD_STOP_GRACE)
                human.write(f"{datetime.now():%H:%M:%S} [runner] fight hold (a fight at WRAP UP): session end "
                            f"{datetime.fromtimestamp(held_until):%H:%M:%S}, hard stop "
                            f"{datetime.fromtimestamp(hard_stop):%H:%M}\n")
                write_status(deadline=held_until, hard_stop=hard_stop)
            if self.abort and reason == "exited":
                reason = "aborted"
                hard_stop = min(hard_stop, now)
            if needs_game and now >= next_health and reason == "exited":
                alive = self.game_alive()
                ok = alive and game_state(HEALTH_EVAL_TIMEOUT) is not None
                health.record(ok)
                next_health = time.time() + (HEALTH_INTERVAL if ok else HEALTH_RETRY_INTERVAL)
                write_status(heartbeat=time.time())
                if ok and mod_loaded(HEALTH_EVAL_TIMEOUT) is False:
                    mod_gone_since = mod_gone_since or now
                else:
                    mod_gone_since = None
                if not alive or health.hung:
                    reason = "crash" if not alive else "hang"
                    detail, fault_at = self.fault_detail(health), now
                elif mod_gone_since is not None and now - mod_gone_since >= MOD_GONE_S:
                    reason, fault_at = "hang", now   # GAME_FAULTS: recover (relaunch + load) and resume claude
                    detail = (f"the game answers but the BG3AI mod is gone (Mods.BG3AI) for {now - mod_gone_since:.0f} s: "
                              "a Lua reset or a mod load error; the agent's tools can't work")
            if needs_game and reason == "exited" and self.until_hit:
                reason = "milestone"
                human.write(f"{datetime.now():%H:%M:%S} [runner] milestone {self.until_hit['id']} reached; "
                            f"stopping claude ({'the game is finished' if self.game_ended() else '--until'})\n")
                hard_stop = min(hard_stop, now)
            if reason == "exited" and meter is not None and self.check_usage_caps(meter.cost, meter.tokens):
                reason = "cap"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {self.cap_hit['why']}; stopping claude\n")
                hard_stop = min(hard_stop, now)
            if reason == "exited" and api["mcp"]:
                # RUN-8: a toolless agent would be pushed to its hard stop by the stop check: end it at once
                reason, detail = "mcp_failed", api["mcp"]
                human.write(f"{datetime.now():%H:%M:%S} [runner] {detail}; stopping {client.name}\n")
                log(f"{sid}{tag}: {detail}; stopping {client.name} (a setup failure)")
                self.event("mcp_failed", session=sid + tag, detail=detail)
                hard_stop = min(hard_stop, now)
            silent = now - last_output[0]
            if reason == "exited" and silent >= AGENT_SILENT_MINUTES * 60:
                reason, fault_at = "agent_hang", now
                detail = f"no claude output for {silent / 60:.1f} min (limit {AGENT_SILENT_MINUTES:g})"
            if fault_at == now:
                human.write(f"{datetime.now():%H:%M:%S} [runner] FAULT {reason}: {detail}; stopping claude\n")
                log(f"{sid}{tag}: FAULT {reason}: {detail}; stopping claude")
                self.event("fault", fault=reason, session=sid + tag, detail=detail)
                hard_stop = min(hard_stop, now)
            if now >= hard_stop and "int" not in signals_sent:
                if reason == "exited":
                    reason = "hard_stop"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {reason}: SIGINT\n")
                self._signal(proc, signal.SIGINT)
                signals_sent.append("int")
                term_at = now + SIGINT_GRACE
            if "int" in signals_sent and "term" not in signals_sent and now >= term_at:
                self._signal(proc, signal.SIGTERM)
                signals_sent.append("term")
                kill_at = now + SIGTERM_GRACE
            if "term" in signals_sent and "kill" not in signals_sent and now >= kill_at:
                self._signal(proc, signal.SIGKILL)
                signals_sent.append("kill")
        reader.join(timeout=10)
        self.sandbox_cleanup()
        reason = exit_reason(reason, proc.returncode)
        if reason in ("exited", "killed") and api["mcp"]:
            reason, detail = "mcp_failed", api["mcp"]   # it exited before the 2 s loop saw the init event
        kind = classify_api_failure(api["texts"], api["rejected"]) if reason == "exited" else None
        if kind == "limit":   # RUN-7: wait it out in the run loop (never a quick failure)
            reason, detail = "api_limit", (api["texts"] or ["rate limit rejected"])[-1]
            log(f"{sid}{tag}: {client.name} hit an API limit: {detail[:200]}")
        elif kind == "auth":
            detail = api["texts"][-1]
            log(f"{sid}{tag}: {client.name} API/config error (counts toward the quick-failure stop): {detail[:200]}")
        if reason == "killed":
            detail = f"claude ended by a signal from outside the runner (exit {proc.returncode})"
            log(f"{sid}{tag}: {detail}")
        poll_stop.set()
        poller.join(timeout=20)
        end = time.time()
        write_status(session_ended=end)
        human.write(f"===== {sid}{tag} ended: {reason}, exit {proc.returncode}, "
                    f"{(end - start) / 60:.1f} min\n")
        human.close()
        jsonl.close()
        log(f"{sid}{tag}: claude finished ({reason}, exit {proc.returncode}, {(end - start) / 60:.1f} min)")
        self.write_latency(f"{sid}{tag}", base, start, end)
        self.write_progress(f"{sid}{tag}", base, start, end)
        self.write_triage(f"{sid}{tag}", base)
        return SessionResult(sid, seen_session[0], proc.returncode, reason, start, end, model=model, effort=effort,
                             detail=detail, fault_at=fault_at, resets_at=api["resets_at"])

    def sandbox_cleanup(self) -> None:
        """--sandbox container: the play process's container (a SIGKILL to the docker CLI doesn't reach it) and
        its http server. Also at the runner's end, for a session an exception left."""
        live, self._sandbox_live = getattr(self, "_sandbox_live", None), None
        if live:
            server, cname = live
            if cname:
                sandbox.Sandbox.remove(cname)
            server.stop()

    def fault_detail(self, health: HealthTracker) -> str:
        if self.game_proc is not None and self.game_proc.poll() is not None:
            return f"game process exited (status {self.game_proc.returncode})"
        if not port_open():
            return f"debug port {DEBUG_PORT} stopped answering" + (" (game process still alive)" if self.game_proc else "")
        return f"port {DEBUG_PORT} open but {health.limit} liveness evals in a row failed or timed out"

    def write_latency(self, session: str, base: Path, start: float, end: float) -> dict | None:
        """Sxxx.latency.json + a line in latency_history.jsonl (bridge records + stream timing)."""
        try:
            summary = latency.summarize_files(Path(f"{base}.metrics.jsonl"), Path(f"{base}.turns.jsonl"), start, end)
            latency.write_summary(summary, Path(f"{base}.latency.json"), self.state.dir / "latency_history.jsonl", session)
        except Exception as e:  # noqa: BLE001 - metrics never break the runner
            log(f"{session}: latency summary failed: {e}")
            return None
        t = summary["targets"]
        verdict = " ".join(f"{k}={'PASS' if v['pass'] else 'FAIL' if v['pass'] is False else 'n/a'}" for k, v in t.items())
        log(f"{session}: latency idle median {summary['idle_s']['all'].get('median')}s, "
            f"{summary['long_gaps']['total']} gaps >10s, think median {summary['think_s'].get('median')}s "
            f"(API {(summary.get('api') or {}).get('api_s_per_turn')}s/turn, "
            f"{(summary.get('api') or {}).get('ms_per_out_tok')} ms/out-tok), "
            f"apm {summary['apm']}, calls/min {(summary.get('rates') or {}).get('calls_pm')} "
            f"(reads {(summary.get('rates') or {}).get('reads_pm')}, actions {(summary.get('rates') or {}).get('actions_pm')}), "
            f"busy {summary['busy_pct']}%, pipelined {summary.get('pipelining_pct')}%, "
            f"think overlap {summary.get('think_overlap_pct')}%, loading {summary.get('loading_s')}s (not idle), "
            f"wait_for noop {(summary.get('wait_for_noop') or {}).get('n')}/{(summary.get('wait_for_noop') or {}).get('of')}, "
            + (f"wrap-up {summary['wrapup_s']}s (not counted)" if summary.get('wrapup_s') is not None
               else "no wrap-up phase") + f" | {verdict}")
        return summary

    def write_triage(self, session: str, base: Path) -> Path | None:
        """Sxxx.triage.md: failed calls by tool + error, long gaps, ToolSearch, clicks, stuck stretches, summary."""
        try:
            path = triage.write(base)
        except Exception as e:  # noqa: BLE001 - triage never breaks the runner
            log(f"{session}: triage failed: {e}")
            return None
        log(f"{session}: triage {path}")
        self.last_triage = (session, path)
        return path

    def between_sessions_check(self) -> dict | None:
        """tools/dev/check_all.py after the mod hot reload, before the next kickoff (no claude is in play/ then;
        our own runner.lock is waved through by BG3AI_CHECK_ALL_RUNNER_PID). At most every CHECK_ALL_INTERVAL.
        The table goes to the finished session's triage report, the counts to the log and runner.status.json
        (dashboard). A FAIL never stops play; nothing here raises."""
        now = time.time()
        if not check_all_due(self.state["check_all_at"], now) or self.stop_requested or (RUNTIME / "STOP").exists():
            return None
        json_path = RUNTIME / "check_all.json"
        json_path.unlink(missing_ok=True)
        env = dict(os.environ, BG3AI_CHECK_ALL_RUNNER_PID=str(os.getpid()))
        t0 = time.time()
        try:
            p = subprocess.run(check_all_cmd(json_path), capture_output=True, text=True, cwd=REPO, env=env,
                               timeout=CHECK_ALL_TIMEOUT)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            rc, out = None, ""
        except OSError as e:
            rc, out = -1, str(e)
        secs = time.time() - t0
        try:
            report = json.loads(json_path.read_text())
        except (OSError, ValueError):
            report = None
        res = check_all_result(rc, out, report)
        session, triage_path = self.last_triage or (None, None)
        log(res["line"] + (f" (after {session}, {secs:.0f} s)" if session else f" ({secs:.0f} s)"))
        if res["ok"]:
            self.state["check_all_at"] = now
        bad = [f"{r.get('name')}: {r.get('detail')}"[:200] for r in res["rows"] if r.get("verdict") == "FAIL"]
        for b in bad:
            log(f"check_all FAIL {b}")
        # S068 started on screen=map (the waypoints check's map was still up, the agent pressed ESCAPE): check_all's
        # last row closes it and says what the next session would start on; no row (timed out): ask ourselves.
        scr = next((r for r in res["rows"] if r.get("name") == "screen"), None)
        if scr is None and (screen := ui_screen()) == "map":
            scr = {"verdict": "FAIL", "detail": f"screen {screen} after check_all ({res['why'] or 'no screen row'})"}
        if scr and scr.get("verdict") == "FAIL":
            log(f"PREP FAIL: the game is not on the game view after check_all: {scr.get('detail')}")
            self.pending_notes.append(f"RUNNER NOTE: the between-sessions check left the game off the game view "
                                      f"({scr.get('detail')}). Close that screen first (ESCAPE).")
        write_status(check_all={"t": now, "session": session, "ok": res["ok"], "counts": res["counts"],
                                "line": res["line"], "fails": bad[:5], "why": res["why"]})
        if triage_path is not None:
            try:
                with open(triage_path, "a") as f:
                    f.write(check_all_markdown(res, now, secs, "after the session "))
            except OSError as e:
                log(f"check_all: triage append failed: {e}")
        return res

    def progress_loop(self, session: str, base: Path, start: float, stop: threading.Event, human=None) -> None:
        """Poll story progress (fair journal/map/status data) into Sxxx.progress.jsonl until stop is set."""
        tl = story.Timeline(Path(f"{base}.progress.jsonl"), self.state.dir / "progress_state.json", start)
        actions = story.actions_in(Path(f"{base}.metrics.jsonl"))
        while not stop.wait(story.POLL_S):
            try:
                got = story.read_game(lambda code, ctx: lua_eval(code, ctx, timeout=EVAL_TIMEOUT))
            except Exception:  # noqa: BLE001 - loading screens, crashes: just a failed poll
                got = None
            try:
                was_stuck = tl.stuck_since is not None
                evs = tl.observe(got, actions_since=actions)
                stamp = datetime.now().strftime("%H:%M:%S")
                if human is not None:
                    for e in evs:
                        human.write(f"{stamp} [progress] +{e['points']} {e['kind']}: {e['what']}\n")
                    if tl.stuck_since is not None and not was_stuck:
                        human.write(f"{stamp} [progress] STUCK: no progress for {story.STUCK_MIN:.0f}+ min while active\n")
                if tl.stuck_since is not None and not was_stuck:
                    log(f"{session}: stuck signal: no story progress for {story.STUCK_MIN:.0f}+ min while active")
                hit = self.check_until(tl.seen, session)
                if hit and human is not None:
                    human.write(f"{stamp} [runner] milestone {hit['id']} ({hit['label']}) reached: "
                                f"{'the game is finished' if self.game_ended() else '--until stop'}\n")
            except Exception as e:  # noqa: BLE001 - progress never breaks a session
                log(f"{session}: progress poll failed: {e}")

    def check_until(self, seen: dict | None, session: str | None) -> dict | None:
        """run --until: the milestone, the first time milestones.status() says it's reached (else None). Every run
        also ends at milestones.GAME_END (the end credits / the epilogue): there is nothing left to play."""
        if self.until_hit:
            return None
        for mid in dict.fromkeys(m for m in (self.opt.until, milestones.GAME_END) if m):
            hit = until_reached(seen, mid)
            if hit:
                self.until_hit = dict(hit, session=session, t=round(time.time(), 1))
                end = mid == milestones.GAME_END
                log(f"{session or 'runner'}: milestone {hit['id']} ({hit['label']}) reached: stopping "
                    + ("(the game is finished)" if end else "(--until)"))
                self.event("milestone", milestone=hit["id"], session=session, **({"terminal": True} if end else {}))
                return self.until_hit
        return None

    def game_ended(self) -> bool:
        return bool(self.until_hit and self.until_hit.get("id") == milestones.GAME_END)

    # ---------------------------------------------------------------- long runs (playthrough mode)

    def write_status_line(self, state: str) -> str:
        """runtime/runner.status.txt (+ runner.status.json "status_line"). Never breaks the runner."""
        try:
            now = time.time()
            t0 = getattr(self, "run_t0", None) or now
            n = sum(1 for h in self.state["history"] or [] if h.get("kind") == "play" and (h.get("started") or 0) >= t0)
            faults = sum(1 for e in run_events(self.state.dir, max(now - CRASH_LOOP_WINDOW_S, t0 - 1))
                         if (e.get("kind") == "fault" and e.get("fault") in GAME_FAULTS) or e.get("kind") == "setup_failed")
            line = status_line(now, self.opt.playthrough, state, t0, n, read_seen(self.state.dir),
                               day_cost(self.state.dir, now), self.opt.max_cost_per_day, faults)
            (RUNTIME / STATUS_LINE).write_text(line + "\n")
            write_status(status_line=line)
            return line
        except Exception as e:  # noqa: BLE001
            log(f"status line failed: {type(e).__name__}: {e}")
            return ""

    def cost_paused(self) -> bool:
        """--max-cost-per-day: True (after waiting up to COST_PAUSE_CHECK_S) while the trailing 24 h spent the cap.
        A pause, not an end: the run goes on once old sessions leave the window. Checked between sessions, so a day
        can go over by about one session."""
        cap = self.opt.max_cost_per_day
        if not cap:
            return False
        spent = day_cost(self.state.dir, time.time())
        if spent < cap:
            if self.cost_pause_since is not None:
                log(f"cost: ${spent:.2f} in the last 24 h, under --max-cost-per-day ${cap:g}: playing again "
                    f"(paused {(time.time() - self.cost_pause_since) / 60:.0f} min)")
                self.event("cost_resume", spent=round(spent, 2), cap=cap)
                self.cost_pause_since = None
            return False
        if self.cost_pause_since is None:
            self.cost_pause_since = time.time()
            log(f"cost: ${spent:.2f} in the last 24 h >= --max-cost-per-day ${cap:g}: pausing (no session starts; "
                f"checked every {COST_PAUSE_CHECK_S / 60:g} min)")
            self.event("cost_pause", spent=round(spent, 2), cap=cap)
        self.write_status_line(f"PAUSED: ${spent:.2f} in 24 h >= --max-cost-per-day ${cap:g}")
        until = time.time() + COST_PAUSE_CHECK_S
        while time.time() < until and not (self.stop_requested or self.abort or (RUNTIME / "STOP").exists()):
            time.sleep(min(5.0, max(0.0, until - time.time())))
        return True

    def heat_killed(self) -> str | None:
        """Why ramwatch's heat guard killed the game of this launch (runtime/heat_kills.jsonl), or None."""
        launched = (self.state["game"] or {}).get("launched")
        kills = read_spikes(launched, RUNTIME / "heat_kills.jsonl") if launched else []
        return kills[-1].get("why", "too hot") if kills else None

    def heat_paused(self, after_kill: bool = False) -> bool:
        """Heat guard: while runtime/thermal.log (ramwatch, or tools/orchestrator/thermal_log.sh) says the CPU or the
        GPU stayed at the heat-pause limit for pause_s (ramwatch.heat_limits: 92 C / 85 C for 60 s), no session
        starts: waits until both stayed under it for pause_s, then True. after_kill (the heat guard killed the game):
        waits unless it is cool already. the test machine cut its power twice under the naut series load (2026-10-16 ~20:07,
        2026-10-17 ~16:01). A stale or missing log pauses nothing."""
        path = RUNTIME / "thermal.log"
        h = ramwatch.read_heat(path)
        if not (h["pause"] or (after_kill and not h["cool"])):
            return False
        lim = ramwatch.heat_limits()
        t0 = time.time()
        what = (f"CPU {h['cpu']} C / GPU {h['gpu']} C" + (f" for {h['hot_s']:.0f} s" if h["pause"] else ""))
        log(f"heat: {what}{' after the heat kill' if after_kill else ''} (pause at CPU >= {lim['pause_cpu_c']:g} C "
            f"or GPU >= {lim['pause_gpu_c']:g} C for {lim['pause_s']:g} s): pausing until both stay under it for "
            f"{lim['pause_s']:g} s")
        self.event("heat_pause", cpu_c=h["cpu"], gpu_c=h["gpu"], seconds=round(h["hot_s"]), after_kill=after_kill)
        menu = not after_kill and self.heat_menu_open()   # after a heat kill there is no game to pause
        try:
            while not (self.stop_requested or self.abort or (RUNTIME / "STOP").exists()):
                self.write_status_line(f"PAUSED: too hot (CPU {h['cpu']} C, GPU {h['gpu']} C; "
                                       f"limit {lim['pause_cpu_c']:g}/{lim['pause_gpu_c']:g} C)")
                time.sleep(HEAT_PAUSE_CHECK_S)
                h = ramwatch.read_heat(path)
                if h["cool"]:
                    break
        finally:
            if menu:
                self.heat_menu_close()
        if not h["cool"]:
            return True   # stopped while paused: the run loop sees the stop
        log(f"heat: CPU {h['cpu']} C / GPU {h['gpu']} C after {(time.time() - t0) / 60:.0f} min: playing again")
        self.event("heat_resume", cpu_c=h["cpu"], gpu_c=h["gpu"], seconds=round(time.time() - t0))
        return True

    @staticmethod
    def wait_screen(want: str, timeout: float = HEAT_MENU_WAIT_S) -> str | None:
        """ui.state's screen, polled until it is want or timeout passed; the last screen seen."""
        end = time.monotonic() + timeout
        while True:
            screen = ui_screen()
            if screen == want or time.monotonic() >= end:
                return screen
            time.sleep(0.15)

    def heat_menu_open(self) -> bool:
        """Heat pause: the ESC game menu over the running game, as a player pauses (the world stops and our
        tools idle under it), so it cools. Only from the plain game view: in a dialogue, a trade, a movie (ESCAPE
        skips it), on a loading screen or with a panel open an ESCAPE does something else, so those are skipped
        with a log line. True when it opened the menu (heat_menu_close closes it again). Never raises."""
        try:
            st = ui_state()
            if st is None:
                log("heat: game menu not opened (the game's UI doesn't answer)")
                return False
            screen, panels = st.get("screen") or "game", st.get("panels_open") or []
            if screen != "game" or panels:
                log(f"heat: game menu not opened: the {screen} screen{' with ' + ', '.join(panels) if panels else ''}"
                    " is up (an ESCAPE there does something else); the game keeps rendering")
                return False
            rpc("ui.key", {"key": "ESCAPE"}, context="client")
            got = self.wait_screen("game_menu")
            if got != "game_menu":
                log(f"heat: ESCAPE did not open the game menu (screen {got}); the game keeps rendering")
                return False
            log("heat: game menu open (ESCAPE) while it cools")
            return True
        except Exception as e:  # noqa: BLE001 - the pause still holds the next session
            log(f"heat: game menu not opened: {e!r}")
            return False

    def heat_menu_close(self) -> None:
        """Closes the game menu heat_menu_open opened: its Resume button (a second ESCAPE left it open 3/3 live,
        bridge server._press_escape), ESCAPE if it is still up. Never raises."""
        try:
            screen = ui_screen()
            if screen != "game_menu":
                log(f"heat: the game menu is already closed (screen {screen})")
                return
            rpc("ui.press", {"button": "Resume", "widget": "GameMenu"}, context="client")
            if (got := self.wait_screen("game")) == "game_menu":
                rpc("ui.key", {"key": "ESCAPE"}, context="client")
                got = self.wait_screen("game")
            log("heat: game menu closed" if got != "game_menu" else "heat: the game menu is STILL OPEN after Resume "
                "and ESCAPE (the next session's agent sees it)")
        except Exception as e:  # noqa: BLE001
            log(f"heat: game menu not closed: {e!r}")

    def caps(self) -> dict:
        """The run's caps as recorded (runs.CAP_KEYS)."""
        return {"run_hours": self.opt.run_hours, "max_cost": self.opt.max_cost, "max_tokens": self.opt.max_tokens}

    def has_usage_caps(self) -> bool:
        return bool(self.opt.until and (self.opt.max_cost is not None or self.opt.max_tokens is not None))

    def check_usage_caps(self, cost: float, tokens: int) -> bool:
        """--max-cost / --max-tokens: True (and cap_hit set, an event logged) once the run reached one."""
        if self.cap_hit:
            return True
        if not self.has_usage_caps():
            return False
        cap = runs.cap_exceeded(self.caps(), cost, tokens)
        if not cap:
            return False
        limit = f"--max-cost ${self.opt.max_cost:g}" if cap == "cost" else f"--max-tokens {self.opt.max_tokens}"
        why = f"{cap} cap used up: ${cost:.2f}, {tokens} tokens ({limit})"
        self.cap_hit = {"cap": cap, "cost": round(cost, 4), "tokens": tokens, "why": why}
        log(why + ": the run ends")
        self.event("cap", cap=cap, cost=round(cost, 4), tokens=tokens)
        return True

    def runs_path(self) -> Path:
        """runs.jsonl of this run: the series' (runtime/sessions/<base>/) for --runs, else the playthrough's."""
        return RUNTIME / "sessions" / self.opt.series / "runs.jsonl" if self.opt.series else self.state.dir / "runs.jsonl"

    def write_run_record(self, run_started: float, outcome: str = "reached", why: str = "") -> dict:
        """runs.jsonl: one line per --until run (the benchmark's raw score), whatever the outcome, with the version
        pins the run started with and its caps. A --runs run also gets its knowledge/ edits put back (FAIR-2) and
        noted in the record."""
        cap = (self.cap_hit or {}).get("cap") if outcome == "over_budget" else None
        rec = run_record(self.state.dir, run_started, time.time(), self.until_hit or {}, self.opt.playthrough,
                         self.state["history"] or [], outcome, why, self.opt.take_over, cap)
        play = self.model_for()[0]
        rec.update(model=play, effort=self.model_for()[1],
                   keeper_model=keeper_model(self.opt.keeper_model, play) if self.opt.keeper else None)
        cur = self.state["run"] or {}
        rec.update(caps=self.caps(), pins=cur.get("pins") or self.opt.pins,
                   version_drift=cur.get("version_drift", self.opt.version_drift) or None,
                   cap_changes=self.opt.cap_changes or None)
        if self.opt.scenario:
            rec["scenario"] = self.opt.scenario
        rec["model_time"] = runs.model_time_field(model_time.model_time(self.state.dir, since=run_started,
                                                                         until=rec["t"]))
        if self.opt.judge_change:
            rec["judge_change"] = self.opt.judge_change
        rec["client"] = self.opt.client
        if getattr(self, "box", None) is not None:
            rec.update(self.box.record())   # sandbox=container (+ sandbox_unfirewalled)
        if self.opt.client_change:
            rec["client_change"] = self.opt.client_change
        if self.opt.sandbox_change:
            rec["sandbox_change"] = self.opt.sandbox_change
        if self.opt.series:
            rec = {"series": self.opt.series, "run": self.opt.run_index, **rec}
            run = self.state["run"] or {}
            if run.get("reset"):
                rec["reset"] = run["reset"]
            k = self.restore_knowledge(run_started)
            if k is not None:
                rec["knowledge"] = k
                # FAIR-2: knowledge/ is shared by every run; one that changed during the run (put back or not)
                # isn't comparable with the others in its series
                if k.get("agent_edits") or k.get("other_changes"):
                    rec["clean"] = False
                    rec["knowledge_changed"] = True
        path = self.runs_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log(f"run record write failed: {e}")
        self.state["run"] = None   # this run is over; a later --until run of the playthrough starts its own
        log(f"run record: {outcome} ({rec['milestone'] or why or '-'}) in {rec['wall_min']} min, {rec['sessions']} "
            f"session(s), ${rec['cost_usd']:.2f}, {rec['crashes']} game crash(es)/hang(s), {rec['recoveries']} "
            f"recoveries, {rec['reloads']} agent reload(s); failures harness {rec['harness_failures']} / agent "
            f"{rec['agent_failures']} / unknown {rec['unknown_failures']}; clean={rec['clean']} → {path}")
        if self.opt.judge:   # last: it costs time and money, and the record above is already safe on disk
            self.judge_run_record(path, rec)
        return rec

    def judge_run_record(self, path: Path, rec: dict) -> None:
        """--judge: the persona judge on the run just recorded, merged into its runs.jsonl line. Never raises."""
        model = self.opt.judge_model or judge.DEFAULT_MODEL
        log(f"persona judge ({model}) on run {rec.get('run') or 1} ({rec['playthrough']}) ...")
        rec["persona"] = judge_record(self.state.dir, rec, model, stop=lambda: self.abort)
        if not runs.update_record(path, rec, {"persona": rec["persona"]}):
            log(f"persona: could not update the record in {path} (play.py judge --playthrough "
                f"{self.opt.series or self.opt.playthrough} redoes it)")
        log("persona: " + persona_line(rec["persona"]))

    # ---------------------------------------------------------------- benchmark runs (--until / --runs)

    def begin_run(self, now: float) -> float:
        """--until: the run's start, kept in runner.json "run" across runner restarts until its record is written.
        A --runs run on a fresh playthrough first gets reset_for_run()."""
        cur = self.state["run"]
        if cur and cur.get("started"):
            return cur["started"]
        rec = {"started": now, "series": self.opt.series, "index": self.opt.run_index, "pins": self.opt.pins,
               "version_drift": self.opt.version_drift}
        if self.opt.series and not self.state["history"] and not self.opt.dry_run:
            rec["reset"] = self.reset_for_run(now)
        if not self.opt.dry_run:
            self.state["run"] = rec
        return now

    def reset_for_run(self, now: float) -> dict:
        """Before a --runs run's first session: nothing a new playthrough would inherit from an earlier one.
        - saves: only saves written from now on are this playthrough's (saves_after; runner and MCP server)
        - the mod's navigation kill list and breadcrumb mirrors (Script Extender/BG3AI/nav_*.json) are moved
          into runtime/sessions/<pt>/reset/ (NAV-3)
        - play/knowledge is snapshotted; the agent's edits are put back after the run (FAIR-2)
        The game process itself is restarted between runs (end_run_game)."""
        self.state["saves_after"] = now
        moved = runs.archive_nav_state(SE_BG3AI_DIR, self.state.dir / "reset")
        snap = runs.snapshot_tree(KNOWLEDGE_DIR)
        try:
            (self.state.dir / "knowledge_snapshot.json").write_text(json.dumps(snap))
        except OSError as e:
            log(f"knowledge snapshot failed: {e}")
        log(f"run {self.opt.run_index} of {self.opt.series}: fresh start for {self.opt.playthrough} (saves from "
            f"{datetime.fromtimestamp(now):%H:%M:%S} on; nav state moved aside: {', '.join(moved) or 'none'}; "
            f"knowledge/ snapshot of {len(snap)} files)")
        self.event("run_reset", nav_files=moved, knowledge_files=len(snap))
        return {"nav_files": moved, "knowledge_files": len(snap)}

    def restore_knowledge(self, run_started: float) -> dict | None:
        """FAIR-2: knowledge/ files the agent edited during the run go back to the snapshot (its versions are kept
        in runtime/sessions/<pt>/knowledge_edits/); other changes are only reported. None: no snapshot."""
        try:
            snap = json.loads((self.state.dir / "knowledge_snapshot.json").read_text())
        except (OSError, ValueError):
            return None
        streams = []
        for f in sorted(self.state.dir.glob("S*.jsonl")):
            try:
                if runs.CLAUDE_STREAM_RE.match(f.name) and f.stat().st_mtime >= run_started:
                    streams.append(f)
            except OSError:
                continue
        res = runs.restore_agent_edits(KNOWLEDGE_DIR, snap, runs.agent_written(streams, KNOWLEDGE_DIR, PLAY_DIR),
                                       self.state.dir / "knowledge_edits")
        if res["agent_edits"] or res["other_changes"]:
            log(f"knowledge/ after the run: agent edits put back: {', '.join(res['agent_edits']) or 'none'}; "
                f"other changes (not reverted): {', '.join(res['other_changes']) or 'none'}")
        return res

    def end_run_game(self, when: str = "between runs", after: str = "the next run launches a new one") -> None:
        """--runs, between runs: stop the game the run played, so the next run starts a new game in a fresh process
        (the mod's Lua state, the kill list in memory included). Also at the end of a series or an --until run
        (when="series end" / "run end"; --keep-game skips that): naut2 left its last run's game running
        (bg3ai-game-20261017-133954.scope). A game the runner doesn't own is left alone."""
        if not port_open() and not self.owns_game():
            return
        if not (self.owns_game() or self.opt.take_over):
            log(f"{when}: the game up is not one this runner launched; left running"
                + (" (the next run needs the main menu)" if when == "between runs" else ""))
            return
        try:
            self.kill_game()
            log(f"{when}: game stopped; {after}")
        except Exception as e:  # noqa: BLE001 - the next run's setup retries (and records it)
            log(f"{when}: stopping the game failed: {e}")

    def write_progress(self, session: str, base: Path, start: float, end: float) -> dict | None:
        """Sxxx.progress.json + a line in progress_history.jsonl."""
        try:
            summary = story.summarize(story.load(Path(f"{base}.progress.jsonl")), start, end)
            story.write_summary(summary, Path(f"{base}.progress.json"), self.state.dir / "progress_history.jsonl", session)
        except Exception as e:  # noqa: BLE001
            log(f"{session}: progress summary failed: {e}")
            return None
        log(f"{session}: story progress {summary['points']} pts ({summary['per_hour']}/h), {summary['events']} events, "
            f"longest stretch without progress {summary['longest_no_progress_min']} min, {summary['stuck_episodes']} stuck episode(s)")
        return summary

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    # ---------------------------------------------------------------- memory session

    def session_py(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SESSION_PY), *args, "--memory-dir", str(self.mem)],
                              capture_output=True, text=True, cwd=PLAY_DIR, stdin=subprocess.DEVNULL)

    def session_env(self, deadline: float, minutes: float, sid: str | None = None) -> dict:
        """The clock env session.py sees for a session (what run_claude gives claude and the MCP server)."""
        env = dict(os.environ, BG3AI_SESSION_DEADLINE=str(int(deadline)),
                   BG3AI_SESSION_WRAPUP=str(int(deadline - wrapup_minutes(minutes) * 60)),
                   BG3_SESSION_MINUTES=str(minutes), BG3_WRAP_MINUTES=str(wrap_minutes(minutes)),
                   BG3AI_KEEPER="1" if self.opt.keeper and not self.opt.dry_run else "0")
        if sid and getattr(self, "state", None) is not None:   # the playthrough state comes with run()
            env["BG3AI_FIGHT_HOLD_PATH"] = str(fight_hold_path(self.state.dir, sid))
        return env

    def session_cmd(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        """session.py <args> with the session's clock env; never reads the runner's stdin."""
        return subprocess.run([sys.executable, str(SESSION_PY), *args, "--memory-dir", str(self.mem)],
                              capture_output=True, text=True, cwd=PLAY_DIR, env=env, stdin=subprocess.DEVNULL)

    def open_memory_session(self, deadline: float, minutes: float) -> tuple[str | None, str]:
        """session.py start (was the SessionStart hook): opens sessions/Sxxx.md. Returns (sid, the memory briefing for
        the kickoff), or (None, "") when it couldn't (the agent then calls session_start)."""
        try:
            r = self.session_cmd(["start"], self.session_env(deadline, minutes))
            st = self.memory_state()
        except (OSError, ValueError) as e:
            log(f"memory session not opened: {e}")
            return None, ""
        if r.returncode != 0 or not st.get("open") or not st.get("current"):
            log(f"memory session not opened (session.py start exit {r.returncode}): {(r.stdout or r.stderr)[-300:]}")
            return None, ""
        return session_id(st["current"]), r.stdout.strip()

    def stop_check(self, sid: str, deadline: float, minutes: float) -> str | None:
        """session.py stop after claude ended its turn (was the Stop hook): the reason the session can't end yet
        (keep playing, write the handoff, fix lint), or None (it ended; on success the memory is committed)."""
        try:
            r = self.session_cmd(["stop"], self.session_env(deadline, minutes, sid))
            out = json.loads(r.stdout) if r.stdout.strip() else {}
        except (OSError, ValueError) as e:
            log(f"{sid}: stop check failed: {e}")
            return None
        if out.get("systemMessage"):
            log(f"{sid}: stop check: {out['systemMessage']}")
        return out.get("reason") if out.get("decision") == "block" else None

    def close_memory_session(self, res: SessionResult) -> None:
        st = self.memory_state()
        if not st.get("open"):
            log(f"{res.sid}: memory already committed (end_session or the stop check)")
            return
        r = self.session_py("end")
        if r.returncode == 0:
            log(f"{res.sid}: memory committed by session.py end")
            return
        if until_stop_handoff_only(res.reason, bool(self.opt.until or self.until_hit), r.stdout):
            # The --until stop ends the session at the milestone, before any wrap-up, so HANDOFF.md is still the
            # previous session's: that alone is no notes problem (naut/naut2: a 14 s fix pass + the wait every run).
            # The keeper rewrites HANDOFF; a later session start reconciles the open session.
            log(f"{res.sid}: memory lint: only the handoff check failed (the --until stop came before the wrap-up): "
                "no fix-notes session; memory left open (the keeper writes the handoff)")
            self.event("fix_notes_skipped", session=res.sid, why="until_milestone_handoff_only")
            return
        if (res.reason in ("exited", "hard_stop", "milestone") and res.claude_session
                and res.ended - res.started >= 60 and not (self.stop_requested or self.abort)):   # RUN-7: <1 min: no
            log(f"{res.sid}: memory lint failed; running a {FIX_SESSION_MINUTES}-minute fix-notes session")
            self.run_claude(res.sid, fix_notes_prompt(r.stdout), FIX_SESSION_MINUTES, res.claude_session,
                            resume=True, tag="-fix", needs_game=False)
        if not self.memory_state().get("open"):
            return
        r = self.session_py("end")
        if r.returncode == 0:
            log(f"{res.sid}: memory committed after the fix-notes session")
        elif res.reason in GAME_FAULTS + ("killed", "aborted"):
            # The game died before the agent's wrap-up, so the end lint's HANDOFF check ("written by the current
            # session") can't pass and no fix pass runs. session.py start reconciles the open session (S014: the
            # note below told the next session to rewrite HANDOFF as the crashed one). Pass on only other errors.
            other = memory_lint_errors(r.stdout, skip=("handoff",))
            why = f"a game {res.reason}" if res.reason in GAME_FAULTS else f"claude {res.reason}"
            log(f"{res.sid}: ended by {why} before wrap-up; memory left open for the next session"
                + (f" ({len(other)} other lint error(s))" if other else ""))
            if other:
                self.pending_notes.append(
                    f"RUNNER NOTE: {res.sid} ended by {why} and its memory is uncommitted. Besides "
                    "the missing handoff (the session ended before wrap-up), fix these lint errors early:\n"
                    + "\n".join(other)[-2500:])
        else:
            log(f"{res.sid}: memory still fails lint; left open for the next session to reconcile")
            self.pending_notes.append(
                f"RUNNER NOTE: memory lint still failed after {res.sid}; the session was left uncommitted. "
                "Fix these lint errors early in this session:\n" + r.stdout[-2500:])

    def check_plan_lines(self, when: str, dry: bool = False) -> list[dict]:
        """tools/dev/check_plans.py (reads only, ~3 s): the stored plan lines for each member's next 2 levels vs the
        game's Progression offers. Logs one line; returns the invalid lines (keeper refused-line format). Needs the
        game Running with the party loaded and no bridge in use (between sessions). Never raises."""
        if (self.opt.dry_run and not dry) or not port_open():
            return []
        try:
            if game_state() != "Running":
                return []
            json_path = RUNTIME / "check_plans.json"
            json_path.unlink(missing_ok=True)
            env = dict(os.environ, BG3AI_PLAYTHROUGH=self.opt.playthrough)
            p = subprocess.run(check_plans_cmd(json_path, self.mem), capture_output=True, text=True, cwd=REPO,
                               env=env, timeout=CHECK_PLANS_TIMEOUT)
            rows = json.loads(json_path.read_text())
        except Exception as e:  # noqa: BLE001 - a probe failure never stops play
            tail = (getattr(locals().get("p"), "stderr", "") or "")[-300:]
            log(f"plan check ({when}) skipped: {type(e).__name__}: {e} {tail}".rstrip())
            return []
        text, bad = plan_check_result(rows)
        log(f"{when}: {text}")
        self.event("plan_check", when=when, checked=len(rows), invalid=[f"{b['name']} L{b['level']}" for b in bad])
        return bad

    # ---------------------------------------------------------------- memory keeper

    def start_keeper(self, sid: str, res: SessionResult, plan_bad: list[dict] | None = None) -> None:
        """Starts the memory-keeper pass for the session that just ended (runner/memory_keeper.py). It runs in a
        thread while the runner prepares the next session (game check, mod reload, settings, check_all: none of
        them write memory) and is joined before the next player starts, so it never races the player's edits."""
        if not self.opt.keeper or self.opt.dry_run or res.ended - res.started < 120 or getattr(self, "cap_hit", None):
            if plan_bad:   # no keeper pass to fix them: the next player gets the note
                self.pending_notes.append(plan_note(plan_bad))
            if not self.opt.dry_run:
                self.write_review(sid)
            return
        import memory_keeper as K
        n = int(sid[1:])
        base = self.state.dir / f"{sid}-keeper"
        model = keeper_model(self.opt.keeper_model, res.model or self.opt.model)
        effort, minutes = self.opt.keeper_effort, self.opt.keeper_minutes
        client = self.opt.client

        def keep():
            log(f"{sid}: memory keeper starting ({client} {model}, effort {effort or 'default'}, ≤{minutes:g} min)")
            try:   # stored level-up plan lines the game refused this session: the keeper rewrites them
                refused = K.refused_plans(read_actions_since(res.started))
            except Exception as e:  # noqa: BLE001 - never stops the keeper
                refused = []
                log(f"{sid}: refused plan lines not read: {e!r}")
            seen = {(x["name"].lower(), x["level"]) for x in refused}
            refused += [b for b in plan_bad or [] if (b["name"].lower(), b["level"]) not in seen]
            if refused:
                log(f"{sid}: refused plan lines for the keeper: {', '.join(x['name'] + ' L' + str(x['level']) for x in refused)}")
            # the play client keeps the memory too (a benchmark compares one CLI end to end); claude stays on its
            # default pass with Claude Code's file tools, the others go through the bg3 server's keeper mode
            r = K.run_keeper(self.mem, n, base, model, effort, minutes, refused=refused, client=client,
                             **({"sandbox": box} if (box := getattr(self, "box", None)) else {}))
            log(r.summary())
            self.event("memory_keeper", session=sid, status=r.status, seconds=r.seconds, files=r.files,
                       commit=r.commit, timed_out=r.timed_out, reverted=r.reverted, lint_errors=r.lint_errors[:5],
                       model=model, client=client)
            try:
                with open(self.state.dir / "keeper_history.jsonl", "a") as f:
                    f.write(json.dumps({"t": round(time.time(), 1), "client": client, "model": model, "effort": effort,
                                        **r.__dict__}) + "\n")
            except OSError as e:
                log(f"keeper history write failed: {e}")

        def work():
            try:
                keep()
            finally:   # after the keeper: the review reads its memory_keeper event
                self.write_review(sid)

        self.keeper_thread = threading.Thread(target=work, name="memory-keeper", daemon=True)
        self.keeper_thread.start()

    def write_review(self, sid: str) -> Path | None:
        """Sxxx.review.md (tools/logs/review_session.py) for the logs worker, once the keeper pass is done (its
        memory/keeper section and the session's runner events are in by then; tools/logs/wait_new_session.sh wakes
        on it). Covers the session and its resumes. Niced, with a timeout; nothing here raises."""
        cmd = ["nice", "-n", "15", sys.executable, str(REVIEW_PY), self.state.dir.name, sid,
               "--runtime", str(RUNTIME), "--sessions", str(self.state.dir.parent)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, timeout=REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            log(f"{sid}: review killed after {REVIEW_TIMEOUT} s")
            return None
        except OSError as e:
            log(f"{sid}: review failed: {e}")
            return None
        if p.returncode != 0:
            log(f"{sid}: review failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")
            return None
        path = self.state.dir / f"{sid}.review.md"
        log(f"{sid}: review {path}")
        return path

    def recover_keeper(self) -> None:
        """MEM-1: a keeper pass the runner died in (killed, crashed, restarted mid-pass) left its pre-pass snapshot:
        its process is killed and its unvalidated edits put back before any session builds on them."""
        if self.opt.dry_run:
            return
        import memory_keeper as K
        msg = K.recover(self.mem, self.state.dir)
        if msg:
            log(f"memory keeper: {msg}")
            self.event("keeper_recovered", detail=msg)

    def join_keeper(self) -> None:
        t, self.keeper_thread = getattr(self, "keeper_thread", None), None
        if t is None:
            return
        if t.is_alive():
            t0 = time.time()
            t.join(self.opt.keeper_minutes * 60 + 60)  # run_keeper kills claude at its own deadline
            log(f"waited {time.time() - t0:.0f} s for the memory keeper before the next session")
        # A session left open by failed lint (e.g. stale objectives) may pass now that the keeper updated the files.
        if self.memory_state().get("open"):
            r = self.session_py("end")
            if r.returncode == 0:
                log("memory session committed after the keeper pass")

    # ---------------------------------------------------------------- one session

    def plan_dry_run(self, minutes: float) -> SessionResult:
        """Print what the next session would do; touches neither the game nor the memory."""
        save = self.choose_save()
        sid = self.next_sid()
        print(f"session:        {sid} ({minutes:.0f} min)")
        print(f"memory:         {self.mem}")
        print(f"save to load:   {save.folder if save else '(none: new game from the main menu)'}"
              + (f" ({self.save_choice[1]})" if save and self.save_choice else ""))
        print(f"game:           {'up, state ' + str(game_state()) if port_open() else 'not running'}")
        if save and self.opt.take_over and port_open():
            touched = self.foreign_use(save)
            print(f"take-over:      {'reload ' + save.label + ' (' + touched + ')' if touched else 'play on from the running game'}")
        print(f"stuck:          {is_stuck(self.state['history'])}")
        k = self.opt
        print(f"memory keeper:  {'on (' + keeper_model(k.keeper_model, self.model_for()[0]) + ', effort ' + str(k.keeper_effort) + ', ≤' + format(k.keeper_minutes, 'g') + ' min) after each session' if k.keeper else 'OFF (--no-keeper)'}")
        print("plan check:     tools/dev/check_plans.py at each session end (→ keeper) and in the prep (→ RUNNER NOTE); now:")
        self.check_plan_lines("dry run", dry=True)
        prompt = build_kickoff(sid, self.opt.playthrough, save.label if save else None, list(self.pending_notes),
                               save_why=self.loaded_why(save.label if save else None))
        return self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))

    def play_session(self, minutes: float, first: bool) -> SessionResult:
        if self.opt.dry_run:
            return self.plan_dry_run(minutes)
        relaunched = self.maybe_relaunch(first, minutes)
        loaded = self.ensure_game(first)
        if self.opt.start_save and not any(h.get("kind") == "play" for h in self.state["history"] or []):
            self.pending_notes.append(scenario_start_note(self.opt.scenario, self.profile, loaded))
        if relaunched:
            self.pending_notes.append(
                f"RUNNER NOTE: the runner relaunched the game before this session ({relaunched}) and loaded "
                f"\"{loaded or 'the newest save'}\". long_rest / region fast travel ready checks should work again.")
        notes = list(self.pending_notes)
        self.pending_notes.clear()

        screen = ui_screen()
        at_game_over = screen == "game_over"
        last = (self.state["history"] or [{}])[-1]
        if at_game_over and last.get("started_at_game_over"):
            log("second session in a row starting at Game Over: the runner loads the latest save")
            self.kill_game()
            loaded = self.load_save(self.choose_save())
            at_game_over = False
        elif at_game_over:
            notes.append("RUNNER NOTE: the game is on the Game Over screen. Load your most recent save "
                         "(load_game) and log the reload with its cause.")
        if is_stuck(self.state["history"]):
            notes.append(stuck_directive(self.state["history"]))

        try:
            self.hot_reload_mod()  # before the settings: a reset wipes the mod's client-side config
        except RuntimeError:
            raise  # the mod is gone: let session setup recover (restart the game)
        except Exception as e:  # noqa: BLE001
            log(f"hot reload failed: {e}")
        try:
            self.apply_play_settings()
        except Exception as e:  # noqa: BLE001 - never fail a session over settings
            log(f"play settings failed: {e}")
        try:
            self.between_sessions_check()  # after the reload: checks the Lua the next session plays with
        except Exception as e:  # noqa: BLE001 - checks never stop play
            log(f"check_all failed: {e}")
        self.join_keeper()  # the keeper must be done (and committed) before the player touches memory
        bad = self.check_plan_lines("prep")  # after the keeper: what is still invalid goes to the player
        if bad:
            notes.append(plan_note(bad))
        notes += self.pending_notes
        self.pending_notes.clear()
        predicted = self.next_sid()
        start_sig = read_progress()
        deadline = time.time() + minutes * 60
        opened, briefing = self.open_memory_session(deadline, minutes)   # was the SessionStart hook
        sid = opened or predicted
        if opened and opened != predicted:
            log(f"session.py start opened {opened} (predicted {predicted})")
        prompt = build_kickoff(sid, self.opt.playthrough, loaded, notes, briefing, self.loaded_why(loaded))
        res = self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))
        if res.reason == "dry_run":
            return res
        session_start, session_end = res.started, res.started + minutes * 60
        if res.reason not in FAULTS and self.state["oom_streak"]:
            self.state["oom_streak"] = None   # a whole session from that save without dying: not an OOM loop

        # Game crash or hang mid-session: relaunch, load the latest save, and resume the SAME claude conversation
        # for the time left (an hour-long session that crashes at minute 10 keeps its context and its 50 min).
        # Agent hang (claude silent too long): the game is fine, so only claude is restarted (resumed).
        # Claude ended its turn on its own: the stop check (session.py stop, which was the Stop hook) may send it back
        # (keep playing until wrap-up, write the handoff, fix lint) by resuming the conversation with the reason.
        resumes = stop_resumes = 0
        gave_up: LaunchGaveUp | None = None
        planned_end = session_end
        while True:
            # a fight on at WRAP UP held the clock: the stop check, resumes and the hard-stop test use the moved end
            session_end = held_deadline(fight_hold_path(self.state.dir, sid), planned_end)
            if res.reason not in FAULTS:
                if (res.reason != "exited" or res.exit_code != 0 or stop_resumes >= MAX_STOP_RESUMES or self.abort
                        or time.time() > session_end + HARD_STOP_GRACE - 60):
                    break
                why = self.stop_check(sid, session_end, minutes)
                if not why:
                    break
                stop_resumes += 1
                left = (session_end - time.time()) / 60
                log(f"{sid}: claude ended its turn but the session can't end yet ({left:.0f} min left); resuming it "
                    f"(-c{stop_resumes}): {why[:160]}")
                res = self.run_claude(sid, stop_resume_prompt(why), left, res.claude_session, resume=True,
                                      tag=f"-c{stop_resumes}")
                continue
            fault, fault_at = res.reason, res.fault_at or res.ended
            game_ok = fault == "agent_hang" and self.game_alive() and game_state(HEALTH_EVAL_TIMEOUT) is not None
            why = {"crash": "crashed", "hang": "stopped responding"}.get(fault, "was unreachable after the agent hang")
            how = "claude restarted, game untouched"
            if not game_ok:
                try:
                    self.recover(why)
                except LaunchGaveUp as e:
                    self.event("recovery_failed", fault=fault, session=sid, error=str(e)[:300])
                    gave_up = e
                    break
                except SETUP_ERRORS as e:
                    log(f"recovery after {sid} failed: {e}; the next session setup retries")
                    self.event("recovery_failed", fault=fault, session=sid, error=str(e)[:300])
                    break
                how = f"game relaunched, loaded {self.state['loaded_save'] or 'main menu'}"
            left = (session_end - time.time()) / 60
            if (resumes >= MAX_RESUMES or left < RESUME_MIN_MINUTES or self.abort or self.stop_requested
                    or (RUNTIME / "STOP").exists()):
                log(f"{sid}: not resuming after the {res.reason} ({left:.0f} min left, {resumes} resume(s) so far); "
                    "the next session picks up")
                self.event("recovered", fault=fault, session=sid, seconds=round(time.time() - fault_at),
                           how=how + "; session ended, the next one picks up")
                break
            resumes += 1
            notes = list(self.pending_notes)
            self.pending_notes.clear()
            log(f"{sid}: resuming claude after the {res.reason} ({left:.0f} min left, resume {resumes}; "
                f"{time.time() - fault_at:.0f} s after the fault)")
            self.event("recovered", fault=fault, session=sid, seconds=round(time.time() - fault_at),
                       how=how + f"; claude resumed (-r{resumes})")
            prompt = agent_resume_prompt(notes) if game_ok else resume_prompt(why, notes)
            res = self.run_claude(sid, prompt, left, res.claude_session, resume=True, tag=f"-r{resumes}")
        crashed = res.reason in GAME_FAULTS
        res = replace(res, started=session_start)
        if not crashed:
            self.ensure_save(res.started)
        try:   # was the SessionEnd hook: commit a finished wrap-up, else log that the session ended before it
            self.session_cmd(["session-end"], self.session_env(session_end, minutes, sid))
        except OSError as e:
            log(f"{sid}: session-end failed: {e}")
        self.close_memory_session(res)
        plan_bad = [] if crashed else self.check_plan_lines(f"{sid} end")   # the keeper rewrites invalid lines
        self.start_keeper(sid, res, plan_bad)
        try:
            if got := self.record_agent_saves(res.started):
                log(f"{sid}: the agent's quicksave(s) {', '.join(got)} are this playthrough's saves too")
        except Exception as e:  # noqa: BLE001 - bookkeeping, never ends the session handling
            log(f"{sid}: agent saves not recorded: {e!r}")
        end_sig = read_progress()
        entry = {
            "kind": "play", "session": sid, "claude_session": res.claude_session, "reason": res.reason,
            "exit": res.exit_code, "started": res.started, "ended": res.ended,
            "model": res.model, "effort": res.effort,
            "keeper_model": keeper_model(self.opt.keeper_model, res.model or self.opt.model) if self.opt.keeper else None,
            "stop_resumes": stop_resumes,
            "minutes": round((res.ended - res.started) / 60, 1), "start_sig": start_sig, "end_sig": end_sig,
            "progress": None, "progress_why": "crashed",
            "started_at_game_over": screen == "game_over", "resumes": resumes,
            "story_points": story_points(self.state.dir / f"{sid}.progress.json"),
        }
        if not crashed:
            entry["progress"], entry["progress_why"] = session_progress(start_sig, end_sig, entry["story_points"])
        log(f"{sid}: session progress {entry['progress']} ({entry['progress_why']})")
        b = boundary_s(self.state["history"], res.started)
        if b is not None:
            entry["boundary_s"] = b
        early = early_exit_minutes(res.reason, res.started, res.ended, minutes)
        if early is not None:
            entry["early_exit_min"] = early
            log(f"{sid}: early exit: claude ended with {early:g} of {minutes:g} min left (exit {res.exit_code}); "
                f"starting the next session now" + (f" (boundary before this session: {b:g} s)" if b is not None else ""))
        self.state["history"] = self.state["history"] + [entry]
        self.new_game = False  # from here on the game has a character: confirm by name
        if is_stuck(self.state["history"]):
            log(f"stuck detector: {STUCK_SESSIONS} sessions without progress; next kickoff says change approach")
        if gave_up is not None:
            raise gave_up
        return res

    # ---------------------------------------------------------------- main loop

    def wait_api_limit(self, res: SessionResult) -> None:
        """RUN-7: the play client hit a usage/rate limit or an overload: wait (api_backoff_s: until its reset time,
        else doubling from API_BACKOFF_MIN_S) and let the run loop start the next session. Never a give-up; an
        operator stop (signal, runtime/STOP) ends the wait at once."""
        wait = api_backoff_s(res.resets_at, time.time(), self.api_limits)
        until = time.time() + wait
        msg = (f"API limit ({res.detail[:200]}); waiting {wait / 60:.0f} min until "
               f"{datetime.fromtimestamp(until):%H:%M}" + (" (its reset time)" if res.resets_at else "")
               + f", limit {self.api_limits} in a row; the run continues by itself")
        log(msg)
        self.event("api_limit", detail=res.detail[:300], wait_s=round(wait), resets_at=res.resets_at,
                   repeat=self.api_limits)
        write_status(alert={"t": time.time(), "kind": "api_limit", "msg": msg[:600]})
        while time.time() < until and not (self.stop_requested or self.abort or (RUNTIME / "STOP").exists()):
            time.sleep(min(15.0, max(until - time.time(), 0.0)))
        write_status(alert=None)

    def session_minutes(self) -> float:
        """--session-minutes, or session-config.json "minutes" (re-read before every session: switch 20 ↔ 60 live)."""
        want = session_overrides(self.state.dir / "session-config.json").get("minutes", self.opt.session_minutes)
        if want < MIN_SESSION_MINUTES and not self.opt.allow_short:
            log(f"session-config.json minutes {want:g} ignored: below {MIN_SESSION_MINUTES} "
                "(start the runner with --allow-short-sessions for that)")
            want = self.opt.session_minutes
        if want != getattr(self, "_last_minutes", want):
            log(f"session length now {want:g} min (session-config.json)")
        self._last_minutes = want
        write_status(session_minutes=want)
        return want

    def code_update_check(self) -> None:
        """Before every session: log bridge edits (the MCP server restarts per session, nothing else to do), and
        raise RestartRunner when the runner's own loaded code changed on disk and the new code imports."""
        cur = tree_hashes(BRIDGE_SRC)
        if self.bridge_hashes is not None:
            changed = changed_files(self.bridge_hashes, cur)
            if changed:
                log(f"bridge code changed since last session: {', '.join(changed[:8])}"
                    + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else ""))
        self.bridge_hashes = cur
        stale = self.code_watch.changed()
        if not stale or self.stop_requested or (RUNTIME / "STOP").exists():
            return
        names = [str(p.relative_to(REPO)) for p in stale]
        try:
            p = subprocess.run(code_check_cmd(Path(__file__).resolve()), capture_output=True, text=True,
                               timeout=60, cwd=os.getcwd())
            ok, why = p.returncode == 0, (p.stderr or p.stdout).strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, why = False, [str(e)]
        if not ok:
            # Keep playing on the code that works; try again once these files change again.
            log(f"runner code changed ({', '.join(names)}) but the new code fails to import: {why[0][:300]}; "
                "keeping the running code")
            self.event("runner_restart_refused", files=names, error=why[0][:300])
            for f in stale:
                self.code_watch.hashes[f] = _sha1(f)
            return
        log(f"runner code changed: restarting ({', '.join(names)})")
        self.join_keeper()
        raise RestartRunner(names)

    def handover_data(self, run_end: float | None, run_started: float) -> dict:
        return {"run_end": run_end, "run_started": run_started, "pending_notes": self.pending_notes,
                "quick_failures": self.quick_failures, "new_game": self.new_game,
                "bridge_hashes": self.bridge_hashes,
                "series_end": getattr(getattr(self, "opt", None), "series_end", None),
                "last_triage": [self.last_triage[0], str(self.last_triage[1])] if self.last_triage else None}

    def run_end(self, now: float) -> float | None:
        """When this run stops starting sessions: the earliest of --hours (one run), the series' end (--runs
        --hours) and --run-hours from now."""
        ends = [t for t in (now + self.opt.hours * 3600 if self.opt.hours else None, self.opt.series_end,
                            now + self.opt.run_hours * 3600 if self.opt.run_hours else None) if t]
        return min(ends) if ends else None

    def run(self) -> int:
        self.check_memory()
        self.recover_keeper()
        h = self.handover or {}
        now = time.time()
        run_started = self.run_t0 = h.get("run_started") or now
        if h:   # same run, new code: keep its --hours budget; the game is the one this run was playing
            run_end = h.get("run_end")
            log(f"runner restarted in place with the new code (pid {os.getpid()})")
            self.event("runner_restarted", pid=os.getpid())
        else:
            run_end = self.run_end(now)
            self.event("runner_start", pid=os.getpid())
        write_status(playthrough=self.opt.playthrough, runner_pid=os.getpid(), run_end=run_end,
                     session=None, deadline=None, run_started=run_started, heartbeat=time.time(), alert=None,
                     series=self.opt.series, run_index=self.opt.run_index, sandbox=self.opt.sandbox)
        first = not h
        setup_failures = 0
        stop_why = "stopped"
        outcome = None   # --until: how the run ended, when that ending writes a record (not a stop or a restart)
        restarting = False
        try:
            if self.check_until(read_seen(self.state.dir), None):
                what = "the game is finished" if self.game_ended() else f"--until {self.opt.until}"
                log(f"{what}: already reached in this playthrough; nothing to play")
                stop_why = f"{what}: already reached"
                outcome = "already_reached"   # no record: that run was recorded when it got there
                return 0
            if self.opt.until:
                run_started = self.run_started = self.begin_run(run_started)
            while not self.stop_requested and not (RUNTIME / "STOP").exists():
                self.code_update_check()
                loop = crash_loop(run_events(self.state.dir, run_started - 1), time.time(), run_started)
                if loop:
                    log(f"{loop}: stopping the run (the game keeps dying; see runner_events.jsonl and the launch logs)")
                    self.event("crash_loop", why=loop)
                    write_status(alert={"t": time.time(), "kind": "crash_loop", "msg": loop})
                    stop_why, outcome = f"gave up: {loop}", "gave_up"
                    return 1
                if self.heat_paused() or self.cost_paused():
                    continue
                self.write_status_line("starting the next session")
                if self.has_usage_caps():   # the finished processes so far, the memory keeper's included
                    done = runs.claude_totals(self.state.dir, run_started)
                    if self.check_usage_caps(done["cost_usd"], runs.token_total(done["tokens"])):
                        stop_why, outcome = self.cap_hit["why"], "over_budget"
                        break
                session_minutes = self.session_minutes()
                minutes = session_budget(time.time(), run_end, session_minutes,
                                         min(MIN_SESSION_MINUTES, session_minutes))
                if minutes is None:
                    stop_why = ("time cap used up (--run-hours/--hours)" if self.opt.run_hours or self.opt.series_end
                                else "--hours budget used up")
                    log(stop_why)
                    outcome = "timeout"
                    break
                try:
                    res = self.play_session(minutes, first)
                except LaunchBlocked as e:
                    # Never a give-up: an operator puts Steam back offline (or closes their own BG3) and the run
                    # continues by itself.
                    log(f"session setup refused: {'Steam not offline' if isinstance(e, SteamNotOffline) else 'launch blocked'} "
                        f"({e}); retrying in {STEAM_RETRY_S} s")
                    if self.abort:
                        break
                    time.sleep(STEAM_RETRY_S)
                    continue
                except SETUP_ERRORS as e:
                    log(f"session setup failed: {type(e).__name__}: {e}")
                    self.event("setup_failed", error=str(e)[:300])
                    if self.abort:
                        break
                    setup_failures += 1
                    if setup_failures >= MAX_SETUP_FAILURES:
                        log(f"{setup_failures} setup failures in a row; giving up")
                        stop_why = "gave up: setup failures"
                        outcome = "gave_up"
                        return 1
                    try:
                        self.recover(f"failed to start ({e})")
                        self.event("recovered", fault="setup", how=f"game relaunched, loaded "
                                   f"{self.state['loaded_save'] or 'main menu'}")
                    except (LaunchGaveUp, Bg3leStale):
                        raise
                    except Exception as e2:  # noqa: BLE001
                        log(f"recovery failed: {e2}; waiting 2 minutes")
                        if not isinstance(e2, LaunchBlocked):
                            self.event("recovery_failed", fault="setup", error=str(e2)[:300])
                        time.sleep(120)
                    continue
                first = False
                if res.reason != "mcp_failed":
                    setup_failures = 0
                if res.reason == "dry_run":
                    break
                if self.until_hit:
                    stop_why = (f"the game is finished: {self.until_hit['label']} in {self.until_hit.get('session')}"
                                if self.game_ended() else
                                f"--until {self.opt.until}: reached in {self.until_hit.get('session')}")
                    outcome = "reached"
                    break
                if self.cap_hit:
                    stop_why, outcome = self.cap_hit["why"], "over_budget"
                    break
                if res.reason == "api_limit":   # RUN-7: wait for the limit's reset, never give up
                    self.quick_failures = 0
                    self.api_limits += 1
                    self.wait_api_limit(res)
                    continue
                self.api_limits = 0
                if res.reason == "mcp_failed":   # RUN-8: a setup failure (the agent had no game tools)
                    setup_failures += 1
                    if setup_failures >= MAX_SETUP_FAILURES:
                        log(f"{setup_failures} setup failures in a row (the bg3 MCP server: {res.detail}); giving up")
                        stop_why = "gave up: the bg3 MCP server fails to start"
                        outcome = "gave_up"
                        return 1
                    time.sleep(min(30 * setup_failures, 300))
                    continue
                if res.ended - res.started < 60 and res.reason == "exited":
                    self.quick_failures += 1
                    if self.quick_failures >= MAX_QUICK_FAILURES:
                        log("claude exited within a minute 3 times in a row; stopping (see the session logs)")
                        stop_why = "gave up: claude exits at once"
                        outcome = "gave_up"
                        return 1
                    time.sleep(30 * self.quick_failures)
                else:
                    self.quick_failures = 0
        except RestartRunner as e:
            restarting = True
            write_handover(self.handover_data(run_end, run_started))
            self.event("runner_restart", files=e.args[0] if e.args else [])
            raise
        except LaunchGaveUp as e:
            self.give_up_launch(e)
            stop_why = "gave up: the game crashes on every launch"
            outcome = "gave_up"
            return 1
        except Bg3leStale as e:
            stop_why = f"stale bg3le build: run {bg3le_buildinfo.REBUILD} ({e})"[:300]
            outcome, self.fatal = "gave_up", True
            return 1
        except BaseException as e:
            stop_why = f"died: {type(e).__name__}: {e}"[:300]
            if not isinstance(e, KeyboardInterrupt):
                outcome, self.fatal = "runner_error", True
            if not isinstance(e, (SystemExit, KeyboardInterrupt)):   # a SystemExit carries its own reason
                log("runner died:\n" + traceback.format_exc().rstrip())
            raise
        finally:
            self.join_keeper()
            self.sandbox_cleanup()
            self.outcome = outcome or ("stopped" if self.opt.until and not restarting else None)
            if not restarting:
                # (a playthrough run without --until gets one record too: the game's end, its terminal milestone)
                if (self.opt.until or self.game_ended()) and outcome in runs.OUTCOMES and not self.opt.dry_run:
                    try:
                        self.write_run_record(run_started, outcome, "" if outcome == "reached" else stop_why)
                    except Exception as e:  # noqa: BLE001 - a failed record never hides why the runner stopped
                        log(f"run record failed: {type(e).__name__}: {e}")
                (RUNTIME / "STOP").unlink(missing_ok=True)
                write_status(session=None, deadline=None, runner_pid=None)
                self.write_status_line(f"STOPPED ({stop_why})")
                self.event("runner_stop", why=stop_why)
                log(f"runner stopped ({stop_why})")   # every exit path: tools/orchestrator/test_cycle.sh waits for it
        return 0


# ======================================================================== CLI

def acquire_lock():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    f = open(RUNTIME / "runner.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another runner is already active (runtime/runner.lock)") from None
    f.write(str(os.getpid()))
    f.flush()
    return f


def load_scenario_file(name: str) -> dict:
    """scenarios/<name>.json, validated (runs.load_scenario); runs.ScenarioError when missing or invalid."""
    path = SCENARIOS / f"{name}.json"
    if not path.exists():
        have = ", ".join(sorted(p.stem for p in SCENARIOS.glob("*.json"))) or "none"
        raise runs.ScenarioError(f"no scenario {name!r} in {SCENARIOS} (have: {have})")
    return runs.load_scenario(path, [m[0] for m in milestones.MILESTONES], ROLEPLAY_MODES, DIFFICULTIES)


def scenario_status(sc: dict, saves: list[Save] | None = None) -> tuple[str, str]:
    """runs.scenario_status against the save folder (read only: the start save's .lsv is hashed, never written)."""
    folders = {s.folder for s in (list_saves() if saves is None else saves)}
    return runs.scenario_status(sc, folders, lambda f: runs.save_digest(SAVE_DIR, f))


def apply_scenario(a, sc: dict) -> tuple[str | None, str | None]:
    """run --scenario: the scenario's values into the parsed args (a flag left out takes the scenario's; one that
    contradicts it is refused). -> (refusal, the start save's sha1)."""
    conflicts = runs.scenario_cli_conflicts(sc, {"until": a.until, "character_brief": a.character_brief,
                                                 "roleplay": a.roleplay, "difficulty": a.difficulty,
                                                 "save_owner": a.save_owner})
    if conflicts:
        return "; ".join(conflicts) + " (drop the flag: the scenario fixes it)", None
    status, detail = scenario_status(sc)
    if status != runs.READY:
        return f"{status}: {detail}", None
    a.playthrough = a.playthrough or sc["name"]
    a.until, a.runs = sc["until"], a.runs or sc["runs"]
    a.character_brief = a.roleplay = a.difficulty = None   # profile.json comes from the scenario (prepare_series_run)
    sha1 = None
    if sc["start"]["kind"] == "save":
        a.save_owner = sc["profile"]["character"]["name"]
        sha1 = runs.save_digest(SAVE_DIR, sc["start"]["save"])
    return runs.scenario_series_check(series_meta(a.playthrough), sc, a.playthrough, sha1), sha1


def cmd_run(a) -> int:
    scenario = save_sha1 = None
    if a.scenario:
        try:
            scenario = load_scenario_file(a.scenario)
        except runs.ScenarioError as e:
            print(f"refusing --scenario: {e}", file=sys.stderr)
            return 2
        refusal, save_sha1 = apply_scenario(a, scenario)
        if refusal:
            print(f"refusing --scenario {a.scenario}: {refusal}", file=sys.stderr)
            return 2
    elif not a.playthrough:
        print("run needs --playthrough (or --scenario NAME)", file=sys.stderr)
        return 2
    if a.session_minutes < MIN_SESSION_MINUTES and not a.allow_short_sessions:
        # S001/S002 ran 15-min sessions and both wrapped up within 3 min: start and wrap-up playbooks
        # alone take ~13 min, so a short session is all overhead.
        print(f"refusing --session-minutes {a.session_minutes:g}: sessions under {MIN_SESSION_MINUTES} min are "
              "all start-up and wrap-up (use 60; --allow-short-sessions for harness tests)", file=sys.stderr)
        return 2
    if a.autocompact is not None and not AUTOCOMPACT_MIN <= a.autocompact <= AUTOCOMPACT_MAX:
        print(f"refusing --autocompact {a.autocompact}: claude takes {AUTOCOMPACT_MIN}-{AUTOCOMPACT_MAX} tokens")
        return 2
    if a.runs is not None and (a.runs < 1 or not a.until):
        print("refusing --runs: it needs N >= 1 and --until MILESTONE (each run ends there)", file=sys.stderr)
        return 2
    if a.restart_run and not a.runs:
        print("refusing --restart-run: it restarts a series run (--runs N)", file=sys.stderr)
        return 2
    if a.run_hours is not None and (a.run_hours <= 0 or not a.until):
        print("refusing --run-hours: it needs a positive value and --until MILESTONE", file=sys.stderr)
        return 2
    for flag, v in (("--max-cost", a.max_cost), ("--max-tokens", a.max_tokens)):
        if v is not None and (v <= 0 or not a.until):
            print(f"refusing {flag}: it needs a positive value and --until MILESTONE", file=sys.stderr)
            return 2
    if a.max_cost_per_day is not None and a.max_cost_per_day <= 0:
        print("refusing --max-cost-per-day: it needs a positive value", file=sys.stderr)
        return 2
    caps = {"run_hours": a.run_hours, "max_cost": a.max_cost, "max_tokens": a.max_tokens}
    cap_changes: list[str] = []
    if a.runs:   # a series' caps are fixed by its first run (series.json), a scenario's by the scenario
        frozen = series_meta(a.playthrough).get("caps") or (scenario["caps"] if scenario else None)
        caps, err, cap_changes = runs.series_caps(frozen, caps, a.allow_cap_change)
        if err:
            print(f"refusing to run series {a.playthrough}: {err}", file=sys.stderr)
            return 2
    use_judge = bool(a.runs) if a.judge is None else a.judge   # on for a series (--runs / --scenario)
    if use_judge and not a.until:
        print("refusing --judge: it scores a run's record, which needs --until MILESTONE", file=sys.stderr)
        return 2
    judge_model, judge_change = a.judge_model or os.environ.get("BG3AI_JUDGE_MODEL") or judge.DEFAULT_MODEL, None
    if use_judge and a.runs:   # a series' judge is fixed like its caps: scores by another judge don't compare
        judge_model, err, judge_change = runs.series_judge_model(
            series_meta(a.playthrough).get("judge_model"), a.judge_model or os.environ.get("BG3AI_JUDGE_MODEL"),
            judge.DEFAULT_MODEL, a.allow_judge_change)
        if err:
            print(f"refusing to run series {a.playthrough}: {err}", file=sys.stderr)
            return 2
    client, client_change = a.client or "claude", None
    if a.runs:   # a series plays with one CLI, fixed like its judge; a series from before --client played claude
        meta = series_meta(a.playthrough)
        client, err, client_change = runs.series_client(meta.get("client") or ("claude" if meta else None),
                                                        a.client, "claude", a.allow_client_change)
        if err:
            print(f"refusing to run series {a.playthrough}: {err}", file=sys.stderr)
            return 2
    if use_judge and judge.same_model(judge_model, a.model):
        print(f"refusing --judge: the judge model {judge_model} is the model under test ({a.model}); pick another "
              "with --judge-model (fixed for the whole benchmark) or pass --no-judge", file=sys.stderr)
        return 2
    if a.session_minutes < MIN_SESSION_MINUTES:
        log(f"WARNING: {a.session_minutes:g}-min sessions (wrap-up at {wrap_minutes(a.session_minutes):.1f} min); "
            "expect little play")
    if a.sandbox_unfirewalled is True and a.sandbox != "container":
        print("refusing --sandbox-unfirewalled: it goes with --sandbox container", file=sys.stderr)
        return 2
    sandbox_change = None
    if a.runs:   # a series plays in one sandbox mode, fixed like its client; checked before docker is touched
        err, sandbox_change = runs.series_sandbox(series_meta(a.playthrough),
                                                  runs.sandbox_mode(a.sandbox, a.sandbox_unfirewalled),
                                                  a.allow_sandbox_change)
        if err:
            msg = f"series {a.playthrough}: REFUSING the run: {err}"
            print(msg, file=sys.stderr)
            log(msg)
            log("runner stopped (sandbox mode differs from the series: run refused)")
            return 2
    # a the multiplexer/ssh shell has no display env: the game (launch_game.sh inherits os.environ) gets the desktop's
    filled = display_env.fill(os.environ)
    if a.plan:   # after every refusal above, before the lock: nothing written, nothing launched
        return plan_run(a, scenario, save_sha1, caps, use_judge, judge_model, client)
    lock = acquire_lock()  # held for the process lifetime (released only to exec the new runner code)
    if filled:
        log("display env filled from the desktop session: " + " ".join(f"{k}={v}" for k, v in filled.items()))
    if not os.environ.get("XAUTHORITY"):
        log("WARNING: no XAUTHORITY and no Xwayland -auth: the game may fail to open a window")
    box = None
    if a.sandbox == "container":   # once per runner: docker, network, images, firewall, egress proxy
        try:
            box = sandbox.prepare(client, None, a.sandbox_unfirewalled, log, RUNTIME / "sandbox-server.pid")
        except sandbox.SandboxError as e:
            print(f"refusing --sandbox container: {e}", file=sys.stderr)
            log(f"runner stopped (sandbox not ready: {str(e).splitlines()[0][:200]})")
            return 2
    (RUNTIME / "STOP").unlink(missing_ok=True)
    opt = Options(a.playthrough, a.hours, a.session_minutes, a.model, a.take_over, a.dry_run,
                  a.save_owner, a.effort, a.fix_model, a.fix_effort, a.fallback_model,
                  a.allow_short_sessions, not a.no_memory_keeper, a.keeper_model, a.keeper_effort,
                  a.keeper_minutes, a.autocompact, until=a.until, character_brief=a.character_brief,
                  roleplay=a.roleplay, difficulty=a.difficulty, scenario=scenario["name"] if scenario else None,
                  start_save=scenario["start"].get("save") if scenario else None)
    opt = replace(opt, run_hours=caps["run_hours"], max_cost=caps["max_cost"], max_tokens=caps["max_tokens"],
                  cap_changes=cap_changes or None, pins=current_pins() if a.until and not a.runs else None,
                  judge=use_judge, judge_model=judge_model if use_judge else None, judge_change=judge_change,
                  client=client, client_change=client_change, sandbox=a.sandbox, box=box,
                  sandbox_change=sandbox_change, restart_run=a.restart_run,
                  keep_game=a.keep_game, max_cost_per_day=a.max_cost_per_day)
    if a.runs:   # --hours is the whole series' budget; each run gets a fresh playthrough
        opt = replace(opt, hours=None, series=a.playthrough,
                      series_end=time.time() + a.hours * 3600 if a.hours else None)
    current: dict = {"runner": None, "stop": False}   # the run playing now (a series has one Runner per run)
    # Cutscenes are always skipped, also while the runner waits on menus and loads (the New Game intro
    # plays during StartLoading) and between sessions; the play session's MCP server idles meanwhile
    # (one skipper per game, runtime/skipper.lock).
    from bg3ai.skipper import start_background
    skip_stop = start_background(DEBUG_PORT, lambda m: log(f"autoskip: {m}"), RUNTIME / "skipper.lock")
    ram_stop = ramwatch.start_background(log)
    log("ramwatch: started (free RAM per minute → runtime/ram.log)" if ram_stop is not None else
        "ramwatch: not started here: another watchdog holds runtime/ramwatch.lock (that one guards the game)")

    def on_signal(signum, _frame):
        runner = current["runner"]
        if runner is not None and runner.stop_requested:
            log("second signal: interrupting the current session")
            runner.abort = True
            return
        log(f"signal {signum}: stopping after the current session (send again to interrupt it)")
        current["stop"] = True
        if runner is not None:
            runner.stop_requested = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        if a.runs and os.environ.get("BG3AI_KNOWLEDGE_LOCK", "1") != "0":
            log(f"play/knowledge read-only for the series: {runs.lock_tree(KNOWLEDGE_DIR, KNOWLEDGE_LOCK)} files "
                f"(modes in {KNOWLEDGE_LOCK}; BG3AI_KNOWLEDGE_LOCK=0 skips)")
        if a.runs:
            return run_series(opt, a.runs, current, a.allow_version_drift, scenario, save_sha1)
        if runs.unlock_tree(KNOWLEDGE_DIR, KNOWLEDGE_LOCK):   # a series runner that died while locked
            log("play/knowledge: a stale series lock was undone")
        current["runner"] = Runner(opt)
        rc = current["runner"].run()
        stop_game_at_end(current["runner"], opt, "run end")
        return rc
    except RestartRunner:
        exec_new_runner(lock, skip_stop)
        raise  # not reached
    finally:   # not reached on a self-restart (exec_new_runner): the successor keeps the series' lock
        if skip_stop is not None:
            skip_stop.set()
        if box is not None:
            sandbox.shutdown()   # the egress proxy and any container left (a self-restart's successor recreates them)
        if ram_stop is not None:
            ram_stop.set()
        runs.unlock_tree(KNOWLEDGE_DIR, KNOWLEDGE_LOCK)


PLAN_PROMPT = "<KICKOFF: built at the session start from the run's memory briefing>"


def resume_preview(pt: str, save_owner: str | None, session_minutes: float, allow_short: bool) -> dict | None:
    """run --plan for a playthrough with memory, read-only: what Runner.next_sid, choose_save, save_env and
    session_minutes would give its next session (without it --plan printed S001 and no save for a resume). None: a
    new playthrough."""
    import memlib as M
    mem = PLAYTHROUGHS / pt / "memory"
    if not mem.is_dir():
        return None
    d = RUNTIME / "sessions" / pt
    try:
        rs = json.loads((d / "runner.json").read_text())
    except (OSError, ValueError):
        rs = {}
    reuse = M.reusable_session(mem)
    sid = session_id(reuse) if reuse else session_id(
        predict_session_number(M.session_numbers(mem), M.load_state(mem).get("current")))
    handoff = mem / "HANDOFF.md"
    text = handoff.read_text() if handoff.exists() else ""
    label = parse_handoff_save(text)
    ours = set(rs.get("runner_saves") or []) | set(rs.get("agent_saves") or [])
    windows = play_windows(rs.get("history") or [], d)
    saves, after = list_saves(), rs.get("saves_after")
    own = [s for s in saves if s.mtime >= after] if after else saves
    save, why = pick_save_why(own, label, rs.get("save_owner"), ours, windows=windows,
                              handoff_session=parse_handoff_session(text), ends=session_ends(rs.get("history"), d))
    owner, prefix = save_identity(save_owner or rs.get("save_owner"))
    foreign = foreign_saves(saves, owner, ours, windows, prefix) if not after else []
    over = session_overrides(d / "session-config.json")
    minutes = over.get("minutes", session_minutes)
    if minutes < MIN_SESSION_MINUTES and not allow_short:
        minutes = session_minutes
    return {"sid": sid, "handoff_label": label, "save": save.folder if save else None, "save_why": why,
            "oom_blocked": bool(save and save.folder in (rs.get("oom_blocked") or [])), "exclude": foreign,
            "minutes": minutes, "model": over.get("model"), "effort": over.get("effort"),
            "memory_linked": (PLAY_DIR / "memory").resolve() == mem.resolve()}


def plan_run(a, scenario: dict | None, save_sha1: str | None, caps: dict, use_judge: bool, judge_model: str,
             client_name: str) -> int:
    """run --plan: what `run` would do next, read-only (no runner lock, no game, no playthrough/memory/series files;
    the Runner isn't built: its RunnerState makes the session dir and read_handover consumes a pending handover)."""
    base = a.playthrough
    print(f"plan for `run` (nothing launched or written; {time.strftime('%Y-%m-%d %H:%M')})")
    k, pt = 1, base
    if a.runs:
        recs = runs.series_records(RUNTIME / "sessions" / base / "runs.jsonl", base)
        k = runs.next_run_index(recs)
        pt = runs.run_name(base, k)
        print(f"series:         {base}: run {k}/{a.runs} next ({pt}), {len(recs)} recorded"
              + ("; ALL DONE: run would exit at once" if k > a.runs else ""))
        half = half_run_state(pt) if k <= a.runs else None
        active = (RUNTIME / "runner.lock").exists() and not _lock_free(RUNTIME / "runner.lock")
        for line in half_run_lines(half, forced=a.restart_run, active=active) if half else []:
            print(line)
        prof = series_profile(base) or (scenario["profile"] if scenario else None)
        if prof is None:
            prof = load_profile(PLAYTHROUGHS / (base if k <= 1 else pt) / "profile.json")
        prof = update_profile(prof, a.character_brief, a.roleplay, a.difficulty) \
            if (a.character_brief or a.roleplay or a.difficulty) else prof
        pins = current_pins()
        meta = series_freeze(base, a.runs, a.until, prof, pins, caps,
                             runs.scenario_freeze(scenario, save_sha1) if scenario else None,
                             judge_model if use_judge else None, client_name,
                             runs.sandbox_mode(a.sandbox, bool(a.sandbox_unfirewalled)))
        path = RUNTIME / "sessions" / base / "series.json"
        if meta is None:
            print(f"series.json:    {path} exists with every key (frozen; this run is held to it):")
            print(json.dumps(series_meta(base), indent=1, ensure_ascii=False))
        else:
            print(f"series.json:    {'NEW ' if not path.exists() else 'keys added to '}{path} would be:")
            print(json.dumps(meta, indent=1, ensure_ascii=False))
        drift = runs.pin_drift(series_meta(base).get("pins"), pins) if path.exists() else []
        if drift:
            print(f"version drift:  {'; '.join(drift)} -> run {k} "
                  + ("plays with it (recorded)" if a.allow_version_drift else "REFUSED (--allow-version-drift)"))
        if os.environ.get("BG3AI_KNOWLEDGE_LOCK", "1") != "0":
            print(f"knowledge:      {KNOWLEDGE_DIR} read-only for the series (BG3AI_KNOWLEDGE_LOCK=0 skips)")
    else:
        print(f"playthrough:    {pt} (not a series)")
    req_off, why = steam_paths.offline_required()
    off, owhy = steam_paths.steam_offline()
    print(f"steam:          offline required: {req_off} ({why}); offline now: {off} ({owhy})"
          + ("  -> THE RUNNER WOULD REFUSE TO LAUNCH" if req_off and not off else ""))
    no_display = display_env.problem(os.environ)
    print(f"display:        {display_env.describe(os.environ)} (unset ones found from the desktop session)"
          + ("" if os.environ.get("XAUTHORITY") else "  -> NO XAUTHORITY")
          + (f"  -> THE RUNNER WOULD REFUSE TO LAUNCH: {no_display}" if no_display else ""))
    bad = bg3le_buildinfo.problems()
    print("bg3le:          " +("STALE: " + "; ".join(bad) + " -> the runner would stop" if bad else "dist has every patch"))
    game = pids_of_game()
    print(f"game:           {'harness bg3 up: ' + str(game) if game else 'not running: the runner launches it'}"
          + (f"; a user's own bg3 runs {foreign_bg3_pids()}: the runner won't launch beside it"
             if foreign_bg3_pids() else ""))
    holder = (RUNTIME / "runner.lock").exists() and not _lock_free(RUNTIME / "runner.lock")
    print(f"runner lock:    {'HELD: another runner is active (run would refuse)' if holder else 'free'}")
    keeper = keeper_model(a.keeper_model, a.model) if not a.no_memory_keeper else "OFF"
    print(f"play:           client {client_name}, model {a.model}, effort {a.effort or 'default'}, "
          f"{a.session_minutes:g}-min sessions, keeper {keeper}, judge {judge_model if use_judge else 'off'}, "
          f"sandbox {a.sandbox}, caps {caps}, --hours {a.hours or 'none (until stopped or the game ends)'}"
          + (f", --max-cost-per-day ${a.max_cost_per_day:g}" if a.max_cost_per_day else ""))
    sid, minutes, model, effort = "S001", a.session_minutes, a.model, a.effort
    res = resume_preview(pt, a.save_owner or os.environ.get("BG3AI_SAVE_OWNER"), a.session_minutes,
                         a.allow_short_sessions)
    if res:
        sid, minutes = res["sid"], res["minutes"]
        model, effort = res["model"] or model, res["effort"] or effort
        print(f"resume:         {sid}; loads {res['save'] or 'NO SAVE (new game)'} (HANDOFF names "
              f"{res['handoff_label'] or '-'})" + ("  -> OOM-BLOCKED: the runner loads an older save" if
                                                    res["oom_blocked"] else ""))
        print(f"save choice:    {res['save_why']}")
        print(f"saves hidden:   {', '.join(res['exclude']) or 'none'} (BG3AI_SAVES_EXCLUDE)")
        print(f"session-config: {minutes:g} min, model {model}, effort {effort or 'default'} (session-config.json "
              "wins over the flags)" + ("" if res["memory_linked"] else f"; play/memory is relinked to {pt}"))
    owner, prefix = save_identity(a.save_owner or os.environ.get("BG3AI_SAVE_OWNER"))
    env = {"BG3AI_PLAYTHROUGH": pt, "BG3AI_SESSION": sid, "BG3_SESSION_MINUTES": str(minutes),
           "BG3_WRAP_MINUTES": str(wrap_minutes(minutes)), "BG3AI_SAVE_OWNER": owner,
           "BG3AI_SAVE_PREFIX": prefix, "BG3AI_KEEPER": "0" if a.no_memory_keeper else "1",
           **({"BG3AI_SAVES_EXCLUDE": "\n".join(res["exclude"])} if res and res["exclude"] else {}),
           **({"BG3AI_START_SAVE": scenario["start"]["save"]} if scenario and scenario["start"].get("save") else {})}
    opt = types.SimpleNamespace(sandbox=a.sandbox, client=client_name)
    cl = make_client(opt)
    req = clients.Request(PLAN_PROMPT, model, RUNTIME / "sessions" / pt, session=str(uuid.uuid4()), resume=False,
                          effort=effort, fallback_model=a.fallback_model, autocompact=a.autocompact, env=env,
                          tag=sid)
    cmd = cl.command(req)
    print(f"{'next session: ' if res else 'first session:'}  {sid} of {pt} in {PLAY_DIR} (deadline in {minutes:g} min; the "
          "session env adds the deadlines, metrics path and BG3AI_* of the runner's env)")
    print(json.dumps({"cmd": cmd, "cwd": str(PLAY_DIR), "env": env}, indent=1, ensure_ascii=False))
    if a.sandbox == "container":
        print("sandbox:        the command runs inside the container (box.run_argv), the bg3 server over http")
    return 0


def _lock_free(path: Path) -> bool:
    """Nobody holds an flock on path (probed with a separate open; released at once)."""
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(f, fcntl.LOCK_UN)
    return True


def persona_line(p: dict) -> str:
    """One log line for a record's persona."""
    what = f"score {p['score']:g}/10" if p.get("score") is not None else p.get("status", "?")
    return (f"{what} by {p.get('judge_model')} on {p.get('decisions', 0)} decision(s), ${p.get('cost_usd') or 0:.2f}"
            + (f": {p['error']}" if p.get("error") else ""))


def series_meta(base: str) -> dict:
    """runtime/sessions/<base>/series.json ({} when there is none yet)."""
    try:
        d = json.loads((RUNTIME / "sessions" / base / "series.json").read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def series_profile(base: str) -> dict | None:
    """The character brief every run of a series plays (runtime/sessions/<base>/series.json "profile"), frozen when
    the series started: run 1 later adds the name its agent chose to its own profile.json, and run 2 must not get
    that name forced on it."""
    return series_meta(base).get("profile")


def freeze_series(base: str, n: int, until: str | None, profile: dict, pins: dict | None = None,
                  caps: dict | None = None, scenario: dict | None = None, judge_model: str | None = None,
                  client: str | None = None, box: dict | None = None) -> None:
    """series.json, written once by the first run: the profile, N, the milestone, the version pins, the caps, the
    persona judge's model, the client (--client) and the sandbox (box: runs.sandbox_mode) every later run is held
    to, and the scenario (runs.scenario_freeze) of a run --scenario series. A series from before
    pins/caps/judge/client gets them from the first run that has them; one from before the sandbox key never gets
    it (it played without a sandbox: runs.series_sandbox reads a missing key as "none")."""
    meta = series_freeze(base, n, until, profile, pins, caps, scenario, judge_model, client, box)
    if meta is None:
        return
    path = RUNTIME / "sessions" / base / "series.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")


def series_freeze(base: str, n: int, until: str | None, profile: dict, pins: dict | None = None,
                  caps: dict | None = None, scenario: dict | None = None, judge_model: str | None = None,
                  client: str | None = None, box: dict | None = None) -> dict | None:
    """What freeze_series would write to series.json (None: nothing, it has every key already). Writes nothing
    (run --plan prints it)."""
    path = RUNTIME / "sessions" / base / "series.json"
    meta = series_meta(base)
    if path.exists() and ("pins" in meta or pins is None) and ("caps" in meta or caps is None) and \
            ("judge_model" in meta or judge_model is None) and ("client" in meta or client is None):
        return None
    new = not path.exists()
    if not path.exists():
        meta = {"series": base, "runs": n, "until": until, "profile": profile, "created": round(time.time(), 1)}
        if scenario is not None:
            meta["scenario"] = scenario
    if pins is not None:
        meta.setdefault("pins", pins)
    if caps is not None:
        meta.setdefault("caps", caps)
    if judge_model is not None:
        meta.setdefault("judge_model", judge_model)
    if client is not None:
        meta.setdefault("client", client)
    if new and box is not None:
        meta.update(box)
    return meta


def current_pins() -> dict:
    """The versions a run starting now plays with (runs.version_pins): the BG3 Steam buildid, the bg3le build the
    launcher loads, the mod tree and the repo."""
    lib = Path(os.environ.get("BG3AI_BG3LE_LIB", bg3le_buildinfo.DIST / "libbg3le.so"))
    return runs.version_pins(REPO, steam_paths.buildid(), lib.parent, MOD_DIR / "Mods" / "BG3AI" / "meta.lsx",
                             mod_tree_hashes())


def gate_series_run(base: str, r: Runner, pins: dict, allow_drift: bool) -> str | None:
    """Before run r of the series: a refusal (str) when a run that hasn't started yet would play with other
    versions than the series' first run (series.json "pins") and drift isn't allowed; else None, with the run's
    pins (and the drift, when allowed) set on r.opt. A run already under way keeps the pins it started with."""
    run = r.state["run"] or {}
    if run.get("started") or r.state["history"]:
        r.opt.pins = run.get("pins") or pins
        return None
    drift = runs.pin_drift(series_meta(base).get("pins"), pins)
    if drift and not allow_drift:
        return (f"series {base}: REFUSING run {r.opt.run_index}: versions differ from the series' first run "
                f"({'; '.join(drift)}); restore them, or pass --allow-version-drift to record the drift")
    r.opt.pins, r.opt.version_drift = pins, drift or None
    if drift:
        log(f"series {base}: run {r.opt.run_index} plays with version drift (allowed): {'; '.join(drift)}")
    return None


def prepare_series_run(base: str, pt: str, k: int, scenario: dict | None = None) -> None:
    """Run k's playthrough before its Runner exists: the series' profile.json (k > 1, or every run of a scenario:
    its profile) and a fresh memory from memory-template/ (tools/memory/new_playthrough.py), unless they're there
    already (a resumed run). A scenario that starts from a save also gets its start note as HANDOFF.md
    (runs.start_note: the harness's neutral text, never another playthrough's memory)."""
    from new_playthrough import create_memory
    import memlib
    prof_path = PLAYTHROUGHS / pt / "profile.json"
    prof = series_profile(base)
    if prof is None and scenario is not None:
        prof = scenario["profile"]
    if prof is None and k > 1:
        prof = load_profile(PLAYTHROUGHS / base / "profile.json") or None
    # run 1 restarted (restart_half_run moved its profile.json aside): the series' frozen profile
    if (k > 1 or scenario is not None or series_profile(base) is not None) and prof and not prof_path.exists():
        prof_path.parent.mkdir(parents=True, exist_ok=True)
        prof_path.write_text(json.dumps(prof, indent=1, ensure_ascii=False) + "\n")
        log(f"{pt}: profile.json " + (f"from scenario {scenario['name']}" if scenario else f"copied from series {base}"))
    mem = PLAYTHROUGHS / pt / "memory"
    if not mem.is_dir() or not any(mem.iterdir()):
        h = create_memory(pt, mem, MEMORY_TEMPLATE)
        log(f"{pt}: new memory {mem} from memory-template (commit {h[:8]})")
        if scenario is not None and scenario["start"]["kind"] == "save":
            (mem / "HANDOFF.md").write_text(runs.start_note(scenario))
            h = memlib.git_commit_all(mem, f"S000 scenario {scenario['name']}: start note") or h
            log(f"{pt}: HANDOFF.md = the start note of scenario {scenario['name']} (commit {h[:8]})")


SERIES_DIR_KEEP = ("series.json", "runs.jsonl", "runner_events.jsonl")   # run 1 shares the series' session dir


def _save_record_path(pt: str) -> Path:
    """runtime/saves/<pt>.txt: the folders the play session's save_game wrote (bridge server._save_record)."""
    return RUNTIME / "saves" / (re.sub(r"[^A-Za-z0-9_.-]", "_", pt) + ".txt")


def _git_lines(mem: Path, *args: str) -> list[str]:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(mem), *args], capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def half_run_state(pt: str) -> dict | None:
    """What a series run with no record left behind (a runner or machine crash mid-run; naut2 run 2, 2026-10-16
    20:10): runner.json's run start and its finished play sessions, its claude streams, memory edits past the
    template (an open session, uncommitted changes, session commits), the saves/<pt>.txt entries and the saves
    written since its start. None: nothing, a fresh run. Read-only (run --plan prints it)."""
    sdir = RUNTIME / "sessions" / pt
    try:
        st = json.loads((sdir / "runner.json").read_text())
    except (OSError, ValueError):
        st = {}
    st = st if isinstance(st, dict) else {}
    started = (st.get("run") or {}).get("started")
    finished = [h.get("session") for h in st.get("history") or []
                if h.get("kind") == "play" and (h.get("started") or 0) >= (started or 0)]
    streams = sorted(f.name for f in sdir.glob("S*.jsonl") if runs.CLAUDE_STREAM_RE.match(f.name)) \
        if sdir.is_dir() else []
    mem = PLAYTHROUGHS / pt / "memory"
    try:
        ms = json.loads((mem / ".session.json").read_text())
    except (OSError, ValueError):
        ms = {}
    open_sid = session_id(ms["current"]) if isinstance(ms, dict) and ms.get("open") and ms.get("current") else None
    dirty = _git_lines(mem, "status", "--porcelain") if (mem / ".git").exists() else []
    commits = [c for c in _git_lines(mem, "log", "--format=%h %s") if not c.split(" ", 1)[-1].startswith("S000")] \
        if (mem / ".git").exists() else []
    try:
        recorded = [x for x in _save_record_path(pt).read_text().splitlines() if x.strip()]
    except OSError:
        recorded = []
    after = st.get("saves_after")
    try:
        saves = [s.folder for s in sorted(list_saves(), key=lambda s: s.mtime) if after and s.mtime >= after]
    except Exception:  # noqa: BLE001 - a report, never fatal
        saves = []
    if not (started or finished or streams or open_sid or dirty or commits or recorded):
        return None
    return {"playthrough": pt, "started": started, "finished_sessions": finished, "streams": streams,
            "open_session": open_sid, "uncommitted": dirty, "commits": commits, "recorded_saves": recorded,
            "saves_since_start": saves, "restartable": not finished}


def half_run_lines(h: dict, forced: bool = False, active: bool = False) -> list[str]:
    """run --plan's warning about a half-played next run, and what `run` would do with it (active: a runner holds
    the lock, so this is most likely the run it is playing right now)."""
    when = datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M:%S") if h.get("started") else "?"
    mem = ", ".join(x for x in (
        f"session {h['open_session']} open" if h.get("open_session") else "",
        f"{len(h['uncommitted'])} uncommitted change(s)" if h.get("uncommitted") else "",
        f"{len(h['commits'])} commit(s) past S000" if h.get("commits") else "") if x) or "template only"
    out = [f"HALF-PLAYED:    {h['playthrough']} started {when} and has no record: "
           f"{len(h['finished_sessions'])} finished play session(s) {h['finished_sessions'] or ''}".rstrip(),
           f"                streams {', '.join(h['streams']) or 'none'}; memory: {mem}",
           f"                saves/{h['playthrough']}.txt: {len(h['recorded_saves'])} entr(y/ies)"
           + (f" ({', '.join(h['recorded_saves'][-3:])})" if h["recorded_saves"] else "")
           + f"; {len(h['saves_since_start'])} save(s) since its start"
           + (f" ({', '.join(h['saves_since_start'][-4:])})" if h["saves_since_start"] else "")]
    if active:
        out.append("                (a runner holds the lock: this is likely the run it is playing now; nothing to do)")
    elif forced or h["restartable"]:
        out.append(f"                -> run RESTARTS it ({'--restart-run' if forced else 'no finished play session'}): "
                   f"moves runtime/sessions/{h['playthrough']}, playthroughs/{h['playthrough']} and "
                   f"saves/{h['playthrough']}.txt aside to *.restarted-<time>, then a fresh start")
    else:
        out.append("                -> run CONTINUES it from its last save (its wall time counts from the start "
                   "above); --restart-run starts it over")
    return out


def archive_half_run(base: str, pt: str, stamp: str | None = None) -> list[str]:
    """Moves a half-played run aside so `run` starts it from scratch: runtime/sessions/<pt>, playthroughs/<pt> and
    runtime/saves/<pt>.txt → <name>.restarted-<stamp> (nothing deleted). Run 1 shares the series' session dir: its
    series.json, runs.jsonl and runner_events.jsonl stay. Returns what moved (as 'src -> dst')."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for root in (RUNTIME / "sessions", PLAYTHROUGHS):
        src = root / pt
        if not src.exists():
            continue
        dst = root / f"{pt}.restarted-{stamp}"
        if root == RUNTIME / "sessions" and pt == base:
            dst.mkdir(parents=True, exist_ok=True)
            for f in sorted(src.iterdir()):
                if f.name not in SERIES_DIR_KEEP:
                    f.rename(dst / f.name)
        else:
            src.rename(dst)
        moved.append(f"{src} -> {dst}")
    rec = _save_record_path(pt)
    if rec.exists():
        dst = rec.with_name(f"{rec.stem}.restarted-{stamp}.txt")
        rec.rename(dst)
        moved.append(f"{rec} -> {dst}")
    return moved


def restart_half_run(opt: Options, base: str, pt: str, k: int, forced: bool) -> dict | None:
    """Before run k's Runner exists: a half-played run whose runner died without a handover (a machine or runner
    crash) and that has no finished play session is started over (forced: --restart-run, whatever it has). Its
    game, when this harness still owns it, is stopped first. Never for this process's own execv successor (a
    pending handover: same run, new code) or a dry run. Returns the half-run state it archived, or None."""
    h = half_run_state(pt)
    if h is None or opt.dry_run or peek_handover() is not None or not (forced or h["restartable"]):
        return None
    try:
        old = Runner(replace(opt, playthrough=pt, run_index=k))
        if old.owns_game():
            old.kill_game()
    except Exception as e:  # noqa: BLE001 - the new run's setup handles a game still up
        log(f"series {base}: stopping run {k}'s old game failed: {e!r}")
    moved = archive_half_run(base, pt)
    when = datetime.fromtimestamp(h["started"]).strftime("%Y-%m-%d %H:%M:%S") if h.get("started") else "?"
    log(f"series {base}: run {k} ({pt}) RESTARTED from scratch "
        f"({'--restart-run' if forced else 'its runner died without a handover and no play session finished'}; "
        f"started {when}, {len(h['finished_sessions'])} finished session(s), "
        f"streams {', '.join(h['streams']) or 'none'}, "
        f"memory session {h['open_session'] or 'closed'}, {len(h['recorded_saves'])} recorded save(s)); moved aside: "
        + "; ".join(moved))
    h["moved"] = moved
    return h


def stop_game_at_end(r: "Runner | None", opt: Options, when: str) -> bool:
    """An --until run or a series that is over stops its harness game, as between runs (--keep-game: left up; a dry
    run and continuous play never). Not reached on a self-restart (RestartRunner leaves run() by exception)."""
    if r is None or not opt.until or opt.keep_game or opt.dry_run:
        return False
    r.end_run_game(when, "--keep-game leaves it up")
    return True


def run_series(opt: Options, n: int, current: dict, allow_drift: bool = False, scenario: dict | None = None,
               save_sha1: str | None = None) -> int:
    """run --until M --runs N: runs 1..N of the series opt.series, each a new playthrough (runs.run_name) and a new
    game, one runs.jsonl record per run in runtime/sessions/<base>/. Resumes at the first run without a record (a
    restart, by hand or on changed code, continues the series). Between runs the game is stopped (end_run_game).
    Stops early on an operator stop or on something a next run can't fix (stale bg3le, a runner error)."""
    from new_playthrough import SetupError
    base = opt.series
    path = RUNTIME / "sessions" / base / "runs.jsonl"
    rc, prev, first = 0, None, True
    while not current["stop"]:
        k = runs.next_run_index(runs.series_records(path, base))
        if k > n:
            break
        if prev is not None:
            prev.end_run_game()
        pt = runs.run_name(base, k)
        restarted = restart_half_run(opt, base, pt, k, forced=opt.restart_run and first)
        first = False
        try:
            prepare_series_run(base, pt, k, scenario)
        except (SetupError, OSError) as e:
            log(f"series {base}: can't set up run {k} ({pt}): {e}")
            log(f"runner stopped (series setup failed: run {k})")   # RUN-10: every exit path logs it
            return 1
        r = current["runner"] = Runner(replace(opt, playthrough=pt, run_index=k))
        if restarted:
            r.event("run_restarted", started=restarted.get("started"), finished_sessions=restarted["finished_sessions"],
                    streams=restarted["streams"], moved=restarted["moved"])
        if current["stop"]:
            r.stop_requested = True
        pins = current_pins()
        freeze_series(base, n, opt.until, r.profile, pins, {k_: getattr(opt, k_) for k_ in runs.CAP_KEYS},
                      runs.scenario_freeze(scenario, save_sha1) if scenario else None,
                      opt.judge_model if opt.judge else None, opt.client,
                      runs.sandbox_mode(opt.sandbox, bool(opt.box and opt.box.unfirewalled)))
        refusal = gate_series_run(base, r, pins, allow_drift)
        if refusal:
            log(refusal)
            print(refusal, file=sys.stderr)
            log("runner stopped (version drift: run refused)")   # tools/orchestrator/test_cycle.sh waits for it
            return 2
        opt = replace(opt, series_end=r.opt.series_end)   # a self-restart's handover keeps the series budget
        log(f"series {base}: run {k}/{n} ({pt}, --until {opt.until})")
        rc = r.run()
        if r.outcome not in runs.OUTCOMES or r.fatal or r.stop_requested:
            log(f"series {base}: stopping after run {k} ({r.outcome or 'no outcome'}"
                + ("; needs a human" if r.fatal else "") + ")")
            break
        prev = r
    stop_game_at_end(current["runner"], opt, "series end")
    recs = runs.series_records(path, base)
    if recs:
        log(f"series {base}: {len(recs)}/{n} run(s) recorded, {sum(1 for x in recs if x.get('clean'))} clean, "
            f"{runs.clean_streak(recs)} clean in a row (python3 runner/play.py runs --playthrough {base})")
    return rc


def exec_new_runner(lock, skip_stop, execv=os.execv) -> None:
    """Replace this process with the new runner code: same pid (the game's pgid bookkeeping and the status file
    stay valid), same argv. The lock and skipper are released first; the successor takes them again at once."""
    if skip_stop is not None:
        skip_stop.set()
    try:
        lock.close()
    except OSError:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    execv(sys.executable, restart_argv())


def cmd_stop(_a) -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "STOP").write_text(str(time.time()))
    print("stop requested: the runner exits after the current session")
    return 0


def _ago(t: float | None) -> str:
    if not t:
        return "-"
    d = t - time.time()
    m = abs(d) / 60
    when = f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"
    return f"{datetime.fromtimestamp(t):%H:%M} ({'in ' + when if d > 0 else when + ' ago'})"


def cmd_status(a) -> int:
    try:
        st = json.loads((RUNTIME / "runner.status.json").read_text())
    except (OSError, ValueError):
        st = {}
    pt = a.playthrough or st.get("playthrough")
    alive_pid = st.get("runner_pid")
    if alive_pid:
        try:
            os.kill(alive_pid, 0)
        except OSError:
            alive_pid = None
    up = port_open()
    gs = game_state() if up else None
    print(f"runner:   {'running (pid %d)' % alive_pid if alive_pid else 'not running'}"
          + (f", STOP requested" if (RUNTIME / 'STOP').exists() else ""))
    print(f"game:     {'up, state ' + str(gs) if up else 'not running (no debug port)'}")
    print(f"playthrough: {pt or '-'}")
    if alive_pid and st.get("session"):
        print(f"session:  {st['session']} started {_ago(st.get('started'))}, deadline {_ago(st.get('deadline'))}, "
              f"hard stop {_ago(st.get('hard_stop'))}")
    if st.get("run_end"):
        print(f"run ends: {_ago(st['run_end'])}")
    if st.get("status_line"):
        print(f"status:   {st['status_line']}")
    th = ramwatch.last_thermal(RUNTIME / "thermal.log")
    print(f"thermal:  {th[1] + ' (' + _ago(th[0]) + ')' if th else '- (no runtime/thermal.log)'}")
    if pt:
        rs = RunnerState(pt)
        hist = rs["history"] or []
        if hist:
            h = hist[-1]
            print(f"last:     {h['session']} {h['reason']} after {h.get('minutes')} min, progress={h.get('progress')}")
        if is_stuck(hist):
            print("stuck:    YES, the next kickoff carries a change-approach directive")
        save = pick_save(list_saves(), None, rs["save_owner"], set(rs["runner_saves"]) | set(rs["agent_saves"] or []),
                         windows=play_windows(hist, rs.dir))
        print(f"last save: {save.folder + ' (' + _ago(save.mtime) + ')' if save else '-'}")
        logs = sorted(rs.dir.glob("S*.log"), key=lambda p: p.stat().st_mtime)
        logfile = Path(st["log"]) if st.get("log") and Path(st["log"]).exists() else (logs[-1] if logs else None)
        if logfile:
            print(f"\n--- {logfile} (last 10 lines)")
            print("\n".join(logfile.read_text(errors="replace").splitlines()[-10:]))
    return 0


def cmd_runs(a) -> int:
    """The runs table of a series (or of one playthrough's --until runs) and its clean streak. --backfill first
    adds the benchmark fields (runs.METRIC_FIELDS) records written before them lack, and the model_time keys
    (api_floor, the headline) an older model_time lacks, in place; present values stay. --vs OTHER: the two series'
    means side by side, "API floor differs" when their API floors are > 0.3 s apart."""
    path = RUNTIME / "sessions" / a.playthrough / "runs.jsonl"
    recs = runs.series_records(path, a.playthrough)
    if not recs:
        print(f"no run records for {a.playthrough} ({path})")
        return 1
    if getattr(a, "backfill", False):
        for r in recs:
            state_dir = RUNTIME / "sessions" / (r.get("playthrough") or a.playthrough)
            fields = {**runs.backfill_metrics(state_dir, r), **runs.backfill_model_time(state_dir, r)}
            if fields:
                ok = runs.update_record(path, r, fields)
                print(f"run {r.get('run') or 1} ({r.get('playthrough')}): "
                      f"{'backfilled' if ok else 'NOT written'} {', '.join(fields)}")
        recs = runs.series_records(path, a.playthrough)
    print(runs.runs_table(recs))
    print("\n" + runs.series_summary(recs))
    if getattr(a, "vs", None):
        other = runs.series_records(RUNTIME / "sessions" / a.vs / "runs.jsonl", a.vs)
        print("\n" + (runs.series_compare(a.vs, other, a.playthrough, recs) if other else
                      f"no run records for {a.vs}"))
    if a.failures:
        for r in recs:
            print(f"\nrun {r.get('run') or 1} ({r.get('playthrough')}): {r.get('outcome')}"
                  + (f" ({r['why']})" if r.get("why") else ""))
            for f in r.get("failures") or []:
                when = datetime.fromtimestamp(f["t"]).strftime("%H:%M:%S") if f.get("t") else "--:--:--"
                print(f"  {when} {f.get('class', '?'):8} {f.get('kind')}{' x%d' % f['n'] if f.get('n') else ''}: "
                      f"{f.get('detail')}")
            for o in r.get("operator") or []:
                print(f"  operator: {o.get('what')}")
            k = r.get("knowledge") or {}
            if k.get("agent_edits"):
                print(f"  knowledge/ edits put back: {', '.join(k['agent_edits'])}")
    return 0


def cmd_judge(a) -> int:
    """Re-judge records of a series (or a playthrough's --until runs) in place: --run K, else every record without
    a scored persona. Also fills a missing `model_time`. The judge model: series.json's, else --judge-model /
    $BG3AI_JUDGE_MODEL / judge.DEFAULT_MODEL (then frozen into an existing series.json)."""
    path = RUNTIME / "sessions" / a.playthrough / "runs.jsonl"
    recs = runs.series_records(path, a.playthrough)
    if a.run is not None:
        recs = [r for r in recs if int(r.get("run") or 1) == a.run]
    elif not a.all:
        recs = [r for r in recs if runs.record_persona(r) is None]
    if not recs:
        print(f"no run {'%d ' % a.run if a.run is not None else 'to judge '}in {path}", file=sys.stderr)
        return 1
    frozen = series_meta(a.playthrough).get("judge_model")
    model, err, change = runs.series_judge_model(frozen, a.judge_model or os.environ.get("BG3AI_JUDGE_MODEL"),
                                                 judge.DEFAULT_MODEL, a.allow_judge_change)
    if err:
        print(f"refusing to judge {a.playthrough}: {err}", file=sys.stderr)
        return 2
    if frozen is None and (RUNTIME / "sessions" / a.playthrough / "series.json").exists():
        freeze_series(a.playthrough, 0, None, {}, judge_model=model)
    rc = 0
    for r in recs:
        state_dir = RUNTIME / "sessions" / (r.get("playthrough") or a.playthrough)
        fields = {"persona": judge_record(state_dir, r, model, a.timeout)}
        if change:
            fields["judge_change"] = change
        if "model_time" not in r:
            end = r.get("t")
            fields["model_time"] = runs.model_time_field(model_time.model_time(
                state_dir, since=r.get("started"), until=end + model_time.WINDOW_SLACK_S if end else None))
        ok = runs.update_record(path, r, fields)
        print(f"run {r.get('run') or 1} ({r.get('playthrough')}): {persona_line(fields['persona'])}"
              + ("" if ok else f" (NOT written: the record is gone from {path})"))
        rc = rc if ok and fields["persona"].get("status") in (judge.OK, judge.NO_DECISIONS) else 1
    return rc


def cmd_scenarios(_a) -> int:
    """scenarios/*.json with their status: ready, needs a start save, or invalid."""
    paths = sorted(SCENARIOS.glob("*.json"))
    if not paths:
        print(f"no scenarios in {SCENARIOS}")
        return 1
    saves = list_saves()
    for path in paths:
        try:
            sc = load_scenario_file(path.stem)
        except runs.ScenarioError as e:
            print(f"{path.stem:20} {runs.INVALID}: {e}")
            continue
        status, detail = scenario_status(sc, saves)
        caps = ", ".join(f"{k}={v:g}" for k, v in sc["caps"].items() if v is not None)
        print(f"{sc['name']:20} {status:18} --until {sc['until']}, {sc['runs']} run(s), {caps}; {detail}"
              + (f"\n{'':20} {sc['title']}" if sc.get("title") else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play sessions until stopped")
    r.add_argument("--playthrough", help="the playthrough (a series: its base name); default with --scenario: the "
                   "scenario's name")
    r.add_argument("--scenario", metavar="NAME",
                   help="play scenarios/<NAME>.json as a --runs series: its start (new game or start save), --until "
                        "milestone, caps and profile, frozen in series.json; a flag that contradicts it is refused")
    r.add_argument("--hours", type=float, help="total wall-clock budget (default, or 0: until stopped or the game "
                   "ends; every run stops at the end credits / epilogue, milestone game_end)")
    r.add_argument("--session-minutes", type=float, default=60,
                   help='per-session override, live: session-config.json {"minutes": 60}, re-read before each session')
    r.add_argument("--allow-short-sessions", action="store_true",
                   help=f"allow --session-minutes below {MIN_SESSION_MINUTES} (harness tests only)")
    r.add_argument("--model", default="opus", help="model for play sessions (claude --model)")
    r.add_argument("--effort", choices=EFFORTS, help="effort for play sessions (claude --effort; default: "
                   "Claude Code's). Per-session override: runtime/sessions/<pt>/session-config.json "
                   '{"model": ..., "effort": ...}, re-read before each session')
    r.add_argument("--fix-model", help="model for the fix-notes pass (default: --model)")
    r.add_argument("--fix-effort", choices=EFFORTS, default="low", help="effort for the fix-notes pass")
    r.add_argument("--no-keeper", "--no-memory-keeper", dest="no_memory_keeper", action="store_true",
                   help="skip the memory-keeper pass between sessions (runner/memory_keeper.py)")
    r.add_argument("--keeper-model", default="same",
                   help="model for the memory-keeper pass; 'same' (default) = the session's play model")
    r.add_argument("--keeper-effort", choices=EFFORTS, default="low", help="effort for the memory-keeper pass")
    r.add_argument("--keeper-minutes", type=float, default=5.0, help="time cap of the memory-keeper pass")
    r.add_argument("--fallback-model", help="claude --fallback-model (used when the main model is overloaded)")
    r.add_argument("--autocompact", type=int, default=None,
                   help=f"claude --autocompact <tokens> for play sessions ({AUTOCOMPACT_MIN}-{AUTOCOMPACT_MAX}); "
                        "default: Claude Code's own window. session-config.json \"autocompact\" overrides it")
    r.add_argument("--save-owner", help="character name that prefixes this playthrough's save folders "
                   "('Claude' for Claude-<id>__<label>); default: learned from the first save, or $BG3AI_SAVE_OWNER")
    r.add_argument("--until", choices=[m[0] for m in milestones.MILESTONES], metavar="MILESTONE",
                   help="stop at this story milestone (bridge/src/bg3ai/milestones.py, e.g. nautiloid_escaped, "
                        "crash_site) and append a run record to runtime/sessions/<pt>/runs.jsonl")
    r.add_argument("--runs", type=int, metavar="N",
                   help="with --until: play N runs in a row, each a new playthrough and a new game: <pt>, <pt>-r2, "
                        "... (fresh memory, the same profile.json, per-run reset); records in "
                        "runtime/sessions/<pt>/runs.jsonl, --hours caps the whole series. Re-running resumes it")
    r.add_argument("--run-hours", type=float, metavar="H",
                   help="with --until: cap one run at H hours; then it ends as a timeout (a failure) and the next starts")
    r.add_argument("--max-cost", type=float, metavar="USD",
                   help="with --until: cap one run's claude cost; past it the run ends over_budget (a failure). A "
                        "series keeps its first run's caps (series.json)")
    r.add_argument("--max-cost-per-day", type=float, metavar="USD",
                   help="long runs: pause (no new session) while the play agent's spend in the trailing 24 h is at "
                        "least USD; it plays again once old sessions leave the window (checked between sessions)")
    r.add_argument("--max-tokens", type=int, metavar="N",
                   help="with --until: cap one run's claude tokens (input + output + cache reads + cache writes)")
    r.add_argument("--allow-cap-change", action="store_true",
                   help="--runs: let --run-hours/--max-cost/--max-tokens differ from series.json (recorded)")
    r.add_argument("--judge", action=argparse.BooleanOptionalAction, default=None,
                   help="with --until: after each run, score its in-character decisions against CHARACTER.md "
                        "(runner/judge.py; the judge's cost is recorded apart, never capped). Default: on with --runs "
                        "and --scenario, off for a single run")
    r.add_argument("--judge-model", help="the judge's model (default $BG3AI_JUDGE_MODEL or "
                   f"{judge.DEFAULT_MODEL}); never the model under test. A series keeps its first one (series.json)")
    r.add_argument("--allow-judge-change", action="store_true",
                   help="--runs: let --judge-model / $BG3AI_JUDGE_MODEL differ from series.json (recorded)")
    r.add_argument("--client", choices=sorted(clients.CLIENTS),
                   help="the CLI that plays (runner/clients.py; default claude). A series keeps its first one "
                        "(series.json); codex and gemini are offline-tested only")
    r.add_argument("--sandbox", choices=sandbox.MODES, default="none",
                   help="container: the client plays from a container that reaches only the bg3 MCP server (served "
                        "over http on the host) and its model API (runner/sandbox.py, tools/container/README.md); "
                        "refuses without the host firewall (sudo tools/container/firewall.sh up). Recorded; a series "
                        "keeps its first mode (series.json) and refuses a run in another")
    r.add_argument("--sandbox-unfirewalled", action="store_true",
                   help="--sandbox container without the host firewall (dev only: the container then reaches every "
                        "host port bound to all interfaces); recorded as sandbox_unfirewalled")
    r.add_argument("--allow-sandbox-change", action="store_true",
                   help="--runs: let --sandbox / --sandbox-unfirewalled differ from series.json (recorded)")
    r.add_argument("--allow-client-change", action="store_true",
                   help="--runs: let --client differ from series.json (recorded)")
    r.add_argument("--allow-version-drift", action="store_true",
                   help="--runs: play a run although the game build, bg3le, the mod or the repo differ from the "
                        "series' first run (series.json pins); the drift is recorded")
    r.add_argument("--character-brief", metavar="TEXT",
                   help="who the character is, in your words: written to playthroughs/<pt>/profile.json; a new game "
                        "then starts with a character-creation phase that designs the build and persona from it")
    r.add_argument("--roleplay", choices=ROLEPLAY_MODES,
                   help="how strictly the agent plays its character (profile.json; default strict)")
    r.add_argument("--difficulty", choices=DIFFICULTIES, help="difficulty of a new game (profile.json)")
    r.add_argument("--take-over", action="store_true",
                   help="allow using a game that is already in a save the runner didn't load")
    r.add_argument("--keep-game", action="store_true",
                   help="--until / --runs: leave the harness game running when the run (or the series) ends; by "
                   "default it is stopped like between runs")
    r.add_argument("--restart-run", action="store_true",
                   help="--runs: start the next run over even when it has finished play sessions (its sessions, "
                   "memory and saves record move aside to *.restarted-<time>). Without it a run with no finished "
                   "session whose runner died is restarted anyway; one with finished sessions continues")
    r.add_argument("--plan", action="store_true", help="print the series.json a series would freeze, its next "
                   "run and the first session's command, then exit: no lock, no game, no files written")
    r.add_argument("--dry-run", action="store_true", help="prepare the first session and print the claude "
                   "command instead of running it (still starts the game and loads the save)")
    s = sub.add_parser("status", help="summary of the runner, game and last session")
    s.add_argument("--playthrough")
    sub.add_parser("stop", help="finish the current session, then exit")
    rs = sub.add_parser("runs", help="table of a series' --until runs: outcome, clean, failures by class")
    rs.add_argument("--playthrough", required=True, help="the series (run --runs) or playthrough (run --until)")
    rs.add_argument("--failures", action="store_true", help="also list every run's failures")
    rs.add_argument("--backfill", action="store_true", help="first add the metrics (latency, deaths, fairness "
                    "flags, story, time to milestone, model_time's API floor) old records lack, from their files; "
                    "present values stay")
    rs.add_argument("--vs", metavar="SERIES", help="compare with another series (means; flags an API floor "
                    "difference instead of reading it as a model-time regression)")
    sub.add_parser("scenarios", help="list scenarios/*.json: ready, or needs a start save")
    j = sub.add_parser("judge", help="(re-)score runs' in-character decisions into their runs.jsonl records")
    j.add_argument("--playthrough", required=True, help="the series (run --runs) or playthrough (run --until)")
    j.add_argument("--run", type=int, help="this run only (default: every run without a persona score)")
    j.add_argument("--all", action="store_true", help="re-judge every run, scored ones too")
    j.add_argument("--judge-model", help="default: series.json's, else $BG3AI_JUDGE_MODEL or " + judge.DEFAULT_MODEL)
    j.add_argument("--allow-judge-change", action="store_true", help="judge with another model than series.json's")
    j.add_argument("--timeout", type=float, default=JUDGE_TIMEOUT_S, help="seconds per run (default %(default)s)")
    a = ap.parse_args(argv)
    return {"run": cmd_run, "status": cmd_status, "stop": cmd_stop, "runs": cmd_runs,
            "scenarios": cmd_scenarios, "judge": cmd_judge}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
