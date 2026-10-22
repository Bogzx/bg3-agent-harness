# pyright: basic
"""Version pins: what one run plays with (game build, bg3le, the mod, the repo, the harness and benchmark versions),
and their drift within a series
(moved from runner/runs.py; runs.py re-exports them). A series freezes its first run's pins in series.json and
refuses a drifted later run unless --allow-version-drift.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import bg3harness

import bg3bench
from bg3bench import suite

# Repo paths whose working-tree state a run plays with (runtime/, playthroughs/ and docs are not code).
CODE_PATHS = ("runner", "bridge", "mod", "play", "tools", "patches", "memory-template")
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


def _git(cwd: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def buildinfo(text: str) -> dict:
    """build/bg3le/dist/BUILDINFO (tools/build_bg3le.sh): the upstream commit, the binary's sha256, the patches."""
    out: dict = {"patches": []}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        k, v = k.strip(), v.strip()
        if k == "bg3le commit":
            out["commit"] = v
        elif k == "sha256":
            out["sha256"] = v
        elif k == "patch":
            out["patches"].append(v)
    return out


def mod_version(meta_lsx: str) -> str | None:
    """The mod's meta.lsx Version64 as major.minor.revision.build (Larian's bit layout), or None."""
    m = re.search(r'id="Version64"[^>]*value="(\d+)"', meta_lsx)
    if not m:
        return None
    v = int(m.group(1))
    return f"{v >> 55}.{(v >> 47) & 0xFF}.{(v >> 31) & 0xFFFF}.{v & 0x7FFFFFFF}"


def tree_digest(hashes: dict[str, str]) -> str:
    """One sha1 over a {relative path: sha1} tree (play.py mod_tree_hashes)."""
    return hashlib.sha1("".join(f"{k}\0{v}\n" for k, v in sorted(hashes.items())).encode()).hexdigest()


def repo_pin(repo: Path, git=_git) -> dict:
    """The repo commit and whether the code paths differ from it (tracked edits or untracked files); diff: a sha1 of
    `git diff HEAD` plus the untracked names over CODE_PATHS, so two dirty trees with different edits differ."""
    head = (git(repo, "rev-parse", "HEAD") or "").strip() or None
    status = git(repo, "status", "--porcelain", "--", *CODE_PATHS)
    diff = git(repo, "diff", "HEAD", "--", *CODE_PATHS)
    dirty = bool(status and status.strip()) if status is not None else None
    untracked = "".join(ln for ln in (status or "").splitlines(True) if ln.startswith("??"))
    return {
        "commit": head,
        "dirty": dirty,
        "diff": hashlib.sha1(((diff or "") + untracked).encode()).hexdigest()[:16] if dirty else None,
    }


def bg3le_pin(dist: Path, source: Path, git=_git) -> dict:
    """bg3le as the game loads it: the built binary (BUILDINFO's upstream commit and sha256, the patches built in)
    and the source tree's `git describe --dirty` (third_party/bg3le carries local changes past its pinned tag:
    local_changes)."""
    try:
        info = buildinfo((dist / "BUILDINFO").read_text(errors="replace"))
    except OSError:
        info = {"patches": []}
    sha = info.get("sha256")
    if not sha:
        try:
            sha = hashlib.sha256((dist / "libbg3le.so").read_bytes()).hexdigest()
        except OSError:
            sha = None
    desc = (git(source, "describe", "--tags", "--dirty", "--always") or "").strip() or None
    return {
        "source": desc,
        "local_changes": desc.endswith("-dirty") if desc else None,
        "commit": info.get("commit"),
        "sha256": sha,
        "patches": len(info["patches"]),
    }


def version_pins(repo: Path, game_build: str, dist: Path, mod_meta: Path, mod_hashes: dict[str, str], git=_git) -> dict:
    """What one run plays with: the BG3 Steam buildid, bg3le, the mod (version + tree sha1), the repo and the
    versions (versions())."""
    try:
        ver = mod_version(mod_meta.read_text(errors="replace"))
    except OSError:
        ver = None
    return {
        "game_build": game_build or None,
        "bg3le": bg3le_pin(dist, repo / "third_party" / "bg3le", git),
        "mod": {"version": ver, "sha1": tree_digest(mod_hashes) if mod_hashes else None},
        "repo": repo_pin(repo, git),
        "versions": versions(),
    }


def versions() -> dict:
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
