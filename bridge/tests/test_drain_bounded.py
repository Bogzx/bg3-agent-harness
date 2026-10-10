"""Game.drain_events_bounded: a stalled tick (a dialogue's end) doesn't hold the tool result; its events reach
the next drain instead of being lost."""

import threading
import time
import unittest

from bg3ai.game import Game


class Bounded(unittest.TestCase):
    def setUp(self):
        self.g = Game.__new__(Game)
        self.g._stash_lock = threading.Lock()
        self.g._stash = {"server": [], "client": []}
        self.g._bg_drain = {"server": None, "client": None}
        self.g._ev_locks = {"server": threading.Lock(), "client": threading.Lock()}
        self.g._tl = threading.local()
        self.g.event_seq = {"server": 0, "client": 0}
        self.delay = 0.0
        self.calls = 0

        def multi(calls, context, events, timeout=12.0):
            self.calls += 1
            with self.g._ev_locks[context]:
                time.sleep(self.delay)
                with self.g._stash_lock:
                    self.g._stash[context].append({"seq": self.calls, "kind": "dialogue_ended"})
            return [], [], True
        self.g._multi = multi

    def test_fast_tick_returns_its_events(self):
        out = self.g.drain_events_bounded("server", 0.25)
        self.assertEqual([e["kind"] for e in out], ["dialogue_ended"])

    def test_stalled_tick_returns_early_and_hands_events_to_the_next_drain(self):
        self.delay = 0.6
        t0 = time.monotonic()
        out = self.g.drain_events_bounded("server", 0.1)
        self.assertLess(time.monotonic() - t0, 0.4)
        self.assertEqual(out, [])
        self.g._bg_drain["server"].join()
        self.delay = 0.0
        later = self.g.drain_events_bounded("server", 0.25)
        self.assertIn("dialogue_ended", [e["kind"] for e in later])
        self.assertEqual(later[0]["seq"], 1, "the stalled drain's events come first")

    def test_fresh_context_needs_no_rpc(self):
        self.g._fresh().add("server")
        self.g._stash["server"].append({"seq": 9, "kind": "x"})
        self.assertEqual(self.g.drain_events_bounded("server", 0.25), [{"seq": 9, "kind": "x"}])
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
