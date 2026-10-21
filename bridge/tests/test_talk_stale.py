"""Offline tests: talk_to / interact on someone out of sight never travels on a stale or far sighting (vis V9).

S067 #129 (19:53): after the long rest at camp, `look` listed `CAMP e254 Gale Wizard6(Evocation) level 6, wants to
talk (at camp, not in the party)`. talk_to e254 answered `e254 is out of sight: going to where you last saw them 201s
ago (40.7,-49.2,989.9)` and fast-travelled the whole party to the Temple of Bhaal (3870 m); the agent cancelled and
camped back (#130-#131).

Run: cd bridge && uv run python -m unittest tests.test_talk_stale
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_turn_macros import (
    Base,  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
)

from bg3ai import actq
from bg3ai.tools import core, travel, batch, queueing, travel_macro

CLAUDE = "aaaaaaaa-0000-4000-8000-000000000004"
GALE = "c7c13742-bacd-460a-8f65-f864fe41f255"
CAMP_POS = [-1421.2, 8.4, -2601.2]
TEMPLE_SEEN = [40.7, -49.2, 989.9]
TEMPLE_WP = {"name": "Temple of Bhaal", "id": "WAYP_LOW_BhaalTemple", "pos": [-58.4, -46.3, 1005.9]}
REFUSED = "talk_to: FAILED — target is not visible"


class Fake:
    def __init__(self, camp=True, here=CAMP_POS, seen=TEMPLE_SEEN, camp_pos=None):
        self.camp, self.here, self.seen, self.calls = camp, here, seen, []
        self.camp_pos = camp_pos   # state.camp's pos for the camp member (only while the party is in its camp)

    def try_rpc(self, op, args=None, context="server", default=None):
        self.calls.append(op)
        if op == "state.status":
            rows = [{"uuid": GALE, "name": "Gale", "level": 6, "wants_talk": True}] if self.camp else []
            return {"active": CLAUDE, "party": [{"uuid": CLAUDE, "name": "Claude", "pos": self.here}], "camp": rows}
        if op == "act.watch":
            return {"combat": False, "party": [{"uuid": CLAUDE, "pos": self.here}]}
        if op == "state.entity":
            return {"uuid": GALE, "visible": False, "last_pos": self.seen, "ago_s": 201}
        if op == "ui.state":
            return {"screen": "game", "region": "Campsite" if self.here == CAMP_POS else "Bhaal Temple"}
        if op == "nav.waypoints":
            return {"waypoints": [TEMPLE_WP]}
        if op == "state.camp":
            return {"camp": [{"uuid": GALE, "name": "Gale", "pos": self.camp_pos}] if self.camp else []}
        return default

    rpc = try_rpc


class StaleSighting(unittest.TestCase):
    def use(self, fake: Fake) -> list:
        saved = (core.game, queueing._watch_game, travel_macro.travel_to, batch._enqueue)
        self.addCleanup(lambda: (setattr(core, "game", saved[0]), setattr(queueing, "_watch_game", saved[1]),
                                 setattr(travel_macro, "travel_to", saved[2]), setattr(batch, "_enqueue", saved[3])))
        trips: list = []
        core.game = queueing._watch_game = fake
        travel_macro.travel_to = lambda **kw: trips.append(kw) or "travel_to: started (98.7m)"
        batch._enqueue = lambda char, steps, ignore=None: "queued 1 step(s)"
        core.H.get(CLAUDE, "Claude")
        core.H.get(GALE, "Gale")
        return trips

    def walk_first(self):
        return travel._walk_first("talk_to", REFUSED, CLAUDE, None, core.H.get(GALE),
                                  actq.Step("talk_to", {"npc": "e"}))

    def test_s067_129_a_camp_member_is_found_at_camp_not_at_the_temple(self):
        trips = self.use(Fake())
        out = self.walk_first()
        self.assertEqual(trips, [])
        self.assertTrue(out.startswith(REFUSED), out)
        self.assertIn("is at camp now (the camp roster), not where the party last saw them: walk around the camp", out)
        self.assertNotIn("going to where you last saw them", out)

    def test_s080_69_at_camp_it_walks_to_the_camp_roster_spot(self):
        # S080 02:34:21: talk_to Karlach at camp → "not visible … walk around the camp to find them"; get_nearby 40 m
        # showed only Withers and Scratch and the agent gave up the swap. state.camp says where she stands: go there.
        spot = [-1466.0, 9.1, -2640.5]
        trips = self.use(Fake(camp_pos=spot))
        out = self.walk_first()
        self.assertEqual(trips, [{"who": None, "x": -1466.0, "y": 9.1, "z": -2640.5, "trespass": True}])
        self.assertIn("is at camp (the camp roster) (-1466,9.1,-2640.5): walking there first; then talk_to", out)
        self.assertNotIn("walk around the camp", out)

    def test_away_from_camp_it_says_camp(self):
        trips = self.use(Fake(here=[-14.0, -46.0, 1003.0]))
        out = self.walk_first()
        self.assertEqual(trips, [])
        self.assertIn("camp() takes the party there", out)

    def test_a_far_sighting_is_asked_not_travelled(self):
        trips = self.use(Fake(camp=False))   # anyone not on the camp roster, last seen 3870 m away
        out = self.walk_first()
        self.assertEqual(trips, [])
        self.assertIn("NOT TRAVELLED: " + core.H.get(GALE) + " is out of sight; the party last saw them 201s ago "
                      "(40.7,-49.2,989.9), 3878m away (a fast travel to Temple of Bhaal + a 100m walk)", out)
        self.assertIn("To go anyway: travel_to x=40.7, y=-49.2, z=989.9, then talk_to", out)

    def test_a_near_sighting_still_walks_there(self):
        trips = self.use(Fake(camp=False, here=[0.0, -49.2, 989.9]))   # 40.7 m off
        out = self.walk_first()
        self.assertEqual(trips, [{"who": None, "x": 40.7, "y": -49.2, "z": 989.9, "trespass": True}])
        self.assertIn("going to where you last saw them 201s ago", out)


if __name__ == "__main__":
    unittest.main()
