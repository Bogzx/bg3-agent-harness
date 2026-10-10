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


class AfterTrim(unittest.TestCase):
    """walk_toward/interact results printed a full portrait after={...} and the position twice (S013)."""

    AFTER = {"max_hp": 50, "movement": 9, "pos": [-175.4, 20, -331.9], "slots": {"1": 3}, "action": 1,
             "bonus": 1, "hp": 48, "reaction": 1}

    def setUp(self):
        server._last_hp.clear()

    def text(self, result, tool="walk_toward", char="c1"):
        from bg3ai.game import JobResult
        return server._job_text(tool, JobResult("done", result), char)

    def test_walk_outside_combat_has_no_stat_block(self):
        # S015: walks/interacts still carried after={hp=34/55} every time the leader was hurt
        out = self.text({"dist": 0.1, "pos": [-175.4, 20, -331.9], "after": dict(self.AFTER)})
        self.assertEqual(out.count("@("), 1)
        self.assertNotIn("after", out)
        self.assertNotIn("hp", out)

    def test_unhurt_outside_combat_has_no_after(self):
        out = self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, hp=50)})
        self.assertNotIn("after", out)

    def test_interact_keeps_the_position_plain(self):
        out = self.text({"used": True, "after": dict(self.AFTER, hp=50)}, tool="interact")  # no position of its own
        self.assertIn("@(-175.4,20,-331.9)", out)
        self.assertNotIn("after", out)

    def test_hp_change_is_one_short_field(self):
        for tool in ("walk_toward", "move_to", "interact", "pick_up", "loot_corpse"):
            server._last_hp.clear()
            self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, hp=34)}, tool)
            self.assertNotIn("hp", self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, hp=34)}, tool))
            out = self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, hp=7)}, tool)
            self.assertIn("hp=34->7/50", out, tool)
            self.assertNotIn("after", out)

    def test_hp_is_tracked_per_character(self):
        self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, hp=34)}, char="c1")
        out = self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, hp=7)}, char="c2")
        self.assertNotIn("hp", out)  # first sight of c2: nothing to compare with

    def test_other_tools_keep_hp_when_hurt(self):
        out = self.text({"after": dict(self.AFTER)}, tool="use_ability")
        self.assertIn("hp=48/50", out)

    def test_in_combat_keeps_the_economy_flat(self):
        out = self.text({"dist": 0, "pos": [1, 2, 3], "after": dict(self.AFTER, in_combat=True, my_turn=True)})
        self.assertIn("movement=9", out)
        self.assertIn("slots=1:3", out)
        self.assertEqual(out.count("{"), 1)  # one level: the combat_turn regex strips after={...} whole
        self.assertEqual(out.count("@("), 1)


class ClutterInChanges(unittest.TestCase):
    def test_props_are_counted_not_listed(self):
        from bg3ai import delta
        H = render.Handles()
        items = [{"uuid": f"00000000-0000-0000-0000-0000000000{i:02d}", "name": n, "kind": k, "dist": 5.0 + i}
                 for i, (n, k) in enumerate([("Candle", "usable"), ("Tin Cup", "item"), ("Padded Chair", "usable"),
                                             ("Door", "door"), ("Plate Armour", "item"), ("Candle", "usable"),
                                             ("Wooden Chest", "container")])]
        items.append({"uuid": "00000000-0000-0000-0000-000000000099", "name": "Candle", "kind": "usable",
                      "dist": 3.0, "story": True})
        old = delta.snapshot(H, nearby={"characters": [], "items": []})
        new = delta.snapshot(H, nearby={"characters": [], "items": items})
        out = "\n".join(delta.diff(old, new, H))
        self.assertNotIn("Tin Cup", out)
        self.assertNotIn("Padded Chair", out)
        self.assertIn("Door", out)
        self.assertIn("Plate Armour", out)
        self.assertIn("Wooden Chest", out)
        self.assertEqual(out.count("Candle"), 1)  # the story one
        self.assertIn("props 4", out)
        gone = "\n".join(delta.diff(new, old, H))
        self.assertNotIn("Tin Cup", gone)
        self.assertIn("Door", gone)
