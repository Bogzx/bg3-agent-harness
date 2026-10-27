"""Did the kernel (or ramwatch) kill the game for its memory? (moved from runner/play.py): the user journal's OOM
lines, ramwatch's RSS spike and kill records, the save the dead game had loaded last and the consecutive-OOM streak.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import io as game
from .saves import saved_key

OOM_LINE = "killed by the OOM killer"
OOM_SAVE_LIMIT = 2  # OOM kills in a row right after loading the same save → the runner refuses that save
OOM_SPIKE_S = 180  # a ramwatch RSS spike this close before an OOM line in another unit = the game's OOM


def game_scope(stamp: str) -> str:
    """The systemd user scope runner/launch_game.sh runs the game in (its own cgroup, OOMPolicy=continue)."""
    return f"bg3ai-game-{stamp}.scope"


def find_game_oom(journal: str, stamp: str, spikes: list[dict[str, Any]]) -> str | None:
    """Why the game died of an OOM, from `journalctl --user -o short-unix` lines since its launch, or None. A kill in
    the game's own scope counts; one in another unit (a game launched before scopes, e.g. in a terminal's unit) only
    with a ramwatch RSS spike (runtime/ram_spikes.jsonl) within OOM_SPIKE_S before it."""
    scope = game_scope(stamp)
    for line in journal.splitlines():
        if OOM_LINE not in line:
            continue
        head, _, rest = line.partition(" ")
        try:
            t = float(head)
        except ValueError:
            continue
        unit = rest.split(": ")[-2].split()[-1] if ": " in rest else "?"
        at = f"{datetime.fromtimestamp(t):%H:%M:%S}"
        if unit == scope:
            return f"the kernel OOM-killed the game at {at} (its scope {scope})"
        spike = next((sp for sp in spikes if 0 <= t - sp.get("t", 0) <= OOM_SPIKE_S), None)
        if spike:
            return (
                f"the kernel OOM-killed the game at {at} (in {unit}; its RSS went {spike['from_mb']} → "
                f"{spike['to_mb']} MB in {spike['seconds']} s just before)"
            )
    return None


def read_spikes(since: float, path: Path) -> list[dict[str, Any]]:
    """ramwatch's records (runtime/ram_spikes.jsonl, ram_kills.jsonl, heat_kills.jsonl) with t >= since."""
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines[-200:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("t", 0) >= since:  # pyright: ignore[reportUnknownMemberType]
            out.append(rec)  # pyright: ignore[reportUnknownArgumentType]
    return out


def user_journal(since: float) -> str:
    """systemd --user manager lines since `since` (where 'A process of this unit has been killed by the OOM killer'
    lands; the kernel log needs root here). '' when journalctl isn't usable."""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "--no-pager", "-q", "-o", "short-unix", "--since", f"@{int(since)}",
             "_COMM=systemd"],
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout


def last_loaded_save(actions: list[dict[str, Any]], saves: list[game.Save], fallback: str | None) -> str | None:
    """The save the game process loaded last: the newest load_game in `actions` (actions.jsonl since its launch) that
    loaded (`loaded=<label>`), as its folder, else `fallback` (the runner's load at the launch; S082's OOM line named
    S078-start, loaded 2 h and two agent reloads before)."""
    for a in reversed(actions):
        m = re.search(r"loaded=(\S+)", str(a.get("result") or "")) if a.get("tool") == "load_game" else None
        if not m:
            continue
        args: dict[str, Any] = a.get("args") or {}
        arg = game.parse_save_folder(str(args.get("save") or ""))
        if arg and arg.label == m.group(1):
            return arg.folder
        # the newest save of that label written before the load, by its own time (meta.lsf SaveTime): a copy Steam's
        # local cloud restores has mtime = the launch
        same = [sv for sv in saves if sv.label == m.group(1) and sv.saved is not None and sv.saved <= a.get("t", 0)]
        return max(same, key=saved_key).folder if same else m.group(1)
    return fallback


def oom_streak_update(streak: dict[str, Any] | None, save: str | None, oom: str | None) -> dict[str, Any] | None:
    """The consecutive-OOM record after a game death: same save again → n+1, another save → 1, no OOM → reset."""
    if not oom or not save:
        return None
    n = streak["n"] + 1 if streak and streak.get("save") == save else 1
    return {"save": save, "n": n, "why": oom, "t": time.time()}
