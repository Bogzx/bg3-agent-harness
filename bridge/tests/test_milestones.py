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

    def test_act3_from_claude_run_2(self):
        # claude-run-2 at S065 (progress_state.json, trimmed): Wyrm's Rock and Gortash done, Bhaal Temple before Orin
        seen = dict(SEEN, regions=SEEN["regions"] + ["Lower City", "Rivington & Wyrm's Crossing"],
                    areas=SEEN["areas"] + ["Rivington & Wyrm's Crossing|Wyrm's Rock Fortress", "Lower City|Bhaal Temple"],
                    quests=dict(SEEN["quests"], **{"Get Gortash's Netherstone": {"done": True},
                                                   "Get Orin's Netherstone": {"done": False},
                                                   "Confront the Elder Brain": {"done": False}}))
        st = MS.status(seen)
        got = {i["id"] for i in st["items"] if i["reached"] and i["act"] == 3}
        self.assertEqual(got, {"act3", "lower_city", "wyrms_rock", "gortash", "bhaal_temple"})
        self.assertEqual((st["act"], st["next"], st["ended"]), (3, "Orin's Netherstone", False))   # Iron Throne: optional

    def test_cazador_from_claude_run_2(self):
        # S077 (01:05-01:09): Cazador killed, "Investigate Cazador's Palace" done; the runner still read 21/30 and
        # "next Morphic Pool". "The Pale Elf" closed in S074 on Astarion's death in a reloaded try: not a kill.
        base = dict(SEEN, regions=SEEN["regions"] + ["Lower City"], areas=SEEN["areas"] + ["Lower City|Bhaal Temple"],
                    quests=dict(SEEN["quests"], **{"Get Orin's Netherstone": {"done": True},
                                                   "The Pale Elf": {"done": True},
                                                   "Call of the Blood": {"done": False}}))

        def caz(**quests):
            st = MS.status(dict(base, quests=dict(base["quests"], **quests)))
            return {i["id"]: i["reached"] for i in st["items"]}["cazador"], st
        self.assertFalse(caz()[0])
        self.assertTrue(caz(**{"Call of the Blood": {"done": True}})[0])
        got, st = caz(**{"Investigate Cazador's Palace": {"done": True}})
        self.assertTrue(got)
        self.assertEqual(st["next"], "Morphic Pool")      # optional: counts, never the next one

    def test_optional_never_moves_next(self):
        # the Iron Throne (optional) reached before Gortash's Netherstone: Gortash stays next
        seen = dict(SEEN, regions=SEEN["regions"] + ["Lower City", "Iron Throne"],
                    areas=SEEN["areas"] + ["Rivington & Wyrm's Crossing|Wyrm's Rock Fortress"])
        self.assertEqual(MS.status(seen)["next"], "Gortash's Netherstone")

    def test_game_end(self):
        st = MS.status(dict(SEEN, game_end={"t": 1, "how": "the epilogue (EPI_Main_A)"}))
        self.assertTrue(st["ended"])
        self.assertIsNone(st["next"])
        self.assertEqual(MS.MILESTONES[-1][0], MS.GAME_END)
        by = {i["id"]: i for i in MS.status(SEEN, [("S099", [ev("game_end", "the end credits", 5)])])["items"]}
        self.assertEqual((by["game_end"]["reached"], by["game_end"]["session"]), (True, "S099"))
        self.assertFalse(MS.status(SEEN)["ended"])

    def test_line(self):
        line = MS.line(MS.status(SEEN, [("S005", [ev("area", "Campsite", 1)])]))
        self.assertTrue(line.startswith(f"milestones 7/{len(MS.MILESTONES)} (Act 1): ✓ Aboard the Nautiloid"))
        self.assertIn("✓ First camp (S005)", line)
        self.assertTrue(line.endswith("next: Emerald Grove"))

    def test_empty(self):
        st = MS.status(None)
        self.assertEqual((st["reached"], st["next"]), (0, "Aboard the Nautiloid"))


if __name__ == "__main__":
    unittest.main()
