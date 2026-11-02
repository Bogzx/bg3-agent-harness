"""Offline tests: shared party turn groups (live bug, claude-run-2 S001 on the Nautiloid).

Claude and Lae'zel had equal initiative, so the game made them one turn group: both were current
(`is_current`), `state.combat.current` named only one of them, and a member that ended its turn kept
IsActiveCombatTurn with RequestedEndTurn set until the other one ended too. The bridge refused
combat_turn who=Lae'zel ("not e46's turn (now e1 Claude)") and let Claude act again after ending.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from test_turn_macros import GOB, IMP, P1, P2, Base

from bg3ai import turn
from bg3ai.tools import core, combat


def group_combat(ended: tuple = ()) -> dict:
    """The live shape: Lae'zel listed first, both current, imps after them."""
    def member(u, name):
        o = {"uuid": u, "name": name, "relation": "party", "is_current": True, "initiative": 4}
        if u in ended:
            o["ended"] = True
        return o
    group = [u for u in (P2, P1) if u not in ended]
    return {"active": True, "round": 1, "current": group[0] if group else P2, "turn_group": group,
            "party_can_act": group, "order": [
                member(P2, "Lae'zel"), member(P1, "Claude"),
                {"uuid": GOB, "name": "Imp", "relation": "hostile", "initiative": -19,
                 "from": {P1: [2.2, 1], P2: [7.0, 1]}},
                {"uuid": IMP, "name": "Imp", "relation": "hostile", "initiative": -19}]}


class TurnHelpers(unittest.TestCase):
    def test_acting_uses_the_group(self):
        self.assertEqual(turn.acting(group_combat()), [P2, P1])
        self.assertEqual(turn.acting(group_combat(ended=(P1,))), [P2])

    def test_acting_falls_back_to_flags_then_current(self):
        cs = group_combat()
        del cs["turn_group"]  # an older mod
        self.assertEqual(turn.acting(cs), [P2, P1])
        self.assertEqual(turn.acting({"active": True, "current": GOB, "order": []}), [GOB])
        self.assertEqual(turn.acting({}), [])

class SharedTurn(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = group_combat()
        self.g.watch["combat"] = True

        def end(op, args):  # ending one member's turn: it is marked ended, the group goes on
            if op == "act.end_turn":
                ended = tuple(o["uuid"] for o in self.g.combat["order"] if o.get("ended")) + (args["char"],)
                self.g.combat = group_combat(ended=ended)
        self.g.on_start = end
        self.e1 = core.H.get(P1)
        self.e46 = core.H.get(P2)

    def test_wait_for_party_turn_names_the_whole_group(self):
        text = combat._wait_for_party_turn(1.0)
        self.assertIn(self.e46, text)
        self.assertIn(self.e1, text)
        self.assertIn("shared turn group", text)

    def test_end_turn_without_who_ends_the_one_who_can_act(self):
        # The game moved the selection? Here the selected Claude already ended: end_turn() means Lae'zel.
        self.g.combat = group_combat(ended=(P1,))
        out = combat.end_turn(wait=False)
        self.assertIn(f"end_turn {self.e46} Lae'zel: done", out)
        self.assertEqual(self.g.ops("act.end_turn")[0][1]["char"], P2)


if __name__ == "__main__":
    unittest.main()
