"""Offline tests: out of combat a batch ends with its first walk step (move_to, walk_toward, travel_to, jump_to;
human-gap research §4). do_actions runs the steps up to and with that walk and lists the rest as not run;
queue_actions queues them and lists the rest as not queued. Batches without a walk, or with the walk last, are as
before.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_batch_walk_end.py'
"""

from __future__ import annotations

import json
import time

from test_turn_macros import CHEST, P1, Base, batch, core, queueing  # sets BG3AI_RUNTIME before importing bg3ai


class WalkEndsBatch(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

    def wait_idle(self) -> None:
        end = time.monotonic() + 5
        while queueing._queues().busy(P1) and time.monotonic() < end:
            time.sleep(0.02)

    def test_do_actions_runs_to_the_walk_and_lists_the_rest(self):
        out = batch.do_actions(
            json.dumps(
                [
                    {"tool": "interact", "target": self.chest},
                    {"tool": "move_to", "x": 5, "y": 0, "z": 0},
                    {"tool": "interact", "target": self.chest},
                    {"tool": "loot", "take": "all"},
                ]
            )
        )
        self.assertIn("1. interact", out)
        self.assertIn("2. move_to: done", out)
        self.assertNotIn("3. ", out)
        self.assertIn(
            f"not run: interact {self.chest}, loot (out of combat a batch ends with its first walk step, "
            "here step 2 move_to 5,0,0)",
            out,
        )
        self.assertEqual(len(self.g.ops("act.interact")), 1)
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_a_walk_chain_runs_its_first_walk(self):
        self.g.reach = {"reachable": True, "pos": [0.0, 0.0, 20.0], "straight_m": 20.0, "dir": "N"}
        out = batch.do_actions(
            json.dumps(
                [
                    {"tool": "walk_toward", "direction": "N"},
                    {"tool": "walk_toward", "direction": "W"},
                    {"tool": "jump_to", "x": 1, "y": 0, "z": 1},
                ]
            )
        )
        self.assertIn(
            "not run: walk_toward W, jump_to 1,0,1 (out of combat a batch ends with its first walk step, "
            "here step 1 walk_toward N)",
            out,
        )
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_queue_actions_queues_to_the_walk(self):
        out = batch.queue_actions(
            json.dumps([{"tool": "move_to", "x": 5, "y": 0, "z": 0}, {"tool": "interact", "target": self.chest}])
        )
        self.assertIn(f"queued 1 step(s) for {core.H.get(P1)} Claude", out)
        self.assertIn(
            f"\nnot queued: interact {self.chest} (out of combat a batch ends with its first walk step, "
            "here step 1 move_to 5,0,0)",
            out,
        )
        self.wait_idle()
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertEqual(self.g.ops("act.interact"), [])

    def test_wait_false_is_queue_actions(self):
        out = batch.do_actions(json.dumps([{"tool": "travel_to", "marker": "m1"}, {"tool": "loot"}]), wait=False)
        why = "(out of combat a batch ends with its first walk step, here step 1 travel_to m1)"
        self.assertIn(f"not queued: loot {why}", out)

    def test_no_walk_or_the_walk_last_runs_everything(self):
        out = batch.do_actions(
            json.dumps(
                [
                    {"tool": "interact", "target": self.chest},
                    {"tool": "loot", "take": "all"},
                    {"tool": "move_to", "x": 5, "y": 0, "z": 0},
                ]
            )
        )
        self.assertIn("3. move_to: done", out)
        self.assertNotIn("not run", out)
        out = batch.do_actions(
            json.dumps(
                [{"tool": "manage_inventory", "op": "use", "item": core.H.get(CHEST)}, {"tool": "wait", "seconds": 0.2}]
            )
        )
        self.assertIn("2. wait: waited", out)
        self.assertNotIn("not run", out)
