#!/usr/bin/env python3
"""Session lifecycle for the BG3 playthrough memory.

  session.py start     open the next session (sessions/Sxxx.md) and print the context injection
  session.py context   print the context injection for the current session (no new session)
  session.py end       lint (wrap-up phase), close the session, `git add -A && git commit -m Sxxx`
  session.py status    one-line status: session, elapsed minutes, wrap-up/compaction flags
  session.py hook      SessionStart-hook entry point (JSON on stdin): source=startup → start, otherwise
                       (resume/clear/compact/fork) → context. Kept for manual Claude Code use; the play
                       harness needs no client hooks.
  session.py clock     wrap-up reminder once the session passes $BG3_WRAP_MINUTES (default 50) minutes, as
                       PostToolUse-hook JSON. The MCP server appends the same reminder to its results.
  session.py memlint   lint errors about one edited file, as PostToolUse-hook JSON (the memory keeper's hook)
  session.py stop      the stop check: once wrap-up is due or HANDOFF.md is written for this session,
                       lint; on errors block the stop (the agent gets the report and fixes it), on
                       success close + commit the session. Gives up after a few blocked attempts.
                       Prints {"decision": "block", "reason": ...}, {"systemMessage": ...} or nothing.
                       The runner runs it after claude exits and resumes claude with the reason; the MCP
                       server's end_session tool makes the same check (stop_decision).
  session.py session-end  commit the session if it lints clean, else leave it open (the runner, at the end)

The playing agent has no shell: it never runs these itself. The runner and the bg3 MCP server do
(bridge/src/bg3ai/memtools.py imports this module).

The memory dir is --memory-dir, else $BG3_MEMORY_DIR, else $CLAUDE_PROJECT_DIR/memory, else ./memory.
Session state lives in <memory>/.session.json (git-ignored):
  {"current": 42, "open": true, "started": "<ISO>", "started_epoch": 1760000000.0, ...}
"""

from __future__ import annotations

import os
import sys

# Fast path for the PostToolUse clock hook, which runs after EVERY bg3 tool call. In runner sessions
# (BG3AI_SESSION_DEADLINE set) the MCP server already appends a ⏱ line to its results, so the hook has
# nothing to add: exit before the heavier imports and never add tokens.
if __name__ == "__main__" and sys.argv[1:2] == ["clock"] and os.environ.get("BG3AI_SESSION_DEADLINE"):
    sys.exit(0)

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from bg3play.memory import lint, memlib

HOOK_CHAR_LIMIT = 9500  # Claude Code caps hook stdout at 10,000 chars
WARN_REPEAT_SECONDS = 300
OVERTIME_REPEAT_SECONDS = 120


def wrap_minutes() -> float:
    return float(os.environ.get("BG3_WRAP_MINUTES", "50"))


def session_minutes() -> float:
    return float(os.environ.get("BG3_SESSION_MINUTES", "60"))


WRAP_WINDOW_S = 3 * 60  # autonomous mode without $BG3AI_SESSION_WRAPUP: wrap-up window before the deadline
MAX_KEEP_PLAYING_BLOCKS = 200  # autonomous mode: cap on "keep playing" stop blocks per session
# Autonomous mode: a finished wrap-up (HANDOFF.md written for this session) this close to the wrap-up time is
# accepted. S018-S025 of claude-run-2: the clock line said "0m/1m of play", the agent wrapped up, and every
# session the hook bounced it 1-4 times ("Keep playing: 3 minutes left"); each bounce re-saved Sxxx-end and
# re-edited HANDOFF.md, and S025 ended with its last save still writing.
# S036 (2026-10-10): wrapped up at 20:10:41 at a point of no return (the descent past the Pedestal of Reckoning),
# 49 s before a 150 s grace, was bounced and killed ~2.5 min with get_status/wiki reads; the grace went to 5 min.
# S040-S066 (2026-10-17): with 5 min the grace became the session end: every session wrapped up 6-18 min before the
# deadline ("the boss fight won't fit in the time left") and ended 3-8 min early; each early end is another ~1 min
# boundary + a wrap-up per session. A fight or descent needs no fit: the game keeps running between sessions and the
# next one plays on from there (S045/S049/S053 opened mid-fight). 2 min covers the "0m/1m of play" lines of S018-S025.
STOP_GRACE_S = 120


