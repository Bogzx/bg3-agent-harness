"""Offline tests: the session clock line shows no time left before WRAP UP, and says WRAP UP NOW at the runner's
wrap-up time (BG3AI_SESSION_WRAPUP), scaled for short sessions.

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

from bg3ai import rules
from bg3ai.tools import core, session as session_tools

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "memory"))
import session  # noqa: E402

def keep() -> str:
    """The line before WRAP UP NOW at the current wall-clock minute (the server's strftime isn't patched)."""
    return "⏱ " + time.strftime("%H:%M") + " · keep playing: you'll be told when to wrap up"


NO_TIME = r"\d+ ?m\b|\d+ min|WRAP UP at|\d\d:\d\d\D*\d\d:\d\d"   # a countdown, a WRAP UP time, a 2nd HH:MM


class Clock(unittest.TestCase):
    def setUp(self):
        self.saved = core.DEADLINE
        self.now = time.time()
        for p in (mock.patch.object(core, "_stuck", lambda: None), mock.patch.object(core, "_fight_now", lambda: False),
                  mock.patch.dict(core._hold, {"of": None})):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        core.DEADLINE = self.saved

    def at(self, in_s: float) -> str:
        return time.strftime("%H:%M", time.localtime(self.now + in_s))

    def clock(self, left_s: float, wrap_left_s: float | None) -> str:
        core.DEADLINE = self.now + left_s
        wrap_at = 0.0 if wrap_left_s is None else core.DEADLINE - wrap_left_s
        with mock.patch.object(core, "WRAPUP_AT", wrap_at), mock.patch.object(time, "time", lambda: self.now):
            return core._clock()

    def test_short_session_14m_left_is_play_time(self):
        # 15-min session: wrap-up is its last third (5 min). 14m left must not read as "wrap up".
        line = self.clock(14 * 60 + 30, 5 * 60)
        self.assertEqual(line, keep())
        self.assertIn("WRAP UP NOW", self.clock(4 * 60 + 50, 5 * 60))
        self.assertEqual(self.clock(16 * 60, 5 * 60), keep())

    def test_hour_session(self):
        self.assertEqual(self.clock(24 * 60 + 30, 10 * 60), keep())
        self.assertIn("WRAP UP NOW", self.clock(9 * 60 + 59, 10 * 60))

    def test_without_the_runner_env_defaults_to_10_min(self):
        self.assertEqual(self.clock(40 * 60, None), keep())
        self.assertIn("WRAP UP NOW", self.clock(9 * 60, None))

    def test_no_time_left_before_wrap_up(self):
        # S069 "13m left", S070 "6m of play", S077/S078 ~6-8 min of play, S082 at 7 min of play ("I'm out of time to
        # work out the door", two S082-end saves): every countdown or WRAP UP time was read as a clock to beat.
        # Before WRAP UP NOW the line shows no time left at all, early or late in the session.
        for left in (40 * 60, 13 * 60, 9 * 60 + 30, 3 * 60 + 5):
            line = self.clock(left, 3 * 60)
            self.assertEqual(line, keep(), left)
            self.assertNotRegex(line, NO_TIME)

    def test_over(self):
        self.assertIn("SESSION OVER", self.clock(-5, 5 * 60))

    def test_between_full_lines_a_terse_clock(self):
        # S009: with no clock for 4 minutes the agent guessed the session was over (15 min early).
        core.DEADLINE = self.now + 30 * 60
        with mock.patch.object(time, "time", lambda: self.now), \
                mock.patch.dict(core._clock_shown, {"at": 0.0, "stuck": None}):
            self.assertEqual(core._clock_line(), keep())  # first: the full line
            self.assertEqual(core._clock_line(), keep())

    def line_at(self, left_s: float, wrap_left_s: float) -> str:
        """_clock_line with left_s to go, a full line shown 1 min ago and WRAP UP at wrap_left_s before the end."""
        core.DEADLINE = self.now + left_s
        with mock.patch.object(core, "WRAPUP_AT", core.DEADLINE - wrap_left_s), \
                mock.patch.object(time, "time", lambda: self.now), \
                mock.patch.dict(core._clock_shown, {"at": self.now - 60, "stuck": None}):
            return core._clock_line()

    def test_terse_line_has_no_time_left(self):
        # S069: "⏱ 20:38 · 13m left" (the last full line 7 min before) read as wrap-up: S069-end + HANDOFF at 13m left
        for left in (13 * 60 + 30, 5 * 60 + 30, 3 * 60 + 10):
            self.assertEqual(self.line_at(left, 3 * 60), keep(), left)

    def test_full_lines_every_call_from_wrap_up_on(self):
        self.assertIn("WRAP UP NOW", self.line_at(2 * 60 + 50, 3 * 60))
        self.assertIn("WRAP UP NOW", self.line_at(10, 3 * 60))

    def session_clock(self, left_s: float, wrap_left_s: float) -> str:
        core.DEADLINE = self.now + left_s
        with mock.patch.object(core, "WRAPUP_AT", core.DEADLINE - wrap_left_s), \
                mock.patch.object(time, "time", lambda: self.now):
            return session_tools.session_clock()

    def test_session_clock_says_no_need_to_call_again(self):
        # S073 called session_clock 10 times in 2.5 min (22:56-22:58), waiting for WRAP UP. S082: no time in it either.
        out = self.session_clock(5 * 60 + 30, 3 * 60)
        self.assertTrue(out.startswith(keep()), out)
        self.assertIn("No need to call session_clock again: the ⏱ line on every result switches to WRAP UP NOW by "
                      "itself. Until then, play.", out)
        self.assertNotRegex(out, NO_TIME)
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
        for p in (mock.patch.object(core, "_stuck", lambda: None),
                  mock.patch.object(core, "_fight_now", lambda: self.fight),
                  mock.patch.object(core, "DEADLINE", self.deadline),
                  mock.patch.object(core, "HOLD_PATH", str(self.hold)),
                  mock.patch.object(core, "WRAPUP_AT", self.wrap),
                  mock.patch.dict(core._hold, {"of": None}),
                  mock.patch.dict(core._clock_shown, {"at": 0.0, "stuck": None}),
                  mock.patch.dict(os.environ, {"BG3AI_SESSION_WRAPUP": str(self.wrap),
                                               "BG3AI_SESSION_DEADLINE": str(self.deadline),
                                               "BG3AI_FIGHT_HOLD_PATH": str(self.hold)}),
                  mock.patch.object(time, "time", lambda: self.now)):
            p.start()
            self.addCleanup(p.stop)

    def hhmm(self) -> str:
        return time.strftime("%H:%M")  # the server's wall clock (strftime doesn't go through the patched time.time)

    def test_in_combat_line_has_no_countdown(self):
        self.now = self.wrap - 2 * 60   # S078's moment: 2 min of play before WRAP UP
        self.fight = True
        for line in (core._clock_line(), core._clock_line(), core._clock(), session_tools.session_clock()):
            self.assertEqual(line, f"⏱ {self.hhmm()} · " + core.FIGHT_LINE)
            self.assertNotRegex(line, r"\d+m|WRAP UP|left|nearly")
        self.now = self.t0 + 60   # early in the session too: no "28m of play left" in a fight
        self.assertEqual(core._clock_line(), f"⏱ {self.hhmm()} · " + core.FIGHT_LINE)
        self.fight = False
        self.assertEqual(core._clock_line(), keep())
        self.assertFalse(self.hold.exists())  # the fight ended before WRAP UP: nothing held

    def test_fight_spanning_the_deadline_extends_the_session_until_it_ends(self):
        self.fight = True
        self.now = self.wrap - 60
        self.assertEqual(core._clock_line(), f"⏱ {self.hhmm()} · " + core.FIGHT_LINE)
        self.now = self.deadline + 4 * 60   # past the planned end, still fighting
        self.assertEqual(core._clock_line(), f"⏱ {self.hhmm()} · " + core.FIGHT_LINE)
        self.assertFalse(core._session_over())
        cap = self.wrap + core.FIGHT_HOLD_S
        self.assertEqual(core._times(), (cap, cap + 180, True))
        self.assertEqual(rules.stop_refusal("move_to", core._times()[1], now=self.now), "")
        held = json.loads(self.hold.read_text())
        self.assertEqual((held["held"], held["wrap"], held["deadline"]), (True, int(cap), int(cap + 180)))
        # the runner's end check sees the hold: mid-fight end_session / stop check says finish the fight, no time
        self.assertEqual(session.wrap_at(), int(cap))
        self.assertEqual(session.session_deadline(), int(cap + 180))
        self.fight = False   # the fight ends 9 min after WRAP UP: WRAP UP now, the end its 3 min later
        self.now = self.wrap + 9 * 60
        end = self.now + 180
        self.assertEqual(core._times(), (self.now, end, False))
        line = core._clock_line()
        self.assertIn("3m left: WRAP UP NOW", line)
        held = json.loads(self.hold.read_text())
        self.assertEqual((held["held"], held["wrap"], held["deadline"]), (False, int(self.now), int(end)))
        self.assertEqual(session.session_deadline(), int(end))
        self.now = end + 1
        self.assertTrue(core._session_over())
        self.assertIn("SESSION OVER", core._clock_line())

    def test_a_fight_still_on_at_the_cap_plays_on_to_the_last_end(self):
        # S095: still fighting at the cap -> "2m left: WRAP UP NOW. Reach a safe spot" in the decisive round, then
        # REFUSED 10 min before the runner's hard stop
        self.fight = True
        self.now = self.wrap + 1
        self.assertTrue(core._times()[2])
        cap = self.wrap + core.FIGHT_HOLD_S
        last = cap + 180 + core.FIGHT_OVERRUN_S
        self.assertEqual(last, self.deadline + core.FIGHT_HOLD_S + 10 * 60 - 120)  # 2 min before the hard stop
        self.now = cap + 1   # still fighting: held to the last end, no WRAP UP, no refusal
        self.assertEqual(core._times(), (last, last, True))
        self.assertEqual(core._clock_line(), f"⏱ {self.hhmm()} · " + core.FIGHT_LINE)
        self.now = cap + 181   # the capped end: the session goes on
        self.assertFalse(core._session_over())
        self.assertEqual(rules.stop_refusal("move_to", core._times()[1], now=self.now), "")
        held = json.loads(self.hold.read_text())
        self.assertEqual((held["held"], held["wrap"], held["deadline"]), (True, int(last), int(last)))
        self.fight = False   # it ends in the overrun: WRAP UP now, the end its 3 min later
        self.assertEqual(core._times(), (self.now, self.now + 180, False))
        self.assertIn("3m left: WRAP UP NOW", core._clock_line())
        self.fight = True   # a new fight after the cap isn't held; the coaching hint names no safe spot in it
        self.assertFalse(core._times()[2])
        with mock.patch.object(rules, "coaching", lambda: True):
            line = core._clock()
            self.assertIn("WRAP UP NOW. Save when the fight allows, then", line)
            self.assertNotIn("safe spot", line)
            self.fight = False
            self.assertIn("WRAP UP NOW. Reach a safe spot, save_game, then", core._clock())

    def test_the_overrun_is_bounded(self):
        self.fight = True
        self.now = self.wrap + 1
        self.assertTrue(core._times()[2])
        last = self.wrap + core.FIGHT_HOLD_S + 180 + core.FIGHT_OVERRUN_S
        self.now = last - 1
        self.assertTrue(core._times()[2])
        self.now = last   # still fighting: the session ends
        self.assertEqual(core._times(), (last, last, False))
        self.now = last + 1
        self.assertTrue(core._session_over())

    def test_a_restarted_server_keeps_the_sessions_cap(self):
        # crash resume: a new server process with the runner's resumed (later) clock reads the hold file
        cap = self.wrap + core.FIGHT_HOLD_S
        self.hold.write_text(json.dumps({"wrap": int(cap), "deadline": int(cap + 180), "held": True, "cap": int(cap)}))
        later = self.wrap + 5 * 60
        with mock.patch.object(core, "DEADLINE", later + 180), \
                mock.patch.object(core, "WRAPUP_AT", later):
            self.now = later + 60
            self.fight = True
            self.assertEqual(core._times(), (int(cap), int(cap) + 180, True))
            self.fight = False   # it ended while the server was down: WRAP UP now
            self.assertEqual(core._times(), (self.now, self.now + 180, False))

    def test_fight_events_drop_the_combat_read(self):
        reads = []
        g = mock.Mock(try_rpc=lambda op: reads.append(op) or {"active": True})
        real = _REAL_FIGHT_NOW   # setUp patched core._fight_now
        with mock.patch.object(core, "game", g), mock.patch.dict(core._hold, {"fight": None, "read_at": 0.0}):
            self.assertTrue(real())
            self.assertTrue(real())
            self.assertEqual(len(reads), 1)   # kept FIGHT_READ_S
            core._fight_seen("combat_turn", "EVENTS\n  combat_ended")
            real()
            self.assertEqual(len(reads), 2)
            core._fight_seen("move_to", "")
            real()
            self.assertEqual(len(reads), 2)


_REAL_FIGHT_NOW = getattr(core, "_fight_now", None)


if __name__ == "__main__":
    unittest.main()
