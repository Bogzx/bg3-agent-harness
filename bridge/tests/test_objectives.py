"""Offline tests: objective markers in get_status/get_map and the walk_toward tool (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-"))  # tool logs go here, not runtime/

from bg3ai import render, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402

PARTY = "11111111-2222-3333-4444-555555555555"
DOOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

MARKERS = [
    {"label": "Escape the ship", "quest": "Escape the Nautiloid", "objective": "Find a way out",
     "kind": "QuestMarker", "pos": [-16.0, 0.0, 0.0], "dist": 16.0, "dir": "W", "here": True},
    {"label": "Find the helm", "quest": "Escape the Nautiloid", "pos": [0.0, 2.0, 40.0], "dist": 40.2,
     "dir": "N", "here": True},
    {"label": "Rescue Shadowheart", "quest": "Rescue the Trapped Woman"},
]


class FakeGame:
    def __init__(self, markers: list[dict] | None = None, reach: dict | None = None):
        self.markers = MARKERS if markers is None else markers
        self.reach = reach if reach is not None else {"reachable": True, "pos": [-14.0, 0.0, 0.0], "path_m": 14.5,
                                                      "straight_m": 14.0, "dir": "W", "samples": 7}
        self.calls: list[tuple[str, Any]] = []
        self.jobs: list[tuple[str, dict]] = []

    def rpc(self, op: str, args: dict | None = None, context: str = "server") -> Any:
        self.calls.append((op, args))
        if op == "state.status":
            return {"region": "Nautiloid", "active": PARTY,
                    "party": [{"uuid": PARTY, "name": "Claude", "hp": 12, "max_hp": 12}]}
        if op == "state.map":
            return {"region": "Nautiloid"}
        if op == "nav.markers":
            return {"markers": self.markers, "ms": 1}
        if op == "nav.reach_toward":
            return self.reach
        raise AssertionError(f"unexpected op {op}")

    def try_rpc(self, op: str, args: dict | None = None, context: str = "server") -> Any:
        if op == "ui.state":
            return None
        if op == "nav.set_subregion":
            return {}
        return self.rpc(op, args, context)

    def run_job(self, op: str, args: dict, context: str = "server", wait_s: float = 25.0, **kw: Any) -> JobResult:
        self.jobs.append((op, args))
        return JobResult(status="done", result={"arrived": True})

    def drain_events(self, ctx: str) -> list:
        return []


class RenderObjectives(unittest.TestCase):
    def test_lines(self):
        lines = render.render_objectives(MARKERS)
        self.assertEqual(lines[0], "OBJECTIVE o1 Escape the ship 16m W [Escape the Nautiloid]")
        self.assertEqual(lines[1], "OBJECTIVE o2 Find the helm 40.2m N [Escape the Nautiloid]")
        self.assertEqual(lines[2], "OBJECTIVE o3 Rescue Shadowheart (another area) [Rescue the Trapped Woman]")
        self.assertEqual(len(lines), 3)

    def test_here_only_and_limit(self):
        lines = render.render_objectives(MARKERS, here_only=True, limit=1)
        self.assertEqual(lines, ["OBJECTIVE o1 Escape the ship 16m W [Escape the Nautiloid]",
                                 "(+2 more objectives; get_map lists all)"])

    def test_quest_equal_to_label_not_repeated(self):
        lines = render.render_objectives([{"label": "Q", "quest": "Q", "here": True, "dist": 3, "dir": "E"}])
        self.assertEqual(lines, ["OBJECTIVE o1 Q 3m E"])

    def test_empty_and_garbage(self):
        self.assertEqual(render.render_objectives(None), [])
        self.assertEqual(render.render_objectives([]), [])
        self.assertEqual(render.render_objectives([{"objective": "x"}]), ["OBJECTIVE o1 x (another area)"])

    def test_find(self):
        self.assertIs(render.find_objective(MARKERS, "o2"), MARKERS[1])
        self.assertIs(render.find_objective(MARKERS, "2"), MARKERS[1])
        self.assertIs(render.find_objective(MARKERS, "shadowheart"), MARKERS[2])
        with self.assertRaises(ValueError):
            render.find_objective(MARKERS, "nautiloid")  # ambiguous
        with self.assertRaises(ValueError):
            render.find_objective(MARKERS, "o9")
        with self.assertRaises(ValueError):
            render.find_objective([], "o1")


class ServerTools(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGame()
        self._orig = server.game
        server.game = self.fake
        server._objectives["last"] = []
        server.DELTA.reset()  # each test starts with no previous look (full views)
        self.addCleanup(setattr, server, "game", self._orig)

    def test_get_status_shows_nearby_objectives(self):
        out = server.get_status()
        self.assertIn("OBJECTIVE o1 Escape the ship 16m W", out)
        self.assertIn("OBJECTIVE o2 Find the helm 40.2m N", out)
        self.assertNotIn("Shadowheart", out)
        self.assertIn("(+1 more objective; get_map lists all)", out)
        self.assertNotIn(PARTY, out)

    def test_get_map_lists_all(self):
        out = server.get_map()
        self.assertIn("OBJECTIVES", out)
        self.assertIn("OBJECTIVE o3 Rescue Shadowheart (another area)", out)

    def test_walk_toward_direction(self):
        out = server.walk_toward(direction="w", wait=True)
        reach = [a for op, a in self.fake.calls if op == "nav.reach_toward"][0]
        self.assertEqual(reach["dir"], "W")
        self.assertEqual(reach["char"], PARTY)
        self.assertEqual(self.fake.jobs, [("act.move_to", {"char": PARTY, "pos": [-14.0, 0.0, 0.0], "run": True})])
        self.assertTrue(out.startswith("heading W toward W: 14m (path 14.5m)"), out)
        self.assertIn("walk_toward: done", out)

    def test_walk_toward_objective_by_shown_id(self):
        server.get_status()  # shows o1/o2
        # The party moved: the server now returns the helm first. o1 must still mean "Escape the ship".
        self.fake.markers = [MARKERS[1], MARKERS[0], MARKERS[2]]
        server.walk_toward(objective="o1", wait=True)
        reach = [a for op, a in self.fake.calls if op == "nav.reach_toward"][-1]
        self.assertEqual(reach["pos"], [-16.0, 0.0, 0.0])

    def test_walk_toward_objective_elsewhere_refused(self):
        out = server.walk_toward(objective="shadowheart")
        self.assertIn("FAILED", out)
        self.assertIn("not in this area", out)
        self.assertEqual(self.fake.jobs, [])

    def test_walk_toward_target(self):
        h = server.H.get(DOOR, "Sphincter Door")
        server.walk_toward(target=h, max_m=12, wait=True)
        reach = [a for op, a in self.fake.calls if op == "nav.reach_toward"][-1]
        self.assertEqual(reach["target"], DOOR)
        self.assertEqual(reach["max_m"], 12)

    def test_walk_toward_unreachable(self):
        self.fake.reach = {"reachable": False, "samples": 9}
        out = server.walk_toward(direction="N", wait=True)
        self.assertTrue(out.startswith("FAILED: no walkable ground toward N within 30m (9 spots checked)"), out)
        self.assertEqual(self.fake.jobs, [])

    def test_walk_toward_needs_an_argument(self):
        self.assertIn("FAILED", server.walk_toward())

    def test_batchable(self):
        self.assertIn("walk_toward", server._BATCHABLE)


if __name__ == "__main__":
    unittest.main()
