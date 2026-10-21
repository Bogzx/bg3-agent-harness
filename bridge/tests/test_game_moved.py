"""Offline tests: a walk the game's story cut short by moving the walker (vis V9), from the real S066 shape.

S066 19:11:49-19:11:58: Claude's move_to (45.5,-53.3,1002) jumped 21.7 m into Orin's arena in one tick (a quest
update with it, her conversation ~5 s later). It read `FAILED — engine teleported the character (not allowed)`,
then regroup walked Karlach/Wyll/Shadowheart 16-20 m toward him until Orin's conversation (party=[Claude]) stopped
all three; Claude fought 3 rounds alone and the fight was reloaded. Actions.lua now settles such a jump and reports
it as the game's (by_game, event teleported_by_game); the Lua side is tested in tools/dev/test_act_lua.lua.

Run: cd bridge && uv run python -m unittest tests.test_game_moved
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_actq import FakeGame
from test_turn_macros import (
    Base,  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
)

from bg3ai import actq, longnav, party
from bg3ai.tools import core, plumbing, travel, batch, queueing, travel_macro
from bg3ai.game import JobResult

CLAUDE = "aaaaaaaa-0000-4000-8000-000000000004"
KARLACH = "aaaaaaaa-0000-4000-8000-000000000001"
WYLL = "c774d764-4a17-48dc-b470-32ace9ce447d"
SHADOWHEART = "3ed74f06-3c60-42dc-83f6-f034cb47c679"
LVL = "CTY_Main_A"
ARENA = [45.8, -53.3, 1003.3]  # where the game put Claude (S066 19:11:50)


def row(uuid, name, pos, **kw):
    return {"uuid": uuid, "name": name, "pos": pos, "region": LVL, "hp": 100, "max_hp": 100, "chained": True, **kw}


def s066_status(in_combat: bool = False) -> dict:
    """19:11:50: Claude in the arena, the others where the jump left them (22.7-24.9 m back, ~10 m above)."""
    return {"active": CLAUDE, "level": LVL, "subregion": "Bhaal Temple", "region": "Lower City",
            "in_combat": in_combat, "party": [
                row(CLAUDE, "Claude", ARENA),
                row(KARLACH, "Karlach", [26.0, -43.6, 997.0]),
                row(WYLL, "Wyll", [25.0, -43.0, 998.0]),
                row(SHADOWHEART, "Shadowheart", [24.0, -42.8, 996.5])]}


BY_GAME = {"pos": ARENA, "teleported": True, "by_game": True, "from": [27.3, -42.9, 998.8], "m": 21.7,
           "dest_m": 1.4, "why": "a quest updated"}


def label(u: str) -> str:
    return {CLAUDE: "e4 Claude", KARLACH: "e1 Karlach", WYLL: "e2 Wyll", SHADOWHEART: "e3 Shadowheart"}[u]


class GameMovedText(unittest.TestCase):
    def test_s066_names_the_move_and_who_follows(self):
        behind = party.moved_left_behind(s066_status(), CLAUDE)
        self.assertEqual([r["uuid"] for r in behind], [KARLACH, WYLL, SHADOWHEART])
        out = party.game_moved_text("move_to", BY_GAME, CLAUDE, label, "Bhaal Temple", behind, None)
        head, follow = out.split("\n")
        self.assertEqual(head, "move_to: INTERRUPTED — the game moved e4 Claude 22m to @(45.8,-53.3,1003.3) into "
                               "Bhaal Temple (a quest updated): not our walk, nothing to undo; the scene there is "
                               "theirs now")
        self.assertNotIn("not allowed", out)
        self.assertTrue(follow.startswith("PARTY LEFT BEHIND: e1 Karlach 23m, 10m above; e2 Wyll 24m"), follow)
        self.assertIn("→ regroup(leader=e4) walks them to e4 Claude", follow)

    def test_sealed_area_and_combat(self):
        behind = party.moved_left_behind(s066_status(), CLAUDE)
        sealed = party.game_moved_text("move_to", BY_GAME, CLAUDE, label, None, behind, KARLACH)
        self.assertIn("no walkable way from e1 Karlach to e4 Claude (a sealed story area?)", sealed)
        self.assertNotIn("regroup", sealed)
        fight = party.game_moved_text("move_to", BY_GAME, CLAUDE, label, None, behind, None, in_combat=True)
        self.assertIn("in combat: move them toward e4 Claude on their turns", fight)

    def test_nobody_left_behind(self):
        st = s066_status()
        for r in st["party"][1:]:
            r["pos"] = [ARENA[0] + 2, ARENA[1], ARENA[2] + 1]
        self.assertEqual(party.moved_left_behind(st, CLAUDE), [])
        out = party.game_moved_text("move_to", BY_GAME, CLAUDE, label, None, [], None)
        self.assertNotIn("PARTY LEFT BEHIND", out)


class Fake:
    def __init__(self, status, reachable=True):
        self.status, self.reachable, self.calls = status, reachable, []

    def try_rpc(self, op, args=None, context="server", default=None):
        self.calls.append((op, args))
        if op == "state.status":
            return self.status
        if op == "nav.path":
            return {"reachable": self.reachable, "length_m": 48.3}
        return default


class JobText(unittest.TestCase):
    def use(self, fake) -> None:
        saved = core.game
        self.addCleanup(lambda: setattr(core, "game", saved))
        core.game = fake
        for u, n in ((CLAUDE, "Claude"), (KARLACH, "Karlach"), (WYLL, "Wyll"), (SHADOWHEART, "Shadowheart")):
            core.H.get(u, n)

    def test_wait_for_shows_the_game_move_and_checks_one_way_back(self):
        fake = Fake(s066_status())
        self.use(fake)
        res = dict(BY_GAME, after={"pos": ARENA, "hp": 168, "max_hp": 168})
        out = plumbing._job_text("move_to", JobResult("interrupted", res, "the game moved the character (a quest "
                                                    "updated), not our walk"), CLAUDE)
        self.assertIn("the game moved " + queueing._label(CLAUDE) + " 22m", out)
        self.assertIn("into Bhaal Temple", out)
        self.assertIn("PARTY LEFT BEHIND", out)
        self.assertIn("regroup(leader=", out)
        paths = [a for op, a in fake.calls if op == "nav.path"]
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]["char"], KARLACH)  # the nearest one (22.9 m)
        self.assertEqual(paths[0]["pos"], ARENA)

    def test_no_way_for_them(self):
        self.use(Fake(s066_status(), reachable=False))
        out = plumbing._job_text("move_to", JobResult("interrupted", dict(BY_GAME), "x"), CLAUDE)
        self.assertIn("(a sealed story area?)", out)

    def test_the_engines_own_teleport_reads_as_before(self):
        self.use(Fake(s066_status()))
        res = {"pos": ARENA, "teleported": True, "from": [27.3, -42.9, 998.8], "m": 21.7, "dest_m": 1.4}
        out = plumbing._job_text("move_to", JobResult("failed", res, "engine teleported the character (not allowed)"))
        self.assertTrue(out.startswith("move_to: FAILED — engine teleported the character (not allowed)"), out)


# V15 S073 22:33:39: 9 s after the fast travel to Lower City Central Wall, the walk from the waypoint jumped its last
# 29.8 m onto its own end (Actions.lua tele_finish by_trip; the Lua side is in tools/dev/test_act_lua.lua).
S073_END, S073_FROM = [-47.7, 20.1, -102.2], [-48.5, 27.9, -73.5]
BY_TRIP = {"pos": S073_END, "teleported": True, "by_trip": True, "from": S073_FROM, "m": 29.8, "dest_m": 0,
           "dist": 0, "after_trip_s": 9}


class TripMoved(unittest.TestCase):
    def setUp(self):
        saved = core.game
        self.addCleanup(lambda: setattr(core, "game", saved))
        core.game = Fake(s066_status())
        core.H.get(CLAUDE, "Claude")

    def test_s073_the_walk_put_on_its_end_after_a_fast_travel_is_done(self):
        out = plumbing._job_text("move_to", JobResult("done", dict(BY_TRIP)), CLAUDE)
        self.assertEqual(out, f"move_to: done — @(-47.7,20.1,-102.2) dist=0: the game moved {queueing._label(CLAUDE)} "
                              "the last 29.8m to the walk's end in one jump from @(-48.5,27.9,-73.5), 9s after the "
                              "fast travel (not walked; nothing to undo)")
        self.assertNotIn("not allowed", out)

    def test_fast_travel_first_marks_the_arrival(self):
        calls = []

        class G:
            def try_rpc(self, op, args=None, context="server", default=None):
                calls.append(op)
                return {"waypoints": []} if op == "nav.waypoints" else default

            def run_job(self, op, args=None, context="server", wait_s=0):
                calls.append(op)
                return JobResult("done", {})

        class W:
            def try_rpc(self, op, args=None):
                return {"party": [{"uuid": CLAUDE, "pos": [-50.2, 27.2, -54.8]}]}

        core.game = G()
        wp = {"name": "Lower City Central Wall", "pos": [-50.2, 27.2, -54.8], "to_goal_m": 60.4}
        with mock.patch.object(travel_macro, "_pos_now", return_value=[150.0, 30.0, -100.0]), \
                mock.patch.object(longnav, "pick_waypoint", return_value=wp), \
                mock.patch.object(travel, "_leave_camp_first", return_value=""), \
                mock.patch.object(queueing, "_watch_conn", return_value=W()):
            line, left = batch._fast_travel_first(CLAUDE, [-76, 19, -112], 224.0)
        self.assertTrue(line.endswith(": done"), line)
        self.assertEqual(calls[calls.index("ui.fast_travel"):], ["ui.fast_travel", "act.trip_arrived"])


class QueueScope(unittest.TestCase):
    """A queue stops for its own member's conversation or game move, not for another member's."""

    def watcher(self, who):
        g = FakeGame()
        w = actq.Watcher(g, who=who)
        w.baseline()
        return g, w

    def test_s066_orins_conversation_does_not_stop_the_members_walking_to_claude(self):
        for who, stops in ((KARLACH, False), (CLAUDE, True), (None, True)):
            g, w = self.watcher(who)
            g.push("dialogue_started", {"speakers": [{"uuid": "orin"}], "party": [{"uuid": CLAUDE, "name": "Claude"}]})
            hit = w.check("travel_to", None, ui=False)
            self.assertEqual(hit is not None and hit[0] == "dialogue", stops, who)

    def test_a_game_move_stops_only_the_moved_members_queue(self):
        for who, stops in ((KARLACH, False), (CLAUDE, True)):
            g, w = self.watcher(who)
            g.push("teleported_by_game", {"uuid": CLAUDE, "m": 21.7, "why": "a quest updated"})
            hit = w.check("travel_to", None, ui=False)
            self.assertEqual(hit is not None and hit[0] == "teleport", stops, who)
            if stops:
                self.assertEqual(hit[1], f"the game moved {CLAUDE} (a quest updated)")

    def test_s073_a_walk_put_on_its_end_after_a_trip_stops_no_queue(self):
        for who in (CLAUDE, KARLACH, None):
            g, w = self.watcher(who)
            g.push("moved_after_trip", {"uuid": CLAUDE, "m": 29.8, "after_trip_s": 9})
            self.assertIsNone(w.check("travel_to", None, ui=False), who)

    def test_a_blocked_teleport_still_stops_every_queue(self):
        g, w = self.watcher(KARLACH)
        g.push("teleport_blocked", {"uuid": CLAUDE})
        self.assertEqual(w.check("travel_to", None, ui=False)[0], "teleport")


if __name__ == "__main__":
    unittest.main()
