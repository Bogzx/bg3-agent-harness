"""Offline tests (S002 triage): travel_to retries a failed leg with an alternative one instead of ending the
trip, and combat_turn steps into range when a cast is just out of it.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest

from bg3ai import macros, server
from test_turn_macros import CHEST, GOB, P1, P2, Base, combat


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

    def test_tiny_sampled_leg_takes_a_longer_alternative(self):
        # Acceptance (nav.log): legs of 30 m, 2.2 m, 29 m; the sampler stopped at an edge.
        def reach(a):
            moves = self.g.ops("act.move_to")
            x = moves[-1][1]["pos"][0] if moves else 0.0
            if a.get("alt"):
                return {"reachable": True, "pos": [x + 25.0, 0.0, 0.0], "alt": "route", "verified": True}
            return {"reachable": True, "pos": [x + 2.0, 0.0, 0.0], "verified": True}
        self.g.reach = reach
        out = server.travel_to(x=50.0, y=0.0, z=0.0)
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
        server.travel_to(x=12.0, y=0.0, z=0.0)
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
        out = server.travel_to(x=90.0, y=0.0, z=0.0)
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
        out = server.travel_to(x=60.0, y=0.0, z=0.0)
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
        out = server.travel_to(x=10.0, y=0.0, z=0.0, avoid_hazards="cross")
        self.assertIn("crossing 3m of Grease", out)
        self.assertIn("arrived", out)

    def test_route_legs_that_stop_gaining_still_stall(self):
        def reach(a):
            return {"reachable": True, "pos": [-40.0, 0.0, 0.0], "direct": True, "verified": True,
                    "route_m": 220.0, "path_m": 40.0, "route_left_m": 180.0}
        self.g.reach = reach
        out = server.travel_to(x=60.0, y=0.0, z=0.0)
        self.assertLessEqual(len(self.g.ops("act.move_to")), 2)
        self.assertIn("no walkable ground leads further", out)


class InTheWay(Base):
    """Live 11:12, Last Light Inn: after an Oak Door, four legs stopped short "path blocked" at one spot: someone
    stood in the doorway. A companion there is stepped back; an NPC gets a moment to move; either way it is named."""
    NPC = "77777777-0000-0000-0000-000000000001"

    def setUp(self) -> None:
        super().setUp()
        server.SETTLE_S = 10.0
        self.saved = server.BLOCKER_WAIT_S
        server.BLOCKER_WAIT_S = 0.5
        self.addCleanup(setattr, server, "BLOCKER_WAIT_S", self.saved)
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
        out = server.travel_to(x=30.0, y=0.0, z=0.0, wait=True)
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
            if op == "state.nearby":
                self.g.nearby["characters"][0]["pos"] = [11.0, 0.0, 5.0]  # steps aside after the first look
            return r
        self.g.rpc = rpc
        out = server.travel_to(x=30.0, y=0.0, z=0.0, wait=True)
        self.assertIn("Mattis: waited", out)
        self.assertIn("arrived", out)
        self.assertEqual(self.moves_of(P2), [])

    def test_npc_that_stays_is_named_in_the_stop(self):
        self.g.result_for = lambda op, a: ({"stopped_short": True, "reason": "path blocked",
                                            "after": {"pos": [10.0, 0.0, 0.0]}} if op == "act.move_to" else {})
        self.g.watch["party"][0]["pos"] = [10.0, 0.0, 0.0]
        self.g.nearby = {"characters": [{"uuid": self.NPC, "name": "Mattis", "pos": [11.0, 0.0, 0.2]}], "items": []}
        out = server.travel_to(x=30.0, y=0.0, z=0.0, wait=True)
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
        out = server.travel_to(x=30.0, y=-12.0, z=0.0, wait=True)
        self.assertIn("STOPPED", out)
        self.assertIn("12m below you: climb with interact on a ladder", out)
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
        out = server.travel_to(x=10.0, y=-11.0, z=0.0, wait=True)
        self.assertIn("11m below you: climb with interact on a ladder", out)
        self.assertGreater(len(self.g.ops("act.move_to")), 1)  # retried another way first


        self.assertEqual(server._in_the_way([0, 0, 0], [10, 0, 0], [{"uuid": "x", "pos": [-1, 0, 0]},
                                                                     {"uuid": "y", "pos": [2, 0, 3]},
                                                                     {"uuid": "z", "pos": [2, 5, 0]}], P1), [])


class MeleeApproach(Base):
    """S023-S025: melee moves ended 2.3-2.9 m from the target ("no walkable route gets closer than 2.5m", or a
    plain move's 2.2 m + slack) and 22 attacks were refused out of range; a later step got in reach."""

    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat(gob_dist=6.0)
        self.g.watch["combat"] = True
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)

    def test_route_ending_at_the_body_walks_beside_the_target(self):
        self.g.approach = {"reachable": True, "pos": [3.5, 0.0, 0.0], "walk_m": 3.5, "limit": "route_end",
                           "end_m": 2.5}
        self.g.result_for = lambda op, a: ({"dist": 1.4, "target": a.get("target"), "in_reach": True}
                                           if op == "act.move_to" else {})
        out = server.combat_turn(json.dumps({"move": {"target": self.gob, "within": 1.5},
                                             "action": ["Shout_Dodge"]}), end=False)
        mv = self.g.ops("act.move_to")[0][1]
        self.assertEqual((mv.get("target"), mv.get("within")), (GOB, 1.5))
        self.assertNotIn("pos", mv)
        self.assertNotIn("stopped short", out)

    def test_plain_move_passes_the_melee_reach_and_says_when_short(self):
        self.g.result_for = lambda op, a: ({"dist": 2.6, "target": a.get("target"), "in_reach": False}
                                           if op == "act.move_to" else {})
        out = server.combat_turn(json.dumps({"move": self.gob, "action": ["Shout_Dodge"]}), end=False)
        self.assertEqual(self.g.ops("act.move_to")[0][1].get("within"), 1.5)
        self.assertIn("stopped short of melee reach", out)

    def test_far_ledge_still_stays_put(self):
        self.g.approach = {"reachable": True, "pos": [0.0, 0.0, 0.0], "walk_m": 0.2, "limit": "route_end",
                           "end_m": 7.8}
        out = server.combat_turn(json.dumps({"move": {"target": self.gob, "within": 1.5},
                                             "action": ["Shout_Dodge"]}), end=False)
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertIn("stays put", out)

    def test_melee_out_of_range_steps_beside_it_and_attacks(self):
        n = {"fails": 1}

        def refuse(op, a):
            if op == "act.cast" and n["fails"] > 0:
                n["fails"] -= 1
                return "target is out of range (2.5 m, range 1.5 m): move closer first"
            return None
        self.g.refuse_if = refuse
        self.g.result_for = lambda op, a: ({"dist": 1.3, "target": a.get("target"), "in_reach": True}
                                           if op == "act.move_to" else {})
        out = server.combat_turn(json.dumps({"action": {"ability": "Target_MainHandAttack", "target": self.gob}}))
        self.assertEqual(self.g.ops("act.move_to")[0][1].get("within"), 1.5)
        self.assertEqual(self.g.ops("nav.approach"), [])
        self.assertIn("stepped next to it", out)
        self.assertEqual(self.casts(), ["Target_MainHandAttack", "Target_MainHandAttack"])


class CombatOver(Base):
    def test_combat_turn_after_the_fight_is_not_a_failure(self):
        self.g.combat = {"active": False}
        out = server.combat_turn(json.dumps({"action": ["Shout_Dodge"]}))
        self.assertTrue(out.startswith("combat_turn: nothing to play — not in combat"), out)
        self.assertEqual(self.casts(), [])

    def test_container_spell_takes_its_first_variant(self):
        self.g.combat = combat()
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        self.g.refuse_if = lambda op, a: ("choose a variant: Shout_SpiritGuardians_Radiant, Shout_SpiritGuardians_Necrotic"
                                          if op == "act.cast" and a.get("spell") == "Shout_SpiritGuardians" else None)
        out = server.combat_turn(json.dumps({"action": ["Shout_SpiritGuardians"]}))
        self.assertEqual(self.casts(), ["Shout_SpiritGuardians", "Shout_SpiritGuardians_Radiant"])
        self.assertIn("took the first variant Shout_SpiritGuardians_Radiant", out)


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
