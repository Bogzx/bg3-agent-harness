"""Benchmark runs (`play.py run --until M --runs N`): run naming, per-run resets, failure tagging, the runs table.

Stdlib only (plus runner/triage.py and runner/fairness.py, which are stdlib + bg3ai.metrics). play.py owns the
run loop; everything here is a pure function over files the runner and the bridge already write, so it is unit
tested without a game.

Failure classes (every failure in a run record is one of them):
    harness   ours: game crash/hang/OOM, launch/load/setup failures, a stale bg3le, the MCP server not connected,
              claude API/usage errors and claude exiting at once, tool exceptions and timeouts, a tool result that
              leaked something a player can't see (fairness LEAK), runner exceptions and runner give-ups
    agent     the model's: stalls with no story progress, party wipes (Game Over), reloads it chose, early exits,
              running out of the run's time, sandbox/fairness-guard blocks of its own file calls
    unknown   can't tell from the logs: claude silent for minutes (a slow model turn or a stalled API), claude
              killed by a signal from outside the runner
Operator actions (a manual restart, a stop, a runner self-restart on changed code, --take-over) are listed apart:
a run with any of them is not clean.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fairness  # noqa: E402
import triage  # noqa: E402

HARNESS, AGENT, UNKNOWN = "harness", "agent", "unknown"
OUTCOMES = ("reached", "timeout", "gave_up", "runner_error")

# runner_events.jsonl kinds that are harness failures (the fault kinds are mapped in event_failures)
HARNESS_EVENTS = {
    "game_oom": "game_oom",
    "setup_failed": "setup_failed",
    "recovery_failed": "recovery_failed",
    "launch_failed": "launch_failed",
    "launch_gave_up": "launch_gave_up",
    "load_refused": "load_refused",
    "bg3le_stale": "bg3le_stale",
    "steam_refused": "steam_refused",
    "oom_save_refused": "oom_save_refused",
}
FAULT_KINDS = {"crash": ("game_crash", HARNESS), "hang": ("game_hang", HARNESS), "agent_hang": ("agent_hang", UNKNOWN)}

PLAY_STREAM_RE = re.compile(r"^S\d{3,}(?:-r\d+)?\.jsonl$")      # play sessions and their resumes (not -fix/-keeper)
CLAUDE_STREAM_RE = re.compile(r"^S\d{3,}(?:-[\w-]+)?\.jsonl$")   # every claude stream, incl. -fix and -keeper
API_ERROR_RE = re.compile(r"API Error|usage limit|rate.?limit|overloaded|credit balance|invalid api key|"
                          r"authentication|\b(?:429|500|502|503|529)\b", re.I)
TOOL_EXCEPTION_RE = re.compile(r"Error executing tool|Traceback \(most recent call last\)|Internal error|MCP error",
                               re.I)
TIMEOUT_RE = re.compile(r"\bTIMEOUT\b|timed out", re.I)
GUARD_BLOCK = "Blocked by the sandbox/fairness rule"
GAME_OVER_RE = re.compile(r"\bgame[_ ]over\b", re.I)
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def failure(t, kind: str, cls: str, detail: str, n: int = 1) -> dict:
    f = {"t": round(t, 1) if isinstance(t, (int, float)) else None, "kind": kind, "class": cls,
         "detail": str(detail)[:300]}
    if n > 1:
        f["n"] = n
    return f


# ======================================================================== naming

def run_name(base: str, k: int) -> str:
    """Run k of the series `base`: run 1 is the playthrough itself, run k > 1 is `<base>-r<k>`."""
    return base if k <= 1 else f"{base}-r{k}"


def series_records(path: Path, base: str) -> list[dict]:
    """The series' runs.jsonl records (runtime/sessions/<base>/runs.jsonl), in file order."""
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("series", r.get("playthrough")) == base:
            out.append(r)
    return out


def next_run_index(records: list[dict]) -> int:
    """The run to play next: one after the highest recorded run (a record means that run is over, any outcome)."""
    return max([int(r.get("run") or 1) for r in records], default=0) + 1


# ======================================================================== per-run reset

