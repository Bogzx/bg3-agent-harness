"""acceptance.py target pickers for items that SKIPped at S027-start (live window 20261016-183253).

    python3 -m unittest tools/dev/test_acceptance_targets.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance as A  # noqa: E402

INN_EXIT = [-54.2, 27.5, 141.8]  # S027-start after camp leave (window 20261016-183253 nav log)
# that window's get_frontier at the inn exit and its look/get_frontier at camp
INN_FRONTIER = "e1 Oak Door <door> 10.8m NW\ne2 Double Oak Doors <door> 17.4m NW (4.1m up)\nWAYS walkable: N 8m"
CAMP_FRONTIER = "e18 Magic Mirror <usable> 16.8m NW unvisited\nWAYS walkable: N 8m, S 20m+"
CAMP_LOOK = ("CHAR e7 Withers neutral HP 1/1 1.2m\nITEM e11 Wooden Crate <item> 7.8m\n"
             "ITEM e12 Traveller's Chest <usable> 12.8m\nITEM e18 Magic Mirror <usable> 16.8m")


class TestFarWalk(unittest.TestCase):
    def test_known_spot_at_the_inn(self):
        q = A.far_walk_target(INN_EXIT, [{"kind": "door", "pos": [-60, 27, 135]}], "Shadow-Cursed Lands")
        self.assertEqual(q, (-155.7, 31.5, 50.3))
        self.assertTrue(40 < ((q[0] - INN_EXIT[0]) ** 2 + (q[2] - INN_EXIT[2]) ** 2) ** 0.5 < 160)

    def test_leads_elsewhere_doors_first(self):
        leads = [{"kind": "usable", "pos": [100, 0, 0]}, {"kind": "door", "pos": [0, 0, 60]},
                 {"kind": "door", "pos": [0, 0, 300]}, {"kind": "exit", "pos": [0, 0, 10]}, {"kind": "door"}]
        self.assertEqual(A.far_walk_target([0, 0, 0], leads, "Rivington"), (0.0, 0.0, 60.0))
        self.assertEqual(A.far_walk_target([0, 0, 0], leads[:1], "Rivington"), (100.0, 0.0, 0.0))

    def test_none(self):
        self.assertIsNone(A.far_walk_target(INN_EXIT, [{"kind": "door", "pos": [-60, 27, 135]}], "Rivington"))
        self.assertIsNone(A.far_walk_target([-150, 31, 50], None, "Shadow-Cursed Lands"))  # standing on the spot


class TestStaticLeads(unittest.TestCase):
    def test_doors_first_then_usables(self):
        self.assertEqual(A.static_lead_ids(INN_FRONTIER), ["e1", "e2"])
        self.assertEqual(A.static_lead_ids("e3 Crate <container> 4m\n" + CAMP_FRONTIER), ["e3", "e18"])

    def test_camp_base_save_finds_one(self):
        self.assertEqual(A.static_lead_ids(CAMP_FRONTIER), ["e18"])
        self.assertEqual(A.static_lead_ids(CAMP_LOOK), ["e12", "e18"])  # <item> rows are not leads
        self.assertEqual(A.static_lead_ids(""), [])


if __name__ == "__main__":
    unittest.main()
