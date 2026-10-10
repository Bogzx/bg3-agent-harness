"""Offline tests: action lists show names only; use_ability / combat_turn take the shown name.

Run: cd bridge/tests && uv run python -m unittest test_ability_names
"""

from __future__ import annotations

import json

from test_turn_macros import GOB, NOT_JOBS, P1, Base, combat  # noqa: F401  (sets BG3AI_RUNTIME first)

from bg3ai import render, server
from bg3ai.game import GameError

ACTS = {"resources": {"ActionPoint": [1, 1]}, "actions": [
    {"spell": "Target_MainHandAttack", "name": "Attack"},
    {"spell": "Projectile_FireBolt", "name": "Fire Bolt", "range": 18},
    {"spell": "Target_Smite_A", "name": "Smite"}, {"spell": "Target_Smite_B", "name": "Smite"}]}


class Names(Base):
    def setUp(self):
        super().setUp()
        server._ability_ids.clear()
        self.g.actions = json.loads(json.dumps(ACTS))

    def test_list_drops_ids_except_shared_names(self):
        out = render.render_actions(ACTS, server.H)
        self.assertIn("Fire Bolt 18m", out)
        self.assertNotIn("Projectile_FireBolt", out)
        self.assertIn("Smite (Target_Smite_A)", out)

    def test_resolution(self):
        self.assertEqual(server._ability_id(P1, "fire bolt"), "Projectile_FireBolt")
        self.assertEqual(server._ability_id(P1, "Fire Bolt (Projectile_FireBolt)"), "Projectile_FireBolt")
        self.assertEqual(server._ability_id(P1, "Shout_Dash"), "Shout_Dash")  # ids pass through
        self.assertEqual(len(self.g.ops("state.actions")), 1)  # one read, then cached
        with self.assertRaisesRegex(GameError, "2 actions called 'Smite'"):
            server._ability_id(P1, "Smite")
        with self.assertRaisesRegex(GameError, "no action called 'Fireball'"):
            server._ability_id(P1, "Fireball")

    def test_use_ability_by_name(self):
        self.g.combat = combat()
        server.use_ability("Fire Bolt", target=self.gob)
        self.assertEqual([a["spell"] for _, a in self.g.ops("act.cast")], ["Projectile_FireBolt"])

    def test_combat_turn_plan_by_name(self):
        self.g.combat = combat()
        self.g.watch["combat"] = True
        self.g.on_start = lambda op, a: op == "act.end_turn" and self.g.combat.update(current=GOB)
        server.combat_turn(json.dumps({"action": {"ability": "Attack", "target": self.gob}}))
        self.assertEqual([a["spell"] for _, a in self.g.ops("act.cast")], ["Target_MainHandAttack"])
