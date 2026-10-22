#!/usr/bin/env python3
"""Offline tests for bg3harness.host_tuning (a fake sudo runner and a fake platform_profile; no real sudo)."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bg3harness import host_tuning as H


class FakeSudo:
    def __init__(self, ok: bool = True):
        self.calls, self.ok = [], ok

    def __call__(self, cmd, input=None, **_kw):
        self.calls.append((cmd, input))
        if self.ok:
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=1, stdout="", stderr="sudo: a password is required\n")


class HostTuningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-test-tune"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.profile = self.tmp / "platform_profile"
        self.profile.write_text("performance\n")
        (self.tmp / "platform_profile_choices").write_text("quiet balanced performance\n")

    def test_marker_path(self):
        with mock.patch.dict(os.environ, {"BG3AI_CONFIG_DIR": str(self.tmp)}):
            self.assertEqual(H.marker(), self.tmp / "host-tuning")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(H.marker(Path("/home/x")), Path("/home/x/.config/bg3ai/host-tuning"))

    def test_read_config(self):
        m = self.tmp / "host-tuning"
        self.assertEqual(H.read_config(m), ({}, []))                       # no marker: nothing to do
        m.write_text("# box\nplatform_profile=balanced\nnvidia_max_clock = 1500  # MHz\nfan=max\nnvidia_min_clock=low\n")
        cfg, warn = H.read_config(m)
        self.assertEqual(cfg, {"platform_profile": "balanced", "nvidia_max_clock": "1500"})
        self.assertEqual(len(warn), 2)
        self.assertIn("'fan=max'", warn[0])
        self.assertIn("MHz", warn[1])

    def test_apply_with_sudo(self):
        sudo = FakeSudo()
        out = H.apply({"platform_profile": "balanced", "nvidia_max_clock": "1500"}, sudo, self.profile)
        self.assertEqual(sudo.calls, [(["sudo", "-n", "tee", str(self.profile)], "balanced\n"),
                                      (["sudo", "-n", "nvidia-smi", "-lgc", "210,1500"], "")])
        self.assertEqual(out, ["set platform_profile performance -> balanced",
                               "locked the NVIDIA GPU clock to 210,1500 MHz (nvidia-smi -lgc)"])
        sudo = FakeSudo()
        H.apply({"nvidia_max_clock": "1400", "nvidia_min_clock": "300"}, sudo, self.profile)
        self.assertEqual(sudo.calls[0][0][-1], "300,1400")

    def test_already_set_needs_no_sudo(self):
        self.profile.write_text("balanced\n")
        sudo = FakeSudo()
        self.assertEqual(H.apply({"platform_profile": "balanced"}, sudo, self.profile),
                         ["platform_profile already balanced"])
        self.assertEqual(sudo.calls, [])

    def test_sudo_with_a_password_is_skipped_with_a_warning(self):
        out = H.apply({"platform_profile": "balanced", "nvidia_max_clock": "1500"}, FakeSudo(ok=False), self.profile)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(ln.startswith("WARNING: skipped") and "a password is required" in ln for ln in out))
        out = H.apply({"nvidia_max_clock": "1500"}, mock.Mock(side_effect=FileNotFoundError("sudo")), self.profile)
        self.assertTrue(out[0].startswith("WARNING: skipped nvidia_max_clock=1500"))

    def test_bad_profile_and_no_sysfs(self):
        sudo = FakeSudo()
        out = H.apply({"platform_profile": "turbo"}, sudo, self.profile)
        self.assertIn("not one of quiet balanced performance", out[0])
        out = H.apply({"platform_profile": "balanced"}, sudo, self.tmp / "none" / "platform_profile")
        self.assertIn("WARNING: skipped platform_profile=balanced: no ", out[0])
        self.assertEqual(sudo.calls, [])

    def test_plan_runs_nothing(self):
        sudo = FakeSudo()
        out = H.apply({"platform_profile": "balanced", "nvidia_max_clock": "1500"}, sudo, self.profile, dry=True)
        self.assertEqual(out, ["would set platform_profile performance -> balanced",
                               "would lock the NVIDIA GPU clock to 210,1500 MHz (nvidia-smi -lgc)"])
        self.assertEqual(sudo.calls, [])

    def test_main_without_a_marker_is_silent(self):
        with mock.patch.dict(os.environ, {"BG3AI_CONFIG_DIR": str(self.tmp)}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.main(["apply"]), 0)
        self.assertEqual(out.getvalue(), "")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(H.main([]), 2)

    def test_launch_game_applies_it_before_the_launch(self):
        sh = (Path(__file__).resolve().parents[3] / "runner" / "launch_game.sh").read_text()
        call = sh.index('-m bg3harness.host_tuning "$_tune"')
        self.assertLess(call, sh.index('launch=("${scope[@]}"'))
        self.assertIn("|| true", sh[call:sh.index("\n", call)])           # never fails a launch


if __name__ == "__main__":
    unittest.main()