def fight_hold() -> dict:
    """The MCP server's fight hold for this session (bridge server.py _times: a fight still on at WRAP UP moves WRAP
    UP to its end, bounded), from $BG3AI_FIGHT_HOLD_PATH: {"wrap", "deadline", "held", "cap"}; {} without one."""
    path = os.environ.get("BG3AI_FIGHT_HOLD_PATH")
    if not path:
        return {}
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def session_deadline() -> float | None:
    """Epoch deadline from $BG3AI_SESSION_DEADLINE (set by runner/play.py = autonomous mode), else None; a fight hold
    moves it later."""
    try:
        v = float(os.environ.get("BG3AI_SESSION_DEADLINE", "") or 0)
    except ValueError:
        return None
    if v <= 0:
        return None
    return max(v, float(fight_hold().get("deadline") or 0))


def wrap_at() -> float | None:
    """Epoch when wrap-up starts in autonomous mode: $BG3AI_SESSION_WRAPUP (set by runner/play.py, the same
    value the MCP clock line uses), else WRAP_WINDOW_S before the deadline. None in manual sessions.
    S002 of claude-run-2: a fixed 10-min window let the agent stop at 7.4 min left while the clock line
    (wrap-up at 6.7 min left) still said "1m of play", so 7 min of the session went unused."""
    deadline = session_deadline()
    if not deadline:
        return None
    try:
        w = float(os.environ.get("BG3AI_SESSION_WRAPUP", "") or 0)
    except ValueError:
        w = 0.0
    w = w if 0 < w < deadline else deadline - WRAP_WINDOW_S
    return max(w, float(fight_hold().get("wrap") or 0))


def wrap_due(state: dict, now: float) -> bool:
    w = wrap_at()
    if w is not None:
        return now >= w
    return elapsed_minutes(state, now) >= wrap_minutes()


def overtime(state: dict, now: float) -> bool:
    deadline = session_deadline()
    if deadline:
        return now >= deadline
    return elapsed_minutes(state, now) >= session_minutes()


def elapsed_minutes(state: dict, now: float | None = None) -> float:
    start = state.get("started_epoch")
    if not start:
        return 0.0
    return ((now or time.time()) - float(start)) / 60.0


def no_memory_message(mem: Path) -> str:
    return (
        f"BG3 memory: no memory directory at {mem}.\n"
        f"Ask the operator to create one: python3 tools/memory/new_playthrough.py <name>\n"
        f"Do not start playing without memory."
    )


# ---------------------------------------------------------------- context injection


