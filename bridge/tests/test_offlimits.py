"""Offline tests: off-limits areas the game warned the party about (vis V26).

S084 04:08-04:12 (the Counting House): Mofty's trespass talk at the upstairs Double Doors ended "left off with a
warning. This time."; at 04:11:45 travel_to e578 (Grated Iron Doors) walked into the vault guards' area, Dadger's
talk said "This area appears to be off-limits" and the second offence started a fight (reload, ~2 min). The bridge
remembers where each warned talk began (offlimits.py) and passes the areas to act.move_to as `avoid`; the walk
itself (round the area, refused inside it) is tested in tools/dev/test_act_lua.lua.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing server
    P1,
    Base,
)

from bg3ai import offlimits, server

LEVEL = "CTY_Main_A"
MOFTY = "12121212-bbbb-cccc-dddd-eeeeeeeeeeee"
DADGER = "34343434-bbbb-cccc-dddd-eeeeeeeeeeee"
WARNED = "*It seems you've made a rather convincing case to be left off with a warning. This time.*"
OFF = "*This area appears to be off-limits. Perhaps you should wander elsewhere.*"


def talk(guard: str, name: str, at: list) -> dict:
    return {"kind": "dialogue_started", "data": {"party": [{"uuid": P1, "name": "Claude"}],
                                                 "speakers": [{"uuid": guard, "name": name}], "at": at,
                                                 "level": LEVEL}}


def line(text: str, speaker: str = "Narrator") -> dict:
    return {"kind": "dialogue_line", "data": {"speaker": speaker, "text": text}}


class Tracker(unittest.TestCase):
    """S084's events, in the order the session got them."""

    def test_s084_the_warning_and_the_off_limits_line_each_mark_where_the_talk_began(self):
        o = offlimits.OffLimits()
        # 04:08:52 interact e501 -> Mofty: "messing with forbidden items", Persuasion, "...with a warning. This time."
        o.feed([talk(MOFTY, "Cashguard Basher Mofty", [-113.7, 12.5, -184.9]),
                line("*The counting house guard is very unhappy about you messing with forbidden items.*")], now=0)
        self.assertEqual(o.areas, [])  # not the trespass line yet
        o.feed([line(WARNED)], now=14)
        self.assertEqual(len(o.areas), 1)
        self.assertEqual(o.areas[0]["pos"], [-113.7, 12.5, -184.9])
        self.assertEqual(o.name(o.areas[0]), "the off-limits area by Cashguard Basher Mofty (-114,12,-185)")
        # 04:12:01 Dadger's talk downstairs (the second offence)
        o.feed([talk(DADGER, "Cashguard Artillerist Dadger", [-111.0, 4.0, -177.0]), line(OFF)], now=190)
        self.assertEqual([a["guard"] for a in o.areas], ["Cashguard Basher Mofty", "Cashguard Artillerist Dadger"])
        # the reload, then the same place warned again: still one area there
        o.feed([talk(DADGER, "Cashguard Artillerist Dadger", [-110.0, 4.0, -178.0]), line(OFF)], now=240)
        self.assertEqual(len(o.areas), 2)
        self.assertEqual(o.avoid()[1], {"id": 2, "pos": [-111.0, 4.0, -177.0], "r": offlimits.RADIUS_M,
                                        "level": LEVEL,
                                        "name": "the off-limits area by Cashguard Artillerist Dadger (-111,4,-177)"})

    def test_only_the_narrators_line_in_a_talk_with_the_party(self):
        o = offlimits.OffLimits()
        o.feed([line(OFF)], now=0)  # no talk began (no place known)
        o.feed([talk(DADGER, "Dadger", [0, 0, 0]), line("This area is off-limits!", speaker="Dadger")], now=1)
        o.feed([line("*The vault door groans open.*")], now=2)
        o.feed([line(OFF)], now=2 + offlimits.PAIR_S + 1)  # long after that talk
        bark = talk(DADGER, "Dadger", [5, 0, 5])
        bark["data"]["automated"] = True
        o.feed([bark, line(OFF)], now=500)  # a bark while walking: the talk before it was long ago
        self.assertEqual(o.areas, [])

    def test_route_line_says_the_goal_is_inside_or_the_way_goes_round(self):
        o = offlimits.OffLimits()
        o.feed([talk(DADGER, "Cashguard Artillerist Dadger", [-111.0, 4.0, -177.0]), line(OFF)], now=0)
        inside = o.route_line(LEVEL, [-92.2, 5.7, -188.2], [-112.0, 4.0, -176.0], [])
        self.assertIn("OFF-LIMITS: the goal is inside the off-limits area by Cashguard Artillerist Dadger (-111,4,-177)"
                      " you were warned about", inside)
        self.assertIn("trespass=true", inside)
        legs = [{"to": [-111.0, 4.0, -177.0]}, {"to": [-122.0, 4.0, -177.0]}]
        self.assertIn("avoiding the off-limits area by Cashguard Artillerist Dadger (-111,4,-177) you were warned"
                      " about", o.route_line(LEVEL, [-100.0, 4.0, -177.0], [-122.0, 4.0, -177.0], legs))
        self.assertEqual(o.route_line(LEVEL, [-100.0, 12.5, -177.0], [-122.0, 12.5, -177.0],
                                      [{"to": [-122.0, 12.5, -177.0]}]), "")  # the floor above
        self.assertEqual(o.route_line("SCL_Main_A", [-92.2, 5.7, -188.2], [-112.0, 4.0, -176.0], []), "")
        self.assertEqual(o.route_line(LEVEL, [-109.0, 4.0, -177.0], [-120.0, 4.0, -177.0],
                                      [{"to": [-120.0, 4.0, -177.0]}]), "")  # walking out of it


