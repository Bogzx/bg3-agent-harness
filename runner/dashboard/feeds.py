"""Pure helpers for the live dashboard (stdlib only): file tailing, clock, state shaping.

Everything here is side-effect free apart from reading files, so it is unit-tested without a game.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
SESSION_LOG_RE = re.compile(r"^S(\d{3,})\.md$")


# ---------------------------------------------------------------- files


def read_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def read_text(path: Path | None, strip_comments: bool = True, limit: int = 20000) -> str | None:
    if path is None:
        return None
    try:
        t = path.read_text(errors="replace")
    except OSError:
        return None
    if strip_comments:
        t = COMMENT_RE.sub("", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.strip()
    return t[:limit] + ("\n…" if len(t) > limit else "")


def tail_lines(path: Path | None, n: int = 30, strip_comments: bool = True) -> list[str]:
    t = read_text(path, strip_comments=strip_comments, limit=10**7)
    if not t:
        return []
    return t.splitlines()[-n:]


def read_new(path: Path, offset: int, initial_bytes: int = 64 * 1024,
             max_bytes: int = 512 * 1024) -> tuple[int, str, bool]:
    """Incremental tail. Returns (new_offset, complete_lines_text, reset).

    offset < 0 means "start near the end" (the last `initial_bytes`, from a line boundary). If the file
    shrank (rotated or truncated), restart from 0. `reset` tells the client to clear what it shows.
    Only complete lines are returned; a trailing partial line is left for the next call.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0, "", offset > 0
    reset = offset < 0 or offset > size
    if offset < 0:
        start = max(0, size - initial_bytes)
    elif offset > size:
        start = 0
    else:
        start = offset
    skip_partial = offset < 0 and start > 0
    if start >= size:
        return start, "", reset
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(max_bytes)
    if skip_partial:
        nl = data.find(b"\n")
        if nl < 0:
            return start, "", reset
        start += nl + 1
        data = data[nl + 1:]
    cut = data.rfind(b"\n")
    if cut < 0:
        # No newline at all: wait for more, unless one giant line fills the window (skip it).
        return (start + len(data) if len(data) >= max_bytes else start), "", reset
    chunk = data[: cut + 1]
    return start + len(chunk), chunk.decode("utf-8", errors="replace"), reset


