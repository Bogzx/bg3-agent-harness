"""facts-text-3 (website ops/logs/facts-text-3.md): results state facts, not advice or the next call to make.

One test per representative row; each failed on d0041904, where the text carried the advice."""

from __future__ import annotations

import unittest

from bg3ai import clicker, economy, macros, navhints
from bg3ai.game import JobResult
from bg3ai.tools import inventory, plumbing
from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME first)


class GameplayAdvice(unittest.TestCase):
    def test_a6_a7_alerts_name_the_shortfall_only(self):
        inv = {"items": [], "supplies": 12, "gold": 50, "members": {"u1": {"enc": 1}}}
        out = economy.alerts({}, inv)
        self.assertIn("camp supplies 12/40 carried", out)
        self.assertIn(f"gold 50 < {economy.GOLD_RESERVE} reserve", out)
        self.assertIn("u1 Encumbered", out)

    def test_a15_crossing_refusal_names_the_option_as_a_fact(self):
        ok, why = macros.crossing_ok({"surface": "Fire", "m": 3}, 3, 20)
        self.assertFalse(ok)
        self.assertTrue(why.endswith("; travel_to(avoid_hazards='cross') crosses anyway"), why)
        self.assertNotIn("heal first", why)

    def test_a12_no_disarm_kit_says_where_one_comes_from(self):
        self.assertEqual(clicker.TOOL_NEEDED["act.disarm"][2], "traders sell Trap Disarm Toolkits")


class NextCallImperatives(unittest.TestCase):
    def test_b1_rest_flag_says_what_turns_it_off(self):
        self.assertEqual(
            inventory._REST_FLAG_TEXT["FTB"], 'turn-based mode is on (set_party(action="turn_based_off") turns it off)'
        )

    def test_b46_too_far_jump_without_a_plan_stops_at_the_fact(self):
        out = navhints.jump_refusal({"too_far": True, "dist_m": 9, "max_m": 4}, [0.0, 0.0, 0.0])
        self.assertTrue(out.endswith("this character can jump 4 m). No walk gets within jumping distance of it."), out)

    def test_b48_still_running_job(self):
        text = plumbing._job_text("move_to", JobResult("still_running", None, None))
        self.assertEqual(text, "move_to: still running")


if __name__ == "__main__":
    unittest.main()