class Walks(Base):
    def setUp(self) -> None:
        super().setUp()
        saved, server.OFFLIMITS = server.OFFLIMITS, offlimits.OffLimits()
        self.addCleanup(setattr, server, "OFFLIMITS", saved)

    def warn(self) -> str:
        self.g.push("dialogue_started", talk(DADGER, "Cashguard Artillerist Dadger", [-111.0, 4.0, -177.0])["data"])
        self.g.push("dialogue_line", {"speaker": "Narrator", "text": OFF})
        return server._events_text()

    def test_a_warned_area_goes_to_every_walk_and_the_events_dont_show_where(self):
        server.move_to(x=1, y=0, z=1)
        self.assertNotIn("avoid", self.g.ops("act.move_to")[-1][1])  # nothing warned yet
        shown = self.warn()
        self.assertIn("dialogue_started", shown)
        self.assertNotIn("at=", shown)
        self.assertNotIn(LEVEL, shown)
        server.move_to(x=1, y=0, z=1)
        avoid = self.g.ops("act.move_to")[-1][1]["avoid"]
        self.assertEqual([a["name"] for a in avoid],
                         ["the off-limits area by Cashguard Artillerist Dadger (-111,4,-177)"])

    def test_trespass_true_walks_without_it(self):
        self.warn()
        server.move_to(x=1, y=0, z=1, trespass=True)
        args = self.g.ops("act.move_to")[-1][1]
        self.assertTrue(args["trespass"])
        self.assertNotIn("avoid", args)

    def test_route_says_it_avoids_the_area(self):
        self.warn()
        base = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "nav.route":
                return {"from": [-100.0, 4.0, -177.0], "goal": [-122.0, 4.0, -177.0], "straight_m": 22,
                        "reachable": True, "length_m": 22, "legs": [{"to": [-122.0, 4.0, -177.0], "m": 22}]}
            if op == "state.status":
                return {"level": LEVEL, "active": P1, "party": [{"uuid": P1, "name": "Claude"}]}
            return base(op, args, context, timeout)
        self.g.rpc = rpc
        out = server.route(x=-122.0, y=4.0, z=-177.0)
        self.assertIn("avoiding the off-limits area by Cashguard Artillerist Dadger (-111,4,-177) you were warned"
                      " about", out)


if __name__ == "__main__":
    unittest.main()
