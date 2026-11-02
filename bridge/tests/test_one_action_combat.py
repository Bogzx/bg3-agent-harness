"""Offline tests: in combat each call is one action (fight-flow research 2026-11-02, D291).

do_actions and queue_actions run no steps in combat and a fight starting always stops them; a walk in combat
returns at its end; use_ability / move_to in combat end with CHANGES (other combatants' HP, deaths); combat_turn
is gone from tools/list. Fixtures: test_turn_macros.py.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_one_action_combat.py
"""

from __future__ import annotations

import asyncio
import json
import unittest

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    GOB,
    IMP,
    NOT_JOBS,
    P1,
    Base,
    CombatTurnBase,
    batch,
    combat_tools,
    core,
    delta,
    queueing,
    world_tools,
)

STEPS = [{"tool": "use_ability", "ability": "Target_MainHandAttack", "target": "e1"}, {"tool": "end_turn"}]


def jobs(g) -> list[str]:
    return [op for op, _ in g.calls if op.startswith("act.") and op not in NOT_JOBS]


class BatchesInCombat(CombatTurnBase):
    def test_do_actions_runs_no_steps(self):
        out = batch.do_actions(json.dumps(STEPS))
        self.assertIn("REFUSED: in combat do_actions runs no steps: each call is one action (use_ability, move_to, "
                      "walk_toward, jump_to, react, end_turn)", out)
        self.assertEqual(jobs(self.g), [])

    def test_queue_actions_queues_nothing(self):
        out = batch.queue_actions(json.dumps(STEPS))
        self.assertIn("REFUSED: in combat queue_actions runs no steps", out)
        self.assertEqual(jobs(self.g), [])
        mgr = queueing._queue_mgr
        self.assertFalse(mgr and any(q.steps for q in mgr.queues.values()))

    def test_do_actions_wait_false_is_refused_too(self):
        out = batch.do_actions(json.dumps(STEPS), wait=False)
        self.assertIn("REFUSED: in combat queue_actions runs no steps", out)
        self.assertEqual(jobs(self.g), [])


class CombatNotIgnorable(Base):
    def test_ignore_combat_is_refused(self):
        with self.assertRaises(ValueError) as e:
            queueing._parse_ignore("combat,hp")
        self.assertIn("combat", str(e.exception))
        self.assertEqual(queueing._parse_ignore("hostile,hp"), {"hostile", "hp"})

    def test_do_actions_out_of_combat_stops_when_a_fight_starts(self):
        self.g.dur["act.move_to"] = 0.3

        def fight(op, a):  # the first walk starts a fight
            if op == "act.move_to":
                self.g.watch["combat"] = True
        self.g.on_start = fight
        out = batch.do_actions(json.dumps([{"tool": "move_to", "x": 1, "y": 0, "z": 1},
                                           {"tool": "move_to", "x": 2, "y": 0, "z": 2}]))
        self.assertEqual(len(self.g.ops("act.move_to")), 1, out)
        self.assertIn("combat", out.lower())


class WalksInCombat(CombatTurnBase):
    def test_move_to_wait_false_returns_at_the_walks_end(self):
        self.g.dur["act.move_to"] = 0.1
        out = world_tools.move_to(x=1, y=0, z=1, wait=False)
        self.assertIn("move_to", out)
        self.assertNotRegex(out, r"started j\d+")
        polls = [a for op, a in self.g.calls if op == "job.poll"]
        self.assertTrue(polls)
        self.assertEqual(self.g.jobs[1]["status"], "done")

    def test_out_of_combat_wait_false_still_returns_at_once(self):
        self.g.watch["combat"] = False
        self.g.combat = {"active": False}
        self.g.dur["act.move_to"] = 5.0
        out = world_tools.move_to(x=1, y=0, z=1, wait=False)
        self.assertRegex(out, r"j\d+")
        self.assertNotIn("pace guard", out)


class ChangesInCombat(CombatTurnBase):
    """The real delta on the fake game: the goblin dies and the imp is hurt by the action."""

    def setUp(self) -> None:
        super().setUp()
        delta.changes_text = REAL_CHANGES
        delta.TRACKER.reset()
        self.addCleanup(delta.TRACKER.reset)
        self.g.nearby = {"characters": [
            {"uuid": GOB, "name": "Goblin", "relation": "hostile", "hp": 7, "max_hp": 7, "dist": 1.5},
            {"uuid": IMP, "name": "Imp", "relation": "hostile", "hp": 10, "max_hp": 10, "dist": 6.0},
        ], "items": []}
        REAL_CHANGES(self.g, core.H)  # the agent's last look
        # the mod's portrait summary in a fight (Actions.lua A.summary: in_combat, my_turn)
        self.g.result_for = lambda op, a: {"dist": 1.0, "after": {"hp": 20, "max_hp": 20, "action": 0, "bonus": 1,
                                                                   "movement": 3, "in_combat": True, "my_turn": True}}

        def hit(op, a):
            if op == "act.cast":
                gob, imp = self.g.nearby["characters"]
                gob.update(hp=0, dead=True)
                imp.update(hp=4)
        self.g.on_start = hit

    def test_use_ability_ends_with_changes(self):
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertIn("after={", out)
        self.assertIn("action=0", out.split("\nCHANGES\n", 1)[0])
        self.assertIn("\nCHANGES\n", out)
        changes = out.split("\nCHANGES\n", 1)[1]
        self.assertIn("e1 Goblin DEAD", changes)
        self.assertIn("e2 Imp HP 10->4/10", changes)

    def test_out_of_combat_no_changes_block(self):
        self.g.watch["combat"] = False
        self.g.combat = {"active": False}
        out = combat_tools.use_ability("Target_MainHandAttack", target=core.H.get(GOB), who=core.H.get(P1))
        self.assertNotIn("CHANGES", out)


class ToolsList(unittest.TestCase):
    def test_combat_turn_is_gone(self):
        names = {t.name for t in asyncio.run(core.mcp.list_tools())}
        self.assertNotIn("combat_turn", names)
        self.assertLessEqual({"use_ability", "move_to", "end_turn", "react", "jump_to"}, names)


REAL_CHANGES = delta.changes_text  # Base stubs it per test; these tests use the real one


if __name__ == "__main__":
    unittest.main()