def parse_actions(text: str, result_limit: int = 2500) -> list[dict]:
    """actions.jsonl lines -> feed items. Bad lines are skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        result = str(e.get("result", ""))
        out.append({
            "t": e.get("t"),
            "tool": str(e.get("tool", "?")),
            "args": e.get("args") or {},
            "ms": e.get("ms"),
            "result": result[:result_limit] + ("…" if len(result) > result_limit else ""),
            "ok": not re.match(r"^(FAILED|ERROR|error|Blocked|NOT READY)", result),
            "kind": tool_kind(str(e.get("tool", ""))),
        })
    return out


def tool_kind(tool: str) -> str:
    t = tool.removeprefix("mcp__bg3__")
    if t in ("screenshot", "zoom"):
        return "vision"
    if t.startswith(("get_", "list_", "look_", "session_clock", "wiki", "ability_preview")):
        return "read"
    if t in ("save_game", "load_game"):
        return "save"
    return "act"


# ---------------------------------------------------------------- clock


def session_clock(runner_status: dict, session_file: dict, mem_state: dict, now: float | None = None,
                  default_minutes: float = 60.0) -> dict:
    """Deadline info from (in order) runner.status.json, runtime/session.json, memory/.session.json."""
    now = now or time.time()
    deadline = None
    source = None
    started = None
    for src, d in (("runner", runner_status), ("session.json", session_file)):
        for key in ("deadline", "BG3AI_SESSION_DEADLINE", "session_deadline"):
            if d.get(key):
                try:
                    deadline = float(d[key])
                    source = src
                    started = d.get("started")
                    break
                except (TypeError, ValueError):
                    pass
        if deadline:
            break
    if deadline is None and mem_state.get("open") and mem_state.get("started_epoch"):
        started = float(mem_state["started_epoch"])
        deadline = started + default_minutes * 60
        source = "memory"
    if deadline is None:
        return {"deadline": None, "left_s": None, "source": None, "phase": "idle"}
    left = deadline - now
    wrap_s = 10 * 60
    phase = "over" if left <= 0 else ("wrapup" if left <= wrap_s else "play")
    return {"deadline": deadline, "left_s": round(left), "source": source, "phase": phase,
            "started": started, "hard_stop": runner_status.get("hard_stop") if source == "runner" else None}


# ---------------------------------------------------------------- game state shaping


def class_str(c: Any) -> str:
    if not isinstance(c, dict):
        return str(c)
    name = c.get("class") or c.get("name") or "?"
    s = f"{name} {c['level']}" if c.get("level") else str(name)
    return s + (f" ({c['subclass']})" if c.get("subclass") else "")


def status_names(sts: Any) -> list[str]:
    out = []
    for s in sts or []:
        out.append(str(s.get("name") or s.get("id") or "?") if isinstance(s, dict) else str(s))
    return out


def shape_status(st: dict) -> dict:
    """state.status -> a GUID-free summary for the page."""
    active = st.get("active")
    party = []
    names = {}
    for m in st.get("party") or []:
        if not isinstance(m, dict):
            continue
        if m.get("uuid"):
            names[str(m["uuid"]).lower()] = m.get("name", "?")
        party.append({
            "name": m.get("name", "?"),
            "classes": ", ".join(class_str(c) for c in (m.get("classes") or [])) or (
                f"L{m['level']}" if m.get("level") else ""),
            "hp": m.get("hp"), "max_hp": m.get("max_hp"), "temp_hp": m.get("temp_hp"),
            "statuses": status_names(m.get("statuses")),
            "downed": bool(m.get("downed")), "dead": bool(m.get("dead")),
            "in_combat": bool(m.get("in_combat")),
            "active": bool(active and m.get("uuid") == active),
        })
    combat = st.get("combat") if isinstance(st.get("combat"), dict) else None
    if combat:
        cur = combat.get("current")
        if isinstance(cur, str) and UUID_RE.fullmatch(cur):
            combat = dict(combat, current=names.get(cur.lower(), "?"))
    mode = "combat" if st.get("in_combat") else ("dialogue" if st.get("in_dialogue") else "exploring")
    return {
        "region": st.get("region"), "subregion": st.get("subregion"), "level": st.get("level"),
        "gold": st.get("gold"), "mode": mode, "turn_based": bool(st.get("turn_based")),
        "combat": scrub(combat) if combat else None, "party": party,
    }


def scrub(v: Any) -> Any:
    """Remove GUIDs anywhere in a JSON-ish value (dashboard never needs them)."""
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items() if k not in ("uuid", "guid")}
    if isinstance(v, list):
        return [scrub(x) for x in v]
    if isinstance(v, str):
        return UUID_RE.sub("…", v)
    return v


def event_text(e: dict) -> str:
    data = e.get("data")
    if isinstance(data, dict):
        parts = [f"{k}={scrub(v)}" for k, v in data.items() if v not in (None, "", [], {}) and k != "uuid"]
        return f"{e.get('kind', '?')} " + " ".join(str(p) for p in parts)
    return f"{e.get('kind', '?')} {scrub(data) if data is not None else ''}".strip()


# ---------------------------------------------------------------- memory


def current_session_log(mem: Path) -> Path | None:
    st = read_json(mem / ".session.json")
    if st.get("current"):
        p = mem / "sessions" / f"S{int(st['current']):03d}.md"
        if p.exists():
            return p
    d = mem / "sessions"
    try:
        logs = [f for f in d.iterdir() if SESSION_LOG_RE.match(f.name)]
    except OSError:
        return None
    return max(logs, key=lambda f: int(SESSION_LOG_RE.match(f.name).group(1))) if logs else None


def memory_view(mem: Path, log_lines: int = 25) -> dict:
    st = read_json(mem / ".session.json")
    log = current_session_log(mem)
    return {
        "dir": str(mem),
        "exists": mem.is_dir(),
        "session": f"S{int(st['current']):03d}" if st.get("current") else None,
        "session_open": bool(st.get("open")),
        "handoff": read_text(mem / "HANDOFF.md"),
        "objectives": read_text(mem / "objectives.md"),
        "log_name": log.name if log else None,
        "log_tail": tail_lines(log, log_lines),
    }


def resolve_memory(repo: Path, runner_status: dict, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    pt = runner_status.get("playthrough") or os.environ.get("BG3AI_PLAYTHROUGH")
    if pt and (repo / "playthroughs" / pt / "memory").is_dir():
        return repo / "playthroughs" / pt / "memory"
    return repo / "play" / "memory"


def resolve_agent_log(runtime: Path, runner_status: dict) -> Path | None:
    """The agent transcript (.log) the runner is writing, else the newest one for the playthrough."""
    p = runner_status.get("log")
    if p and Path(p).exists():
        return Path(p)
    pt = runner_status.get("playthrough")
    d = runtime / "sessions" / pt if pt else runtime / "sessions"
    try:
        logs = list(d.glob("S*.log")) if pt else list(d.glob("*/S*.log"))
    except OSError:
        return None
    return max(logs, key=lambda f: f.stat().st_mtime) if logs else None


# ---------------------------------------------------------------- latency


def resolve_latency_paths(runtime: Path, runner_status: dict) -> dict:
    """The live session's bridge metrics + stream timing files, and the playthrough's history."""
    metrics = runner_status.get("metrics")
    turns = runner_status.get("turns")
    pt = runner_status.get("playthrough")
    hist = runtime / "sessions" / pt / "latency_history.jsonl" if pt else None
    m = Path(metrics) if metrics and Path(metrics).exists() else runtime / "metrics.jsonl"
    return {"metrics": m if m.exists() else None,
            "turns": Path(turns) if turns and Path(turns).exists() else None,
            "history": hist if hist and hist.exists() else None}


def _tail_jsonl(path: Path | None, max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    if path is None:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # partial first line
    out = []
    for ln in lines:
        try:
            v = json.loads(ln)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def latency_payload(runtime: Path, runner_status: dict, now: float | None = None, history_rows: int = 30) -> dict:
    """/api/latency: live view (bg3ai.metrics.live_view) plus the per-session history table."""
    from bg3ai import metrics as M  # noqa: PLC0415 - bridge/src is on sys.path (serve.py)

    now = time.time() if now is None else now
    paths = resolve_latency_paths(runtime, runner_status)
    recs = _tail_jsonl(paths["metrics"])
    tools = [r for r in recs if r.get("type") == "tool" and "t0" in r]
    samples = [r for r in recs if r.get("type") == "sample" and "t" in r]
    turns = _tail_jsonl(paths["turns"])
    live = M.live_view(tools, samples, turns, now) if (tools or samples or turns) else None
    hist = _tail_jsonl(paths["history"])[-history_rows:]
    return {"now": now, "live": live, "history": hist, "targets": M.TARGETS,
            "files": {k: (v.name if v else None) for k, v in paths.items()}}


# ---------------------------------------------------------------- story progress


def progress_payload(runtime: Path, runner_status: dict, now: float | None = None, history_rows: int = 30) -> dict:
    """/api/progress: live counter of the running session (bg3ai.progress.live_view) + per-session history."""
    from bg3ai import progress as P  # noqa: PLC0415 - bridge/src is on sys.path (serve.py)

    now = time.time() if now is None else now
    live_path = runner_status.get("progress")
    live_path = Path(live_path) if live_path and Path(live_path).exists() else None
    pt = runner_status.get("playthrough")
    hist = runtime / "sessions" / pt / "progress_history.jsonl" if pt else None
    live = None
    if live_path is not None:
        started = runner_status.get("started") or now
        ended = runner_status.get("session_ended")
        live = P.live_view(_tail_jsonl(live_path), float(started), float(ended) if ended else now)
        live["session"] = runner_status.get("session")
        live["running"] = not ended
    rows = _tail_jsonl(hist if hist and hist.exists() else None)[-history_rows:]
    ms = milestones_payload(runtime / "sessions" / pt) if pt else None
    return {"now": now, "live": live, "history": rows, "weights": P.WEIGHTS, "stuck_after_min": P.STUCK_MIN,
            "milestones": ms,
            "files": {"live": live_path.name if live_path else None, "history": hist.name if hist and hist.exists() else None}}


# ---------------------------------------------------------------- goal scorecard (all sessions)

# The user's /goal, as per-session pass/fail lines. The first three are bg3ai.metrics.TARGETS (the latency goal);
# the rest are provisional bars for "feels like a skilled human playing live" (tune here).
#   key, label, unit, op, target, digits
GOALS = [
    ("idle", "idle median (outside combat)", "s", "<", 2.0, 2),
    ("gaps", "gaps >10 s (not dialogue)", "", "==", 0, 0),
    ("rt", "bridge round trip median", "ms", "<", 100.0, 0),
    ("apm", "actions / min", "", ">=", 10.0, 1),
    ("busy", "busy (party acting)", "%", ">=", 60.0, 0),
    ("pipe", "pipelined actions", "%", ">=", 30.0, 0),
    # out of combat/dialogue: share of the agent's working time with the party busy (metrics.think_overlap)
    ("overlap", "think overlap (acting while it thinks)", "%", ">=", 50.0, 0),
    ("pts", "story points / hour", "", ">=", 90.0, 0),
]
SESSION_FILE_RE = re.compile(r"^(S\d{3,}(?:-r\d+)?)\.(metrics\.jsonl|latency\.json|progress\.jsonl|progress\.json)$")
_CACHE: dict[tuple, Any] = {}


def _cached(key: tuple, fn):
    if key not in _CACHE:
        if len(_CACHE) > 200:
            _CACHE.clear()
        _CACHE[key] = fn()
    return _CACHE[key]


def _sig(p: Path) -> tuple:
    try:
        st = p.stat()
        return (str(p), st.st_size, st.st_mtime)
    except OSError:
        return (str(p), None, None)


def session_ids(d: Path) -> list[str]:
    """Play sessions (and crash-resume legs, Sxxx-rN) that left any metrics or progress file, in play order."""
    try:
        names = {m.group(1) for f in d.iterdir() if (m := SESSION_FILE_RE.match(f.name))}
    except OSError:
        return []
    def order(s: str):
        m = re.match(r"S(\d+)(?:-r(\d+))?", s)
        return (int(m.group(1)), int(m.group(2) or 0))
    return sorted(names, key=order)


def goal_values(lat: dict | None, prog: dict | None) -> dict:
    """One session's latency + progress summaries → {goal key: value}."""
    lat, prog = lat or {}, prog or {}
    t = lat.get("targets") or {}
    v = lambda k: (t.get(k) or {}).get("value")  # noqa: E731
    return {"idle": v("median_idle_explore_s"),
            "gaps": v("long_gaps_non_dialogue") if (t.get("long_gaps_non_dialogue") or {}).get("pass") is not None else None,
            "rt": v("bridge_rt_median_ms"), "apm": lat.get("apm") if lat.get("actions") else None,
            "busy": lat.get("busy_pct"), "pipe": lat.get("pipelining_pct"),
            "overlap": lat.get("think_overlap_pct"), "pts": prog.get("per_hour")}


