"""Offline tests: shared party turn groups (live bug, claude-run-2 S001 on the Nautiloid).

Claude and Lae'zel had equal initiative, so the game made them one turn group: both were current
(`is_current`), `state.combat.current` named only one of them, and a member that ended its turn kept
IsActiveCombatTurn with RequestedEndTurn set until the other one ended too. The bridge refused
combat_turn who=Lae'zel ("not e46's turn (now e1 Claude)") and let Claude act again after ending.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import unittest

from test_turn_macros import GOB, IMP, NOT_JOBS, P1, P2, Base

from bg3ai import server, turn


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

    def test_next_up_skips_the_ended_group(self):
        label = lambda u: {P1: "e1", P2: "e46", GOB: "e48", IMP: "e50"}[u]  # noqa: E731
        # Both members ended, the order hasn't moved on yet: the imps are next, then the party again.
        cs = group_combat(ended=(P1, P2))
        text, mine = turn.next_up(cs, {P1, P2}, label, ended=P1)
        self.assertEqual(text, "NEXT e48, e50, then e46 + e1 (yours, shared turn)")
        self.assertEqual(mine, P2)

    def test_next_up_single_party_member_wraps_round(self):
        label = lambda u: {P1: "e1", GOB: "e48", IMP: "e50"}[u]  # noqa: E731
        cs = {"active": True, "current": P1, "order": [
            {"uuid": P1, "is_current": True}, {"uuid": GOB}, {"uuid": IMP}]}
        self.assertEqual(turn.next_up(cs, {P1}, label, ended=P1), ("NEXT e48, e50, then e1 (yours)", P1))


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
        self.e1 = server.H.get(P1)
        self.e46 = server.H.get(P2)

    def plan(self, **kw) -> str:
        return json.dumps(kw)

    def test_other_group_member_may_take_its_turn(self):
        out = server.combat_turn(self.plan(action="Shout_Disengage"), who=self.e46)
        self.assertNotIn("FAILED", out)
        self.assertIn(f"TURN {self.e46}", out)
        self.assertEqual(self.g.ops("act.cast")[0][1]["char"], P2)

    def test_default_actor_is_the_selected_member(self):
        # current names Lae'zel (listed first) but the selected character Claude is in the group too.
        out = server.combat_turn(self.plan(action="Shout_Disengage"))
        self.assertIn(f"TURN {self.e1}", out)

    def test_ending_one_member_hands_over_to_the_other(self):
        out = server.combat_turn(self.plan(action="Shout_Disengage"))
        self.assertIn("end_turn: done", out)
        self.assertIn(f"NOW {self.e46} Lae'zel's turn: plan it", out)
        self.assertIn("YOUR ACTIONS", out)
        self.assertNotIn("NEXT", out)

    def test_ended_member_is_refused(self):
        self.g.combat = group_combat(ended=(P1,))
        out = server.combat_turn(self.plan(action="Shout_Disengage"), who=self.e1)
        self.assertIn("already ended its turn", out)
        self.assertEqual([o for o, _ in self.g.ops("act.") if o not in NOT_JOBS], [])

    def test_turn_check_accepts_any_group_member(self):
        self.assertEqual(server._turn_check(P2, None), (False, ""))
        self.assertEqual(server._turn_check(P1, None), (False, ""))
        self.g.combat = group_combat(ended=(P1,))
        fatal, why = server._turn_check(P1, None)
        self.assertTrue(fatal)
        self.assertIn("no longer", why)

    def test_whole_group_then_enemies(self):
        server.combat_turn(self.plan(action="Shout_Disengage"), who=self.e1)
        out = server.combat_turn(self.plan(action="Shout_Disengage"), who=self.e46)
        self.assertNotIn("'s turn: plan it", out)
        self.assertIn(f"NOW {self.e46} hp=", out)  # whose numbers they are
        self.assertIn(f"Imp, then {self.e46} Lae'zel + {self.e1} Claude (yours, shared turn)", out)

    def test_wait_for_party_turn_names_the_whole_group(self):
        text = server._wait_for_party_turn(1.0)
        self.assertIn(self.e46, text)
        self.assertIn(self.e1, text)
        self.assertIn("shared turn group", text)

    def test_end_turn_without_who_ends_the_one_who_can_act(self):
        # The game moved the selection? Here the selected Claude already ended: end_turn() means Lae'zel.
        self.g.combat = group_combat(ended=(P1,))
        out = server.end_turn(wait=False)
        self.assertIn(f"end_turn {self.e46} Lae'zel: done", out)
        self.assertEqual(self.g.ops("act.end_turn")[0][1]["char"], P2)


    def test_within_move_reports_distance_to_the_target(self):
        # Live: "move e49: done — @(…) dist=0.9" was the distance to the walk point; the imp was 1.8 m away.
        self.g.reach = {"reachable": True, "pos": [1.0, 0.0, 0.0]}
        out = server.combat_turn(self.plan(move={"target": server.H.get(GOB), "within": 1.2},
                                           action="Shout_Disengage"), who=self.e1, end=False)
        line = next(x for x in out.splitlines() if x.startswith("move "))
        self.assertNotIn("dist=", line)
        self.assertIn("now 2.2m from", line)


if __name__ == "__main__":
    unittest.main()
