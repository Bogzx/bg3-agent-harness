"""Offline tests: the flee tool (research/ovh/live2.md §2: the HUD's Flee sends one character to camp once every
enemy is 27 m away; the game asks "Do you want to flee from combat?" first). The UI op sequence against a fake mod.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_flee.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import boxclick, hoverclick
from bg3ai.render import Handles
from bg3ai.tools import combat, core, world_tools

P1, P2, GOB = (f"00000000-0000-0000-0000-{n:012d}" for n in (1, 2, 50))


class FakeInput:
    def __init__(self):
        self.at = (0, 0)
        self.clicks: list[tuple[int, int]] = []

    def window_size(self):
        return (1000, 1000)

    def move(self, x, y, sync=True):
        self.at = (x, y)

    def click(self, x, y, button="L", count=1, sync=True):
        self.clicks.append((x, y))

    def wait_frames(self, n, sync=True):
        pass


class Flee(unittest.TestCase):
    def setUp(self):
        saved = (core.H, core.game, world_tools._inp, combat.FLEE_BOX_S, combat.FLEE_LEFT_S)

        def restore():
            core.H, core.game, world_tools._inp, combat.FLEE_BOX_S, combat.FLEE_LEFT_S = saved
            hoverclick._hits.clear()

        self.addCleanup(restore)
        core.H = Handles()
        combat.FLEE_BOX_S = combat.FLEE_LEFT_S = 0.0
        self.ops: list[tuple[str, dict]] = []
        self.gob_m = 40.0
        self.commands = [{"widget": "HotBar", "name": "RequestFlee", "can": True}]
        self.command_works = True
        self.button = None  # {"over_at": (x, y)} when the HUD has a findable Flee button
        self.box = False
        self.fled = False
        self.inp = FakeInput()
        test = self

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                test.ops.append((op, dict(args or {})))
                if op == "state.combat":
                    order = [
                        {"uuid": P2, "name": "Karlach", "relation": "party"},
                        {"uuid": GOB, "name": "Goblin", "relation": "hostile", "from": {P1: [test.gob_m, 1]}},
                    ]
                    if not test.fled:
                        order.insert(0, {"uuid": P1, "name": "Claude", "relation": "party"})
                    return {"active": True, "round": 2, "order": order}
                if op == "ui.selected":
                    return {"uuid": P1}
                if op == "state.status":
                    return self.rpc(op)
                return default

            def rpc(self, op, args=None, context="server", **k):
                test.ops.append((op, dict(args or {})))
                if op == "ui.flee_probe":
                    b = test.button
                    return {
                        "commands": test.commands,
                        "button": b
                        and {
                            "widget": "HotBar",
                            "name": "FleeBtn",
                            "over": test.inp.at == b["over_at"],
                            "enabled": True,
                        },
                    }
                if op == "ui.flee":
                    test.box = test.command_works
                    return {"pressed": args["name"]}
                if op == "ui.box_open":
                    if test.button and test.inp.clicks:
                        test.box = True
                    return {"box": "Dialog_box"} if test.box else {}
                return {"active": P1, "party": [{"uuid": P1, "name": "Claude"}, {"uuid": P2, "name": "Karlach"}]}

            def run_job(self, op, args, context="server", wait_s=25.0):
                test.ops.append((op, dict(args)))
                raise AssertionError(f"unexpected job {op}")

            def drain_events(self, ctx):
                return []

        core.game = G()
        world_tools._inp = lambda: self.inp
        core.H.get(P1, "Claude")
        core.H.get(GOB, "Goblin")

        def click_box(rpc, inp, answer="yes", **kw):
            test.ops.append(("click_box", {"answer": answer}))
            test.fled = test.box
            return {"clicked": "Yes", "closed": True, "tries": 1} if test.box else {"gone": True, "tries": 0}

        patcher = mock.patch.object(boxclick, "click_box", click_box)
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self) -> list[str]:
        return [op for op, _ in self.ops if op not in ("state.status", "ui.selected")]

    def test_command_then_yes(self):
        out = combat.flee()
        self.assertIn("fled: Claude left the fight and is at camp; still fighting: Karlach", out)
        self.assertEqual(
            self.names(), ["state.combat", "ui.flee_probe", "ui.flee", "ui.box_open", "click_box", "state.combat"]
        )
        self.assertEqual(self.inp.clicks, [])

    def test_enemy_too_close_is_refused_before_any_ui(self):
        # fair: the distance is to a hostile Claude sees (state.combat lists only seen combatants)
        self.gob_m = 5.5
        out = combat.flee()
        self.assertIn("REFUSED: enemy too close: e2 Goblin at 5.5 m", out)
        self.assertEqual(self.names(), ["state.combat"])

    def test_not_in_a_fight(self):
        self.fled = True
        self.assertIn("REFUSED: Claude is not in a fight", combat.flee())

    def test_command_does_nothing_then_the_button_is_clicked(self):
        self.command_works = False
        self.button = {"over_at": (831, 922)}  # under FLEE_SEED
        with mock.patch.object(hoverclick, "PROBE_GAP_S", 0.0):
            out = combat.flee()
        self.assertIn("fled: Claude", out)
        self.assertEqual(self.inp.clicks, [(831, 922)])

    def test_greyed_out_and_no_button(self):
        self.commands = [{"widget": "HotBar", "name": "RequestFlee", "can": False}]
        out = combat.flee()
        self.assertIn("FAILED: the game won't flee now", out)
        self.assertNotIn("ui.flee", self.names())
        self.assertNotIn("click_box", self.names())

    def test_no_question_no_yes(self):
        self.command_works = False
        out = combat.flee()
        self.assertIn("FAILED: the game didn't ask to flee", out)
        self.assertNotIn("click_box", self.names())
        self.commands = []
        self.assertIn("FAILED: no Flee button on the HUD", combat.flee())

    def test_yes_but_still_in_the_fight(self):
        def click_box(rpc, inp, answer="yes", **kw):
            return {"clicked": "Yes", "closed": True, "tries": 1}

        with mock.patch.object(boxclick, "click_box", click_box):
            out = combat.flee()
        self.assertIn("FAILED: pressed Flee (RequestFlee) and answered Yes, but Claude is still in the fight", out)


if __name__ == "__main__":
    unittest.main()
