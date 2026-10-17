"""Offline tests: the display env found from the desktop session (runner/display_env.py)."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
