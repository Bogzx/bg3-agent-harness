"""Offline tests: clouds and surfaces in combat output (act2-fights-analysis 2026-11-03, fix 1).

Balthazar's fight: Cloudkill lands on the bunched-up party in round 2, and in all 11 lost launch-v1 runs the agent
learnt of it only as "+Cloudkill" on whoever it had already hit; look/get_status were the only tools with HAZARD
lines (2 of 12 runs ever called one). get_combat_state, the turn block (end_turn, wait_for('my_turn')) and the
CHANGES after a combat action now list the patches in view (nav.hazards, the same rule as look), the latter two only
when a patch appeared, moved or went. Fixtures: test_turn_macros.py.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_combat_hazards.py
"""

from __future__ import annotations

import unittest

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    GOB,
    P1,
    P2,
    CombatTurnBase,
    batch,
    combat_tools,
    core,
    delta,
    observe,
    overview,
)

CLOUD = {"surface": "CloudkillCloud", "dist": 2.5, "dir": "NE", "center": [12.0, 0.0, 8.0], "w_m": 6.0, "d_m": 6.0}
HERE = dict(CLOUD, dist=0.0, dir=None, under=True)
LINE = "HAZARD CloudkillCloud 2.5m NE (6x6m)"
REAL_CHANGES = delta.changes_text  # Base stubs it per test


class Base(CombatTurnBase):
    def setUp(self) -> None:
        super().setUp()
        overview._hazards_seen["sig"] = None
        self.addCleanup(overview._hazards_seen.update, sig=None)
        self.g.actions_by = {
            P2: {
                "resources": {"ActionPoint": [1, 1], "Movement": [9, 9]},
                "actions": [{"name": "Attack", "spell": "Target_MainHandAttack"}],
            }
        }

    def hazard_reads(self) -> list:
        return [a for op, a in self.g.calls if op == "nav.hazards"]


class CombatState(Base):
    def test_lists_the_cloud_on_every_call(self):
        self.g.hazards = [CLOUD]
        for _ in range(2):  # the full combat view, not a delta
            out = observe.get_combat_state()
            self.assertIn(LINE, out)
        self.assertNotIn("HAZARD", observe._turn_block(self.g.combat, P1, hazards=False))

    def test_no_line_without_a_patch_or_an_old_mod(self):
        self.assertNotIn("HAZARD", observe.get_combat_state())  # op unknown
        self.g.hazards = []
        self.assertNotIn("HAZARD", observe.get_combat_state())

    def test_one_read_for_the_turn(self):
        self.g.hazards = [CLOUD]
        observe.get_combat_state()
        self.assertEqual(len(self.hazard_reads()), 1)  # with the status, not again in the turn block


class TurnBlock(Base):
    """The cloud appears on the enemies' turn: the next party turn's block names it, once."""

    def test_end_turn_names_a_new_cloud_then_not_again(self):
        def hand_over(op, a):
            if op == "act.end_turn":
                self.g.combat["current"] = P2
                self.g.hazards = [CLOUD]  # Balthazar's Cloudkill on the party

        self.g.on_start = hand_over
        out = combat_tools.end_turn()
        self.assertIn(LINE, out.partition("\nYOUR ACTIONS (e5)\n")[0])
        self.assertEqual(self.hazard_reads()[-1], {"char": P2})  # around the member whose turn it is
        self.assertNotIn("HAZARD", batch.wait_for("my_turn"))  # same cloud, same place: not repeated

    def test_moved_and_gone(self):
        self.g.combat["current"] = P2
        self.g.hazards = [CLOUD]
        self.assertIn(LINE, batch.wait_for("my_turn"))
        self.g.hazards = [HERE | {"center": [6.0, 0.0, 2.0]}]  # it moved onto the member
        self.assertIn("HAZARD CloudkillCloud here (you stand in it; 6x6m)", batch.wait_for("my_turn"))
        self.g.hazards = []
        self.assertIn("HAZARD none in view now", batch.wait_for("my_turn"))


class AfterAnAction(Base):
    """The CHANGES of a combat action: the real delta on the fake game."""

    def setUp(self) -> None:
        super().setUp()
        delta.changes_text = REAL_CHANGES
        delta.TRACKER.reset()
        self.addCleanup(delta.TRACKER.reset)
        self.g.nearby = {
            "characters": [{"uuid": GOB, "name": "Goblin", "relation": "hostile", "hp": 7, "max_hp": 7, "dist": 1.5}],
            "items": [],
        }
        REAL_CHANGES(self.g, core.H)  # the agent's last look

    def test_a_cloud_that_appears_is_listed_without_other_changes(self):
        self.g.on_start = lambda op, a: op == "act.cast" and setattr(self.g, "hazards", [CLOUD])
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertIn("\n" + LINE, out)
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertNotIn("HAZARD", out)

    def test_read_with_the_changes(self):
        self.g.hazards = [CLOUD]
        reads = []
        self.g.multi = lambda calls, context="server", events=False: (
            reads.append([op for op, _ in calls]) or [self.g.try_rpc(op, a, context) for op, a in calls]
        )
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertIn(LINE, out)
        self.assertIn(["state.status", "state.nearby", "nav.markers", "nav.hazards"], reads)


class EnemyConcentration(AfterAnAction):
    """Fix 2: the mod's state.combat rows already carried an enemy's concentration (get_combat_state showed
    'concentration=Cloudkill' in 9 of 12 Balthazar runs), the nearby rows didn't, so no CHANGES said it began."""

    def balthazar(self, conc: str | None) -> None:
        row = self.g.nearby["characters"][0]
        row.update(name="Balthazar", hp=91, max_hp=91)
        row.pop("concentration", None)
        if conc:
            row["concentration"] = conc

    def test_changes_say_when_it_starts_and_ends(self):
        self.balthazar(None)
        REAL_CHANGES(self.g, core.H)
        self.g.on_start = lambda op, a: op == "act.cast" and self.balthazar("Cloudkill")
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertIn("~ e1 Balthazar concentrating on Cloudkill", out)
        self.g.on_start = lambda op, a: op == "act.cast" and self.balthazar(None)
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertIn("~ e1 Balthazar no longer concentrating on Cloudkill", out)

    def test_nearby_and_appearing_rows_show_it(self):
        from bg3ai import render

        row = {"uuid": GOB, "name": "Balthazar", "relation": "hostile", "dist": 9.0, "concentration": "Cloudkill"}
        self.assertIn("concentrating on Cloudkill", render.render_nearby({"characters": [row]}, core.H))
        self.g.nearby["characters"] = []
        REAL_CHANGES(self.g, core.H)
        self.g.nearby["characters"] = [row]
        self.assertIn("concentrating on Cloudkill", REAL_CHANGES(self.g, core.H))

    def test_get_party_says_it_once(self):
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            res = real(op, args, context, timeout)
            if op == "state.status":
                res["party"][0]["concentration"] = "Bless"
            return res

        self.g.rpc = rpc
        self.g.sheets = {P1: {"uuid": P1, "name": "Claude", "ac": 16, "concentration": "Bless"}}
        out = overview.get_party()
        self.assertEqual(out.count("Bless"), 1, out)
        self.assertIn("concentrating on Bless", out)


if __name__ == "__main__":
    unittest.main()
