"""Offline tests for tools/dev/test_new_game.py's pass_splash (no game needed).

Run: uv run --project bridge python -m unittest tools/dev/test_splash.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_new_game as T  # noqa: E402


class FakeGame:
    def __init__(self, presses_needed=1, errors=0):
        self.presses_needed, self.errors = presses_needed, errors
        self.presses, self.t = 0, 0.0

    def rpc(self, op, args):
        assert op == "ui.widgets"
        if self.errors:
            self.errors -= 1
            raise TimeoutError("client thread busy")
        splash = self.presses < self.presses_needed
        return [{"n": "SplashScreen", "vis": splash}, {"n": "MainMenu", "vis": not splash}]

    def press(self):
        self.presses += 1

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


class PassSplash(unittest.TestCase):
    def run_it(self, g, timeout=60.0):
        return T.pass_splash(g.rpc, g.press, timeout=timeout, clock=g.clock, sleep=g.sleep)

    def test_no_splash_no_press(self):
        g = FakeGame(presses_needed=0)
        self.assertEqual(self.run_it(g), 0)

    def test_presses_until_gone(self):
        g = FakeGame(presses_needed=2)
        self.assertEqual(self.run_it(g), 2)

    def test_eval_errors_retry_without_pressing(self):
        g = FakeGame(presses_needed=1, errors=2)
        self.assertEqual(self.run_it(g), 1)

    def test_timeout(self):
        g = FakeGame(presses_needed=10**6)
        with self.assertRaisesRegex(TimeoutError, "splash screen still up"):
            self.run_it(g, timeout=10)
        self.assertGreater(g.presses, 3)


if __name__ == "__main__":
    unittest.main()
