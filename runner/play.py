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
sys.path.insert(0, str(REPO / "tools"))
import steam_paths  # noqa: E402  (stdlib: Steam's offline state before any game launch)

PLAY_DIR = REPO / "play"
PLAYTHROUGHS = Path(os.environ.get("BG3AI_PLAYTHROUGHS", REPO / "playthroughs"))
RUNTIME = Path(os.environ.get("BG3AI_RUNTIME", REPO / "runtime"))
SESSION_PY = REPO / "tools" / "memory" / "session.py"
LAUNCH_SH = REPO / "runner" / "launch_game.sh"
CHECK_ALL = REPO / "tools" / "dev" / "check_all.py"
SAVE_DIR = Path(os.environ.get(
    "BG3AI_SAVE_DIR",
    Path.home() / ".local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story"))
DEBUG_HOST = "127.0.0.1"
DEBUG_PORT = int(os.environ.get("BG3LE_DEBUG_PORT", "9998"))
SAVE_PREFIX = os.environ.get("BG3AI_SAVE_PREFIX", "")

HARD_STOP_GRACE = 10 * 60      # deadline + this → SIGINT
SIGINT_GRACE = 30              # then SIGTERM
SIGTERM_GRACE = 15             # then SIGKILL
KILL_GRACE = 10                # the game ignores SIGTERM: SIGKILL it after this many seconds
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
GAME_FAULTS = ("crash", "hang")
FAULTS = GAME_FAULTS + ("agent_hang",)
STUCK_SESSIONS = 2
MAX_QUICK_FAILURES = 3         # claude exiting within a minute this many times in a row → give up
MAX_RESUMES = 3                # game crashes per session that resume the same claude conversation
RESUME_MIN_MINUTES = 8         # less than this left after a crash: end the session, the next one picks up
MAX_SETUP_FAILURES = 5         # game launch/load failing this many times in a row → give up
LAUNCH_ATTEMPTS = 3            # a launch that dies before the save plays is retried; the 3rd try loads the previous
LAUNCH_BACKOFF = (15, 45)      # good save; then the runner stops (alert). Seconds to wait before the 2nd and 3rd try
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


def ui_screen() -> str | None:
    if "ui.state" not in available_ops("client"):
        return None
    try:
        return (rpc("ui.state", context="client") or {}).get("screen")
    except Exception:  # noqa: BLE001
        return None


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
    return out


