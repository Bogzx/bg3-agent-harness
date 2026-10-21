"""Revivify (S057/S058 0 of 14): two-step scroll clicks (body, then the ground beside it), far bodies refused before a
scroll is spent, and a dead selected character never acts for an omitted `who`."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bg3ai import hotbar, render, twostep  # noqa: E402
from bg3ai.tools import combat, core, plumbing, world_tools  # noqa: E402

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
        orig = core.game
        core.game = fake
        self.addCleanup(setattr, core, "game", orig)
        saved_h, core.H = core.H, render.Handles()
        self.addCleanup(setattr, core, "H", saved_h)
        core.H.get(ME, "Claude")
        core.H.get(SH, "Shadowheart")
        return fake

    def test_two_step_scroll_names(self):
        self.assertTrue(world_tools._two_step_scroll("Scroll of Revivify"))
        self.assertTrue(world_tools._two_step_scroll("Scroll of True Resurrection"))
        self.assertFalse(world_tools._two_step_scroll("Scroll of Invisibility"))

    def test_far_body_refused_before_reading(self):
        self.use(Fake([{"uuid": ME, "pos": [0, 0, 0]}, {"uuid": SH, "pos": [120, 0, 0], "dead": True}]))
        why = world_tools._body_far(ME, SH)
        self.assertIn("120 m away", why)
        self.assertIn("Not read", why)

    def test_near_body_ok(self):
        self.use(Fake([{"uuid": ME, "pos": [0, 0, 0]}, {"uuid": SH, "pos": [3, 0, 0], "dead": True}]))
        self.assertIsNone(world_tools._body_far(ME, SH))

    def test_dead_selected_never_acts(self):
        self.use(Fake([{"uuid": SH, "dead": True}, {"uuid": ME}], active=SH))
        self.assertEqual(plumbing._active(), ME)

    def test_living_selected_kept(self):
        self.use(Fake([{"uuid": SH}, {"uuid": ME}], active=SH))
        self.assertEqual(plumbing._active(), SH)

    def ground(self, res):
        """twostep.revive_ground (tested in test_twostep.ReviveScroll) answers `res`; the calls it got."""
        calls = []

        def fake(game, inp, reader, spell, body, should_stop=None):
            calls.append((reader, spell, body))
            return res
        orig = twostep.revive_ground
        twostep.revive_ground = fake
        self.addCleanup(setattr, twostep, "revive_ground", orig)
        for mod, k, v in ((world_tools, "REVIVE_WAIT_S", 1.0), (combat, "_log_hotbar", lambda spell, r: None)):
            self.addCleanup(setattr, mod, k, getattr(mod, k))
            setattr(mod, k, v)
        return calls

    def test_second_click_then_revived(self):
        fake = self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        fake.revive_after = 2
        calls = self.ground(hotbar.HotbarCastResult(True, picked=1))
        out = world_tools._revive_spot(Inp(), "Scroll of Revivify", ME, SH)
        self.assertEqual(calls, [(ME, "Scroll of Revivify", SH)])
        self.assertIn("REVIVED", out)

    def test_still_dead_said(self):
        self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        self.ground(hotbar.HotbarCastResult(True, picked=1))
        self.assertIn("STILL DEAD", world_tools._revive_spot(Inp(), "Scroll of Revivify", ME, SH))

    def test_s089_no_spot_taken_says_nothing_was_cast(self):
        """S089 07:08 x3: the spot was on a creature, the spell stayed in hand, and the result said only STILL DEAD."""
        fake = self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        self.ground(hotbar.HotbarCastResult(False, stage="ground", reason="ground_refused", detail={
            "why": f"a character is under the cursor ({ME})", "cancelled": "escape",
            "looks": [{"hover": ME, "on_avoid": True}]}))
        out = world_tools._revive_spot(Inp(), "Scroll of Revivify", ME, SH)
        self.assertNotIn("STILL DEAD", out)
        self.assertIn("the spell was put back, nothing cast (the scroll is still in the bags)", out)
        self.assertIn("a character is under the cursor (e1 Claude)", out)
        self.assertNotIn("state.status", [op for op, _ in fake.calls])   # no 4 s wait for a cast never made

    def test_spot_in_fire_said(self):
        fake = self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        fake.revive_after = 1
        self.ground(hotbar.HotbarCastResult(True, picked=1, detail={"spot_in": "Fire"}))
        out = world_tools._revive_spot(Inp(), "Scroll of Revivify", ME, SH)
        self.assertIn("REVIVED", out)
        self.assertIn("they stand up in Fire (the spot beside the body the game took): it can put them down again", out)
        self.assertIn("heal them now (Lay on Hands, a potion: Help gives 1 HP)", out)

    def test_nothing_in_hand_after_the_body_click(self):
        self.use(Fake([{"uuid": ME}, {"uuid": SH, "dead": True}]))
        self.ground(hotbar.HotbarCastResult(False, stage="arm", reason="not_armed"))
        out = world_tools._revive_spot(Inp(), "Scroll of Revivify", ME, SH)
        self.assertIn("no spell in hand after it", out)
        self.assertIn("STILL DEAD", out)


if __name__ == "__main__":
    unittest.main()
