"""Offline tests: waits whose end the game shows are read every POLL_S (50 ms) with the old fixed sleep or loop as
the cap (latency survey 2026-11-04 §5.5: 0.2-0.5 s sleeps between reads of a readable condition). A fake clock:
time.sleep advances it, nothing really sleeps.

Run: cd bridge && uv run python -m unittest tests.test_poll_waits
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import Base  # sets BG3AI_RUNTIME before importing the server

# isort: split
from bg3ai.tools import batch, core, plumbing, progression, queueing, uitools, world_tools

P1 = "00000000-0000-0000-0000-000000000001"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Polls(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()
        self.t0 = self.clock.t
        for name in ("monotonic", "sleep"):
            p = mock.patch(f"time.{name}", getattr(self.clock, name))
            p.start()
            self.addCleanup(p.stop)

    def waited(self) -> float:
        return round(self.clock.t - self.t0, 3)

    def test_poll_until_reads_every_50_ms_and_caps(self):
        def read():
            return self.waited() >= 0.12

        self.assertTrue(plumbing.poll_until(read, bool, 3.0))
        self.assertEqual(self.waited(), 0.15)
        t = self.clock.t
        self.assertFalse(plumbing.poll_until(lambda: False, bool, 0.4))
        self.assertTrue(0.4 <= self.clock.t - t <= 0.4 + plumbing.POLL_S + 1e-6, self.clock.t - t)  # the cap

    def test_the_trade_window_closes_on_the_first_read_that_shows_it_gone(self):
        def screen():
            return ("trade" if self.waited() < 0.05 else "game"), {}

        with (
            mock.patch.object(uitools, "_ui_screen", screen),
            mock.patch.object(core.game, "try_rpc", lambda *a, **k: None),
        ):
            self.assertTrue(plumbing._close_left_trade(2.0))
        self.assertLessEqual(self.waited(), 0.05)  # was the first read 0.2 s after the ESCAPE

    def test_the_donate_box_is_answered_once_it_shows(self):
        def try_rpc(op, args=None, context="server", default=None):
            if op == "ui.confirm" and self.waited() >= 0.05:
                return {"title": "Donate these items?", "buttons": ["Yes", "No"]}
            return None

        with (
            mock.patch.object(core.game, "try_rpc", try_rpc),
            mock.patch.object(world_tools, "_box_act", lambda *a, **k: "confirm: done"),
        ):
            out = progression._answer_donate({"moved": []}, handover=False)
        self.assertIn("trade: done", out)
        self.assertLessEqual(self.waited(), 0.05)  # was 0.2

    def test_the_trade_screen_wait_ends_when_it_opens(self):
        def try_rpc(op, args=None, context="server", default=None):
            if op != "ui.state":
                return None
            if self.waited() < 0.05:
                return {"screen": "dialogue", "dialogue": {"options": [{"i": 1, "text": "Let me see your wares."}]}}
            return {"screen": "trade"}

        def run_job(op, args, context="server", wait_s=25.0, **k):
            return plumbing.JobResult("done", {})

        with mock.patch.object(core.game, "try_rpc", try_rpc), mock.patch.object(core.game, "run_job", run_job):
            out = progression._open_trade("")
        self.assertIn("(opened:", out)
        self.assertLessEqual(self.waited(), 0.05)  # was 0.2

    def test_fast_travel_arrival_ends_when_the_party_is_there(self):
        wp = [100.0, 0.0, 100.0]

        test = self

        class Watch:
            def try_rpc(self, op, args=None, context="server", default=None):
                pos = [0.0, 0.0, 0.0] if test.waited() < 0.05 else [101.0, 0.0, 99.0]
                return {"party": [{"uuid": P1, "pos": pos}]}

        with mock.patch.object(queueing, "_watch_conn", lambda: Watch()):
            self.assertEqual(batch._ft_arrival(P1, wp), [101.0, 0.0, 99.0])
            self.assertLessEqual(self.waited(), 0.05)  # was 0.5
            t = self.clock.t
            self.assertEqual(batch._ft_arrival("someone else", wp), None)
            cap = batch.FT_ARRIVE_S  # the old 20 reads 0.5 s apart
            self.assertTrue(cap <= self.clock.t - t <= cap + plumbing.POLL_S + 1e-6, self.clock.t - t)


if __name__ == "__main__":
    unittest.main()
