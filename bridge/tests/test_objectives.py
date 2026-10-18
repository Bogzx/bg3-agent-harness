"""Offline tests: objective markers in get_status/get_map and the walk_toward tool (no game needed).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Any

_RUNTIME_TMP = tempfile.TemporaryDirectory(prefix="bg3ai-test-")  # removed at exit (/tmp filled up 2026-10-10)
os.environ.setdefault("BG3AI_RUNTIME", _RUNTIME_TMP.name)  # tool logs go here, not runtime/

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
        if op == "nav.markers":  # copies: server._set_objectives tags each with its oid
            return {"markers": [dict(m) for m in self.markers], "ms": 1}
        if op == "nav.reach_toward":
            return self.reach
        if op == "act.watch":  # in combat: wait=true still blocks (the blocking path these tests check)
            return {"combat": True}
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

    def test_two_markers_of_one_target_one_line(self):
        # live 20261016-183253: o1 and o2 both "Zariel's Asset 1508.9m N [The Blade of Frontiers]"
        z = {"label": "Zariel's Asset", "quest": "The Blade of Frontiers", "here": True, "dist": 1508.9, "dir": "N"}
        ms = [dict(z, oid=1), dict(z, oid=2), {"label": "Path to Crèche Y'llek", "quest": "The Githyanki Warrior",
                                                "here": True, "dist": 1547.2, "dir": "N", "oid": 3}]
        self.assertEqual(render.render_objectives(ms, limit=2), [
            "OBJECTIVE o1/o2 Zariel's Asset 1508.9m N [The Blade of Frontiers]",
            "OBJECTIVE o3 Path to Crèche Y'llek 1547.2m N [The Githyanki Warrior]"])
        # a merged marker takes no limit slot and is not counted as "more"
        self.assertEqual(render.render_objectives(ms, limit=1), [
            "OBJECTIVE o1/o2 Zariel's Asset 1508.9m N [The Blade of Frontiers]",
            "(+1 more objective; get_map lists all)"])
        # the same label elsewhere on the map stays its own line; both ids still resolve
        far = dict(z, oid=4, dist=30)
        self.assertEqual(render.render_objectives(ms + [far])[-1], "OBJECTIVE o4 Zariel's Asset 30m N [The Blade of Frontiers]")
        self.assertEqual(render.find_objective(ms, "o2")["oid"], 2)

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
        server._objective_ids.clear()
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

    def test_map_unchanged_names_the_objectives_here(self):
        # naut2 run 1: "No map change (Nautiloid; 1 objectives, 0 markers)" hid that o1 now stood 66.8m W
        server.get_map()
        out = server.get_map()
        self.assertTrue(out.startswith("No map change (Nautiloid; 3 objectives"))
        self.assertIn("Here: o1 Escape the ship 16m W", out)
        self.assertIn("o2 Find the helm 40.2m N", out)
        self.assertNotIn("Shadowheart", out)
        server.MAP_SAME_OBJECTIVES = False
        self.addCleanup(setattr, server, "MAP_SAME_OBJECTIVES", True)
        self.assertNotIn("Here:", server.get_map())

    def test_objective_moved_on_after_arrival(self):
        server.get_map()  # o1/o2 ids as shown
        arrived_at = [-16.0, 0.0, 0.0]
        self.assertEqual(server._objective_moved_note("o1", arrived_at), "")  # still there: nothing to say
        moved = dict(MARKERS[0], pos=[-60.0, 0.0, 30.0], dist=66.8, dir="W")
        self.fake.markers = [moved, MARKERS[1], MARKERS[2]]
        self.assertEqual(server._objective_moved_note("o1", arrived_at),
                         'o1 Escape the ship moved on: now 66.8m W: travel_to(objective="o1") again')
        self.fake.markers = [MARKERS[1], MARKERS[2]]
        self.assertIn("is gone now", server._objective_moved_note("o1", arrived_at))

    def test_objective_handle_as_a_target(self):
        """S041 22:24 talk_to(npc="o1") for the Balthazar marker: 'unknown target'. o1 resolves by its label."""
        balth = "99999999-2222-3333-4444-555555555555"
        self.fake.markers = [{"label": "Balthazar", "quest": "Find the Nightsong", "pos": [-5.0, 0, 0], "dist": 5.0,
                              "dir": "W", "here": True}] + MARKERS
        server.H.get(balth, "Balthazar")
        self.assertEqual(server._uuid("o1"), balth)
        orig = self.fake.try_rpc
        self.fake.try_rpc = lambda op, args=None, context="server": {} if op == "state.nearby" else orig(op, args, context)
        with self.assertRaises(ValueError) as cm:
            server._uuid("o2")  # "Escape the ship": nothing in view by that name
        self.assertIn("o2 is the objective marker 'Escape the ship'", str(cm.exception))
        self.assertIn("route(objective='o2', go=true)", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            server._uuid("o9")
        self.assertIn("no objective o9 now", str(cm.exception))

    def test_objective_target_by_id_and_label(self):
        """S094 09:54 interact("Netherbrain Stem") (o1's label) for the Brain Stem: 'unknown target' (the item's name
        differs and it wasn't listed). nav.markers names the item the marker sits on: o1 and the label resolve to it."""
        stem = "dddddddd-0000-0000-0000-000000000466"
        self.fake.markers = [{"label": "Netherbrain Stem", "quest": "Confront the Elder Brain", "pos": [-5.0, 0, 0],
                              "dist": 5.7, "dir": "SW", "here": True, "target": stem}] + MARKERS
        out = server.get_status()
        self.assertIn("OBJECTIVE o1 Netherbrain Stem 5.7m SW", out)
        self.assertNotIn(stem, out)
        self.assertEqual(server._uuid("o1"), stem)
        orig = self.fake.try_rpc
        self.fake.try_rpc = lambda op, args=None, context="server": (
            {} if op in ("state.nearby", "state.combat") else orig(op, args, context))
        self.assertEqual(server._uuid("Netherbrain Stem"), stem)
        self.assertEqual(server._uuid("netherbrain stem"), stem)
        with self.assertRaises(ValueError) as cm:
            server._uuid("Netherbrain")  # part of a label is no target
        self.assertIn("unknown target 'Netherbrain'", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            server._uuid("Escape the ship")  # an objective without a marked thing (a trigger/area)
        self.assertIn("unknown target 'Escape the ship'", str(cm.exception))

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

    def test_objective_ids_survive_a_resort(self):
        """S059 08:53: route(objective="o3") meant Gortash; the list re-sorted by distance and o3 was the Ordination."""
        ms = [dict(m) for m in MARKERS]
        self.fake.markers = ms
        server.get_map()
        self.fake.markers = [ms[1], ms[0], ms[2]]  # the party moved: the helm is nearest now
        server._objectives["at"] = 0.0  # stale: the next use fetches the re-sorted list
        pos, what = server._travel_dest("", "o1", None, None, None)
        self.assertEqual(pos, [-16.0, 0.0, 0.0])
        self.assertIn("Escape the ship", what)
        out = server.get_map(full=True)
        self.assertIn("OBJECTIVE o2 Find the helm", out)
        self.assertIn("OBJECTIVE o1 Escape the ship", out)
        self.fake.markers = [ms[1], ms[2]]  # "Escape the ship" done: o1 is gone, not the helm
        server._objectives["at"] = 0.0
        with self.assertRaises(ValueError) as cm:
            server._travel_dest("", "o1", None, None, None)
        self.assertIn("no objective o1 now (done or gone); current: o2 Find the helm", str(cm.exception))

    def test_one_marker_under_two_quests_is_not_ambiguous(self):
        """S059 08:53:12 route(objective="Gortash"): 'matches 3 objectives' for one marker in two quests."""
        g = [-34.8, 64.5, 200.3]
        ms = [{"label": "Gortash", "quest": "Servant of None", "pos": g, "dist": 22.2, "here": True},
              {"label": "Gortash", "quest": "Get Gortash's Netherstone", "pos": list(g), "dist": 22.2, "here": True}]
        self.assertIs(render.find_objective(ms, "gortash"), ms[0])
        ms[1]["pos"] = [10.0, 0.0, 0.0]
        with self.assertRaises(ValueError) as cm:
            render.find_objective(ms, "gortash")
        self.assertIn("o1 Gortash [Servant of None], o2 Gortash [Get Gortash's Netherstone]; use the id", str(cm.exception))

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

    def test_walk_toward_chasm_edge(self):
        # S035 19:38: walk_toward W ran onto the Faith-Leap chasm's invisible floor (Claude died)
        self.fake.reach = {"reachable": False, "samples": 6, "hidden_floor": {"pos": [-766.5, -28.4, -731.6], "at_m": 12.3}}
        out = server.walk_toward(direction="W", wait=True)
        self.assertTrue(out.startswith("FAILED: the way toward W is the chasm, with no floor you can see"
                                       " (12.3m along, at (-766.5,-28.4,-731.6))"), out)
        self.assertIn("short steps (move_to ≤ 3m)", out)
        self.assertEqual(self.fake.jobs, [])
        self.fake.reach = {"reachable": True, "pos": [-764.0, -28.3, -732.5], "path_m": 10.0, "straight_m": 10.0,
                           "dir": "W", "hidden_beyond": {"pos": [-766.5, -28.4, -731.6], "at_m": 12.3}}
        out = server.walk_toward(direction="W", wait=True)
        self.assertIn("stopping at the chasm edge (no floor you can see beyond", out)

    def test_walk_toward_needs_an_argument(self):
        self.assertIn("FAILED", server.walk_toward())

    def test_batchable(self):
        self.assertIn("walk_toward", server._BATCHABLE)


if __name__ == "__main__":
    unittest.main()
