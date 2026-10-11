"""Revivify (S057/S058 0 of 14): two-step scroll clicks (body, then the ground beside it), far bodies refused before a
scroll is spent, and a dead selected character never acts for an omitted `who`."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bg3ai import clicker, server  # noqa: E402

ME = "aaaaaaaa-0000-0000-0000-000000000001"
SH = "aaaaaaaa-0000-0000-0000-000000000002"


class Fake:
    def __init__(self, party, active=ME):
        self.party, self.active, self.calls = party, active, []
        self.revive_after = None   # status reads until SH is alive

    def rpc(self, op, args=None, context="server"):
        self.calls.append((op, args))
        if op == "state.status":
            if self.revive_after is not None:
                self.revive_after -= 1
                if self.revive_after <= 0:
                    for r in self.party:
                        r.pop("dead", None)
            return {"active": self.active, "party": [dict(r) for r in self.party]}
        if op == "ui.screen_pos":
            return {"on_screen": True, "x": 100 + int(10 * args["dist"]), "y": 200}
        if op == "ui.hover_check":
            return {"hovered": False}
        return {}

    try_rpc = rpc


class Inp:
    def __init__(self):
        self.clicks = []

    def move(self, x, y):
        pass

    def wait_frames(self, n):
        pass

    def click(self, x, y, button="L"):
        self.clicks.append((x, y, button))


class Revive(unittest.TestCase):
    def use(self, fake):
        orig = server.game
        server.game = fake
        self.addCleanup(setattr, server, "game", orig)
        saved_h, server.H = server.H, server.render.Handles()
        self.addCleanup(setattr, server, "H", saved_h)
        server.H.get(ME, "Claude")
        server.H.get(SH, "Shadowheart")
        return fake

    def test_two_step_scroll_names(self):
        self.assertTrue(server._two_step_scroll("Scroll of Revivify"))
        self.assertTrue(server._two_step_scroll("Scroll of True Resurrection"))
        self.assertFalse(server._two_step_scroll("Scroll of Invisibility"))

    def test_far_body_refused_before_reading(self):
        self.use(Fake([{"uuid": ME, "pos": [0, 0, 0]}, {"uuid": SH, "pos": [120, 0, 0], "dead": True}]))
        why = server._body_far(ME, SH)
        self.assertIn("120 m away", why)
        self.assertIn("Not read", why)

    def test_near_body_ok(self):
        self.use(Fake([{"uuid": ME, "pos": [0, 0, 0]}, {"uuid": SH, "pos": [3, 0, 0], "dead": True}]))
        self.assertIsNone(server._body_far(ME, SH))

    def test_dead_selected_never_acts(self):
        self.use(Fake([{"uuid": SH, "dead": True}, {"uuid": ME}], active=SH))
        self.assertEqual(server._active(), ME)

    def test_living_selected_kept(self):
        self.use(Fake([{"uuid": SH}, {"uuid": ME}], active=SH))
        self.assertEqual(server._active(), SH)

    def test_second_click_then_revived(self):
        fake = self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        fake.revive_after = 2
        inp = Inp()
        orig = server.REVIVE_WAIT_S
        server.REVIVE_WAIT_S = 2.0
        self.addCleanup(setattr, server, "REVIVE_WAIT_S", orig)
        out = server._revive_spot(inp, "Scroll of Revivify", ME, SH)
        self.assertEqual(len(inp.clicks), 1)
        self.assertIn("REVIVED", out)
        sp = [a for op, a in fake.calls if op == "ui.screen_pos"][0]
        self.assertEqual((sp["toward"], sp["dist"]), (ME, clicker.SPOT_DISTS[0]))

    def test_still_dead_said(self):
        self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        orig = server.REVIVE_WAIT_S
        server.REVIVE_WAIT_S = 0.5
        self.addCleanup(setattr, server, "REVIVE_WAIT_S", orig)
        self.assertIn("STILL DEAD", server._revive_spot(Inp(), "Scroll of Revivify", ME, SH))

    def test_spot_skips_the_body_under_the_cursor(self):
        fake = Fake([])
        hov = iter([{"hovered": True}, {"hovered": False}])
        base = fake.rpc
        fake.rpc = lambda op, args=None, context="server": next(hov) if op == "ui.hover_check" else base(op, args, context)
        inp = Inp()
        cr = clicker.click_spot(fake, inp, SH, ME)
        self.assertTrue(cr.ok)
        self.assertEqual(cr.detail["dist_m"], clicker.SPOT_DISTS[1])
        self.assertEqual(len(inp.clicks), 1)


if __name__ == "__main__":
    unittest.main()
