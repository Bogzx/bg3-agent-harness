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
        self.lay["mosaic"] = {"rows": ["####   OOO", "   #####OOO#####"], "x1": -807, "z2": -719, "cell": 1}
        out = server.get_layout()
        self.assertIn("MOSAIC on the floor of this room", out)
        self.assertIn("|   #####OOO#####", out)
        # S036: the picture lined up with the room (1 m a char), never the hidden platforms' own positions
        self.assertIn("column 1 = x -807, row 1 = z -719, so a # at column c, row r is about x -807+c-1, z -719-r+1", out)
        self.assertNotIn("LINES", out)

    # The S036 Faith-Leap picture (live, 19:50) and the way the agent crossed it in ~15 min of checked 1-3 m steps.
    FAITH_LEAP = ["          #######     ###OOOOOO",
                  "          #     #     #  OOOOOO   #######",
                  "       ####     #######  OOOOOO   #",
                  "       #              #  OOOOOO   #",
                  " #######              #       #   #",
                  " ##                   #           #",
                  "O##       ###OOOOOO  ######       #",
                  "OO        #  OOOOOO       #       #",
                  "OO   ######  OOOOOO       #########   ###",
                  "OO      #    OOOOOO          ##",
                  "OO      #    OOOOOO####       #",
                  "OO    #####  OOOOOO   #       #     #",
                  "          #           #  OOOOO#     #",
                  "          #######     #  OOOOOO     #",
                  "                #     #  OOOOOO     #",
                  "                #   #####OOOOOO##   #####",
                  "                         OOOOOO"]
    CROSSED = [(-773.4, -720.2), (-773.4, -727.2), (-781.4, -727.2), (-781.4, -725.2), (-785.4, -725.2),
               (-785.4, -721.2), (-791.4, -721.2), (-791.4, -719.2), (-797.4, -719.2), (-797.4, -721.2),
               (-800.4, -721.2), (-800.4, -723.2), (-806.4, -723.2), (-806.4, -725.2)]

    def test_mosaic_path_is_the_crossing(self):
        mo = {"rows": self.FAITH_LEAP, "x1": -807, "z2": -719, "y": -28.5}
        way = render.mosaic_path(mo, [-765.0, -28.3, -720.2])
        corners = [(x, z) for x, z, _ in way]
        self.assertEqual(corners[0], (-767, -720))  # the line's start by the ledge
        for (x, z), (cx, cz) in zip(corners[1:], self.CROSSED):
            self.assertLessEqual(abs(x - cx) + abs(z - cz), 1.0, (corners, self.CROSSED))  # on the line it walked
        self.assertEqual(way[-1][2], "O")  # ends on the far statue platforms
        to_gem = render.mosaic_path(mo, [-765.0, -28.3, -720.2], (-777.5, -733.0))
        self.assertEqual([(x, z) for x, z, _ in to_gem][-3:], [(-777, -727), (-777, -732), (-777, -733)])
        self.assertIsNone(render.mosaic_path(mo, [-740.0, -26.0, -735.0]))  # not at its start: no path

    def test_mosaic_path_in_get_layout(self):
        self.lay["center"] = [-765.0, -28.3, -720.2]
        self.lay["mosaic"] = {"rows": self.FAITH_LEAP, "x1": -807, "z2": -719, "y": -28.5, "cell": 1}
        out = server.get_layout()
        self.assertIn("MOSAIC PATH (the picture's lines from your end to its far end, 17 straight legs", out)
        # 2 m off the line's start: onto it first, then corner to corner
        self.assertIn('walk it: do_actions [{"tool":"move_to","x":-767,"y":-28.5,"z":-720},{"tool":"move_to","x":-773,', out)
        self.assertIn('{"tool":"move_to","x":-807,"y":-27.6,"z":-730}]', out)  # a statue platform: 1 m up
        out = server.get_layout(to="-777.5,-733")
        self.assertIn("to the cell nearest your goal, 6 straight legs", out)

    def test_render_without_nearby(self):
        text = render.render_layout(LAY, server.H)
        self.assertIn("|#..@..#|", text)
        self.assertNotIn("ON IT", text)


if __name__ == "__main__":
    unittest.main()
