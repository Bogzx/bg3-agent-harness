"""Party positions for the breadcrumb map: sampled from the mod's state.status into positions.jsonl, read back as
tracks per level.

    state.status (mod State.lua): {"level": "WLD_Main_A", "region": "...", "active": uuid,
                                   "party": [{"name", "uuid", "pos": [x, y, z], "region", ...}], ...}
    <runtime>/sessions/<pt>/positions.jsonl, one row per kept sample (POSITIONS_VERSION):
        {"v": 1, "t": epoch s, "level": ..., "region": ..., "party": [{"name", "x", "y", "z", "active"?}]}

A sample is kept when it is the first, the level changed, someone moved MIN_MOVE_M, or KEEP_S passed. On read the
active member's points form segments, split at a level change or a gap longer than GAP_S (the game was down or no
session ran). Map frame (research/ovh/live1.md 8): the minimap's X is world x and its Y is world z (y is height), so
the plot uses (x, z). Human view only: positions never reach the agent (FAIRNESS).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

POSITIONS_VERSION = 1
MIN_MOVE_M = 0.5
KEEP_S = 30.0
GAP_S = 120.0
MAX_POINTS = 2000


def snapshot(t: float, status: dict) -> dict | None:
    """A state.status dict -> a positions row (None without a level or any member position). Members on another
    level (their own "region" differs) are left out: their coordinates are in another space."""
    level = status.get("level")
    if not isinstance(level, str) or not level:
        return None
    active = status.get("active")
    party = []
    for m in status.get("party") or []:
        if not isinstance(m, dict):
            continue
        pos = m.get("pos")
        if not (isinstance(pos, list) and len(pos) == 3 and all(isinstance(c, (int, float)) for c in pos)):
            continue
        if m.get("region") not in (None, level):
            continue
        row = {"name": str(m.get("name") or "?"), "x": pos[0], "y": pos[1], "z": pos[2]}
        if (active and m.get("uuid") == active) or m.get("active"):
            row["active"] = True
        party.append(row)
    if not party:
        return None
    return {"v": POSITIONS_VERSION, "t": round(t, 3), "level": level, "region": status.get("region") or level,
            "party": party}


def moved(a: dict, b: dict) -> float:
    """The largest distance any member (by name) moved between two rows; inf when the party changed."""
    pa = {m["name"]: m for m in a["party"]}
    pb = {m["name"]: m for m in b["party"]}
    if pa.keys() != pb.keys():
        return math.inf
    return max(math.dist((pa[n]["x"], pa[n]["y"], pa[n]["z"]), (pb[n]["x"], pb[n]["y"], pb[n]["z"])) for n in pa)


def keep(last: dict | None, row: dict, min_move: float = MIN_MOVE_M, keep_s: float = KEEP_S) -> bool:
    if last is None or row["level"] != last["level"] or row["t"] - last["t"] >= keep_s:
        return True
    return moved(last, row) >= min_move


def lead(row: dict) -> dict:
    """The member the breadcrumb follows: the active one, else the first."""
    return next((m for m in row["party"] if m.get("active")), row["party"][0])


def tracks(rows: list[dict], level: str | None = None, gap_s: float = GAP_S, max_points: int = MAX_POINTS) -> dict:
    """The breadcrumb payload: the levels seen, and for `level` (default: the latest row's) the lead's (t, x, z)
    segments, the party's last positions and the bounds."""
    rows = sorted((r for r in rows if r.get("party")), key=lambda r: r["t"])
    levels: dict[str, dict] = {}
    for r in rows:
        lv = levels.setdefault(r["level"], {"level": r["level"], "region": r.get("region"), "samples": 0,
                                            "first_t": r["t"]})
        lv["samples"] += 1
        lv["last_t"] = r["t"]
    if level is None and rows:
        level = rows[-1]["level"]
    segments: list[list[list[float]]] = []
    prev: dict | None = None
    picked = []
    for r in rows:
        if prev is None or r["level"] != prev["level"] or r["t"] - prev["t"] > gap_s:
            segments.append([])
        if r["level"] == level:
            m = lead(r)
            segments[-1].append([r["t"], m["x"], m["z"]])
            picked.append(r)
        prev = r
    segments = [s for s in segments if s]
    n = sum(len(s) for s in segments)
    if n > max_points:
        step = math.ceil(n / max_points)
        segments = [s[::step] + ([s[-1]] if (len(s) - 1) % step else []) for s in segments]
    xs = [p[1] for s in segments for p in s]
    zs = [p[2] for s in segments for p in s]
    now = picked[-1] if picked else None
    return {"level": level, "levels": sorted(levels.values(), key=lambda lv: lv["first_t"]),
            "segments": segments,
            "party": [{"name": m["name"], "x": m["x"], "z": m["z"], "active": bool(m.get("active"))}
                      for m in now["party"]] if now else [],
            "t": now["t"] if now else None,
            "bounds": {"min_x": min(xs), "max_x": max(xs), "min_z": min(zs), "max_z": max(zs)} if xs else None}


def read(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("t"), (int, float)) and isinstance(r.get("party"), list) \
                and isinstance(r.get("level"), str):
            out.append(r)
    return out


class StatusSource(Protocol):
    def status(self) -> dict | None:
        """The game's state.status, or None when the game can't be asked."""
        ...


@dataclass
class Sampler:
    """Samples `source` and appends the kept rows to the file `path_for()` names (the current playthrough's
    positions.jsonl; None = no playthrough). `on_row` sees every kept row (the SSE hub)."""

    source: StatusSource
    path_for: Callable[[], Path | None]
    clock: Callable[[], float]
    on_row: Callable[[dict], Any] = lambda row: None
    last: dict | None = None
    path: Path | None = None

    def poll(self) -> dict | None:
        path = self.path_for()
        if path is None:
            return None
        if path != self.path:
            rows = read(path)
            self.path, self.last = path, rows[-1] if rows else None
        st = self.source.status()
        row = snapshot(self.clock(), st) if isinstance(st, dict) else None
        if row is None or not keep(self.last, row):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.last = row
        self.on_row(row)
        return row
