"""Offline tests (S002 triage): travel_to retries a failed leg with an alternative one instead of ending the
trip.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from bg3ai import macros
from bg3ai.tools import core, plumbing, travel_macro
from test_turn_macros import CHEST, P1, P2, Base


class LegRules(unittest.TestCase):
    def test_retryable_only_for_ground_failures(self):
        f = "step 1 move_to failed"
        self.assertTrue(macros.leg_retryable("failed", f, "move_to: FAILED — no walkable path there (may need a jump)"))
        self.assertTrue(macros.leg_retryable("failed", f, "move_to: FAILED — could not get closer to the destination "
                                                          "— @(28.3,18.3,-352.2) reason=path blocked stopped_short"))
        self.assertFalse(macros.leg_retryable("failed", f, "move_to: FAILED — character is in a conversation"))
        self.assertFalse(macros.leg_retryable("failed", "step 1 move_to interrupted",
                                              "move_to: INTERRUPTED — a conversation started"))
        self.assertFalse(macros.leg_retryable("dialogue", "a conversation started", "no walkable path there"))
        # live 13:09: from a ladder's top toward its foot the walk stood still ("no path to the destination")
        self.assertTrue(macros.leg_retryable("failed", f, "move_to: FAILED — no path to the destination — travelled=0"))

class TravelLegs(Base):
    def setUp(self) -> None:
        super().setUp()
        plumbing.SETTLE_S = 10.0
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

    def reach(self, a):
        moves = [m for m in self.g.ops("act.move_to")]
        x = moves[-1][1]["pos"][0] if moves else 0.0
        if a.get("alt"):
            return {"reachable": True, "pos": [x + 10.0, 0.0, 2.0], "alt": "route", "verified": True}
        return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0], "verified": True}

    def test_failed_leg_gets_another_leg_and_the_trip_goes_on(self):
        # S002: "leg 2: move_to: FAILED — no walkable path there" ended the whole trip.
        self.g.reach = self.reach
        self.g.refuse_if = lambda op, a: ("no walkable path there (may need a jump or another route)"
                                          if op == "act.move_to" and a.get("pos") == [30.0, 0.0, 0.0] else None)
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0)
        calls = self.g.ops("nav.reach_toward")
        self.assertTrue(calls[1][1].get("alt"))
        self.assertEqual(calls[1][1]["exclude"], [30.0, 0.0, 0.0])
        self.assertNotIn("alt", calls[2][1])  # back to normal legs after a good one
        self.assertIn("another way (along the route)", out)
        self.assertIn("arrived", out)

    def test_run1_leg_ends_reach_the_guard_unchanged(self):
        # E2E run 1 (actions.jsonl #323, travel_to (-47.4,13.2,-426)): leg 3 refused by the mod's point_ok, which keys
        # known ground by the exact point (1 m cells): the leg ends nav.reach_toward checked go to act.move_to as they
        # are, and a refusal ends the trip (no retry that hides a planner/guard disagreement).
        legs = [[-14.2, 17.6, -428.1], [-20.7, 17.3, -432.2], [-37.6, 15.0, -434.3]]
        self.g.reach = lambda a: {"reachable": True, "pos": legs[len(self.g.ops("act.move_to"))], "verified": True}
        guard = "that spot is not in view and the party hasn't walked there: pick ground you can see"
        self.g.refuse_if = lambda op, a: guard if op == "act.move_to" and a.get("pos") == legs[2] else None
        out = travel_macro.travel_to(x=-47.4, y=13.2, z=-426.0)
        self.assertEqual([m[1]["pos"] for m in self.g.ops("act.move_to")], legs)
        self.assertFalse(macros.leg_retryable("failed", "step 1 move_to failed", f"move_to: FAILED — {guard}"))
        self.assertIn("STOPPED: leg 3 failed", out)

    def test_retries_are_capped(self):
        self.g.reach = self.reach
        self.g.refuse_if = lambda op, a: "no walkable path there" if op == "act.move_to" else None
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual(len(self.g.ops("act.move_to")), 1 + macros.TRAVEL_LEG_RETRIES)
        self.assertIn("STOPPED: leg 3 failed", out)

    def test_no_alternative_says_so(self):
        def reach(a):
            if a.get("alt"):
                return {"reachable": False, "alt_failed": True}
            return {"reachable": True, "pos": [30.0, 0.0, 0.0]}
        self.g.reach = reach
        self.g.refuse_if = lambda op, a: "no walkable path there" if op == "act.move_to" else None
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertIn("no other leg either", out)

    def test_a_conversation_is_not_retried(self):
        self.g.reach = self.reach
        self.g.refuse_if = lambda op, a: "character is in a conversation" if op == "act.move_to" else None
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertIn("leg 1 failed", out)

    def test_tiny_sampled_leg_takes_a_longer_alternative(self):
        # Acceptance (nav.log): legs of 30 m, 2.2 m, 29 m; the sampler stopped at an edge.
        def reach(a):
            moves = self.g.ops("act.move_to")
            x = moves[-1][1]["pos"][0] if moves else 0.0
            if a.get("alt"):
                return {"reachable": True, "pos": [x + 25.0, 0.0, 0.0], "alt": "route", "verified": True}
            return {"reachable": True, "pos": [x + 2.0, 0.0, 0.0], "verified": True}
        self.g.reach = reach
        out = travel_macro.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual([m[1]["pos"][0] for m in self.g.ops("act.move_to")], [25.0, 50.0])
        self.assertIn("arrived", out)

    def test_short_leg_kept_when_no_better_one(self):
        def reach(a):
            moves = self.g.ops("act.move_to")
            x = moves[-1][1]["pos"][0] if moves else 0.0
            if a.get("alt"):
                return {"reachable": False, "alt_failed": True}
            return {"reachable": True, "pos": [min(50.0, x + 4.0), 0.0, 0.0], "verified": True}
        self.g.reach = reach
        travel_macro.travel_to(x=12.0, y=0.0, z=0.0)
        self.assertEqual([m[1]["pos"][0] for m in self.g.ops("act.move_to")][:2], [4.0, 8.0])

    def test_direct_route_is_one_leg(self):
        # S005 (the beach): a reachable goal 90 m off is walked along the engine's route in <= 55 m legs.
        def reach(a):
            self.assertTrue(a.get("direct"))
            moves = self.g.ops("act.move_to")
            x = moves[-1][1]["pos"][0] if moves else 0.0
            nx = min(90.0, x + 55.0)
            return {"reachable": True, "pos": [nx, 0.0, 0.0], "direct": True, "verified": True,
                    "route_m": 92.0 - x, "path_m": nx - x}
        self.g.reach = reach
        out = travel_macro.travel_to(x=90.0, y=0.0, z=0.0)
        self.assertEqual([m[1]["pos"][0] for m in self.g.ops("act.move_to")], [55.0, 90.0])
        self.assertIn("leg 1: along the route (92m): move_to: done", out)
        self.assertIn("arrived", out)

    def test_route_heading_away_first_is_progress(self):
        # S029 16:07: Last Light -> Mausoleum, a 325 m route (2.4x the straight line) whose first legs ran away
        # from the goal; "no walkable ground leads further toward it" ended the trip, 2 min of hand-made legs.
        legs = [[-40.0, 0.0, 0.0], [-40.0, 0.0, 60.0], [20.0, 0.0, 60.0], [60.0, 0.0, 60.0], [60.0, 0.0, 0.0]]
        left = [180.0, 120.0, 60.0, 20.0, 0.0]

        def reach(a):
            n = len(self.g.ops("act.move_to"))
            return {"reachable": True, "pos": legs[n], "direct": True, "verified": True,
                    "route_m": left[n] + 40.0, "path_m": 40.0, "route_left_m": left[n]}
        self.g.reach = reach
        out = travel_macro.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertEqual([m[1]["pos"] for m in self.g.ops("act.move_to")], legs)
        self.assertIn("arrived", out)
        self.assertNotIn("no walkable ground", out)

    def test_route_leg_across_a_surface_is_weighed_and_walked(self):
        # S030 16:35 Soft-Step Trial: the only way crossed Grease; travel_to(avoid_hazards="cross") said "no walkable
        # ground leads further". Lua now hands the route leg back with its exposure; crossing_ok decides.
        def reach(a):
            n = len(self.g.ops("act.move_to"))
            pos = [[-20.0, 0.0, 10.0], [10.0, 0.0, 0.0]][min(n, 1)]
            r = {"reachable": True, "pos": pos, "direct": True, "verified": True, "route_m": 45.0 - 25 * n,
                 "path_m": 25.0, "route_left_m": 20.0 - 20 * n}
            if n == 0:
                r["exposure"] = {"surface": "Grease", "m": 3.0, "patches": 1, "pos": [-15.0, 0.0, 5.0], "at_m": 12}
            return r
        self.g.reach = reach
        out = travel_macro.travel_to(x=10.0, y=0.0, z=0.0, avoid_hazards="cross")
        self.assertIn("crossing 3m of Grease", out)
        self.assertIn("arrived", out)

    def test_route_legs_that_stop_gaining_still_stall(self):
        def reach(a):
            return {"reachable": True, "pos": [-40.0, 0.0, 0.0], "direct": True, "verified": True,
                    "route_m": 220.0, "path_m": 40.0, "route_left_m": 180.0}
        self.g.reach = reach
        out = travel_macro.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertLessEqual(len(self.g.ops("act.move_to")), 2)
        self.assertIn("no walkable ground leads further", out)


class DoorLink(Base):
    """B4 (S037 Rescue Volo; vis f3b25d2): Volo inside the Sanctum read 610 m off, and travel_to walked toward the
    straight line until blocked; nav.route now names the Heavy Oak Doors the party came out of (door_link). The trip
    goes to the door, uses it, and walks on from where it let the party out."""
    DOOR = "dddddddd-0000-0000-0000-000000000352"

    def setUp(self) -> None:
        super().setUp()
        plumbing.SETTLE_S = 10.0
        def result(op, a):
            if op != "act.move_to":
                return {"used": True}
            for r in self.g.watch["party"]:
                if r["uuid"] == a.get("char"):
                    r["pos"] = list(a["pos"])
            return {"after": {"pos": a.get("pos")}}
        self.g.result_for = result
        self.door, self.through = [20.0, 0.0, 0.0], True

        def route(a):
            if a["pos"][0] != 560.0:
                return {"reachable": True}
            return {"too_far": True, "door_link": {"uuid": self.DOOR, "name": "Heavy Oak Doors", "door": self.door,
                                                   "to": [500.0, 0.0, 0.0], "door_m": self.door[0], "dir": "E",
                                                   "to_goal_m": 60.0}}
        self.g.route = route

        def on_start(op, a):
            if op == "act.interact" and self.through:
                for r in self.g.watch["party"]:
                    r["pos"] = [500.0 + (r["uuid"] == P2), 0.0, 0.0]
        self.g.on_start = on_start

        def reach(a):
            x = next(r["pos"][0] for r in self.g.watch["party"] if r["uuid"] == P1)
            return {"reachable": True, "pos": [min(a["pos"][0], x + a["max_m"]), 0.0, 0.0], "verified": True}
        self.g.reach = reach

    def legs(self) -> tuple[list, list]:
        i = self.g.calls.index(self.g.ops("act.interact")[0])
        moves = [(self.g.calls.index(m), m[1]["pos"][0]) for m in self.g.ops("act.move_to")]
        return [x for n, x in moves if n < i], [x for n, x in moves if n > i]

    def test_the_trip_goes_through_the_door_then_on(self):
        out = travel_macro.travel_to(x=560.0, y=0.0, z=0.0)
        self.assertEqual(self.g.ops("act.interact")[0][1]["target"], self.DOOR)
        self.assertEqual(self.legs(), ([], [530.0, 560.0]))
        h = core.H.get(self.DOOR)
        self.assertIn(f"BEHIND A DOOR: {h} Heavy Oak Doors @(20,0,0) took the party to (500,0,0) before, 60m from "
                      "the goal: going through it", out)
        self.assertIn(f"through {h} Heavy Oak Doors: now @(500,0,0)", out)
        self.assertIn("arrived", out)
        self.assertEqual(self.g.ops("nav.waypoints"), [])  # no fast travel to a waypoint walk (S037's blocked one)

    def test_a_far_door_is_walked_to_first(self):
        self.door = [100.0, 0.0, 0.0]
        out = travel_macro.travel_to(x=560.0, y=0.0, z=0.0)
        self.assertEqual(self.legs(), ([30.0, 60.0, 90.0, 100.0], [530.0, 560.0]))
        self.assertIn("to the door: travel_to", out)
        self.assertTrue(all(a["pos"][0] == 560.0 for _, a in self.g.ops("nav.route")), "the walk to the door asks none")
        self.assertIn("arrived", out)

    def test_a_door_that_did_not_take_the_party_stops(self):
        self.through = False
        out = travel_macro.travel_to(x=560.0, y=0.0, z=0.0)
        self.assertEqual(self.legs(), ([], []))
        self.assertIn(f"STOPPED: used {core.H.get(self.DOOR)} Heavy Oak Doors, but it did not take the party through "
                      "(still @(0,0,0))", out)

    def test_a_goal_below_behind_the_door_is_not_another_floor(self):
        """Live D2 (T7): travel_to(objective) from the Goblin Camp was refused "likely inside an interior ... Goblin
        Tunnel <exit>" by _other_floor before the trip reached the door; the route's door_link is the way there."""
        for r in self.g.watch["party"]:
            r["pos"][1] = 30.0
        self.door = [20.0, 30.0, 0.0]
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.frontier":
                return {"leads": [{"uuid": CHEST, "name": "Goblin Tunnel", "kind": "exit", "dist": 10.0}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake
        out = travel_macro.travel_to(x=560.0, y=0.0, z=0.0)
        self.assertNotIn("REFUSED", out)
        self.assertEqual(self.g.ops("act.interact")[0][1]["target"], self.DOOR)
        self.assertEqual(self.legs(), ([], [530.0, 560.0]))
        self.assertIn("arrived", out)

    def test_no_door_link_walks_as_before(self):
        self.g.route = {"too_far": True}
        out = travel_macro.travel_to(x=560.0, y=0.0, z=0.0)
        self.assertEqual(self.g.ops("act.interact"), [])
        self.assertNotIn("BEHIND A DOOR", out)


class InTheWay(Base):
    """Live 11:12, Last Light Inn: after an Oak Door, four legs stopped short "path blocked" at one spot: someone
    stood in the doorway. A companion there is stepped back; an NPC gets a moment to move; either way it is named."""
    NPC = "77777777-0000-0000-0000-000000000001"

    def setUp(self) -> None:
        super().setUp()
        plumbing.SETTLE_S = 10.0
        self.saved = travel_macro.BLOCKER_WAIT_S
        travel_macro.BLOCKER_WAIT_S = 0.5
        self.addCleanup(setattr, travel_macro, "BLOCKER_WAIT_S", self.saved)
        self.leader_moves = 0

        def result(op, a):
            if op != "act.move_to":
                return {}
            if a.get("char") == P2:
                return {"after": {"pos": a.get("pos")}}
            self.leader_moves += 1
            if self.leader_moves == 1:  # the first leg ends at the doorway
                self.g.watch["party"][0]["pos"] = [10.0, 0.0, 0.0]
                return {"stopped_short": True, "reason": "path blocked", "after": {"pos": [10.0, 0.0, 0.0]}}
            self.g.watch["party"][0]["pos"] = list(a["pos"])
            return {"after": {"pos": a.get("pos")}}
        self.g.result_for = result

        def reach(a):
            x = self.g.watch["party"][0]["pos"][0]
            return {"reachable": True, "pos": [min(30.0, x + 20.0), 0.0, 0.0], "verified": True}
        self.g.reach = reach

    def moves_of(self, who):
        return [a for _, a in self.g.ops("act.move_to") if a.get("char") == who]

    def test_companion_in_the_doorway_is_stepped_back(self):
        self.g.watch["party"][1]["pos"] = [11.5, 0.0, 0.4]
        out = travel_macro.travel_to(x=30.0, y=0.0, z=0.0, wait=True)
        self.assertIn("in the way: ", out)
        self.assertIn("stepped back behind", out)
        self.assertEqual(self.moves_of(P2)[0]["pos"], [7.0, 0.0, 0.0])
        self.assertIn("arrived", out)
        self.assertNotIn("STOPPED", out)

    def test_npc_that_moves_on_is_waited_for(self):
        npc = {"uuid": self.NPC, "name": "Mattis", "pos": [11.0, 0.0, 0.0], "dist": 1.0}
        self.g.nearby = {"characters": [npc], "items": []}
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            r = orig(op, args, context, timeout)
            if op == "state.nearby" and "center" not in (args or {}):  # not a leg end's read (the PASSED line)
                self.g.nearby["characters"][0]["pos"] = [11.0, 0.0, 5.0]  # steps aside after the first look
            return r
        self.g.rpc = rpc
        out = travel_macro.travel_to(x=30.0, y=0.0, z=0.0, wait=True)
        self.assertIn("Mattis: waited", out)
        self.assertIn("arrived", out)
        self.assertEqual(self.moves_of(P2), [])

    def test_npc_that_stays_is_named_in_the_stop(self):
        self.g.result_for = lambda op, a: ({"stopped_short": True, "reason": "path blocked",
                                            "after": {"pos": [10.0, 0.0, 0.0]}} if op == "act.move_to" else {})
        self.g.watch["party"][0]["pos"] = [10.0, 0.0, 0.0]
        self.g.nearby = {"characters": [{"uuid": self.NPC, "name": "Mattis", "pos": [11.0, 0.0, 0.2]}], "items": []}
        out = travel_macro.travel_to(x=30.0, y=0.0, z=0.0, wait=True)
        self.assertIn("Mattis (stands in the path, didn't move", out)
        self.assertIn("STOPPED", out)
        self.assertEqual(out.count("didn't move"), 1)

    def test_stalled_trip_to_a_lower_floor_names_the_ladder(self):
        # se 12:3x: only the "no walkable ground" stop named the ladder; a stall (legs that get nowhere) did not.
        self.g.reach = lambda a: {"reachable": True, "pos": [10.0, 0.0, 0.0], "verified": True}
        self.g.result_for = lambda op, a: {"after": {"pos": [0.0, 0.0, 0.0]}} if op == "act.move_to" else {}
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.frontier":
                return {"leads": [{"uuid": CHEST, "name": "Ladder", "kind": "ladder", "dist": 13.0, "dir": "N"}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake
        out = travel_macro.travel_to(x=30.0, y=-12.0, z=0.0, wait=True)
        self.assertIn("STOPPED", out)
        self.assertIn("12m below you; ladders/stairs/hatches: ", out)
        self.assertIn("Ladder <ladder> 13m N", out)

    def test_failed_legs_toward_a_lower_floor_name_the_ladder(self):
        # live 13:09: "leg 1 failed" (no path to the destination) ended the trip without naming the ladder 0 m away
        self.g.reach = lambda a: {"reachable": True, "pos": [10.0, -11.0, 0.0], "direct": True, "route_m": 27.6}
        self.g.refuse_if = lambda op, a: "no path to the destination" if op == "act.move_to" else None
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.frontier":
                return {"leads": [{"uuid": CHEST, "name": "Ladder", "kind": "ladder", "dist": 1.0, "dir": "E"}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake
        out = travel_macro.travel_to(x=10.0, y=-11.0, z=0.0, wait=True)
        self.assertIn("11m below you; ladders/stairs/hatches: ", out)
        self.assertGreater(len(self.g.ops("act.move_to")), 1)  # retried another way first


        self.assertEqual(travel_macro._in_the_way([0, 0, 0], [10, 0, 0], [{"uuid": "x", "pos": [-1, 0, 0]},
                                                                     {"uuid": "y", "pos": [2, 0, 3]},
                                                                     {"uuid": "z", "pos": [2, 5, 0]}], P1), [])


if __name__ == "__main__":
    unittest.main()
