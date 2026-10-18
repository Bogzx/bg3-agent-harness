"""Offline tests: get_layout (S030 Gauntlet of Shar trials: 11 min and 16 screenshots in the Soft-Step maze and the
Faith-Leap room; no text view of walls, openings or the floor mosaic).

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_layout.py'
"""

from __future__ import annotations

import unittest

from test_turn_macros import P1, P2, Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import render, server  # noqa: E402

DOOR = "dddddddd-0000-0000-0000-000000000001"
SHADOW = "dddddddd-0000-0000-0000-000000000002"
FAR = "dddddddd-0000-0000-0000-000000000003"

LAY = {"n": 3, "cell": 1.5, "center": [0.0, 0.0, 0.0],
       "rows": ["#######",
                "#.....#",
                "#.###.#",
                "#.....#",
                "#.#.#.#",
                "#.....#",
                "###.###"]}


class Layout(Base):
    def setUp(self) -> None:
        super().setUp()
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "nav.layout":
                self.g.calls.append((op, args or {}))
                return dict(self.lay)
            if op == "state.status":
                return {"active": P1, "party": [{"uuid": P1, "name": "Claude", "pos": [0.0, 0.0, 0.0]},
                                                {"uuid": P2, "name": "Lae'zel", "pos": [1.5, 0.0, -1.5]}]}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        self.lay = dict(LAY)
        self.g.nearby = {"characters": [{"uuid": SHADOW, "name": "Shadow", "relation": "hostile", "pos": [-3.0, 0.0, 3.0]},
                                        {"uuid": FAR, "name": "Far Away", "relation": "neutral", "pos": [40.0, 0.0, 0.0]}],
                         "items": [{"uuid": DOOR, "name": "Iron Gate", "kind": "door", "locked": True,
                                    "pos": [0.0, 0.0, -4.5]}]}

    def test_grid_with_what_the_party_sees(self):
        out = server.get_layout(radius=4.5)
        lines = out.splitlines()
        self.assertIn("LAYOUT 4.5m around you (@), N up", lines[0])
        grid = [l for l in lines if l.startswith("|")]
        self.assertEqual(len(grid), 7)
        self.assertEqual(grid[3], "|#..@..#|")       # centre row: you
        self.assertEqual(grid[4][5], "1")              # Lae'zel 1.5 m E, 1.5 m S
        self.assertEqual(grid[1][1:3], "#E")           # the Shadow 3 m W, 3 m N
        self.assertEqual(grid[6], "|###D###|")         # the gate 4.5 m S
        self.assertIn("E ", out)
        self.assertIn("Iron Gate locked", out)
        self.assertNotIn("Far Away", out)            # outside the grid: not in the legend
        self.assertNotIn("MOSAIC", out)
        self.assertEqual(self.g.ops("nav.layout")[0][1]["radius"], 4.5)

    def test_mosaic_block_in_the_trial_room(self):
        self.lay["mosaic"] = {"rows": ["####   OOO", "   #####OOO#####"], "cell": 1}
        out = server.get_layout()
        self.assertIn("MOSAIC on the floor of this room", out)
        self.assertIn("1 char = 1m of the chasm; # line, O statue platform)", out)
        self.assertIn("|   #####OOO#####", out)
        self.assertNotIn("LINES", out)

    def test_mosaic_is_only_the_picture(self):
        # Fairness (user, 2026-10-18): no route along it and nothing tying it to world coordinates, even from an
        # older mod that still sends its anchor and the walkway height
        self.lay["center"] = [-765.0, -28.3, -720.2]
        self.lay["mosaic"] = {"rows": ["          #######", "##########     #######OOO"], "x1": -807, "z2": -719,
                              "y": -28.5, "cell": 1}
        out = server.get_layout()
        self.assertIn("|##########     #######OOO", out)
        self.assertNotIn("MOSAIC PATH", out)
        self.assertNotIn("do_actions", out)
        for coord in ("-807", "-719", "-28.5", "column 1", "row 1"):
            self.assertNotIn(coord, out)

    def test_render_without_nearby(self):
        text = render.render_layout(LAY, server.H)
        self.assertIn("|#..@..#|", text)
        self.assertNotIn("ON IT", text)


if __name__ == "__main__":
    unittest.main()
