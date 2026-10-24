"""S073 22:47-22:58: 3 hand ESCAPEs after interact opened a cabinet/wardrobe. interact closes an empty container's
window itself and says how to close a full one; loot(take="none") closes without taking; loot(take=ids) closes after
the take."""

from __future__ import annotations

import unittest

from test_turn_macros import Base  # noqa: F401 (sets BG3AI_RUNTIME first)

from bg3ai.tools import core, plumbing, world_tools, travel
from bg3ai.game import JobResult

U = "11111111-2222-3333-4444-555555555801"


class G:
    def __init__(self, containers: list):
        self.containers = list(containers)   # what ui.state shows, one per read (the last one repeats)
        self.jobs: list[tuple[str, dict]] = []

    def try_rpc(self, op, args=None, context="server", default=None):
        if op == "ui.state":
            c = self.containers.pop(0) if len(self.containers) > 1 else self.containers[0]
            return {"screen": "container", "container": c} if c is not None else {"screen": "game"}
        return default

    def run_job(self, op, args, context="server", wait_s=25.0, **k):
        self.jobs.append((op, dict(args)))
        if op == "ui.loot" and args.get("close"):
            self.containers = [None]
        return JobResult(status="done", result={"closed": True})


OPENED = "interact: done — used @(1,2,3)\nCONTAINER text=Take All\nSTOPPED: container on screen"


class ContainerClose(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (core.game, plumbing._act, travel._walk_first, plumbing._who, plumbing._interact_uuid,
                      world_tools.CONTAINER_EMPTY_S)
        plumbing._act = lambda tool, op, args, **k: (self.acts.append((op, dict(args))), self.out)[1]
        travel._walk_first = lambda *a, **k: None
        plumbing._who = lambda w: U
        plumbing._interact_uuid = lambda t: t
        world_tools.CONTAINER_EMPTY_S = 0.0
        self.acts: list = []
        self.out = OPENED

    def tearDown(self) -> None:
        (core.game, plumbing._act, travel._walk_first, plumbing._who, plumbing._interact_uuid,
         world_tools.CONTAINER_EMPTY_S) = self.saved

    def interact(self, containers: list) -> str:
        core.game = G(containers)
        fn = world_tools.interact.fn if hasattr(world_tools.interact, "fn") else world_tools.interact
        return fn(target="e722")

    def test_interact_waits_past_a_long_walk_and_a_climb(self):
        # live P4 2026-10-24: a 65.7 m walk round to a ladder's top + the climb ended at 24.5 s of a 25 s wait
        waits: list = []
        plumbing._act = lambda tool, op, args, **k: (waits.append(k.get("wait_s")), "interact: done — used")[1]
        self.interact([None])
        self.assertEqual(waits, [world_tools.INTERACT_WAIT_S])
        self.assertGreaterEqual(world_tools.INTERACT_WAIT_S, 40.0)

    def test_empty_container_is_closed_and_said(self):
        out = self.interact([{"text": "Take All"}])
        self.assertEqual(core.game.jobs, [("ui.loot", {"close": True})])
        self.assertIn("(the container is empty: closed its window)", out)
        self.assertNotIn("STOPPED: container on screen", out)
        self.assertNotIn("CONTAINER ", out)

    def test_late_filling_grid_is_not_closed(self):
        out = self.interact([{"text": "Take All"}, {"text": "Take All", "slots": [{"i": 1, "gold": 65}]}])
        self.assertEqual(core.game.jobs, [])
        self.assertIn('loot(take="none") closes it without taking', out)

    def test_full_container_stays_open_with_the_way_out(self):
        out = self.interact([{"text": "Take All", "slots": [{"i": 1, "gold": 65}]}])
        self.assertEqual(core.game.jobs, [])
        self.assertIn("STOPPED: container on screen", out)
        self.assertIn("the container window stays open", out)

    def test_no_container_leaves_the_result_alone(self):
        self.out = "interact: done — used @(1,2,3)"
        self.assertEqual(self.interact([None]), self.out)

    def test_loot_after_the_empty_close_says_nothing_to_take(self):
        self.interact([{"text": "Take All"}])
        fn = world_tools.loot.fn if hasattr(world_tools.loot, "fn") else world_tools.loot
        self.acts.clear()
        out = fn()
        self.assertIn("nothing to take", out)
        self.assertEqual(self.acts, [])

    def test_loot_none_closes_without_taking(self):
        core.game = G([{"text": "Take All", "slots": [{"i": 1}]}])
        fn = world_tools.loot.fn if hasattr(world_tools.loot, "fn") else world_tools.loot
        fn(take="none")
        self.assertEqual(self.acts, [("ui.loot", {"close": True})])

    def test_loot_some_ids_closes_after(self):
        core.game = G([{"text": "Take All", "slots": [{"i": 1}]}])
        saved = plumbing._uuid
        plumbing._uuid = lambda t: t
        try:
            fn = world_tools.loot.fn if hasattr(world_tools.loot, "fn") else world_tools.loot
            out = fn(take="e5,e6")
        finally:
            plumbing._uuid = saved
        self.assertEqual([op for op, _ in self.acts], ["act.transfer", "act.transfer"])
        self.assertEqual(core.game.jobs, [("ui.loot", {"close": True})])
        self.assertIn("(closed the container window)", out)


if __name__ == "__main__":
    unittest.main()
