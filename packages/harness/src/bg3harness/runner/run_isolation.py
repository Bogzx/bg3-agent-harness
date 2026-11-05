"""A bench run's isolation from the machine's shared state (split out of run_host.py for the 1000-line cap; run_host
re-exports what its callers use): the mod's nav files and scenario.json in the shared Script Extender/BG3AI folder
(moved aside for a save start, the run's own moved out after it) and the saves the run wrote into the shared Story
folder (moved into its run dir), each with a marker so the next start finishes what a run that died left."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

from bg3ai import saves as bsaves

from bg3harness.game.io import Save
from bg3harness.runner import env as renv


def archive_nav_state(se_bg3ai: Path, dest: Path) -> list[str]:
    """The reset's nav_state "archive": the mod's machine-global nav_*.json (kill lists, breadcrumb mirrors) moved
    into dest, so the run starts with none from an earlier game (bg3bench.resets.archive_nav_state's rule)."""
    moved: list[str] = []
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


SE_RUN_FILES = ("nav_*.json", "scenario.json")  # what the mod writes in the shared Script Extender/BG3AI folder


def se_files(se_bg3ai: Path) -> list[str]:
    """The names of the mod's files (SE_RUN_FILES) in the shared folder now."""
    try:
        return sorted({p.name for pat in SE_RUN_FILES for p in se_bg3ai.glob(pat) if p.is_file()})
    except OSError:
        return []


def isolate_nav(se_bg3ai: Path, dest: Path, marker: Path, archive: bool) -> list[str]:
    """A run's nav reset. The marker, written first, names the mod's files the shared folder held before the run, so
    restore_nav can move the run's own out after it (live9: a new game left its nav files and scenario.json), even
    after a crash. A save start (archive) also moves every nav_*.json into dest (archive_nav_state). The decision of
    2026-10-22 03:30 is to move only the start host's two (bg3bench.resets.isolate_nav); that needs the save's host
    uuid, which tools/saves/inspect.py doesn't report yet, so all of them go (and all come back). A new game moves
    none: its host is a new uuid. Returns the moved names."""
    before = se_files(se_bg3ai)
    names = [n for n in before if n.startswith("nav_")] if archive else []
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"se": str(se_bg3ai), "dest": str(dest), "files": names, "before": before}))
    return archive_nav_state(se_bg3ai, dest) if names else []


def restore_nav(marker: Path) -> list[str]:
    """After the run (or a failed start, or at the next start after a crash): the marker's files go back from dest;
    a file of the same name the run wrote meanwhile is kept in <dest>/run/, and so is every file of the mod's that
    wasn't there before the run. Idempotent; the marker goes last. No marker: nothing was moved, []."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return []
    se, dest = Path(m["se"]), Path(m["dest"])
    back: list[str] = []
    for n in m["files"]:
        if not (dest / n).is_file():
            continue
        if (se / n).exists():
            (dest / "run").mkdir(parents=True, exist_ok=True)
            shutil.move(str(se / n), str(dest / "run" / n))
        shutil.move(str(dest / n), str(se / n))
        back.append(n)
    before: list[str] | None = m.get("before")  # an older marker has no list: it can't tell the run's files
    for n in [] if before is None else [n for n in se_files(se) if n not in before]:
        (dest / "run").mkdir(parents=True, exist_ok=True)
        shutil.move(str(se / n), str(dest / "run" / n))
    marker.unlink(missing_ok=True)
    return back


def nav_marker() -> Path:
    return renv.RUNTIME / "nav-reset.json"


def saves_marker() -> Path:
    return renv.RUNTIME / "saves-isolate.json"


def mark_run_saves(marker: Path, story: Path, dest: Path, after: float, start_save: str | None) -> None:
    """At a run's start: which saves will be the run's (isolate_saves), written before the game loads anything, so
    a run that dies without a record is isolated by the next start."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"story": str(story), "dest": str(dest), "after": after, "start": start_save}))


def isolate_saves(marker: Path) -> list[str]:
    """At a run's end (record, abort, a failed start, or the next start after a run that died): every save folder the
    run wrote into the shared Story folder (saved since its start, `after`, by the time its meta.lsf holds; its start
    save never) moves into its run dir (`dest`), so no later run reaches it (bench B3: the main menu's Continue loaded
    act1_level_3's S001 save in a Balthazar run). Moved, never deleted. A folder's mtime says nothing: Steam's local
    cloud restores the moved saves at each launch with mtime = the launch (B3 job 9 moved 33 such copies, 523 MB);
    a restored copy keeps its own SaveTime and stays where it is. Idempotent; the marker goes last. No marker: []
    (nothing to do)."""
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return []
    story, dest = Path(m["story"]), Path(m["dest"])
    moved: list[str] = []
    try:
        folders = sorted(d for d in story.iterdir() if d.is_dir() and d.name != m.get("start"))
    except OSError:
        folders = []
    for d in folders:
        try:
            saved = bsaves.saved_at(d)
            if saved is None or saved < m["after"]:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dest / d.name))
            moved.append(d.name)
        except OSError as e:
            renv.log(f"save isolation: {d.name} not moved: {e}")
    marker.unlink(missing_ok=True)
    return moved


def own_save(name: str, saves: Sequence[Save], after: float, start_save: str | None) -> bool:
    """A save (folder or label) is the run's own: its start save, or one saved since the run started (`after`, by the
    time the save holds, Save.saved; bg3ai.tools.saving's SAVES_AFTER rule). Never by mtime: a restored copy of an
    earlier run's save (isolate_saves) has mtime = the launch."""
    return any(
        name in (s.folder, s.label) and (s.folder == start_save or (s.saved is not None and s.saved >= after))
        for s in saves
    )
