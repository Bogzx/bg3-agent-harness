#!/usr/bin/env python3
"""An operator stop in the middle of a session, through the real Runner on test_crash.py's fake game and fake claude.

A bench run (run_host sets stop_ends_session) is one session: runtime/STOP or a signal (stop_requested) ends it at the
agent loop's next check, reason "stopped", and run_host records it (live7 §1b: STOP did nothing for 7 min). A
playthrough's session plays on (its STOP means "after this session").

    python3 runner/test_crash_stop.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
from bg3harness.runner import env as renv
from test_crash import FakeRunBase


class OperatorStop(FakeRunBase):
    def stop_when_claude_runs(self, stop) -> None:
        def go():
            while not self.claude_log.exists():  # the fake claude logs its argv as it starts
                time.sleep(0.05)
            stop()

        threading.Thread(target=go, daemon=True).start()

    def test_the_stop_file_ends_a_bench_runs_session(self):
        self.r.stop_ends_session = True
        self.stop_when_claude_runs(lambda: (renv.RUNTIME / "STOP").write_text("1"))
        t0 = time.time()
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "stopped")
        self.assertLess(time.time() - t0, 30)  # not the fake claude's 60 s
        self.assertEqual(len(self.claude_runs()), 1)  # no stop-check resume
        self.assertIsNone(self.r.agent_proc)
        self.assertIn("operator_stop", [e["kind"] for e in self.events()])

    def test_a_signal_ends_a_bench_runs_session(self):
        self.r.stop_ends_session = True
        self.stop_when_claude_runs(lambda: setattr(self.r, "stop_requested", True))
        res = self.r.play_session(20, first=True)
        self.assertEqual(res.reason, "stopped")
        self.assertEqual(len(self.claude_runs()), 1)

    def test_a_playthroughs_session_plays_on_after_the_stop_file(self):
        os.environ["FAKE_CLAUDE_FIRST_S"] = "3"
        self.stop_when_claude_runs(lambda: (renv.RUNTIME / "STOP").write_text("1"))
        t0 = time.time()
        res = self.r.play_session(20, first=True)
        self.assertNotEqual(res.reason, "stopped")
        self.assertGreaterEqual(time.time() - t0, 3)  # the fake claude's whole run


if __name__ == "__main__":
    unittest.main()
