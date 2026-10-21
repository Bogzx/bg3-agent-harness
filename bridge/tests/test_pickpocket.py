"""Offline tests: the pickpocket bugs found live (research/ovh/live2.md §1, 2026-10-21) and the hover-checked click.

1. take= never stole: ui.pickpocket "pressed" StealBtn through its Command and nothing happened. Now the item is
   selected with nothing pressed (min_chance above any odds) and Steal is clicked hover-checked (hoverclick.py,
   the mod's ui.steal_probe), then the window's roll is read.
2. pickpocket(npc) listed whatever window was open, another NPC's too: a window this server did not open for this
   NPC is closed first and the NPC's own is opened.
3. The listed odds ignore being seen: the listing and the description say so.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_pickpocket.py
"""

from __future__ import annotations

import unittest
from typing import ClassVar
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import economy, hoverclick
from bg3ai.game import JobResult
from bg3ai.render import Handles
from bg3ai.tools import core, reporting, world_tools


def _u(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class FakeInput:
    def __init__(self, over: set[tuple[int, int]] | None = None):
        self.over = over or set()
        self.at = (0, 0)
        self.log: list[tuple] = []

    def window_size(self):
        return (1000, 1000)

    def move(self, x, y, sync=True):
        self.at = (x, y)
        self.log.append(("move", x, y))

    def click(self, x, y, button="L", count=1, sync=True):
        self.log.append(("click", x, y))

    def wait_frames(self, n, sync=True):
        pass

    def clicks(self):
        return [e for e in self.log if e[0] == "click"]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class HoverClick(unittest.TestCase):
    def setUp(self):
        hoverclick._hits.clear()
        self.addCleanup(hoverclick._hits.clear)

    def probe_for(self, inp: FakeInput, window=None):
        state = window if window is not None else {"open": True}
        return lambda: dict(state, over=inp.at in inp.over)

    def test_candidates_seed_first_then_nearest(self):
        pts = hoverclick.candidates((0.5, 0.5), step=0.01, rings=1)
        self.assertEqual(pts[0], (0.5, 0.5))
        self.assertEqual(len(pts), 9)
        self.assertEqual(sorted(pts[1:5]), [(0.49, 0.5), (0.5, 0.49), (0.5, 0.51), (0.51, 0.5)])

    def test_clicks_only_where_the_probe_says_over(self):
        inp = FakeInput(over={(512, 500)})
        clock = Clock()
        res = hoverclick.hover_click(self.probe_for(inp), inp, "k", (0.5, 0.5), clock=clock, sleep=clock.sleep)
        self.assertTrue(res["clicked"])
        self.assertEqual(inp.clicks(), [("click", 512, 500)])
        self.assertEqual(inp.log[-1], ("move", 500, 200))   # the cursor leaves the button
        # the hit is tried first next time
        self.assertEqual(hoverclick.candidates((0.5, 0.5), "k")[0], (0.512, 0.5))

    def test_never_clicks_without_hover(self):
        inp = FakeInput()
        clock = Clock()
        res = hoverclick.hover_click(self.probe_for(inp), inp, "k", (0.5, 0.5), budget_s=0.95, clock=clock,
                                     sleep=clock.sleep)
        self.assertIn("no cursor position", res["error"])
        self.assertEqual(inp.clicks(), [])
        self.assertEqual(res["tries"], 10)   # 0.95 s budget / 0.1 s per probe

    def test_closed_or_greyed_window(self):
        inp = FakeInput(over={(500, 500)})
        self.assertTrue(hoverclick.hover_click(lambda: {"open": False}, inp, "k", (0.5, 0.5))["gone"])
        res = hoverclick.hover_click(lambda: {"open": True, "enabled": False}, inp, "k", (0.5, 0.5))
        self.assertIn("greyed out", res["error"])
        self.assertEqual(inp.clicks(), [])


class Steal(unittest.TestCase):
    """The UI op sequence of pickpocket(npc, take=...) against a fake mod."""

    POCKETS: ClassVar[dict] = {"items": [{"uuid": _u(31), "name": "Dagger", "price": 2, "chance": 54, "dc": 12},
                         {"uuid": _u(32), "name": "Ring", "price": 90, "chance": 20, "dc": 18}]}

    def setUp(self):
        saved = (core.H, core.game, world_tools._click_act, world_tools._inp, world_tools.STEAL_ROLL_S)
        core.H = Handles()
        world_tools._pp_open["npc"] = None
        hoverclick._hits.clear()

        def restore():
            core.H, core.game, world_tools._click_act, world_tools._inp, world_tools.STEAL_ROLL_S = saved
            world_tools._pp_open["npc"] = None
            hoverclick._hits.clear()
        self.addCleanup(restore)
        self.ops: list[tuple[str, dict]] = []
        self.window: str | None = None          # the NPC whose window shows
        self.roll_after_click: dict | None = {"finished": True, "success": True, "rolled": 15, "dc": 12}
        self.closes_on_click = False
        self.inp = FakeInput(over={(560, 651)})  # Steal under the seed (0.5604, 0.6519) of a 1000x1000 window
        test = self

        class G:
            def run_job(self, op, args, context="server", wait_s=25.0):
                test.ops.append((op, dict(args)))
                if op == "act.select":
                    return JobResult("done", {})
                if test.window is None:
                    return JobResult("failed", error="no pickpocket window is open: pickpocket(npc) opens it")
                if args.get("leave"):
                    test.window = None
                    return JobResult("done", {"left": True})
                if args.get("take"):
                    it = next(i for i in test.POCKETS["items"] if i["uuid"] == args["take"])
                    if args.get("min_chance", 0) > it["chance"]:
                        return JobResult("failed", {"item": it}, f"{it['name']}: below min_chance; nothing taken")
                    return JobResult("done", {"item": it, "pending": True})   # the dead press (live2)
                if test.window != _u(30):
                    return JobResult("done", {"items": []})                     # another NPC's empty window
                return JobResult("done", test.POCKETS)

            def rpc(self, op, args=None, context="server", **k):
                test.ops.append((op, dict(args or {})))
                if op == "ui.steal_probe":
                    if test.window is None:
                        return {"open": False}
                    clicked = bool(test.inp.clicks())
                    roll = test.roll_after_click if clicked else {"finished": False}
                    return {"open": True, "button": True, "enabled": True, "over": test.inp.at in test.inp.over,
                            "roll": dict(roll or {"finished": False})}
                return {"active": _u(1), "party": [{"uuid": _u(1), "name": "Claude"}]}

            def try_rpc(self, op, *a, **k):
                if op == "ui.selected":
                    return {"uuid": _u(1)}
                return self.rpc(op) if op == "state.status" else None

            def drain_events(self, ctx):
                return []

        core.game = G()
        orig_click = self.inp.click

        def click(x, y, button="L", count=1, sync=True):
            orig_click(x, y, button, count, sync)
            if test.closes_on_click:
                test.window = None
        self.inp.click = click

        def click_act(tool, op, args, wait_s=40.0):
            test.ops.append((op, dict(args)))
            test.window = args["target"]
            reporting._batch["last"] = "done"
            return "pickpocket: done — opened pickpocket"
        world_tools._click_act = click_act
        world_tools._inp = lambda: self.inp
        world_tools.STEAL_ROLL_S = 0.0
        self.npc = core.H.get(_u(30), "Refugee")
        self.dagger = core.H.get(_u(31), "Dagger")

    def names(self) -> list[str]:
        return [op for op, _ in self.ops if op != "state.status"]

    def test_take_selects_without_press_then_clicks_steal(self):
        out = world_tools.pickpocket(self.npc, take=self.dagger)
        self.assertIn("stole e2 Dagger", out)
        self.assertIn("rolled 15 vs DC 12", out)
        take = next(a for op, a in self.ops if op == "ui.pickpocket" and a.get("take"))
        self.assertEqual(take["min_chance"], world_tools.SELECT_ONLY)   # selected, nothing pressed by the mod
        self.assertEqual(self.inp.clicks(), [("click", 560, 651)])       # Steal clicked once, where it was hovered
        i_take = self.names().index("ui.pickpocket", 2)
        self.assertIn("ui.steal_probe", self.names()[i_take:])

    def test_min_chance_takes_nothing_and_never_clicks(self):
        out = world_tools.pickpocket(self.npc, take=self.dagger, min_chance=60)
        self.assertIn("below min_chance 60; nothing taken", out)
        self.assertEqual(self.inp.clicks(), [])

    def test_window_closing_after_the_click_says_caught(self):
        self.closes_on_click = True
        out = world_tools.pickpocket(self.npc, take=self.dagger)
        self.assertIn("CAUGHT?", out)
        self.assertIn("window closed right after Steal", out)

    def test_roll_not_finished(self):
        self.roll_after_click = None
        out = world_tools.pickpocket(self.npc, take=self.dagger)
        self.assertIn("the roll had not finished", out)

    def test_no_hover_no_click(self):
        self.inp.over = set()
        with mock.patch.object(hoverclick, "BUDGET_S", 0.5), mock.patch.object(hoverclick, "PROBE_GAP_S", 0.0):
            out = world_tools.pickpocket(self.npc, take=self.dagger)
        self.assertIn("couldn't click Steal (no cursor position over the button found", out)
        self.assertEqual(self.inp.clicks(), [])

    def test_another_npcs_window_is_closed_first(self):
        # live2 §1: a stale empty Cerys window read "nothing to steal" for Alfira and Tumeril
        self.window = _u(40)
        out = world_tools.pickpocket(self.npc)
        self.assertIn("Dagger", out)
        self.assertEqual(self.names()[:3], ["ui.pickpocket", "ui.pickpocket", "act.pickpocket"])
        self.assertTrue(self.ops[1][1].get("leave"))
        # the window is now ours: a second call lists it without closing
        self.ops.clear()
        world_tools.pickpocket(self.npc)
        self.assertEqual(self.names(), ["ui.pickpocket"])

    def test_leave_forgets_the_window(self):
        world_tools.pickpocket(self.npc)
        world_tools.pickpocket(self.npc, leave=True)
        self.assertIsNone(world_tools._pp_open["npc"])

    def test_seen_rule_is_told(self):
        self.assertIn("in the owner's sight a steal fails", economy.pockets_text(self.POCKETS, lambda it: ""))
        doc = core.mcp._tool_manager.get_tool("pickpocket").description
        self.assertIn("a steal in the owner's sight fails whatever they say", doc)


if __name__ == "__main__":
    unittest.main()
