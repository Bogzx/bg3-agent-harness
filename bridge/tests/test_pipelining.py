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
        done = server.wait_for("queue", timeout_s=5)
        self.assertIn("queue e", done)
        self.assertIn("done (1 steps)", done)
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
        self.assertIn("RUNNING queue e3 Claude: travel_to", out)
        self.assertIn("(queue_actions appends after it)", out)
        self.assertIn("NEXT: queue_actions", out)

    def test_a_trip_that_ends_in_the_settle_window_reports_now(self):
        server.SETTLE_S = 3.0
        out = server.travel_to(marker="m1")
        self.assertIn("queue e3 Claude: done (1 steps)", out)
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
        self.assertIn("\n(wait=true ignored outside combat", out.split("RUNNING")[0])  # before the status lines
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
        self.assertIn("RUNNING queue e3 Claude:", out)
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
        for name in ("new_game", "ui_dump", "fast_travel"):  # lockpick, click: preloaded since S002-S007
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

    def test_history_and_live_view_carry_it(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        tools = [{"type": "tool", "tool": "move_to", "t0": 0, "t1": 0.1, "ms": 100, "bg": 1}]
        s = metrics.summarize(tools, [], [])
        metrics.write_summary(s, d / "S1.latency.json", d / "h.jsonl", "S1")
        row = json.loads((d / "h.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["pipelining_pct"], 100.0)
        self.assertEqual(metrics.live_view(tools, [], [], now=1.0)["pipelining_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