def goal_pass(op: str, value, target) -> bool | None:
    if value is None:
        return None
    return {"<": value < target, "==": value == target, ">=": value >= target}[op]


def _session_summaries(d: Path, sid: str, live: bool, started: float | None, now: float) -> tuple[dict | None, dict | None]:
    from bg3ai import metrics as M  # noqa: PLC0415
    from bg3ai import progress as P  # noqa: PLC0415

    lat_f, met_f, turns_f = d / f"{sid}.latency.json", d / f"{sid}.metrics.jsonl", d / f"{sid}.turns.jsonl"
    prog_f, progl_f = d / f"{sid}.progress.json", d / f"{sid}.progress.jsonl"
    if lat_f.exists() and not live:
        lat = read_json(lat_f)
    elif met_f.exists() or turns_f.exists():
        # the live session, or one that ended without a summary (runner restarted mid-session)
        end = now if live else None
        lat = _cached(("lat", _sig(met_f), _sig(turns_f), started if live else None, int(now / 15) if live else 0),
                      lambda: M.summarize_files(met_f if met_f.exists() else None, turns_f if turns_f.exists() else None,
                                                started if live else None, end))
    else:
        lat = None
    if prog_f.exists() and not live:
        prog = read_json(prog_f)
    elif progl_f.exists():
        recs = P.load(progl_f)
        s0 = started if live and started else (lat or {}).get("start") or min((r.get("t", now) for r in recs), default=now)
        s1 = now if live else (lat or {}).get("end") or max((r.get("t", s0) for r in recs), default=s0)
        prog = P.summarize(recs, float(s0), float(s1))
    else:
        prog = None
    return lat, prog


