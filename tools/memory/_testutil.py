"""Shared helpers for the memory tool tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import memlib as M
import new_playthrough as NP


def isolate_git() -> None:
    """Make git ignore the user's/system config (identity, signing, hooks) for reproducible tests."""
    fd, path = tempfile.mkstemp(prefix="gitconfig-")
    os.close(fd)
    os.environ["GIT_CONFIG_GLOBAL"] = path
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["BG3_SNAPSHOTS"] = "0"  # never touch the live game from unit tests
    for var in ("BG3_MEMORY_DIR", "CLAUDE_PROJECT_DIR", "BG3_WRAP_MINUTES", "BG3_SESSION_MINUTES",
                "BG3AI_SESSION_DEADLINE", "BG3AI_SESSION_WRAPUP"):
        os.environ.pop(var, None)


CHARACTER_HEADER = {"Name": "Mira", "Race": "Dwarf / Gold Dwarf", "Class": "Cleric / Life Domain",
                    "Background": "Acolyte", "Deity": "Moradin", "Roleplay": "strict"}


def character_text(dev: tuple[str, ...] = ("- S002: distrusts mind flayers more after the nautiloid [seen]",)) -> str:
    """memory-template/CHARACTER.md filled in (header values and one line per section), like the playbook makes it."""
    t = (M.TEMPLATE_DIR / "CHARACTER.md").read_text().replace("# Character: (name)", "# Character: Mira")
    for key, val in CHARACTER_HEADER.items():
        t = re.sub(rf"^{key}:.*$", f"{key}: {val}", t, flags=re.MULTILINE)
    t = re.sub(r"^(## (?!Character development).+)$", r"\1\nA short line.", t, flags=re.MULTILINE)
    return t.rstrip("\n") + "\n" + "".join(d + "\n" for d in dev)


class MemoryTestCase(unittest.TestCase):
    """Creates a fresh memory dir (from the real template, committed as S000) per test."""

    def setUp(self) -> None:
        isolate_git()
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3mem-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mem = self.tmp / "memory"
        NP.create_memory("test-run", self.mem)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -----------------------------------------------------------
    def write(self, rel: str, text: str) -> None:
        p = self.mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def append(self, rel: str, text: str) -> None:
        with (self.mem / rel).open("a") as f:
            f.write(text)

    def read(self, rel: str) -> str:
        return (self.mem / rel).read_text()

    def commit(self, msg: str = "test") -> None:
        M.git_commit_all(self.mem, msg)

    def git_log(self) -> list[str]:
        return M.git(self.mem, "log", "--format=%s").stdout.split("\n")[:-1]

    def open_session(self, n: int = 1, minutes_ago: float = 0.0) -> None:
        self.write(f"sessions/{M.session_id(n)}.md", f"# Session {M.session_id(n)}\n\n## Log\n")
        M.save_state(self.mem, {"current": n, "open": True, "started": "2026-10-09T20:00:00+02:00",
                                "started_epoch": time.time() - minutes_ago * 60})

    def good_handoff(self, n: int) -> str:
        return (f"# Handoff {M.session_id(n)} → {M.session_id(n + 1)}\n"
                f"Save to load: \"{M.session_id(n)}-end\" — at the crash site\n\n"
                f"## Doing right now\nLooting the pod.\n\n## Next steps\n1. Go north.\n")

    def state(self) -> dict:
        return json.loads(self.read(".session.json"))