def archive_nav_state(se_bg3ai: Path, dest: Path) -> list[str]:
    """NAV-3: move the mod's machine-global navigation files (the per-level kill list nav_guard.json and the
    breadcrumb mirrors nav_<host uuid>.json, Script Extender/BG3AI/) into dest, so a new playthrough starts with no
    level killed and no breadcrumbs from an earlier game. Moved, not deleted. Returns the moved names."""
    moved = []
    try:
        files = sorted(p for p in se_bg3ai.glob("nav_*.json") if p.is_file())
    except OSError:
        return moved
    for p in files:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest / p.name))
            moved.append(p.name)
        except OSError:
            continue
    return moved


def snapshot_tree(root: Path) -> dict[str, str]:
    """{relative path: text} of every file under root (play/knowledge: ~200 KB of markdown)."""
    out = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out[str(p.relative_to(root))] = p.read_text(errors="replace")
            except OSError:
                continue
    return out


def tree_changes(root: Path, snap: dict[str, str]) -> dict[str, str]:
    """{relative path: changed|added|removed} between the snapshot and the tree now."""
    now = snapshot_tree(root)
    out = {}
    for rel in sorted(set(snap) | set(now)):
        if rel not in now:
            out[rel] = "removed"
        elif rel not in snap:
            out[rel] = "added"
        elif snap[rel] != now[rel]:
            out[rel] = "changed"
    return out


def agent_written(streams: list[Path], root: Path, cwd: Path) -> set[str]:
    """Paths under root (relative to it) that the playing agent's Edit/Write calls targeted (relative paths are
    resolved from cwd, play/)."""
    out = set()
    root = root.resolve()
    for s in streams:
        for ev in _read_jsonl(s):
            content = (ev.get("message") or {}).get("content") if ev.get("type") == "assistant" else None
            for b in content if isinstance(content, list) else []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in WRITE_TOOLS):
                    continue
                fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                if not isinstance(fp, str) or not fp:
                    continue
                p = Path(fp)
                p = (p if p.is_absolute() else cwd / p).resolve()
                try:
                    out.add(str(p.relative_to(root)))
                except ValueError:
                    continue
    return out


def restore_agent_edits(root: Path, snap: dict[str, str], agent_files: set[str], keep: Path) -> dict:
    """FAIR-2: after a run, put back the knowledge files the agent changed (its version is kept under `keep`).
    Changes nobody can pin on the agent (an operator's edit meanwhile) are reported, never reverted."""
    changes = tree_changes(root, snap)
    restored, other = [], []
    for rel, how in changes.items():
        if rel not in agent_files:
            other.append(f"{rel} ({how})")
            continue
        p = root / rel
        try:
            if p.exists():
                (keep / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, keep / rel)
            if how == "added":
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(snap[rel])
            restored.append(f"{rel} ({how})")
        except OSError as e:
            other.append(f"{rel} ({how}; restore failed: {e})")
    return {"agent_edits": restored, "other_changes": other}


# ======================================================================== failure tagging

