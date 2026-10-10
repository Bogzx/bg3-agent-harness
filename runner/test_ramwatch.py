#!/usr/bin/env python3
"""Offline tests for runner/ramwatch.py (fake /proc rows; never signals real processes)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ramwatch as R  # noqa: E402

PROCS = [
    (100, "bg3", "./bin/bg3 -continueGame"),
    (200, "python3", "python3 -m unittest discover -s tests -p test_*.py"),
    (201, "bash", "/bin/bash tools/dev/test_act_lua.sh"),
    (202, "docker", "docker run --rm nickblah/lua lua tools/dev/test_act_lua.lua"),
    (300, "claude", "claude -p --model opus test_x.py"),
    (301, "python3", "python3 runner/play.py run --playthrough claude-run-2"),
    (302, "python3", "uv run --project bridge python -m bg3ai.server"),
]


class RamWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-test-ram"))
        self.logs = []
        self.w = R.RamWatch(self.logs.append, logfile=self.tmp / "ram.log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_selects_only_test_runs(self):
        self.assertEqual(R.game_pids(PROCS), [100])
        self.assertEqual(R.test_pids(PROCS), [200, 201, 202])

    def test_meminfo_parse(self):
        p = self.tmp / "meminfo"
        p.write_text("MemTotal:       16000000 kB\nMemAvailable:    2048000 kB\nSwapFree: 1024 kB\n")
        self.assertEqual(R.meminfo(p)["MemAvailable"], 2000)

    @mock.patch.object(R, "rss_mb", return_value=5000)
    @mock.patch.object(R, "get_adj", return_value=200)
    def test_ok_ram_protects_game_once_and_logs(self, _adj, _rss):
        with mock.patch.object(R, "set_adj", return_value="sudo") as sa, mock.patch.object(R.os, "kill") as k:
            out = self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, PROCS)
            self.w.tick({"MemAvailable": 6000, "SwapFree": 100}, PROCS)
        sa.assert_called_once_with(100, R.GAME_OOM_ADJ)
        k.assert_not_called()
        self.assertEqual(out["action"], "")
        self.assertIn("avail=6000MB", (self.tmp / "ram.log").read_text())

    @mock.patch.object(R, "rss_mb", return_value=5000)
    @mock.patch.object(R, "get_adj", return_value=-500)
    def test_low_ram_marks_tests_then_kills_them(self, _adj, _rss):
        with mock.patch.object(R, "set_adj", return_value="ok") as sa, mock.patch.object(R.os, "kill") as k:
            out = self.w.tick({"MemAvailable": 1200}, PROCS)
            self.assertEqual(sorted(c.args[0] for c in sa.call_args_list), [200, 201, 202])
            k.assert_not_called()
            self.assertIn("marked", out["action"])
            self.assertTrue(any("WARNING" in m for m in self.logs))
            out = self.w.tick({"MemAvailable": 500}, PROCS)
        self.assertEqual(sorted(c.args[0] for c in k.call_args_list), [200, 201, 202])
        self.assertIn("killed 3", out["action"])

    @mock.patch.object(R, "rss_mb", return_value=0)
    @mock.patch.object(R, "get_adj", return_value=200)
    def test_unprivileged_logs_why(self, _adj, _rss):
        with mock.patch.object(R, "set_adj", return_value="not permitted"):
            self.w.tick({"MemAvailable": 6000}, PROCS)
        self.assertTrue(any("can't lower bg3 100" in m for m in self.logs))

    def test_lock_single_instance(self):
        a = R.take_lock(self.tmp / "l")
        self.assertIsNotNone(a)
        self.assertIsNone(R.take_lock(self.tmp / "l"))
        a.close()


if __name__ == "__main__":
    unittest.main()
