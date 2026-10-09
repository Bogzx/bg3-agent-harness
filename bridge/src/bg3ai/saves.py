"""Save-folder helpers for loading a named save through the in-game Load page.

The Load page groups saves by playthrough (the character's name) and lists them newest first, but the mod
cannot read a save's title there. So the caller tells it where to look: the playthrough name (the folder
prefix), the save's position among that playthrough's folders by mtime, and the save time, which the mod
checks against the page's info pane before pressing Load.
"""

from __future__ import annotations

import time
from pathlib import Path


def owner_of(folder: str) -> str:
    """'Claude-45912619919__S001-end' -> 'Claude' (the playthrough heading on the Load page)."""
    head = folder.split("__", 1)[0]
    return head.rsplit("-", 1)[0] if "-" in head else head


def _mtime(d: Path) -> float:
    lsv = d / f"{d.name}.lsv"  # the save file itself (the runner uses it too); else the folder
    try:
        return lsv.stat().st_mtime
    except OSError:
        return d.stat().st_mtime


def load_args(save_dir: Path, folder: str) -> dict:
    """Arguments for ui.menu {action: "load"}: name, owner, index (1 = newest of that owner), date."""
    owner = owner_of(folder)
    same = [d for d in save_dir.iterdir() if d.is_dir() and owner_of(d.name) == owner] if save_dir.is_dir() else []
    same.sort(key=_mtime, reverse=True)
    args: dict = {"action": "load", "name": folder, "owner": owner}
    for i, d in enumerate(same, 1):
        if d.name == folder:
            t = time.localtime(_mtime(d))
            args["index"] = i
            args["date"] = {"y": t.tm_year, "m": t.tm_mon, "d": t.tm_mday, "H": t.tm_hour, "M": t.tm_min}
            break
    return args
