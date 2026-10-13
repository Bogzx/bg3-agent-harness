"""Offline tests: pipelining. travel_to returns at once and runs as a queue entry (arrival steps append
after it, cancel_queue stops it), the core play tools carry the alwaysLoad hint (no ToolSearch round
trip before queue_actions/do_actions/wait_for), and the per-session pipelining metric.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest

from test_turn_macros import CHEST, P1, Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import metrics, server  # noqa: E402


def who() -> str:
    """The party leader's label in queue lines ("e4 Claude"): handle numbering depends on setup order."""
    return server._label(P1)


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class AsyncTravel(Base):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0],
                                                             "level": "TUT"}]))
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def reach(a):
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0]}
        self.g.reach = reach

    def test_returns_at_once_and_the_queue_walks_the_legs(self):
        self.g.dur["act.move_to"] = 0.15
        t0 = time.monotonic()
        out = server.travel_to(marker="m1")
        self.assertLess(time.monotonic() - t0, 0.1)  # did not wait for the first 0.15 s leg
        self.assertIn("travel_to marker m1 Old well: started (50m)", out)
        self.assertIn("queued 1 step(s)", out)
        real = self.g.try_rpc
        self.g.try_rpc = lambda op, args=None, context="server", default=None: (
            {"leads": [{"uuid": CHEST, "name": "Well", "kind": "usable", "dist": 3.0, "dir": "N", "unvisited": True}]}
            if op == "nav.frontier" else real(op, args, context, default))
        done = server.wait_for("queue", timeout_s=5)
        self.assertIn("queue e", done)
        self.assertIn("done (1 steps)", done)
        self.assertIn("LEADS (unvisited; get_frontier lists all): " + server.H.get(CHEST) + " Well <usable> 3m N", done)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [30.0, 50.0])
        self.assertTrue(any(e["kind"] == "queue_done" for e in self.g.events))

    def test_arrival_steps_append_after_the_trip(self):
        self.g.dur["act.move_to"] = 0.1
        server.travel_to(marker="m1")
        out = server.queue_actions(json.dumps([{"tool": "interact", "target": server.H.get(CHEST)}]))
        self.assertIn("queued 1 step(s)", out)
        server.wait_for("queue", timeout_s=5)
        ops = [op for op, _ in self.g.calls if op in ("act.move_to", "act.interact")]
        self.assertEqual(ops, ["act.move_to", "act.move_to", "act.interact"])

    def test_cancel_queue_stops_the_trip(self):
        self.g.dur["act.move_to"] = 0.5
        server.travel_to(marker="m1")
        self.assertTrue(wait_until(lambda: self.g.ops("act.move_to")))
        out = server.cancel_queue()
        self.assertIn("cancelled", out)
        server.wait_for("queue", timeout_s=5)
        time.sleep(0.1)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)  # the first leg was cancelled, no second leg
        jid = max(self.g.jobs)
        self.assertEqual(self.g.jobs[jid]["status"], "interrupted")

    def test_queue_after_an_async_walk_waits_for_it(self):
        # move_to returns at once; queue_actions right after must not supersede the walk.
        self.g.dur["act.move_to"] = 0.3
        out = server.move_to(x=5.0, y=0.0, z=0.0)
        self.assertIn("started j", out)
        h = out.split("started ", 1)[1].split(",")[0].split()[0]
        q = server.queue_actions(json.dumps([{"tool": "interact", "target": server.H.get(CHEST)}]))
        self.assertIn(f"after {h} arrives", q)
        server.wait_for("queue", timeout_s=5)
        walk = self.g.jobs[int(h[1:])]
        self.assertEqual(walk["status"], "done")  # not superseded
        self.assertEqual([op for op, _ in self.g.calls if op in ("act.move_to", "act.interact")],
                         ["act.move_to", "act.interact"])

    def test_cancel_queue_also_stops_the_walk_it_waits_for(self):
        self.g.dur["act.move_to"] = 1.0
        h = server.move_to(x=5.0, y=0.0, z=0.0).split("started ", 1)[1].split(",")[0].split()[0]
        server.queue_actions(json.dumps([{"tool": "interact", "target": server.H.get(CHEST)}]))
        time.sleep(0.1)
        server.cancel_queue()
        server.wait_for("queue", timeout_s=5)
        self.assertEqual(self.g.jobs[int(h[1:])]["status"], "interrupted")
        self.assertEqual(self.g.ops("act.interact"), [])

    def test_bad_destination_fails_at_once(self):
        out = server.travel_to(marker="nowhere")
        self.assertIn("FAILED: no marker 'nowhere'", out)
        self.assertFalse(server._queues().busy())
        self.assertEqual(self.g.ops("act.move_to"), [])

    def test_wait_true_does_not_block_and_shows_progress(self):
        server.SETTLE_S = 0.2
        self.g.dur["act.move_to"] = 1.0
        self.g.watch["party"][0]["pos"] = [0.0, 0.0, 0.0]
        t0 = time.monotonic()
        out = server.travel_to(marker="m1", wait=True)
        self.assertLess(time.monotonic() - t0, 0.6)
        self.assertIn("travel_to marker m1 Old well: started (50m); 50m left", out)
        self.assertIn("wait=true ignored", out)
        self.assertIn(f"RUNNING queue {who()}: travel_to", out)
        self.assertIn("(queue_actions appends after it)", out)
        self.assertIn("NEXT: queue_actions", out)

    def test_a_trip_that_ends_in_the_settle_window_reports_now(self):
        server.SETTLE_S = 3.0
        out = server.travel_to(marker="m1")
        self.assertIn(f"queue {who()}: done (1 steps)", out)
        self.assertIn("arrived", out)
        self.assertNotIn("RUNNING", out)
        self.assertNotIn("NEXT:", out)

    def test_trip_progress_has_an_eta(self):
        server._trips[P1] = {"dest": [50.0, 0.0, 0.0], "d0": 50.0, "t0": time.monotonic() - 4.0}
        self.g.watch["party"][0]["pos"] = [20.0, 0.0, 0.0]  # 20 m in 4 s: 30 m left at 5 m/s
        self.assertEqual(server._trip_progress(P1), "30m left, ETA ~6s")

    def test_do_actions_can_travel(self):
        out = server.do_actions(json.dumps([{"tool": "travel_to", "marker": "m1"},
                                            {"tool": "interact", "target": server.H.get(CHEST)}]))
        self.assertIn("1. travel_to marker m1 Old well: 50m", out)
        self.assertIn("arrived", out)
        self.assertIn("2. interact", out)
        self.assertEqual(len(self.g.ops("act.move_to")), 2)


