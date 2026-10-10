#!/usr/bin/env python3
"""Autonomous session runner: keeps an AI playing BG3 for hours, one ~1 h Claude Code session at a time.

    runner/play.py run --playthrough NAME [--hours N] [--session-minutes 60] [--model opus] [--effort medium]
    runner/play.py status [--playthrough NAME]
    runner/play.py stop                  # finish the current session, then exit

Per session: make sure the game is up with this playthrough's save loaded, launch `claude -p` in play/
(the SessionStart hook opens the memory session, the Stop hook lints and commits it), stream its output
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
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bridge" / "src"))
sys.path.insert(0, str(REPO / "tools" / "memory"))
from bg3ai import metrics as latency  # noqa: E402  (stdlib only; doesn't import the MCP server)
from bg3ai import progress as story  # noqa: E402  (stdlib only: story progress per hour, stuck signal)
sys.path.insert(0, str(REPO / "runner"))
import triage  # noqa: E402  (runner/triage.py: post-session Sxxx.triage.md)
import ramwatch  # noqa: E402  (runner/ramwatch.py: RAM watchdog, RSS spikes with the last tool calls)
sys.path.insert(0, str(REPO / "tools"))
import steam_paths  # noqa: E402  (stdlib: Steam's offline state before any game launch)

PLAY_DIR = REPO / "play"
PLAYTHROUGHS = Path(os.environ.get("BG3AI_PLAYTHROUGHS", REPO / "playthroughs"))
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
SESSION_PY = REPO / "tools" / "memory" / "session.py"
LAUNCH_SH = REPO / "runner" / "launch_game.sh"
CHECK_ALL = REPO / "tools" / "dev" / "check_all.py"
CHECK_PLANS = REPO / "tools" / "dev" / "check_plans.py"   # party-build.md plan lines vs the game's level-up offers
CHECK_PLANS_TIMEOUT = 90
SAVE_DIR = Path(os.environ.get(
    "BG3AI_SAVE_DIR",
    Path.home() / ".local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story"))
DEBUG_HOST = "127.0.0.1"
DEBUG_PORT = int(os.environ.get("BG3LE_DEBUG_PORT", "9998"))
SAVE_PREFIX = os.environ.get("BG3AI_SAVE_PREFIX", "")

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
STEAM_RETRY_S = 120            # Steam not offline: refuse to launch, retry this often (never give up)
# Party ready checks (long_rest's "end the day?", region fast travel) stop working in a long-lived game process (se's
# discriminator 2026-10-10 18:07-18:18, research/impl-se.md §6: the 12:48 process failed from S028 on, fresh processes
# work with or without the mod). Between sessions the runner relaunches a game older than this (0 = never by age), or
# one whose last session logged a ready-check failure, and loads the playthrough's newest save.
GAME_MAX_AGE_H = float(os.environ.get("BG3AI_GAME_MAX_AGE_H", "2"))
READY_FAIL_RE = re.compile(r"\b(?:ready_failed|end_day_failed)\b(?::\w+)?")
GAME_FAULTS = ("crash", "hang")
FAULTS = GAME_FAULTS + ("agent_hang",)
KILLED_EXITS = (-2, -9, -15, 130, 137, 143)   # claude ended by a signal (SIGINT/KILL/TERM, raw or as 128+n)


def exit_reason(reason: str, returncode: int | None) -> str:
    """'killed' when claude died of a signal the runner didn't send (S027: an outside SIGTERM at 3 min was taken
    for a clean exit, so the end lint demanded a handoff and a fix-notes session ran); else `reason`."""
    return "killed" if reason == "exited" and returncode in KILLED_EXITS else reason
STUCK_SESSIONS = 2
MAX_QUICK_FAILURES = 3         # claude exiting within a minute this many times in a row → give up
MAX_RESUMES = 3                # game crashes per session that resume the same claude conversation
RESUME_MIN_MINUTES = 8         # less than this left after a crash: end the session, the next one picks up
MAX_SETUP_FAILURES = 5         # game launch/load failing this many times in a row → give up
LAUNCH_ATTEMPTS = 3            # a launch that dies before the save plays is retried; the 3rd try loads the previous
LAUNCH_BACKOFF = (15, 45)      # good save; then the runner stops (alert). Seconds to wait before the 2nd and 3rd try
MENU_STATES = ("Menu", "LoadMenu")   # client game states at the main menu or its splash ("Press any key")
SPLASH_LOADMENU_S = 10         # LoadMenu this long without the main menu = the splash waiting for a key
MENU_TIMEOUT = 180             # debug port up → main menu (splash pressed past), else wait_menu fails
MENU_POLL = 3                  # seconds between menu/splash checks
LOAD_START_TIMEOUT = 60        # ui.menu load → the game leaves the menu, else the load never started
PORT_DEAD_S = 60               # bg3 up this long with port 9998 refusing = dead (bg3le listens ~1 s after start)
BG3_APP_ENV = b"SteamAppId=1086940"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Flags for every playing session. Keep the cacheable prefix (system prompt + tool schemas) byte-identical
# across sessions: per-machine sections (cwd, git status, env info) move into the first user message, and the
# operator's user-level settings (hooks, plugins) stay out of the playing agent.
# No skills at all (bundled, synced or project): the playing agent's procedures are read-only playbooks in
# play/knowledge/playbooks/, which play/CLAUDE.md says when to read.
CLAUDE_FLAGS = ["--exclude-dynamic-system-prompt-sections", "--setting-sources", "project,local",
                "--disable-slash-commands"]


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


def pick_save(saves: list[Save], handoff_label: str | None, owner: str | None,
              runner_saves: set[str], prefix: str = SAVE_PREFIX) -> Save | None:
    """The newest save of this playthrough.

    Candidates: the folder HANDOFF names, any `Sxxx-` save of the playthrough's owner, and quicksaves the
    runner itself made for this playthrough (recorded by folder). Never anything else.
    """
    cands = []
    for s in saves:
        if s.folder in runner_saves:
            cands.append(s)
        elif is_autosave(s.label):
            continue  # AutoSave_N/QuickSave_N: the game's or a worker's test state (se 18:14 AutoSave_59), never ours
        elif owner is not None and s.owner != owner:
            continue
        elif handoff_label and label_matches(s.label, handoff_label, prefix):
            cands.append(s)
        elif owner is not None and is_ai_save(s, prefix):
            cands.append(s)
    return max(cands, key=lambda s: s.mtime, default=None)


def saves_since(saves: list[Save], t: float, owner: str | None, prefix: str = SAVE_PREFIX) -> list[Save]:
    """AI-labelled saves written after t (by this playthrough's owner, when known)."""
    return sorted((s for s in saves if s.mtime >= t and is_ai_save(s, prefix)
                   and (owner is None or s.owner == owner)), key=lambda s: s.mtime)


def relaunch_reason(started: float | None, now: float, last_log: str | None,
                    max_age_h: float = GAME_MAX_AGE_H) -> str | None:
    """Why the game process should be relaunched before the next session, or None: the last session's log shows a
    party ready check failing (ready_failed / end_day_failed), or the process is max_age_h or more old."""
    m = READY_FAIL_RE.search(last_log or "")
    if m:
        return f"the last session logged {m.group(0)} (ready checks break in a long-lived game process)"
    if started and max_age_h > 0 and now - started >= max_age_h * 3600:
        return f"the game process is {(now - started) / 3600:.1f} h old (relaunched at {max_age_h:g} h: ready checks " \
               "break in a long-lived process)"
    return None


def is_autosave(label: str) -> bool:
    return re.match(r"(?i)^(auto|quick)save_\d+$", label) is not None


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


def made_progress(start_sig: dict | None, end_sig: dict | None) -> bool | None:
    """True/False, or None when it can't be told (a signature is missing)."""
    if start_sig is None or end_sig is None:
        return None
    return start_sig != end_sig


def story_points(summary_path: Path) -> int | None:
    """Story progress points of a finished session (Sxxx.progress.json), or None."""
    try:
        return json.loads(summary_path.read_text()).get("points")
    except (OSError, ValueError, AttributeError):
        return None


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
    look() / get_frontier() / Edit your session log  -> while it walks
    wait_for("queue")                              -> only now, if nothing else is left to do
- Never pass wait=true to move_to, walk_toward or travel_to, not even after a walk stopped or
  failed (failures come back at once anyway), and not because you did it earlier this session. Never
  wait() to pass time: wait_for("my_turn" | "idle" | a job id).
- Stuck (a loop, a bugged action, a route that keeps failing) never ends the session: follow
  knowledge/playbooks/stuck.md section 0 (cancel and retry differently, reload the last good save, lead
  with another party member, another objective) and keep playing until the ⏱ line says WRAP UP NOW."""


def build_kickoff(sid: str, playthrough: str, deadline: float, loaded_save: str | None,
                  notes: list[str]) -> str:
    end = datetime.fromtimestamp(deadline).strftime("%H:%M")
    lines = [
        f"This is session {sid} of the BG3 playthrough '{playthrough}', started by the autonomous runner.",
        "Your memory injection (PLAYTHROUGH.md and HANDOFF.md) is above. Run the session-start playbook (Read "
        "knowledge/playbooks/session-start.md), then "
        "play following CLAUDE.md.",
        f"Session deadline: {end} local time (the session_clock tool shows the time left). Run the "
        "session-wrapup playbook when the SESSION CLOCK reminder appears, and end your turn when it reports "
        "the commit. Nobody will answer questions: decide yourself and write down why.",
        PACE_PREFLIGHT,
    ]
    if loaded_save:
        lines.append(f"The runner has already loaded save \"{loaded_save}\". Don't reload it; verify state "
                     "with get_status.")
    for note in notes:
        lines.append(note)
    return "\n\n".join(lines)


def resume_prompt(why: str, deadline: float, notes: list[str]) -> str:
    """The message that resumes a session's claude conversation after the runner recovered a crashed game."""
    end = datetime.fromtimestamp(deadline).strftime("%H:%M")
    lines = [f"RUNNER: the game {why}. The runner relaunched it and loaded the latest save; the same session "
             f"continues (deadline still {end}). Call get_status first: anything after that save is lost, so redo it "
             "and note the crash in the session log in one line. Then keep playing following CLAUDE.md."]
    return "\n\n".join(lines + notes)


def agent_resume_prompt(deadline: float, notes: list[str]) -> str:
    """The message that resumes a session whose claude process went silent and was stopped by the runner."""
    end = datetime.fromtimestamp(deadline).strftime("%H:%M")
    lines = [f"RUNNER: your previous turn produced no output for {AGENT_SILENT_MINUTES:g} min, so the runner stopped "
             f"and resumed you; the game kept running. Same session (deadline still {end}). Call get_status, then "
             "keep playing following CLAUDE.md; keep each step short (no tool call or think longer than a minute)."]
    return "\n\n".join(lines + notes)


def new_game_note(owner: str) -> str:
    return (f"RUNNER NOTE: this is a new game. Name your character exactly \"{owner}\" (create_character plan "
            f"\"name\": \"{owner}\"; it is set and verified before Venture forth). Saves are matched to this "
            "playthrough by that name; if create_character reports the name wasn't set, don't finish character "
            "creation another way.")


def fix_notes_prompt(lint_report: str) -> str:
    return ("The session is over, but `session.py end` refused to commit your memory because lint "
            "failed. Do NOT play or touch the game. Only fix the memory files so lint passes, then end your "
            f"turn. You have {FIX_SESSION_MINUTES} minutes.\n\nLint report:\n{lint_report[-6000:]}")


STUCK_DIRECTIVE = (
    "RUNNER DIRECTIVE, CHANGE APPROACH: the last {n} sessions ended with no measurable progress "
    "(same level, region, party levels/XP and journal: {what}). What you've been doing isn't working. "
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


def format_stream_event(ev: dict) -> list[str]:
    """Human-readable lines for one stream-json event (assistant text, tool calls, results)."""
    t = ev.get("type")
    out = []
    if t == "system" and ev.get("subtype") == "init":
        servers = [f"{m.get('name')}:{m.get('status')}" for m in ev.get("mcp_servers") or [] if isinstance(m, dict)]
        out.append(f"[init] session={ev.get('session_id')} model={ev.get('model')} "
                   f"tools={len(ev.get('tools') or [])} mcp={servers}")
    elif t == "system" and ev.get("subtype") in ("compact_boundary",):
        out.append("[compact] context compacted")
    elif t == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(f"[say] {_short(c['text'].strip(), 2000)}")
            elif c.get("type") == "tool_use":
                name = c.get("name", "?").removeprefix("mcp__bg3__")
                out.append(f"[tool] {name} {_short(c.get('input') or {}, 300)}")
    elif t == "user":
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", f"<{b.get('type')}>") for b in body if isinstance(b, dict))
                tag = "[error]" if c.get("is_error") else "[result]"
                out.append(f"{tag} {_short(body or '', 400)}")
    elif t == "result":
        out.append(f"[end] {ev.get('subtype')} turns={ev.get('num_turns')} "
                   f"duration={round((ev.get('duration_ms') or 0) / 60000, 1)}min "
                   f"cost=${ev.get('total_cost_usd', 0) or 0:.2f}")
    return out


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


PLAY_SETTINGS = [
    # (op, args) applied before every session; cutscene autoskip is a user requirement.
    ("ui.config", {"autoskip": True}),
    ("ui.settings", {"show_tutorials": False}),
    ("ui.dismiss", {}),
]

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
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    finally:
        path.unlink(missing_ok=True)
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

class RpcError(Exception):
    pass


class SteamNotOffline(RuntimeError):
    """Steam isn't in offline mode: launching BG3 could kick the account's other machine off. Never launch."""


class GameDied(RuntimeError):
    """The game died while the runner waited on it (crash, crash reporter, bg3 gone, dead debug port, launcher exit)."""


class LaunchGaveUp(Exception):
    """LAUNCH_ATTEMPTS launches in a row died before the save played: the runner stops (not a RuntimeError, so no
    retry loop swallows it)."""


def steam_ready() -> tuple[bool, str]:
    """(ok to launch BG3?, why): Steam's last login was offline and a Steam client is running.
    $BG3AI_ALLOW_STEAM_ONLINE=1 skips the offline requirement (a machine where online is fine)."""
    if os.environ.get("BG3AI_ALLOW_STEAM_ONLINE") == "1":
        ok, why = True, "BG3AI_ALLOW_STEAM_ONLINE=1"
    else:
        ok, why = steam_paths.steam_offline()
    if not ok:
        return False, why
    r = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, text=True)
    if not r.stdout.strip():
        # libsteam_api would start Steam itself, maybe online
        return False, f"Steam isn't running ({why})"
    return True, why


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
        lsv = Path(e.path) / f"{e.name}.lsv"
        try:
            mtime = lsv.stat().st_mtime if lsv.exists() else e.stat().st_mtime
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


def pids_of_game() -> list[int]:
    """The native game processes (./bin/bg3), not the pressure-vessel wrapper."""
    r = subprocess.run(["pgrep", "-x", "bg3"], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


def crash_reporter_pids() -> list[int]:
    """BG3's LinuxCrashReporter processes. One starts after the game crashes and keeps the launcher alive (S014:
    the runner then waited 10 min for a dead game). Only reporters with the game's SteamAppId (or its install path)."""
    r = subprocess.run(["pgrep", "-f", "LinuxCrashReporter"], capture_output=True, text=True)
    out = []
    for p in r.stdout.split():
        try:
            env = Path(f"/proc/{p}/environ").read_bytes().split(b"\0")
            cmd = Path(f"/proc/{p}/cmdline").read_bytes()
        except OSError:
            continue
        if BG3_APP_ENV in env or b"Baldurs Gate 3" in cmd:
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
    keeper_model: str = "sonnet"
    keeper_effort: str | None = "low"
    keeper_minutes: float = 5.0
    autocompact: int | None = None     # claude --autocompact <tokens> (play sessions); None = Claude Code's default


# claude --autocompact accepts 100k-1M tokens. Context-cost model (tools/logs/context_model.py, S021-S026): a
# compaction is a ~1-2 min summary stall (a >10 s gap) and a generic summary, so shorter sessions with our own
# handoff are the default lever; this is an opt-in safety net / A-B switch (e.g. 350000 with 30-min sessions).
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000


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
    reason: str          # exited | hard_stop | crash | hang | agent_hang | aborted | killed | dry_run
    started: float
    ended: float
    notes: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    detail: str = ""     # what the runner saw, for a fault
    fault_at: float | None = None


class Runner:
    watch: LaunchWatch | None = None   # crash signs of the game the runner launched (see launch_game)

    def __init__(self, opt: Options):
        self.opt = opt
        self.mem = PLAYTHROUGHS / opt.playthrough / "memory"
        self.state = RunnerState(opt.playthrough)
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
        g = self.state["game"] or {}
        # The game a previous run of the runner launched: keep watching its bg3le logs.
        self.watch = LaunchWatch(g["stamp"], RUNTIME / "logs", g.get("launched")) if g.get("stamp") else None

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

    def owns_game(self) -> bool:
        """True if the running game is one this runner (or a previous run of it) launched."""
        if self.game_proc is not None and self.game_proc.poll() is None:
            return True
        pg = (self.state["game"] or {}).get("pgid")
        if not pg:
            return False
        try:   # after a self-restart (execv) the game is still our child: reap it if it exited, or it stays a zombie
            os.waitpid(pg, os.WNOHANG)
        except OSError:
            pass
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
            log(f"!!!!! REFUSING TO LAUNCH BG3: Steam is not offline: {why}. Put Steam in offline mode "
                "(Steam > Go Offline...) and leave it running; the runner keeps retrying. !!!!!")
            self.event("steam_refused", why=why)
            raise SteamNotOffline(why)
        if port_open():
            raise RuntimeError(f"port {DEBUG_PORT} is still in use; can't launch another game")
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
        self.state["game"] = {"pgid": self.game_proc.pid, "launched": now, "load": load_folder, "stamp": stamp}
        self.state["mod_hashes"] = mod_tree_hashes()  # a fresh game loads the mod as it is now
        write_status(game_pgid=self.game_proc.pid)

    def kill_game(self) -> None:
        """Stop the game: SIGTERM, then SIGKILL after KILL_GRACE s (bg3 ignores SIGTERM), on both the
        launcher's process group and every bg3 process; then require port 9998 to be free."""
        log("killing the game")
        pg = (self.state["game"] or {}).get("pgid")
        groups = []
        if self.game_proc is not None and self.game_proc.poll() is None:
            groups.append(self.game_proc.pid)
        elif pg:
            groups.append(pg)

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

    def choose_save(self) -> Save | None:
        handoff = self.mem / "HANDOFF.md"
        label = parse_handoff_save(handoff.read_text()) if handoff.exists() else None
        saves = list_saves()
        save = pick_save(saves, label, self.state["save_owner"], set(self.state["runner_saves"]))
        blocked = set(self.state["oom_blocked"] or [])
        if save and save.folder in blocked:
            save = self.refuse_oom_save(save, saves, blocked)
        if save and not self.state["save_owner"] and not self.opt.dry_run:
            self.state["save_owner"] = save.owner
        return save

    def refuse_oom_save(self, save: Save, saves: list[Save], blocked: set[str]) -> Save:
        """The newest save OOM-killed the game OOM_SAVE_LIMIT times in a row right after loading: load the newest
        other save of the playthrough (loudly), or stop the runner when there is none (a crash loop needs a human)."""
        alt = pick_save([s for s in saves if s.folder not in blocked], None, self.state["save_owner"],
                        set(self.state["runner_saves"]))
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
        save = self.state["loaded_save"]
        streak = oom_streak_update(self.state["oom_streak"], save, oom)
        if streak:
            spikes = read_spikes(launched)
            acts = read_actions_since(launched)
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
        return foreign_use_since(save, list_saves(), read_actions_since(save.mtime), started,
                                 set(self.state["runner_saves"]))

    def load_save(self, save: Save | None) -> str | None:
        """Bring the game to this playthrough's latest save (or the main menu for a new game)."""
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
            if owner:
                # Saves are matched to this playthrough by the character's name: a "Tav" breaks that.
                self.pending_notes.append(new_game_note(owner))
            return None
        if port_open() and game_state() == "Running" and server_ready() and self.opt.take_over:
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
        if port_open() and ui_load:
            state = game_state()
            if state != "Running" and not self.wait_menu():   # splash included: press past it first
                raise RuntimeError(f"the main menu never came up (state {game_state()})")
            # ui.menu load works from the main menu and from in-game (it opens the load screen itself).
            rpc("ui.menu", load_menu_args(save, list_saves()), context="client", timeout=30)
            # Wait for Running only once the load really started. From in-game the state stays Running until the
            # load begins (S002's agent started mid-load); from the menu a load that never starts is an error,
            # not a 10-minute wait.
            here = ("Running",) if state == "Running" else MENU_STATES
            if not self.wait_for("the load to start", lambda: game_state() not in here, LOAD_START_TIMEOUT, 0.5):
                raise RuntimeError(f"the load of {save.folder} never started (state {game_state()})")
        else:
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
        saves = [s for s in list_saves() if s.folder not in failed]
        good = self.state["loaded_save"]
        hit = next((s for s in saves if s.folder == good), None)
        return hit or pick_save(saves, None, self.state["save_owner"], set(self.state["runner_saves"]))

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

    def maybe_relaunch(self, first: bool) -> str | None:
        """Between sessions: kill a game process that relaunch_reason() condemns, so ensure_game() launches a fresh
        one with the newest save. Never kills when the relaunch can't happen (Steam not offline: the launch guard
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
        why = relaunch_reason(started, time.time(), text)
        if why is None:
            if started:
                log(f"game process {(time.time() - started) / 3600:.1f} h old, no ready-check failure last session: "
                    f"kept (relaunch at {GAME_MAX_AGE_H:g} h)")
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
            if not first or owned:
                return None  # keep playing where the last session left off
        if port_open() and state is None:
            raise RuntimeError("debug port open but the game doesn't answer")
        if state is None:
            self.note_game_gone(self.check_game_oom())   # gone (e.g. 20:48: an OOM took the runner down with it)
        return self.load_save(self.choose_save())

    def apply_play_settings(self) -> None:
        """Best effort, never fatal: autoskip, no tutorials, dismiss popups, then the save/load gates."""
        for op, args in PLAY_SETTINGS:
            try:
                rpc(op, args, context="client")
            except Exception as e:  # noqa: BLE001
                log(f"play settings: {op} {args} failed: {e}")
        name = None
        if not self.new_game:
            name = host_character_name()
            if name is None:
                log("play settings: couldn't read the avatar's name")
        owner = self.opt.save_owner or self.state["save_owner"]
        allow = gates_decision(name, owner, self.new_game)
        if allow is None:
            log(f"save/load gates left unchanged (character {name!r}, save owner {owner!r})")
            return
        try:
            rpc("ui.config", {"save_enabled": allow, "load_enabled": allow}, context="client")
            log(f"save/load gates {'enabled' if allow else 'DISABLED'} "
                f"({'new game' if self.new_game else f'character {name!r}, save owner {owner!r}'})")
        except Exception as e:  # noqa: BLE001
            log(f"play settings: ui.config gates failed: {e}")

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
        if not self.game_alive():
            oom = self.check_game_oom()
            if oom:
                why = f"{why} (out of memory: {oom})"
        crashed_at = time.time()
        self.kill_game()
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
                self.state["save_owner"] = new[-1].owner
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

    def claude_cmd(self, prompt: str, claude_session: str, resume: bool, tag: str = "") -> list[str]:
        model, effort = self.model_for(tag)
        cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json",
               "--verbose", "--strict-mcp-config", "--mcp-config", str(PLAY_DIR / ".mcp.json"), *CLAUDE_FLAGS]
        if effort:
            cmd += ["--effort", effort]
        if self.opt.fallback_model:
            cmd += ["--fallback-model", self.opt.fallback_model]
        if tag != "-fix":
            state = getattr(self, "state", None)
            ac = (session_overrides(state.dir / "session-config.json") if state else {}).get(
                "autocompact", self.opt.autocompact)
            if ac:
                cmd += ["--autocompact", str(ac)]
        cmd += ["--resume", claude_session] if resume else ["--session-id", claude_session]
        return cmd

    def run_claude(self, sid: str, prompt: str, minutes: float, claude_session: str,
                   resume: bool = False, tag: str = "", needs_game: bool = True) -> SessionResult:
        """needs_game=False (the fix-notes pass edits memory only): a game crash meanwhile doesn't stop it (S027-fix
        was killed 30 s in by the OOM kill of the game); the next session's setup relaunches the game."""
        start = time.time()
        deadline, hard_stop = deadlines(start, minutes)
        env = dict(os.environ,
                   BG3AI_SESSION_DEADLINE=str(int(deadline)),
                   BG3AI_PLAYTHROUGH=self.opt.playthrough,
                   BG3AI_SAVE_PREFIX=SAVE_PREFIX,
                   BG3_SESSION_MINUTES=str(minutes),
                   BG3_WRAP_MINUTES=str(wrap_minutes(minutes)),
                   # when the MCP clock line switches to WRAP UP (epoch seconds); play until then
                   BG3AI_SESSION_WRAPUP=str(int(start + wrap_minutes(minutes) * 60)),
                   # The playthrough has its own memory system; Claude Code's auto-memory would add a second one
                   # (and a per-machine path in the prompt).
                   CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        base = self.state.dir / f"{sid}{tag}"
        env["BG3AI_METRICS_PATH"] = f"{base}.metrics.jsonl"  # the bridge's per-tool latency records
        # the memory keeper compacts old logs between sessions: session.py then doesn't ask the player to
        env["BG3AI_KEEPER"] = "1" if self.opt.keeper and not self.opt.dry_run else "0"
        if not needs_game:
            env["BG3AI_FIX_NOTES"] = "1"   # the Stop hook lints + commits instead of "keep playing"
        cmd = self.claude_cmd(prompt, claude_session, resume, tag)
        model, effort = self.model_for(tag)
        write_status(playthrough=self.opt.playthrough, session=sid + tag, claude_session=claude_session,
                     model=model, effort=effort or "default",
                     started=start, deadline=deadline, hard_stop=hard_stop, log=str(base) + ".log",
                     metrics=f"{base}.metrics.jsonl", turns=f"{base}.turns.jsonl",
                     progress=f"{base}.progress.jsonl", session_ended=None)
        log(f"{sid}{tag}: claude starting ({model}, effort {effort or 'default'}; deadline {datetime.fromtimestamp(deadline):%H:%M}, "
            f"hard stop {datetime.fromtimestamp(hard_stop):%H:%M})")
        if self.opt.dry_run:
            print(json.dumps({"cmd": cmd, "cwd": str(PLAY_DIR), "deadline": deadline}, indent=1))
            return SessionResult(sid, claude_session, 0, "dry_run", start, start, model=model, effort=effort)

        jsonl = open(f"{base}.jsonl", "a")
        human = open(f"{base}.log", "a", buffering=1)
        human.write(f"===== {sid}{tag} {datetime.now():%Y-%m-%d %H:%M:%S} claude session {claude_session}\n")
        human.write("[prompt] " + _short(prompt, 4000) + "\n")
        proc = subprocess.Popen(cmd, cwd=PLAY_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, text=True, bufsize=1)

        timing = latency.StreamTiming(Path(f"{base}.turns.jsonl"))
        last_output = [time.time()]  # the agent-hang watchdog: any stream line counts as alive

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
                for text in format_stream_event(ev):
                    human.write(f"{stamp} {text}\n")
                try:
                    timing.on_event(ev)
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
        reason = "exited"
        detail, fault_at = "", None
        signals_sent = []
        while proc.poll() is None:
            time.sleep(2)
            now = time.time()
            if self.abort and reason == "exited":
                reason = "aborted"
                hard_stop = min(hard_stop, now)
            if needs_game and now >= next_health and reason == "exited":
                alive = self.game_alive()
                ok = alive and game_state(HEALTH_EVAL_TIMEOUT) is not None
                health.record(ok)
                next_health = time.time() + (HEALTH_INTERVAL if ok else HEALTH_RETRY_INTERVAL)
                write_status(heartbeat=time.time())
                if not alive or health.hung:
                    reason = "crash" if not alive else "hang"
                    detail, fault_at = self.fault_detail(health), now
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
        reason = exit_reason(reason, proc.returncode)
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
        return SessionResult(sid, claude_session, proc.returncode, reason, start, end, model=model, effort=effort,
                             detail=detail, fault_at=fault_at)

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
            except Exception as e:  # noqa: BLE001 - progress never breaks a session
                log(f"{session}: progress poll failed: {e}")

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
                              capture_output=True, text=True, cwd=PLAY_DIR)

    def close_memory_session(self, res: SessionResult) -> None:
        st = self.memory_state()
        if not st.get("open"):
            log(f"{res.sid}: memory already committed by the Stop hook")
            return
        r = self.session_py("end")
        if r.returncode == 0:
            log(f"{res.sid}: memory committed by session.py end")
            return
        if res.reason in ("exited", "hard_stop") and res.claude_session and not (self.stop_requested or self.abort):
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
        if not self.opt.keeper or self.opt.dry_run or res.ended - res.started < 120:
            if plan_bad:   # no keeper pass to fix them: the next player gets the note
                self.pending_notes.append(plan_note(plan_bad))
            return
        import memory_keeper as K
        n = int(sid[1:])
        base = self.state.dir / f"{sid}-keeper"
        model, effort, minutes = self.opt.keeper_model, self.opt.keeper_effort, self.opt.keeper_minutes

        def work():
            log(f"{sid}: memory keeper starting ({model}, effort {effort or 'default'}, ≤{minutes:g} min)")
            try:   # stored level-up plan lines the game refused this session: the keeper rewrites them
                refused = K.refused_plans(read_actions_since(res.started))
            except Exception as e:  # noqa: BLE001 - never stops the keeper
                refused = []
                log(f"{sid}: refused plan lines not read: {e!r}")
            seen = {(x["name"].lower(), x["level"]) for x in refused}
            refused += [b for b in plan_bad or [] if (b["name"].lower(), b["level"]) not in seen]
            if refused:
                log(f"{sid}: refused plan lines for the keeper: {', '.join(x['name'] + ' L' + str(x['level']) for x in refused)}")
            r = K.run_keeper(self.mem, n, base, model, effort, minutes, refused=refused)
            log(r.summary())
            self.event("memory_keeper", session=sid, status=r.status, seconds=r.seconds, files=r.files,
                       commit=r.commit, timed_out=r.timed_out, reverted=r.reverted, lint_errors=r.lint_errors[:5])
            try:
                with open(self.state.dir / "keeper_history.jsonl", "a") as f:
                    f.write(json.dumps({"t": round(time.time(), 1), **r.__dict__}) + "\n")
            except OSError as e:
                log(f"keeper history write failed: {e}")

        self.keeper_thread = threading.Thread(target=work, name="memory-keeper", daemon=True)
        self.keeper_thread.start()

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
        print(f"save to load:   {save.folder if save else '(none: new game from the main menu)'}")
        print(f"game:           {'up, state ' + str(game_state()) if port_open() else 'not running'}")
        if save and self.opt.take_over and port_open():
            touched = self.foreign_use(save)
            print(f"take-over:      {'reload ' + save.label + ' (' + touched + ')' if touched else 'play on from the running game'}")
        print(f"stuck:          {is_stuck(self.state['history'])}")
        k = self.opt
        print(f"memory keeper:  {'on (' + k.keeper_model + ', effort ' + str(k.keeper_effort) + ', ≤' + format(k.keeper_minutes, 'g') + ' min) after each session' if k.keeper else 'OFF (--no-memory-keeper)'}")
        print("plan check:     tools/dev/check_plans.py at each session end (→ keeper) and in the prep (→ RUNNER NOTE); now:")
        self.check_plan_lines("dry run", dry=True)
        prompt = build_kickoff(sid, self.opt.playthrough, time.time() + minutes * 60,
                               save.label if save else None, list(self.pending_notes))
        return self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))

    def play_session(self, minutes: float, first: bool) -> SessionResult:
        if self.opt.dry_run:
            return self.plan_dry_run(minutes)
        relaunched = self.maybe_relaunch(first)
        loaded = self.ensure_game(first)
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
        sid = self.next_sid()
        start_sig = read_progress()
        prompt = build_kickoff(sid, self.opt.playthrough, time.time() + minutes * 60, loaded, notes)
        res = self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))
        if res.reason == "dry_run":
            return res
        session_start, session_end = res.started, res.started + minutes * 60
        if res.reason not in FAULTS and self.state["oom_streak"]:
            self.state["oom_streak"] = None   # a whole session from that save without dying: not an OOM loop

        # Game crash or hang mid-session: relaunch, load the latest save, and resume the SAME claude conversation
        # for the time left (an hour-long session that crashes at minute 10 keeps its context and its 50 min).
        # Agent hang (claude silent too long): the game is fine, so only claude is restarted (resumed).
        resumes = 0
        gave_up: LaunchGaveUp | None = None
        while res.reason in FAULTS:
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
                except (RuntimeError, OSError, TimeoutError) as e:
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
            prompt = (agent_resume_prompt(session_end, notes) if game_ok
                      else resume_prompt(why, session_end, notes))
            res = self.run_claude(sid, prompt, left, res.claude_session, resume=True, tag=f"-r{resumes}")
        crashed = res.reason in GAME_FAULTS
        res = replace(res, started=session_start)
        if not crashed:
            self.ensure_save(res.started)
        self.close_memory_session(res)
        plan_bad = [] if crashed else self.check_plan_lines(f"{sid} end")   # the keeper rewrites invalid lines
        self.start_keeper(sid, res, plan_bad)
        end_sig = read_progress()
        entry = {
            "kind": "play", "session": sid, "claude_session": res.claude_session, "reason": res.reason,
            "exit": res.exit_code, "started": res.started, "ended": res.ended,
            "model": res.model, "effort": res.effort,
            "minutes": round((res.ended - res.started) / 60, 1), "start_sig": start_sig, "end_sig": end_sig,
            "progress": None if crashed else made_progress(start_sig, end_sig),
            "started_at_game_over": screen == "game_over", "resumes": resumes,
            "story_points": story_points(self.state.dir / f"{sid}.progress.json"),
        }
        early = early_exit_minutes(res.reason, res.started, res.ended, minutes)
        if early is not None:
            entry["early_exit_min"] = early
            log(f"{sid}: early exit: claude ended with {early:g} of {minutes:g} min left (exit {res.exit_code}); "
                f"starting the next session now")
        self.state["history"] = self.state["history"] + [entry]
        self.new_game = False  # from here on the game has a character: confirm by name
        if is_stuck(self.state["history"]):
            log(f"stuck detector: {STUCK_SESSIONS} sessions without progress; next kickoff says change approach")
        if gave_up is not None:
            raise gave_up
        return res

    # ---------------------------------------------------------------- main loop

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
                "last_triage": [self.last_triage[0], str(self.last_triage[1])] if self.last_triage else None}

    def run(self) -> int:
        self.check_memory()
        h = self.handover or {}
        run_started = h.get("run_started") or time.time()
        if h:   # same run, new code: keep its --hours budget; the game is the one this run was playing
            run_end = h.get("run_end")
            log(f"runner restarted in place with the new code (pid {os.getpid()})")
            self.event("runner_restarted", pid=os.getpid())
        else:
            run_end = time.time() + self.opt.hours * 3600 if self.opt.hours else None
            self.event("runner_start", pid=os.getpid())
        write_status(playthrough=self.opt.playthrough, runner_pid=os.getpid(), run_end=run_end,
                     session=None, deadline=None, run_started=run_started, heartbeat=time.time(), alert=None)
        first = not h
        setup_failures = 0
        stop_why = "stopped"
        restarting = False
        try:
            while not self.stop_requested and not (RUNTIME / "STOP").exists():
                self.code_update_check()
                session_minutes = self.session_minutes()
                minutes = session_budget(time.time(), run_end, session_minutes,
                                         min(MIN_SESSION_MINUTES, session_minutes))
                if minutes is None:
                    log("--hours budget used up")
                    break
                try:
                    res = self.play_session(minutes, first)
                except SteamNotOffline as e:
                    # Never a give-up: an operator puts Steam back offline and the run continues by itself.
                    log(f"session setup refused: Steam not offline ({e}); retrying in {STEAM_RETRY_S} s")
                    if self.abort:
                        break
                    time.sleep(STEAM_RETRY_S)
                    continue
                except (RuntimeError, OSError, TimeoutError) as e:
                    log(f"session setup failed: {e}")
                    self.event("setup_failed", error=str(e)[:300])
                    if self.abort:
                        break
                    setup_failures += 1
                    if setup_failures >= MAX_SETUP_FAILURES:
                        log(f"{setup_failures} setup failures in a row; giving up")
                        stop_why = "gave up: setup failures"
                        return 1
                    try:
                        self.recover(f"failed to start ({e})")
                        self.event("recovered", fault="setup", how=f"game relaunched, loaded "
                                   f"{self.state['loaded_save'] or 'main menu'}")
                    except LaunchGaveUp:
                        raise
                    except Exception as e2:  # noqa: BLE001
                        log(f"recovery failed: {e2}; waiting 2 minutes")
                        if not isinstance(e2, SteamNotOffline):
                            self.event("recovery_failed", fault="setup", error=str(e2)[:300])
                        time.sleep(120)
                    continue
                first = False
                setup_failures = 0
                if res.reason == "dry_run":
                    break
                if res.ended - res.started < 60 and res.reason == "exited":
                    self.quick_failures += 1
                    if self.quick_failures >= MAX_QUICK_FAILURES:
                        log("claude exited within a minute 3 times in a row; stopping (see the session logs)")
                        stop_why = "gave up: claude exits at once"
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
            return 1
        except BaseException as e:
            stop_why = f"died: {type(e).__name__}: {e}"[:300]
            raise
        finally:
            self.join_keeper()
            if not restarting:
                (RUNTIME / "STOP").unlink(missing_ok=True)
                write_status(session=None, deadline=None, runner_pid=None)
                self.event("runner_stop", why=stop_why)
        log("runner stopped")
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


