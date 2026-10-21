"""Offline tests: turn-based mode outside combat (S021 burning inn, S024-S026 Shadow-Cursed Lands). The game forced
the party into turn-based mode; walks failed with 0 movement and the agent hand-clicked the HUD's Exit TB button
(0.87, 0.927) 2-10x per session; set_party(turn_based_off) timed out (host only). Walk ops now leave it first via
the HUD button (HotBar ExitFTBButton), falling back to ForceTurnBasedMode for the whole party.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from test_turn_macros import P1, Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai.tools import plumbing, world_tools, travel, batch  # noqa: E402
from bg3ai.game import GameError  # noqa: E402


class TurnBasedOutsideCombat(Base):
    def setUp(self) -> None:
        super().setUp()
        self.mode = {"ftb": True, "combat": False, "ftb_members": [P1]}
        self.button_works = True
        self.osiris_works = True
        self.presses: list[str] = []
        plumbing._tb_wanted["on"] = False
        saved = plumbing.TB_EXIT_WAIT_S
        plumbing.TB_EXIT_WAIT_S = 0.3
        self.addCleanup(setattr, plumbing, "TB_EXIT_WAIT_S", saved)
        real = self.g.rpc

        def rpc(op, a=None, context="server", timeout=12.0):
            if op == "act.mode":
                if self.mode is None:
                    raise GameError("unknown op act.mode")  # a mod not reloaded yet
                return dict(self.mode)
            if op == "ui.press":
                self.presses.append(a["button"])
                if self.button_works:
                    self.mode["ftb"] = a["button"] == "EnterFTBButton"
                return {"done": True, "page": "HotBar", "via": "command"}
            return real(op, a, context, timeout)
        self.g.rpc = rpc

        def refuse(op, a):  # Actions.lua A.tb_gate
            if op == "act.turn_based" and not self.osiris_works:
                return "timed out"
            if (op in plumbing._WALK_OPS and self.mode and self.mode["ftb"] and not self.mode["combat"]
                    and not a.get("tb_ok")):
                return "turn-based mode outside combat (the game switched it on): a walk gets only the turn's movement"
            return None
        self.g.refuse_if = refuse

        def started(op, a):
            if op == "act.turn_based" and self.osiris_works:
                self.mode["ftb"] = bool(a.get("on"))
        self.g.on_start = started

    def test_a_walk_leaves_turn_based_mode_first_with_the_hud_button(self):
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertEqual(self.presses, ["ExitFTBButton"])
        self.assertIn("left it first: pressed the HUD's Exit TB button", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 2)  # refused (A.tb_gate), then walked
        self.assertFalse(self.g.ops("act.turn_based"))
        self.assertNotIn("FAILED", out)

    def test_no_turn_based_mode_means_nothing_extra(self):
        self.mode["ftb"] = False
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertEqual(self.presses, [])
        self.assertNotIn("turn-based", out)

    def test_in_combat_nothing_is_pressed(self):
        self.mode["combat"] = True
        world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertEqual(self.presses, [])

    def test_no_extra_calls_on_a_normal_walk(self):
        self.mode["ftb"] = False
        n0 = len(self.g.calls)
        world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertNotIn("act.mode", [c[0] for c in self.g.calls[n0:]])

    def test_button_without_effect_falls_back_to_osiris_for_the_party(self):
        self.button_works = False
        out = world_tools.interact(self.chest)
        self.assertEqual(len(self.g.ops("act.turn_based")), 1)
        self.assertEqual(self.g.ops("act.turn_based")[0][1], {"on": False})
        self.assertIn("then ForceTurnBasedMode for the party", out)

    def test_both_ways_fail_say_so_and_how_to_get_out(self):
        self.button_works = self.osiris_works = False
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertIn("STILL IN TURN-BASED MODE outside combat", out)
        self.assertIn("Exit TB button", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 2)  # refused, then retried once (refused again)

    def test_a_walk_inside_do_actions_and_a_waited_walk(self):
        out = batch.do_actions('[{"tool": "move_to", "x": 5, "y": 0, "z": 0}]')
        self.assertNotIn("FAILED", out)
        self.assertEqual(self.presses, ["ExitFTBButton"])
        self.mode["ftb"] = True
        out = world_tools.interact(self.chest, wait=True)
        self.assertIn("left it first", out)

    def test_set_party_turn_based_off_presses_the_hud_button(self):
        out = travel.set_party("turn_based_off")
        self.assertIn("set_party: done — turn-based mode off (pressed the HUD's Exit TB button)", out)
        self.assertFalse(self.g.ops("act.turn_based"))

    def test_set_party_turn_based_on(self):
        self.mode["ftb"] = False
        out = travel.set_party("turn_based_on")
        self.assertEqual(self.presses, ["EnterFTBButton"])
        self.assertIn("set_party: done — turn-based mode on", out)

    def test_turn_based_the_agent_chose_is_kept_on_walks(self):
        self.mode["ftb"] = False
        travel.set_party("turn_based_on")
        world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertEqual(self.presses, ["EnterFTBButton"])  # no Exit press before the walk
        self.assertTrue(self.g.ops("act.move_to")[-1][1].get("tb_ok"))
        travel.set_party("turn_based_off")
        self.mode["ftb"] = True  # the game forces it again later
        world_tools.move_to(x=6.0, y=0.0, z=0.0)
        self.assertEqual(self.presses, ["EnterFTBButton", "ExitFTBButton", "ExitFTBButton"])

    def test_set_party_in_combat_refuses(self):
        self.mode["combat"] = True
        out = travel.set_party("turn_based_off")
        self.assertIn("FAILED — in combat", out)
        self.assertEqual(self.presses, [])


if __name__ == "__main__":
    unittest.main()
