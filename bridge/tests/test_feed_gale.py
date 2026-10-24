"""feed_gale after live D10 (research/fix/live.md "Window D10", 2026-10-24): the mod's donate left the window open
and feed_gale replied "done — donated=e132 window_open" with the gloves still worn and Gale's Arcane Hunger on.
The game takes the item only when the window closes; ESCAPE doesn't close it, a click on its X did (then the hunger
went). Now the X is clicked hover-checked (the mod's ui.donate_probe) and done needs the item gone and no hunger.

Run: cd bridge && uv run python -m pytest -q tests/test_feed_gale.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_pickpocket import FakeInput

# isort: split
from bg3ai import hoverclick
from bg3ai.game import JobResult
from bg3ai.render import Handles
from bg3ai.tools import core, galefeed, inventory, world_tools

GALE = "ad9af97d-75da-406a-ae13-7071c563f604"
GLOVES = "1f328160-e493-ea4b-f04e-247a264e34a3"
HUNGER = {"id": "ORI_GALE_BOMB1", "name": "Arcane Hunger"}


class FeedGale(unittest.TestCase):
    def setUp(self):
        saved = (core.H, core.game, world_tools._inp, galefeed.SETTLE_S, galefeed.POLL_S)

        def restore():
            core.H, core.game, world_tools._inp, galefeed.SETTLE_S, galefeed.POLL_S = saved
            hoverclick._hits.clear()

        self.addCleanup(restore)
        hoverclick._hits.clear()
        gap = mock.patch.object(hoverclick, "PROBE_GAP_S", 0.0)
        gap.start()
        self.addCleanup(gap.stop)
        core.H = Handles()
        galefeed.SETTLE_S, galefeed.POLL_S = 0.0, 0.0
        self.job = JobResult("done", {"donated": GLOVES, "window_open": True})  # D10 after DonateItemsCommand(Tag)
        self.window = True
        self.consumes_on_close = True  # D10: closing the window made Gale absorb it
        self.items = [{"uuid": GLOVES, "name": "Gloves of Power", "equipped_slot": "Gloves"}]
        self.hungry = True
        self.ops: list[str] = []
        self.inp = FakeInput(over={(572, 287)})  # the X under the seed (0.5727, 0.2875) of a 1000x1000 window
        test = self
        orig_click = self.inp.click

        def click(x, y, button="L", count=1, sync=True):
            orig_click(x, y, button, count, sync)
            if (x, y) in test.inp.over:
                test.window = False
                if test.consumes_on_close:
                    test.items, test.hungry = [], False

        self.inp.click = click
        world_tools._inp = lambda: self.inp

        class G:
            def run_job(self, op, args, context="server", wait_s=25.0, **kw):
                test.ops.append(op)
                return test.job

            def rpc(self, op, args=None, context="server", **kw):
                test.ops.append(op)
                if op == "ui.donate_probe":
                    if not test.window:
                        return {"open": False}
                    return {"open": True, "button": True, "enabled": True, "over": test.inp.at in test.inp.over}
                if op == "state.status":
                    return {"party": [{"uuid": GALE, "name": "Gale", "statuses": [HUNGER] if test.hungry else []}]}
                if op == "state.inventory":
                    return {"items": test.items}
                return {}

            def try_rpc(self, op, args=None, context="server", default=None):
                return self.rpc(op, args, context)

            def drain_events(self, ctx):
                return []

        core.game = G()
        self.h = core.H.get(GLOVES, "Gloves of Power")

    def feed(self) -> str:
        return getattr(inventory.feed_gale, "fn", inventory.feed_gale)(self.h)

    def test_window_left_open_is_closed_with_a_click_on_its_x(self):
        out = self.feed()
        self.assertEqual(self.inp.clicks(), [("click", 572, 287)])
        self.assertTrue(out.startswith("feed_gale: done — Gale absorbed e1 Gloves of Power"), out)
        self.assertNotIn("window_open", out)

    def test_x_never_hovered_is_not_clicked_and_not_done(self):
        self.inp.over = set()
        out = self.feed()
        self.assertEqual(self.inp.clicks(), [])
        self.assertTrue(out.startswith("feed_gale: FAILED"), out)
        self.assertIn("couldn't click its close button", out)

    def test_hunger_still_on_after_the_close_is_not_done(self):
        self.consumes_on_close = False
        out = self.feed()
        self.assertTrue(out.startswith("feed_gale: FAILED"), out)
        self.assertIn("it is still in the party's inventory", out)
        self.items = []
        self.window = True
        out = self.feed()
        self.assertIn("Gale still has Arcane Hunger", out)

    def test_mod_failure_passes_through_without_a_click(self):
        self.job = JobResult(
            "failed", error="ran Donate, but the item is still in the donation window: nothing was given"
        )
        out = self.feed()
        self.assertTrue(out.startswith("feed_gale: FAILED — ran Donate"), out)
        self.assertEqual(self.inp.clicks(), [])
        self.assertNotIn("state.inventory", self.ops)

    def test_no_input_helper(self):
        world_tools._inp = lambda: None
        out = self.feed()
        self.assertIn("no mouse input to close it", out)


if __name__ == "__main__":
    unittest.main()
