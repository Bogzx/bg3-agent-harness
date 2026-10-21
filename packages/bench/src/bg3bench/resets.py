# pyright: basic
"""What every run of a series starts without (moved from runner/runs.py; runs.py re-exports them).

NAV-3: the mod's machine-global navigation files are archived. FAIR-2: play/knowledge is locked (chmod a-w) for a
series, and the knowledge files the playing agent changed are put back after each run.
"""

from __future__ import annotations

import json
import shutil
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
