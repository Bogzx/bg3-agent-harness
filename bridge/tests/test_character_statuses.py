"""get_character shows a condition's name, never the engine's status id, on a party member's sheet and on an examined
character alike (as look_at: observe._shown_statuses). A run's get_character on Astarion served
"id=DIFFICULT_TERRAIN_DEEPWATER" and was flagged a fairness leak; an examined enemy's sheet carried the same ids.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_character_statuses.py
"""

from __future__ import annotations

import unittest

from test_turn_macros import P1, Base

# isort: split
from bg3ai.tools import overview

STATUSES = [
    {"id": "DIFFICULT_TERRAIN_DEEPWATER", "name": "Difficult Terrain: Deep Water", "turns": 1},
    {"id": "GREMISHKA_MAGICALLERGY_AURA", "name": "Magic Allergy"},
]


class CharacterStatuses(Base):
    def setUp(self) -> None:
        super().setUp()
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.character":
                u = (args or {}).get("uuid")
                return {"uuid": u, "name": "Claude" if u == P1 else "Goblin", "level": 2, "statuses": STATUSES}
            return real(op, args, context, timeout)

        self.g.rpc = rpc

    def test_no_status_ids_on_any_sheet(self):
        for who in ("Claude", self.gob):  # a party member's own sheet; a visible non-party character's Examine
            out = overview.get_character(who)
            self.assertIn("Difficult Terrain: Deep Water", out)
            self.assertIn("Magic Allergy", out)
            self.assertNotIn("DIFFICULT_TERRAIN_DEEPWATER", out, who)
            self.assertNotIn("GREMISHKA_MAGICALLERGY_AURA", out, who)


if __name__ == "__main__":
    unittest.main()