def _clean(text: str) -> str:
    text = memlib.strip_comments(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _file_block(mem: Path, name: str, budget: int) -> str:
    p = mem / name
    if not p.is_file():
        return f"----- {name} (MISSING) -----"
    body = _clean(p.read_text())
    if len(body) > budget:
        body = body[: max(0, budget - 80)].rstrip() + f"\n[... truncated; memory_read memory/{name} for the rest]"
    return f"----- {name} -----\n{body}"


CHARACTER_BUDGET = 1900  # CHARACTER.md: its core (lint: <= 1600 chars) + the newest development lines


def _character_block(mem: Path, budget: int = CHARACTER_BUDGET) -> str | None:
    """CHARACTER.md (the persona) for the injection, or None before character creation wrote it. The core is kept
    whole when it fits; of "## Character development" the newest lines that fit (the older ones stay in the file)."""
    p = mem / lint.CHARACTER_FILE
    if not p.is_file():
        return None
    core, dev = lint.character_split(_clean(p.read_text()))
    title = "----- CHARACTER.md (who you are: play as this character; the Roleplay line says how strictly) -----"
    core = core.strip()
    if len(core) > budget - 200:
        core = (
            core[: budget - 280].rstrip() + f"\n[... truncated; memory_read memory/{lint.CHARACTER_FILE} for the rest]"
        )
    lines = [ln for ln in dev.strip().splitlines()[1:] if ln.strip()]
    room = budget - len(core) - len(lint.CHARACTER_DEV_HEADING) - 80
    kept: list[str] = []
    for ln in reversed(lines):
        if room - len(ln) - 1 < 0:
            break
        kept.insert(0, ln)
        room -= len(ln) + 1
    if len(kept) < len(lines):
        kept.insert(0, f"(+{len(lines) - len(kept)} older lines in memory/{lint.CHARACTER_FILE})")
    body = core + (f"\n\n{lint.CHARACTER_DEV_HEADING}\n" + "\n".join(kept) if dev.strip() else "")
    return f"{title}\n{body}"


NEWS_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(.*\S)")
NEWS_MAX = 6


def knowledge_dir() -> Path:
    return Path(os.environ.get("BG3_KNOWLEDGE_DIR") or (memlib.REPO_ROOT / "play" / "knowledge"))


def harness_news(since: str | None) -> list[str]:
    """Operator entries in knowledge/harness-news.md ("- YYYY-MM-DD HH:MM text") newer than `since`
    (ISO time of the previous session's start; None = only the newest few). A fixed harness bug otherwise
    lives on as a wrong lesson in the notes (claude-run-2: "in a conversation = a tutorial popup")."""
    p = knowledge_dir() / "harness-news.md"
    if not p.is_file():
        return []
    entries = [
        (m.group(1), m.group(2))
        for line in memlib.strip_comments(p.read_text()).splitlines()
        if (m := NEWS_RE.match(line))
    ]
    cut = (since or "")[:16].replace("T", " ")
    new = [e for e in entries if e[0] > cut] if cut else entries
    new.sort()
    out = [f"- {t} {text}" for t, text in new[-NEWS_MAX:]]
    # after a long break 15+ entries: say the rest exist instead of dropping them silently. Not for a new
    # playthrough (no previous start): it has no older notes for the old entries to correct (naut-r3 S001 read
    # the whole file, ~8k tokens, on that pointer)
    if len(new) > NEWS_MAX and cut:
        out.insert(
            0,
            f"- (+{len(new) - NEWS_MAX} older changes since your last session, from {new[0][0]}: "
            "memory_read knowledge/harness-news.md once now)",
        )
    return out


def memory_health(mem: Path, current: int | None) -> str | None:
    stale = lint.stale_files(mem, current)
    if not stale:
        return None
    files = ", ".join(f"{rel} ({age} sessions)" for rel, age in stale)
    return (
        f"STALE NOTES: {files} not updated since; HANDOFF.md and the session logs are newer, so trust "
        f"them over these files. Update objectives.md this session (lint requires it at wrap-up), "
        f"and the others during walks when you touch their subject."
    )


def _status_block(mem: Path, budget: int = 700) -> str | None:
    p = mem / "snapshots" / "status.md"
    if not p.is_file():
        return None
    body = "\n".join(
        line
        for line in p.read_text().splitlines()
        if line.strip() and not line.startswith("<!--") and not line.startswith("# Snapshot")
    )
    if not body:
        return None
    out = f"----- snapshots/status.md (game state at the end of the last session) -----\n{body[:budget]}"
    g = mem / "snapshots" / "gear.md"
    if g.is_file():
        gear = "\n".join(
            line
            for line in g.read_text().splitlines()
            if line.strip() and not line.startswith("<!--") and not line.startswith("# Snapshot")
        )
        if gear and "nothing to change" not in gear:
            head = "snapshots/gear.md (do this first: equip_best, sell junk at the next trader)"
            out += f"\n----- {head} -----\n{gear[:600]}"
    return out


def build_context(
    mem: Path, source: str = "startup", notes: list[str] | None = None, news_since: str | None = None
) -> str:
    st = memlib.load_state(mem)
    cur = st.get("current")
    sid = memlib.session_id(cur) if cur else "(none)"
    head = [f"=== BG3 PLAYTHROUGH MEMORY — session {sid} ==="]
    if st.get("open"):
        deadline = session_deadline()
        if deadline:
            # No deadline or minutes before WRAP UP NOW: every time shown was read as a clock to beat (S066-S082).
            head.append(
                "Autonomous session: keep playing until the ⏱ line in tool results says WRAP UP NOW (you'll "
                "be told when to wrap up), then run the session-wrapup playbook. Don't stop early: nobody "
                "is there to answer."
            )
        else:
            head.append(
                f"Started {st.get('started', '?')}; elapsed {elapsed_minutes(st):.0f} min. "
                f"Wrap up at ~{wrap_minutes():.0f} min (session-wrapup playbook); hard limit "
                f"{session_minutes():.0f} min."
            )
        head.append(f"Your append-only log for this session: memory/sessions/{sid}.md")
    else:
        head.append(
            f"Session {sid} is CLOSED (already committed). Don't keep playing in this "
            f"conversation; the operator starts a fresh session."
        )
    if source == "compact":
        head.append(
            "CONTEXT WAS COMPACTED: before acting, re-read memory/sessions/"
            f"{sid}.md and memory/objectives.md, then call get_status."
            + (" Stay in character: CHARACTER.md below." if (mem / lint.CHARACTER_FILE).is_file() else "")
        )
    elif source in ("resume", "fork"):
        head.append("Resumed conversation: call get_status and check it matches what you remember.")
    elif source == "clear":
        head.append("Context was cleared: re-read objectives.md and your session log before acting.")
    elif source == "repeat":  # session_start called again in an open session (e.g. after a context compaction)
        head.append(
            "Session already open: this is your memory again. If your context was compacted, re-read "
            f"memory/sessions/{sid}.md and memory/objectives.md (memory_read), then call get_status."
        )
    # With the runner's memory keeper on, it compacts between sessions (S021 spent 11 Reads of old logs on it
    # mid-play); the player compacts only without a keeper.
    if cur and memlib.compaction_due(mem, cur) and os.environ.get("BG3AI_KEEPER") != "1":
        head.append(
            f"COMPACTION DUE: sessions after S{memlib.last_compacted_session(mem):03d} are not yet in "
            f"a chapter. Run the compaction playbook this session (after session-start)."
        )
    for n in notes or []:
        head.append(n)
    if source == "startup" and (news := harness_news(news_since)):
        head.append(
            "HARNESS CHANGES since the last session (they override older notes and lessons):\n" + "\n".join(news)
        )
    if health := memory_health(mem, cur if st.get("open") else None):
        head.append(health)
    lint_r = lint.lint(mem, "any", cur if st.get("open") else None)
    if lint_r.errors:
        head.append(
            f"Memory lint currently reports {len(lint_r.errors)} error(s) (fix before wrap-up):\n"
            + "\n".join(f"  {k}: {m.splitlines()[0]}" for k, m in lint_r.errors[:6])
        )
    if source == "startup":
        head.append("Next: run the session-start playbook.")
    header = "\n".join(head)

    status = _status_block(mem)
    character = _character_block(mem)  # before PLAYTHROUGH.md, with its own budget (interface I6)
    remaining = (
        HOOK_CHAR_LIMIT
        - len(header)
        - 200
        - (len(status) + 2 if status else 0)
        - (len(character) + 2 if character else 0)
    )
    pt_budget = min(5200, int(remaining * 0.58))
    # A short file leaves its unused share to the other (CHARACTER.md takes ~1.9 KB of the same 9.5 KB).
    pt_len, ho_len = (
        len(_clean((mem / n).read_text())) if (mem / n).is_file() else 0 for n in ("PLAYTHROUGH.md", "HANDOFF.md")
    )
    if pt_len < pt_budget:
        pt_budget = pt_len + 40
    elif ho_len < remaining - pt_budget:
        pt_budget = min(5200, remaining - ho_len - 40)
    ho_budget = remaining - pt_budget
    parts = [
        header,
        *([character] if character else []),
        _file_block(mem, "PLAYTHROUGH.md", pt_budget),
        _file_block(mem, "HANDOFF.md", ho_budget),
    ]
    if status:
        parts.append(status)
    parts.append("=== end of memory injection ===")
    out = "\n\n".join(parts)
    return out[:HOOK_CHAR_LIMIT]


# ---------------------------------------------------------------- commands


def log_header(n: int, started: str) -> str:
    return (
        f"# Session {memlib.session_id(n)} — started {started}\n\n"
        f"Goal: (write one line after session-start reconciliation)\n\n"
        f"## Log\n"
        f'<!-- Append-only. One line per event: "- HH:MM [tag] text". Tags: quest, decision, npc,\n'
        f"combat, death, loot, travel, puzzle, lesson, discrepancy, save, level, rest, stuck, note. -->\n"
    )


def append_log(mem: Path, n: int, line: str) -> None:
    p = mem / "sessions" / f"{memlib.session_id(n)}.md"
    if p.exists():
        with p.open("a") as f:
            f.write(line.rstrip("\n") + "\n")


def cmd_start(mem: Path, quiet: bool = False) -> int:
    ok, text = start_session(mem)
    if not quiet or not ok:
        print(text)
    return 0 if ok else 1


def start_session(mem: Path) -> tuple[bool, str]:
    """Open the next session (sessions/Sxxx.md) and return (ok, the context injection). The runner puts the
    text at the top of its kickoff; the MCP server's session_start tool returns it."""
    if not mem.is_dir():
        return False, no_memory_message(mem)
    st = memlib.load_state(mem)
    notes = []
    now = memlib.now_local()
    news_since = st.get("started")
    # Killed seconds after start with nothing logged: reuse its id (runner/play.py next_sid predicts the same).
    reuse = memlib.reusable_session(mem)
    if reuse is None and st.get("open") and st.get("current"):
        prev = st["current"]
        append_log(
            mem,
            prev,
            f"- {now:%H:%M} [note] (harness) session ended WITHOUT wrap-up; "
            f"work continues in {memlib.session_id(max([*memlib.session_numbers(mem), prev]) + 1)}",
        )
        notes.append(
            f"WARNING: previous session {memlib.session_id(prev)} ended without wrap-up: HANDOFF.md may "
            f"be stale and its notes are uncommitted. memory_read memory/sessions/{memlib.session_id(prev)}.md "
            f"and reconcile everything against live state."
        )
    n = reuse or max([*memlib.session_numbers(mem), st.get("current") or 0]) + 1
    (mem / "sessions").mkdir(exist_ok=True)
    started = now.isoformat(timespec="seconds")
    (mem / "sessions" / f"{memlib.session_id(n)}.md").write_text(log_header(n, now.strftime("%Y-%m-%d %H:%M %z")))
    memlib.save_state(
        mem, {"current": n, "open": True, "started": started, "started_epoch": time.time(), "warned_epoch": None}
    )
    return True, build_context(mem, "startup", notes, news_since)


def cmd_context(mem: Path, source: str = "startup") -> int:
    if not mem.is_dir():
        print(no_memory_message(mem))
        return 1
    print(build_context(mem, source))
    return 0


def cmd_end(mem: Path, human_override: bool = False) -> int:
    if not mem.is_dir():
        print(no_memory_message(mem))
        return 1
    st = memlib.load_state(mem)
    if not st.get("open"):
        print("No open session (already ended?). Nothing to do. Use `session.py start` to open one.")
        return 1
    n = st["current"]
    r = lint.lint(mem, "end", n)
    print(r.render(mem))
    if not r.ok and not human_override:
        print(
            f"\nSession {memlib.session_id(n)} NOT closed and NOT committed. Fix the errors and run "
            f"`session.py end` again (operator: --human-override commits anyway)."
        )
        return 1
    h = _close_and_commit(
        mem,
        st,
        " (lint override by operator)" if not r.ok else "",
        memlib.session_id(n) + (" (lint override)" if not r.ok else ""),
    )
    if h:
        stat = memlib.git(mem, "show", "--stat", "--format=", "HEAD", check=False).stdout.strip()
        print(f"\nSession {memlib.session_id(n)} closed; memory committed as {h}.\n{stat}")
    else:
        print(f"\nSession {memlib.session_id(n)} closed; nothing new to commit.")
    return 0


def cmd_status(mem: Path) -> int:
    if not mem.is_dir():
        print(no_memory_message(mem))
        return 1
    st = memlib.load_state(mem)
    cur = st.get("current")
    if not cur:
        print("No session has been started yet.")
        return 0
    mins = elapsed_minutes(st)
    flags = []
    if st.get("open") and mins >= wrap_minutes():
        flags.append("WRAP-UP NOW")
    if memlib.compaction_due(mem, cur):
        flags.append("compaction due")
    state = "open" if st.get("open") else "closed"
    print(
        f"{memlib.session_id(cur)} {state}; elapsed {mins:.0f} min of {session_minutes():.0f}"
        + (f" — {', '.join(flags)}" if flags else "")
    )
    return 0


def _read_hook_input() -> dict:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def cmd_hook(mem: Path) -> int:
    data = _read_hook_input()
    source = data.get("source")
    if not mem.is_dir():
        print(no_memory_message(mem))
        return 0  # never break the Claude session from a hook
    if source is None:
        source = "resume" if memlib.load_state(mem).get("open") else "startup"
    if source == "startup":
        cmd_start(mem)
    else:
        cmd_context(mem, source)
    return 0


def clock_message(state: dict, now: float) -> str | None:
    """Wrap-up reminder text if one is due now, else None. Mutates state['warned_epoch']."""
    if not state.get("open"):
        return None
    mins = elapsed_minutes(state, now)
    if not wrap_due(state, now):
        return None
    over = overtime(state, now)
    last = state.get("warned_epoch") or 0
    if now - last < (OVERTIME_REPEAT_SECONDS if over else WARN_REPEAT_SECONDS):
        return None
    state["warned_epoch"] = now
    sid = memlib.session_id(state["current"])
    if over:
        return (
            f"SESSION OVERTIME ({mins:.0f} min, limit {session_minutes():.0f}): stop playing now. "
            f"Get out of danger if you must, then run the session-wrapup playbook for {sid}."
        )
    return (
        f"SESSION CLOCK: {mins:.0f} min elapsed (wrap-up at {wrap_minutes():.0f}, limit "
        f"{session_minutes():.0f}). Finish the current fight or conversation, reach a safe spot, then "
        f"run the session-wrapup playbook for {sid}. Don't start new quests or travel."
    )


def cmd_clock(mem: Path) -> int:
    _read_hook_input()
    if not mem.is_dir():
        return 0
    st = memlib.load_state(mem)
    msg = clock_message(st, time.time())
    if msg:
        memlib.save_state(mem, st)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
    return 0


# ---------------------------------------------------------------- hook-driven wrap-up

MAX_STOP_BLOCKS = 4


def refresh_snapshots(mem: Path, timeout: float = 30.0) -> str:
    """Best effort: regenerate snapshots/ from the live game (tools/memory/snapshots.py). Never raises.

    Runs in a subprocess so a stuck game connection can't hang the hook. BG3_SNAPSHOTS=0 disables it.
    """
    if os.environ.get("BG3_SNAPSHOTS", "1") == "0":
        return "snapshots: disabled"
    src = str(Path(__file__).resolve().parents[2])  # the bg3play source root, for `-m` without an install
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))}
    try:
        p = subprocess.run(
            [sys.executable, "-m", "bg3play.memory.snapshots", "--memory-dir", str(mem)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else "snapshots: ?"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"snapshots: skipped ({e.__class__.__name__})"


def _close_and_commit(mem: Path, st: dict, note: str = "", message: str | None = None) -> str | None:
    """Refresh snapshots, append the end line to the log, mark the session closed, `git add -A && git commit`."""
    refresh_snapshots(mem)
    n = st["current"]
    now = memlib.now_local()
    mins = elapsed_minutes(st)
    append_log(mem, n, f"- {now:%H:%M} [note] session ended after {mins:.0f} min{note}")
    st.update(
        {"open": False, "ended": now.isoformat(timespec="seconds"), "elapsed_min": round(mins, 1), "stop_blocks": 0}
    )
    memlib.save_state(mem, st)
    return memlib.git_commit_all(mem, message or memlib.session_id(n)) if memlib.is_git_repo(mem) else None


def stop_decision(mem: Path, now: float | None = None, in_combat: Callable[[], bool] | None = None) -> dict | None:
    """Decide what the Stop hook does. Returns hook JSON output, or None to allow the stop silently.

    in_combat (the end_session tool): asked only for an early refusal, which then says to finish the fight, with no
    time in it. Without it (the runner's stop check) a held fight hold counts as a fight."""
    now = now or time.time()
    st = memlib.load_state(mem)
    if not st.get("open"):
        return None
    n = st["current"]
    sid = memlib.session_id(n)
    handoff_ready = lint.handoff_session(mem) == n
    deadline = session_deadline()
    # Manual session: an ordinary pause (a question to the operator), or an early stop the operator asked
    # for once HANDOFF.md is written. Autonomous session: nobody is there to answer, so stopping before
    # the wrap-up window just wastes the session, even with HANDOFF.md already written (S001 of
    # claude-run-2 wrote it at 16 min left because it was stuck on a ladder, and the hook let it stop).
    w = wrap_at()
    if os.environ.get("BG3AI_FIX_NOTES"):
        # The runner's fix-notes pass (play is over; memory edits only): never "keep playing" (S027-fix was bounced
        # 4 times with "9 minutes left"); lint + commit, or the lint report.
        early = False
    elif deadline and w is not None and st.get("time_named"):
        # S074: refused with "Call end_session again at 23:27", accepted at 23:25:50 by the grace below (the 4th
        # call, after 3 refusals each with a HANDOFF edit). Once refused ("Call end_session again only when the ⏱
        # line says WRAP UP NOW"), WRAP UP holds against the grace.
        early = now < w
    elif deadline and handoff_ready and w is not None and now >= w - STOP_GRACE_S:
        early = False
    else:
        early = not wrap_due(st, now) if deadline else not handoff_ready and not wrap_due(st, now)
    if early:
        if deadline is None:
            return None
        keep = int(st.get("keep_playing_blocks") or 0)
        if keep >= MAX_KEEP_PLAYING_BLOCKS:
            append_log(
                mem,
                n,
                f"- {memlib.now_local():%H:%M} [note] (harness) allowed an early stop after {keep} keep-playing blocks",
            )
            return {"systemMessage": f"{sid}: early stop allowed after {keep} keep-playing blocks."}
        st["keep_playing_blocks"] = keep + 1
        if in_combat() if in_combat is not None else fight_hold().get("held"):
            # S074: 3 of 4 refusals came mid-fight, each after a HANDOFF edit, and "8/4/2 min of play before WRAP
            # UP" read as a clock to beat. S078: the session waits for a fight (the fight hold), so no time at all.
            memlib.save_state(mem, st)
            return {
                "decision": "block",
                "reason": (
                    f"Finish the fight first: you are in combat. Play it out turn by turn (combat_turn), "
                    f"taking the time each turn needs: the session waits for the fight (WRAP UP comes "
                    f"after it ends), so reloading or leaving it is never about time. Don't save "
                    f"{sid}-end or edit HANDOFF.md during the fight. Your next message is a game tool "
                    f"call, not a summary (nobody reads it; a text-only message ends your turn). Call "
                    f"end_session again only when the ⏱ line says WRAP UP NOW."
                ),
            }
        st["time_named"] = True  # "when the ⏱ line says WRAP UP NOW" below
        memlib.save_state(mem, st)
        # S030: 7 early stops from 10 min left, each with a 1.3-2.1k-char session summary (6-9 s) and, after the
        # first, another save_game Sxxx-end + HANDOFF.md rewrite. A text-only message is what ends the turn, so
        # the reason asks for a game call next and says the early wrap-up need not be redone.
        # S070: wrapped up at 9 min left, then end_session x5 (4 refused) with a HANDOFF edit before each (~130 s of
        # gaps): "Keep playing: 8 minutes left" said neither when to call again nor that the minutes include the
        # wrap-up. So: when to call again, and no HANDOFF/end_session until then. S082: any minutes or time shown
        # were read as a clock to beat (early wrap-up 6 sessions in a row), so it names neither.
        again = (
            " You already wrapped up early. Start the next boss fight, descent or trip: a fight holds the "
            "session until it ends, and the next session plays on from where you are. "
            "Never idle reads."
            if handoff_ready or keep
            else ""
        )
        return {
            "decision": "block",
            "reason": (
                f"Keep playing: you'll be told when to wrap up. Call end_session again only when the "
                f"⏱ line says WRAP UP NOW; until then don't call end_session, save "
                f"{sid}-end or edit HANDOFF.md (at WRAP UP: one memory_edit of the lines that changed, a "
                f"save only if the party moved). Your next message is a game tool call, not a summary "
                f"(nobody reads it; a text-only message ends your turn): get_status if you lost track. "
                f"Stuck is not a reason to stop: follow knowledge/playbooks/stuck.md (cancel_queue, reload "
                f"the last save, move another party member, another objective). Only stop after the "
                f"session-wrapup playbook when the SESSION CLOCK reminder appears (the ⏱ line in tool "
                f"results says WRAP UP).{again}"
            ),
        }
    blocks = int(st.get("stop_blocks") or 0)
    if not handoff_ready and os.environ.get("BG3AI_FIX_NOTES"):
        # No game in the fix-notes pass (S027 was killed before wrap-up): only the memory part of the wrap-up.
        reason = (
            f"HANDOFF.md isn't written for {sid} (the session ended before wrap-up). There is no game now: "
            f"don't save or call game tools. Rewrite HANDOFF.md from the session log with the heading "
            f"'# Handoff {sid} → {memlib.session_id(n + 1)}' (keep its Save to load line, or name the newest "
            f"{sid}-* save the log mentions), then stop again."
        )
    elif not handoff_ready:
        reason = (
            f"Wrap-up is due for {sid} but HANDOFF.md hasn't been rewritten for it yet. Run the "
            f"session-wrapup playbook: save the game as {sid}-end, update memory, write HANDOFF.md "
            f"with the heading '# Handoff {sid} → {memlib.session_id(n + 1)}', then stop again."
        )
    else:
        r = lint.lint(mem, "end", n)
        if r.ok:
            h = _close_and_commit(mem, st)
            msg = f"Memory for {sid} committed ({h})." if h else f"{sid} closed; nothing new to commit."
            return {"systemMessage": msg}
        reason = f"Memory lint failed, so {sid} is NOT committed yet. Fix these, then stop again:\n" + r.render(mem)
    if blocks >= MAX_STOP_BLOCKS:
        append_log(
            mem,
            n,
            f"- {memlib.now_local():%H:%M} [note] (harness) gave up blocking stop after "
            f"{blocks} attempts; session left open and uncommitted",
        )
        st["stop_blocks"] = 0
        memlib.save_state(mem, st)
        return {
            "systemMessage": f"{sid} left open: wrap-up incomplete after {blocks} attempts "
            f"(operator: see `session.py end`)."
        }
    st["stop_blocks"] = blocks + 1
    memlib.save_state(mem, st)
    return {"decision": "block", "reason": reason}


def cmd_stop(mem: Path) -> int:
    _read_hook_input()
    if not mem.is_dir():
        return 0
    out = stop_decision(mem)
    if out:
        print(json.dumps(out))
    return 0


def cmd_session_end(mem: Path) -> int:
    """SessionEnd hook: last chance to commit a session whose wrap-up was finished."""
    _read_hook_input()
    if not mem.is_dir():
        return 0
    st = memlib.load_state(mem)
    if not st.get("open"):
        return 0
    n = st["current"]
    if lint.handoff_session(mem) == n and lint.lint(mem, "end", n).ok:
        _close_and_commit(mem, st, " (committed by SessionEnd hook)")
    else:
        append_log(
            mem,
            n,
            f"- {memlib.now_local():%H:%M} [note] (harness) Claude session ended before wrap-up "
            f"finished; left open and uncommitted",
        )
    return 0


def memlint_message(mem: Path, edited: str) -> str | None:
    """Lint errors that mention the edited file (for PostToolUse on Edit/Write), or None."""
    try:
        rel = os.path.relpath(os.path.realpath(edited), os.path.realpath(mem))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    mine = memlint_findings(mem, rel)
    if not mine:
        return None
    return "Memory lint, " + rel + ":\n" + "\n".join(f"- {k}: {m}" for k, m in mine[:10])


def memlint_findings(mem: Path, rel: str) -> list[tuple[str, str]]:
    """Lint errors about one memory file (`rel`, relative to the memory dir), as (kind, message)."""
    st = memlib.load_state(mem)
    r = lint.lint(mem, "any", st.get("current") if st.get("open") else None)
    return [(k, m) for k, m in r.errors if m.startswith(rel)]


def cmd_memlint(mem: Path) -> int:
    data = _read_hook_input()
    if not mem.is_dir():
        return 0
    path = (data.get("tool_input") or {}).get("file_path")
    msg = memlint_message(mem, path) if isinstance(path, str) else None
    if msg:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "command", choices=["start", "context", "end", "status", "hook", "clock", "memlint", "stop", "session-end"]
    )
    ap.add_argument("--memory-dir")
    ap.add_argument("--source", default="startup", help="for 'context': startup|resume|clear|compact|fork")
    ap.add_argument(
        "--human-override", action="store_true", help="end: commit even if lint fails. For the human operator only."
    )
    args = ap.parse_args(argv)
    mem = memlib.resolve_memory_dir(args.memory_dir)
    if args.command == "start":
        return cmd_start(mem)
    if args.command == "context":
        return cmd_context(mem, args.source)
    if args.command == "end":
        return cmd_end(mem, args.human_override)
    if args.command == "status":
        return cmd_status(mem)
    if args.command == "hook":
        return cmd_hook(mem)
    if args.command == "memlint":
        return cmd_memlint(mem)
    if args.command == "stop":
        return cmd_stop(mem)
    if args.command == "session-end":
        return cmd_session_end(mem)
    return cmd_clock(mem)


if __name__ == "__main__":
    sys.exit(main())
