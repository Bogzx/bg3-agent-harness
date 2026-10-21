"""Offline tests: party cohesion (S005: an unchained Shadowheart stayed 77 m behind and regroup's one long
move was refused). PARTY APART line, re-chaining, the leader's travel_to waiting for stragglers, regroup in
legs, stay.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import time
import unittest

from bg3ai import actq, party, render
from bg3ai.tools import core, overview, plumbing, queueing, travel_macro
from test_turn_macros import P1, P2, Base

P3 = "33333333-2222-3333-4444-555555555555"


def status(p2: dict | None = None, p3: dict | None = None) -> dict:
    rows = [{"uuid": P1, "name": "Claude", "pos": [0, 0, 0], "region": "WLD"},
            dict({"uuid": P2, "name": "Lae'zel", "pos": [1, 0, 0], "region": "WLD", "chained": True}, **(p2 or {}))]
    if p3 is not None:
        rows.append(dict({"uuid": P3, "name": "Gale", "pos": [2, 0, 0], "region": "WLD", "chained": True}, **p3))
    return {"active": P1, "level": "WLD", "party": rows}


class Pure(unittest.TestCase):
    def label(self, u: str) -> str:
        return {P2: "e2 Lae'zel", P3: "e3 Gale"}[u]

    def test_together_says_nothing(self):
        self.assertEqual(party.apart_line(party.apart(status()), self.label), "")

    def test_far_and_other_level(self):
        st = status({"pos": [34, 0, 0]}, {"region": "TUT", "pos": [900, 0, 0]})
        line = party.apart_line(party.apart(st), self.label)
        self.assertEqual(line, "PARTY APART: e2 Lae'zel 34m, e3 Gale other level → regroup()")

    def test_unchained_near_and_staying(self):
        st = status({"chained": False}, {"pos": [40, 0, 0]})
        rows = party.apart(st, stay={P3})
        self.assertEqual(party.apart_line(rows, self.label),
                         "PARTY APART: e2 Lae'zel near, unchained, e3 Gale 40m (staying) → regroup()")
        self.assertEqual(party.rechain_candidates(rows, lambda u: False), [P2])
        self.assertEqual(party.rechain_candidates(rows, lambda u: True), [])

    def test_dead_and_old_mod_rows_are_ignored(self):
        st = status({"dead": True, "pos": [50, 0, 0]}, {"chained": None, "region": None})
        self.assertEqual(party.apart(st), [])

    def test_member_on_another_floor_close_by(self):
        # S015: Claude jumped 5 m down from a ledge; Gale stayed up there 7 m away with no path down
        st = status({"pos": [5, 5.2, 4]}, {"pos": [2, 1.5, 0]})
        line = party.apart_line(party.apart(st), self.label)
        self.assertEqual(line, "PARTY APART: e2 Lae'zel 8m, 5m above → regroup()")
        st = status({"pos": [30, -6, 0]})
        self.assertEqual(party.apart_line(party.apart(st), self.label), "PARTY APART: e2 Lae'zel 31m, 6m below → regroup()")

    def test_stragglers(self):
        rows = [{"uuid": P2, "pos": [20, 0, 0]}, {"uuid": P3, "pos": [3, 0, 0]}, {"uuid": "x", "pos": [30, 0, 0], "downed": True}]
        self.assertEqual([u for u, _ in party.stragglers(rows, [0, 0, 0])], [P2])
        self.assertEqual(party.stragglers(rows, [0, 0, 0], stay={P2}), [])

    def test_summons_are_never_apart(self):
        # S017 probe: a chained `Spiritual Weapon` row in the party; unchained and far after combat
        st = status({"pos": [40, 0, 0], "chained": False, "summon": True, "owner": P1}, {"pos": [3, 9, 0]})
        rows = party.apart(st)
        self.assertEqual([r["uuid"] for r in rows], [P3])
        self.assertEqual(party.rechain_candidates(rows, lambda u: False), [])
        self.assertEqual(party.summons(st), {P2})

    def test_summon_leader_reports_nothing(self):
        st = status({"pos": [40, 0, 0]})
        st["party"][0]["summon"] = True
        self.assertEqual(party.apart(st), [])

    def test_stragglers_skip_summons(self):
        rows = [{"uuid": P2, "pos": [20, 0, 0]}, {"uuid": P3, "pos": [25, 0, 0], "summon": True}]
        self.assertEqual([u for u, _ in party.stragglers(rows, [0, 0, 0])], [P2])
        self.assertEqual(party.stragglers(rows, [0, 0, 0], skip={P2}), [])

    def test_party_line_tags_summon(self):
        H = render.Handles()
        H.get(P1, "Claude")
        line = render.character_line({"uuid": P2, "name": "Spiritual Weapon", "summon": True, "owner": P1}, H)
        self.assertTrue(line.endswith("summon of " + H.get(P1)), line)


class Cohesion(Base):
    def setUp(self) -> None:
        super().setUp()
        overview._stay.clear()
        self.addCleanup(overview._stay.clear)
        saved, party.CATCH_UP_S = party.CATCH_UP_S, 0.3
        self.addCleanup(setattr, party, "CATCH_UP_S", saved)
        self.st = status()
        orig = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "state.status":
                return self.st
            return orig(op, args, context)
        self.g.rpc = rpc

    def chains(self) -> list:
        return [a["char"] for _, a in self.g.ops("act.chain")]

    def test_get_status_warns_and_rechains(self):
        self.st = status({"chained": False, "pos": [34, 0, 0]})
        out = overview.get_status()
        self.assertEqual(self.chains(), [P2])
        self.assertIn("PARTY APART: ", out)
        self.assertIn("34m", out)
        self.assertIn("re-chained to ", out)

    def test_no_party_apart_at_camp(self):
        # S014: companions idle at their tents 16-23 m away while long resting: not a split
        self.st = status({"pos": [20, 0, 0]})
        self.assertEqual(overview._party_check(self.st, ui={"region": "Campsite"}), "")
        self.assertIn("PARTY APART: ", overview._party_check(self.st, ui={"region": "Blighted Village"}))

    def test_floor_split_survives_rechain_filter(self):
        self.st = status({"chained": False, "pos": [4, 5.5, 4]})
        out = overview._party_check(self.st)
        self.assertEqual(self.chains(), [P2])
        self.assertIn("6m above", out)
        self.assertIn("re-chained to ", out)

    def test_staying_member_is_left_alone(self):
        self.st = status({"chained": False, "pos": [34, 0, 0]})
        r = overview.stay(core.H.get(P2))
        self.assertIn("stays put", r)
        self.assertEqual([a["char"] for _, a in self.g.ops("act.unchain")], [P2])
        out = overview.look()
        self.assertEqual(self.chains(), [])
        self.assertIn("(staying)", out)

    def test_companion_queue_end_rechains_it(self):
        self.st = status({"chained": False})
        queueing._on_queue_report(P2, actq.Report(done=1))
        self.assertEqual(self.chains(), [P2])
        queueing._on_queue_report(P1, actq.Report(done=1))  # the leader: nothing to chain
        self.assertEqual(self.chains(), [P2])

    def test_regroup_names_a_member_on_another_level_and_chains(self):
        self.st = status({"region": "TUT"}, {"chained": False})
        self.g.watch["party"].append({"uuid": P3, "hp": 20, "max_hp": 20, "pos": [2, 0, 0]})
        core.H.get(P3, "Gale")
        out = travel_macro.regroup()
        self.assertIn("on another level (TUT)", out)
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual(self.chains(), [P3])

    def test_regroup_leaves_a_member_on_its_own_queue(self):
        # S035 19:28: regroup replaced Shadowheart's own move_to + jump_to queue with a travel_to that had no way up.
        self.st = status({"chained": False})
        self.g.watch["party"][1]["pos"] = [40, 0, 0]
        q = queueing._queues()
        orig = q.busy
        q.busy = lambda u: u == P2
        self.addCleanup(setattr, q, "busy", orig)
        out = travel_macro.regroup()
        self.assertIn("left on its own queue", out)
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertEqual(self.chains(), [])

    def test_far_unchained_summon_is_left_alone(self):
        self.st = status({"chained": False, "pos": [34, 0, 0], "summon": True})
        self.assertNotIn("PARTY APART", overview.get_status())
        self.assertEqual(self.chains(), [])
        queueing._on_queue_report(P2, actq.Report(done=1))
        self.assertEqual(self.chains(), [])

    def test_regroup_does_not_walk_or_chain_a_summon(self):
        self.st = status({"chained": False, "summon": True}, {"chained": False})
        self.g.watch["party"][1]["pos"] = [40, 0, 0]
        self.g.watch["party"].append({"uuid": P3, "hp": 20, "max_hp": 20, "pos": [2, 0, 0]})
        core.H.get(P3, "Gale")
        travel_macro.regroup()
        self.assertEqual([a["char"] for _, a in self.g.ops("act.move_to")], [])
        self.assertEqual(self.chains(), [P3])

    def test_leader_trip_does_not_wait_for_a_summon(self):
        self.st = status({"summon": True})
        out = self._trip(follower_moves=False)
        self.assertNotIn("behind", out)
        self.assertIn("arrived", out)

    def _trip(self, follower_moves: bool) -> str:
        plumbing.SETTLE_S = 10.0
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def reach(a):
            here = [m for m in self.g.ops("act.move_to") if m[1]["char"] == a.get("char", P1)]
            x = here[-1][1]["pos"][0] if here else 0.0
            dest = a["pos"][0]
            step = max(-a["max_m"], min(a["max_m"], dest - x))
            return {"reachable": True, "pos": [x + step, 0.0, 0.0]}
        self.g.reach = reach

        def walk(op, a):
            if op != "act.move_to":
                return
            if a.get("char") == P1:
                self.g.watch["party"][0]["pos"] = list(a["pos"])
            elif follower_moves:  # the walk back works
                self.g.watch["party"][1]["pos"] = list(a["pos"])
        self.g.on_start = walk
        return travel_macro.travel_to(x=30.0, y=0.0, z=0.0)

    def test_leader_trip_walks_a_straggler_back(self):
        out = self._trip(follower_moves=True)
        p2 = [a for _, a in self.g.ops("act.move_to") if a.get("char") == P2]
        self.assertTrue(p2, out)
        self.assertIn("fell behind: walked back", out)
        self.assertIn("arrived", out)

    def test_leader_trip_stops_when_a_member_cannot_keep_up(self):
        out = self._trip(follower_moves=False)
        self.assertIn("can't keep up", out)
        self.assertNotIn("arrived", out)

    def test_camp_walk_does_not_wait_for_companions_at_their_tents(self):
        # Logs pass 4 #5: every Campsite walk stopped "e4 Karlach 37m … can't keep up" (companions idle at camp)
        self.g.ui = {"region": "Campsite"}
        out = self._trip(follower_moves=False)
        self.assertNotIn("behind", out)
        self.assertIn("arrived", out)
        self.assertEqual([a for _, a in self.g.ops("act.move_to") if a.get("char") == P2], [])

    def test_a_follower_still_closing_in_is_waited_for_not_walked_back(self):
        # A running leader's 30 m leg: the follower trails 25 m behind, then follows at 8 m/s, so it needs ~1.6 s
        # to come within STRAGGLER_M: past CATCH_UP_S (0.3 s here) it is still waited for, no walk back, no stop.
        orig, t_arr = self.g.rpc, []

        def rpc(op, args=None, context="server", timeout=12.0):
            r = orig(op, args, context, timeout) if op != "state.status" else self.st
            if op == "act.watch" and self.g.watch["party"][0]["pos"][0] >= 30.0:
                if not t_arr:
                    t_arr.append(time.monotonic())
                self.g.watch["party"][1]["pos"][0] = min(30.0, 5.0 + 8.0 * (time.monotonic() - t_arr[0]))
            return r
        self.g.rpc = rpc
        out = self._trip(follower_moves=False)
        self.assertNotIn("can't keep up", out)
        self.assertIn("caught up", out)
        self.assertEqual([a for _, a in self.g.ops("act.move_to") if a.get("char") == P2], [])

    def test_staying_member_is_not_waited_for(self):
        overview._stay.add(P2)
        out = self._trip(follower_moves=False)
        self.assertNotIn("behind", out)
        self.assertIn("arrived", out)


if __name__ == "__main__":
    unittest.main()
