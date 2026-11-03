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

    def test_a_walk_step_its_condition_skips_doesnt_end_the_batch(self):
        out = batch.do_actions(
            json.dumps(
                [
                    {"tool": "move_to", "x": 5, "y": 0, "z": 0, "if": "in_combat"},
                    {"tool": "interact", "target": self.chest},
                    {"tool": "move_to", "x": 6, "y": 0, "z": 0},
                    {"tool": "loot", "take": "all"},
                ]
            )
        )
        self.assertIn("1. move_to 5,0,0: skipped (if in_combat)", out)
        self.assertIn("2. interact", out)
        self.assertIn("3. move_to: done", out)
        self.assertIn(
            "not run: loot (out of combat a batch ends with its first walk step, here step 3 move_to 6,0,0)", out
        )
        self.assertEqual(len(self.g.ops("act.interact")), 1)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to")], [6])

    def test_a_conditional_walk_step_that_runs_ends_the_batch(self):
        out = batch.do_actions(
            json.dumps(
                [
                    {"tool": "move_to", "x": 5, "y": 0, "z": 0, "unless": "in_combat"},
                    {"tool": "interact", "target": self.chest},
                    {"tool": "move_to", "x": 6, "y": 0, "z": 0},
                ]
            )
        )
        self.assertIn("1. move_to: done", out)
        for n, short in ((2, f"interact {self.chest}"), (3, "move_to 6,0,0")):
            self.assertIn(
                f"{n}. {short}: not run (out of combat a batch ends with its first walk step, "
                "here step 1 move_to 5,0,0)",
                out,
            )
        self.assertEqual(self.g.ops("act.interact"), [])
        self.assertEqual(len(self.g.ops("act.move_to")), 1)

    def test_a_batch_that_ends_with_its_walk_carries_the_minimap_block(self):
        # review-v010-final #4: do_actions' walk end had CHANGES only, every other walk end the minimap block too
        out = batch.do_actions(
            json.dumps([{"tool": "interact", "target": self.chest}, {"tool": "move_to", "x": 5, "y": 0, "z": 0}])
        )
        self.assertIn("2. move_to: done", out)
        self.assertIn("\nOBJECTIVES none on the map", out)
        # the walk ran last even with the steps after it listed as not run
        out = batch.do_actions(
            json.dumps(
                [
                    {"tool": "move_to", "x": 5, "y": 0, "z": 0, "unless": "in_combat"},
                    {"tool": "interact", "target": self.chest},
                ]
            )
        )
        self.assertIn(f"2. interact {self.chest}: not run", out)
        self.assertIn("\nOBJECTIVES none on the map", out)
        out = batch.do_actions(json.dumps([{"tool": "interact", "target": self.chest}]))
        self.assertNotIn("OBJECTIVES", out)  # no walk, no block

    def test_a_queues_walk_end_with_not_run_steps_carries_the_minimap_block(self):
        batch.queue_actions(
            json.dumps(
                [
                    {"tool": "move_to", "x": 5, "y": 0, "z": 0, "unless": "in_combat"},
                    {"tool": "interact", "target": self.chest},
                ]
            )
        )
        self.wait_idle()
        out = batch.wait_for("queue")
        self.assertIn(f"2. interact {self.chest}: not run", out)
        self.assertIn("\nOBJECTIVES none on the map", out)

    def test_a_queue_after_a_conditional_walk_keeps_a_later_calls_steps(self):
        batch.queue_actions(
            json.dumps(
                [{"tool": "move_to", "x": 5, "y": 0, "z": 0, "unless": "in_combat"}, {"tool": "wait", "seconds": 0.2}]
            )
        )
        batch.queue_actions(json.dumps([{"tool": "interact", "target": self.chest}]))
        self.wait_idle()
        self.assertEqual(len(self.g.ops("act.move_to")), 1)
        self.assertEqual(len(self.g.ops("act.interact")), 1)

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
