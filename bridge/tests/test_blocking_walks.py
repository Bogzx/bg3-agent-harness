"""Offline tests: walks block out of combat (human-gap research 2026-11-02, item 4). move_to, walk_toward and
travel_to return at the walk's end with its own result (what wait_for returns, with CHANGES and the minimap block);
wait=false still returns at once; a prompt ends the wait; a walk longer than the budget returns its progress and
the id to keep waiting on, before the MCP clients' 110 s timeout.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_blocking_walks.py'
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    P1,
    Base,
    batch,
    core,
    observe,
    plumbing,
    queueing,
    travel_macro,
    world_tools,
)


class Blocking(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

    def test_move_to_returns_at_the_walks_end(self):
        self.g.dur["act.move_to"] = 0.4
        t0 = time.monotonic()
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)
        self.assertTrue(out.startswith("move_to: done"), out)
        self.assertNotIn("started j", out)
        self.assertIn("\nOBJECTIVES none on the map", out)  # the walk end's minimap block
        self.assertEqual(plumbing._async, {})
        self.assertEqual(self.g.jobs[1]["status"], "done")

    def test_walk_toward_returns_at_the_walks_end(self):
        self.g.reach = {"reachable": True, "pos": [0.0, 0.0, 20.0], "straight_m": 20.0, "dir": "N"}
        self.g.dur["act.move_to"] = 0.3
        out = observe.walk_toward(direction="N")
        self.assertTrue(out.startswith("heading N toward N: 20m\nwalk_toward: done — @(0,0,20)\nOBJECTIVES "), out)
        self.assertEqual(plumbing._async, {})

    def test_the_budget_counts_from_the_tool_call(self):
        # review-v010-human-gap #5: the 90 s budget started in _act_run, after move_to's far-move check and
        # walk_toward's path search (a cold one took seconds, NAV-6); they now count too.
        seen: list[float | None] = []
        real = plumbing._act_run

        def spy(*a, **k):
            seen.append(a[11] if len(a) > 11 else k.get("t0"))
            return real(*a, **k)

        self.g.reach = {"reachable": True, "pos": [0.0, 0.0, 20.0], "straight_m": 20.0, "dir": "N"}
        slow_far = lambda *a: time.sleep(0.3)  # noqa: E731
        with mock.patch.object(plumbing, "_act_run", spy), mock.patch.object(world_tools, "_far_move", slow_far):
            for call in (lambda: world_tools.move_to(x=5.0, y=0.0, z=0.0), lambda: observe.walk_toward(direction="N")):
                start = time.monotonic()
                call()
                t0 = seen[-1]
                self.assertIsNotNone(t0)
                self.assertLess(t0 - start, 0.1)  # the call's start, not after the 0.3 s pre-step

    def test_wait_false_still_returns_at_once(self):
        self.g.dur["act.move_to"] = 1.0
        t0 = time.monotonic()
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0, wait=False)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIn("move_to: started j1", out)

    def test_travel_to_returns_at_the_trips_end(self):
        (self.tmp / "markers.json").write_text(
            json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0], "level": "TUT"}])
        )

        def reach(a):
            here = self.g.ops("act.move_to")
            x = here[-1][1]["pos"][0] if here else 0
            return {"reachable": True, "pos": [min(50.0, x + a["max_m"]), 0.0, 0.0]}

        self.g.reach = reach
        self.g.dur["act.move_to"] = 0.2
        out = travel_macro.travel_to(marker="m1")
        self.assertIn("travel_to marker m1 Old well: started (50m)", out)
        self.assertIn(f"queue {queueing._label(P1)}: done (1 steps)", out)
        self.assertIn("arrived", out)
        self.assertIn("\nOBJECTIVES none on the map", out)
        self.assertNotIn("keeps waiting", out)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [30.0, 50.0])
        self.assertFalse(queueing._queues().busy(P1))

    def test_a_prompt_ends_the_wait_with_the_reason(self):
        # same early stops as wait_for: a dialogue screen opening mid-walk returns, the walk's id to wait on
        self.g.dur["act.move_to"] = 3.0

        def talk():
            time.sleep(0.3)
            self.g.ui = {"screen": "dialogue", "dialogue": {"speaker": "Lae'zel", "text": "Wait.", "options": []}}

        threading.Thread(target=talk).start()
        t0 = time.monotonic()
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertIn("move_to: started j1", out)
        self.assertIn("stopped waiting: dialogue on screen", out)
        self.assertIn("wait_for('j1') keeps waiting for it", out)

    def test_a_long_walk_returns_its_progress_within_the_budget(self):
        self.enterContext(mock.patch.object(plumbing, "WALK_BUDGET_S", 0.6))
        self.g.dur["act.move_to"] = 5.0
        t0 = time.monotonic()
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertIn("move_to: started j1, ETA 1s (4m)", out)
        self.assertIn("still waiting for j1 after", out)
        self.assertIn("RUNNING j1 move_to", out)
        self.assertIn("wait_for('j1') keeps waiting for it", out)
        self.assertNotIn("OBJECTIVES", out)  # not a walk end yet
        self.assertEqual(self.g.jobs[1]["status"], "running")  # the walk goes on

    def test_a_long_trip_returns_its_progress_within_the_budget(self):
        (self.tmp / "markers.json").write_text(
            json.dumps([{"id": "m1", "label": "Old well", "pos": [50, 0, 0], "level": "TUT"}])
        )
        self.g.reach = lambda a: {"reachable": True, "pos": [30.0, 0.0, 0.0]}
        self.enterContext(mock.patch.object(plumbing, "WALK_BUDGET_S", 0.6))
        self.g.dur["act.move_to"] = 5.0
        t0 = time.monotonic()
        out = travel_macro.travel_to(marker="m1")
        self.assertLess(time.monotonic() - t0, 1.5)
        h = core.H.get(P1)
        self.assertIn(f"still waiting for queue:{h} after", out)
        self.assertIn(f"RUNNING queue {queueing._label(P1)}: travel_to", out)
        self.assertIn(f"wait_for('queue:{h}') keeps waiting for it", out)
        self.assertTrue(queueing._queues().busy(P1))
        batch.cancel_queue()

    def test_the_budget_is_under_the_client_timeout(self):
        self.assertLessEqual(plumbing.WALK_BUDGET_S, 90.0)  # clients.py: 110 s per MCP request


class AsyncMetric(unittest.TestCase):
    def test_a_job_that_ended_inside_the_call_is_not_async(self):
        from bg3ai import metrics

        rec = metrics.Recorder(None)
        rec.bg_start("j1", eta_s=3.0, t=100.0)
        rec.bg_end("j1", t=103.0)
        self.assertNotIn("async", rec.tool_done("move_to", 3100, t1=103.1))  # blocked to the walk's end
        rec.bg_start("j2", eta_s=3.0, t=200.0)
        self.assertTrue(rec.tool_done("move_to", 50, t1=200.05).get("async"))  # wait=false: returned at once


if __name__ == "__main__":
    unittest.main()
