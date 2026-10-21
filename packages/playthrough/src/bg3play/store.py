# pyright: basic
"""The run store: one reader over the files today's runner, bridge and memory leave on disk.

    <runtime>/runner.status.json             the runner's live status (play.py write_status)
    <runtime>/actions.jsonl                  every bridge tool call (t, tool, args, ms, result)
    <runtime>/shots/*.png|jpg                screen captures (latest = the Watch screen's picture)
    <runtime>/sessions/<pt>/runner.json      session history (started, ended, model, minutes)
    <runtime>/sessions/<pt>/Sxxx.log         the agent's stream as text ([say], [tool], [result], [end] ...)
    <runtime>/sessions/<pt>/Sxxx.latency.json, Sxxx.progress.json, Sxxx.fights.jsonl, progress_history.jsonl
    <runtime>/sessions/<pt>/positions.jsonl  party positions (positions.py; written by the app's sampler)
    <playthroughs>/<pt>/memory/              the memory dir, its own git repo
    <playthroughs>/<pt>/playthrough.toml     (config.py)

Paths written into runner.status.json are absolute on the machine that wrote them; the store only uses their file
names inside its own runtime dir, so a copied run (a test fixture) reads the same. Git is behind `GitRunner`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import parse as P

SESSION_LOG_RE = re.compile(r"^(S\d{3,})\.log$")
MEMORY_SUFFIXES = (".md", ".json", ".jsonl", ".txt")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

GitRunner = Callable[[Path, list[str]], str]  # (repo dir, git args) -> stdout; raises on failure


def run_git(repo: Path, args: list[str]) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=10
    ).stdout


class StoreError(ValueError):
    """A request for something the store refuses (bad name, path outside the memory dir)."""


def read_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def read_tail(path: Path, max_bytes: int) -> list[str]:
    """The last complete lines within `max_bytes` of a file ([] when missing)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > max_bytes and lines else lines


