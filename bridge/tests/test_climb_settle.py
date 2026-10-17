"""naut2 runs 2/3 (vis): a queued [interact e56, interact e57] at the Nautiloid helm climbed e56, then e57 failed
"no walkable path; closest walkable spot (-46.2,5.1,-361.8)" (the lower deck's answer, from before the climb). After a
climb the next step waits for the climber to stand still off the ladder, and a "no walkable path" right after a climb
is planned once more from where the climber stands."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bg3ai import actq  # noqa: E402

CLIMBED = {"used": True, "climbed": "up", "dy": 2.9}
NO_PATH = ("interact: FAILED — no walkable path there (may need a jump or another route); closest walkable spot "
           "(-46.2,5.1,-361.8) is 8.4m from it")


class _W:
    """act.watch reads in order (the last one repeats)."""
    last: dict = {}

    def __init__(self, rows: list):
        self.rows, self.reads = rows, 0

    def _watch(self) -> dict:
        row = self.rows[min(self.reads, len(self.rows) - 1)]
        self.reads += 1
        return {"party": [dict(row, uuid="P1")]}

    def check(self, *a, **k):
        return None

    def settle(self):
        pass


def _run(results, rows, n_steps=2):
    it, calls = iter(results), []
    w = _W(rows)

    def call(step, who):
        calls.append((step.short(), w.reads))
        return next(it)
    ex = actq.Executor(call, w, lambda c, j: None, who="P1")
    steps = [actq.Step("interact", {"target": "e56"}), actq.Step("interact", {"target": "e57"})][:n_steps]
    rep = ex.run(lambda: steps.pop(0) if steps else None, lambda: steps[0] if steps else None)
    return rep, calls, w


class ClimbSettle(unittest.TestCase):
    def setUp(self) -> None:
        saved = (actq.CLIMB_SETTLE_S, actq.CLIMB_SETTLE_MAX_S, actq.CLIMB_POLL_S, actq.CLIMB_RETRY_SETTLE_S)
        actq.CLIMB_SETTLE_S, actq.CLIMB_SETTLE_MAX_S, actq.CLIMB_POLL_S, actq.CLIMB_RETRY_SETTLE_S = 0.03, 1.0, 0.01, 0.03
        self.addCleanup(lambda: setattr(actq, "CLIMB_SETTLE_S", saved[0]) or setattr(actq, "CLIMB_SETTLE_MAX_S", saved[1])
                        or setattr(actq, "CLIMB_POLL_S", saved[2]) or setattr(actq, "CLIMB_RETRY_SETTLE_S", saved[3]))

    def test_next_step_waits_until_off_the_ladder_and_still(self):
        top = [-43.0, 7.5, -363.1]
        rows = [{"pos": [-43.0, 6.9, -363.1], "climbing": True}, {"pos": top, "climbing": True},
                {"pos": top, "climbing": True}, {"pos": [-43.0, 7.5, -363.0]}] + [{"pos": [-43.0, 7.5, -363.0]}] * 3
        rep, calls, w = _run([("interact: done — climbed=up", "done", CLIMBED), ("interact: done", "done", None)], rows)
        self.assertEqual(rep.done, 2)
        self.assertIsNone(rep.stopped)
        self.assertGreaterEqual(calls[1][1], 5)  # the step after the climb read the climber off the ladder and still

    def test_no_wait_without_a_climb_or_a_next_step(self):
        rep, calls, w = _run([("interact: done — opened", "done", {"used": True}), ("interact: done", "done", None)],
                             [{"pos": [0.0, 0.0, 0.0]}])
        self.assertEqual((rep.done, w.reads), (2, 0))
        rep, calls, w = _run([("interact: done — climbed=up", "done", CLIMBED)], [{"pos": [0.0, 0.0, 0.0]}], 1)
        self.assertEqual((rep.done, w.reads), (1, 0))

    def test_settle_gives_up_at_the_cap(self):
        actq.CLIMB_SETTLE_MAX_S = 0.05
        rows = [{"pos": [0.0, float(i), 0.0], "climbing": True} for i in range(1000)]
        rep, calls, w = _run([("interact: done — climbed=up", "done", CLIMBED), ("interact: done", "done", None)], rows)
        self.assertEqual(rep.done, 2)

    def test_no_path_right_after_a_climb_is_planned_again_once(self):
        rows = [{"pos": [-43.0, 7.5, -363.1]}]
        rep, calls, w = _run([("interact: done — climbed=up", "done", CLIMBED), (NO_PATH, "failed", None),
                              ("interact: done — climbed=up dy=10", "done", CLIMBED)], rows)
        self.assertEqual(rep.done, 2)
        self.assertIsNone(rep.stopped)
        self.assertEqual([c[0] for c in calls], ["interact e56", "interact e57", "interact e57"])
        # still failing: one retry only, then the queue stops as before
        rep, calls, w = _run([("interact: done — climbed=up", "done", CLIMBED), (NO_PATH, "failed", None),
                              (NO_PATH, "failed", None)], rows)
        self.assertEqual(len(calls), 3)
        self.assertIn("step 2 interact e57", rep.stopped)

    def test_no_retry_without_a_climb_before(self):
        rep, calls, w = _run([("interact: done — opened", "done", {"used": True}), (NO_PATH, "failed", None)],
                             [{"pos": [0.0, 0.0, 0.0]}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(rep.stop_kind, "failed")


if __name__ == "__main__":
    unittest.main()
