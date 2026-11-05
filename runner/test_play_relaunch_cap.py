#!/usr/bin/env python3
"""The relaunch point between sessions follows the game's memory cap (morning-2026-11-05 decision 8): at an 8G cap the
old flat 7000 left ~45 min of growth under ramwatch's 7692 kill. Next to test_play_saves.py, which is at the line cap
and whose Relaunch rig these tests share. Never launches claude or the game.

    python3 runner/test_play_relaunch_cap.py
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir before play)
import test_play_saves as saves_tests
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy


class RelaunchLimits(unittest.TestCase):
    def test_the_relaunch_point_follows_the_game_cap(self):
        self.assertEqual(rpolicy.relaunch_limits(8192), (6500, 7692))
        self.assertEqual(rpolicy.relaunch_limits(10240), (7000, 9000))  # 10G unchanged
        self.assertEqual(rpolicy.relaunch_limits(None), (7000, 9000))  # no cap read: the 10G numbers
        self.assertEqual(rpolicy.relaunch_limits(12288), (7000, 9000))
        self.assertEqual(rpolicy.relaunch_limits(6144), (4400, 5644))

    def test_zero_stays_no_ram_check_under_a_cap(self):
        with mock.patch.object(renv, "RELAUNCH_RAM_MB", 0):
            self.assertEqual(rpolicy.relaunch_limits(8192), (0, 7692))

    def test_the_reason_names_the_kill_it_was_given(self):
        now = 1_000_000.0
        why = rpolicy.relaunch_reason(now - 600, now, "", 2, ram_mb=6600, ram_limit_mb=6500, ram_kill_mb=7692) or ""
        self.assertIn("relaunched at 6500 MB", why)
        self.assertIn("kills it at 7692 MB", why)


class RelaunchAtTheCap(unittest.TestCase):
    _runner = saves_tests.Relaunch._runner
    tearDown = saves_tests.Relaunch.tearDown
    killed: list[int]  # the rig's: set by _runner
    logs: list[str]

    def test_a_game_under_an_8g_cap_is_relaunched_at_6500(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0, ram=6600, cap=8192)
            why = r.maybe_relaunch(False, 30) or ""
            self.assertIn("relaunched at 6500 MB", why)
            self.assertIn("kills it at 7692 MB", why)
            self.assertEqual(self.killed, [1])

    def test_the_same_ram_is_kept_under_a_10g_cap(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._runner(Path(td), launched=time.time() - 600, log_text="ok", save_mtime=100.0, ram=6600, cap=10240)
            self.assertIsNone(r.maybe_relaunch(False, 30))
            self.assertTrue(any("6600 MB RAM+swap" in m and "or 7000 MB" in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
