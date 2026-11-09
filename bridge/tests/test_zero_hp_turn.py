"""Offline tests: a party member at 0 HP without the Downed status keeps an open turn only an end_turn moves on.

Windmill 01:39:19: Gale dropped to 0 HP in her own turn. Her row read downed, so the wait after Shadowheart's end_turn
left her out as "its turn passes by itself" and ran its whole 80 s cap with the game waiting on her; wait_for my_turn
then ran out too. A real downed turn (the DOWNED status) does pass by itself, and enemy turns are waited out as before.

Run: cd bridge && uv run python -m unittest tests/test_zero_hp_turn.py
"""

from __future__ import annotations

import unittest

from test_turn_macros import GOB, P1, P2, Base  # (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import turn
from bg3ai.tools import batch, combat, core


def combat_state(current: str = P2, flag: str | None = "zero_hp_turn", relation: str = "party") -> dict:
    """P1 has ended; `current` is the one group member left, its row marked `flag` by the mod (None: neither)."""
    row = {"uuid": current, "name": "Lae'zel", "relation": relation, "is_current": True, "downed": True, "hp_pct": 0}
    if flag:
        row[flag] = True
    ended = {"uuid": P1, "name": "Claude", "relation": "party", "is_current": True, "ended": True}
    return {"active": True, "round": 3, "current": current, "turn_group": [current], "order": [ended, row]}


class Rule(unittest.TestCase):
    PARTY = frozenset((P1, P2))

    def test_the_open_turn_at_zero_hp_is_named(self):
        self.assertEqual(turn.zero_hp_turns(combat_state(), set(self.PARTY)), [P2])

    def test_it_stays_out_of_who_can_act(self):
        self.assertEqual(turn.party_acting(combat_state(), set(self.PARTY)), [])

    def test_a_row_without_the_mark_is_not_named(self):
        self.assertEqual(turn.zero_hp_turns(combat_state(flag=None), set(self.PARTY)), [])

    def test_another_sides_row_is_not_named(self):
        self.assertEqual(turn.zero_hp_turns(combat_state(current=GOB, relation="hostile"), set(self.PARTY)), [])

    def test_a_marked_row_outside_the_current_group_is_not_named(self):
        cs = combat_state()
        cs["turn_group"] = [P1]
        self.assertEqual(turn.zero_hp_turns(cs, set(self.PARTY)), [])

    def test_empty_reads(self):
        self.assertEqual(turn.zero_hp_turns({}, set(self.PARTY)), [])


class Wait(Base):
    """max_s=0 reads the state once and stops: a wait that doesn't return for the row ends in the cap's words."""

    def setUp(self) -> None:
        super().setUp()
        self.g.watch["combat"] = True
        self.e46 = core.H.get(P2)

    def test_end_turns_wait_stops_at_the_members_open_turn(self):
        self.g.combat = combat_state()
        out = combat._wait_for_party_turn(0.0)
        self.assertNotIn("other combatants are still acting", out)
        self.assertIn(f"now {self.e46}'s turn", out)
        self.assertIn("at 0 HP without the Downed status", out)

    def test_it_offers_no_actions_for_that_turn(self):
        self.g.combat = combat_state()
        self.assertNotIn("YOUR ACTIONS", combat._wait_for_party_turn(0.0))

    def test_a_downed_status_turn_is_still_waited_out(self):
        self.g.combat = combat_state(flag=None)
        self.assertEqual(combat._wait_for_party_turn(0.0), "other combatants are still acting")

    def test_an_enemy_turn_is_still_waited_out(self):
        self.g.combat = combat_state(current=GOB, flag=None, relation="hostile")
        self.assertEqual(combat._wait_for_party_turn(0.0), "other combatants are still acting")

    def test_a_marked_enemy_row_is_still_waited_out(self):
        self.g.combat = combat_state(current=GOB, relation="hostile")
        self.assertEqual(combat._wait_for_party_turn(0.0), "other combatants are still acting")

    def test_a_member_who_can_act_comes_first(self):
        cs = combat_state()
        cs["order"].append({"uuid": P1, "name": "Claude", "relation": "party", "is_current": True})
        cs["order"].pop(0)
        cs["turn_group"] = [P2, P1]
        self.g.combat = cs
        out = combat._wait_for_party_turn(0.0)
        self.assertNotIn("Downed status", out)
        self.assertIn(f"now {core.H.get(P1)}'s turn", out)


class WaitFor(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.watch["combat"] = True
        self.e46 = core.H.get(P2)

    def state(self, ui: dict | None = None) -> str | None:
        return batch._wait_state("my_turn", "my_turn", {"my_turn"}, ui or {}, {})

    def test_my_turn_answers_at_the_members_open_turn(self):
        self.g.combat = combat_state()
        out = self.state()
        self.assertIsNotNone(out)
        self.assertIn(f"now {self.e46}'s turn", str(out))
        self.assertIn("at 0 HP without the Downed status", str(out))

    def test_my_turn_keeps_waiting_on_a_downed_status_turn(self):
        self.g.combat = combat_state(flag=None)
        self.assertIsNone(self.state())

    def test_another_sides_prompt_holds_it_as_it_holds_a_turn(self):
        self.g.combat = combat_state()
        self.assertIsNone(self.state({"waiting": True}))


class EndTurn(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.watch["combat"] = True

    def test_end_turn_without_who_ends_the_open_turn_at_zero_hp(self):
        # the selected P1 already ended; only P2's open turn is left
        self.g.combat = combat_state()
        combat.end_turn(wait=False)
        self.assertEqual(self.g.ops("act.end_turn")[0][1]["char"], P2)


if __name__ == "__main__":
    unittest.main()
