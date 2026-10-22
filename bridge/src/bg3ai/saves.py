"""Save-folder helpers for loading a named save through the in-game Load page.

The Load page groups saves by playthrough (the character's name) and lists them newest first, but the mod
cannot read a save's title there. So the caller tells it where to look: the playthrough name, the save's
position in that playthrough's list and the save time, which the mod checks against the page's info pane
before pressing Load. The page groups by the save's LeaderName and sorts by its SaveTime (meta.lsf), so those
come from the save itself (read_meta); a save whose meta can't be read counts by its folder prefix and .lsv
mtime. live5: copies whose mtime wasn't their save time, or whose folder prefix wasn't their leader, shifted
the mtime order by one and every load of an older save timed out.
"""

from __future__ import annotations

import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path

# The save readers (LSPK, LSF) live with the save inspector, outside the bridge package.
SAVE_TOOLS = Path(__file__).resolve().parents[3] / "tools" / "saves"


def owner_of(folder: str) -> str:
    """'Claude-45912619919__S001-end' -> 'Claude' (the playthrough heading on the Load page)."""
    head = folder.split("__", 1)[0]
    return head.rsplit("-", 1)[0] if "-" in head else head


def save_file(d: Path) -> Path:
    """The .lsv in a save folder: 'Claude-4101260944__S017-pre-gut/S017-pre-gut.lsv' (named after the part
    after '__', not after the folder); any .lsv there if that one is missing."""
    want = d / f"{d.name.split('__', 1)[-1]}.lsv"
    if want.exists():
        return want
    return next(iter(sorted(d.glob("*.lsv"))), want)


def _mtime(d: Path) -> float:
    try:
        return save_file(d).stat().st_mtime  # the save file itself; else the folder
    except OSError:
        return d.stat().st_mtime


def read_meta(lsv: Path) -> tuple[str, float] | None:
    """(LeaderName, SaveTime) from the save's meta.lsf, or None when it can't be read (no readers, zstd before
    Python 3.14, not a v18 package, no such fields). Reads the header, the file list and that one member only."""
    try:
        if str(SAVE_TOOLS) not in sys.path:
            sys.path.append(str(SAVE_TOOLS))
        import lsf
        import lspk
    except ImportError:
        return None
    try:
        with lsv.open("rb") as f:
            head = f.read(lspk.HEADER.size)
            magic, version, list_off, _, flags, priority, _, parts = lspk.HEADER.unpack(head)
            if magic != lspk.MAGIC or version != 18:
                return None
            f.seek(list_off)
            count, csize = struct.unpack("<II", f.read(8))
            table = lspk.lz4_block(f.read(csize), count * lspk.ENTRY.size)
            for k in range(count):
                name, off1, off2, part, eflags, on_disk, size = lspk.ENTRY.unpack_from(table, k * lspk.ENTRY.size)
                if name.split(b"\0", 1)[0].lower() == b"meta.lsf":
                    break
            else:
                return None
            f.seek(off1 | (off2 << 32))
            entry = lspk.Entry("meta.lsf", 0, part, eflags, on_disk, size)
            body = lspk.Package(version, flags, priority, parts, (entry,), f.read(on_disk)).read(entry)
        if body is None:
            return None
        fields = {a.name: a.value for a in lsf.attributes(body) if a.path == "MetaData/MetaData"}
    except (OSError, ValueError, struct.error, IndexError, KeyError):  # LspkError/LsfError are ValueErrors
        return None
    leader, saved = fields.get("LeaderName"), fields.get("SaveTime")
    if not isinstance(leader, str) or not leader or not isinstance(saved, int) or saved <= 0:
        return None
    return leader, float(saved)


def load_args(save_dir: Path, folder: str, meta: Callable[[Path], tuple[str, float] | None] = read_meta) -> dict:
    """Arguments for ui.menu {action: "load"}: name, owner, index (1 = newest of that owner), date. Owner and
    time are each save's LeaderName and SaveTime (meta), else its folder prefix and .lsv mtime."""

    def key(d: Path) -> tuple[str, float]:
        return meta(save_file(d)) or (owner_of(d.name), _mtime(d))

    keys = {d.name: key(d) for d in save_dir.iterdir() if d.is_dir()} if save_dir.is_dir() else {}
    owner = keys[folder][0] if folder in keys else owner_of(folder)
    same = sorted((t, n) for n, (o, t) in keys.items() if o == owner)
    same.reverse()
    args: dict = {"action": "load", "name": folder, "owner": owner}
    for i, (when, name) in enumerate(same, 1):
        if name == folder:
            t = time.localtime(when)
            args["index"] = i
            args["date"] = {"y": t.tm_year, "m": t.tm_mon, "d": t.tm_mday, "H": t.tm_hour, "M": t.tm_min}
            break
    return args
