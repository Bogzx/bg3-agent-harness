"""bg3harness.game.launch_load (first_launch.sh step 7) with a fake game and clock: READY only once the launch's own
-loadSaveGame load has finished, never at the main menu it passes first (live D6: the save loaded twice)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from bg3harness.game import launch_load as L  # noqa: N812

SPLASH = {"page": "SplashScreen"}


class Game:
    """Client states, one per poll (the last one repeats), and the server context from the first Running poll on."""

    def __init__(
        self, states: list[str | None], server_after: int = 0, ui: dict[str, Any] | None = None, alive: int = 10**6
    ):
        self.states, self.server_after, self.alive_polls = states, server_after, alive
        self.ui_reply: dict[str, Any] = ui or {}
        self.t, self.polls, self.running_polls, self.presses = 0.0, 0, 0, 0
        self.logs: list[str] = []

    def state(self) -> str | None:
        st = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return st

    def server(self) -> bool:
        self.running_polls += 1
        return self.running_polls > self.server_after

    def press(self) -> str:
        self.presses += 1
        return "shim Enter"

    def wait(self, timeout: float = 600.0) -> str | None:
        return L.wait_loaded(
            lambda: self.polls < self.alive_polls, timeout, state=self.state, ui=lambda: self.ui_reply,
            server=self.server, press=self.press, clock=lambda: self.t, sleep=self.advance, log=self.logs.append,
        )  # fmt: skip

    def advance(self, s: float) -> None:
        self.t += s


class WaitLoaded(unittest.TestCase):
    def test_waits_through_the_menu_until_the_launch_load_has_finished(self) -> None:
        # D5: LoadMenu, Menu (1.5 s: the menu READY used to come here), then the launch's own load, then Running
        g = Game(["LoadMenu", "Menu", "Menu", "LoadSession", "LoadLevel", "PrepareRunning", "Running"], server_after=2)
        self.assertIsNone(g.wait())
        self.assertEqual(g.polls, 9)  # 2 Running polls before the server context answered
        self.assertEqual(g.presses, 0)

    def test_presses_past_a_splash(self) -> None:
        g = Game(["Menu", "Menu", "LoadSession", "Running"], ui=SPLASH)
        self.assertIsNone(g.wait())
        self.assertEqual(g.presses, 2)
        self.assertIn("splash screen: pressed shim Enter", g.logs[0])

    def test_a_load_that_never_starts_fails_after_the_menu_grace(self) -> None:
        g = Game(["Menu"])
        why = g.wait()
        self.assertEqual(why, "the game stayed at the main menu for 30s: its launch load never started")
        self.assertEqual(g.t, L.MENU_GRACE_S)

    def test_a_splash_does_not_count_as_the_menu(self) -> None:
        g = Game(["Menu"], ui=SPLASH)
        self.assertIn("didn't finish within 100s (client state Menu)", g.wait(timeout=100) or "")

    def test_timeout_names_the_state(self) -> None:
        g = Game(["LoadSession"])
        self.assertEqual(g.wait(timeout=5), "the launch load didn't finish within 5s (client state LoadSession)")

    def test_launcher_exit_fails_at_once(self) -> None:
        g = Game(["LoadMenu", "Menu", "LoadSession"], alive=2)
        self.assertEqual(g.wait(), "the game exited before its launch load finished")
        self.assertEqual(g.t, 2 * L.POLL_S)

    def test_main_exit_code(self) -> None:
        with mock.patch.object(L, "wait_loaded", return_value=None) as w:
            self.assertEqual(L.main(["--timeout", "7", "--pid", "1"]), 0)
        self.assertEqual(w.call_args.args[1], 7.0)
        with mock.patch.object(L, "wait_loaded", return_value="nope"), mock.patch("sys.stderr"):
            self.assertEqual(L.main([]), 1)


if __name__ == "__main__":
    unittest.main()
