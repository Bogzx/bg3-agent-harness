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

from bg3ai import compact, turn
from bg3ai.tools import core, observe, travel, combat, turn_slots, turn_tool

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
        self.e46 = core.H.get(P2)
        self.line = f"DISABLED {self.e46} {GALE_TAIL}"
        self.g.actions = {"actions": [{"name": "Scorching Ray", "spell": "Projectile_ScorchingRay"}],
                          "resources": {"ActionPoint": [1, 1]}}  # Action 1/1 while asleep, as in S076

    def test_turn_block_next_line_carries_it(self):
        # 00:39:38 shape: Shadowheart's TURN block (her Help went to Karlach), the NEXT line names Gale
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1))
        self.assertIn("end_turn: done", out)
        self.assertIn(f"then {self.e46} Lae'zel (yours)", out)
        self.assertIn(self.line, out)

    def test_now_line_of_the_sleeping_member_carries_it(self):
        # rounds 4-6: "NOW e3's turn" + YOUR ACTIONS (Action 1/1) with no word of the sleep
        def advance(op, args):
            if op == "act.end_turn":
                self.g.combat = s076_combat(current=P2, gale_current=True)
        self.g.on_start = advance
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1))
        self.assertIn(f"NOW {self.e46} Lae'zel's turn: plan it\n{self.line}\nYOUR ACTIONS ({self.e46})", out)

    def test_a_lethargic_member_shows_no_movement(self):
        # S091 08:30:24: `DISABLED e5 Claude: Lethargic (1 turn)` beside `YOUR ACTIONS (e5) NOW … Move 9/9m …`, and
        # the agent planned a walk. Guard.disabling now says what Lethargy does, State.lua moveView zeroes the figure.
        leth = {"name": "Lethargic", "turns": 1,
                "effect": "cannot move; its turn passes once the rest of its turn group has ended (act with it first)"}

        def advance(op, args):
            if op == "act.end_turn":
                self.g.combat = s076_combat(current=P2, gale_current=True)
                self.g.combat["order"][2]["disabled"] = [dict(leth)]
        self.g.on_start = advance
        self.g.actions = {"actions": [{"name": "Scorching Ray", "spell": "Projectile_ScorchingRay"}],
                          "resources": {"ActionPoint": [1, 1], "Movement": [0, 9], "MoveBlocked": "Lethargic"}}
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1))
        self.assertIn(f"DISABLED {self.e46} Lae'zel: Lethargic (1 turn): cannot move; its turn passes once the rest"
                      " of its turn group has ended (act with it first)", out)
        self.assertIn("Move 0/9m (Lethargic)", out)
        self.assertNotIn("MoveBlocked", out)

    def test_interrupted_turn_carries_it(self):
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1), end=False)
        self.assertIn(self.line, out)

    def test_get_combat_state_carries_it_once(self):
        out = observe.get_combat_state()
        self.assertEqual(out.count("Sleeping"), 1, out)
        self.assertIn(self.line, out)
        self.assertNotIn("disabled=", out)

    def test_wait_for_party_turn_carries_it(self):
        self.g.combat = s076_combat(current=P2, gale_current=True)
        self.assertIn(self.line, combat._wait_for_party_turn(1.0))

    def test_set_party_select_carries_it(self):
        out = travel.set_party("select", who=self.e46)
        self.assertIn(self.line, out)
        self.g.combat = {"active": False}
        self.assertNotIn("DISABLED", travel.set_party("select", who=self.e46))

    def test_compaction_keeps_it_on_every_read(self):
        c = compact.Compactor(clock=lambda: 1000.0)
        for _ in range(3):  # three reads in a row, the same order each time: never "(unchanged)" / "(as before)"
            text = turn_slots._turn_next(P1, {P1, P2})
            self.assertIn(self.line, c.apply("combat_turn", text))
            self.assertIn(self.line, c.apply("get_combat_state", observe._combat_state_text()))


FEARED = {"name": "Frightened", "turns": 2, "fix": "it saves again each turn",
          "effect": "the game may move it away by itself at its turn start (orders sent meanwhile are dropped) and may "
                    "end that turn itself: read the combat state before planning it"}


