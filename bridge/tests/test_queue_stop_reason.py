"""Queue headline names a macro step's own STOPPED reason (live window #2: "STOPPED: step 1 travel_to … interrupted"
on a ground stop, the reason 4 lines further down)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bg3ai import actq, macros  # noqa: E402

TRIP = ("travel_to (-155.7,31.5,50.3): 136.7m\nleg 1: move_to: done — dist=0.2 @(-67.6,28.9,129.8)\n"
        "STOPPED: no walkable ground leads further toward it (105.6m left): try get_frontier, a door, or a jump; "
        "it is 3m above you: find a ladder or stairs (get_frontier) or jump_to")


class _W:
    last: dict = {}

    def check(self, *a, **k):
        return None

    def settle(self):
        pass


def _run(results):
    it = iter(results)
    ex = actq.Executor(lambda step, who: next(it), _W(), lambda c, j: None, who="P1")
    steps = [actq.Step("travel_to", {"x": -155.7, "y": 31.5, "z": 50.3}), actq.Step("interact", {"target": "e3"})]
    return ex.run(lambda: steps.pop(0) if steps else None, lambda: steps[0] if steps else None)


class TestStopReason(unittest.TestCase):
    def test_first_clause(self):
        self.assertEqual(actq.stopped_reason(TRIP), "no walkable ground leads further toward it (105.6m left)")
        self.assertEqual(actq.stopped_reason("leg 1: move_to: done"), "")
        self.assertTrue(actq.stopped_reason("STOPPED: " + "x" * 300).endswith("…"))

    def test_headline_carries_the_reason(self):
        rep = _run([(TRIP, "interrupted", None)])
        self.assertEqual(rep.stop_kind, "failed")
        self.assertIn(": no walkable ground leads further toward it (105.6m left)", rep.stopped)
        self.assertNotIn("interrupted", rep.stopped)
        self.assertEqual(rep.left, ["interact e3"])
        self.assertFalse(macros.leg_retryable(rep.stop_kind, rep.stopped, TRIP))

    def test_failed_and_bare_interrupts_unchanged(self):
        rep = _run([("move_to: FAILED — no path\nSTOPPED: whatever", "failed", None)])
        self.assertTrue(rep.stopped.endswith(" failed"))
        rep = _run([("move_to: INTERRUPTED — cancelled", "interrupted", None)])
        self.assertTrue(rep.stopped.endswith(" interrupted"))


if __name__ == "__main__":
    unittest.main()
