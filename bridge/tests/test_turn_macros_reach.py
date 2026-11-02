"""Offline tests: use_ability's target list and set_reactions in a fight. Fixtures: test_turn_macros.py."""

from __future__ import annotations

import unittest

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    GOB,
    IMP,
    P1,
    P2,
    CombatTurnBase,
    combat_tools,
    core,
    uitools,
)


class CombatTools(CombatTurnBase):
    def test_use_ability_takes_a_list_of_targets(self):
        # ACT-8: a client sending targets as a JSON list reached targets.split(",")
        out = combat_tools.use_ability("Target_Bless", targets=[self.gob, " ", self.imp])
        self.assertNotIn("split", out)
        self.assertEqual([a.get("targets") for _, a in self.g.ops("act.cast")], [[GOB, IMP]])

    # S081 #8 02:43:22: set_reactions(Wyll) under an open Counterspell prompt → "Wyll could not be selected: the HUD
    # kept another member selected". The prompt is named and nothing is selected.
    def test_set_reactions_under_a_reaction_prompt_names_it(self):
        self.g.hud = P2
        self.g.ui = {"screen": "reaction", "reaction": {
            "prompt": "Sharran Fidelian Lamona cast Bone Chill on Claude. | Choose how to react. | 108/160 | Counterspell"}}
        out = uitools.set_reactions(who=core.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertIn("a reaction prompt is on screen (Sharran Fidelian Lamona cast Bone Chill on Claude.)", out)
        self.assertIn("react(use=true|false)", out)
        self.assertEqual(self.g.ops("act.select"), [])
        self.assertEqual(self.g.ops("ui.reactions"), [])

    # S087 06:10:18: set_reactions(Wyll) in the enemies' turns: the select fails; the reason and when it works are named
    def test_set_reactions_in_an_enemy_turn_says_why_the_select_failed(self):
        self.g.hud = P2
        self.g.combat["current"] = GOB      # an enemy acts: no party member's turn
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "act.select":
                self.g.calls.append((op, args))
                return {"status": "failed", "error": "the HUD kept another member selected"}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = uitools.set_reactions(who=core.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertIn("could not be selected: the HUD kept another member selected", out)
        self.assertIn("It is the enemies' turn", out)
        self.assertEqual(self.g.ops("ui.reactions"), [])

    # S081 #258: a mode change answers with a job (the commands apply a frame later): the result printed `job=1`
    def test_set_reactions_waits_for_the_change(self):
        orig, polls = self.g.rpc, []

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "ui.reactions":
                self.g.calls.append((op, args))
                return {"job": 1, "status": "running"}
            if op == "job.poll" and context == "client":
                polls.append(1)
                return {"status": "done", "result": {"char": P1, "reactions": [{"name": "Counterspell", "mode": "never"}]}}
            return orig(op, args, context, timeout)
        self.g.rpc = rpc
        out = uitools.set_reactions(who=core.H.get(P1), settings='{"Counterspell": "never"}')
        self.assertTrue(polls)
        self.assertIn("Counterspell", out)
        self.assertIn("never", out)
        self.assertNotIn("job=1", out)


if __name__ == "__main__":
    unittest.main()
