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


class RestFlowHidden(unittest.TestCase):
    def test_rest_flow_diagnostics_are_not_shown(self):
        """S044 23:50:48 look: `rest_flow step=ready_ok:MOO_Assault_ReadyCheck` (an engine id, fairness LEAK)."""
        evs = [{"kind": "rest_flow", "data": {"step": "ready_ok:MOO_Assault_ReadyCheck"}},
               ev("status_applied", GALE, "Shadow Curse")]
        out = render.render_events(evs, Handles())
        self.assertNotIn("MOO_", out)
        self.assertNotIn("rest_flow", out)
        self.assertIn("Shadow Curse", out)


class LevelIds(unittest.TestCase):
    def test_level_ids_become_map_names(self):
        """S040 get_explored `level=SCL_Main_A`, S046 look_at `region=SCL_Main_A` (fairness LEAK judged_id)."""
        ex = {"level": "SCL_Main_A", "cells": 2200, "recent": [{"subregion": "Nightsong's Prison"}]}
        out = render.compact(render.public_levels(ex), Handles())
        self.assertNotIn("SCL_Main_A", out)
        self.assertIn("Shadow-Cursed Lands", out)
        ent = {"name": "Claude", "level": 7, "region": "SCL_Main_A", "statuses": [{"name": "Bless"}]}
        got = render.public_levels(ent)
        self.assertEqual(got["region"], "Shadow-Cursed Lands")
        self.assertEqual(got["level"], 7)  # a character level stays
        self.assertEqual(render.public_levels({"region": "XYZ_Main_A"})["region"], "another area")
        self.assertEqual(render.public_levels({"note": "MAG_SHADOW_OBSCURITY"})["note"], "MAG_SHADOW_OBSCURITY")


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


class CrowdFlapping(unittest.TestCase):
    """S026/S027 at Moonrise: Moonshield on/off on ~20 pilgrims, each once per call (no single target flaps)."""
    PARTY = {GALE["uuid"], KARLACH["uuid"]}

    def setUp(self) -> None:
        self.t = [0.0]
        self.flap = render.StatusFlap(clock=lambda: self.t[0])
        self.npcs = [{"uuid": f"22222222-0000-0000-0000-0000000000{i:02d}", "name": f"Pilgrim {i}"} for i in range(8)]

    def call(self, *evs, party=PARTY):
        self.t[0] += 2.0
        return render.render_events(list(evs), Handles(), flap=self.flap, party=party)

    def wave(self, kind, ix):
        return [ev(kind, self.npcs[i], "Moonshield", seq=i) for i in ix]

    def test_bystanders_muted_party_kept(self):
        first = self.call(*self.wave("status_applied", range(4)))
        self.assertIn("on 4: Pilgrim 0", first)
        second = self.call(*self.wave("status_removed", range(4)))
        self.assertIn("status_flapping", second)
        self.assertIn("muted for non-party", second)
        self.assertNotIn("Pilgrim", second)
        self.assertEqual(self.call(*self.wave("status_applied", range(2, 8))), "")
        self.assertIn("Gale", self.call(*self.wave("status_removed", range(8)), ev("status_removed", GALE, "Moonshield")))

    def test_mass_apply_without_removals_is_not_muted(self):
        for _ in range(3):  # Frightened on many enemies, never lost: every line kept
            out = self.call(*[ev("status_applied", self.npcs[i], "Frightened", seq=i) for i in range(5)])
            self.assertIn("Frightened", out)
            self.assertNotIn("status_flapping", out)

    def test_party_unknown_keeps_old_behaviour(self):
        self.call(*self.wave("status_applied", range(4)), party=None)
        self.assertNotIn("status_flapping", self.call(*self.wave("status_removed", range(4)), party=None))

    def test_mute_ends_after_quiet(self):
        self.call(*self.wave("status_applied", range(4)))
        self.call(*self.wave("status_removed", range(4)))
        self.t[0] += 300
        self.assertIn("Pilgrim 1", self.call(*self.wave("status_applied", range(1, 2))))


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


class NearbyEffect(unittest.TestCase):
    def test_lit_painting_and_button(self):
        H = Handles()
        n = {"items": [{"uuid": "u1", "name": "Button", "kind": "usable", "dist": 1.8, "near": "under General"},
                       {"uuid": "u2", "name": "General", "kind": "usable", "dist": 3.6, "effect": "lit"},
                       {"uuid": "u3", "name": "Candle", "kind": "item", "dist": 4.0}]}
        out = render.render_nearby(n, H)
        self.assertRegex(out, r"ITEM e\d+ General <usable> 3.6m \[lit\]")
        self.assertNotIn("Button <usable> 1.8m (under General) [", out)


class CampLine(unittest.TestCase):
    """get_status's camp roster reads as text, name first (live 20261016-183253: `ALSO camp=[Warlock3(The Fiend)
    level=3 e5 wants_talk level_up=4 name=Wyll; Druid5 name=Halsin level=5 e6]`)."""

    CAMP = [{"uuid": "22222222-0000-0000-0000-000000000005", "name": "Wyll", "level": 3, "level_up": 4,
             "wants_talk": True, "classes": [{"class": "Warlock", "level": 3, "subclass": "The Fiend"}]},
            {"uuid": "22222222-0000-0000-0000-000000000006", "name": "Halsin", "level": 5, "level_up": 5,
             "classes": [{"class": "Druid", "level": 5}]}]

    def test_status_camp_line(self):
        H = Handles()
        out = render.render_status({"region": "Shadow-Cursed Lands", "gold": 43, "party": [], "camp": self.CAMP},
                                   None, H)
        w, h = H.get(self.CAMP[0]["uuid"]), H.get(self.CAMP[1]["uuid"])
        self.assertIn(f"CAMP {w} Wyll Warlock3(The Fiend) level 3 (level up to 4 ready), wants to talk; "
                      f"{h} Halsin Druid5 level 5 (at camp, not in the party)", out.split("\n"))
        self.assertNotIn("ALSO", out)
        self.assertNotIn("=", out.split("CAMP ")[1])

    def test_other_extras_stay_on_also(self):
        out = render.render_status({"party": [], "camp": self.CAMP[1:], "weather": "rain"}, None, Handles())
        self.assertIn("ALSO weather=rain", out)
        self.assertIn("CAMP ", out)

    def test_geo_gen_is_never_shown(self):
        # audit C18: it counts doors/walls changing near the party, seen or not
        out = render.render_status({"party": [], "geo_gen": 15, "weather": "rain"}, None, Handles())
        self.assertNotIn("geo_gen", out)
        self.assertIn("ALSO weather=rain", out)

    def test_odd_rows(self):
        H = Handles()
        self.assertEqual(render.camp_text([{"name": "Minthara"}], H), "Minthara (at camp, not in the party)")
        self.assertIn("can_reach=6", render.camp_text([{"name": "Gale", "level": 5, "can_reach": 6}], H))
