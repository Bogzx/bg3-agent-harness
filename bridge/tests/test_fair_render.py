"""Fairness in rendering: status events and Examine show a condition's name, never the engine id; a status
gained and lost within one EVENTS batch (Moonshield flicker) is no change and isn't printed."""

import unittest

from bg3ai import render
from bg3ai.render import Handles

GALE = {"uuid": "11111111-0000-0000-0000-000000000002", "name": "Gale"}
KARLACH = {"uuid": "11111111-0000-0000-0000-000000000001", "name": "Karlach"}


def ev(kind, target, name, seq=1):
    return {"seq": seq, "kind": kind, "data": {"target": target, "name": name}}


class Flicker(unittest.TestCase):
    def test_gained_and_lost_in_one_batch_is_dropped(self):
        evs = [ev("status_applied", GALE, "Moonshield"), ev("status_removed", GALE, "Moonshield"),
               ev("status_removed", KARLACH, "Moonshield"), ev("status_applied", KARLACH, "Moonshield"),
               ev("status_applied", GALE, "Shadow Curse")]
        out = render.render_events(evs, Handles())
        self.assertNotIn("Moonshield", out)
        self.assertIn("Shadow Curse", out)

    def test_net_change_is_kept(self):
        evs = [ev("status_applied", GALE, "Moonshield"), ev("status_removed", GALE, "Moonshield"),
               ev("status_applied", GALE, "Moonshield")]
        out = render.render_events(evs, Handles())
        self.assertEqual(out.count("Moonshield"), 1)
        self.assertIn("status_applied", out)

    def test_other_targets_are_separate(self):
        evs = [ev("status_applied", GALE, "Moonshield"), ev("status_removed", KARLACH, "Moonshield")]
        self.assertEqual(render.render_events(evs, Handles()).count("Moonshield"), 2)


class Flapping(unittest.TestCase):
    """S026-S027: Moonshield on/off on Scratch every tick, split across calls (the in-batch flicker drop misses it)."""
    def setUp(self) -> None:
        self.t = [0.0]
        self.flap = render.StatusFlap(clock=lambda: self.t[0])

    def call(self, *evs):
        self.t[0] += 2.0
        return render.render_events(list(evs), Handles(), flap=self.flap)

    def test_flapping_is_muted_after_one_line(self):
        outs = []
        for i in range(10):
            outs.append(self.call(ev("status_applied" if i % 2 == 0 else "status_removed", GALE, "Moonshield")))
        self.assertTrue(all("Moonshield" in o for o in outs[:5]))
        self.assertIn("status_flapping", outs[5])
        self.assertTrue(all(o == "" for o in outs[6:]))
        self.assertIn("Shadow Curse", self.call(ev("status_applied", GALE, "Shadow Curse")))  # others unaffected

    def test_mute_ends_after_quiet(self):
        for i in range(6):
            self.call(ev("status_applied" if i % 2 == 0 else "status_removed", GALE, "Moonshield"))
        self.t[0] += 200
        self.assertIn("status_removed", self.call(ev("status_removed", GALE, "Moonshield")))

    def test_slow_changes_never_mute(self):
        for i in range(10):
            self.t[0] += 30
            self.assertIn("Moonshield", self.call(ev("status_applied" if i % 2 == 0 else "status_removed",
                                                     KARLACH, "Moonshield")))


class GroupStatuses(unittest.TestCase):
    def test_one_line_for_many_targets(self):
        npcs = [{"uuid": f"22222222-0000-0000-0000-00000000000{i}", "name": f"Pilgrim {i}"} for i in range(5)]
        evs = [ev("status_applied", n, "Moonshield", seq=i) for i, n in enumerate(npcs)]
        evs.append(ev("status_applied", GALE, "Shadow Curse", seq=9))
        out = render.render_events(evs, Handles())
        self.assertEqual(out.count("Moonshield"), 1)
        self.assertIn("on 5: Pilgrim 0 e", out)
        self.assertIn("Pilgrim 4", out)
        self.assertIn("Shadow Curse", out)
        self.assertEqual(len(out.splitlines()), 3)

    def test_two_targets_stay_separate(self):
        evs = [ev("status_applied", GALE, "Moonshield"), ev("status_applied", KARLACH, "Moonshield")]
        self.assertEqual(render.render_events(evs, Handles()).count("Moonshield"), 2)


class ExamineStatuses(unittest.TestCase):
    def test_look_at_drops_status_ids(self):
        from bg3ai import server
        res = {"name": "Gremishka", "statuses": [{"id": "GREMISHKA_MAGICALLERGY_AURA", "name": "Magic Allergy"},
                                                 {"id": "NAMELESS_ONE"}]}
        out = server._shown_statuses(res)
        self.assertEqual(out["statuses"][0], {"name": "Magic Allergy"})
        self.assertEqual(out["statuses"][1], {"id": "NAMELESS_ONE"})
        self.assertNotIn("GREMISHKA", render.compact(out, Handles()))
        self.assertEqual(res["statuses"][0]["id"], "GREMISHKA_MAGICALLERGY_AURA", "input not mutated")


if __name__ == "__main__":
    unittest.main()


class MapNames(unittest.TestCase):
    """#8: get_map shows region and waypoint names a player reads on the map, never WLD_Main_A / WAYP_ ids."""

    MAP = {"level": "SCL_Main_A", "region": "Shadow-Cursed Lands", "waypoints": [
        {"id": "WAYP_CHA_Chapel", "name": "Overgrown Ruins", "area": "CHA", "level": "WLD_Main_A",
         "region": "Wilderness"},
        {"id": "WAYP_CHA_Top", "name": "Roadside Cliffs", "area": "CHA", "level": "WLD_Main_A",
         "region": "Wilderness"},
        {"id": "WAYP_SCL_LastLight", "name": "Last Light Inn", "area": "SCL", "level": "SCL_Main_A",
         "region": "Shadow-Cursed Lands", "here": True}]}

    def test_names_not_ids(self):
        out = render.render_map(self.MAP, Handles())
        self.assertNotRegex(out, r"WAYP_|_Main_|area=")
        self.assertIn("REGION Shadow-Cursed Lands", out)
        self.assertIn("here: Last Light Inn", out)
        self.assertIn("Wilderness: Overgrown Ruins, Roadside Cliffs", out)

    def test_level_changed_event_has_region_only(self):
        out = render.render_events([{"seq": 1, "kind": "level_changed", "data": {"region": "Shadow-Cursed Lands"}}],
                                   Handles())
        self.assertIn("region=Shadow-Cursed Lands", out)
        self.assertNotIn("_Main_", out)
