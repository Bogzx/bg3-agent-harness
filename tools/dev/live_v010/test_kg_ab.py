"""Offline tests of kg_ab.py's pure parts (no game: nothing here connects)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("V010_OUT", tempfile.mkdtemp(prefix="v010-test-"))  # common.py makes OUT on import
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kg_ab


class CleanStart(unittest.TestCase):
    def test_every_member_back_exactly_is_a_clean_start(self):
        # bp-live K1 2026-11-03: worst_m 0 read as "no value" (or 99) and refused the perfect start
        self.assertTrue(kg_ab.clean_start({"worst_m": 0, "in_combat": False, "in_dialogue": False}))
        self.assertTrue(kg_ab.clean_start({"worst_m": 0.0}))
        self.assertTrue(kg_ab.clean_start({"worst_m": 3}))

    def test_not_clean(self):
        self.assertFalse(kg_ab.clean_start({"worst_m": 3.1}))
        self.assertFalse(kg_ab.clean_start({"worst_m": 0, "in_combat": True}))
        self.assertFalse(kg_ab.clean_start({"worst_m": 0, "in_dialogue": True}))
        self.assertFalse(kg_ab.clean_start({}))  # the read failed
        self.assertFalse(kg_ab.clean_start({"error": "EXCEPTION ...", "worst_m": None}))


class Parse(unittest.TestCase):
    def test_stop_reason_and_metres_left(self):
        r = kg_ab.parse(
            "travel_to (120,10,330): 43.9m | leg 1: along the route (30m): move_to: done — | "
            "STOPPED: no walkable ground leads further toward it (43.9m left)"
        )
        self.assertFalse(r["arrived"])
        self.assertEqual(r["left_m"], 43.9)
        self.assertEqual((r["legs"], r["route_legs"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
