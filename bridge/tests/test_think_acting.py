"""Offline tests: vis's think-while-acting proposals for act (naut2 run 1), on the test_turn_macros fake.
ACT-P1 combat_turn move to an objective (oN), the partial walk's shrink retry, ACT-P2 a left-out `who` in a fight,
ACT-P3 walk_toward cut to the movement left. (ACT-P4 early wake: test_pipelining.EarlyWake.)

Run: cd bridge/tests && uv run --project .. python -m unittest test_think_acting
"""

from __future__ import annotations

import json
import unittest

from test_turn_macros import GOB, P1, P2, Base, combat  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import server

TRANSPONDER = {"label": "Transponder", "quest": "Escape the Nautiloid", "pos": [30.0, 0.0, 0.0], "dist": 30.0,
               "dir": "E", "here": True}


class Fight(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = combat()
        self.g.watch["combat"] = True
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            out = real(op, args, context, timeout)
            if op == "state.status" and self.g.combat.get("active"):
                out["in_combat"] = True
            return out
        self.g.rpc = rpc

    def markers(self, *ms: dict) -> None:
        server._objectives["last"] = []
        server._objective_ids.clear()

        def read(max_age=0.0):
            out = [dict(m) for m in ms]
            server._set_objectives(out)
            return out
        orig = server._objective_markers
        server._objective_markers = read
        self.addCleanup(setattr, server, "_objective_markers", orig)
        read()

    def moves(self) -> list[dict]:
        return [a for _, a in self.g.ops("act.move_to")]


class MoveToObjective(Fight):
    def test_objective_move_walks_toward_the_marker_partially(self):
        # naut2 run 1 19:59:07: {"move":"o3"} was REFUSED "o3 is the objective marker 'Transponder' …"
        self.markers(TRANSPONDER)
        reach = []

        def reach_toward(a):
            reach.append(a)
            return {"reachable": True, "pos": [20.0, 0.0, 0.0] if len(reach) == 1 else [8.0, 0.0, 0.0]}
        self.g.reach = reach_toward
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 33.2 m, 9.0 m left)"
                                          if op == "act.move_to" and a.get("pos") == [20.0, 0.0, 0.0] else None)
        out = server.combat_turn(json.dumps({"move": "o1", "action": ["Shout_Dash"]}), end=False)
        self.assertNotIn("objective marker", out)
        self.assertEqual(reach[0]["pos"], [30.0, 0.0, 0.0])  # toward the marker, a walkable spot first
        self.assertTrue(reach[0].get("loose"))
        # the walk cut to the movement left, then the Dash walks the move again (then_move by default)
        self.assertEqual([m.get("pos") for m in self.moves()][:2], [[20.0, 0.0, 0.0], [8.0, 0.0, 0.0]])
        self.assertEqual(len(self.moves()), 3)
        self.assertIn("move o1 (objective Transponder): ", out)
        self.assertIn("→ partial 8.7m toward it: done", out)
        self.assertIn("Shout_Dash: done", out)

    def test_no_walkable_ground_toward_the_marker(self):
        self.markers(TRANSPONDER)
        self.g.reach = {"reachable": False}
        out = server.combat_turn(json.dumps({"move": "o1"}), end=False)
        self.assertIn("move o1 (objective Transponder): no walkable ground toward it", out)
        self.assertEqual(self.moves(), [])

    def test_objective_in_another_area_still_fails(self):
        self.markers(dict(TRANSPONDER, here=False))
        out = server.combat_turn(json.dumps({"move": "o1"}), end=False)
        self.assertIn("FAILED: objective o1 'Transponder' is not in this area", out)

    def test_switch_off_refuses_as_before(self):
        self.markers(TRANSPONDER)
        old, server.TURN_MOVE_OBJECTIVE = server.TURN_MOVE_OBJECTIVE, False
        self.addCleanup(setattr, server, "TURN_MOVE_OBJECTIVE", old)
        out = server.combat_turn(json.dumps({"move": "o1"}), end=False)
        self.assertIn("is the objective marker 'Transponder'", out)


