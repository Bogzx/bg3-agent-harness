"""Offline tests: story milestones from the progress seen-state and session events (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from bg3ai import milestones as MS

# claude-run-2 after S005 (runtime/sessions/claude-run-2/progress_state.json, trimmed)
SEEN = {"regions": ["Nautiloid", "Wilderness"], "areas": ["Wilderness|Ravaged Beach", "Wilderness|Campsite"],
        "quests": {"Escape the Nautiloid": {"steps": 4, "done": True}, "Find a Cure": {"steps": 1, "done": False}},
        "levels": {"Claude": 2, "Lae'zel": 1, "Shadowheart": 2}}


def ev(kind: str, what: str, t: float) -> dict:
    return {"type": "progress", "t": t, "kind": kind, "what": what, "points": 1}


class Milestones(unittest.TestCase):
    def test_reached_from_seen_state(self):
        st = MS.status(SEEN)
        got = {i["id"] for i in st["items"] if i["reached"]}
        self.assertEqual(got, {"nautiloid", "first_companion", "nautiloid_escaped", "crash_site", "beach",
                               "first_camp", "level_2"})
        self.assertEqual((st["reached"], st["act"], st["next"]), (7, 1, "Emerald Grove"))
        self.assertTrue(all(i["session"] is None for i in st["items"]))   # no events: "before tracking"

    def test_grove_is_behind_the_gate(self):
        # claude-run-2 S007: waypoint "Emerald Grove Environs" 00:12:37 (outside the gate), area "Emerald Grove" 00:13:52
        def grove(*areas):
            return {i["id"]: i["reached"] for i in MS.status(dict(SEEN, areas=list(areas)))["items"]}["grove"]
        self.assertFalse(grove("Wilderness|Emerald Grove Environs"))
        self.assertFalse(grove("Wilderness|Forest", "Wilderness|Chapel Entrance"))
        for a in ("Emerald Grove", "The Hollow", "Druids' Chambers"):
            self.assertTrue(grove("Wilderness|" + a), a)

    def test_first_session_from_events(self):
        sessions = [("S002", [ev("companion", "Shadowheart", 10)]),
                    ("S003", [ev("region", "Wilderness", 20), ev("level_up", "Claude -> level 2", 21),
                              ev("quest_done", "Escape the Nautiloid", 22), ev("area", "Ravaged Beach", 23)]),
                    ("S005", [{"type": "poll", "t": 30, "ok": True}, ev("area", "Campsite", 31)])]
        st = MS.status(SEEN, sessions)
        by = {i["id"]: (i["session"], i["t"]) for i in st["items"] if i["reached"]}
        self.assertEqual(by["nautiloid_escaped"], ("S003", 22))
        self.assertEqual(by["first_camp"], ("S005", 31))
        self.assertEqual(by["level_2"], ("S003", 21))
        self.assertEqual(by["nautiloid"], (None, None))             # baseline
        self.assertEqual(by["first_companion"], ("S002", 10))      # a companion joining, not a level-up

    def test_later_acts(self):
        seen = dict(SEEN, regions=SEEN["regions"] + ["Shadow-Cursed Lands"],
                    areas=SEEN["areas"] + ["Shadow-Cursed Lands|Moonrise Towers"])
        st = MS.status(seen)
        self.assertEqual(st["act"], 2)
        self.assertIn("moonrise", {i["id"] for i in st["items"] if i["reached"]})

    def test_line(self):
        line = MS.line(MS.status(SEEN, [("S005", [ev("area", "Campsite", 1)])]))
        self.assertTrue(line.startswith("milestones 7/20 (Act 1): ✓ Aboard the Nautiloid"))
        self.assertIn("✓ First camp (S005)", line)
        self.assertTrue(line.endswith("next: Emerald Grove"))

    def test_empty(self):
        st = MS.status(None)
        self.assertEqual((st["reached"], st["next"]), (0, "Aboard the Nautiloid"))


if __name__ == "__main__":
    unittest.main()
