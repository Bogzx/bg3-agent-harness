"""Offline tests: the movie skipper sends one shim ESCAPE per AutoSkip request, never without a movie,
and falls back to the ui.state poll for a mod without autoskip.status (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import threading
import time
import unittest

from bg3ai.game import GameError, NotReady
from bg3ai.skipper import MovieSkipper


class FakeGame:
    def __init__(self):
        self.status: dict | Exception = {"on": True, "now": 0}
        self.screen = "world"
        self.calls: list[str] = []

    def rpc(self, op, args=None, context="server", timeout=12.0):
        self.calls.append(op)
        if op == "autoskip.status":
            if isinstance(self.status, Exception):
                raise self.status
            return self.status
        if op == "ui.state":
            return {"screen": self.screen}
        raise GameError(f"unknown op '{op}'")


class FakeInput:
    def __init__(self):
        self.keys: list[str] = []

    def key(self, k, sync=True):
        self.keys.append(k)


class SkipperTest(unittest.TestCase):
    def setUp(self):
        self.game = FakeGame()
        self.inp = FakeInput()
        self.logs: list[str] = []
        self.sk = MovieSkipper(self.game, lambda: self.inp, self.logs.append, fast_s=0.15, fallback_s=2.0)

    def test_idle_polls_fast_and_sends_nothing(self):
        self.assertEqual(self.sk.step(), 0.15)
        self.assertEqual(self.inp.keys, [])
        self.assertEqual(self.game.calls, ["autoskip.status"])

    def test_one_key_per_request(self):
        want = {"seq": 3, "key": "ESCAPE", "reason": "movie", "try": 2}
        self.game.status = {"on": True, "movie": {"age": 400, "tries": 2}, "want": want}
        for _ in range(5):
            self.sk.step()
        self.assertEqual(self.inp.keys, ["ESCAPE"])
        self.game.status = {"on": True, "movie": {"age": 1100, "tries": 3}, "want": dict(want, seq=4, **{"try": 3})}
        self.sk.step()
        self.assertEqual(self.inp.keys, ["ESCAPE", "ESCAPE"])
        self.assertIn("try 3", self.logs[-1])

    def test_no_key_once_the_movie_is_gone(self):
        # A stale want without a movie in the same reply: a stray ESCAPE would open the game menu.
        self.game.status = {"on": True, "want": {"seq": 9, "key": "ESCAPE"}}
        self.sk.step()
        self.assertEqual(self.inp.keys, [])

    def test_not_ready_backs_off(self):
        self.game.status = NotReady("no mod")
        self.assertEqual(self.sk.step(), 1.0)
        self.assertEqual(self.sk.mode, "fast")

    def test_fallback_for_an_old_mod(self):
        self.game.status = GameError("unknown op 'autoskip.status' in client context")
        self.game.screen = "movie"
        self.sk.step()
        self.assertEqual(self.sk.mode, "fallback")
        self.assertEqual(self.inp.keys, [])  # one poll is not enough
        self.sk.step()
        self.assertEqual(self.inp.keys, ["ESCAPE"])
        self.assertIn("fallback", self.logs[-1])
        self.assertNotIn("autoskip.status", self.game.calls[-1:])

    def test_fallback_when_autoskip_is_off(self):
        self.game.status = {"on": False}
        self.sk.step()
        self.assertEqual(self.sk.mode, "fallback")

    def test_fast_path_is_retried_later(self):
        self.game.status = GameError("unknown op 'autoskip.status' in client context")
        self.sk.step()
        self.game.status = {"on": True}
        self.sk.next_fast_probe = time.monotonic() - 1
        self.assertEqual(self.sk.step(), 0.15)
        self.assertEqual(self.sk.mode, "fast")

    def test_no_input_shim(self):
        sk = MovieSkipper(self.game, lambda: None, self.logs.append)
        self.game.status = {"on": True, "movie": {"age": 400}, "want": {"seq": 1, "key": "ESCAPE"}}
        sk.step()
        self.assertEqual(self.logs, [])

    def test_run_stops(self):
        stop = threading.Event()
        t = threading.Thread(target=self.sk.run, args=(stop,))
        t.start()
        time.sleep(0.4)
        stop.set()
        t.join(1.0)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(self.game.calls.count("autoskip.status"), 2)


if __name__ == "__main__":
    unittest.main()
