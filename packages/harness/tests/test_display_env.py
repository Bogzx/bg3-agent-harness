"""Offline tests: the display env found from the desktop session (runner/display_env.py)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import display_env as D

PS = "1873 /usr/bin/Xwayland :1 -auth /run/user/1000/xauth_ABcd -rootless -listenfd 4\n"


class DisplayEnv(unittest.TestCase):
    def test_parse_xwayland(self):
        self.assertEqual(D.xwayland_auth(PS), "/run/user/1000/xauth_ABcd")
        self.assertEqual(D.xwayland_display(PS), ":1")
        self.assertEqual(D.xwayland_display("5 Xwayland -rootless :2 -auth /a"), ":2")
        self.assertIsNone(D.xwayland_auth(""))
        self.assertIsNone(D.xwayland_display(""))

    def test_fills_only_what_is_unset(self):
        with tempfile.TemporaryDirectory() as rd:
            for n in ("wayland-0", "wayland-0.lock", "pipewire-0"):
                Path(rd, n).write_text("")
            env = {"XDG_RUNTIME_DIR": rd}
            got = D.fill(env, ps_text=PS)
            self.assertEqual(got, {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_ABcd",
                                   "WAYLAND_DISPLAY": "wayland-0"})
            self.assertEqual(env["DISPLAY"], ":1")
            mine = str(Path(rd, "xauth_mine"))
            Path(mine).write_text("")
            env = {"DISPLAY": ":5", "XAUTHORITY": mine, "WAYLAND_DISPLAY": "w", "XDG_RUNTIME_DIR": rd}
            self.assertEqual(D.fill(env, ps_text=PS), {})
            self.assertEqual(env["XAUTHORITY"], mine)

    def test_a_vanished_xauthority_is_replaced(self):
        # naut3: after the 16:01 freeze Xwayland restarted with a new -auth file; the old value pointed nowhere
        env = {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_gone", "WAYLAND_DISPLAY": "w",
               "XDG_RUNTIME_DIR": "/nonexistent"}
        self.assertEqual(D.fill(env, ps_text=PS), {"XAUTHORITY": "/run/user/1000/xauth_ABcd"})
        self.assertEqual(env["DISPLAY"], ":1")
        env["XAUTHORITY"] = "/run/user/1000/xauth_gone"
        self.assertEqual(D.fill(env, ps_text=""), {})              # no Xwayland: dropped (the runner warns)
        self.assertNotIn("XAUTHORITY", env)

    def test_no_xwayland_keeps_xauthority_unset(self):
        env = {"XDG_RUNTIME_DIR": "/nonexistent"}
        got = D.fill(env, default_display=":9", ps_text="")
        self.assertEqual(got, {"DISPLAY": ":9"})
        self.assertNotIn("XAUTHORITY", env)

    def test_runtime_dir_from_uid(self):
        env: dict = {"DISPLAY": ":1", "XAUTHORITY": "/a"}
        D.fill(env, uid=999999)   # no /run/user/999999
        self.assertNotIn("XDG_RUNTIME_DIR", env)

    def test_problem(self):
        with tempfile.TemporaryDirectory() as d:
            x11, home = Path(d, "x11"), Path(d, "home")
            x11.mkdir()
            home.mkdir()
            Path(x11, "X1").write_text("")
            auth = Path(d, "xauth_ok")
            auth.write_text("")
            def p(env: dict) -> str | None:
                return D.problem(env, home=str(home), x11_dir=str(x11))
            self.assertIsNone(p({"DISPLAY": ":1", "XAUTHORITY": str(auth)}))
            self.assertIsNone(p({"DISPLAY": "unix:1.0", "XAUTHORITY": str(auth)}))
            self.assertIn("DISPLAY is unset", p({"XAUTHORITY": str(auth)}))
            self.assertIn("no X server socket", p({"DISPLAY": ":7", "XAUTHORITY": str(auth)}))
            self.assertIn("doesn't exist", p({"DISPLAY": ":1", "XAUTHORITY": str(auth) + "-gone"}))
            self.assertIn("XAUTHORITY is unset", p({"DISPLAY": ":1"}))
            Path(home, ".Xauthority").write_text("")
            self.assertIsNone(p({"DISPLAY": ":1"}))                  # Xlib's default file
            self.assertIsNone(p({"DISPLAY": "otherhost:0", "XAUTHORITY": str(auth)}))   # remote: no socket check


class LaunchRefusesWithoutDisplay(unittest.TestCase):
    """L8 2026-10-18: launches from a shell without DISPLAY/XAUTHORITY fell back to the AMD iGPU and dumped cores.
    launch_game.sh against a fake game and sniper (its `run` would write a marker): nothing real can start."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        game, sniper = self.tmp / "game", self.tmp / "sniper"
        (game / "bin").mkdir(parents=True)
        sniper.mkdir()
        self.marker = self.tmp / "launched"
        for exe in (game / "bin" / "bg3", sniper / "run"):
            exe.write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 0\n")
            exe.chmod(0o755)
        self.env = {**os.environ, "BG3_DIR": str(game), "BG3AI_SNIPER_DIR": str(sniper),
                    "BG3AI_RUNTIME": str(self.tmp / "runtime"), "BG3AI_PROFILE_DIR": str(self.tmp / "profile"),
                    "BG3AI_CONFIG_DIR": str(self.tmp / "config"), "BG3AI_REQUIRE_STEAM_OFFLINE": "0",
                    "BG3AI_GAME_SCOPE": "0", "BG3LE_DEBUG_PORT": "59997",
                    "DISPLAY": ":97", "XAUTHORITY": str(self.tmp / "no-such-xauth")}   # no /tmp/.X11-unix/X97

    def launch(self, *args: str) -> subprocess.CompletedProcess:
        sh = Path(__file__).resolve().parent / "launch_game.sh"
        return subprocess.run(["bash", str(sh), "--no-bg3le", "--no-input-shim", *args], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def test_refused_exit_5(self):
        self.assertFalse(os.path.exists("/tmp/.X11-unix/X97"))
        r = self.launch()
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("REFUSING TO LAUNCH: no usable display: DISPLAY=:97 but no X server socket", r.stderr)
        self.assertFalse(self.marker.exists())

    def test_dry_run_only_warns(self):
        r = self.launch("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("warning: no usable display", r.stderr)
        self.assertIn("./bin/bg3", r.stdout)
        self.assertFalse(self.marker.exists())

    def test_ui_strict_env(self):
        """S17: BG3AI_UI_STRICT=1 starts the game with bg3le's strict frame lock; unset/0 leaves it off."""
        sh = Path(__file__).resolve().parent / "launch_game.sh"

        def cmd(**env):
            r = subprocess.run(["bash", str(sh), "--no-input-shim", "--dry-run"], capture_output=True, text=True,
                               env={**self.env, "BG3AI_BG3LE_LIB": str(self.tmp / "libbg3le.so"), **env}, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        self.assertIn("BG3LE_UI_STRICT=1", cmd(BG3AI_UI_STRICT="1"))
        self.assertNotIn("BG3LE_UI_STRICT", cmd(BG3AI_UI_STRICT="0"))
        self.assertNotIn("BG3LE_UI_STRICT", cmd())
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
