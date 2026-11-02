"""Offline tests: travel_to and regroup name what came into view on the way and is out of view at the end (the
PASSED line; human-gap research §1e). Each leg's read rides in the check the leg's end already makes (one round
trip, no RPC of its own); what the agent's last view had, what is in view at the end, scenery and clutter are left
out; nearest first from the end spot, capped.

Run: cd bridge && uv run python -m unittest discover -s tests -p 'test_passed.py'
"""

from __future__ import annotations

import math
import unittest
from typing import Any

from test_turn_macros import (  # sets BG3AI_RUNTIME before importing bg3ai
    CHEST,
    GOB,
    P1,
    P2,
    Base,
    FakeGame,
    core,
    delta,
    travel_macro,
)

passed = travel_macro.passed

LADDER = "c0000000-bbbb-cccc-dddd-eeeeeeeeeeee"
CUP = "c1111111-bbbb-cccc-dddd-eeeeeeeeeeee"
ROCK = "c2222222-bbbb-cccc-dddd-eeeeeeeeeeee"
DOOR = "c3333333-bbbb-cccc-dddd-eeeeeeeeeeee"

# The world along a trip from (0,0,0) east to (75,0,0), in legs ending at x=30, 60, 75.
WORLD = [
    {"uuid": CHEST, "name": "Chest", "kind": "container", "pos": [30.0, 0.0, 10.0]},  # leg 1 only
    {"uuid": GOB, "name": "Goblin", "relation": "hostile", "pos": [55.0, 0.0, -20.0], "char": True},  # leg 2 only
    {"uuid": LADDER, "name": "Ladder", "kind": "ladder", "pos": [70.0, 5.0, 0.0]},  # in view at the end
    {"uuid": CUP, "name": "Tin Cup", "kind": "usable", "pos": [30.0, 0.0, -5.0]},  # clutter
    {"uuid": ROCK, "name": "Rock", "kind": "object", "pos": [31.0, 0.0, 3.0]},  # scenery
    {"uuid": DOOR, "name": "Door", "kind": "door", "pos": [5.0, 0.0, 0.0]},  # in the agent's view at the start
]


class Fake(FakeGame):
    """state.nearby is the WORLD within the radius of the centre (args.center, else the active member); state.status
    rows carry positions; read() records each round trip's server ops."""

    def __init__(self) -> None:
        super().__init__()
        self.world: list[dict] = WORLD
        self.reads: list[list[tuple[str, dict]]] = []

    def _at(self, u: str) -> list:
        return next(r["pos"] for r in self.watch["party"] if r["uuid"] == u)

    def rpc(self, op: str, args: dict | None = None, context: str = "server", timeout: float = 12.0) -> Any:
        args = args or {}
        if op == "state.nearby":
            with self.lock:
                self.calls.append((op, args))
            c = self._at(args.get("center") or self.active)
            out: dict = {"characters": [], "items": []}
            for w in self.world:
                d = math.dist(c, w["pos"])
                if d <= args.get("radius", 25):
                    row = {k: v for k, v in w.items() if k != "char"} | {"dist": round(d, 1)}
                    out["characters" if w.get("char") else "items"].append(row)
            return out
        res = super().rpc(op, args, context, timeout)
        if op == "state.status":
            for r in res["party"]:
                r["pos"] = list(self._at(r["uuid"]))
        return res

    def read(self, server=(), client=(), events: bool = True, soft: tuple = ("client",), strict: bool = False):
        self.reads.append(list(server))
        return [self.try_rpc(op, a) for op, a in server], [self.try_rpc(op, a, context="client") for op, a in client]


