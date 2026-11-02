"""Offline: actq.Watcher's batched check read (BATCH_CHECKS) falling back when the read fails, and the round-trip
metrics (metrics.round_trips, the per-tool rpc/round-trip counts, combat_turn_rt in recorded runs).

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_turn_rpcs.py'
"""

from __future__ import annotations

import threading
import time
import unittest

import test_turn_macros as T
from bg3ai import actq, metrics
from bg3ai.game import GameError


class SerialRoundTrips(unittest.TestCase):
    def test_a_failed_read_falls_back_like_try_rpc(self):
        class Broken(T.FakeGame):
            def read(self, *a, **kw):
                raise GameError("tick lost")
        w = actq.Watcher(Broken())
        saved = actq.BATCH_CHECKS
        actq.BATCH_CHECKS = True
        try:
            w.baseline()
            self.assertEqual(w.start, {})
            self.assertIsNone(w.check("move_to", None, ui=False))
        finally:
            actq.BATCH_CHECKS = saved


class RoundTripMetric(unittest.TestCase):
    def test_round_trips_merge_parallel_evals(self):
        self.assertEqual(metrics.round_trips([(0, 1), (0.1, 0.9), (1, 2), (3, 4), (3.5, 5)]), 3)
        self.assertEqual(metrics.round_trips([]), 0)

    def test_tool_record_counts_both_connections(self):
        rec = metrics.Recorder(None)
        now = time.time()
        rec.rpc_done("state.combat", now - 0.30, now - 0.28)
        w = rec.watch_view()
        w.rpc_done("events.since+act.watch", now - 0.20, now - 0.18)
        w.rpc_done("events.since", now - 0.20, now - 0.185)  # the client half of the same read
        out = rec.tool_done("combat_turn", 400.0, t1=now)
        self.assertEqual((out["rpc_n"], out["rt_n"], out["watch_n"]), (1, 2, 2))
        other = threading.Thread(target=lambda: w.rpc_done("act.watch", now - 0.1, now - 0.09))
        other.start()
        other.join()  # the monitor thread's polls are not the tool's
        self.assertEqual(rec.tool_done("combat_turn", 400.0, t1=now)["watch_n"], 2)

    def test_session_summary_has_combat_turn_round_trips(self):
        t = 1000.0
        tools = [{"type": "tool", "tool": "combat_turn", "t0": t, "t1": t + 5, "ms": 5000, "rt_n": n}
                 for n in (10, 20, 30)] + [{"type": "tool", "tool": "combat_turn", "t0": t, "t1": t + 1, "ms": 900},
                                           {"type": "tool", "tool": "look", "t0": t, "t1": t + 1, "ms": 90, "rt_n": 2}]
        s = metrics.summarize(tools, [], [], start=t, end=t + 60)
        self.assertEqual((s["combat_turn_rt"]["n"], s["combat_turn_rt"]["median"]), (3, 20))


if __name__ == "__main__":
    unittest.main()
