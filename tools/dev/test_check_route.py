"""Offline tests for tools/dev/check_route.py's ladder pivot verdict (no game needed).

S085 after-session check_all (05:11:43): `[FAIL] ladder: Ladder 19353fd1: pivot at top (height 5.0m, floors rel.
pivot [-5.3, 0.1]) -> flip leads/far_dy`, with no failed climb in S085 (the party at the Guildhall's upper floor,
y 26.6, over the main hall ~10 m below). The foot convention is verified live (S024/S025 ladder tops, the House of
Healing ladder, S085's own e416: route "climbs 5m down to (-188.4,11.5,875.5)" = its pivot, interact climbed down to
y 12.1). Floors at 0 and -height in the pivot's column read the same for a foot standing on a floor over a lower one
as for a top: the column alone can't tell, so that is a WARN unless a landing at +height is found round the pivot.

Run: uv run --project bridge python -m unittest tools/dev/test_check_route.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_route as C


class LadderPivotTest(unittest.TestCase):
    def test_s085_19353fd1_a_floor_below_the_foot_is_not_a_top(self):
        self.assertEqual(C.classify(5.0, [-5.3, 0.1]), "ambiguous")  # was "top" (FAIL: flip leads/far_dy)
        self.assertEqual(C.classify(5.0, [-5.3, 0.1], [-5.3, 0.0]), "ambiguous")
        self.assertEqual(C.classify(5.0, [-5.3, 0.1], [-5.3, 0.0, 5.1]), "foot")  # the landing behind the ladder

    def test_foot_top_middle_unknown(self):
        self.assertEqual(C.classify(6.5, [0.0, 6.4]), "foot")
        self.assertEqual(C.classify(6.5, [0.1], [0.1, 6.3]), "foot")
        self.assertEqual(C.classify(5.0, [-5.0], [-5.0, -0.2]), "top")  # hanging over its foot's floor
        self.assertEqual(C.classify(6.0, [-3.0, 3.1]), "middle")
        self.assertEqual(C.classify(6.5, [0.9]), "unknown")


class PhasesTest(unittest.TestCase):
    def test_live4_says_where_the_time_went(self):
        r = {"phases": {"probe": 181.4, "lower": 0, "connectors": 22.0, "rest": 1.2, "ways": 64.9, "note": 3.1}}
        self.assertEqual(C.phases(r), "probe=181 lower=0 connectors=22 rest=1 ways=65 note=3")
        self.assertEqual(C.phases({"phases": {"probe": 5.0}}), "probe=5")
        self.assertEqual(C.phases({}), "no phases")  # a mod from before the phases


if __name__ == "__main__":
    unittest.main()
