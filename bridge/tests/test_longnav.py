"""Long trips (claude-run-2 S010/S011, Karlach 90-150 m NW of the Emerald Grove): route on a far goal, the
fast-travel shortcut, and dead ends remembered per session ("tried 2x: blocked").

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_dead_ends_apply_only_on_their_level(self):
        # NAV-10: coordinates of another map mean nothing here
        lv = ["WLD_Main_A"]
        d = longnav.DeadEnds(clock_seq("01:11", "01:15", "01:20"), level=lambda: lv[0], now=lambda: 0.0)
        d.record(KARLACH, [101.8, 22.2, 481.3], 35.6)
        self.assertEqual(d.items[0].level, "WLD_Main_A")
        self.assertEqual(len(d.for_goal(KARLACH)), 1)  # same level: applies
        lv[0] = "SCL_Main_A"
        self.assertEqual(d.for_goal(KARLACH), [])  # another level: ignored
        self.assertIsNone(d.near(KARLACH, [101.8, 22.2, 481.3]))
        e = d.record(KARLACH, [101.8, 22.2, 481.3])  # the same coordinates there are a new spot
        self.assertEqual((e.count, e.level, len(d.items)), (1, "SCL_Main_A", 2))
        lv[0] = "WLD_Main_A"
        self.assertEqual([x.level for x in d.for_goal(KARLACH)], ["WLD_Main_A"])  # back: the old one applies again

    def test_unknown_level_falls_back_to_matching(self):
        lv: list = [None]
        d = longnav.DeadEnds(clock_seq("01:11"), level=lambda: lv[0], now=lambda: 0.0)
        d.record(KARLACH, [101.8, 22.2, 481.3])
        lv[0] = "WLD_Main_A"
        self.assertEqual(len(d.for_goal(KARLACH)), 1)

        def boom():
            raise GameError("loading")
        d.level = boom  # a failed level read (loading) neither fails nor hides anything
        self.assertEqual(len(d.for_goal(KARLACH)), 1)

    def test_dead_ends_expire_after_their_last_hit(self):
        t = [0.0]
        d = longnav.DeadEnds(clock_seq("01:11", "01:25", "02:00"), level=lambda: "WLD_Main_A", now=lambda: t[0])
        d.record(KARLACH, [101.8, 22.2, 481.3])
        t[0] = longnav.DEAD_TTL_S - 1
        self.assertEqual(d.record(KARLACH, [101.8, 22.2, 481.3]).count, 2)  # a hit refreshes it
        t[0] = 2 * longnav.DEAD_TTL_S - 2
        self.assertEqual(len(d.for_goal(KARLACH)), 1)
        t[0] = 2 * longnav.DEAD_TTL_S
        self.assertEqual(d.for_goal(KARLACH), [])  # the way may have opened since
        self.assertEqual(d.items, [])
        self.assertEqual(d.record(KARLACH, [101.8, 22.2, 481.3]).count, 1)  # starts over


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


class RouteJump(unittest.TestCase):
    def test_no_path_names_the_jump_across(self):
        """S032 17:30-17:35 Yurgir: the broken staircase is a 3 m jump; route said only NO WALKABLE PATH."""
        res = {"from": [-698, 8.1, -799], "goal": [-688, -6, -795], "straight_m": 19.2, "dir": "E", "dy": -14.1,
               "reachable": False,
               "jump": {"from": [-689, 1.3, -786], "to": [-686, -1.2, -786], "gap_m": 3, "dy": -2.5, "walk_m": 16.4,
                        "to_goal_m": 9.2},
               "closest": {"pos": [-697.9, 8.1, -799], "to_goal_m": 17.7, "dy_to_goal": -14.1, "path_m": 1.2}}
        out = route.render_route(res, render.Handles(), "it")
        self.assertIn("JUMP ACROSS: from (-689,1.3,-786) (walk 16.4m) over a 3m gap, 2.5m down to (-686,-1.2,-786)", out)
        self.assertIn("NEXT: travel_to x,y,z = (-689,1.3,-786) (the edge), then jump_to x,y,z = (-686,-1.2,-786)", out)

    def test_lower_ground_listed_beyond_a_jump(self):
        """S038/S039 Nightsong's Prison: the islands 21 m below, 14 m out; route said only 'find a ladder/ledge'."""
        res = {"from": [-442, -6.7, -1422], "goal": [-611, -142, -1427], "straight_m": 237.6, "dir": "W", "dy": -135.3,
               "reachable": False, "far": True,
               "lower": [{"dir": "NW", "from": [-443.5, -6.7, -1416.3], "to": [-455.5, -27.8, -1408.8], "gap_m": 14.2,
                          "drop_m": 21.1, "to_goal_m": 156.8, "toward": True, "reach_m": 8.5}],
               "closest": {"pos": [-427.7, -2.3, -1432.1], "to_goal_m": 230.6, "dy_to_goal": -139.7, "path_m": 7.7}}
        out = route.render_route(res, render.Handles(), "objective Balthazar")
        self.assertIn("LOWER GROUND NW: a floor 21.1m below at (-455.5,-27.8,-1408.8), 14.2m out from the edge at "
                      "(-443.5,-6.7,-1416.3), 156.8m from the goal; beyond a jump from there (~8.5m)", out)
        self.assertNotIn("then jump_to x,y,z = (-455.5", out)  # out of reach: no jump suggested
        res["lower"][0]["reach_m"] = 30.0
        out = route.render_route(res, render.Handles(), "objective Balthazar")
        self.assertIn("jumping down from there reaches ~30m: move_to the edge, then jump_to (-455.5,-27.8,-1408.8)", out)
        self.assertIn("(a 21.1m fall: fall damage unless the jumper has Feather Fall)", out)
        res["feather_fall"] = True  # FEATHER_FALL on the jumper: no fall damage, no warning
        self.assertNotIn("fall damage", route.render_route(res, render.Handles(), "objective Balthazar"))
        self.assertIn("NEXT: move_to x,y,z = (-443.5,-6.7,-1416.3) (the edge), then jump_to x,y,z = (-455.5,-27.8,-1408.8)",
                      out)

    def test_teleport_hint_for_lower_ground_beyond_a_jump(self):
        """S039: Scrolls of Misty Step / Dimension Door in the bags while the agent looked for a jump off the island."""
        from unittest import mock
        lo = {"dir": "NW", "from": [-443.5, -6.7, -1416.3], "to": [-455.5, -10.8, -1408.8], "gap_m": 14.2,
              "drop_m": 4.1, "reach_m": 8.5}
        inv = {"items": [{"uuid": "11111111-0000-0000-0000-00000000aaaa", "name": "Scroll of Misty Step", "owner": P1}]}
        g = mock.Mock()
        g.try_rpc.return_value = inv
        with mock.patch.object(server, "game", g):
            out = server._teleport_hint({"lower": [lo]})
            self.assertIn("TELEPORT:", out)
            self.assertIn("Scroll of Misty Step (18m) reaches the floor NW", out)
            lo2 = dict(lo, to=[-455.5, -27.8, -1408.8])  # 21 m down: 25 m in 3D
            self.assertIn("too far", server._teleport_hint({"lower": [lo2]}))
            self.assertEqual(server._teleport_hint({"lower": [dict(lo, reach_m=30)]}), "")  # a jump does it
            g.try_rpc.return_value = {"items": []}
            self.assertEqual(server._teleport_hint({"lower": [lo]}), "")

    def test_unknown_path_says_why(self):
        """S039 21:44: 'path unknown (pathfinding unavailable)' right after a jump onto a rock off the grid."""
        res = {"from": [-413, 1.1, -1441], "goal": [-516, -60, -1397], "straight_m": 127.3, "dir": "NW", "dy": -61.1,
               "far": True, "unknown_reason": "no floor under the character (mid-jump or falling)"}
        out = route.render_route(res, render.Handles(), "it")
        self.assertIn("path unknown: no floor under the character (mid-jump or falling); step onto plain ground", out)

    def test_no_path_to_the_void_says_so(self):
        """S039 21:44 route (-516,-60,-1397): a goal with no ground at all is 'no ground there', not 'unknown'."""
        res = {"from": [-413, 1.1, -1441], "goal": [-516, -60, -1397], "straight_m": 127.3, "dir": "NW", "dy": -61.1,
               "far": True, "reachable": False, "no_path_reason": "no walkable ground at the target"}
        out = route.render_route(res, render.Handles(), "it")
        self.assertIn("NO WALKABLE PATH from here: the engine finds no way there from this side: no walkable ground at "
                      "the target", out)


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

    def test_dead_ends_carry_the_level_and_a_load_clears_them(self):
        self.g.reach = {"reachable": False}
        server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertEqual([e.level for e in server.DEAD.items], ["TUT"])  # state.status level
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "P__S099-pre").mkdir()
            with mock.patch.object(server, "SAVE_DIR", Path(tmp)), mock.patch.object(server, "SAVE_PREFIX", "P"), \
                    mock.patch.object(server, "_enable_save_gates", lambda: None), \
                    mock.patch.object(server.SAVER, "settle", lambda: ""), \
                    mock.patch.object(server, "_game_process_alive", lambda: True), \
                    mock.patch.object(server, "_server_boot", lambda: "b"), \
                    mock.patch.object(server, "_load_once", lambda *a: (JobResult("done", {"loaded": "S099-pre"}), False)):
                server.load_game("S099-pre")
        self.assertEqual(server.DEAD.items, [])
        second = server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertNotIn("tried", second)  # the wall hit before the load is forgotten

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

    def test_fast_travel_from_camp_leaves_camp_first(self):
        # live 2026-10-10: a fast travel from camp moved only Claude (companions stayed at the Campsite)
        seen, at_camp = [], {"on": True}
        orig_rpc, orig_job, orig_wait = self.g.rpc, self.g.run_job, server._wait_running

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.fast_travel" and (args or {}).get("dry"):
                seen.append("dry")
                if at_camp["on"]:
                    raise GameError("at camp: leave camp first (camp leave), then fast travel")
                return {"can": True}
            return orig_rpc(op, args, context, timeout)

        def run_job(op, args, context="server", wait_s=25.0, poll_s=0.01, on_start=None):
            if op == "act.leave_camp":
                seen.append("leave")
                at_camp["on"] = False
                return JobResult("done", {"at_camp": False})
            if op == "ui.fast_travel":
                seen.append("travel")
                return JobResult("done", {"arrived": args["waypoint"]})
            return orig_job(op, args, context, wait_s, poll_s, on_start)
        self.g.rpc, self.g.run_job = rpc, run_job
        server._wait_running = lambda max_s=60.0: True
        try:
            out = server.fast_travel("Last Light Inn")
            self.assertEqual(seen, ["dry", "leave", "travel"])
            self.assertIn("left camp first", out)
            seen.clear()
            server.fast_travel("Last Light Inn")  # not at camp: no leave
            self.assertEqual(seen, ["dry", "travel"])
            at_camp["on"] = True
            self.g.run_job = lambda op, args, **kw: (seen.append(op), JobResult("failed", None, "cannot travel right now"))[1]
            seen.clear()
            out = server.fast_travel("Last Light Inn")
            self.assertIn("FAILED to leave camp first", out)
            self.assertNotIn("ui.fast_travel", seen)
        finally:
            server._wait_running = orig_wait

    def test_fast_travel_in_a_no_travel_area_says_why_at_once(self):
        # S035 19:27/19:39: "can't fast travel now (Location)" twice in the Gauntlet of Shar, no way out given
        seen = []
        orig_try, orig_job = self.g.try_rpc, self.g.run_job

        def try_rpc(op, args=None, context="server", **kw):
            if op == "ui.travel_block":
                return {"error": "Location"}
            return orig_try(op, args, context=context, **kw)
        self.g.try_rpc = try_rpc
        self.g.run_job = lambda op, args, **kw: (seen.append(op), JobResult("done", {}))[1]
        try:
            out = server.fast_travel("Last Light Inn")
        finally:
            self.g.try_rpc, self.g.run_job = orig_try, orig_job
        self.assertIn("FAILED — can't fast travel now (Location)", out)
        self.assertIn("Way out: walk out the way you came", out)
        self.assertNotIn("ui.fast_travel", seen)

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
