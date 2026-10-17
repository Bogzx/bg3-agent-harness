"""Offline tests: a party member's disabling status is shown on every combat read (vis V18).

S076 (claude-run-2, Cazador try 7) 00:39:38: Chatterteeth's Eyebite put Gale to sleep. `e3 Gale +Sleeping` showed
once, in a CHANGES line; every later read (NOW/YOUR ACTIONS, NEXT, the TURN blocks) carried no statuses. The agent
planned her Scorching Ray in rounds 4, 5 and 6 ("Gale is awake now", 00:42:04) and nobody woke her with Help.
state.combat now lists a party member's disabling statuses (`disabled`, Guard.disabling) and the bridge prints a
DISABLED line with the game's fix on each combat read.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest

from test_turn_macros import GOB, P1, P2, Base

from bg3ai import compact, server, turn

# Guard.disabling's entry for SLEEPING_EYEBITE (using SLEEPING_MAGICAL -> SLEEPING: SG_Sleeping, RemoveEvents OnDamage)
SLEEPING = {"name": "Sleeping", "turns": 7, "effect": "cannot act",
            "fix": "an adjacent ally's Help (an action) ends it; any damage ends it"}
GALE_TAIL = "Lae'zel: Sleeping (7 turns): cannot act; an adjacent ally's Help (an action) ends it; any damage ends it"
GALE_LINE = f"DISABLED e46 {GALE_TAIL}"


def s076_combat(current: str = P1, gale_current: bool = False) -> dict:
    """Shadowheart (P1) acting, the Chatterteeth (GOB) next, then Gale (P2) asleep."""
    gale = {"uuid": P2, "name": "Lae'zel", "relation": "party", "hp_pct": 74, "disabled": [dict(SLEEPING)]}
    if gale_current:
        gale.update(is_current=True)
    return {"active": True, "round": 3, "current": current, "order": [
        {"uuid": P1, "name": "Claude", "relation": "party", "hp_pct": 100, "is_current": current == P1},
        {"uuid": GOB, "name": "Goblin", "relation": "hostile", "from": {P1: [6.0, 1], P2: [9.0, 1]}},
        gale]}


class DisabledLines(unittest.TestCase):
    def label(self, u: str) -> str:
        return {P1: "e1 Claude", P2: "e46 Lae'zel"}.get(u, u)

    def test_one_line_per_disabled_member(self):
        self.assertEqual(turn.disabled_lines(s076_combat(), self.label), [GALE_LINE])
        self.assertEqual(turn.disabled_lines(s076_combat(), self.label, only=P1), [])
        self.assertEqual(turn.disabled_lines(s076_combat(), self.label, only=P2), [GALE_LINE])

    def test_several_statuses_and_no_fix(self):
        cs = s076_combat()
        cs["order"][2]["disabled"] = [{"name": "Prone", "effect": "stands up at its turn start for half its movement",
                                       "fix": "an adjacent ally's Help (an action) ends it"},
                                      {"name": "Lethargic", "turns": 1}]
        self.assertEqual(turn.disabled_lines(cs, self.label), [(
            "DISABLED e46 Lae'zel: Prone: stands up at its turn start for half its movement; an adjacent ally's Help "
            "(an action) ends it | Lethargic (1 turn)")])

    def test_dead_or_unaffected_members_have_no_line(self):
        cs = s076_combat()
        cs["order"][2]["dead"] = True
        self.assertEqual(turn.disabled_lines(cs, self.label), [])
        cs["order"][2].pop("dead")
        cs["order"][2]["disabled"] = None  # Lua: nil when nothing disables it
        self.assertEqual(turn.disabled_lines(cs, self.label), [])
        self.assertEqual(turn.disabled_lines({}, self.label), [])


class EveryCombatRead(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.combat = s076_combat()
        self.g.watch["combat"] = True

        def advance(op, args):  # Shadowheart's end hands the turn to the Chatterteeth
            if op == "act.end_turn":
                self.g.combat = s076_combat(current=GOB)
        self.g.on_start = advance
        self.e46 = server.H.get(P2)
        self.line = f"DISABLED {self.e46} {GALE_TAIL}"
        self.g.actions = {"actions": [{"name": "Scorching Ray", "spell": "Projectile_ScorchingRay"}],
                          "resources": {"ActionPoint": [1, 1]}}  # Action 1/1 while asleep, as in S076

    def test_turn_block_next_line_carries_it(self):
        # 00:39:38 shape: Shadowheart's TURN block (her Help went to Karlach), the NEXT line names Gale
        out = server.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=server.H.get(P1))
        self.assertIn("end_turn: done", out)
        self.assertIn(f"then {self.e46} Lae'zel (yours)", out)
        self.assertIn(self.line, out)

    def test_now_line_of_the_sleeping_member_carries_it(self):
        # rounds 4-6: "NOW e3's turn" + YOUR ACTIONS (Action 1/1) with no word of the sleep
        def advance(op, args):
            if op == "act.end_turn":
                self.g.combat = s076_combat(current=P2, gale_current=True)
        self.g.on_start = advance
        out = server.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=server.H.get(P1))
        self.assertIn(f"NOW {self.e46} Lae'zel's turn: plan it\n{self.line}\nYOUR ACTIONS ({self.e46})", out)

    def test_interrupted_turn_carries_it(self):
        out = server.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=server.H.get(P1), end=False)
        self.assertIn(self.line, out)

    def test_get_combat_state_carries_it_once(self):
        out = server.get_combat_state()
        self.assertEqual(out.count("Sleeping"), 1, out)
        self.assertIn(self.line, out)
        self.assertNotIn("disabled=", out)

    def test_wait_for_party_turn_carries_it(self):
        self.g.combat = s076_combat(current=P2, gale_current=True)
        self.assertIn(self.line, server._wait_for_party_turn(1.0))

    def test_set_party_select_carries_it(self):
        out = server.set_party("select", who=self.e46)
        self.assertIn(self.line, out)
        self.g.combat = {"active": False}
        self.assertNotIn("DISABLED", server.set_party("select", who=self.e46))

    def test_compaction_keeps_it_on_every_read(self):
        c = compact.Compactor(clock=lambda: 1000.0)
        for _ in range(3):  # three reads in a row, the same order each time: never "(unchanged)" / "(as before)"
            text = server._turn_next(P1, {P1, P2})
            self.assertIn(self.line, c.apply("combat_turn", text))
            self.assertIn(self.line, c.apply("get_combat_state", server._combat_state_text()))


if __name__ == "__main__":
    unittest.main()
