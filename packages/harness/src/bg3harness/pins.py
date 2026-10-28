# pyright: basic
"""What one run plays with: the game build, bg3le, the mod and the repo (moved from bg3bench.pins, which adds the
harness, bench and suite versions on top and re-exports these). The bench's LiveRunHost records them at every run's
start (runner/run_host.py machine_pins), so a matrix run's manifest carries the same pins as a legacy series run's.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Repo paths whose working-tree state a run plays with (runtime/, playthroughs/ and docs are not code).
CODE_PATHS = ("runner", "bridge", "mod", "play", "tools", "patches", "memory-template")


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


def run_pins(
    repo: Path,
    game_build: str,
    dist: Path,
    mod_meta: Path,
    mod_hashes: dict[str, str],
    git: Callable[..., str | None] = _git,
) -> dict[str, Any]:
    """The BG3 Steam buildid, bg3le, the mod (version + tree sha1) and the repo; no versions (the bench adds them:
    bg3bench.pins.version_pins)."""
    try:
        ver = mod_version(mod_meta.read_text(errors="replace"))
    except OSError:
        ver = None
    return {
        "game_build": game_build or None,
        "bg3le": bg3le_pin(dist, repo / "third_party" / "bg3le", git),
        "mod": {"version": ver, "sha1": tree_digest(mod_hashes) if mod_hashes else None},
        "repo": repo_pin(repo, git),
    }