class EarlyWake(Base):
    """wait_for on a walk returns when ~lead_s of it is left; what is issued then runs after the arrival."""

    def setUp(self) -> None:
        super().setUp()
        self.started: dict[str, float] = {}
        self.g.on_start = lambda op, a: self.started.setdefault(op, time.monotonic())

    def walk(self, dur: float) -> str:
        self.g.dur["act.move_to"] = dur  # the fake's ETA is 1.0 s
        out = server.move_to(x=5.0, y=0.0, z=0.0)
        return out.split("started ", 1)[1].split(",")[0].split()[0]

    def test_wakes_early_and_the_next_action_waits_for_the_walk(self):
        h = self.walk(1.2)
        t0 = time.monotonic()
        out = server.wait_for(h, timeout_s=5)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIn(f"{h} move_to: arriving, ETA ~", out)
        self.assertIn("runs after the walk arrives", out)
        walk = self.g.jobs[int(h[1:])]
        self.assertEqual(walk["status"], "running")
        res = server.interact(target=server.H.get(CHEST))
        self.assertIn("waited", res)
        self.assertEqual(walk["status"], "done")  # not cancelled
        self.assertGreaterEqual(self.started["act.interact"], walk["end"])

    def test_once_per_walk_then_blocks_to_the_end(self):
        h = self.walk(0.8)
        self.assertIn("arriving", server.wait_for(h, timeout_s=5))
        out = server.wait_for(h, timeout_s=5)
        self.assertIn("move_to: done", out)
        self.assertNotIn("arriving", out)

    def test_lead_zero_waits_for_the_end(self):
        h = self.walk(0.5)
        out = server.wait_for(h, timeout_s=5, lead_s=0)
        self.assertIn("move_to: done", out)

    def test_far_from_arrival_blocks(self):
        h = self.walk(1.5)
        t0 = time.monotonic()
        out = server.wait_for(h, timeout_s=5, lead_s=0.2)  # ETA 1.0 s: more than 0.2 s left at first
        self.assertIn("arriving", out)  # wakes once the ETA drops under 0.2 s
        self.assertGreater(time.monotonic() - t0, 0.6)
        self.assertEqual(self.g.jobs[int(h[1:])]["status"], "running")

    def test_arrival_carries_unvisited_leads_and_ways(self):
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.frontier":
                return {"leads": [{"uuid": CHEST, "name": "Door", "kind": "door", "dist": 15.3, "dir": "NW",
                                   "dy": -2.4, "unvisited": True},
                                  {"uuid": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Crate",
                                   "kind": "container", "dist": 2.0, "dir": "E"}],
                        "ways": [{"dir": "N", "open_m": 20}, {"dir": "S", "open_m": 0}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake
        h = self.walk(0.3)
        out = server.wait_for(h, timeout_s=5, lead_s=0)
        self.assertIn("move_to: done", out)
        self.assertIn(f"LEADS (unvisited; get_frontier lists all): {server.H.get(CHEST)} Door <door> 15.3m NW "
                      "(2.4m down)", out)
        self.assertNotIn("Crate", out)  # visited
        self.assertIn("WAYS walkable: N 20m+ | blocked: S", out)
        h = self.walk(0.8)
        self.assertNotIn("LEADS", server.wait_for(h, timeout_s=5))  # early wake: still walking

    def test_not_in_combat(self):
        h = self.walk(0.5)
        self.g.combat = {"active": True, "current": server.H.get(P1)}
        out = server.wait_for(h, timeout_s=5)
        self.assertNotIn("arriving", out)
        self.assertIn("move_to: done", out)
        self.assertEqual(server._early, {})

    def test_not_for_non_walks(self):
        self.g.dur["act.interact"] = 0.5
        out = server.interact(target=server.H.get(CHEST), wait=False)
        h = out.split("started ", 1)[1].split(",")[0].split()[0]
        self.assertNotIn("arriving", server.wait_for(h, timeout_s=5))

    def test_trip_wakes_queue_wait_and_a_direct_action_follows_it(self):
        (self.tmp / "markers.json").write_text(json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0],
                                                             "level": "TUT"}]))
        self.g.reach = lambda a: {"reachable": True, "pos": [50.0, 0.0, 0.0]}
        self.g.dur["act.move_to"] = 1.0
        server.travel_to(marker="m1")
        self.assertTrue(wait_until(lambda: self.g.ops("act.move_to")))
        server._trips[P1] = {"dest": [50.0, 0.0, 0.0], "d0": 50.0, "t0": time.monotonic() - 4.0}
        self.g.watch["party"][0]["pos"] = [45.0, 0.0, 0.0]  # 45 m in 4 s: ~0.4 s left
        out = server.wait_for("queue", timeout_s=5)
        self.assertIn(f"queue {who()} travel_to: arriving, ETA ~", out)
        self.assertTrue(server._queues().busy(P1))
        res = server.interact(target=server.H.get(CHEST))
        self.assertIn("waited", res)
        legs = [j for j in self.g.jobs.values() if j["op"] == "act.move_to"]
        self.assertEqual({j["status"] for j in legs}, {"done"})  # the trip's leg was not cancelled

    def test_queue_with_arrival_steps_waits_to_the_end(self):
        self.g.dur["act.move_to"] = 0.3
        self.g.dur["act.interact"] = 0.3
        server.queue_actions(json.dumps([{"tool": "move_to", "x": 5, "y": 0, "z": 0},
                                         {"tool": "interact", "target": server.H.get(CHEST)}]))
        out = server.wait_for("queue", timeout_s=5)
        self.assertNotIn("arriving", out)
        self.assertIn("done (2 steps)", out)