def goals_payload(runtime: Path, runner_status: dict, now: float | None = None, max_sessions: int = 30) -> dict:
    """/api/goals: every session's goal metrics side by side with PASS/FAIL, plus the trend across sessions."""
    now = time.time() if now is None else now
    pt = runner_status.get("playthrough")
    if not pt:
        return {"now": now, "sessions": [], "goals": [], "playthrough": None}
    d = runtime / "sessions" / pt
    live_sid = runner_status.get("session") if not runner_status.get("session_ended") else None
    sessions = []
    for sid in session_ids(d)[-max_sessions:]:
        live = sid == live_sid
        lat, prog = _session_summaries(d, sid, live, runner_status.get("started"), now)
        vals = goal_values(lat, prog)
        verdict = {k: goal_pass(op, vals[k], tgt) for k, _l, _u, op, tgt, _d in GOALS}
        judged = [p for p in verdict.values() if p is not None]
        minutes = (lat or prog or {}).get("minutes")
        sessions.append({"session": sid, "live": live, "minutes": minutes,
                         "short": not live and (minutes or 0) < 1,  # a runner restart's stub: shown, not counted
                         "start": (lat or {}).get("start"), "values": vals, "pass": verdict,
                         "passed": sum(judged), "judged": len(judged),
                         "points": (prog or {}).get("points")})
    goals = []
    counted = [s for s in sessions if not s["short"] and not s["live"]]
    for k, label, unit, op, tgt, nd in GOALS:
        done = [s["values"][k] for s in counted if s["values"][k] is not None]
        judged = [s["pass"][k] for s in counted if s["pass"][k] is not None]
        goals.append({"key": k, "label": label, "unit": unit, "op": op, "target": tgt, "digits": nd,
                      "series": [None if s["short"] else s["values"][k] for s in sessions],
                      "best": (min if op in ("<", "==") else max)(done) if done else None,
                      "pass_rate": round(100 * sum(judged) / len(judged)) if judged else None})
    return {"now": now, "playthrough": pt, "sessions": sessions, "goals": goals,
            "milestones": milestones_payload(d), "unattended": unattended_payload(d, runner_status, now)}


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


