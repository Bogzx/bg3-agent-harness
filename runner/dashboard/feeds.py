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
    return {"now": now, "live": live, "history": rows, "weights": P.WEIGHTS, "stuck_after_min": P.STUCK_MIN,
            "files": {"live": live_path.name if live_path else None, "history": hist.name if hist and hist.exists() else None}}
