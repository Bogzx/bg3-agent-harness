"""Offline tests for get_frontier's WAYS line and named exits (vis, S010-S015: "screenshot to find the way out /
the exits / the hatch" was the most common reason to look at the screen)."""

import unittest

from bg3ai import render
from bg3ai.render import Handles


class Ways(unittest.TestCase):
    def test_open_short_blocked_and_heights(self):
        ways = [{"dir": "N", "open_m": 20}, {"dir": "NE", "open_m": 20, "dy": 6.2}, {"dir": "E", "open_m": 8},
                {"dir": "SE", "open_m": 0}, {"dir": "S", "open_m": 20, "dy": -3}, {"dir": "W", "open_m": 0}]
        self.assertEqual(render.render_ways(ways),
                         "WAYS walkable: N 20m+, NE 20m+ (6.2m up), E 8m, S 20m+ (3m down) | blocked: SE W")

    def test_all_blocked_and_missing(self):
        self.assertEqual(render.render_ways([{"dir": "N", "open_m": 0}]), "WAYS walkable: none | blocked: N")
        self.assertEqual(render.render_ways(None), "")
        self.assertEqual(render.render_ways([]), "")

    def test_frontier_shows_exits_then_ways_then_hazards(self):
        H = Handles()
        res = {"leads": [{"uuid": "u-hatch", "name": "Hatch", "kind": "exit", "dist": 4.2, "dir": "SW", "dy": -3}],
               "ways": [{"dir": "N", "open_m": 20}, {"dir": "S", "open_m": 0}],
               "hazards": [{"surface": "Fire", "dist": 3, "dir": "W", "size": [2, 4]}]}
        lines = render.render_frontier(res, H).splitlines()
        self.assertRegex(lines[0], r"^e\d+ Hatch <exit> 4.2m SW \(3m down\)$")
        self.assertEqual(lines[1], "WAYS walkable: N 20m+ | blocked: S")
        self.assertTrue(lines[2].startswith("HAZARD Fire"))


if __name__ == "__main__":
    unittest.main()
