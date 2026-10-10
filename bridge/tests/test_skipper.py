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

    def test_stuck_dialogue_line_gets_one_shim_space_per_request(self):
        # S023: cinematic lines the page's skip command and the engine SPACE did not skip.
        want = {"seq": 5, "key": "SPACE", "reason": "line", "try": 3, "at": 1000}
        self.game.status = {"on": True, "now": 1100, "line": {"tries": 3, "age": 1500}, "want": want}
        for _ in range(3):
            self.sk.step()
        self.assertEqual(self.inp.keys, ["SPACE"])
        self.assertIn("line skipped (SPACE via shim, try 3)", self.logs[-1])

    def test_no_line_space_when_stale_or_the_line_is_gone(self):
        want = {"seq": 6, "key": "SPACE", "reason": "line", "try": 3, "at": 1000}
        self.game.status = {"on": True, "now": 1900, "line": {"tries": 3}, "want": want}  # stale request
        self.sk.step()
        self.game.status = {"on": True, "now": 1100, "want": dict(want, seq=7)}  # no line any more
        self.sk.step()
        self.assertEqual(self.inp.keys, [])

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


    def test_load_movie_requests(self):
        # The New Game intro during StartLoading: the mod asks every 2 s; each new seq gets one key.
        for seq in (1, 2, 3):
            self.game.status = {"on": True, "movie": {"age": 2000 * seq, "tries": seq + 1, "kind": "load"},
                                "want": {"seq": seq, "key": "ESCAPE", "reason": "load_movie", "try": seq + 1}}
            self.sk.step()
            self.sk.step()
        self.assertEqual(self.inp.keys, ["ESCAPE"] * 3)

    def test_one_skipper_per_game(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, "skipper.lock")
            a = MovieSkipper(FakeGame(), lambda: self.inp, lock_path=lock)
            b_game = FakeGame()
            b = MovieSkipper(b_game, lambda: self.inp, lock_path=lock, lock_retry_s=0.05)
            self.assertTrue(a.acquire())
            stop = threading.Event()
            t = threading.Thread(target=b.run, args=(stop,))
            t.start()
            time.sleep(0.3)
            self.assertEqual(b_game.calls, [])  # idles while a holds the lock
            a.release()
            time.sleep(0.4)
            stop.set()
            t.join(1.0)
            self.assertIn("autoskip.status", b_game.calls)  # took over
            self.assertTrue(a.acquire())  # b released it on stop
            a.release()

    def test_start_background_can_be_disabled(self):
        import os
        from unittest import mock
        from bg3ai.skipper import start_background
        with mock.patch.dict(os.environ, {"BG3AI_MOVIE_SKIP": "0"}):
            self.assertIsNone(start_background())

if __name__ == "__main__":
    unittest.main()
