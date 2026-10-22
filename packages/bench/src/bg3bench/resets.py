# pyright: basic
"""What every run of a series starts without (moved from runner/runs.py; runs.py re-exports them).

NAV-3: the mod's navigation files are kept away from a run. The mod names them per host character
(Navigation.lua: nav_<host uuid>.json breadcrumbs, nav_guard_<host uuid>.json kill list; nav_guard.json only before a
host exists), and the per-user Script Extender folder is shared with every other playthrough on the machine, so
`isolate_nav` moves only the start host's two files and `restore_nav` puts them back after the run (`recover_nav`
after a crash, from the marker). A new game needs nothing: its host is a fresh uuid. `archive_nav_state` (every
nav_*.json) is the old rule play.py still calls. FAIR-2: play/knowledge is locked (chmod a-w) for a series, and the
knowledge files the playing agent changed are put back after each run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def archive_nav_state(se_bg3ai: Path, dest: Path) -> list[str]:
    """NAV-3: move the mod's machine-global navigation files (the per-level kill lists nav_guard*.json and the
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


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
INSPECT = Path(__file__).resolve().parents[4] / "tools" / "saves" / "inspect.py"


class NavIsolationError(RuntimeError):
    pass


def host_nav_files(host: str) -> tuple[str, str]:
    """The mod's two files for one host character (Navigation.lua N.guardFile and the breadcrumb mirror)."""
    if not UUID_RE.match(host):
        raise NavIsolationError(f"not a host uuid: {host!r}")
    return f"nav_{host}.json", f"nav_guard_{host}.json"


def isolate_nav(se_bg3ai: Path, dest: Path, marker: Path, start_kind: str, host: str | None) -> list[str]:
    """Before a run: a new game moves nothing (its host is a new uuid, so no file is its own); a save start moves its
    host's two files into dest. The legacy nav_guard.json is never moved: the mod reads it only while no host exists,
    so it is no one's and a save-start run never reads it. The marker (written before the first move) lets
    recover_nav put the files back after a crash; an older marker is recovered first. Returns the moved names."""
    recover_nav(marker)
    if start_kind == "new_game":
        return []
    if start_kind != "save" or host is None:
        raise NavIsolationError(f"a {start_kind} start needs its host uuid to isolate the nav files")
    names = [n for n in host_nav_files(host) if (se_bg3ai / n).is_file()]
    if not names:
        return []
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"se": str(se_bg3ai), "dest": str(dest), "files": names}))
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.move(str(se_bg3ai / n), str(dest / n))
    return names


def restore_nav(marker: Path) -> list[str]:
    """After a run: the moved files go back; the run's own versions of them are kept in <dest>/run/. Idempotent (a
    file already back is skipped); the marker goes last. No marker: nothing was moved, []."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return []
    se, dest = Path(m["se"]), Path(m["dest"])
    back = []
    for n in m["files"]:
        if not (dest / n).is_file():
            continue
        if (se / n).exists():
            (dest / "run").mkdir(parents=True, exist_ok=True)
            shutil.move(str(se / n), str(dest / "run" / n))
        shutil.move(str(dest / n), str(se / n))
        back.append(n)
    marker.unlink(missing_ok=True)
    return back


def recover_nav(marker: Path) -> list[str]:
    """At a runner's start: a marker left by a run that crashed between isolate and restore is restored."""
    return restore_nav(marker) if marker.exists() else []


@contextmanager
def nav_isolated(se_bg3ai: Path, dest: Path, marker: Path, start_kind: str, host: str | None) -> Iterator[list[str]]:
    """isolate_nav for the block, restore_nav after it, on an exception too."""
    moved = isolate_nav(se_bg3ai, dest, marker, start_kind, host)
    try:
        yield moved
    finally:
        restore_nav(marker)


def start_host(save_lsv: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """The start save's host character uuid, from the mod's save inspector (tools/saves/inspect.py, run as a
    subprocess: its name shadows the stdlib inspect module, so it can't be imported). Raises when the report has no
    valid `host_uuid`."""
    p = run([sys.executable, str(INSPECT), str(save_lsv), "--shallow"], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise NavIsolationError(f"inspect.py {save_lsv} failed (rc {p.returncode}): {p.stderr.strip()[:300]}")
    try:
        host = json.loads(p.stdout).get("host_uuid")
    except (ValueError, AttributeError):
        host = None
    if not isinstance(host, str) or not UUID_RE.match(host):
        raise NavIsolationError(f"inspect.py reports no host_uuid for {save_lsv}")
    return host


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


def lock_tree(root: Path, record: Path) -> int:
    """FAIR-2, second fence: chmod a-w every file under root for a benchmark series (the clients already have no
    file tools; this makes an accidental write fail loudly, the agent's or an operator's mid-series edit alike).
    Files only: git and a hand `cp` can still replace one, and git ignores the write bit. The original modes go to
    record first; an existing record (an execv'd successor re-locking, or a runner killed while locked) is merged,
    never overwritten, so unlock_tree always restores the modes from before the first lock. Returns files locked."""
    try:
        saved = json.loads(record.read_text())
    except (OSError, ValueError):
        saved = {}
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()] if root.is_dir() else []
    for p in files:
        saved.setdefault(str(p.relative_to(root)), p.stat().st_mode & 0o7777)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(saved, indent=1, sort_keys=True))
    n = 0
    for p in files:
        try:
            p.chmod(p.stat().st_mode & 0o7777 & ~0o222)
            n += 1
        except OSError:
            continue
    return n


def unlock_tree(root: Path, record: Path) -> int:
    """Undo lock_tree: put back every recorded mode, then delete the record. No record = nothing locked: 0."""
    try:
        saved = json.loads(record.read_text())
    except (OSError, ValueError):
        return 0
    n = 0
    for rel, mode in saved.items():
        p = root / rel
        try:
            if p.is_file() and not p.is_symlink():
                p.chmod(int(mode))
                n += 1
        except (OSError, ValueError):
            continue
    record.unlink(missing_ok=True)
    return n


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
