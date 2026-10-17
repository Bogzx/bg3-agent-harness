"""Offline tests: the session clock line says how much of the time left is play time, and only says
WRAP UP at the runner's wrap-up time (BG3AI_SESSION_WRAPUP), scaled for short sessions.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bg3ai import rules, server

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "memory"))
import session  # noqa: E402

ADVANCE = (": keep advancing the story: start the next step even if it won't finish; the next session continues from "
           "where the party stands")
NEAR = (". Until then: start the next story step now, even a fight or a descent: it needn't finish. A fight holds the "
        "session until it ends, and the next session continues from where the party stands")


class Clock(unittest.TestCase):
    def setUp(self):
        self.saved = server.DEADLINE
        self.now = time.time()
        for p in (mock.patch.object(server, "_stuck", lambda: None), mock.patch.object(server, "_fight_now", lambda: False),
                  mock.patch.dict(server._hold, {"of": None})):
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
        self.assertEqual(line, "⏱ " + time.strftime("%H:%M") + " · WRAP UP at " + self.at(14 * 60 + 30 - 5 * 60) + NEAR)
        self.assertNotIn("WRAP UP NOW", line)
        self.assertIn("WRAP UP NOW", self.clock(4 * 60 + 50, 5 * 60))
        self.assertEqual(self.clock(16 * 60, 5 * 60), "⏱ " + time.strftime("%H:%M") + " · 11m of play left until "
                         "WRAP UP at " + self.at(11 * 60) + ADVANCE)

    def test_hour_session(self):
        self.assertEqual(self.clock(24 * 60 + 30, 10 * 60),
                         "⏱ " + time.strftime("%H:%M") + " · 14m of play left until WRAP UP at "
                         + self.at(14 * 60 + 30) + ADVANCE)
        self.assertIn("WRAP UP NOW", self.clock(9 * 60 + 59, 10 * 60))

    def test_without_the_runner_env_defaults_to_10_min(self):
        self.assertIn("30m of play left", self.clock(40 * 60, None))
        self.assertIn("WRAP UP NOW", self.clock(9 * 60, None))

    def test_no_time_to_the_end_before_wrap_up(self):
        # S070 21:09: "9m left · 6m of play: keep playing until WRAP UP at 3m left" -> "Time is nearly up", wrap-up
        # at once (end_session x5, HANDOFF x6). Before WRAP UP the line names only the play time and WRAP UP's time.
        # S078: the near line has no countdown at all, only what to do and WRAP UP's time.
        line = self.clock(9 * 60 + 30, 3 * 60)
        self.assertNotRegex(line, r"\d+m ")
        self.assertIn("WRAP UP at " + self.at(6 * 60 + 30), line)
        self.assertNotIn("WRAP UP NOW", line)
        self.assertNotIn("nearly", line)
        self.assertIn("start the next story step now", line)

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
        self.assertNotIn("advancing the story", self.line_at(14 * 60 + 30, 3 * 60))
        self.assertIn("11m of play left", self.line_at(14 * 60 + 30, 3 * 60))
        self.assertIn("WRAP UP NOW", self.line_at(2 * 60 + 50, 3 * 60))

    def test_last_minutes_of_play_full_line_every_result(self):
        # S073 22:55-22:58: terse "3m of play left" lines only; the agent looted, ran equip_best, sent junk to camp and
        # waited 60 s next to the unused elevator to Cazador's dungeon instead of starting the next story step.
        # S077/S078 began the wrap-up at ~6 min of play: the near line covers the last 10.
        self.assertTrue(self.line_at(5 * 60 + 30, 3 * 60).endswith(NEAR))  # 2m30 of play left
        self.assertIn("WRAP UP at " + self.at(2 * 60 + 30) + ".", self.line_at(5 * 60 + 30, 3 * 60))
        self.assertTrue(self.line_at(3 * 60 + 10 * 60 - 5, 3 * 60).endswith(NEAR))  # just inside the 10 min
        self.assertEqual(self.line_at(3 * 60 + 10 * 60 + 30, 3 * 60),
                         "⏱ " + time.strftime("%H:%M") + " · 10m of play left")  # 10m30 of play: terse

    def session_clock(self, left_s: float, wrap_left_s: float) -> str:
        server.DEADLINE = self.now + left_s
        env = {"BG3AI_SESSION_WRAPUP": str(server.DEADLINE - wrap_left_s)}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(server.time, "time", lambda: self.now):
            return server.session_clock()

    def test_session_clock_says_no_need_to_call_again(self):
        # S073 called session_clock 10 times in 2.5 min (22:56-22:58), waiting for WRAP UP
        out = self.session_clock(5 * 60 + 30, 3 * 60)
        self.assertIn("start the next story step now", out)
        self.assertIn("No need to call session_clock again: the ⏱ line on every result switches to WRAP UP NOW at "
                      + self.at(2 * 60 + 30), out)
        self.assertNotIn("session_clock again", self.session_clock(2 * 60, 3 * 60))  # WRAP UP NOW: just the line
        self.assertIn("WRAP UP NOW", self.session_clock(2 * 60, 3 * 60))


class FightHold(unittest.TestCase):
    """S078 01:42:25 (2 min of play before WRAP UP): "Time is nearly up, so I'm reloading" a Viconia fight at round 1.
    In a fight the ⏱ line has no time, and a fight on at WRAP UP holds the session until it ends (at most
    FIGHT_HOLD_S past the planned WRAP UP)."""

    def setUp(self):
        self.t0 = time.time()
        self.now = self.t0
        self.fight = False
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.hold = self.tmp / "S001.hold.json"
        self.deadline = self.t0 + 30 * 60
        self.wrap = self.deadline - 180
        for p in (mock.patch.object(server, "_stuck", lambda: None),
                  mock.patch.object(server, "_fight_now", lambda: self.fight),
                  mock.patch.object(server, "DEADLINE", self.deadline),
                  mock.patch.object(server, "HOLD_PATH", str(self.hold)),
                  mock.patch.dict(server._hold, {"of": None}),
                  mock.patch.dict(server._clock_shown, {"at": 0.0, "stuck": None}),
                  mock.patch.dict(os.environ, {"BG3AI_SESSION_WRAPUP": str(self.wrap),
                                               "BG3AI_SESSION_DEADLINE": str(self.deadline),
                                               "BG3AI_FIGHT_HOLD_PATH": str(self.hold)}),
                  mock.patch.object(server.time, "time", lambda: self.now)):
            p.start()
            self.addCleanup(p.stop)

    def hhmm(self) -> str:
        return time.strftime("%H:%M")  # the server's wall clock (strftime doesn't go through the patched time.time)

    def test_in_combat_line_has_no_countdown(self):
        self.now = self.wrap - 2 * 60   # S078's moment: 2 min of play before WRAP UP
        self.fight = True
        for line in (server._clock_line(), server._clock_line(), server._clock(), server.session_clock()):
            self.assertEqual(line, f"⏱ {self.hhmm()} · " + server.FIGHT_LINE)
            self.assertNotRegex(line, r"\d+m|WRAP UP|left|nearly")
        self.now = self.t0 + 60   # early in the session too: no "28m of play left" in a fight
        self.assertEqual(server._clock_line(), f"⏱ {self.hhmm()} · " + server.FIGHT_LINE)
        self.fight = False
        self.assertIn("m of play left until WRAP UP", server._clock_line())
        self.assertFalse(self.hold.exists())  # the fight ended before WRAP UP: nothing held

    def test_fight_spanning_the_deadline_extends_the_session_until_it_ends(self):
        self.fight = True
        self.now = self.wrap - 60
        self.assertEqual(server._clock_line(), f"⏱ {self.hhmm()} · " + server.FIGHT_LINE)
        self.now = self.deadline + 4 * 60   # past the planned end, still fighting
        self.assertEqual(server._clock_line(), f"⏱ {self.hhmm()} · " + server.FIGHT_LINE)
        self.assertFalse(server._session_over())
        cap = self.wrap + server.FIGHT_HOLD_S
        self.assertEqual(server._times(), (cap, cap + 180, True))
        self.assertEqual(rules.stop_refusal("move_to", server._times()[1], now=self.now), "")
        held = json.loads(self.hold.read_text())
        self.assertEqual((held["held"], held["wrap"], held["deadline"]), (True, int(cap), int(cap + 180)))
        # the runner's end check sees the hold: mid-fight end_session / stop check says finish the fight, no time
        self.assertEqual(session.wrap_at(), int(cap))
        self.assertEqual(session.session_deadline(), int(cap + 180))
        self.fight = False   # the fight ends 9 min after WRAP UP: WRAP UP now, the end its 3 min later
        self.now = self.wrap + 9 * 60
        end = self.now + 180
        self.assertEqual(server._times(), (self.now, end, False))
        line = server._clock_line()
        self.assertIn("3m left: WRAP UP NOW", line)
        held = json.loads(self.hold.read_text())
        self.assertEqual((held["held"], held["wrap"], held["deadline"]), (False, int(self.now), int(end)))
        self.assertEqual(session.session_deadline(), int(end))
        self.now = end + 1
        self.assertTrue(server._session_over())
        self.assertIn("SESSION OVER", server._clock_line())

    def test_the_hold_is_bounded(self):
        self.fight = True
        self.now = self.wrap + 1
        self.assertTrue(server._times()[2])
        cap = self.wrap + server.FIGHT_HOLD_S
        self.now = cap + 1   # still fighting: the normal end comes
        self.assertEqual(server._times(), (cap, cap + 180, False))
        self.assertIn("WRAP UP NOW", server._clock_line())
        self.now = cap + 181
        self.assertTrue(server._session_over())

    def test_a_restarted_server_keeps_the_sessions_cap(self):
        # crash resume: a new server process with the runner's resumed (later) clock reads the hold file
        cap = self.wrap + server.FIGHT_HOLD_S
        self.hold.write_text(json.dumps({"wrap": int(cap), "deadline": int(cap + 180), "held": True, "cap": int(cap)}))
        later = self.wrap + 5 * 60
        with mock.patch.object(server, "DEADLINE", later + 180), \
                mock.patch.dict(os.environ, {"BG3AI_SESSION_WRAPUP": str(later)}):
            self.now = later + 60
            self.fight = True
            self.assertEqual(server._times(), (int(cap), int(cap) + 180, True))
            self.fight = False   # it ended while the server was down: WRAP UP now
            self.assertEqual(server._times(), (self.now, self.now + 180, False))

    def test_fight_events_drop_the_combat_read(self):
        reads = []
        g = mock.Mock(try_rpc=lambda op: reads.append(op) or {"active": True})
        real = _REAL_FIGHT_NOW   # setUp patched server._fight_now
        with mock.patch.object(server, "game", g), mock.patch.dict(server._hold, {"fight": None, "read_at": 0.0}):
            self.assertTrue(real())
            self.assertTrue(real())
            self.assertEqual(len(reads), 1)   # kept FIGHT_READ_S
            server._fight_seen("combat_turn", "EVENTS\n  combat_ended")
            real()
            self.assertEqual(len(reads), 2)
            server._fight_seen("move_to", "")
            real()
            self.assertEqual(len(reads), 2)


_REAL_FIGHT_NOW = getattr(server, "_fight_now", None)


if __name__ == "__main__":
    unittest.main()
