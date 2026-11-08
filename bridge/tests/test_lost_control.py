"""Offline tests: a party member whose turn the game runs itself (live.md "## Goaded read": the game's AI cast, spent
and ended the turn with no order, in a match and in normal play; the HUD said "Can't control: Goaded"). The mod marks
its state.combat row game_turn (and ended, so nobody waits on it) and tags its own casts turn=game's.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from bg3ai import compact, delta, render, turn
from bg3ai import compact_check as cc
from bg3ai.render import Handles, render_combat
from bg3ai.tools import combat
from test_compact import fresh
from test_turn_macros import GOB, P1, P2, Base


def goaded_combat() -> dict:
    """P2 Goaded in its open turn (the mod's row: is_current, ended, game_turn), P1 in the same group still to act."""
    return {
        "active": True,
        "round": 5,
        "current": P1,
        "turn_group": [P1],
        "party_can_act": [P1],
        "order": [
            {
                "uuid": P2,
                "name": "Red Ash",
                "relation": "party",
                "initiative": 6,
                "hp_pct": 40,
                "is_current": True,
                "ended": True,
                "game_turn": True,
            },
            {
                "uuid": P1,
                "name": "Claude",
                "relation": "party",
                "initiative": 6,
                "hp_pct": 90,
                "is_current": True,
                "can_act": True,
            },
            {"uuid": GOB, "name": "Blue Ash", "relation": "hostile", "initiative": 6, "hp_pct": 60},
        ],
    }


class Rows(unittest.TestCase):
    def test_the_row_says_game_turn_not_ended(self):
        lines = render_combat(goaded_combat(), Handles()).split("\n")
        ash = next(x for x in lines if "Red Ash" in x)
        self.assertIn("game_turn", ash)
        self.assertNotIn("ended", ash)  # it is running, not over
        self.assertNotIn("is_current", ash)
        self.assertEqual(
            compact.combat_row("  game_turn relation=party e1 Red Ash initiative=6 hp_pct=40"),
            "  e1 Red Ash party init 6 hp 40% game_turn",
        )

    def test_nobody_acts_or_waits_on_it(self):
        cs = goaded_combat()
        self.assertEqual(turn.acting({**cs, "turn_group": None}), [P1])
        self.assertEqual(turn.passing(cs), [])  # never ended after 5 s by the wait, as a stunned member is


class Casts(unittest.TestCase):
    def test_an_own_cast_in_the_games_turn_is_tagged(self):
        text = (
            "EVENTS\n  cast caster={e1 name=Red Ash} spell=Target_PommelStrike roll=attack turn=game's "
            "name=Pommel Strike"
        )
        c = fresh()
        out = c.apply("wait_for", text)
        self.assertEqual(cc.check(text, out, list(c.elided), ""), [], out)
        self.assertIn("cast e1 Red Ash (game's turn): Pommel Strike (attack)", out)


class EndTurn(Base):
    def test_a_member_the_game_runs_is_no_peer_for_the_huds_end_turn(self):
        self.assertIsNone(combat._peer_requested(goaded_combat(), P1))


class Status(unittest.TestCase):
    """live.md "## Lost control fix live": get_status read `COMBAT round 3 turn=e1` on a turn the game ran."""

    WORDS = "can't control: Goaded, the game runs its turn"

    def status(self, **combat):
        return {"in_combat": True, "party": [], "combat": {"round": 3, "current": P1, **combat}}

    def test_get_status_says_the_game_runs_the_turn(self):
        h = Handles()
        out = render.render_status(self.status(game_turn=self.WORDS), None, h)
        self.assertIn(f"COMBAT round 3 turn={h.get(P1)} ({self.WORDS})", out)
        self.assertNotIn("the game runs", render.render_status(self.status(), None, h))

    def test_the_change_line_says_it_too(self):
        h = Handles()
        self.assertTrue(delta._turn(self.status(game_turn=self.WORDS), h).endswith(f"turn={h.get(P1)} ({self.WORDS})"))
        self.assertNotIn("the game runs", delta._turn(self.status(), h))


if __name__ == "__main__":
    unittest.main()
