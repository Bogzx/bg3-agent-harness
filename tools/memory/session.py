#!/usr/bin/env python3
"""Session lifecycle for the BG3 playthrough memory.

  session.py start     open the next session (sessions/Sxxx.md) and print the context injection
  session.py context   print the context injection for the current session (no new session)
  session.py end       lint (wrap-up phase), close the session, `git add -A && git commit -m Sxxx`
  session.py status    one-line status: session, elapsed minutes, wrap-up/compaction flags
  session.py hook      SessionStart hook entry point: reads the hook JSON on stdin;
                       source=startup → start, otherwise (resume/clear/compact/fork) → context
  session.py clock     PostToolUse hook (mcp__bg3__*): wrap-up reminder once the session passes
                       $BG3_WRAP_MINUTES (default 50) minutes
  session.py memlint   PostToolUse hook (Edit/Write): reports lint errors about the edited file
  session.py stop      Stop hook: once wrap-up is due or HANDOFF.md is written for this session,
                       lint; on errors block the stop (Claude gets the report and fixes it), on
                       success close + commit the session. Gives up after a few blocked attempts.
  session.py session-end  SessionEnd hook: commit the session if it lints clean, else leave it open

The playing agent has no shell: it never runs these itself; hooks do.

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lint as L  # noqa: E402
import memlib as M  # noqa: E402

HOOK_CHAR_LIMIT = 9500  # Claude Code caps hook stdout at 10,000 chars
WARN_REPEAT_SECONDS = 300
OVERTIME_REPEAT_SECONDS = 120


def wrap_minutes() -> float:
    return float(os.environ.get("BG3_WRAP_MINUTES", "50"))


def session_minutes() -> float:
    return float(os.environ.get("BG3_SESSION_MINUTES", "60"))


WRAP_WINDOW_S = 3 * 60           # autonomous mode without $BG3AI_SESSION_WRAPUP: wrap-up window before the deadline
MAX_KEEP_PLAYING_BLOCKS = 200    # autonomous mode: cap on "keep playing" stop blocks per session
# Autonomous mode: a finished wrap-up (HANDOFF.md written for this session) this close to the wrap-up time is
# accepted. S018-S025 of claude-run-2: the clock line said "0m/1m of play", the agent wrapped up, and every
# session the hook bounced it 1-4 times ("Keep playing: 3 minutes left"); each bounce re-saved Sxxx-end and
# re-edited HANDOFF.md, and S025 ended with its last save still writing.
STOP_GRACE_S = 150


def session_deadline() -> float | None:
    """Epoch deadline from $BG3AI_SESSION_DEADLINE (set by runner/play.py = autonomous mode), else None."""
    try:
        v = float(os.environ.get("BG3AI_SESSION_DEADLINE", "") or 0)
    except ValueError:
        return None
    return v if v > 0 else None


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
    return w if 0 < w < deadline else deadline - WRAP_WINDOW_S


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
    return (f"BG3 memory: no memory directory at {mem}.\n"
            f"Ask the operator to create one: python3 tools/memory/new_playthrough.py <name>\n"
            f"Do not start playing without memory.")


# ---------------------------------------------------------------- context injection


def _clean(text: str) -> str:
    text = M.strip_comments(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _file_block(mem: Path, name: str, budget: int) -> str:
    p = mem / name
    if not p.is_file():
        return f"----- {name} (MISSING) -----"
    body = _clean(p.read_text())
    if len(body) > budget:
        body = body[: max(0, budget - 80)].rstrip() + f"\n[... truncated; Read memory/{name} for the rest]"
    return f"----- {name} -----\n{body}"


NEWS_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(.*\S)")
NEWS_MAX = 6


def knowledge_dir() -> Path:
    return Path(os.environ.get("BG3_KNOWLEDGE_DIR") or (M.REPO_ROOT / "play" / "knowledge"))


def harness_news(since: str | None) -> list[str]:
    """Operator entries in knowledge/harness-news.md ("- YYYY-MM-DD HH:MM text") newer than `since`
    (ISO time of the previous session's start; None = only the newest few). A fixed harness bug otherwise
    lives on as a wrong lesson in the notes (claude-run-2: "in a conversation = a tutorial popup")."""
    p = knowledge_dir() / "harness-news.md"
    if not p.is_file():
        return []
    entries = [(m.group(1), m.group(2)) for line in M.strip_comments(p.read_text()).splitlines()
               if (m := NEWS_RE.match(line))]
    cut = (since or "")[:16].replace("T", " ")
    new = [e for e in entries if e[0] > cut] if cut else entries
    new.sort()
    return [f"- {t} {text}" for t, text in new[-NEWS_MAX:]]


def memory_health(mem: Path, current: int | None) -> str | None:
    stale = L.stale_files(mem, current)
    if not stale:
        return None
    files = ", ".join(f"{rel} ({age} sessions)" for rel, age in stale)
    return (f"STALE NOTES: {files} not updated since; HANDOFF.md and the session logs are newer, so trust "
            f"them over these files. Update objectives.md this session (lint requires it at wrap-up), "
            f"and the others during walks when you touch their subject.")


def _status_block(mem: Path, budget: int = 700) -> str | None:
    p = mem / "snapshots" / "status.md"
    if not p.is_file():
        return None
    body = "\n".join(l for l in p.read_text().splitlines()
                     if l.strip() and not l.startswith("<!--") and not l.startswith("# Snapshot"))
    if not body:
        return None
    out = f"----- snapshots/status.md (game state at the end of the last session) -----\n{body[:budget]}"
    g = mem / "snapshots" / "gear.md"
    if g.is_file():
        gear = "\n".join(l for l in g.read_text().splitlines()
                         if l.strip() and not l.startswith("<!--") and not l.startswith("# Snapshot"))
        if gear and "nothing to change" not in gear:
            out += f"\n----- snapshots/gear.md (do this first: equip_best, sell junk at the next trader) -----\n{gear[:600]}"
    return out


def build_context(mem: Path, source: str = "startup", notes: list[str] | None = None,
                  news_since: str | None = None) -> str:
    st = M.load_state(mem)
    cur = st.get("current")
    sid = M.session_id(cur) if cur else "(none)"
    head = [f"=== BG3 PLAYTHROUGH MEMORY — session {sid} ==="]
    if st.get("open"):
        deadline = session_deadline()
        if deadline:
            left = (deadline - time.time()) / 60
            head.append(f"Autonomous session: deadline {time.strftime('%H:%M', time.localtime(deadline))} "
                        f"({left:.0f} min left). Play until the ⏱ line in tool results says WRAP UP "
                        f"({(deadline - wrap_at()) / 60:.0f} min before), then "
                        f"run the session-wrapup playbook. Don't stop early: nobody is there to answer.")
        else:
            head.append(f"Started {st.get('started', '?')}; elapsed {elapsed_minutes(st):.0f} min. "
                        f"Wrap up at ~{wrap_minutes():.0f} min (session-wrapup playbook); hard limit "
                        f"{session_minutes():.0f} min.")
        head.append(f"Your append-only log for this session: memory/sessions/{sid}.md")
    else:
        head.append(f"Session {sid} is CLOSED (already committed). Don't keep playing in this "
                    f"conversation; the operator starts a fresh session.")
    if source == "compact":
        head.append("CONTEXT WAS COMPACTED: before acting, re-read memory/sessions/"
                    f"{sid}.md and memory/objectives.md, then call get_status.")
    elif source in ("resume", "fork"):
        head.append("Resumed conversation: call get_status and check it matches what you remember.")
    elif source == "clear":
        head.append("Context was cleared: re-read objectives.md and your session log before acting.")
    # With the runner's memory keeper on, it compacts between sessions (S021 spent 11 Reads of old logs on it
    # mid-play); the player compacts only without a keeper.
    if cur and M.compaction_due(mem, cur) and os.environ.get("BG3AI_KEEPER") != "1":
        head.append(f"COMPACTION DUE: sessions after S{M.last_compacted_session(mem):03d} are not yet in "
                    f"a chapter. Run the compaction playbook this session (after session-start).")
    for n in notes or []:
        head.append(n)
    if source == "startup" and (news := harness_news(news_since)):
        head.append("HARNESS CHANGES since the last session (they override older notes and lessons):\n"
                    + "\n".join(news))
    if health := memory_health(mem, cur if st.get("open") else None):
        head.append(health)
    lint_r = L.lint(mem, "any", cur if st.get("open") else None)
    if lint_r.errors:
        head.append(f"Memory lint currently reports {len(lint_r.errors)} error(s) (fix before wrap-up):\n"
                    + "\n".join(f"  {k}: {m.splitlines()[0]}" for k, m in lint_r.errors[:6]))
    if source == "startup":
        head.append("Next: run the session-start playbook.")
    header = "\n".join(head)

    status = _status_block(mem)
    remaining = HOOK_CHAR_LIMIT - len(header) - 200 - (len(status) + 2 if status else 0)
    pt_budget = min(5200, int(remaining * 0.58))
    ho_budget = remaining - pt_budget
    parts = [header, _file_block(mem, "PLAYTHROUGH.md", pt_budget), _file_block(mem, "HANDOFF.md", ho_budget)]
    if status:
        parts.append(status)
    parts.append("=== end of memory injection ===")
    out = "\n\n".join(parts)
    return out[:HOOK_CHAR_LIMIT]


# ---------------------------------------------------------------- commands


def log_header(n: int, started: str) -> str:
    return (f"# Session {M.session_id(n)} — started {started}\n\n"
            f"Goal: (write one line after session-start reconciliation)\n\n"
            f"## Log\n"
            f"<!-- Append-only. One line per event: \"- HH:MM [tag] text\". Tags: quest, decision, npc,\n"
            f"combat, death, loot, travel, puzzle, lesson, discrepancy, save, level, rest, stuck, note. -->\n")


def append_log(mem: Path, n: int, line: str) -> None:
    p = mem / "sessions" / f"{M.session_id(n)}.md"
    if p.exists():
        with p.open("a") as f:
            f.write(line.rstrip("\n") + "\n")


def cmd_start(mem: Path, quiet: bool = False) -> int:
    if not mem.is_dir():
        print(no_memory_message(mem))
        return 1
    st = M.load_state(mem)
    notes = []
    now = M.now_local()
    news_since = st.get("started")
    if st.get("open") and st.get("current"):
        prev = st["current"]
        append_log(mem, prev, f"- {now:%H:%M} [note] (harness) session ended WITHOUT wrap-up; "
                              f"work continues in {M.session_id(max(M.session_numbers(mem) + [prev]) + 1)}")
        notes.append(f"WARNING: previous session {M.session_id(prev)} ended without wrap-up: HANDOFF.md may "
                     f"be stale and its notes are uncommitted. Read memory/sessions/{M.session_id(prev)}.md "
                     f"and reconcile everything against live state.")
    n = max(M.session_numbers(mem) + [st.get("current") or 0]) + 1
    (mem / "sessions").mkdir(exist_ok=True)
    started = now.isoformat(timespec="seconds")
    (mem / "sessions" / f"{M.session_id(n)}.md").write_text(log_header(n, now.strftime("%Y-%m-%d %H:%M %z")))
    M.save_state(mem, {"current": n, "open": True, "started": started, "started_epoch": time.time(),
                       "warned_epoch": None})
    if not quiet:
        print(build_context(mem, "startup", notes, news_since))
    return 0


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
    st = M.load_state(mem)
    if not st.get("open"):
        print("No open session (already ended?). Nothing to do. Use `session.py start` to open one.")
        return 1
    n = st["current"]
    r = L.lint(mem, "end", n)
    print(r.render(mem))
    if not r.ok and not human_override:
        print(f"\nSession {M.session_id(n)} NOT closed and NOT committed. Fix the errors and run "
              f"`session.py end` again (operator: --human-override commits anyway).")
        return 1
    h = _close_and_commit(mem, st, " (lint override by operator)" if not r.ok else "",
                          M.session_id(n) + (" (lint override)" if not r.ok else ""))
    if h:
        stat = M.git(mem, "show", "--stat", "--format=", "HEAD", check=False).stdout.strip()
        print(f"\nSession {M.session_id(n)} closed; memory committed as {h}.\n{stat}")
    else:
        print(f"\nSession {M.session_id(n)} closed; nothing new to commit.")
    return 0


def cmd_status(mem: Path) -> int:
    if not mem.is_dir():
        print(no_memory_message(mem))
        return 1
    st = M.load_state(mem)
    cur = st.get("current")
    if not cur:
        print("No session has been started yet.")
        return 0
    mins = elapsed_minutes(st)
    flags = []
    if st.get("open") and mins >= wrap_minutes():
        flags.append("WRAP-UP NOW")
    if M.compaction_due(mem, cur):
        flags.append("compaction due")
    state = "open" if st.get("open") else "closed"
    print(f"{M.session_id(cur)} {state}; elapsed {mins:.0f} min of {session_minutes():.0f}"
          + (f" — {', '.join(flags)}" if flags else ""))
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
        source = "resume" if M.load_state(mem).get("open") else "startup"
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
    sid = M.session_id(state["current"])
    if over:
        return (f"SESSION OVERTIME ({mins:.0f} min, limit {session_minutes():.0f}): stop playing now. "
                f"Get out of danger if you must, then run the session-wrapup playbook for {sid}.")
    return (f"SESSION CLOCK: {mins:.0f} min elapsed (wrap-up at {wrap_minutes():.0f}, limit "
            f"{session_minutes():.0f}). Finish the current fight or conversation, reach a safe spot, then "
            f"run the session-wrapup playbook for {sid}. Don't start new quests or travel.")


def cmd_clock(mem: Path) -> int:
    _read_hook_input()
    if not mem.is_dir():
        return 0
    st = M.load_state(mem)
    msg = clock_message(st, time.time())
    if msg:
        M.save_state(mem, st)
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
    script = Path(__file__).resolve().parent / "snapshots.py"
    try:
        p = subprocess.run([sys.executable, str(script), "--memory-dir", str(mem)],
                           capture_output=True, text=True, timeout=timeout)
        return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else "snapshots: ?"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"snapshots: skipped ({e.__class__.__name__})"


def _close_and_commit(mem: Path, st: dict, note: str = "", message: str | None = None) -> str | None:
    """Refresh snapshots, append the end line to the log, mark the session closed, `git add -A && git commit`."""
    refresh_snapshots(mem)
    n = st["current"]
    now = M.now_local()
    mins = elapsed_minutes(st)
    append_log(mem, n, f"- {now:%H:%M} [note] session ended after {mins:.0f} min{note}")
    st.update({"open": False, "ended": now.isoformat(timespec="seconds"), "elapsed_min": round(mins, 1),
               "stop_blocks": 0})
    M.save_state(mem, st)
    return M.git_commit_all(mem, message or M.session_id(n)) if M.is_git_repo(mem) else None


def stop_decision(mem: Path, now: float | None = None) -> dict | None:
    """Decide what the Stop hook does. Returns hook JSON output, or None to allow the stop silently."""
    now = now or time.time()
    st = M.load_state(mem)
    if not st.get("open"):
        return None
    n = st["current"]
    sid = M.session_id(n)
    handoff_ready = L.handoff_session(mem) == n
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
    elif deadline and handoff_ready and w is not None and now >= w - STOP_GRACE_S:
        early = False
    else:
        early = not wrap_due(st, now) if deadline else not handoff_ready and not wrap_due(st, now)
    if early:
        if deadline is None:
            return None
        keep = int(st.get("keep_playing_blocks") or 0)
        if keep >= MAX_KEEP_PLAYING_BLOCKS:
            append_log(mem, n, f"- {M.now_local():%H:%M} [note] (harness) allowed an early stop after {keep} "
                               f"keep-playing blocks")
            return {"systemMessage": f"{sid}: early stop allowed after {keep} keep-playing blocks."}
        st["keep_playing_blocks"] = keep + 1
        M.save_state(mem, st)
        left = max(1, int((deadline - now) // 60))
        return {"decision": "block",
                "reason": (f"Keep playing: {left} minutes left in this session. Continue your current plan "
                           f"(get_status if you lost track). Stuck is not a reason to stop: follow "
                           f"knowledge/playbooks/stuck.md (cancel_queue, reload the last save, move another party "
                           f"member, another objective). A HANDOFF.md written early is fine; rewrite it at "
                           f"wrap-up. Only stop after the session-wrapup playbook when the SESSION CLOCK "
                           f"reminder appears (the ⏱ line in tool results says WRAP UP).")}
    blocks = int(st.get("stop_blocks") or 0)
    if not handoff_ready:
        reason = (f"Wrap-up is due for {sid} but HANDOFF.md hasn't been rewritten for it yet. Run the "
                  f"session-wrapup playbook: save the game as {sid}-end, update memory, write HANDOFF.md "
                  f"with the heading '# Handoff {sid} → {M.session_id(n + 1)}', then stop again.")
    else:
        r = L.lint(mem, "end", n)
        if r.ok:
            h = _close_and_commit(mem, st)
            msg = f"Memory for {sid} committed ({h})." if h else f"{sid} closed; nothing new to commit."
            return {"systemMessage": msg}
        reason = (f"Memory lint failed, so {sid} is NOT committed yet. Fix these, then stop again:\n"
                  + r.render(mem))
    if blocks >= MAX_STOP_BLOCKS:
        append_log(mem, n, f"- {M.now_local():%H:%M} [note] (harness) gave up blocking stop after "
                           f"{blocks} attempts; session left open and uncommitted")
        st["stop_blocks"] = 0
        M.save_state(mem, st)
        return {"systemMessage": f"{sid} left open: wrap-up incomplete after {blocks} attempts "
                                 f"(operator: see `session.py end`)."}
    st["stop_blocks"] = blocks + 1
    M.save_state(mem, st)
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
    st = M.load_state(mem)
    if not st.get("open"):
        return 0
    n = st["current"]
    if L.handoff_session(mem) == n and L.lint(mem, "end", n).ok:
        _close_and_commit(mem, st, " (committed by SessionEnd hook)")
    else:
        append_log(mem, n, f"- {M.now_local():%H:%M} [note] (harness) Claude session ended before wrap-up "
                           f"finished; left open and uncommitted")
    return 0


def memlint_message(mem: Path, edited: str) -> str | None:
    """Lint errors that mention the edited file (for PostToolUse on Edit/Write), or None."""
    try:
        rel = os.path.relpath(os.path.realpath(edited), os.path.realpath(mem))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    st = M.load_state(mem)
    r = L.lint(mem, "any", st.get("current") if st.get("open") else None)
    mine = [(k, m) for k, m in r.errors if m.startswith(rel)]
    if not mine:
        return None
    return "Memory lint, " + rel + ":\n" + "\n".join(f"- {k}: {m}" for k, m in mine[:10])


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
    ap.add_argument("command", choices=["start", "context", "end", "status", "hook", "clock", "memlint",
                                        "stop", "session-end"])
    ap.add_argument("--memory-dir")
    ap.add_argument("--source", default="startup", help="for 'context': startup|resume|clear|compact|fork")
    ap.add_argument("--human-override", action="store_true",
                    help="end: commit even if lint fails. For the human operator only.")
    args = ap.parse_args(argv)
    mem = M.resolve_memory_dir(args.memory_dir)
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
