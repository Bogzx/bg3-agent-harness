"""Offline tests: vis's think-while-acting proposals for act (naut2 run 1), on the test_turn_macros fake.
ACT-P2 a left-out `who` in a fight, ACT-P3 walk_toward cut to the movement left.
(ACT-P4 early wake: test_pipelining.EarlyWake.)

Run: cd bridge/tests && uv run --project .. python -m unittest test_think_acting
"""

from __future__ import annotations

import unittest

from test_turn_macros import GOB, P1, P2, Base, combat  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai.tools import core, plumbing, observe, combat as combat_tools, batch

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

    def moves(self) -> list[dict]:
        return [a for _, a in self.g.ops("act.move_to")]


class WhoInAFight(Fight):
    def setUp(self) -> None:
        super().setUp()
        self.g.actions = [{"spell": "Shout_Dash", "name": "Dash"}]
        # the selected Claude ended; Lae'zel is the one party member acting now
        self.g.combat.update(current=P2, turn_group=[P2])

    def test_left_out_who_is_the_member_acting_now(self):
        # naut2 run 1 19:59:10: use_ability Shout_Dash, no who → "FAILED — it is not this character's turn"
        out = combat_tools.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P2])
        self.assertIn("whose turn it is; the selected", out)
        self.assertTrue(out.rstrip().endswith("can't act now)"))  # last: the result's own first word stays first

    def test_the_selected_member_acting_keeps_it(self):
        self.g.combat.update(current=P1, turn_group=[P1])
        out = combat_tools.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])
        self.assertNotIn("whose turn it is", out)

    def test_two_acting_keeps_the_selection(self):
        self.g.combat.update(current=P2, turn_group=[P1, P2])
        combat_tools.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])

    def test_out_of_combat_no_combat_read(self):
        self.g.combat = {"active": False}
        self.g.watch["combat"] = False
        combat_tools.use_ability(ability="Shout_Dash")
        self.assertEqual([a["char"] for _, a in self.g.ops("act.cast")], [P1])
        self.assertEqual(self.g.ops("state.combat"), [])

    def test_note_does_not_leak_into_the_next_call(self):
        combat_tools.use_ability(ability="Shout_Dash")
        out = combat_tools.use_ability(ability="Shout_Dash", who=core.H.get(P2))
        self.assertNotIn("whose turn it is", out)

    def test_switch_off(self):
        old, plumbing.COMBAT_WHO_ACTING = plumbing.COMBAT_WHO_ACTING, False
        self.addCleanup(setattr, plumbing, "COMBAT_WHO_ACTING", old)
        combat_tools.use_ability(ability="Shout_Dash")
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
        out = observe.walk_toward(direction="E", max_m=9, wait=True)
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
        out = observe.walk_toward(direction="E", max_m=9, wait=True)
        ap = [a for _, a in self.g.ops("nav.approach")]
        self.assertEqual((ap[-1]["pos"], ap[-1]["budget_m"]), ([9.0, 0.0, 0.0], 8.7))
        self.assertEqual(len(self.g.ops("nav.reach_toward")), 1)  # only the first pick
        self.assertEqual(self.moves()[-1]["pos"], [7.9, 0.0, 1.0])
        self.assertIn("cut to the movement left (9m): 8.7m", out)
        self.assertNotIn("FAILED", out)

    def test_switch_off_fails_as_before(self):
        old, observe.WALK_CLIP_MOVEMENT = observe.WALK_CLIP_MOVEMENT, False
        self.addCleanup(setattr, observe, "WALK_CLIP_MOVEMENT", old)
        self.g.reach = {"reachable": True, "pos": [9.0, 0.0, 0.0], "straight_m": 9.0, "path_m": 9.6}
        self.g.refuse_if = lambda op, a: ("destination is beyond remaining movement (path 9.6 m, 9.0 m left)"
                                          if op == "act.move_to" else None)
        out = observe.walk_toward(direction="E", max_m=9, wait=True)
        self.assertIn("FAILED", out)
        self.assertEqual(len(self.moves()), 1)


class DoActionsDoc(unittest.TestCase):
    def test_travel_to_is_listed(self):
        # ACT-P5: _BATCHABLE has travel_to; the docstring's "Allowed:" list didn't say so
        self.assertIn("travel_to", batch._BATCHABLE)
        allowed = batch.do_actions.__doc__.split("Allowed:", 1)[1].split("\n", 1)[0]
        self.assertIn("travel_to", allowed)


if __name__ == "__main__":
    unittest.main()
