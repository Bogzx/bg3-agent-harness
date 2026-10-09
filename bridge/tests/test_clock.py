"""Offline tests: the session clock line says how much of the time left is play time, and only says
WRAP UP at the runner's wrap-up time (BG3AI_SESSION_WRAPUP), scaled for short sessions.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from bg3ai import server


class Clock(unittest.TestCase):
    def setUp(self):
        self.saved = server.DEADLINE
        self.now = time.time()
        p = mock.patch.object(server, "_stuck", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        server.DEADLINE = self.saved

    def clock(self, left_s: float, wrap_left_s: float | None) -> str:
        server.DEADLINE = self.now + left_s
        env = {} if wrap_left_s is None else {"BG3AI_SESSION_WRAPUP": str(server.DEADLINE - wrap_left_s)}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(server.time, "time", lambda: self.now):
            if wrap_left_s is None:
                os.environ.pop("BG3AI_SESSION_WRAPUP", None)
            return server._clock()

    def test_short_session_14m_left_is_play_time(self):
        # 15-min session: wrap-up is its last third (5 min). 14m left must not read as "wrap up".
        line = self.clock(14 * 60 + 30, 5 * 60)
        self.assertEqual(line, "⏱ 14m left · 9m of play: keep playing until WRAP UP at 5m left")
        self.assertNotIn("WRAP UP NOW", line)
        self.assertIn("WRAP UP NOW", self.clock(4 * 60 + 50, 5 * 60))

    def test_hour_session(self):
        self.assertEqual(self.clock(14 * 60 + 30, 10 * 60),
                         "⏱ 14m left · 4m of play: keep playing until WRAP UP at 10m left")
        self.assertIn("WRAP UP NOW", self.clock(9 * 60 + 59, 10 * 60))

    def test_without_the_runner_env_defaults_to_10_min(self):
        self.assertIn("until WRAP UP at 10m left", self.clock(40 * 60, None))
        self.assertIn("WRAP UP NOW", self.clock(9 * 60, None))

    def test_over(self):
        self.assertIn("SESSION OVER", self.clock(-5, 5 * 60))


if __name__ == "__main__":
    unittest.main()