class Passed(Base):
    def setUp(self) -> None:
        super().setUp()
        self.g = Fake()
        core.game = self.g
        travel_macro.queueing._watch_game = self.g
        self.g.result_for = lambda op, a: {"after": {"pos": a.get("pos")}} if op == "act.move_to" else {}

        def arrive(op, a):  # a walk ends where it was sent (the walkers' positions follow)
            if op == "act.move_to":
                for r in self.g.watch["party"]:
                    if r["uuid"] == a["char"]:
                        r["pos"] = list(a["pos"])

        self.g.on_start = arrive
        self.addCleanup(delta.TRACKER.reset)
        self.addCleanup(passed.LAST.clear)
        delta.TRACKER.reset()

    def leg_reads(self) -> list[dict]:
        """The state.nearby args of every round trip that also read act.watch (a finished step's check)."""
        ops = [dict(g) for g in self.g.reads]
        return [g["state.nearby"] for g in ops if "act.watch" in g and "state.nearby" in g]

    def test_travel_to_names_what_it_passed(self):
        delta.TRACKER.last = {"ents": {DOOR: {"name": "Door"}}}  # the agent's last view had the door
        self.g.watch["party"][1]["pos"] = [0.0, 0.0, 1.0]

        def reach(a):
            x = self.g._at(a["char"])[0]
            return {"reachable": True, "pos": [min(75.0, x + a["max_m"]), 0.0, 0.0]}

        self.g.reach = reach
        out = travel_macro.travel_to(x=75.0, y=0.0, z=0.0)
        self.assertIn("arrived", out)
        self.assertEqual([a["pos"][0] for _, a in self.g.ops("act.move_to") if a["char"] == P1], [30.0, 60.0, 75.0])
        # e1 Goblin, e3 Chest (Base's handles); the ladder is in view at the end, the door was known, the cup is
        # clutter, the rock scenery
        line = "PASSED (seen on the way, out of view now): e1 Goblin hostile 28m SW | e3 Chest <container> 46m W\n"
        self.assertIn(line, out)
        for word in ("Ladder", "Tin Cup", "Rock", "Door"):
            self.assertNotIn(word, out.split("PASSED", 1)[1].split("\n", 1)[0])
        # each leg's read rode in its end check (with act.watch: no round trip of its own), centred on the walker
        self.assertEqual(self.leg_reads(), [{"radius": 25, "center": P1}] * 3)
        n_nearby = sum(1 for op, _ in self.g.calls if op == "state.nearby")
        self.assertEqual(
            n_nearby, 3 + sum(1 for g in self.g.reads if "act.watch" not in dict(g) and "state.nearby" in dict(g))
        )

    def test_nothing_passed_no_line(self):
        self.g.world = [w for w in WORLD if w["uuid"] == LADDER]
        self.g.reach = lambda a: {"reachable": True, "pos": [min(75.0, self.g._at(a["char"])[0] + a["max_m"]), 0, 0]}
        out = travel_macro.travel_to(x=75.0, y=0.0, z=0.0)
        self.assertIn("arrived", out)
        self.assertNotIn("PASSED", out)

    def test_regroup_names_what_its_walkers_passed(self):
        self.g.world = [{"uuid": GOB, "name": "Goblin", "relation": "hostile", "pos": [15.0, 0.0, -24.0], "char": True}]
        self.g.watch["party"][1]["pos"] = [40.0, 0.0, 0.0]
        self.g.reach = lambda a: {"reachable": True, "pos": [max(0.0, self.g._at(a["char"])[0] - a["max_m"]), 0.0, 0.0]}
        out = travel_macro.regroup()
        self.assertIn("(40m → 0m): back", out)
        self.assertIn("PASSED (seen on the way, out of view now): e1 Goblin hostile 28m SE", out)
        self.assertEqual(self.leg_reads(), [{"radius": 25, "center": P2}] * 2)


class Line(unittest.TestCase):
    def setUp(self) -> None:
        self.H = core.H

    def test_cap_nearest_first_and_height(self):
        s = passed.Sightings()
        far = [
            {
                "uuid": f"d{i:07d}-bbbb-cccc-dddd-eeeeeeeeeeee",
                "name": f"Door{i}",
                "kind": "door",
                "pos": [float(10 + i), 0.0, 0.0],
            }
            for i in range(passed.MAX_PASSED + 2)
        ]
        far[0]["pos"] = [10.0, 4.0, 0.0]
        s.add({"items": far})
        line = s.line(
            self.H,
            {
                "nearby": {"characters": [], "items": []},
                "status": {"active": P1, "party": [{"uuid": P1, "pos": [0, 0, 0]}]},
            },
            P1,
        )
        parts = line.split(": ", 1)[1].split(" | ")
        self.assertEqual(len(parts), passed.MAX_PASSED + 1)
        self.assertTrue(parts[0].endswith("Door0 <door> 11m E (4m up)"), parts[0])
        self.assertTrue(parts[1].endswith("Door1 <door> 11m E"), parts[1])
        self.assertEqual(parts[-1], "+2 more")

    def test_no_end_read_no_line(self):
        s = passed.Sightings()
        s.add({"characters": [{"uuid": GOB, "name": "Goblin", "pos": [5, 0, 0]}]})
        self.assertEqual(s.line(self.H, {"status": None}, P1), "")


if __name__ == "__main__":
    unittest.main()
