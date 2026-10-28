# pyright: basic
"""Version pins: what one run plays with (game build, bg3le, the mod, the repo, the harness and benchmark versions),
and their drift within a series (moved from runner/runs.py; runs.py re-exports them). bg3harness.pins reads the game,
bg3le, mod and repo pins (the bench's live host records them too); this module re-exports them and adds the
versions. A series freezes its first run's pins in series.json and refuses a drifted later run unless
--allow-version-drift.
"""

from __future__ import annotations

from pathlib import Path

import bg3harness
from bg3harness.pins import CODE_PATHS, _git, bg3le_pin, buildinfo, mod_version, repo_pin, run_pins, tree_digest

import bg3bench
from bg3bench import suite

__all__ = [
    "CODE_PATHS",
    "DRIFT_KEYS",
    "bg3le_pin",
    "buildinfo",
    "mod_version",
    "pin_drift",
    "repo_pin",
    "tree_digest",
    "version_pins",
    "versions",
]

# The pins compared between the runs of a series (dotted paths into version_pins()).
DRIFT_KEYS = (
    "game_build",
    "bg3le.sha256",
    "bg3le.source",
    "mod.sha1",
    "repo.commit",
    "repo.dirty",
    "repo.diff",
    "versions.harness",
    "versions.suite",
)


def version_pins(repo: Path, game_build: str, dist: Path, mod_meta: Path, mod_hashes: dict[str, str], git=_git) -> dict:
    """What one run plays with: the BG3 Steam buildid, bg3le, the mod (version + tree sha1), the repo
    (bg3harness.pins.run_pins) and the versions (versions())."""
    return {**run_pins(repo, game_build, dist, mod_meta, mod_hashes, git), "versions": versions()}


def versions() -> dict[str, str]:
    """The harness, bg3bench and suite versions of the running code (CHANGELOG.md: every result cites them)."""
    return {
        "harness": bg3harness.__version__,
        "bench": bg3bench.__version__,
        "suite": suite.suite_version(bg3bench.__version__),
    }


def _dig(d: dict | None, dotted: str):
    for k in dotted.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d


def pin_drift(first: dict | None, now: dict) -> list[str]:
    """`key: first → now` for every DRIFT_KEYS pin that differs from the series' first run (none: no first pins)."""
    if not first:
        return []
    return [f"{k}: {_dig(first, k)} → {_dig(now, k)}" for k in DRIFT_KEYS if _dig(first, k) != _dig(now, k)]
