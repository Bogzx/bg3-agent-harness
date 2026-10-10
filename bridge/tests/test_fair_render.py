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
