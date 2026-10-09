"""Tool results stay terse: events the result already reports are left out, repeated action lists shrink.

Why: model think time grew from ~1.2 s to ~2.9 s per turn over S003..S013; every repeated line is re-read
on every later turn (research/impl-act.md, 'Think time')."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bg3ai import render, server  # noqa: E402

U1, U2, U3 = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
              "33333333-3333-3333-3333-333333333333")


def ref(u, name):
    return {"uuid": u, "name": name}


class EventDedup(unittest.TestCase):
    def setUp(self):
        self.H = render.Handles()
        self.wyll, self.mari, self.gale = (self.H.get(U1, "Wyll"), self.H.get(U2, "Mari"), self.H.get(U3, "Gale"))

    def test_turn_owner_cast_damage_status_dropped_others_kept(self):
        evs = [{"kind": "cast", "data": {"caster": ref(U1, "Wyll"), "spell": "Projectile_EldritchBlast"}},
               {"kind": "damage", "data": {"target": ref(U2, "Mari"), "source": ref(U1, "Wyll"), "amount": 10}},
               {"kind": "status_applied", "data": {"target": ref(U1, "Wyll"), "name": "Dark One's Blessing"}},
               {"kind": "damage", "data": {"target": ref(U1, "Wyll"), "source": ref(U2, "Mari"), "amount": 3}},
               {"kind": "cast", "data": {"caster": ref(U3, "Gale"), "spell": "Projectile_FireBolt"}}]
        out = render.render_events(evs, self.H, body=f"TURN {self.wyll} Wyll (round 1)\naction: ...")
        self.assertNotIn("EldritchBlast", out)
        self.assertNotIn("amount=10", out)
        self.assertNotIn("Blessing", out)
        self.assertIn("amount=3", out)          # the enemy's hit on the owner is news
        self.assertIn("FireBolt", out)          # another character's cast too
        full = render.render_events(evs, self.H)  # outside combat_turn nothing is dropped
        self.assertIn("EldritchBlast", full)

    def test_turn_ended_runs_share_a_line(self):
        evs = [{"kind": "turn_ended", "data": ref(U1, "Wyll")}, {"kind": "turn_ended", "data": ref(U3, "Gale")},
               {"kind": "died", "data": ref(U2, "Mari")}, {"kind": "turn_ended", "data": ref(U2, "Mari")}]
        lines = render.render_events(evs, self.H).splitlines()[1:]
        self.assertEqual(len(lines), 3)
        self.assertIn("Wyll", lines[0])
        self.assertIn("Gale", lines[0])

    def test_job_and_queue_reported_by_the_result_are_dropped(self):
        evs = [{"kind": "job_finished", "data": {"job": "j13", "status": "done"}},
               {"kind": "job_finished", "data": {"job": "j14", "status": "done"}},
               {"kind": "queue_interrupted", "data": {"who": U1, "why": "x"}}]
        out = render.render_events(evs, self.H, body=f"queue {self.wyll} Wyll: STOPPED", job="j13")
        self.assertNotIn("j13", out)
        self.assertIn("j14", out)
        self.assertNotIn("queue_interrupted", out)


def acts(*names, slots=3):
    return {"resources": {"ActionPoint": {"amount": 1, "max": 1}},
            "actions": [{"name": n, "spell": f"Target_{n}", "range": 1.5,
                         "uses": slots if n == "Bless" else None} for n in names]}


class ActionsDelta(unittest.TestCase):
    def setUp(self):
        server._acts_shown.clear()

    def test_full_list_once_then_only_changes(self):
        many = [f"A{i}" for i in range(12)]
        first = server._actions_delta(U1, acts(*many, "Bless"))
        self.assertIn("A0 (Target_A0)", first)
        again = server._actions_delta(U1, acts(*many, "Bless", slots=2))
        self.assertNotIn("A0 (Target_A0)", again)
        self.assertIn("as listed before", again)
        self.assertIn("+ Bless (Target_Bless)", again)    # changed uses: shown again, not as removed
        self.assertNotIn("- Bless", again)
        gone = server._actions_delta(U1, acts(*many[1:], "Bless", slots=2))
        self.assertIn("- A0 (Target_A0)", gone)
        self.assertIn("A5 (Target_A5)", server._actions_delta(U2, acts(*many)))  # per character

    def test_mostly_different_list_is_shown_whole(self):
        server._actions_delta(U1, acts("A", "B", "C", "D"))
        out = server._actions_delta(U1, acts("E", "F", "G", "D"))
        self.assertNotIn("as listed before", out)
        self.assertIn("E (Target_E)", out)


if __name__ == "__main__":
    unittest.main()
