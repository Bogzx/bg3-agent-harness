"""The runtime/ dir that offline tools (tools/logs miners, tools/vis/series_report.py) read by default. Stdlib only.

runner/play.py writes to $BG3AI_RUNTIME, else <repo>/runtime. A worktree (e.g. ../bg3-ai-<name>) has
no runtime/: the game, the runner and the transcripts live in the main checkout. So: $BG3AI_RUNTIME, else this
checkout's runtime/ when it holds sessions/, else the main tree's (found through the worktree's .git file).
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main_tree(root: Path = REPO) -> Path:
    """The main checkout: root itself, or for a worktree (.git is a file "gitdir: <main>/.git/worktrees/<name>") the
    main tree, which holds the untracked docs/SESSION-WATCH.md and runtime/."""
    git = root / ".git"
    return Path(git.read_text().split(":", 1)[1].strip()).parents[2] if git.is_file() else root


def runtime_dir(root: Path = REPO) -> Path:
    if os.environ.get("BG3AI_RUNTIME"):
        return Path(os.environ["BG3AI_RUNTIME"])
    return next((d / "runtime" for d in (root, main_tree(root)) if (d / "runtime" / "sessions").is_dir()),
                root / "runtime")