FAULT_KINDS = ("crash", "hang", "agent_hang", "setup")


def unattended_payload(d: Path, runner_status: dict, now: float | None = None, last_n: int = 8,
                       pid_alive=_pid_alive) -> dict | None:
    """Unattended hours and crashes/recoveries from runtime/sessions/<pt>/runner_events.jsonl (play.py Runner.event).

    A run = runner_start → runner_stop (or the next start: the runner was killed hard; or now, for the live runner).
    Unattended hours = how long the runner has gone without an operator (re)starting it."""
    now = time.time() if now is None else now
    evs = _tail_jsonl(d / "runner_events.jsonl")
    if not evs:
        return None
    runs, cur = [], None
    for e in evs:
        if e.get("kind") == "runner_start":
            if cur is not None:
                runs.append({**cur, "end": cur["last"], "why": "no stop event (killed?)"})
            cur = {"start": e["t"], "last": e["t"]}
        elif cur is not None:
            cur["last"] = e["t"]
            if e.get("kind") == "runner_stop":
                runs.append({**cur, "end": e["t"], "why": e.get("why")})
                cur = None
    live = None
    if cur is not None:
        if runner_status.get("runner_pid") and pid_alive(runner_status["runner_pid"]):
            live = {**cur, "end": now, "why": None}
            runs.append(live)
        else:
            beat = max(cur["last"], float(runner_status.get("heartbeat") or 0))
            runs.append({**cur, "end": beat, "why": "no stop event (killed?)"})
    hours = [max(0.0, r["end"] - r["start"]) / 3600 for r in runs]
    faults = {k: 0 for k in FAULT_KINDS}
    for e in evs:
        if e.get("kind") == "fault":
            faults[e.get("fault", "crash")] = faults.get(e.get("fault", "crash"), 0) + 1
    rec = [e for e in evs if e.get("kind") == "recovered"]
    secs = sorted(e["seconds"] for e in rec if isinstance(e.get("seconds"), (int, float)))
    shown = [e for e in evs if e.get("kind") in ("fault", "recovered", "recovery_failed", "steam_refused",
                                                  "setup_failed", "launch_failed", "launch_gave_up", "runner_stop")]
    alert = runner_status.get("alert") if isinstance(runner_status.get("alert"), dict) else None
    return {"live": live is not None,
            "current_h": round(hours[-1], 2) if live is not None else None,
            "longest_h": round(max(hours), 2) if hours else None,
            "total_h": round(sum(hours), 2), "runs": len(runs),
            "last_stop": next((r["why"] for r in reversed(runs) if r is not live), None),
            "faults": faults, "fault_total": sum(faults.values()),
            "recovered": len(rec), "failed": sum(e.get("kind") == "recovery_failed" for e in evs),
            "steam_refused": sum(e.get("kind") == "steam_refused" for e in evs),
            "launch_failed": sum(e.get("kind") == "launch_failed" for e in evs),
            "alert": alert,
            "steam_offline": runner_status.get("steam_offline"),
            "recovery_s_median": secs[len(secs) // 2] if secs else None,
            "recovery_s_max": secs[-1] if secs else None,
            "events": shown[-last_n:]}


def milestones_payload(d: Path) -> dict | None:
    """Story milestones for the playthrough dir (bg3ai.milestones), with the session that first reached each."""
    from bg3ai import milestones as MS  # noqa: PLC0415
    from bg3ai import progress as P  # noqa: PLC0415

    seen = read_json(d / "progress_state.json")
    sessions = [(sid, P.load(d / f"{sid}.progress.jsonl")) for sid in session_ids(d)]
    if not seen and not any(r for _s, r in sessions):
        return None
    st = MS.status(seen, sessions)
    st["line"] = MS.line(st)
    return st
