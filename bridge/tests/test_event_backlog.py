"""A new bridge process doesn't replay the game's event ring as new events.

S084 04:38-04:39: the mod's ring still held S084's Bhaal fight (the game stayed up between sessions). Each new
bridge process drained it from seq 0: check_all's 5 check processes and S085's first tool result each saw the
fight start and end, and debriefed it again (5 [fight] lines in S084.md, a 6th in S085.md and S085.fights.jsonl).

  uv run --project bridge python -m unittest bridge/tests/test_event_backlog.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fake_bg3le import FakeBg3le  # noqa: E402

from bg3ai.game import Game  # noqa: E402


class Backlog(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBg3le(server_tick_s=0.005, client_tick_s=0.005).start()
        self.addCleanup(self.fake.stop)
        # the last session's fight, before this process exists
        for kind in ("combat_started", "round_started", "died", "combat_ended"):
            self.fake.emit("server", kind)
        self.fake.emit("client", "dialogue_ended")

        def move(a, c):
            self.fake.emit("server", "moved")
            return {"job": 1}
        self.fake.op("act.move", move)
        self.game = Game(port=self.fake.port)
        self.addCleanup(self.game.close)

    def test_first_drain_skips_the_ring_before_the_process(self):
        self.assertEqual(self.game.drain_events("server"), [])   # old code: the 4 fight events
        self.assertEqual(self.game.drain_events("client"), [])
        self.fake.emit("server", "round_started")
        self.assertEqual([e["kind"] for e in self.game.drain_events("server")], ["round_started"])

    def test_the_first_calls_own_events_are_kept(self):
        self.game.rpc("act.move")
        self.assertEqual([e["kind"] for e in self.game.drain_events("server")], ["moved"])

    def test_a_read_with_events_skips_it_too(self):
        self.game.multi([("sys.ping", {})], "server", events=True)
        self.assertEqual(self.game.drain_events("server"), [])
        self.assertEqual(self.game.drain_events("client"), [])

    def test_a_parallel_read_first_waits_for_the_sync(self):
        # read() drains the client in a background thread while the server eval runs: both must wait
        self.game.read1("sys.ping")
        self.assertEqual(self.game.drain_events("server"), [])
        self.assertEqual(self.game.drain_events("client"), [])   # first version: dialogue_ended (seq read too early)

    def test_once_per_process_not_after_a_load(self):
        self.game.drain_events("server")
        with self.fake._cv:   # a load: the mod's Lua state and its ring restart
            self.fake.events["server"], self.fake.seq["server"] = [], 0
        self.game.event_seq = {"server": 0, "client": 0}   # as load_game does
        self.fake.emit("server", "level_loaded")
        self.assertEqual([e["kind"] for e in self.game.drain_events("server")], ["level_loaded"])


if __name__ == "__main__":
    unittest.main()
