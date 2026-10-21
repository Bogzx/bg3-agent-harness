"""Offline tests: floor plates (vis V25, S084 04:16-04:22 Counting House vault: 9 Marble Plates, code 1-3-5-6;
every walk crossed other plates, companions shuffled onto them, 7 of the 9 were 'scenery objects not listed').
move_to onto a plate keeps the companions put; get_nearby lists every plate with who stands on it.
The walk itself (onto the centre, round seen plates) is tested in tools/dev/test_act_lua.lua.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

from test_turn_macros import (  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
    GOB,
    P1,
    P2,
    Base,
)

from bg3ai import render
from bg3ai.tools import core, overview, world_tools

PLATE = "a1a1a1a1-bbbb-cccc-dddd-eeeeeeeeeeee"


class PlateStep(Base):
    def setUp(self) -> None:
        super().setUp()
        overview._stay.clear()
        self.addCleanup(overview._stay.clear)
        self.plate = core.H.get(PLATE, "Marble Plate")  # listed by get_nearby: the bridge knows its name
        base = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.status":  # Lae'zel chained to Claude: she follows Claude's walks
                return {"active": P1, "party": [{"uuid": P1, "name": "Claude"},
                                                {"uuid": P2, "name": "Lae'zel", "chained": True}]}
            return base(op, args, context, timeout)
        self.g.rpc = rpc

    def test_a_step_onto_a_plate_keeps_the_companions_put(self):
        out = world_tools.move_to(target=self.plate)
        self.assertEqual([a["char"] for _, a in self.g.ops("act.unchain")], [P2])
        self.assertIn(P2, overview._stay)  # look/get_status don't re-chain her onto the plates
        self.assertIn("staying off the plates: e5 Lae'zel (regroup brings them back)", out)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["target"], PLATE)

    def test_a_move_to_anything_else_leaves_the_party_as_it_is(self):
        out = world_tools.move_to(target=self.gob)
        self.assertEqual(self.g.ops("act.unchain"), [])
        self.assertNotIn("staying off the plates", out)
        self.assertEqual(overview._stay, set())


class PlateListing(Base):
    def test_every_plate_is_listed_with_who_stands_on_it(self):
        plates = [{"uuid": f"a{i}a1a1a1-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Marble Plate", "kind": "plate",
                   "dist": 2.0 + i} for i in range(4)]
        plates[1]["on"] = [P1]
        out = render.render_nearby({"characters": [], "items": plates}, core.H)
        self.assertEqual(out.count("Marble Plate <plate>"), 4, out)  # one line each, never "(+3 more ...)"
        self.assertIn("Marble Plate <plate> 3m [on it: e4 Claude]", out)
        self.assertNotIn("scenery objects not listed", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
