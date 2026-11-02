"""render.render_actions: the YOUR ACTIONS lines of the one-action pilot (ops/logs/live.md '# fight-flow pilot', 4 runs
of fights-v09-pilot-fight_act1_grove_gate): no stat placeholder as a range, named resources in words, one cost order.

Run: cd bridge && uv run python -m unittest tests/test_actions_text.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bg3ai import compact, render


def actions(*acts: dict) -> dict:
    return {
        "resources": {"ActionPoint": [1, 1], "BonusActionPoint": [1, 1], "ChannelDivinity": [1, 1]},
        "actions": list(acts),
    }


def text(*acts: dict) -> str:
    return render.render_actions(actions(*acts), render.Handles())


class ActionsText(unittest.TestCase):
    def test_an_unresolved_range_is_not_printed(self):
        # pilot: "Throw [A] ThrownObjectRangem point" (an older mod sent the stat's range name)
        out = text(
            {
                "spell": "Throw_Throw",
                "name": "Throw",
                "cost": {"action": 1},
                "target": "point",
                "range": "ThrownObjectRange",
            }
        )
        self.assertIn("Throw [A] point", out)
        self.assertNotIn("Range", out)
        self.assertIn(
            "Fire Bolt [A] 18m",
            text({"spell": "Projectile_FireBolt", "name": "Fire Bolt", "cost": {"action": 1}, "range": 18}),
        )

    def test_named_resources_in_words(self):
        # pilot: "Invoke Duplicity [A+other:{'ChannelDivinity': 1}] 9m"
        out = text(
            {
                "spell": "Target_InvokeDuplicity",
                "name": "Invoke Duplicity",
                "cost": {"action": 1, "other": {"ChannelDivinity": 1}},
                "range": 9,
            },
            {"spell": "Shout_HolyRebuke", "name": "Holy Rebuke", "cost": {"reaction": 1, "other": {"ChannelOath": 1}}},
            {
                "spell": "Target_LayOnHands",
                "name": "Lay on Hands",
                "cost": {"action": 1, "other": {"LayOnHandsCharge": 1}},
                "range": 1.5,
            },
        )
        self.assertIn("Invoke Duplicity [A+Channel Divinity 1] 9m", out)
        self.assertIn("Holy Rebuke [R+Channel Oath 1]", out)
        self.assertIn("Lay on Hands [A+Lay On Hands Charge 1] 1.5m", out)
        self.assertNotIn("other", out)
        self.assertNotIn("{", out)
        self.assertIn("NOW Action 1/1 · Bonus 1/1 · Channel Divinity 1/1", out)  # the same name as in the costs

    def test_one_cost_order_whatever_order_the_mod_sends(self):
        # pilot: "Cure Wounds [slot L1+A]" in one run, "[A+slot L1]" in another (Lua pairs order)
        a = {"spell": "Target_CureWounds", "name": "Cure Wounds", "cost": {"slot": 1, "action": 1}, "range": 1.5}
        b = {**a, "cost": {"action": 1, "slot": 1}}
        self.assertEqual(text(a), text(b))
        self.assertIn("Cure Wounds [A+slot L1] 1.5m", text(a))
        c = {"spell": "Shout_X", "name": "X", "cost": {"other": {"KiPoint": 1}, "movement": 3, "bonus": 1, "slot": 2}}
        self.assertIn("X [B+3m+slot L2+Ki Point 1]", text(c))

    def test_the_same_list_in_another_order_folds(self):
        # ACTIONS_AGAIN folds an identical list: with the costs in Lua's order the "same" list differed between reads
        def turn(order: tuple[str, str]) -> str:
            acts = [
                {"spell": f"Target_S{i}", "name": f"Spell {i}", "cost": dict.fromkeys(order, 1), "range": 18}
                for i in range(9)
            ]
            return "YOUR ACTIONS (e2)\n" + render.render_actions(actions(*acts), render.Handles())

        c = compact.Compactor(clock=lambda: 0.0)
        c.apply("end_turn", turn(("slot", "action")))
        self.assertIn("(actions as listed before)", c.apply("end_turn", turn(("action", "slot"))))


if __name__ == "__main__":
    unittest.main()
