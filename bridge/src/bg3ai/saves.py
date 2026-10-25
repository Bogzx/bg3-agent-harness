"""Save-folder helpers for loading a named save through the in-game Load page.

The Load page groups saves by playthrough and lists them newest first, but the mod cannot read a save's title
there. So the caller tells it where to look: the playthrough's heading, the save's position in that playthrough's
list and the save time, which the mod checks against the page's info pane before pressing Load. The page groups by
the save's GameSessionID and sorts by its SaveTime (meta.lsf), so those come from the save itself (read_meta); a
save whose meta can't be read counts by its folder prefix and .lsv mtime. live5: copies whose mtime wasn't their
save time, or whose folder prefix wasn't their leader, shifted the mtime order by one and every load of an older
save timed out. live6: saves made from claude-run-2 copies under another leader (Tav-…__SCN-*) keep its
GameSessionID and sit in the Claude group, so a LeaderName count was 4 low.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from pathlib import Path

from bg3ai import lsf, lspk


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


def read_meta(lsv: Path) -> tuple[str, float, str] | None:
    """(LeaderName, SaveTime, playthrough) from the save's meta.lsf, or None when it can't be read (zstd before
    Python 3.14, not a v18 package, no such fields). The playthrough is the GameSessionID (the Load page's group),
    else the LeaderName. Reads the header, the file list and that one member only."""
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
    leader, saved, session = fields.get("LeaderName"), fields.get("SaveTime"), fields.get("GameSessionID")
    if not isinstance(leader, str) or not leader or not isinstance(saved, int) or saved <= 0:
        return None
    return leader, float(saved), session if isinstance(session, str) and session else leader


def saved_at(d: Path) -> float | None:
    """When the game wrote the save in folder d: its meta.lsf SaveTime (read_meta), None when that can't be read.
    Not a file's mtime: Steam's local cloud restores a moved-aside save folder at each launch with mtime = the
    launch (bench B3, research/fix/live.md), and only the save's own SaveTime still says when it was made."""
    m = read_meta(save_file(d))
    return m[1] if m else None


def load_args(save_dir: Path, folder: str, meta: Callable[[Path], tuple[str, float, str] | None] = read_meta) -> dict:
    """Arguments for ui.menu {action: "load"}: name, owner (the playthrough's heading), index (1 = newest of its
    playthrough), date. Each save's leader, time and playthrough come from its meta, else its folder prefix (as
    leader and playthrough) and .lsv mtime. The heading is the leader of the playthrough's oldest save, not the
    newest's: live6 loaded under 'Claude' with a Tav-…__SCN save on top of run 2's group."""

    def key(d: Path) -> tuple[str, float, str]:
        prefix = owner_of(d.name)
        return meta(save_file(d)) or (prefix, _mtime(d), prefix)

    keys = {d.name: key(d) for d in save_dir.iterdir() if d.is_dir()} if save_dir.is_dir() else {}
    owner, _, group = keys.get(folder) or (owner_of(folder), 0.0, owner_of(folder))
    same = sorted(((t, n, o) for n, (o, t, g) in keys.items() if g == group), reverse=True)
    args: dict = {"action": "load", "name": folder, "owner": same[-1][2] if same else owner}
    for i, (when, name, _) in enumerate(same, 1):
        if name == folder:
            t = time.localtime(when)
            args["index"] = i
            args["date"] = {"y": t.tm_year, "m": t.tm_mon, "d": t.tm_mday, "H": t.tm_hour, "M": t.tm_min}
            break
    return args
