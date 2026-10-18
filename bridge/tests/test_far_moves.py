"""Offline tests: far moves go via travel_to (S005: 'destination is too far away' ×4), travel_to to a far
target walks legs to where it stands, the NOTES WINDOW hint while a long walk runs, and the ToolSearch
preload list (S002-S007 usage).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import time
import unittest

from test_turn_macros import P1, P2, Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import server  # noqa: E402


class FarMoves(Base):
    def setUp(self) -> None:
        super().setUp()
        server._trips.clear()
        server._notes_shown.clear()
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def walked(op, a):  # the walker ends where the leg went (act.watch follows)
            if op == "act.move_to" and a.get("pos"):
                row = next(r for r in self.g.watch["party"] if r["uuid"] == a["char"])
                row["pos"] = list(a["pos"])
        self.g.on_start = walked

        def reach(a):
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            return {"reachable": True, "pos": [min(a["pos"][0], x + a["max_m"]), 0.0, 0.0]}
        self.g.reach = reach

    def test_a_far_point_move_goes_via_travel_to(self):
        out = server.move_to(x=80.0, y=0.0, z=0.0)
        self.assertIn("move_to: 80m is past one walk (58m): went via travel_to", out)
        self.assertIn("travel_to (80,0,0): started", out)
        server.wait_for("queue", timeout_s=5)
        legs = [a["pos"][0] for _, a in self.g.ops("act.move_to")]
        self.assertEqual(legs, [30.0, 60.0, 80.0])  # every leg within one walk

    def test_a_near_move_stays_a_plain_move(self):
        out = server.move_to(x=20.0, y=0.0, z=0.0)
        self.assertNotIn("travel_to", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_in_combat_a_far_move_is_not_rerouted(self):
        self.g.watch["combat"] = True
        out = server.move_to(x=80.0, y=0.0, z=0.0)
        self.assertNotIn("travel_to", out)
        self.assertEqual(self.g.ops("act.move_to")[0][1]["pos"], [80.0, 0.0, 0.0])

    def test_a_far_move_inside_do_actions_walks_legs(self):
        out = server.do_actions('[{"tool": "move_to", "x": 70, "y": 0, "z": 0}]')
        self.assertNotIn("FAILED", out)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [30.0, 60.0, 70.0])

    def test_travel_to_a_far_party_member_walks_legs_to_them(self):
        # S005: travel_to(target=e2, who=e1) was one move_to to e2 → 'destination is too far away'.
        self.g.watch["party"][1]["pos"] = [77.0, 0.0, 0.0]
        out = server.do_actions(f'[{{"tool": "travel_to", "target": "{server.H.get(P2)}"}}]')
        self.assertNotIn("FAILED", out)
        moves = self.g.ops("act.move_to")
        self.assertTrue(all("target" not in a for _, a in moves))
        self.assertEqual([a["pos"][0] for _, a in moves], [30.0, 60.0, 77.0])

    def test_travel_to_a_near_target_still_walks_to_it(self):
        self.g.watch["party"][1]["pos"] = [5.0, 0.0, 0.0]
        server.do_actions(f'[{{"tool": "travel_to", "target": "{server.H.get(P2)}"}}]')
        self.assertEqual(self.g.ops("act.move_to")[0][1].get("target"), P2)

    def test_far_visible_entity_position_from_state_entity(self):
        real = self.g.rpc
        far = "cccccccc-2222-3333-4444-555555555555"
        self.g.rpc = lambda op, a=None, context="server", timeout=12.0: (
            {"pos": [90.0, 0.0, 0.0]} if op == "state.entity" else real(op, a, context, timeout))
        self.g.visible.add(far)
        h = server.H.get(far, "Statue")
        dest, d = server._far_move(P1, far, None)
        self.assertEqual((dest, d), ([90.0, 0.0, 0.0], 90.0))
        self.assertTrue(h)

    def _npc(self, pos):
        """A visible NPC as state.entity sends it (S083 Nocturne); pos=None: a mod without the position."""
        real = self.g.rpc
        npc = "cccccccc-4444-3333-4444-555555555555"
        row = {"uuid": npc, "name": "Nocturne", "relation": "neutral", **({"pos": pos} if pos else {})}
        self.g.rpc = lambda op, a=None, context="server", timeout=12.0: (
            dict(row) if op == "state.entity" and (a or {}).get("uuid") == npc else real(op, a, context, timeout))
        self.g.visible.add(npc)
        return npc, server.H.get(npc, "Nocturne")

    def test_travel_to_a_far_npc_walks_legs_to_where_it_stands(self):
        # S083 03:44:25: travel_to(target=e444 Nocturne) 90 m off was one move_to → "too far away (90m, max 60m)".
        _, h = self._npc([90.0, 0.0, 0.0])
        out = server.travel_to(target=h)
        self.assertNotIn("FAILED", out)
        server.wait_for("queue", timeout_s=5)
        moves = self.g.ops("act.move_to")
        self.assertTrue(all("target" not in a for _, a in moves))
        self.assertEqual([a["pos"][0] for _, a in moves], [30.0, 60.0, 90.0])

    def test_a_failed_walk_to_a_target_stops_the_queue(self):
        # S083 03:44:32: the one move_to failed and the queue still said `queue_done done=1`, then ran the talk_to.
        _, h = self._npc(None)
        self.g.refuse_if = lambda op, a: ("destination is too far away (90m from this character, max 60m)"
                                          if op == "act.move_to" else None)
        server.travel_to(target=h)
        server.queue_actions(f'[{{"tool": "talk_to", "npc": "{h}"}}]')
        out = server.wait_for("queue", timeout_s=5)
        self.assertIn("STOPPED: step 1 travel_to", out)
        self.assertNotIn("queue_done", out)
        self.assertEqual(self.g.ops("act.talk"), [])


class LastSeen(FarMoves):
    """S025/S026: travel_to/route/talk_to on someone out of sight refused "not visible" (10x in two sessions); the
    party remembers where it saw them on this level, so those calls go there instead."""
    NPC = "dddddddd-2222-3333-4444-555555555555"
    FAR = "eeeeeeee-2222-3333-4444-555555555555"

    def setUp(self) -> None:
        super().setUp()
        real = self.g.rpc
        self.route_args: list = []

        def rpc(op, a=None, context="server", timeout=12.0):
            if op == "state.entity" and (a or {}).get("uuid") == self.FAR:
                return {"uuid": self.FAR, "name": "Bedroll", "pos": [50.0, 0.0, 0.0]}
            if op == "state.entity" and (a or {}).get("uuid") == self.NPC:
                return {"uuid": self.NPC, "name": "Mattis", "visible": False, "last_pos": [40.0, 0.0, 0.0], "ago_s": 95}
            if op == "nav.route":
                self.route_args.append(dict(a))
                if "target" in a:
                    raise server.GameError("not visible")
                return {"reachable": True, "straight_m": 40.0, "path_m": 40.0, "legs": []}
            return real(op, a, context, timeout)
        self.g.rpc = rpc
        self.h = server.H.get(self.NPC, "Mattis")

    def test_travel_to_an_unseen_character_walks_to_where_they_were_seen(self):
        out = server.travel_to(target=self.h)
        self.assertIn(f"{self.h} is out of sight: going to where you last saw them 95s ago (40,0,0)", out)
        server.wait_for("queue", timeout_s=5)
        self.assertEqual(self.g.ops("act.move_to")[-1][1]["pos"], [40.0, 0.0, 0.0])

    def test_route_to_an_unseen_character_routes_to_the_last_seen_spot(self):
        out = server.route(target=self.h)
        self.assertNotIn("FAILED", out)
        self.assertIn("last saw them 95s ago", out)
        self.assertEqual(self.route_args[-1].get("pos"), [40.0, 0.0, 0.0])

    def test_talk_to_an_unseen_character_walks_there_and_talks_on_arrival(self):
        self.g.refuse_if = lambda op, a: "target is not visible" if op == "act.talk" and len(self.g.ops("act.talk")) == 1 else None
        out = server.talk_to(self.h)
        self.assertIn("is out of sight: going to where you last saw them", out)
        self.assertIn(f"then talk_to {self.h} on arrival", out)
        server.wait_for("queue", timeout_s=5)
        ops = [c[0] for c in self.g.calls if c[0] in ("act.move_to", "act.talk")]
        self.assertEqual(ops[0], "act.talk")
        self.assertEqual(ops[-1], "act.talk")  # after the walk
        self.assertIn("act.move_to", ops[1:-1])

    def test_interact_past_40m_walks_there_then_uses_it(self):
        # S028 15:53: interact e649 (a camp bedroll) "target is too far away (40m from this character, max 40m)"
        far = server.H.get(self.FAR, "Bedroll")
        self.g.refuse_if = lambda op, a: ("target is too far away (50m from this character, max 40m)"
                                          if op == "act.interact" and len(self.g.ops("act.interact")) == 1 else None)
        out = server.interact(far)
        self.assertIn(f"{far} is 50m away: walking there first; then interact {far} on arrival", out)
        server.wait_for("queue", timeout_s=5)
        ops = [c[0] for c in self.g.calls if c[0] in ("act.move_to", "act.interact")]
        self.assertEqual((ops[0], ops[-1]), ("act.interact", "act.interact"))
        self.assertEqual(self.g.ops("act.move_to")[-1][1]["pos"], [50.0, 0.0, 0.0])

    def test_too_far_refusal_with_one_decimal_still_walks(self):
        # Guard.within now says "40.3m ..., max 40m" when the distance rounds to the max
        m = server._TOO_FAR_RX.search("target is too far away (40.3m from this character, max 40m)")
        self.assertEqual(m.group(1), "40.3")

    def test_talk_to_past_40m_walks_there_first(self):
        far = server.H.get(self.FAR, "Bedroll")
        self.g.refuse_if = lambda op, a: ("character is too far away (47m from this character, max 40m)"
                                          if op == "act.talk" and len(self.g.ops("act.talk")) == 1 else None)
        out = server.talk_to(far)
        self.assertIn("is 47m away: walking there first; then talk_to", out)

    def test_inside_do_actions_a_refusal_stays_a_refusal(self):
        self.g.refuse_if = lambda op, a: "target is too far away (50m from this character, max 40m)" if op == "act.interact" else None
        out = server.do_actions(f'[{{"tool": "interact", "target": "{server.H.get(self.FAR, "Bedroll")}"}}]')
        self.assertIn("too far away", out)
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_talk_to_in_combat_stays_a_refusal(self):
        self.g.watch["combat"] = True
        self.g.refuse_if = lambda op, a: "target is not visible" if op == "act.talk" else None
        out = server.talk_to(self.h)
        self.assertIn("not visible", out)
        self.assertEqual(self.g.ops("act.move_to"), [])


class NotesWindow(Base):
    def setUp(self) -> None:
        super().setUp()
        server._trips.clear()
        server._notes_shown.clear()

    def test_long_trip_says_once_it_is_time_for_notes(self):
        self.g.dur["act.move_to"] = 5.0
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}
        self.g.reach = lambda a: {"reachable": True, "pos": [30.0, 0.0, 0.0]}
        (self.tmp / "markers.json").write_text('[{"id": "m1", "label": "Far", "pos": [60, 0, 0], "level": "TUT"}]')
        out = server.travel_to(marker="m1")
        self.assertIn("NOTES WINDOW: ~30s of walking left", out)  # 60 m at TRIP_MPS before the speed is known
        again = server.session_clock()
        self.assertNotIn("NOTES WINDOW", again)  # once per trip
        server.cancel_queue()

    def test_short_job_has_no_hint(self):
        server._async["j9"] = {"ctx": "server", "id": 1, "tool": "move_to", "t0": time.monotonic(), "eta": 3.0}
        self.addCleanup(server._async.pop, "j9", None)
        self.g.jobs[1] = {"status": "running", "end": time.monotonic() + 5, "op": "act.move_to", "args": {}}
        self.assertEqual(server._notes_window(), "")

    def test_long_job_hint_from_its_eta(self):
        server._async["j9"] = {"ctx": "server", "id": 1, "tool": "move_to", "t0": time.monotonic(), "eta": 20.0}
        self.addCleanup(server._async.pop, "j9", None)
        self.g.jobs[1] = {"status": "running", "end": time.monotonic() + 5, "op": "act.move_to", "args": {}}
        self.assertIn("NOTES WINDOW: ~20s", server._notes_window())
        self.assertEqual(server._notes_window(), "")


class Preload(unittest.TestCase):
    def test_tools_loaded_by_toolsearch_in_s002_s007_are_preloaded(self):
        for name in ("screenshot", "click", "click_on", "press_key", "dismiss_popups", "lockpick", "regroup",
                     "get_map", "get_party", "get_actions", "level_up", "short_rest", "long_rest", "camp"):
            self.assertIn(name, server.ALWAYS_LOAD)
        for name in ("ui_dump", "ui_action"):
            self.assertNotIn(name, server.ALWAYS_LOAD)
        # S027-S039: still loaded by ToolSearch 1-4 times each
        for name in ("get_layout", "path_preview", "swap_party", "zoom", "insert_item", "get_explored", "disarm"):
            self.assertIn(name, server.ALWAYS_LOAD)
        # S018-S026: loaded by ToolSearch 6-18 times each
        for name in ("manage_inventory", "trade", "fast_travel", "read_all", "get_journal", "stay", "set_party"):
            self.assertIn(name, server.ALWAYS_LOAD)
        self.assertEqual(server._mark_always_load(), [])


if __name__ == "__main__":
    unittest.main()
