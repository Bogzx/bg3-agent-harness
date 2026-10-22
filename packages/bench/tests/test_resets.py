# pyright: basic
"""bg3bench.resets: the FAIR-2 knowledge lock (moved from runner/test_runs.py; the series wiring stays there)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
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


HOST = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
OTHER = "ad9af97d-75da-406a-ae13-7071c563f604"


class NavIsolation(TmpDir):
    """NAV-3 per host: the per-user Script Extender folder is shared with every playthrough on the machine."""

    def se(self) -> Path:
        se = self.tmp / "se" / "BG3AI"
        se.mkdir(parents=True)
        for name in (f"nav_{HOST}.json", f"nav_guard_{HOST}.json", f"nav_{OTHER}.json", f"nav_guard_{OTHER}.json",
                     "nav_guard.json", "other.json"):  # fmt: skip
            (se / name).write_text(f"orig {name}")
        return se

    def names(self, d: Path) -> list[str]:
        return sorted(p.name for p in d.iterdir() if p.is_file())

    def test_a_new_game_moves_nothing(self):
        se = self.se()
        before = self.names(se)
        self.assertEqual(R.isolate_nav(se, self.tmp / "reset", self.tmp / "m.json", "new_game", None), [])
        self.assertEqual(self.names(se), before)
        self.assertFalse((self.tmp / "m.json").exists())

    def test_a_save_start_moves_only_its_hosts_two_files_and_restores_them(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        moved = R.isolate_nav(se, dest, marker, "save", HOST)
        self.assertEqual(moved, [f"nav_{HOST}.json", f"nav_guard_{HOST}.json"])
        self.assertEqual(
            self.names(se), sorted([f"nav_{OTHER}.json", f"nav_guard_{OTHER}.json", "nav_guard.json", "other.json"])
        )  # other playthroughs' files and the host-less legacy kill list stay
        self.assertEqual(self.names(dest), sorted(moved))
        (se / f"nav_{HOST}.json").write_text("the run's breadcrumbs")  # the run writes its own
        self.assertEqual(R.restore_nav(marker), moved)
        self.assertEqual((se / f"nav_{HOST}.json").read_text(), f"orig nav_{HOST}.json")
        self.assertEqual((se / f"nav_guard_{HOST}.json").read_text(), f"orig nav_guard_{HOST}.json")
        self.assertEqual((dest / "run" / f"nav_{HOST}.json").read_text(), "the run's breadcrumbs")  # kept to look at
        self.assertFalse(marker.exists())
        self.assertEqual(R.restore_nav(marker), [])  # idempotent

    def test_a_host_with_no_files_moves_nothing(self):
        se = self.se()
        new = "00000000-0000-0000-0000-000000000001"
        self.assertEqual(R.isolate_nav(se, self.tmp / "reset", self.tmp / "m.json", "save", new), [])
        self.assertFalse((self.tmp / "m.json").exists())

    def test_a_save_start_without_a_valid_host_is_refused(self):
        se = self.se()
        for host in (None, "Claude", "../nav"):
            with self.assertRaises(R.NavIsolationError):
                R.isolate_nav(se, self.tmp / "reset", self.tmp / "m.json", "save", host)
        self.assertEqual(len(self.names(se)), 6)

    def test_the_context_restores_on_a_crash(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        with self.assertRaises(RuntimeError), R.nav_isolated(se, dest, marker, "save", HOST) as moved:
            self.assertEqual(len(moved), 2)
            self.assertFalse((se / f"nav_{HOST}.json").exists())
            raise RuntimeError("the run died")
        self.assertEqual((se / f"nav_{HOST}.json").read_text(), f"orig nav_{HOST}.json")
        self.assertFalse(marker.exists())

    def test_a_killed_runner_is_recovered_from_the_marker(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        R.isolate_nav(se, dest, marker, "save", HOST)  # then SIGKILL: no finally ran
        self.assertTrue(marker.exists())
        self.assertEqual(R.recover_nav(marker), [f"nav_{HOST}.json", f"nav_guard_{HOST}.json"])
        self.assertEqual(len(self.names(se)), 6)
        self.assertEqual(R.recover_nav(marker), [])
        # the next isolate recovers a leftover marker before moving anything
        R.isolate_nav(se, dest, marker, "save", HOST)
        R.isolate_nav(se, self.tmp / "reset2", marker, "save", OTHER)
        self.assertEqual((se / f"nav_{HOST}.json").read_text(), f"orig nav_{HOST}.json")
        self.assertEqual(self.names(self.tmp / "reset2"), sorted([f"nav_{OTHER}.json", f"nav_guard_{OTHER}.json"]))

    def test_a_crash_between_moves_is_recovered(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        R.isolate_nav(se, dest, marker, "save", HOST)
        shutil.move(str(dest / f"nav_guard_{HOST}.json"), str(se / f"nav_guard_{HOST}.json"))  # never moved
        self.assertEqual(R.recover_nav(marker), [f"nav_{HOST}.json"])
        self.assertEqual(len(self.names(se)), 6)


class StartHost(unittest.TestCase):
    """The start save's host uuid comes from the mod's inspector (a subprocess; its report's host_uuid)."""

    def runner(self, rc=0, out="", err=""):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, out, err)

        return run, calls

    def test_the_inspectors_host_uuid(self):
        run, calls = self.runner(out=json.dumps({"host_uuid": HOST, "findings": []}))
        self.assertEqual(R.start_host(Path("/store/x/S.lsv"), run=run), HOST)
        self.assertEqual(calls[0][1:], [str(R.INSPECT), "/store/x/S.lsv", "--shallow"])
        self.assertTrue(R.INSPECT.is_file())

    def test_no_host_or_a_failed_inspector_is_refused(self):
        for run, _ in (self.runner(out=json.dumps({"findings": []})), self.runner(out="not json"),
                       self.runner(out=json.dumps({"host_uuid": "Claude"})), self.runner(rc=2, err="refused")):  # fmt: skip
            with self.assertRaises(R.NavIsolationError):
                R.start_host(Path("S.lsv"), run=run)
