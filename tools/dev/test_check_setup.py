"""Offline tests for tools/dev/check_setup.py's credits probe (no game needed).

Run: uv run --project bridge python -m unittest tools/dev/test_check_setup.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_setup as C  # noqa: E402

MENU = ["Continue", "New Game", "Load Game", "Multiplayer", "Mod Manager", "Options", "Credits", "Quit Game",
        "Larian Account"]


class FakeGame:
    """A fresh launch: loading for `loading_s`, the splash until Enter, then the main menu, which ignores clicks
    for `fade_s` after it shows (live L6: it reads main_menu while still fading in)."""

    def __init__(self, loading_s=0.0, splash=True, fade_s=0.0, buttons=MENU, credits_work=True):
        self.t, self.loading_s, self.fade_s, self.buttons = 0.0, loading_s, fade_s, buttons
        self.splash, self.credits_work = splash, credits_work
        self.menu_at = None if splash else 0.0
        self.credits, self.keys, self.clicks, self.lines = False, [], [], []

    def ui(self):
        if self.t < self.loading_s:
            return {"screen": "loading", "game_state": "LoadModule", "blocking": True}
        if self.credits:
            return {"screen": "credits", "page": "Credits", "blocking": True, "credits": {"ending": False}}
        if self.menu_at is None or self.t < self.menu_at:
            return {"screen": "splash", "page": "SplashScreen", "blocking": True}
        st = {"screen": "main_menu", "page": "MainMenu", "blocking": True}
        if self.buttons is not None:
            st["menu"] = {"buttons": self.buttons}
        return st

    def key(self, name):
        self.keys.append((round(self.t, 1), name, self.ui()["screen"]))
        if name == "ENTER" and self.ui()["screen"] == "splash":
            self.menu_at = self.t + 1.0
        if name == "ESCAPE":
            self.credits = False

    def click(self):
        self.clicks.append(round(self.t, 1))
        if self.credits_work and self.menu_at is not None and self.t >= self.menu_at + self.fade_s:
            self.credits = True

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def run(self):
        return C.credits_steps(self.ui, self.key, self.click, clock=self.clock, sleep=self.sleep,
                               out=self.lines.append)

    def fails(self):
        return [x for x in self.lines if x.startswith("FAIL")]


class CreditsProbe(unittest.TestCase):
    def test_waits_out_the_loading(self):
        # live L6 run 1: straight after the launch the screen was loading (FAIL splash screen, Enter lost)
        g = FakeGame(loading_s=40)
        self.assertEqual(g.run(), 0, g.lines)
        self.assertEqual([k[1:] for k in g.keys], [("ENTER", "splash"), ("ESCAPE", "credits")])
        self.assertTrue(g.lines[0].startswith("PASS splash screen"))

    def test_menu_fade_in_waited_before_the_click(self):
        # live L6 run 2: the click right after the main menu read did nothing (still fading in)
        g = FakeGame(fade_s=1.5)
        self.assertEqual(g.run(), 0, g.lines)
        self.assertEqual(len(g.clicks), 1)
        self.assertGreaterEqual(g.clicks[0], 1.0 + C.MENU_SETTLE_S)
        self.assertFalse(any(x.startswith("RETRY") for x in g.lines))

    def test_a_click_lost_anyway_is_retried_once(self):
        g = FakeGame(fade_s=C.MENU_SETTLE_S + 1)
        self.assertEqual(g.run(), 0, g.lines)
        self.assertEqual(len(g.clicks), 2)
        self.assertEqual(sum(x.startswith("RETRY") for x in g.lines), 1)
        self.assertEqual(g.fails(), [])

    def test_credits_never_open_fails_after_two_clicks(self):
        g = FakeGame(credits_work=False)
        self.assertEqual(g.run(), 1)
        self.assertEqual(len(g.clicks), 2)
        self.assertTrue(any("menu Credits -> screen credits" in x for x in g.fails()))

    def test_at_the_main_menu_no_enter(self):
        # Enter on the main menu would press Continue (loads a save)
        g = FakeGame(splash=False)
        self.assertEqual(g.run(), 0, g.lines)
        self.assertEqual([k[1] for k in g.keys], ["ESCAPE"])

    def test_menu_without_continue_is_not_clicked(self):
        # the button position assumes Continue is shown; elsewhere (382, 820) is another button
        g = FakeGame(splash=False, buttons=[b for b in MENU if b != "Continue"])
        self.assertEqual(g.run(), 1)
        self.assertEqual(g.clicks, [])

    def test_old_mod_buttons_under_popup(self):
        self.assertEqual(C.menu_buttons({"popup": {"title": "Log In", "buttons": MENU}}), MENU)
        self.assertIsNone(C.menu_buttons({"screen": "main_menu"}))

    def test_stuck_loading_fails_without_keys(self):
        g = FakeGame(loading_s=10 * C.MENU_WAIT_S)
        self.assertEqual(g.run(), 1)
        self.assertEqual(g.keys, [])
        self.assertEqual(g.clicks, [])


if __name__ == "__main__":
    unittest.main()
