"""Offline tests: the minimap block (human-gap research 2026-11-02, item 2). Every walk end out of combat and every
look carry the objectives (distance, direction, height), the WAYS line and the nearest unvisited leads, always, from
the read the result makes anyway.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_minimap.py'
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    CHEST,
    P1,
    P2,
    Base,
    batch,
    core,
    delta,
    macro_tools,
    plumbing,
    travel,
    world_tools,
)

minimap = batch.minimap
Handles = delta.Handles

STATUS = {
    "active": P1,
    "party": [
        {"uuid": P2, "name": "Lae'zel", "pos": [5.0, 9.0, 0.0]},
        {"uuid": P1, "name": "Claude", "pos": [0.0, 2.0, 0.0]},
    ],
}
MARKERS = [
    {
        "label": "Escape the ship",
        "quest": "Escape the Nautiloid",
        "here": True,
        "dist": 16.0,
        "dir": "W",
        "pos": [-16.0, -1.4, 0.0],
        "oid": 1,
    },
    {
        "label": "Find the helm",
        "quest": "Escape the Nautiloid",
        "here": True,
        "dist": 40.2,
        "dir": "N",
        "pos": [0.0, 13.6, 40.0],
        "oid": 2,
    },
    {
        "label": "Find the helm",
        "quest": "Escape the Nautiloid",
        "here": True,
        "dist": 40.2,
        "dir": "N",
        "pos": [0.0, 13.6, 40.0],
        "oid": 3,
    },
    {"label": "Rescue the Trapped Woman", "here": True, "dist": 55.0, "dir": "E", "pos": [55.0, 2.5, 0.0], "oid": 4},
    {"label": "Find a healer", "quest": "Find a Cure", "oid": 5},
]
U = "cccccccc-bbbb-cccc-dddd-eeeeeeeeee{:02d}".format
FRONTIER = {
    "leads": [
        {"uuid": U(1), "name": "Door", "kind": "door", "dist": 15.3, "dir": "NW", "dy": -2.4, "unvisited": True},
        {"uuid": U(2), "name": "Crate", "kind": "container", "dist": 2.0, "dir": "E"},
        {"uuid": U(3), "name": "Ladder", "kind": "ladder", "dist": 4.1, "dir": "S", "unvisited": True},
        {"uuid": U(4), "name": "Chest", "kind": "container", "dist": 22.0, "dir": "N", "unvisited": True},
        {"uuid": U(5), "name": "Lever", "kind": "lever", "dist": 9.0, "dir": "W", "unvisited": True},
    ],
    "ways": [{"dir": "N", "open_m": 20}, {"dir": "E", "open_m": 8}, {"dir": "S", "open_m": 0}],
}


class Render(unittest.TestCase):
    def test_the_block(self):
        h = Handles()
        out = minimap.block({"status": STATUS, "markers": MARKERS, "frontier": FRONTIER}, h).splitlines()
        self.assertEqual(
            out[0],
            "OBJECTIVES o1 Escape the ship 16m W [Escape the Nautiloid] | o2/o3 Find "
            "the helm 40.2m N [Escape the Nautiloid] | o4 Rescue the Trapped Woman 55m E"
            " | +1 more (get_map lists all)",
        )
        self.assertEqual(out[1], "WAYS walkable: N 20m+, E 8m | blocked: S")
        self.assertEqual(
            out[2],
            f"LEADS (unvisited; get_frontier lists all): {h.get(U(3))} Ladder <ladder> 4.1m S | "
            f"{h.get(U(5))} Lever <lever> 9m W | {h.get(U(1))} Door <door> 15.3m NW (2.4m down) "
            "| +1 more",
        )
        self.assertEqual(len(out), 3)

    def test_another_area_and_none(self):
        self.assertEqual(
            minimap.objectives_line([{"label": "Find a healer", "quest": "Find a Cure", "oid": 5}]),
            "OBJECTIVES o5 Find a healer (another area) [Find a Cure]",
        )
        self.assertEqual(minimap.objectives_line([]), "OBJECTIVES none on the map")
        self.assertEqual(minimap.objectives_line(None), "")  # not read: no line
        self.assertEqual(
            minimap.leads_line({"leads": [FRONTIER["leads"][1]]}, Handles()),
            "LEADS (unvisited; get_frontier lists all): none",
        )

    def test_size(self):
        # target <= ~300 tokens: a busy spot (3 objectives with quests, 8 directions, 3 leads) at ~4 chars/token
        busy = {
            "status": STATUS,
            "markers": MARKERS * 3,
            "frontier": dict(
                FRONTIER, ways=[{"dir": d, "open_m": 20, "dy": 3} for d in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")]
            ),
        }
        text = minimap.block(busy, Handles())
        self.assertLess(len(text) / 4, 300, text)


class WalkEnds(Base):
    """The block on the walk ends, from the result's own read."""

    def setUp(self) -> None:
        super().setUp()
        real = self.g.rpc

        def rpc(op, args=None, context="server", timeout=12.0):
            if op == "nav.frontier":
                self.g.calls.append((op, args or {}))
                return {
                    "leads": [
                        {"uuid": CHEST, "name": "Door", "kind": "door", "dist": 15.3, "dir": "NW", "unvisited": True}
                    ],
                    "ways": [{"dir": "N", "open_m": 20}],
                }
            if op == "nav.markers":
                self.g.calls.append((op, args or {}))
                return {
                    "markers": [
                        {"label": "Escape the ship", "here": True, "dist": 16.0, "dir": "W", "pos": [-16.0, -4.0, 0.0]}
                    ]
                }
            return real(op, args, context, timeout)

        self.g.rpc = rpc
        self.g.watch["party"][0]["pos"] = [0.0, 0.0, 0.0]

    def assert_block(self, out: str) -> None:
        self.assertIn("\nOBJECTIVES o", out)
        self.assertIn(" Escape the ship 16m W", out)
        self.assertIn(
            f"\nWAYS walkable: N 20m+\nLEADS (unvisited; get_frontier lists all): {core.H.get(CHEST)} Door "
            "<door> 15.3m NW",
            out,
        )

    def test_a_walk_that_stopped_short_carries_it(self):
        # before: only a walk that ended "done" got LEADS/WAYS; a stopped walk is where the agent looks around most
        self.g.dur["act.move_to"] = 0.2
        self.g.refuse_if = lambda op, a: None
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0, wait=False)
        h = out.split("started ", 1)[1].split(",")[0].split()[0]
        self.g.jobs[int(h[1:])].update(status="failed", error="path blocked")
        self.assert_block(batch.wait_for(h, timeout_s=5))

    def test_a_walk_that_ends_in_the_settle_window(self):
        plumbing.SETTLE_S = 1.0
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0, wait=False)
        self.assertIn("move_to: done", out)
        self.assert_block(out)

    def test_jump_to(self):
        self.g.landing = {"safe": True}
        out = travel.jump_to(3.0, 0.0, 0.0)
        self.assertIn("jump_to: done", out)
        self.assert_block(out)

    def test_one_read_no_rpc_of_its_own(self):
        # the block reads the frontier in the CHANGES read (one delta.fetch): no separate nav.frontier/nav.markers
        reads = []
        real_fetch = delta.fetch
        self.enterContext(
            mock.patch.object(
                delta,
                "fetch",
                lambda game, parts=delta.ACTION_PARTS, events=False: (
                    reads.append(parts) or real_fetch(game, parts, events)
                ),
            )
        )
        self.g.dur["act.move_to"] = 0.05
        h = world_tools.move_to(x=5.0, y=0.0, z=0.0, wait=False).split("started ", 1)[1].split(",")[0].split()[0]
        n0 = len(self.g.calls)
        out = batch.wait_for(h, timeout_s=5)
        self.assert_block(out)
        self.assertEqual(reads, [(*delta.ACTION_PARTS, "frontier")])
        later = self.g.calls[n0:]
        self.assertEqual(sum(op == "nav.frontier" for op, _ in later), 1)
        self.assertEqual(sum(op == "nav.markers" for op, _ in later), 1)

    def test_not_in_combat(self):
        # a combat walk blocks and ends with the combat CHANGES only (one_action_combat), never the block
        self.g.combat = {"active": True, "current": core.H.get(P1)}
        self.g.watch["combat"] = True
        self.g.result_for = lambda op, a: {"after": {"hp": 20, "max_hp": 20, "in_combat": True, "my_turn": True}}
        out = world_tools.move_to(x=5.0, y=0.0, z=0.0, wait=False)
        self.assertIn("move_to: done", out)
        self.assertNotIn("OBJECTIVES", out)
        self.assertNotIn("LEADS", out)
        self.assertFalse(any(op == "nav.frontier" for op, _ in self.g.calls))

    def test_regroup_end(self):
        out = macro_tools._macro_finish("regroup", {}, ["regroup at e4 Claude: 1 walking"], None, [], 0.0, walked=True)
        self.assert_block(out)

    def test_an_objective_has_no_height(self):
        # The HUD's compass and minimap show a quest diamond's direction and distance, never up/down (live window
        # 2026-11-03: a marker 7.7 m down showed no arrow or number), so the line gives no height either.
        st = {"active": P1, "party": [{"uuid": P1, "pos": [0.0, 0.0, 0.0]}]}
        out = minimap.block({"status": st, "markers": [{"label": "X", "here": True, "dist": 9.0, "dir": "N",
                                                        "pos": [0.0, -4.0, 9.0], "oid": 1}]}, Handles())
        self.assertEqual(out.splitlines()[0], "OBJECTIVES o1 X 9m N")


if __name__ == "__main__":
    unittest.main()
