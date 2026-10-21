# pyright: basic
"""bg3bench.resets: the FAIR-2 knowledge lock (moved from runner/test_runs.py; the series wiring stays there)."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bg3bench import resets as R


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3bench-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class KnowledgeLock(TmpDir):
    """FAIR-2: play/knowledge's files are read-only for a --runs series, and their modes come back after."""

    def tree(self) -> Path:
        k = self.tmp / "knowledge"
        (k / "playbooks").mkdir(parents=True)
        (k / "a.md").write_text("a")
        (k / "playbooks" / "b.md").write_text("b")
        (k / "x.sh").write_text("#!/bin/sh")
        (k / "x.sh").chmod(0o755)
        (k / "a.md").chmod(0o640)
        return k

    def modes(self, k: Path) -> dict:
        return {str(p.relative_to(k)): p.stat().st_mode & 0o777 for p in k.rglob("*") if p.is_file()}

    def test_lock_unlock_restores_modes(self):
        k, rec = self.tree(), self.tmp / "rt" / "lock.json"
        before = self.modes(k)
        self.assertEqual(R.lock_tree(k, rec), 3)
        self.assertEqual(self.modes(k), {"a.md": 0o440, "playbooks/b.md": 0o444, "x.sh": 0o555})
        with self.assertRaises(PermissionError) if os.geteuid() else contextlib.nullcontext():
            (k / "a.md").write_text("agent edit")
        # an execv'd successor (or a restart after a kill) locks again: the record keeps the first modes
        self.assertEqual(R.lock_tree(k, rec), 3)
        self.assertEqual(R.unlock_tree(k, rec), 3)
        self.assertEqual(self.modes(k), before)
        self.assertFalse(rec.exists())
        self.assertEqual(R.unlock_tree(k, rec), 0)  # nothing locked: no-op
        self.assertEqual(R.lock_tree(self.tmp / "none", rec), 0)