class PartialShrink(Fight):
    def test_a_partial_walk_over_the_movement_tries_once_shorter(self):
        # naut2 runs 1-2: "→ partial 8.7m: FAILED — beyond movement (path 9.9 m, 9.0 m left)" (the route bends)
        def refuse(op, a):
            if op != "act.move_to":
                return None
            if a.get("target"):
                return "destination is beyond remaining movement (path 14.2 m, 5.5 m left)"
            if a.get("pos") == [3.0, 0.0, 4.0]:
                return "destination is beyond remaining movement (path 6.1 m, 5.5 m left)"
            return None
        self.g.refuse_if = refuse
        self.g.reach = lambda a: {"reachable": True, "pos": [3.0, 0.0, 4.0] if a["max_m"] > 5 else [2.0, 0.0, 3.0]}
        out = server.combat_turn(json.dumps({"move": self.gob}), end=False)
        # 5.2 m that needed 6.1 m: shorter in proportion, 5.2 × 5.2 / 6.1 = 4.4 (A6: was 5.2 − 0.6 − 0.3)
        self.assertEqual([round(a["max_m"], 1) for _, a in self.g.ops("nav.reach_toward")], [5.2, 4.4])
        self.assertEqual(self.moves()[-1]["pos"], [2.0, 0.0, 3.0])
        self.assertIn("→ partial 4.4m toward it: done", out)

    def r3(self, second_ok: bool) -> str:
        # naut3-r3 #56: "move e85: beyond movement (path 5.6 m, 2.1 m left) → partial 1.8m: FAILED — beyond movement
        # (path 3.1 m, 2.1 m left)": the old retry budget 1.8 − 1.0 − 0.3 = 0.5 was too small to try
        def refuse(op, a):
            if op != "act.move_to":
                return None
            if a.get("target"):
                return "destination is beyond remaining movement (path 5.6 m, 2.1 m left)"
            if a.get("pos") == [1.5, 0.0, 0.5] or not second_ok:
                return "destination is beyond remaining movement (path 3.1 m, 2.1 m left)"
            return None
        self.g.refuse_if = refuse
        self.g.approach = lambda a: {"reachable": True, "limit": "movement", "walk_m": a.get("budget_m"),
                                     "pos": [1.5, 0.0, 0.5] if a.get("budget_m", 9) > 1.5 else [0.9, 0.0, 0.3]}
        return server.combat_turn(json.dumps({"move": self.gob}), end=False)

    def test_r3_a_short_walk_that_bends_retries_in_proportion(self):
        out = self.r3(second_ok=True)
        budgets = [round(a["budget_m"], 2) for _, a in self.g.ops("nav.approach") if "budget_m" in a]
        self.assertEqual(budgets, [1.8, 1.05])  # 1.8 × 1.8 / 3.1
        self.assertEqual(self.moves()[-1]["pos"], [0.9, 0.0, 0.3])
        self.assertIn("→ partial 1m toward it: done", out)
        self.assertNotIn("FAILED", out.split("→", 1)[1])

    def test_r3_no_spot_fits_says_it_stays_put(self):
        out = self.r3(second_ok=False)
        self.assertIn("→ no spot toward it fits the 2.1m left (the nearest needs 3.1m of walking): stays put", out)
        self.assertNotIn("FAILED", out.split("→", 1)[1])

    def test_switch_off_keeps_the_one_try(self):
        old, server.PARTIAL_SHRINK_RETRY = server.PARTIAL_SHRINK_RETRY, False
        self.addCleanup(setattr, server, "PARTIAL_SHRINK_RETRY", old)
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 14.2 m, 5.5 m left)"
                                          if op == "act.move_to" else None)
        self.g.reach = {"reachable": True, "pos": [3.0, 0.0, 4.0]}
        out = server.combat_turn(json.dumps({"move": self.gob}), end=False)
        self.assertEqual(len(self.g.ops("nav.reach_toward")), 1)
        self.assertIn("→ partial 5.2m toward it: FAILED", out)


class SpentTurn(Fight):
    def test_a_plan_on_a_turn_already_spent_says_so(self):
        # naut3-r2 #54: Tamsin's turn group stayed open after her attack, walk and end request; the same plan again
        self.g.refuse_if = lambda op, a: ("no movement left" if op == "act.move_to" else
                                          "not enough action" if op == "act.cast" else None)
        out = server.combat_turn(json.dumps({"move": self.gob, "action": [{"ability": "Shout_Dash"}]}), end=False)
        self.assertIn("no movement left", out)
        self.assertIn("both were spent earlier in this same turn (not a new turn)", out)

    def test_no_note_when_the_action_was_spent_by_this_plan(self):
        self.g.refuse_if = lambda op, a: "no movement left" if op == "act.move_to" else None
        out = server.combat_turn(json.dumps({"move": self.gob, "action": [{"ability": "Shout_Dash"}]}), end=False)
        self.assertNotIn("spent earlier", out)


