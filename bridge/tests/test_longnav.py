"""Long trips (claude-run-2 S010/S011, Karlach 90-150 m NW of the Emerald Grove): route on a far goal, the
fast-travel shortcut, and dead ends remembered per session ("tried 2x: blocked").

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from test_turn_macros import P1, Base

from bg3ai import longnav, render, route, server
from bg3ai.game import GameError, JobResult

KARLACH = [104.2, 21.1, 516.7]
ROCK = "11111111-2222-3333-4444-000000000044"

FAR_NO_PATH = {  # S011 01:20 from (186.8,24.2,422.3): route said only "too far to preview the walk"
    "from": [186.8, 24.2, 422.3], "goal": KARLACH, "straight_m": 125.0, "dir": "NW", "dy": -3.1,
    "far": True, "reachable": False,
    "closest": {"pos": [170.0, 25.0, 440.0], "path_m": 26.0, "to_goal_m": 100.0, "dy_to_goal": -3.9, "via": "toward"},
    "visited_near": {"pos": [102.0, 22.0, 482.0], "to_goal_m": 34.8},
    "waypoints": [{"name": "Emerald Grove Environs", "id": "WAYP_DEN_Camp", "pos": [246.5, 25.4, 423.2],
                   "to_goal_m": 170.3, "dist": 59.7},
                  {"name": "Roadside Cliffs", "id": "WAYP_CHA_Top", "pos": [223.6, 16.9, 327.5],
                   "to_goal_m": 223.7, "dist": 103.0}],
    "connectors": [{"uuid": ROCK, "name": "Cragged Rock", "kind": "ladder", "pos": [101.0, 12.0, 478.0],
                    "dist": 12.0, "dir": "N", "dy": 0, "height_m": 10.0, "leads": "up", "path_m": 12.5,
                    "to_goal_m": 47.0, "helps": True}],
}


def clock_seq(*ts):
    it = iter(ts)
    return lambda: next(it)


class Pure(unittest.TestCase):
    WPS = [{"name": "Grove", "pos": [250.0, 25.0, 420.0]}, {"name": "Road", "pos": [82.0, 33.0, 597.0]}]

    def test_pick_waypoint_only_for_long_trips_with_a_big_saving(self):
        far = [400.0, 25.0, 400.0]
        wp = longnav.pick_waypoint(far, KARLACH, self.WPS)
        self.assertEqual(wp["name"], "Road")  # 84 m from her, we are 318 m away
        self.assertLess(wp["to_goal_m"], 90)
        self.assertIsNone(longnav.pick_waypoint([150.0, 25.0, 480.0], KARLACH, self.WPS))  # 60 m: walk
        self.assertIsNone(longnav.pick_waypoint(far, KARLACH, [{"name": "Grove", "pos": [300.0, 25.0, 450.0]}]))  # 207 of 318 m
        self.assertIsNone(longnav.pick_waypoint(far, KARLACH, None))
        # standing at the waypoint already: no fast travel to it
        self.assertIsNone(longnav.pick_waypoint([85.0, 33.0, 600.0], [85.0, 33.0, 900.0], self.WPS))

    def test_ground_stop(self):
        self.assertTrue(longnav.ground_stop("no walkable ground leads further toward it (68.5m left): try ..."))
        self.assertTrue(longnav.ground_stop("made no progress 2 legs in a row (8.3m left): the way is blocked here"))
        self.assertTrue(longnav.ground_stop("leg 5 failed", ["leg 5: move_to: FAILED — could not get closer — path blocked"]))
        self.assertFalse(longnav.ground_stop("leg 2 failed", ["leg 2: move_to: INTERRUPTED — cancelled"]))
        self.assertFalse(longnav.ground_stop("a conversation started"))
        self.assertFalse(longnav.ground_stop("the way on crosses Fire 3m ahead: no way around"))
        self.assertFalse(longnav.ground_stop(None))

    def test_dead_ends_count_per_spot_and_goal(self):
        d = longnav.DeadEnds(clock_seq("01:11", "01:15", "01:17", "01:18"))
        d.record(KARLACH, [101.8, 22.2, 481.3], 35.6)
        e = d.record(KARLACH, [103.0, 22.0, 484.0], 33.0)  # same boulder wall, 3 m off
        self.assertEqual(e.count, 2)
        self.assertEqual(longnav.dead_end_text(e), "blocked 2x at (101.8,22.2,481.3) 33m short (01:11, 01:15)")
        d.record(KARLACH, [119.3, 22.1, 449.8], 68.5)  # another spot
        d.record([500.0, 0.0, 0.0], [101.8, 22.2, 481.3])  # another goal
        ends = d.for_goal([104.0, 21.0, 517.0])
        self.assertEqual([x.count for x in ends], [2, 1])
        self.assertIs(d.near(KARLACH, [102.0, 22.0, 482.0]), e)
        self.assertEqual(longnav.stop_note(ends[1], None), "")  # once is no pattern yet
        note = longnav.stop_note(e, None, "Karlach")
        self.assertIn("tried 2x: blocked 2x at (101.8,22.2,481.3)", note)
        self.assertIn("Don't try this approach again", note)
        self.assertIn('travel_to waypoint="Road"', longnav.stop_note(e, {"name": "Road", "to_goal_m": 84.8}))

    def test_a_ladder_to_a_dead_end(self):
        d = longnav.DeadEnds(clock_seq("01:11"))
        d.record(KARLACH, [101.8, 22.2, 480.8])  # S010: the top of the Cragged Rock
        c = FAR_NO_PATH["connectors"][0]
        self.assertIsNotNone(longnav.connector_dead(c, d.for_goal(KARLACH)))
        self.assertIsNone(longnav.connector_dead({**c, "leads": "down"}, d.for_goal(KARLACH)))


class RouteText(unittest.TestCase):
    def test_far_goal_without_path_says_so_with_the_closest_spot_and_waypoints(self):
        out = route.render_route(FAR_NO_PATH, render.Handles(), "objective Karlach")
        self.assertNotIn("too far to preview", out)
        self.assertIn("NO WALKABLE PATH from here: the engine finds no way there from this side", out)
        self.assertIn("CLOSEST YOU HAVE BEEN: (102,22,482), 34.8m from it", out)
        self.assertIn("WAYPOINTS (unlocked, nearest the goal first): Emerald Grove Environs 170.3m from it (you: 59.7m)", out)
        self.assertIn("NEXT: interact", out)  # the rock is no known dead end yet

    def test_after_two_stops_the_side_is_closed_and_the_rock_is_flagged(self):
        d = longnav.DeadEnds(clock_seq("01:11", "01:15"))
        d.record(KARLACH, [101.8, 22.2, 481.3], 35.6)
        d.record(KARLACH, [101.0, 22.0, 479.0], 35.0)
        out = route.render_route(FAR_NO_PATH, render.Handles(), "objective Karlach", d.for_goal(KARLACH))
        self.assertIn("BLOCKED BEFORE (this session): blocked 2x at (101.8,22.2,481.3)", out)
        self.assertIn("Cragged Rock <ladder>", out)
        self.assertIn("LEADS TO A DEAD END", out)
        self.assertIn("NEXT: this side is closed", out)
        self.assertNotIn("NEXT: interact", out)

    def test_a_waypoint_much_nearer_the_goal_is_the_next_step(self):
        res = {**FAR_NO_PATH, "waypoints": [{"name": "The Risen Road", "pos": [82.0, 33.3, 597.6], "to_goal_m": 84.8,
                                             "dist": 200.0}]}
        out = route.render_route(res, render.Handles(), "objective Karlach")
        self.assertIn('NEXT: travel_to waypoint="The Risen Road" (84.8m from the goal, you are 125m), then route again', out)

    def test_far_path_found_is_a_path(self):
        res = {"from": [0, 0, 0], "goal": [150, 0, 0], "straight_m": 150.0, "dir": "E", "dy": 0, "far": True,
               "reachable": True, "length_m": 310.0, "legs": [{"to": [150, 0, 0], "at_m": 310, "m": 310, "dir": "E"}]}
        out = route.render_route(res, render.Handles(), "it")
        self.assertIn("PATH 310m (a long way round: 2.1x the straight line)", out)
        self.assertIn("NEXT: travel_to/move_to walks it", out)


class TravelTrips(Base):
    def setUp(self) -> None:
        super().setUp()
        server.SETTLE_S = 10.0
        server.DEAD.clear()
        self.addCleanup(server.DEAD.clear)
        self.wps: list | None = None
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "nav.waypoints":
                self.g.calls.append((op, args or {}))
                if self.wps is None:
                    raise GameError("unknown op nav.waypoints")
                return {"waypoints": self.wps}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc

    def test_the_second_stop_at_the_same_wall_says_tried_twice(self):
        self.g.reach = {"reachable": False}
        first = server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertIn("no walkable ground leads further", first)
        self.assertNotIn("tried", first)
        second = server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertIn("tried 2x: blocked 2x at (0,0,0) 60m short", second)
        self.assertIn("Don't try this approach again", second)
        third = server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertIn("KNOWN DEAD END toward it: blocked 2x", third)
        self.assertIn("tried 3x", third)

    def test_a_cancelled_trip_is_no_dead_end(self):
        self.g.reach = {"reachable": False, "hazard": {"surface": "Fire", "at_m": 3}}
        server.travel_to(x=60.0, y=0.0, z=0.0)
        server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertEqual(server.DEAD.items, [])

    def test_long_trip_fast_travels_first_to_a_waypoint_near_the_goal(self):
        # S011: travel_to Zevlor 463 m on foot with an unlocked waypoint next to him
        self.wps = [{"name": "Emerald Grove Environs", "id": "WAYP_DEN_Camp", "pos": [440.0, 0.0, 10.0]},
                    {"name": "Roadside Cliffs", "id": "WAYP_CHA_Top", "pos": [-50.0, 0.0, 0.0]}]
        travelled = []
        orig_job = self.g.run_job

        def run_job(op, args, context="server", wait_s=25.0, poll_s=0.01, on_start=None):
            if op == "ui.fast_travel":
                travelled.append(args["waypoint"])
                self.g.watch["party"][0]["pos"] = [440.0, 0.0, 10.0]
                return JobResult("done", {"arrived": args["waypoint"]})
            return orig_job(op, args, context, wait_s, poll_s, on_start)
        self.g.run_job = run_job
        self.g.reach = {"reachable": False}
        out = server.travel_to(x=463.0, y=0.0, z=0.0)
        self.assertEqual(travelled, ["Emerald Grove Environs"])
        self.assertIn("fast travel to Emerald Grove Environs first (25.1m from it; you were 463m away): done", out)
        self.assertIn("travel_to (463,0,0): started (25.1m)", out)

    def test_fast_never_walks_and_short_trips_never_ask(self):
        self.wps = [{"name": "Grove", "pos": [440.0, 0.0, 10.0]}]
        self.g.reach = {"reachable": False}
        server.travel_to(x=463.0, y=0.0, z=0.0, fast="never")
        server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertEqual(self.g.ops("nav.waypoints"), [])
        self.assertEqual(self.g.ops("ui.fast_travel"), [])

    def test_failed_fast_travel_walks_instead(self):
        self.wps = [{"name": "Grove", "pos": [440.0, 0.0, 10.0]}]
        self.g.run_job = lambda op, args, **kw: JobResult("failed", None, "in dialogue")
        self.g.reach = {"reachable": False}
        out = server.travel_to(x=463.0, y=0.0, z=0.0)
        self.assertIn("fast travel to Grove failed (in dialogue): walking instead", out)

    def test_route_tool_shows_this_sessions_dead_ends(self):
        server.DEAD.record([60.0, 0.0, 0.0], [10.0, 0.0, 0.0], 50.0)
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "nav.route":
                return {"from": [0, 0, 0], "goal": [60.0, 0.0, 0.0], "straight_m": 60.0, "dir": "E", "dy": 0,
                        "reachable": False}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.route(x=60.0, y=0.0, z=0.0)
        self.assertIn("BLOCKED BEFORE (this session): blocked 1x at (10,0,0) 50m short", out)


if __name__ == "__main__":
    unittest.main()