@dataclass
class SessionResult:
    sid: str
    claude_session: str
    exit_code: int | None
    reason: str          # exited | hard_stop | crash | hang | agent_hang | aborted | dry_run
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
        return session_id(predict_session_number(M.session_numbers(self.mem), self.memory_state().get("current")))

    # ---------------------------------------------------------------- game lifecycle

    def owns_game(self) -> bool:
        """True if the running game is one this runner (or a previous run of it) launched."""
        if self.game_proc is not None and self.game_proc.poll() is None:
            return True
        pg = (self.state["game"] or {}).get("pgid")
        if not pg:
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
        if not self.wait_for("debug port", port_open, 240):
            return False
        if not self.wait_for("client game state Menu", lambda: game_state() == "Menu", 300):
            return False
        # Splash screen → main menu. ui.state tells us when it's there; without it, two presses.
        for i in range(5):
            screen = ui_screen()
            if screen == "main_menu" or (screen is None and i >= 2):
                break  # without ui.state: two blind presses
            try:
                lua_eval('Ext.Input.InjectKeyPress("SPACE") return 1')
            except Exception as e:  # noqa: BLE001
                log(f"InjectKeyPress failed: {e}")
            time.sleep(3)
        if game_state() != "Menu":
            log(f"warning: game left the menu on its own (state {game_state()})")
        return True

    def wait_running(self) -> bool:
        return (self.wait_for("game state Running", lambda: game_state() == "Running", 600, 3)
                and self.wait_for("server context", server_ready, 120, 3))

    def choose_save(self) -> Save | None:
        handoff = self.mem / "HANDOFF.md"
        label = parse_handoff_save(handoff.read_text()) if handoff.exists() else None
        save = pick_save(list_saves(), label, self.state["save_owner"], set(self.state["runner_saves"]))
        if save and not self.state["save_owner"] and not self.opt.dry_run:
            self.state["save_owner"] = save.owner
        return save

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
            self.wait_menu()
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
            if host and owner and host.casefold() == owner.casefold():
                log(f"--take-over: the running game is '{host}' (this playthrough): playing on from here")
                return None
        log(f"loading save {save.folder}")
        if port_open() and ui_load:
            state = game_state()
            if state not in ("Menu", "Running"):
                self.wait_menu()
            # ui.menu load works from the main menu and from in-game (it opens the load screen itself).
            rpc("ui.menu", load_menu_args(save, list_saves()), context="client", timeout=30)
            if state == "Running":
                # From in-game the state is still Running until the load begins: without this wait,
                # wait_running() passed at once and S002's agent started mid-load (se triage).
                self.wait_for("the load to start", lambda: game_state() != "Running", 60, 0.5)
        else:
            if port_open():
                if not (self.owns_game() or self.opt.take_over or game_state() == "Menu"):
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

    def ensure_game(self, first: bool) -> str | None:
        """Make sure the game runs with this playthrough loaded. Returns the loaded save label, if any."""
        state = game_state() if port_open() else None
        if state not in (None, "Menu", "Running") or (state == "Running" and not server_ready()):
            log(f"game is busy (state {state}); waiting for it to settle")
            self.wait_running()
            state = game_state()
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
        self.kill_game()
        save = self.choose_save()
        label = self.load_save(save)
        self.pending_notes.append(
            f"RUNNER NOTE: the game {why} at {datetime.now():%H:%M}; the runner restarted it and resumed from "
            f"save \"{label or 'main menu'}\". Anything after that save is lost: reconcile your notes and the "
            "session log against get_status before continuing.")

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
        cmd += ["--resume", claude_session] if resume else ["--session-id", claude_session]
        return cmd

    def run_claude(self, sid: str, prompt: str, minutes: float, claude_session: str,
                   resume: bool = False, tag: str = "") -> SessionResult:
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
            if now >= next_health and reason == "exited":
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
            f"apm {summary['apm']}, busy {summary['busy_pct']}%, pipelined {summary.get('pipelining_pct')}% | {verdict}")
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
        log(f"{res.sid}: memory lint failed; running a {FIX_SESSION_MINUTES}-minute fix-notes session")
        if res.reason in ("exited", "hard_stop") and res.claude_session:
            self.run_claude(res.sid, fix_notes_prompt(r.stdout), FIX_SESSION_MINUTES, res.claude_session,
                            resume=True, tag="-fix")
        if not self.memory_state().get("open"):
            return
        r = self.session_py("end")
        if r.returncode == 0:
            log(f"{res.sid}: memory committed after the fix-notes session")
        elif res.reason in GAME_FAULTS:
            # The game died before the agent's wrap-up, so the end lint's HANDOFF check ("written by the current
            # session") can't pass and no fix pass runs. session.py start reconciles the open session (S014: the
            # note below told the next session to rewrite HANDOFF as the crashed one). Pass on only other errors.
            other = memory_lint_errors(r.stdout, skip=("handoff",))
            log(f"{res.sid}: ended by a game {res.reason} before wrap-up; memory left open for the next session"
                + (f" ({len(other)} other lint error(s))" if other else ""))
            if other:
                self.pending_notes.append(
                    f"RUNNER NOTE: {res.sid} ended in a game {res.reason} and its memory is uncommitted. Besides "
                    "the missing handoff (the game crashed before wrap-up), fix these lint errors early:\n"
                    + "\n".join(other)[-2500:])
        else:
            log(f"{res.sid}: memory still fails lint; left open for the next session to reconcile")
            self.pending_notes.append(
                f"RUNNER NOTE: memory lint still failed after {res.sid}; the session was left uncommitted. "
                "Fix these lint errors early in this session:\n" + r.stdout[-2500:])

    # ---------------------------------------------------------------- one session

    def plan_dry_run(self, minutes: float) -> SessionResult:
        """Print what the next session would do; touches neither the game nor the memory."""
        save = self.choose_save()
        sid = self.next_sid()
        print(f"session:        {sid} ({minutes:.0f} min)")
        print(f"memory:         {self.mem}")
        print(f"save to load:   {save.folder if save else '(none: new game from the main menu)'}")
        print(f"game:           {'up, state ' + str(game_state()) if port_open() else 'not running'}")
        print(f"stuck:          {is_stuck(self.state['history'])}")
        prompt = build_kickoff(sid, self.opt.playthrough, time.time() + minutes * 60,
                               save.label if save else None, list(self.pending_notes))
        return self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))

    def play_session(self, minutes: float, first: bool) -> SessionResult:
        if self.opt.dry_run:
            return self.plan_dry_run(minutes)
        loaded = self.ensure_game(first)
        sid = self.next_sid()
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
        start_sig = read_progress()
        prompt = build_kickoff(sid, self.opt.playthrough, time.time() + minutes * 60, loaded, notes)
        res = self.run_claude(sid, prompt, minutes, str(uuid.uuid4()))
        if res.reason == "dry_run":
            return res
        session_start, session_end = res.started, res.started + minutes * 60

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

    def run(self) -> int:
        self.check_memory()
        run_end = time.time() + self.opt.hours * 3600 if self.opt.hours else None
        write_status(playthrough=self.opt.playthrough, runner_pid=os.getpid(), run_end=run_end,
                     session=None, deadline=None, run_started=time.time(), heartbeat=time.time(), alert=None)
        self.event("runner_start", pid=os.getpid())
        first = True
        setup_failures = 0
        stop_why = "stopped"
        try:
            while not self.stop_requested and not (RUNTIME / "STOP").exists():
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
        except LaunchGaveUp as e:
            self.give_up_launch(e)
            stop_why = "gave up: the game crashes on every launch"
            return 1
        except BaseException as e:
            stop_why = f"died: {type(e).__name__}: {e}"[:300]
            raise
        finally:
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
    if a.session_minutes < MIN_SESSION_MINUTES:
        log(f"WARNING: {a.session_minutes:g}-min sessions (wrap-up at {wrap_minutes(a.session_minutes):.1f} min); "
            "expect little play")
    lock = acquire_lock()  # noqa: F841 - held for the process lifetime
    (RUNTIME / "STOP").unlink(missing_ok=True)
    runner = Runner(Options(a.playthrough, a.hours, a.session_minutes, a.model, a.take_over, a.dry_run,
                            a.save_owner, a.effort, a.fix_model, a.fix_effort, a.fallback_model,
                            a.allow_short_sessions))
    # Cutscenes are always skipped, also while the runner waits on menus and loads (the New Game intro
    # plays during StartLoading) and between sessions; the play session's MCP server idles meanwhile
    # (one skipper per game, runtime/skipper.lock).
    from bg3ai.skipper import start_background
    skip_stop = start_background(DEBUG_PORT, lambda m: log(f"autoskip: {m}"), RUNTIME / "skipper.lock")

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
    finally:
        if skip_stop is not None:
            skip_stop.set()


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
    r.add_argument("--fallback-model", help="claude --fallback-model (used when the main model is overloaded)")
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
