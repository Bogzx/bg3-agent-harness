"""Offline tests: the story turn counter under the minimap is shown on combat reads (vis V29).

S091 (claude-run-2, Netherbrain try 4) 08:19-08:33: inside the Netherbrain's psyche the HUD showed "The brain is
resisting the Chains of Karsus! Defeat it before it breaks free! 4 Turns Remaining." with a ring counting down,
seen only on screenshots; the try ran out of turns with the brain at its last 28 HP. The client reads that counter
(ui.story_timer: Minimap's StoryTimer) and get_combat_state and each TURN block carry it as a TURN LIMIT line.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest

from test_disabled import s076_combat
from test_turn_macros import P1, Base

from bg3ai import render
from bg3ai.tools import core, observe, turn_tool

PSYCHE = {"text": "The brain is resisting the Chains of Karsus! Defeat it before it breaks free! 4 Turns Remaining.",
          "turns_left": 4}
LINE = ("TURN LIMIT (the counter on screen): 4 turns left — The brain is resisting the Chains of Karsus! Defeat it "
        "before it breaks free! 4 Turns Remaining.")


class TurnLimitLine(unittest.TestCase):
    def test_line(self):
        self.assertEqual(render.turn_limit_line(PSYCHE), LINE)
        self.assertEqual(render.turn_limit_line({"turns_left": 1}), "TURN LIMIT (the counter on screen): 1 turn left")
        self.assertEqual(render.turn_limit_line({"text": "Defeat it!"}), "TURN LIMIT (the counter on screen) — Defeat it!")

    def test_none_on_screen(self):
        for t in (None, {"none": True}, [], {}):  # Lua's empty table can come back as []
            self.assertEqual(render.turn_limit_line(t), "")


class CombatReads(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = s076_combat()
        self.g.watch["combat"] = True
        self.g.actions = {"actions": [{"name": "Ranged Attack", "spell": "Projectile_MainHandAttack"}],
                          "resources": {"ActionPoint": [1, 1]}}

    def test_get_combat_state_carries_it(self):
        self.g.story_timer = dict(PSYCHE)
        self.assertIn(LINE, observe.get_combat_state())

    def test_turn_block_carries_it(self):
        self.g.story_timer = dict(PSYCHE)
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1), end=False)
        self.assertIn(LINE, out)

    def test_no_counter_no_line(self):
        self.g.story_timer = {"none": True}
        self.assertNotIn("TURN LIMIT", observe.get_combat_state())
        self.g.story_timer = None  # an old mod without ui.story_timer
        self.assertNotIn("TURN LIMIT", turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}),
                                                          who=core.H.get(P1), end=False))


if __name__ == "__main__":
    unittest.main()