class RunStore:
    def __init__(self, runtime: Path, playthroughs: Path, git: GitRunner = run_git):
        self.runtime = runtime
        self.playthroughs_dir = playthroughs
        self.git = git

    # ------------------------------------------------------------ paths

    def check_name(self, pt: str) -> str:
        if not NAME_RE.match(pt):
            raise StoreError(f"bad playthrough name {pt!r}")
        return pt

    def session_dir(self, pt: str) -> Path:
        return self.runtime / "sessions" / self.check_name(pt)

    def memory_dir(self, pt: str) -> Path:
        return self.playthroughs_dir / self.check_name(pt) / "memory"

    def positions_path(self, pt: str) -> Path:
        return self.session_dir(pt) / "positions.jsonl"

    @property
    def actions_path(self) -> Path:
        return self.runtime / "actions.jsonl"

    @property
    def status_path(self) -> Path:
        return self.runtime / "runner.status.json"

    # ------------------------------------------------------------ runner

    def status(self) -> dict:
        return read_json(self.status_path)

    def current(self) -> str | None:
        pt = self.status().get("playthrough")
        return pt if isinstance(pt, str) and NAME_RE.match(pt) else None

    def playthroughs(self) -> list[str]:
        """Playthroughs with a session dir holding a runner.json or a session log, sorted by name."""
        try:
            dirs = sorted(d for d in (self.runtime / "sessions").iterdir() if d.is_dir() and NAME_RE.match(d.name))
        except OSError:
            return []
        return [d.name for d in dirs if (d / "runner.json").exists() or any(d.glob("S*.log"))]

    def session_logs(self, pt: str) -> list[Path]:
        """Play session logs (Sxxx.log; not -fix/-keeper passes), in session order."""
        try:
            logs = [f for f in self.session_dir(pt).iterdir() if SESSION_LOG_RE.match(f.name)]
        except OSError:
            return []
        return sorted(logs, key=lambda f: int(f.name[1:-4]))

    def agent_log(self, pt: str) -> Path | None:
        """The log the runner is writing for `pt` (runner.status.json "log", by file name), else its last one."""
        st = self.status()
        if st.get("playthrough") == pt and isinstance(st.get("log"), str):
            p = self.session_dir(pt) / Path(st["log"]).name
            if p.exists():
                return p
        logs = self.session_logs(pt)
        return logs[-1] if logs else None

    # ------------------------------------------------------------ sessions

    def sessions(self, pt: str) -> list[dict]:
        """One row per play session: runner.json history joined with the log's [end] line and latency summary."""
        d = self.session_dir(pt)
        hist = {
            h.get("session"): h
            for h in read_json(d / "runner.json").get("history") or []
            if isinstance(h, dict) and h.get("kind", "play") == "play"
        }
        rows = []
        for log in self.session_logs(pt):
            sid = log.stem
            end: dict[str, Any] = {"outcome": None, "turns": None, "minutes": None, "cost_usd": None}
            for line in reversed(read_tail(log, 8192)):
                e = P.log_line(line)
                if e and e["kind"] == "end":
                    end = P.end_line(e["text"])
                    break
            h = hist.get(sid, {})
            lat = read_json(d / f"{sid}.latency.json")
            prog = read_json(d / f"{sid}.progress.json")
            rows.append(
                {
                    "session": sid,
                    "started": h.get("started"),
                    "ended": h.get("ended"),
                    "model": h.get("model"),
                    "effort": h.get("effort"),
                    **end,
                    "minutes": end["minutes"] or h.get("minutes") or lat.get("minutes"),
                    "latency": latency_summary(lat) if lat else None,
                    "points": prog.get("points"),
                    "points_per_hour": prog.get("per_hour"),
                }
            )
        return rows

    def log_entries(self, path: Path, max_bytes: int = 256 * 1024) -> list[dict]:
        return [e for e in (P.log_line(ln) for ln in read_tail(path, max_bytes)) if e]

    def recent_actions(self, max_bytes: int = 1024 * 1024) -> list[dict]:
        return [a for a in (P.action(ln) for ln in read_tail(self.actions_path, max_bytes)) if a]

    # ------------------------------------------------------------ memory

    def memory_text(self, pt: str, name: str) -> str:
        try:
            return (self.memory_dir(pt) / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def memory_tree(self, pt: str) -> list[dict]:
        """Text files in the memory dir (no .git, no hidden files), sorted by path."""
        root = self.memory_dir(pt)
        out = []
        for f in sorted(root.rglob("*")):
            rel = f.relative_to(root)
            if any(part.startswith(".") for part in rel.parts) or not f.is_file() or f.suffix not in MEMORY_SUFFIXES:
                continue
            st = f.stat()
            out.append({"path": rel.as_posix(), "size": st.st_size, "mtime": st.st_mtime})
        return out

    def memory_path(self, pt: str, rel: str) -> Path:
        root = self.memory_dir(pt).resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or any(part.startswith(".") for part in p.relative_to(root).parts):
            raise StoreError(f"path outside the memory dir: {rel!r}")
        if p.suffix not in MEMORY_SUFFIXES:
            raise StoreError(f"not a memory text file: {rel!r}")
        return p

    def memory_file(self, pt: str, rel: str) -> str | None:
        try:
            return self.memory_path(pt, rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def memory_log(self, pt: str, limit: int = 50, rel: str | None = None) -> list[dict]:
        """Memory commits, newest first (the memory dir is a git repo); [] when it is not one."""
        root = self.memory_dir(pt)
        if not (root / ".git").exists():
            return []
        args = ["log", f"-n{int(limit)}", f"--format={P.GIT_LOG_FORMAT}"]
        if rel:
            args += ["--", self.memory_path(pt, rel).relative_to(root.resolve()).as_posix()]
        return P.git_log(self.git(root, args))

    def memory_diff(self, pt: str, rel: str, sha: str) -> str | None:
        """One commit's patch of one memory file (`git show`), or None for a commit the repo doesn't have."""
        root = self.memory_dir(pt)
        if not SHA_RE.match(sha):
            raise StoreError(f"bad commit id {sha!r}")
        if not (root / ".git").exists():
            return None
        path = self.memory_path(pt, rel).relative_to(root.resolve()).as_posix()
        try:
            return self.git(root, ["show", "--format=", "--no-color", "--no-ext-diff", sha, "--", path])
        except subprocess.CalledProcessError:
            return None

    # ------------------------------------------------------------ timeline, screen

    def fights(self, pt: str) -> list[dict]:
        d = self.session_dir(pt)
        rows = []
        for f in sorted(d.glob("S*.fights.jsonl")):
            rows += [x for x in (P.fight(r) for r in read_jsonl(f)) if x]
        return rows

    def latest_screenshot(self) -> Path | None:
        try:
            shots = [f for f in (self.runtime / "shots").iterdir() if f.suffix.lower() in IMAGE_SUFFIXES]
        except OSError:
            return None
        return max(shots, key=lambda f: (f.stat().st_mtime, f.name)) if shots else None


def latency_summary(lat: dict) -> dict:
    """The few Sxxx.latency.json numbers the cost/latency strip shows."""
    targets = lat.get("targets") or {}
    return {
        "apm": lat.get("apm"),
        "busy_pct": lat.get("busy_pct"),
        "think_overlap_pct": lat.get("think_overlap_pct"),
        "idle_median_s": (targets.get("median_idle_explore_s") or {}).get("value"),
        "long_gaps": (targets.get("long_gaps_non_dialogue") or {}).get("value"),
        "bridge_rt_ms": (targets.get("bridge_rt_median_ms") or {}).get("value"),
    }
