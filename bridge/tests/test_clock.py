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

ADVANCE = (": keep advancing the story: start the next step even if it won't finish; the next session continues from "
           "where the party stands")


class Clock(unittest.TestCase):
    def setUp(self):
        self.saved = server.DEADLINE
        self.now = time.time()
        p = mock.patch.object(server, "_stuck", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        server.DEADLINE = self.saved

    def at(self, in_s: float) -> str:
        return time.strftime("%H:%M", time.localtime(self.now + in_s))

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
        self.assertEqual(line, "⏱ " + time.strftime("%H:%M") + " · 9m of play left until WRAP UP at "
                         + self.at(14 * 60 + 30 - 5 * 60) + ADVANCE)
        self.assertNotIn("WRAP UP NOW", line)
        self.assertIn("WRAP UP NOW", self.clock(4 * 60 + 50, 5 * 60))

    def test_hour_session(self):
        self.assertEqual(self.clock(14 * 60 + 30, 10 * 60),
                         "⏱ " + time.strftime("%H:%M") + " · 4m of play left until WRAP UP at "
                         + self.at(4 * 60 + 30) + ADVANCE)
        self.assertIn("WRAP UP NOW", self.clock(9 * 60 + 59, 10 * 60))

    def test_without_the_runner_env_defaults_to_10_min(self):
        self.assertIn("30m of play left", self.clock(40 * 60, None))
        self.assertIn("WRAP UP NOW", self.clock(9 * 60, None))

    def test_no_time_to_the_end_before_wrap_up(self):
        # S070 21:09: "9m left · 6m of play: keep playing until WRAP UP at 3m left" -> "Time is nearly up", wrap-up
        # at once (end_session x5, HANDOFF x6). Before WRAP UP the line names only the play time and WRAP UP's time.
        line = self.clock(9 * 60 + 30, 3 * 60)
        self.assertNotIn("9m", line)
        self.assertIn("6m of play left", line)
        self.assertIn("until WRAP UP at " + self.at(6 * 60 + 30), line)
        self.assertNotIn("WRAP UP NOW", line)

    def test_over(self):
        self.assertIn("SESSION OVER", self.clock(-5, 5 * 60))

    def test_between_full_lines_a_terse_clock(self):
        # S009: with no clock for 4 minutes the agent guessed the session was over (15 min early).
        server.DEADLINE = self.now + 30 * 60
        with mock.patch.object(server.time, "time", lambda: self.now), \
                mock.patch.dict(server._clock_shown, {"at": 0.0, "stuck": None}):
            self.assertIn("advancing the story", server._clock_line())  # first: the full line
            self.assertEqual(server._clock_line(), "⏱ " + time.strftime("%H:%M") + " · 20m of play left")

    def line_at(self, left_s: float, wrap_left_s: float) -> str:
        """_clock_line with left_s to go, a full line shown 1 min ago and WRAP UP at wrap_left_s before the end."""
        server.DEADLINE = self.now + left_s
        env = {"BG3AI_SESSION_WRAPUP": str(server.DEADLINE - wrap_left_s)}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(server.time, "time", lambda: self.now), \
                mock.patch.dict(server._clock_shown, {"at": self.now - 60, "stuck": None}):
            return server._clock_line()

    def test_terse_line_keeps_the_play_time(self):
        # S069: "⏱ 20:38 · 13m left" (the last full line 7 min before) read as wrap-up: S069-end + HANDOFF at 13m left
        self.assertEqual(self.line_at(13 * 60 + 30, 3 * 60), "⏱ " + time.strftime("%H:%M") + " · 10m of play left")

    def test_full_lines_every_call_from_wrap_up_on(self):
        # the every-call full line started at the old 10-min wrap-up (WRAPUP_S); it starts at WRAP UP now
        self.assertNotIn("advancing the story", self.line_at(9 * 60 + 30, 3 * 60))
        self.assertIn("6m of play left", self.line_at(9 * 60 + 30, 3 * 60))
        self.assertIn("WRAP UP NOW", self.line_at(2 * 60 + 50, 3 * 60))


    def test_last_minutes_of_play_full_line_every_result(self):
        # S073 22:55-22:58: terse "3m of play left" lines only; the agent looted, ran equip_best, sent junk to camp and
        # waited 60 s next to the unused elevator to Cazador's dungeon instead of starting the next story step.
        self.assertTrue(self.line_at(5 * 60 + 30, 3 * 60).endswith(ADVANCE))  # 2m30 of play left
        self.assertIn("2m of play left until WRAP UP at " + self.at(2 * 60 + 30), self.line_at(5 * 60 + 30, 3 * 60))
        self.assertTrue(self.line_at(3 * 60 + 5 * 60 - 5, 3 * 60).endswith(ADVANCE))  # just inside the 5 min
        self.assertNotIn("advancing", self.line_at(3 * 60 + 5 * 60 + 30, 3 * 60))  # 5m30 of play: terse

    def session_clock(self, left_s: float, wrap_left_s: float) -> str:
        server.DEADLINE = self.now + left_s
        env = {"BG3AI_SESSION_WRAPUP": str(server.DEADLINE - wrap_left_s)}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(server.time, "time", lambda: self.now):
            return server.session_clock()

    def test_session_clock_says_no_need_to_call_again(self):
        # S073 called session_clock 10 times in 2.5 min (22:56-22:58), waiting for WRAP UP
        out = self.session_clock(5 * 60 + 30, 3 * 60)
        self.assertIn("advancing the story", out)
        self.assertIn("No need to call session_clock again: the ⏱ line on every result switches to WRAP UP NOW at "
                      + self.at(2 * 60 + 30), out)
        self.assertNotIn("session_clock again", self.session_clock(2 * 60, 3 * 60))  # WRAP UP NOW: just the line
        self.assertIn("WRAP UP NOW", self.session_clock(2 * 60, 3 * 60))


if __name__ == "__main__":
    unittest.main()