class ClimbHint(Base):
    def test_asks_leads_only_and_names_exits(self):
        seen = []

        def frontier(op, args=None, context="server", default=None):
            seen.append((op, args))
            return {"leads": [{"uuid": CHEST, "name": "Trapdoor", "kind": "exit", "dist": 4.0},
                              {"uuid": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Crate", "kind": "container",
                               "dist": 1.0}]}
        self.g.try_rpc = frontier
        out = server._climb_hint([0.0, 0.0, 0.0], [0.0, 5.0, 0.0])
        self.assertEqual(seen, [("nav.frontier", {"ways": False})])
        self.assertIn("5m above you", out)
        self.assertIn("Trapdoor <exit> 4m", out)
        self.assertNotIn("Crate", out)
        self.assertEqual(server._climb_hint([0.0, 0.0, 0.0], [3.0, 1.0, 0.0]), "")  # same deck


class OtherFloor(Base):
    """Live 10:45: travel_to toward the Mausoleum puzzle room (62 m below, an interior) walked 120 m the wrong way
    with the Mausoleum Entrance 17 m away: no route + a way in in view = a fast refusal naming it."""
    def setUp(self) -> None:
        super().setUp()
        self.route: dict = {"reachable": False}
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.route":
                self.g.calls.append((op, args))
                return dict(self.route)
            if op == "nav.frontier":
                return {"leads": [{"uuid": CHEST, "name": "Mausoleum Entrance", "kind": "exit", "dist": 17.1,
                                   "dir": "NW"},
                                  {"uuid": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Far Ladder",
                                   "kind": "ladder", "dist": 55.0}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake

    def test_refuses_fast_and_names_the_entrance(self):
        out = server.travel_to(x=20.0, y=-62.0, z=0.0)
        self.assertIn("REFUSED: the goal is 62m below you", out)
        self.assertIn(f"{server.H.get(CHEST)} Mausoleum Entrance <exit> 17.1m NW", out)
        self.assertNotIn("Far Ladder", out)  # past FLOOR_WAY_M
        self.assertEqual(self.g.ops("act.move_to"), [])
        self.assertFalse(server._queues().busy())

    def test_walks_when_the_route_exists_or_the_goal_is_level(self):
        self.g.reach = lambda a: {"reachable": True, "pos": [20.0, -62.0, 0.0]}
        self.route = {"reachable": True}
        self.assertIsNone(server._other_floor(P1, [0.0, 0.0, 0.0], [20.0, -62.0, 0.0]))
        self.route = {"reachable": False}
        self.assertIsNone(server._other_floor(P1, [0.0, 0.0, 0.0], [20.0, -3.0, 0.0]))  # same floor: no probe
        self.assertEqual(len(self.g.ops("nav.route")), 1)
        self.route = {"too_far": True}
        self.assertIsNone(server._other_floor(P1, [0.0, 0.0, 0.0], [500.0, -12.0, 0.0]))  # not far below: walk

    def test_far_interior_goal_names_the_entrance(self):
        # Live 11:2x: the Mausoleum room is ~950 m off and 69 m below the gate in level coordinates (too far to
        # preview); travel_to walked 115 m south to the House of Healing. An entrance 17 m away = the way in.
        self.route = {"too_far": True, "straight_m": 954.6}
        out = server._other_floor(P1, [0.0, 38.0, 0.0], [-100.0, -31.0, -950.0])
        self.assertIn("REFUSED: the goal is 69m below you", out)
        self.assertIn("likely inside an interior", out)
        self.assertIn("Mausoleum Entrance <exit> 17.1m NW", out)

    def test_entrance_past_the_frontier_default_radius_is_asked_for(self):
        """acceptance 01:07: from the ladder top the Mausoleum Entrance was 34 m off, past nav.frontier's 30 m
        default, and travel_to started a 934 m walk. The frontier is asked for FLOOR_WAY_M."""
        self.route = {"too_far": True, "straight_m": 933.7}
        asked = []
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.frontier":
                asked.append(args)
            return real(op, args, context, default)
        self.g.try_rpc = fake
        server._other_floor(P1, [0.0, 35.6, 0.0], [-100.0, -31.0, -950.0])
        self.assertEqual(asked, [{"ways": False, "radius": server.FLOOR_WAY_M, "known": True}])

    def test_far_goal_with_only_a_ladder_walks(self):
        self.route = {"too_far": True}
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.frontier":
                return {"leads": [{"uuid": CHEST, "name": "Ladder", "kind": "ladder", "dist": 10.0}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake
        self.assertIsNone(server._other_floor(P1, [0.0, 38.0, 0.0], [-100.0, -31.0, -950.0]))


class FloorDetour(Base):
    """se 12:3x, Roadside Cliffs: locks 9-16 m below a ledge, the ladder 13 m N named by some travel_to stops and not
    by others. A walkable route that winds far around names the ladder at the start (the walk still runs)."""
    LADDER = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"

    def setUp(self) -> None:
        super().setUp()
        self.route: dict = {"reachable": True, "length_m": 140.0}
        real = self.g.try_rpc

        def fake(op, args=None, context="server", default=None):
            if op == "nav.route":
                return dict(self.route)
            if op == "nav.frontier":
                return {"leads": [{"uuid": self.LADDER, "name": "Ladder", "kind": "ladder", "dist": 13.0, "dir": "N"},
                                  {"uuid": CHEST, "name": "Oak Door", "kind": "door", "dist": 5.0}]}
            return real(op, args, context, default)
        self.g.try_rpc = fake

    def test_long_detour_names_the_ladder(self):
        out = server._other_floor(P1, [0.0, 0.0, 0.0], [15.0, -12.0, 0.0])
        self.assertFalse(out.startswith("REFUSED"))
        self.assertIn("12m below you", out)
        self.assertIn("winds 140m for 19m straight", out)
        self.assertIn("Ladder <ladder> 13m N", out)
        self.assertNotIn("Oak Door", out)

    def test_short_route_or_no_ladder_says_nothing(self):
        self.route = {"reachable": True, "length_m": 40.0}
        self.assertIsNone(server._other_floor(P1, [0.0, 0.0, 0.0], [15.0, -12.0, 0.0]))
        self.route = {"reachable": True}
        self.assertIsNone(server._other_floor(P1, [0.0, 0.0, 0.0], [15.0, -12.0, 0.0]))
        # live 13:08: the long route climbs the ladder itself (a climb/drop link): no note
        self.route = {"reachable": True, "length_m": 140.0, "links": [{"kind": "drop", "dy": -11.0}]}
        self.assertIsNone(server._other_floor(P1, [0.0, 0.0, 0.0], [15.0, -12.0, 0.0]))

    def test_travel_to_walks_with_the_note_first(self):
        self.g.reach = lambda a: {"reachable": True, "pos": [15.0, -12.0, 0.0]}
        out = server.travel_to(x=15.0, y=-12.0, z=0.0)
        self.assertIn("note: the goal is 12m below you", out)
        self.assertNotIn("REFUSED", out)
        self.assertTrue(wait_until(lambda: self.g.ops("act.move_to")))


class SettleAndPace(Base):
    def setUp(self) -> None:
        super().setUp()
        server.SETTLE_S = 0.3

    def test_quick_walk_reports_its_result(self):
        self.g.dur["act.move_to"] = 0.05
        t0 = time.monotonic()
        out = server.move_to(x=5.0, y=0.0, z=0.0)
        self.assertLess(time.monotonic() - t0, 0.25)  # returned when the job ended, not after the window
        self.assertTrue(out.startswith("move_to: done"), out)
        self.assertEqual(server._async, {})

    def test_failure_in_the_window_comes_back_now(self):
        self.g.refuse_if = lambda op, a: "no walkable path there" if op == "act.move_to" else None
        self.assertIn("FAILED — no walkable path there", server.move_to(x=5.0, y=0.0, z=0.0))

    def test_long_walk_returns_after_the_window(self):
        self.g.dur["act.move_to"] = 3.0
        t0 = time.monotonic()
        out = server.move_to(x=5.0, y=0.0, z=0.0, wait=True)
        self.assertLess(time.monotonic() - t0, 0.8)
        self.assertTrue(out.startswith("move_to: started j1, ETA"), out)
        self.assertIn("\n(pace guard: wait=true ignored outside combat", out.split("RUNNING")[0])  # before the status lines
        self.assertIn("RUNNING j1 move_to", out)

    def test_wait_true_blocks_in_combat(self):
        self.g.watch["combat"] = True
        self.g.dur["act.move_to"] = 0.5
        out = server.move_to(x=5.0, y=0.0, z=0.0, wait=True)
        self.assertTrue(out.startswith("move_to: done"), out)
        self.assertNotIn("ignored", out)

    def test_pace_hint_when_waiting_right_after_issuing(self):
        self.g.dur["act.move_to"] = 0.6
        server.move_to(x=5.0, y=0.0, z=0.0)
        self.assertIn("PACE:", server.wait_for("j1", timeout_s=2))
        server.move_to(x=6.0, y=0.0, z=0.0)
        server.get_events()  # independent work in between: no nudge
        self.assertNotIn("PACE:", server.wait_for("j2", timeout_s=2))

    def test_next_hint_three_times_then_after_a_nudge(self):
        self.g.dur["act.move_to"] = 5.0
        seen = []
        for i in range(5):
            seen.append("NEXT:" in server.move_to(x=float(i), y=0.0, z=0.0))
            server.get_events()
        self.assertEqual(seen, [True, True, True, False, False])
        server.move_to(x=9.0, y=0.0, z=0.0)
        server.wait_for("idle", timeout_s=0.1)  # right after issuing: nudged
        self.assertIn("NEXT:", server.move_to(x=10.0, y=0.0, z=0.0))

    def test_queue_result_shows_what_runs(self):
        self.g.dur["act.interact"] = 2.0
        out = server.queue_actions(json.dumps([{"tool": "interact", "target": server.H.get(CHEST)},
                                               {"tool": "interact", "target": server.H.get(CHEST)}]))
        self.assertIn(f"RUNNING queue {who()}:", out)
        self.assertIn("(queue_actions appends after it)", out)

    def test_wait_returns_on_the_first_event(self):
        def later():
            time.sleep(0.3)
            self.g.push("combat_started", {})
        threading.Thread(target=later).start()
        t0 = time.monotonic()
        out = server.wait(5)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertIn("combat_started after", out)


class AlwaysLoad(unittest.TestCase):
    def test_core_tools_are_marked_and_the_rest_deferred(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        self.assertEqual(server._mark_always_load(), [])  # every listed name exists
        for name in ("queue_actions", "do_actions", "wait_for", "look", "travel_to", "combat_turn"):
            self.assertEqual((tools[name].meta or {}).get("anthropic/alwaysLoad"), True, name)
        for name in ("wiki_page", "wiki_search", "load_game", "list_saves"):
            self.assertEqual((tools[name].meta or {}).get("anthropic/alwaysLoad"), True, name)
        for name in ("new_game", "ui_dump"):  # lockpick, click: preloaded since S002-S007; fast_travel since S026
            self.assertFalse((tools[name].meta or {}).get("anthropic/alwaysLoad"), name)

    def test_movement_defaults_to_async(self):
        import inspect
        for fn in (server.move_to, server.walk_toward, server.travel_to):
            self.assertIs(inspect.signature(fn).parameters["wait"].default, False, fn.__name__)


class PipeliningMetric(unittest.TestCase):
    def test_recorder_marks_calls_issued_while_something_runs(self):
        rec = metrics.Recorder(None)
        t = 1000.0
        rec.bg_start("j1", eta_s=4.0, t=t - 0.02)              # move_to starts the walk...
        a = rec.tool_done("move_to", 50, t1=t)                 # ...and returns: async, nothing ran before
        self.assertEqual((a["bg"], a.get("async")), (0, True))
        b = rec.tool_done("look", 30, t1=t + 1)
        rec.bg_start("queue:p1", t=t + 1.99)
        c = rec.tool_done("queue_actions", 20, t1=t + 2)       # issued during the walk
        self.assertEqual((b["bg"], c["bg"], c.get("async")), (1, 1, True))
        rec.bg_end("j1", t=t + 3)
        d = rec.tool_done("interact", 100, t1=t + 4)           # the queue still runs
        rec.bg_end("queue:p1", t=t + 5)
        e = rec.tool_done("interact", 100, t1=t + 9)           # j1's ETA passed and the queue ended
        self.assertEqual((d["bg"], e["bg"]), (1, 0))
        self.assertNotIn("bg", rec.tool_done("move_to", 10, nested=True, t1=t + 10))

    def test_async_job_without_end_expires_after_its_eta(self):
        rec = metrics.Recorder(None)
        rec.bg_start("j1", eta_s=2.0, t=0.0)
        self.assertEqual(rec.tool_done("interact", 10, t1=3.0)["bg"], 1)   # within ETA + grace
        self.assertEqual(rec.tool_done("interact", 10, t1=4.0)["bg"], 0)

    def test_summary_from_bridge_records(self):
        tools = [
            {"type": "tool", "tool": "travel_to", "t0": 0, "t1": 0.1, "ms": 100, "bg": 0, "async": True},
            {"type": "tool", "tool": "move_to", "t0": 0.2, "t1": 3, "ms": 2800, "nested": True},
            {"type": "tool", "tool": "look", "t0": 0.5, "t1": 0.6, "ms": 100, "bg": 1},
            {"type": "tool", "tool": "queue_actions", "t0": 1, "t1": 1.05, "ms": 50, "bg": 1, "async": True},
            {"type": "tool", "tool": "do_actions", "t0": 5, "t1": 8, "ms": 3000, "bg": 0},
        ]
        s = metrics.summarize(tools, [], [])
        p = s["pipelining"]
        self.assertEqual((p["issued"], p["pipelined"], p["async"], p["queue_calls"], p["batch_calls"]),
                         (3, 1, 2, 1, 2))
        self.assertEqual(s["pipelining_pct"], 33.3)
        self.assertEqual(p["source"], "bridge")

    def test_summary_falls_back_to_samples(self):
        # Old records (no bg field): S001-style blocking calls are not pipelined, a call made while a
        # busy sample shows the walk still going is.
        tools = [
            {"type": "tool", "tool": "travel_to", "t0": 0, "t1": 10, "ms": 10000},
            {"type": "tool", "tool": "move_to", "t0": 12, "t1": 12.1, "ms": 100},
            {"type": "tool", "tool": "interact", "t0": 14, "t1": 15, "ms": 1000},
        ]
        samples = [{"type": "sample", "t": 9.5, "moving": True, "job": 1},
                   {"type": "sample", "t": 13.5, "moving": True, "job": 1}]
        p = metrics.summarize(tools, samples, [])["pipelining"]
        self.assertEqual((p["issued"], p["pipelined"], p["source"]), (3, 1, "samples"))

    def test_dialogue_answers_left_out_and_enemy_turn_plans_count(self):
        # S015: 33 of 101 issued calls answered dialogue (nothing can run there) and 13 combat turns were
        # sent while enemies acted (they wait for the turn): the strict bg-only metric said 2%.
        tools = [
            {"type": "tool", "tool": "choose_dialogue", "t0": 1, "t1": 1.5, "ms": 500, "bg": 0},
            {"type": "tool", "tool": "interact", "t0": 3, "t1": 4, "ms": 1000, "bg": 0},     # in dialogue
            {"type": "tool", "tool": "combat_turn", "t0": 10, "t1": 20, "ms": 10000, "bg": 0},  # enemy turn
            {"type": "tool", "tool": "combat_turn", "t0": 30, "t1": 35, "ms": 5000, "bg": 0},  # own turn
            {"type": "tool", "tool": "move_to", "t0": 40, "t1": 41, "ms": 1000, "bg": 0, "async": True},
            {"type": "tool", "tool": "interact", "t0": 42, "t1": 43, "ms": 1000, "bg": 1},
        ]
        samples = [{"type": "sample", "t": 2.5, "dlg": True},
                   {"type": "sample", "t": 9.5, "combat": True, "pturn": False},
                   {"type": "sample", "t": 29.5, "combat": True, "pturn": True},
                   {"type": "sample", "t": 39.5}]
        p = metrics.summarize(tools, samples, [])["pipelining"]
        self.assertEqual((p["issued"], p["dialogue"], p["pipelined"], p["enemy_turn"]), (6, 2, 2, 1))
        self.assertEqual((p["pct"], p["strict_pct"]), (50.0, 16.7))

    def test_think_overlap(self):
        # Out of combat/dialogue, samples while the agent works (between calls, or in a read) and how
        # many had the party busy; time blocked in wait_for or a blocking action doesn't count.
        tools = [
            {"type": "tool", "tool": "move_to", "t0": 0, "t1": 1, "ms": 1000, "bg": 0, "async": True},
            {"type": "tool", "tool": "look", "t0": 2, "t1": 2.2, "ms": 200, "bg": 1},
            {"type": "tool", "tool": "wait_for", "t0": 3, "t1": 6, "ms": 3000, "bg": 1},
            {"type": "tool", "tool": "interact", "t0": 9, "t1": 10, "ms": 1000, "bg": 0},
        ]
        samples = [{"type": "sample", "t": 1.5, "moving": True},
                   {"type": "sample", "t": 2.1, "moving": True},          # in a read: counts
                   {"type": "sample", "t": 4, "moving": True},            # in wait_for: not
                   {"type": "sample", "t": 7},
                   {"type": "sample", "t": 8, "combat": True},            # combat: not
                   {"type": "sample", "t": 8.5, "dlg": True}]             # dialogue: not
        s = metrics.summarize(tools, samples, [])
        self.assertEqual(s["think_overlap"], {"samples": 3, "busy": 2, "pct": 66.7})
        self.assertEqual(s["think_overlap_pct"], 66.7)

    def test_history_and_live_view_carry_it(self):
        import shutil
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        tools = [{"type": "tool", "tool": "move_to", "t0": 0, "t1": 0.1, "ms": 100, "bg": 1}]
        s = metrics.summarize(tools, [], [])
        metrics.write_summary(s, d / "S1.latency.json", d / "h.jsonl", "S1")
        row = json.loads((d / "h.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["pipelining_pct"], 100.0)
        self.assertEqual(metrics.live_view(tools, [], [], now=1.0)["pipelining_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
