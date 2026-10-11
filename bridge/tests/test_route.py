"""route tool (nav.route) and the walk_toward failure hint: the way to a goal as text, so the agent doesn't take a
screenshot and click its way through (claude-run-2 S001: 23 screenshots and 13 clicks in 14 min at the fire maze and
the ladder; S002: path_preview 'error_cause=3' and a screenshot to find the way up to the Helm)."""
from __future__ import annotations

import unittest

from test_turn_macros import Base, P1  # noqa: F401  (sets BG3AI_RUNTIME first)

from bg3ai import render, route, server  # noqa: E402

LADDER = "11111111-2222-3333-4444-000000000070"
DOOR = "11111111-2222-3333-4444-000000000040"
HELM = [-31.8, 18.2, -354.8]

UP_THERE = {  # S002: the Helm is on the deck above; the Arterial Mesh ladder climbs there
    "from": [-41.0, 9.0, -361.0], "goal": HELM, "straight_m": 17.8, "dir": "NE", "dy": 9.2,
    "levels_here": [0.0, 9.3], "levels_goal": [0.1, 9.2], "reachable": False,
    "closest": {"pos": [-33.0, 9.0, -356.0], "path_m": 9.5, "to_goal_m": 9.3, "dy_to_goal": 9.2, "via": "toward",
                "legs": [{"to": [-33.0, 9.0, -356.0], "at_m": 9.5, "m": 9.5, "dir": "NE", "dy": 0}]},
    "connectors": [
        {"uuid": LADDER, "name": "Arterial Mesh", "kind": "ladder", "pos": [-40.0, 9.0, -359.0], "dist": 2.5,
         "dir": "N", "dy": 0, "height_m": 9.4, "leads": "up", "far_dy": 9.4, "path_m": 2.7, "to_goal_m": 9.1,
         "helps": True},
        {"uuid": DOOR, "name": "Sphincter", "kind": "door", "pos": [-28.0, 9.0, -362.0], "dist": 13.0, "dir": "E",
         "dy": 0, "locked": True, "path_m": 14.0, "to_goal_m": 10.0, "helps": True},
    ],
}

WITH_CLIMB = {
    "goal": HELM, "straight_m": 12.0, "dir": "NE", "dy": 9.2, "levels_here": [0.0, 9.3], "levels_goal": [0.0],
    "reachable": True, "length_m": 16.4, "needs_jump": True,
    "legs": [{"to": [-38.0, 9.0, -358.0], "at_m": 4.0, "m": 4.0, "dir": "N", "dy": 0},
             {"to": [-38.0, 18.2, -357.5], "at_m": 13.3, "m": 9.3, "dir": "N", "dy": 9.2, "link": "climb"},
             {"to": HELM, "at_m": 16.4, "m": 3.1, "dir": "E", "dy": 0}],
    "links": [{"at_m": 4.0, "from": [-38.0, 9.0, -358.0], "to": [-38.0, 18.2, -357.5], "dy": 9.2, "m": 0.5,
               "kind": "climb"}],
}

FIRE_MAZE = {  # S001: the corridor west is burning; no fire-free path to the escape route
    "goal": [-40.0, 16.0, -431.0], "straight_m": 24.0, "dir": "W", "dy": -1.6, "levels_here": [0.0],
    "levels_goal": [-1.6], "reachable": False,
    "straight_hazard": {"m": 3.0, "surface": "Fire", "patches": 1, "longest_m": 3.0, "pos": [-26.3, 16.2, -431.0],
                        "at_m": 6.0},
    "closest": {"pos": [-24.0, 18.0, -428.0], "path_m": 5.0, "to_goal_m": 16.2, "dy_to_goal": -1.6, "via": "toward"},
    "connectors": [],
}


