"""Free disk space before a run starts. A run writes its recording (~16 MB a minute at the bench size, launch pilot
2026-10-29) and then its evidence video (about as much again), so a matrix must not start a job on a filesystem that
can't hold it: the floor is BG3AI_DISK_FLOOR_MB (default 4096) + the job's cap in minutes x BG3AI_DISK_MB_PER_MIN
(default 16). Mid-run, the recorder's own guard stops recording under 1 GB free (recorder.GuardLimits.min_disk_mb).
"""

from __future__ import annotations

import math
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

FLOOR_MB_ENV = "BG3AI_DISK_FLOOR_MB"
PER_MIN_ENV = "BG3AI_DISK_MB_PER_MIN"
FLOOR_MB = 4096
MB_PER_MIN = 16


def free_mb(path: Path) -> int | None:
    """Free space on path's filesystem (MB), None when it can't be read."""
    try:
        return shutil.disk_usage(path).free // 1024**2
    except OSError:
        return None


def need_mb(cap_minutes: float, env: Mapping[str, str] = os.environ) -> tuple[int, int, int]:
    """(needed MB, floor MB, MB per minute) for a run capped at cap_minutes."""
    floor = int(env.get(FLOOR_MB_ENV) or FLOOR_MB)
    per_min = int(env.get(PER_MIN_ENV) or MB_PER_MIN)
    return floor + math.ceil(cap_minutes * per_min), floor, per_min


def start_refusal(
    path: Path,
    cap_minutes: float,
    env: Mapping[str, str] = os.environ,
    free: Callable[[Path], int | None] | None = None,
) -> str | None:
    """Why a run capped at cap_minutes must not start with path's free space, or None."""
    need, floor, per_min = need_mb(cap_minutes, env)
    have = (free or free_mb)(path)
    if have is None or have >= need:
        return None
    return f"{have} MB free on {path} < {need} MB ({floor} + {cap_minutes:g} min x {per_min} MB)"