class FearSettle(Base):
    """B8 (S066 #100/#130, S067 #32): Karlach's first order (Rage) at a turn she started Fearful was dropped. Live D2
    P5: a Fearful Wyll walked 4.4 m by himself in 0-2.3 s of his turn start, then stood still with the turn his. The
    plan's first order waits for that walk to stop."""

    def setUp(self) -> None:
        super().setUp()
        self.g.combat = s076_combat()
        self.g.combat["order"][0]["disabled"] = [dict(FEARED)]
        self.g.watch["combat"] = True
        self.reads = 0
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "act.watch":
                self.reads += 1
                if self.reads <= 5:  # the fear walk: 0.9 m per read
                    self.g.watch["party"][0]["pos"] = [0.9 * self.reads, 0.0, 0.0]
            return real(op, args, context, timeout)
        self.g.rpc = rpc

    def test_waits_for_the_fear_walk_to_stop(self):
        clock = [0.0]
        out = turn_slots._fear_settle(self.g.combat, P1, clock=lambda: clock[0],
                                      sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertEqual(out, f"Frightened: the game moved {core.H.get(P1)} Claude 3.6m by itself at its turn start "
                              "(now @(4.5,0,0)); the plan started after it stopped (0.4s)")
        self.assertAlmostEqual(clock[0], 0.4 + turn_slots.FEAR_STILL_S)

    def test_a_member_that_isnt_fearful_waits_for_nothing(self):
        self.g.combat["order"][0].pop("disabled")
        self.assertEqual(turn_slots._fear_settle(self.g.combat, P1), "")
        self.assertEqual(self.reads, 0)

    def test_a_walk_that_never_stops_is_bounded(self):
        self.reads = -10**6
        clock = [0.0]
        out = turn_slots._fear_settle(self.g.combat, P1, clock=lambda: clock[0],
                                      sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertTrue(out.endswith("still moving when the wait ended"), out)
        self.assertLess(clock[0], turn_slots.FEAR_SETTLE_MAX_S + 0.2)

    def test_combat_turn_sends_its_first_order_after_the_fear_walk(self):
        self.addCleanup(setattr, turn_slots, "FEAR_POLL_S", turn_slots.FEAR_POLL_S)
        self.addCleanup(setattr, turn_slots, "FEAR_STILL_S", turn_slots.FEAR_STILL_S)
        turn_slots.FEAR_POLL_S, turn_slots.FEAR_STILL_S = 0.0, 0.05
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1))
        self.assertIn("Frightened: the game moved", out)
        first = next(i for i, (op, _) in enumerate(self.g.calls)
                     if op.startswith("act.") and op not in ("act.watch", "act.note", "act.target_state"))
        watches = [i for i, (op, _) in enumerate(self.g.calls) if op == "act.watch"]
        self.assertGreater(first, watches[4], "the order went out during the fear walk")

    def _fast_settle(self) -> None:
        self.addCleanup(setattr, turn_slots, "FEAR_POLL_S", turn_slots.FEAR_POLL_S)
        self.addCleanup(setattr, turn_slots, "FEAR_STILL_S", turn_slots.FEAR_STILL_S)
        turn_slots.FEAR_POLL_S, turn_slots.FEAR_STILL_S = 0.0, 0.05
        self.reads = 100  # no fear walk: its turn group's start already moved it (live D6 B8: Shadowheart, Move 0/6m)

    def _orders(self) -> list[str]:
        return [op for op, _ in self.g.calls
                if op.startswith("act.") and op not in ("act.watch", "act.note", "act.target_state")]

    def test_a_fearful_turn_the_game_ended_during_the_settle_plays_nothing(self):
        # Live D6 B8: the 2nd Fearful member of a shared group; the game ended her turn during the settle, then
        # "action: Shout_Dash: FAILED — it is not this character's turn" / "turn NOT ended: decide, then continue".
        self._fast_settle()
        settle = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "act.watch" and self.reads >= 102:
                self.g.combat["order"][0]["ended"] = True
            return settle(op, args, context, timeout)
        self.g.rpc = rpc
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1))
        self.assertIn(f"Frightened: the game ended {core.H.get(P1)} Claude's turn by itself before the plan started",
                      out)
        self.assertNotIn("turn NOT ended", out)
        self.assertEqual(self._orders(), [])

    def test_a_fearful_turn_the_game_ends_during_the_plan_says_so(self):
        self._fast_settle()

        def refuse(op, args):
            if op == "act.cast":
                self.g.combat["order"][0]["ended"] = True
                return "it is not this character's turn"
            return None
        self.g.refuse_if = refuse
        out = turn_tool.combat_turn(json.dumps({"action": "Shout_Disengage"}), who=core.H.get(P1))
        self.assertIn(f"Frightened: the game ended {core.H.get(P1)} Claude's turn by itself during the plan", out)
        self.assertNotIn("turn NOT ended", out)
        self.assertNotIn("act.end_turn", self._orders())


if __name__ == "__main__":
    unittest.main()