class RouteTool(Base):
    def setUp(self) -> None:
        super().setUp()
        self.reply: dict = UP_THERE
        self.wedged: dict = {"wedged": False}
        self.markers = [{"label": "Helm", "quest": "Escape the Nautiloid", "pos": HELM, "here": True,
                         "dist": 17.8, "dir": "NE"}]
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "nav.route":
                self.g.calls.append((op, args or {}))
                return dict(self.reply)
            if op == "nav.markers":
                return {"markers": self.markers}
            if op == "nav.wedged":
                self.g.calls.append((op, args or {}))
                return dict(self.wedged)
            return orig(op, args, context, timeout)
        self.g.rpc = rpc

    def test_go_travels_to_the_goal_or_the_closest_spot(self):
        """S023-S026: route then travel_to the same goal (17x) or its closest walkable spot (12x)."""
        seen = []
        orig = server.travel_to
        server.travel_to = lambda **kw: seen.append(kw) or "travel_to: started"
        self.addCleanup(setattr, server, "travel_to", orig)
        self.reply = WITH_CLIMB
        out = server.route(x=HELM[0], y=HELM[1], z=HELM[2], go=True)
        self.assertIn("GO travel_to: started", out)
        self.assertEqual(seen[-1], {"who": None, "x": HELM[0], "y": HELM[1], "z": HELM[2]})
        self.reply = UP_THERE
        server.route(x=HELM[0], y=HELM[1], z=HELM[2], go=True)
        self.assertEqual(seen[-1], {"who": None, "x": -33.0, "y": 9.0, "z": -356.0})
        self.reply = dict(UP_THERE, closest=None)
        self.assertIn("go: not started", server.route(x=1.0, y=2.0, z=3.0, go=True))
        server.route(x=1.0, y=2.0, z=3.0)
        self.assertEqual(len(seen), 2)

    def test_no_path_lists_the_open_directions(self):
        """S023 06:23: 8 guessed route calls across a chasm; the way on (a stone bridge) lay SW."""
        self.reply = dict(FIRE_MAZE, ways=[{"dir": "SW", "open_m": 20}, {"dir": "N", "open_m": 8, "dy": 3.1},
                                           {"dir": "E", "open_m": 0}])
        out = server.route(x=-40.0, y=16.0, z=-431.0)
        self.assertIn("NO WALKABLE PATH", out)
        self.assertIn("WAYS walkable: SW 20m+, N 8m (3.1m up) | blocked: E", out)

    def test_goal_above_names_the_ladder_up(self):
        out = server.route(objective="o1")
        args = self.g.ops("nav.route")[0][1]
        self.assertEqual(args["pos"], HELM)
        self.assertNotIn("char", args)  # the mod defaults to the active character: no extra state.status RPC
        self.assertIn("ROUTE to objective Helm: 17.8m NE, 9.2m above you", out)
        self.assertIn("FLOORS here: 0 (you), +9.3 · at the goal: +0.1, +9.2 (m, relative to you)", out)
        self.assertIn("NO WALKABLE PATH", out)
        self.assertIn("CLOSEST walkable spot (-33,9,-356): 9.3m from the goal, 9.2m below it", out)
        lad = server.H.get(LADDER, "Arterial Mesh")
        self.assertIn(f"{lad} Arterial Mesh <ladder> 2.5m N climbs 9.4m up [interact to climb] walk 2.7m", out)
        self.assertIn("LOCKED", out)
        self.assertIn(f"NEXT: interact {lad} (ladder up), then route again", out)

    def test_objective_by_words_and_target_id(self):
        server.route(objective="helm")
        self.assertEqual(self.g.ops("nav.route")[-1][1]["pos"], HELM)
        door = server.H.get(DOOR, "Sphincter")
        server.route(target=door)
        self.assertEqual(self.g.ops("nav.route")[-1][1]["target"], DOOR)
        server.route(x=1.0, y=2.0, z=3.0, connectors=True)
        self.assertEqual(self.g.ops("nav.route")[-1][1], {"pos": [1.0, 2.0, 3.0], "connectors": True})

    def test_path_with_a_climb_lists_legs_and_the_link(self):
        self.reply = WITH_CLIMB
        out = server.route(objective="o1")
        self.assertIn("PATH 16.4m, with a climb (9.2m up) at 4m", out)
        self.assertIn("  1. 4m N", out)
        self.assertIn("  2. CLIMB 9.2m up to (-38,18.2,-357.5)", out)
        self.assertIn("  3. 3.1m E", out)
        self.assertIn("jump_to (-38,18.2,-357.5)", out)

    def test_fire_maze_points_to_the_closest_spot_and_crossing(self):
        self.reply = FIRE_MAZE
        out = server.route(x=-40.0, y=16.0, z=-431.0)
        self.assertIn("NO WALKABLE PATH (the straight line crosses 3m of Fire (~1-4 dmg + Burning) at 6m)", out)
        self.assertIn("NEXT: travel_to x,y,z = (-24,18,-428) (the closest walkable spot); the rest crosses 3m of Fire", out)

    def test_no_goal_is_a_readable_failure(self):
        self.assertTrue(server.route().startswith("FAILED: give objective=o1"))

    def test_walk_toward_objective_failure_says_the_way(self):
        self.g.reach = {"reachable": False, "samples": 6}
        out = server.walk_toward(objective="o1")
        self.assertIn("FAILED: no walkable ground toward objective Helm within 30m (6 spots checked)", out)
        self.assertIn("ROUTE: the goal is 9.2m above you; closest walkable spot (-33,9,-356) is 9.3m from it", out)
        self.assertIn("NEXT: interact", out)
        self.assertNotIn("Try another direction", out)

    def test_walk_toward_direction_failure_keeps_the_old_advice(self):
        self.g.reach = {"reachable": False, "samples": 1}
        out = server.walk_toward(direction="SE")
        self.assertIn("Try another direction, get_frontier, or a door.", out)
        self.assertEqual(self.g.ops("nav.route"), [])


    def test_path_with_a_drop_says_walks_stop_there_and_go_walks_to_the_edge(self):
        """S053 05:49: route said PATH 170.8m; travel_to's first leg: 'the way there drops 8.4m (fall damage)'."""
        seen = []
        orig = server.travel_to
        server.travel_to = lambda **kw: seen.append(kw) or "travel_to: started"
        self.addCleanup(setattr, server, "travel_to", orig)
        self.reply = dict(WITH_CLIMB, drop={"pos": [-227.6, 13.5, -114.0], "m": 8.4})
        out = server.route(x=HELM[0], y=HELM[1], z=HELM[2], go=True)
        self.assertIn("  DROPS 8.4m at (-227.6,13.5,-114) (fall damage): walks stop there", out)
        self.assertIn("NEXT: travel_to x,y,z = (-227.6,13.5,-114) (the edge of the 8.4m drop), then jump_to", out)
        self.assertEqual(seen[-1], {"who": None, "x": -227.6, "y": 13.5, "z": -114.0})

    def test_wedged_route_says_jump_out_next_to_a_member(self):
        """S058 08:00: Claude stuck in a nook after a pick_up: WAYS none, no closest spot; 6 calls to guess a jump."""
        self.reply = dict(FIRE_MAZE, closest=None, ways=[{"dir": d, "open_m": 0} for d in ("N", "E", "S", "W")],
                          wedged={"wedged": True, "to": [21.0, 46.4, 228.5], "near": "Wyll", "dist_m": 3.1})
        out = server.route(x=-40.0, y=16.0, z=-431.0)
        self.assertIn("WEDGED: no walkable ground leads out from where you stand: jump_to x,y,z = (21,46.4,228.5) "
                      "next to Wyll (3.1m), then try again", out)
        self.assertIn("NEXT: jump_to x,y,z = (21,46.4,228.5) (out of the nook), then route again", out)
        self.assertIn("you are wedged", route.hint_text(self.reply, server.H))

    def test_walk_toward_direction_failure_when_wedged(self):
        self.g.reach = {"reachable": False, "samples": 3}
        self.wedged = {"wedged": True, "to": [21.0, 46.4, 228.5], "near": "Wyll", "dist_m": 3.1, "in_reach": False}
        out = server.walk_toward(direction="N", max_m=6)
        self.assertIn("FAILED: no walkable ground toward N within 6m (3 spots checked)", out)
        self.assertIn("you are wedged: no walkable ground leads out from where you stand: jump_to x,y,z = "
                      "(21,46.4,228.5) next to Wyll (3.1m) if it is in reach, then try again", out)
        self.assertNotIn("Try another direction", out)


class Rendering(unittest.TestCase):
    def test_hint_is_empty_when_reachable(self):
        self.assertEqual(route.hint_text(WITH_CLIMB, render.Handles()), "")

    def test_unprobed_connector_says_so(self):
        # nav.route's connector probes have a budget: past it a ladder/door is listed without a path probe
        line = route._connector({"uuid": LADDER, "name": "Ladder", "kind": "ladder", "dist": 13.0, "dir": "W",
                                 "unprobed": True}, render.Handles())
        self.assertIn("path not checked", line)
        self.assertNotIn("no walkable path", line)

    def test_frontier_shows_ladder_way_and_levels(self):
        H = render.Handles()
        res = {"leads": [{"uuid": LADDER, "name": "Arterial Mesh", "kind": "ladder", "dist": 2.5, "dir": "N",
                          "height_m": 9.4, "leads": "up"},
                         {"uuid": DOOR, "name": "Sphincter", "kind": "door", "dist": 8.0, "dir": "E", "dy": 9.1}]}
        out = render.render_frontier(res, H)
        self.assertIn("<ladder> 2.5m N climbs 9.4m up", out)
        self.assertIn("<door> 8m E (9.1m up)", out)


if __name__ == "__main__":
    unittest.main()