class WhoInAFight(Fight):
    def setUp(self) -> None:
        super().setUp()
        self.g.actions = [{"spell": "Shout_Dash", "name": "Dash"}]
        # the selected Claude ended; Lae'zel is the one party member acting now
        self.g.combat.update(current=P2, turn_group=[P2])

    def test_left_out_who_is_the_member_acting_now(self):
        # naut2 run 1 19:59:10: use_ability Shout_Dash, no who → "FAILED — it is not this character's turn"
        out = server.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P2])
        self.assertIn("whose turn it is; the selected", out)
        self.assertTrue(out.rstrip().endswith("can't act now)"))  # last: the result's own first word stays first

    def test_the_selected_member_acting_keeps_it(self):
        self.g.combat.update(current=P1, turn_group=[P1])
        out = server.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])
        self.assertNotIn("whose turn it is", out)

    def test_two_acting_keeps_the_selection(self):
        self.g.combat.update(current=P2, turn_group=[P1, P2])
        server.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])

    def test_out_of_combat_no_combat_read(self):
        self.g.combat = {"active": False}
        self.g.watch["combat"] = False
        server.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])
        self.assertEqual(self.g.ops("state.combat"), [])

    def test_note_does_not_leak_into_the_next_call(self):
        server.use_ability(ability="Shout_Dash")
        out = server.use_ability(ability="Shout_Dash", who=server.H.get(P2))
        self.assertNotIn("whose turn it is", out)

    def test_switch_off(self):
        old, server.COMBAT_WHO_ACTING = server.COMBAT_WHO_ACTING, False
        self.addCleanup(setattr, server, "COMBAT_WHO_ACTING", old)
        server.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])


class WalkTowardClip(Fight):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat.update(current=P1, turn_group=[P1])

    def test_walk_cut_to_the_movement_left(self):
        # naut2 run 1 19:59:37: walk_toward(max_m=9) → "FAILED — beyond movement (path 9.6 m, 9.0 m left)"
        self.g.reach = lambda a: ({"reachable": True, "pos": [9.0, 0.0, 0.0], "straight_m": 9.0, "path_m": 9.6}
                                  if a["max_m"] > 8.5 else
                                  {"reachable": True, "pos": [8.1, 0.0, 0.0], "straight_m": 8.1})
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 9.6 m, 9.0 m left)"
                                          if op == "act.move_to" and a.get("pos") == [9.0, 0.0, 0.0] else None)
        out = server.walk_toward(direction="E", max_m=9, wait=True)
        self.assertEqual([round(a["max_m"], 1) for _, a in self.g.ops("nav.reach_toward")], [9.0, 8.1])
        self.assertEqual([m["pos"] for m in self.moves()], [[9.0, 0.0, 0.0], [8.1, 0.0, 0.0]])
        self.assertIn("cut to the movement left (9m): 8.1m", out)
        self.assertNotIn("FAILED", out)

    def test_walk_cut_along_the_route_with_nav_approach(self):
        # vis (A6): the straight-line re-pick can still need more walking than is left; nav.approach cuts the real
        # route where the spot's own walk fits (the mod checks it)
        self.g.reach = {"reachable": True, "pos": [9.0, 0.0, 0.0], "straight_m": 9.0, "path_m": 9.6}
        self.g.approach = {"reachable": True, "pos": [7.9, 0.0, 1.0], "walk_m": 8.7, "limit": "movement"}
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 9.6 m, 9.0 m left)"
                                          if op == "act.move_to" and a.get("pos") == [9.0, 0.0, 0.0] else None)
        out = server.walk_toward(direction="E", max_m=9, wait=True)
        ap = [a for _, a in self.g.ops("nav.approach")]
        self.assertEqual((ap[-1]["pos"], ap[-1]["budget_m"]), ([9.0, 0.0, 0.0], 8.7))
        self.assertEqual(len(self.g.ops("nav.reach_toward")), 1)  # only the first pick
        self.assertEqual(self.moves()[-1]["pos"], [7.9, 0.0, 1.0])
        self.assertIn("cut to the movement left (9m): 8.7m", out)
        self.assertNotIn("FAILED", out)

    def test_switch_off_fails_as_before(self):
        old, server.WALK_CLIP_MOVEMENT = server.WALK_CLIP_MOVEMENT, False
        self.addCleanup(setattr, server, "WALK_CLIP_MOVEMENT", old)
        self.g.reach = {"reachable": True, "pos": [9.0, 0.0, 0.0], "straight_m": 9.0, "path_m": 9.6}
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 9.6 m, 9.0 m left)"
                                          if op == "act.move_to" else None)
        out = server.walk_toward(direction="E", max_m=9, wait=True)
        self.assertIn("FAILED", out)
        self.assertEqual(len(self.moves()), 1)


class DoActionsDoc(unittest.TestCase):
    def test_travel_to_is_listed(self):
        # ACT-P5: _BATCHABLE has travel_to; the docstring's "Allowed:" list didn't say so
        self.assertIn("travel_to", server._BATCHABLE)
        allowed = server.do_actions.__doc__.split("Allowed:", 1)[1].split("\n", 1)[0]
        self.assertIn("travel_to", allowed)


if __name__ == "__main__":
    unittest.main()
