"""Offline tests (S002 triage): travel_to retries a failed leg with an alternative one instead of ending the
trip, and combat_turn steps into range when a cast is just out of it.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest

from bg3ai import macros, server
from test_turn_macros import GOB, Base, combat


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

    def test_range_gap(self):
        self.assertEqual(macros.range_gap("FAILED: target is out of range (18.6 m, range 18.0 m): move closer first"),
                         (18.6, 18.0))
        self.assertIsNone(macros.range_gap("FAILED: no line of sight"))


class TravelLegs(Base):
    def setUp(self) -> None:
        super().setUp()
        server.SETTLE_S = 10.0
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
        out = server.travel_to(x=50.0, y=0.0, z=0.0)
        calls = self.g.ops("nav.reach_toward")
        self.assertTrue(calls[1][1].get("alt"))
        self.assertEqual(calls[1][1]["exclude"], [30.0, 0.0, 0.0])
        self.assertNotIn("alt", calls[2][1])  # back to normal legs after a good one
        self.assertIn("another way (along the route)", out)
        self.assertIn("arrived", out)

    def test_retries_are_capped(self):
        self.g.reach = self.reach
        self.g.refuse_if = lambda op, a: "no walkable path there" if op == "act.move_to" else None
        out = server.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual(len(self.g.ops("act.move_to")), 1 + macros.TRAVEL_LEG_RETRIES)
        self.assertIn("STOPPED: leg 3 failed", out)

    def test_no_alternative_says_so(self):
        def reach(a):
            if a.get("alt"):
                return {"reachable": False, "alt_failed": True}
            return {"reachable": True, "pos": [30.0, 0.0, 0.0]}
        self.g.reach = reach
        self.g.refuse_if = lambda op, a: "no walkable path there" if op == "act.move_to" else None
        out = server.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertIn("no other leg either", out)

    def test_a_conversation_is_not_retried(self):
        self.g.reach = self.reach
        self.g.refuse_if = lambda op, a: "character is in a conversation" if op == "act.move_to" else None
        out = server.travel_to(x=50.0, y=0.0, z=0.0)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertIn("leg 1 failed", out)

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
        out = server.travel_to(x=90.0, y=0.0, z=0.0)
        self.assertEqual([m[1]["pos"][0] for m in self.g.ops("act.move_to")], [55.0, 90.0])
        self.assertIn("leg 1: along the route (92m): move_to: done", out)
        self.assertIn("arrived", out)


class StepIntoRange(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat(gob_dist=18.6)
        self.g.watch["combat"] = True
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        self.fails = 1

        def refuse(op, a):
            if op == "act.cast" and a.get("spell") == "Target_SacredFlame" and self.fails > 0:
                self.fails -= 1
                return "target is out of range (18.6 m, range 18.0 m): move closer first"
            return None
        self.g.refuse_if = refuse

    def turn(self) -> str:
        return server.combat_turn(json.dumps({"action": {"ability": "Target_SacredFlame", "target": self.gob}}))

    def test_steps_in_and_casts(self):
        self.g.approach = {"reachable": True, "pos": [0.8, 0.0, 0.0], "walk_m": 0.8, "limit": "in_range"}
        out = self.turn()
        self.assertEqual(self.g.ops("nav.approach")[0][1]["within"], 17.7)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [0.8, 0.0, 0.0])
        self.assertEqual(self.casts(), ["Target_SacredFlame", "Target_SacredFlame"])
        self.assertIn("stepped 0.8m into range", out)
        self.assertIn(f"Target_SacredFlame {self.gob}: done", out)

    def test_not_enough_movement_stays_put(self):
        self.g.approach = {"reachable": True, "pos": [0.3, 0.0, 0.0], "walk_m": 0.3, "limit": "movement"}
        out = self.turn()
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual(self.casts(), ["Target_SacredFlame"])
        self.assertIn("out of range (18.6 m, range 18.0 m)", out)

    def test_far_out_of_range_is_left_to_the_fallbacks(self):
        self.fails = 0
        self.g.refuse_if = lambda op, a: ("target is out of range (15.6 m, range 1.5 m): move closer first"
                                          if op == "act.cast" else None)
        self.g.approach = {"reachable": True, "pos": [9.0, 0.0, 0.0], "walk_m": 9.0, "limit": "in_range"}
        self.turn()
        self.assertEqual(self.g.ops("nav.approach"), [])


if __name__ == "__main__":
    unittest.main()
