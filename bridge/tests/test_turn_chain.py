"""reporting._auto_combat: the combat state appended to the result of the call that started a fight."""
import unittest
from unittest import mock

from bg3ai.tools import observe, reporting


class AutoCombat(unittest.TestCase):
    def test_fight_start_carries_the_combat_state(self):
        with mock.patch.object(observe, "_combat_state_text", return_value="ROUND 1 order: e1 Karlach, e9 Goblin\nYOUR ACTIONS (e1)"):
            out = reporting._auto_combat("move_to", "move_to: done", "EVENTS\n  combat_started {}")
            self.assertTrue(out.startswith("COMBAT STARTED (no get_combat_state needed):"))
            self.assertIn("YOUR ACTIONS (e1)", out)
            self.assertEqual(reporting._auto_combat("move_to", "move_to: done", "EVENTS\n  died {}"), "")
            self.assertEqual(reporting._auto_combat("move_to", "x\nYOUR ACTIONS (e1)", "  combat_started"), "")


if __name__ == "__main__":
    unittest.main()
