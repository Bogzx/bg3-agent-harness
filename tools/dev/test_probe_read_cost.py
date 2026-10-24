"""Offline tests for tools/dev/probe_read_cost.py (no game needed): the Lua it sends parses, the summary lines.

Run: uv run pytest -q tools/dev/test_probe_read_cost.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_read_cost as probe


class ProbeTest(unittest.TestCase):
    def test_route_code_fills_char_and_goal(self):
        code = probe.route_code("abc", [1.0, 2.5, -3.0])
        self.assertIn('local char = "abc"', code)
        self.assertIn("local goal = {1.0,2.5,-3.0}", code)
        self.assertIn("local goal = nil", probe.route_code(None, None))

    def test_inventory_summary_names_the_costs(self):
        r = {
            "items": 194,
            "cold": {"min": 55, "median": 58},
            "warm": {"min": 54, "median": 57},
            "no_gc": 50,
            "heap_kb": 812.4,
            "prep_cold": {"median": 29},
            "prep_warm": {"median": 28},
            "pieces_items": 120,
            "pieces": {"name": 21.5, "stack": 3.0, "stats_get_cold": 4.2},
            "camp_chest": {"supplies": 80, "ms": 9.1},
            "gold_ms": 0.1,
        }
        lines = probe.summarize("inventory", r)
        self.assertIn("memo emptied min 55 median 58 ms; filled min 54 median 57 ms", lines[0])
        self.assertIn("heap +812 KB", lines[0])
        self.assertTrue(lines[2].endswith("name 21.5, stats_get_cold 4.2, stack 3.0"), lines[2])  # biggest first
        self.assertIn("camp chest supplies 80: 9.1 ms", lines[3])

    def test_route_summary_and_error(self):
        r = {
            "goal": [1, 2, 3],
            "straight_m": 133.2,
            "runs": [{"ms": 274.0, "phases": {"connectors": 200}}],
            "steps": {"known_ms": 3.0},
        }
        lines = probe.summarize("route", r)
        self.assertIn("(133 m): 274 ms", lines[0])
        self.assertEqual(lines[1], "steps: known_ms 3.0")
        self.assertEqual(probe.summarize("route", {"error": "no goal"}), ["route: ERROR no goal"])


if __name__ == "__main__":
    unittest.main()