def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            v = json.loads(ln)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def event_failures(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(failures, operator actions) from the run's runner_events.jsonl records."""
    fails, ops = [], []
    starts = 0
    for e in events:
        k, t = e.get("kind"), e.get("t")
        if k == "fault":
            kind, cls = FAULT_KINDS.get(e.get("fault"), (f"fault_{e.get('fault')}", UNKNOWN))
            fails.append(failure(t, kind, cls, f"{e.get('session') or ''}: {e.get('detail') or e.get('fault')}"))
        elif k in HARNESS_EVENTS:
            detail = e.get("error") or e.get("why") or e.get("problems") or e.get("save") or ""
            fails.append(failure(t, HARNESS_EVENTS[k], HARNESS, detail if isinstance(detail, str) else
                                 json.dumps(detail)))
        elif k == "runner_start":
            starts += 1
            if starts > 1:
                ops.append({"t": t, "what": "runner started again by hand mid-run"})
        elif k == "runner_restart":
            ops.append({"t": t, "what": "runner restarted itself on changed code: " + ", ".join(e.get("files") or [])})
        elif k == "runner_stop" and e.get("why") == "stopped":
            ops.append({"t": t, "what": "runner stopped by the operator (play.py stop / signal)"})
    return fails, ops


def history_failures(sessions: list[dict]) -> list[dict]:
    """From runner.json history entries of the run's play sessions."""
    out = []
    for h in sessions:
        sid, t = h.get("session"), h.get("ended") or h.get("started")
        dur = (h.get("ended") or 0) - (h.get("started") or 0)
        if h.get("reason") == "killed":
            out.append(failure(t, "claude_killed", UNKNOWN, f"{sid}: claude ended by a signal from outside the runner"))
        elif h.get("reason") == "exited" and 0 <= dur < 60:
            out.append(failure(t, "claude_quick_exit", HARNESS, f"{sid}: claude exited after {dur:.0f} s "
                                                               f"(exit {h.get('exit')}: auth, config or usage)"))
        if h.get("early_exit_min") is not None:
            out.append(failure(t, "early_exit", AGENT, f"{sid}: ended the session with {h['early_exit_min']} min left"))
        if h.get("started_at_game_over"):
            out.append(failure(h.get("started"), "party_wipe", AGENT, f"{sid} started at the Game Over screen"))
    return out


def progress_failures(paths: list[Path], since: float) -> list[dict]:
    """Stalls: the progress poller's `stuck` records (no story progress for a while although the agent acted)."""
    out = []
    for p in paths:
        for r in _read_jsonl(p):
            if r.get("type") == "stuck" and (r.get("t") or 0) >= since:
                out.append(failure(r.get("t"), "stall", AGENT, f"{p.name.split('.')[0]}: no story progress for "
                                   f"{r.get('idle_min')} min ({r.get('actions')} tool calls meanwhile)"))
    return out


def stream_failures(stream: Path) -> list[dict]:
    """One play stream (Sxxx.jsonl / Sxxx-rN.jsonl): MCP server not connected, claude API/usage errors, tool
    exceptions and timeouts, fairness-guard blocks, fairness leaks, Game Over. Same-kind failures of one tool and
    error are grouped (n)."""
    sid = stream.name[:-len(".jsonl")]
    try:
        t_end = stream.stat().st_mtime
    except OSError:
        return []
    out = []
    for ev in _read_jsonl(stream):
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            for s in ev.get("mcp_servers") or []:
                if isinstance(s, dict) and s.get("name") == "bg3" and s.get("status") != "connected":
                    out.append(failure(None, "mcp_failed", HARNESS, f"{sid}: the bg3 MCP server is {s.get('status')}"))
        elif typ == "result" and ev.get("is_error") and API_ERROR_RE.search(str(ev.get("result") or "")):
            out.append(failure(t_end, "claude_api", HARNESS, f"{sid}: {ev.get('result')}"))
        elif typ == "assistant":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                txt = b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else ""
                if txt.lstrip().startswith(("API Error", "Claude AI usage limit")):
                    out.append(failure(t_end, "claude_api", HARNESS, f"{sid}: {txt.strip()}"))
    calls, _ = triage.load_calls(stream, Path(str(stream)[:-len(".jsonl")] + ".turns.jsonl"))
    groups: dict[tuple[str, str, str], list[dict]] = {}
    wipe = None
    for c in calls:
        name, text = c["name"], c.get("text") or ""
        if name in fairness.NOT_GAME:
            if GUARD_BLOCK in text:
                groups.setdefault(("fairness_block", name, triage.error_key(text)), []).append(c)
            continue
        if wipe is None and GAME_OVER_RE.search(text):
            wipe = c
        for step, msg in c["fails"]:
            if TOOL_EXCEPTION_RE.search(msg):
                kind = "tool_timeout" if TIMEOUT_RE.search(msg) else "tool_exception"
            elif TIMEOUT_RE.search(msg.split(":", 1)[0]) or msg.startswith("TIMEOUT"):
                kind = "tool_timeout"
            else:
                continue   # FAILED / NOT READY / REFUSED: the game said no (the agent's call), not our bug
            groups.setdefault((kind, step, triage.error_key(msg)), []).append(c)
    for (kind, tool, key), cs in groups.items():
        cls = AGENT if kind == "fairness_block" else HARNESS
        out.append(failure(cs[0].get("t") or t_end, kind, cls, f"{sid}: {tool}: {key}", len(cs)))
    if wipe is not None:
        out.append(failure(wipe.get("t") or t_end, "party_wipe", AGENT, f"{sid}: Game Over ({wipe['name']})"))
    for rule, hits in fairness.audit_calls(calls).items():
        if fairness.RULES[rule][0] == fairness.LEAK:
            out.append(failure(hits[0][0].get("t") or t_end, "fairness_leak", HARNESS,
                               f"{sid}: {rule}: {hits[0][1]}", len(hits)))
    return out


def reload_failures(actions: list[dict]) -> list[dict]:
    """Every load_game the agent called (runtime/actions.jsonl): a reload it chose."""
    out = []
    for a in actions:
        if a.get("tool") == "load_game":
            args = a.get("args") or {}
            out.append(failure(a.get("t"), "reload", AGENT, f"load_game {args.get('save') or args.get('label') or ''}"
                               .strip()))
    return out


def outcome_failure(outcome: str, why: str, t: float, others: list[dict]) -> list[dict]:
    """The run's own ending as a failure: a timeout is the agent's (unknown when harness failures hit the run),
    a give-up and a runner error are ours."""
    if outcome == "timeout":
        cls = UNKNOWN if any(f["class"] == HARNESS for f in others) else AGENT
        return [failure(t, "timeout", cls, why or "the run's time ran out before the milestone")]
    if outcome == "gave_up":
        return [failure(t, "gave_up", HARNESS, why)]
    if outcome == "runner_error":
        return [failure(t, "runner_error", HARNESS, why)]
    return []


def classify_run(state_dir: Path, run_started: float, now: float, sessions: list[dict], events: list[dict],
                 actions: list[dict], outcome: str, why: str) -> list[dict]:
    """Every failure of one run, in time order (failures without a time first)."""
    fails, _ = event_failures(events)
    fails += history_failures(sessions)
    files = []
    try:
        files = sorted(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        pass
    recent = []
    for f in files:
        try:
            if f.stat().st_mtime >= run_started:
                recent.append(f)
        except OSError:
            continue
    fails += progress_failures([f for f in recent if f.name.endswith(".progress.jsonl")], run_started)
    for f in recent:
        if PLAY_STREAM_RE.match(f.name):
            fails += stream_failures(f)
    fails += reload_failures(actions)
    fails += outcome_failure(outcome, why, now, fails)
    return sorted(fails, key=lambda f: (f["t"] is not None, f["t"] or 0))


def is_clean(outcome: str, failures: list[dict], operator: list[dict]) -> bool:
    """The milestone reached with no harness failure and no operator action: a run that scores the model alone."""
    return outcome == "reached" and not operator and not any(f.get("class") == HARNESS for f in failures)


# ======================================================================== the table

def class_counts(failures: list[dict]) -> Counter:
    c = Counter()
    for f in failures or []:
        c[f.get("class", UNKNOWN)] += int(f.get("n") or 1)
    return c


def clean_streak(records: list[dict]) -> int:
    """Clean runs in a row at the end of the series (the launch bar: 3)."""
    n = 0
    for r in reversed(records):
        if not r.get("clean"):
            break
        n += 1
    return n


def best_streak(records: list[dict]) -> int:
    best = cur = 0
    for r in records:
        cur = cur + 1 if r.get("clean") else 0
        best = max(best, cur)
    return best


def runs_table(records: list[dict]) -> str:
    """Plain-text table of a series' runs (play.py runs --playthrough <base>)."""
    head = ("run", "playthrough", "outcome", "clean", "wall min", "sessions", "harness", "agent", "unknown", "cost $",
            "top failures")
    rows = []
    for r in records:
        c = class_counts(r.get("failures"))
        kinds = Counter()
        for f in r.get("failures") or []:
            kinds[f"{f.get('kind')}({f.get('class', '?')[0]})"] += int(f.get("n") or 1)
        top = ", ".join(f"{k}×{n}" if n > 1 else k for k, n in kinds.most_common(4))
        outcome = r.get("outcome") or ("reached" if r.get("milestone") else "?")
        rows.append((str(r.get("run") or 1), str(r.get("playthrough") or ""), outcome,
                     "yes" if r.get("clean") else "no", f"{r.get('wall_min', 0):g}", str(r.get("sessions", "")),
                     str(c[HARNESS]), str(c[AGENT]), str(c[UNKNOWN]), f"{r.get('cost_usd', 0):.2f}", top))
    widths = [max(len(x) for x in col) for col in zip(head, *rows)] if rows else [len(h) for h in head]
    lines = ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in [head, *rows]]
    lines.insert(1, "  ".join("-" * w for w in widths))
    n = clean_streak(records)
    clean = sum(1 for r in records if r.get("clean"))
    lines += ["", f"{len(records)} run(s), {clean} clean; {n} clean in a row (latest), best streak "
                  f"{best_streak(records)}"]
    return "\n".join(lines)