def cmd_run(a) -> int:
    if a.session_minutes < MIN_SESSION_MINUTES and not a.allow_short_sessions:
        # S001/S002 ran 15-min sessions and both wrapped up within 3 min: start and wrap-up playbooks
        # alone take ~13 min, so a short session is all overhead.
        print(f"refusing --session-minutes {a.session_minutes:g}: sessions under {MIN_SESSION_MINUTES} min are "
              "all start-up and wrap-up (use 60; --allow-short-sessions for harness tests)", file=sys.stderr)
        return 2
    if a.autocompact is not None and not AUTOCOMPACT_MIN <= a.autocompact <= AUTOCOMPACT_MAX:
        print(f"refusing --autocompact {a.autocompact}: claude takes {AUTOCOMPACT_MIN}-{AUTOCOMPACT_MAX} tokens")
        return 2
    if a.session_minutes < MIN_SESSION_MINUTES:
        log(f"WARNING: {a.session_minutes:g}-min sessions (wrap-up at {wrap_minutes(a.session_minutes):.1f} min); "
            "expect little play")
    lock = acquire_lock()  # held for the process lifetime (released only to exec the new runner code)
    (RUNTIME / "STOP").unlink(missing_ok=True)
    runner = Runner(Options(a.playthrough, a.hours, a.session_minutes, a.model, a.take_over, a.dry_run,
                            a.save_owner, a.effort, a.fix_model, a.fix_effort, a.fallback_model,
                            a.allow_short_sessions, not a.no_memory_keeper, a.keeper_model, a.keeper_effort,
                            a.keeper_minutes, a.autocompact))
    # Cutscenes are always skipped, also while the runner waits on menus and loads (the New Game intro
    # plays during StartLoading) and between sessions; the play session's MCP server idles meanwhile
    # (one skipper per game, runtime/skipper.lock).
    from bg3ai.skipper import start_background
    skip_stop = start_background(DEBUG_PORT, lambda m: log(f"autoskip: {m}"), RUNTIME / "skipper.lock")
    ram_stop = ramwatch.start_background(log)
    log("ramwatch: started (free RAM per minute → runtime/ram.log)" if ram_stop is not None else
        "ramwatch: not started here: another watchdog holds runtime/ramwatch.lock (that one guards the game)")

    def on_signal(signum, _frame):
        if runner.stop_requested:
            log("second signal: interrupting the current session")
            runner.abort = True
            return
        log(f"signal {signum}: stopping after the current session (send again to interrupt it)")
        runner.stop_requested = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        return runner.run()
    except RestartRunner:
        exec_new_runner(lock, skip_stop)
        raise  # not reached
    finally:
        if skip_stop is not None:
            skip_stop.set()
        if ram_stop is not None:
            ram_stop.set()


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
    if pt:
        rs = RunnerState(pt)
        hist = rs["history"] or []
        if hist:
            h = hist[-1]
            print(f"last:     {h['session']} {h['reason']} after {h.get('minutes')} min, progress={h.get('progress')}")
        if is_stuck(hist):
            print("stuck:    YES, the next kickoff carries a change-approach directive")
        save = pick_save(list_saves(), None, rs["save_owner"], set(rs["runner_saves"]))
        print(f"last save: {save.folder + ' (' + _ago(save.mtime) + ')' if save else '-'}")
        logs = sorted(rs.dir.glob("S*.log"), key=lambda p: p.stat().st_mtime)
        logfile = Path(st["log"]) if st.get("log") and Path(st["log"]).exists() else (logs[-1] if logs else None)
        if logfile:
            print(f"\n--- {logfile} (last 10 lines)")
            print("\n".join(logfile.read_text(errors="replace").splitlines()[-10:]))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play sessions until stopped")
    r.add_argument("--playthrough", required=True)
    r.add_argument("--hours", type=float, help="total wall-clock budget (default: until stopped)")
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
    r.add_argument("--no-memory-keeper", action="store_true",
                   help="skip the memory-keeper pass between sessions (runner/memory_keeper.py)")
    r.add_argument("--keeper-model", default="sonnet", help="model for the memory-keeper pass")
    r.add_argument("--keeper-effort", choices=EFFORTS, default="low", help="effort for the memory-keeper pass")
    r.add_argument("--keeper-minutes", type=float, default=5.0, help="time cap of the memory-keeper pass")
    r.add_argument("--fallback-model", help="claude --fallback-model (used when the main model is overloaded)")
    r.add_argument("--autocompact", type=int, default=None,
                   help=f"claude --autocompact <tokens> for play sessions ({AUTOCOMPACT_MIN}-{AUTOCOMPACT_MAX}); "
                        "default: Claude Code's own window. session-config.json \"autocompact\" overrides it")
    r.add_argument("--save-owner", help="character name that prefixes this playthrough's save folders "
                   "('Claude' for Claude-<id>__<label>); default: learned from the first save, or $BG3AI_SAVE_OWNER")
    r.add_argument("--take-over", action="store_true",
                   help="allow using a game that is already in a save the runner didn't load")
    r.add_argument("--dry-run", action="store_true", help="prepare the first session and print the claude "
                   "command instead of running it (still starts the game and loads the save)")
    s = sub.add_parser("status", help="summary of the runner, game and last session")
    s.add_argument("--playthrough")
    sub.add_parser("stop", help="finish the current session, then exit")
    a = ap.parse_args(argv)
    return {"run": cmd_run, "status": cmd_status, "stop": cmd_stop}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
