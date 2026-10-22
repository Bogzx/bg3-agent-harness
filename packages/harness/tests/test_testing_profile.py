"""bg3harness.testing.profile: one temp profile per process, forced over whatever the shell exported."""

from __future__ import annotations

import unittest
from pathlib import Path

from bg3harness import steam_paths
from bg3harness.testing.profile import ENV, temp_profile


class TempProfile(unittest.TestCase):
    def test_the_real_folders_are_overridden(self) -> None:
        real = steam_paths.profile_dir({})
        env = {
            "BG3AI_SAVE_DIR": str(steam_paths.saves_dir({})),
            "BG3AI_SE_DIR": str(steam_paths.se_dir({})),
            "BG3AI_PROFILE_DIR": str(real),
        }
        root = temp_profile(env)
        self.addCleanup(root.rmdir)
        self.assertTrue(root.name.startswith("bg3ai-test-profile-"))
        self.assertEqual(steam_paths.saves_dir(env), root / "saves")
        self.assertEqual(steam_paths.se_dir(env), root / "se")
        self.assertEqual(steam_paths.profile_dir(env), root / "profile")

    def test_a_second_call_reuses_the_first_dir(self) -> None:
        env: dict[str, str] = {}
        root = temp_profile(env)
        self.addCleanup(root.rmdir)
        env["BG3AI_SAVE_DIR"] = "/elsewhere"
        self.assertEqual(temp_profile(env), root)
        self.assertEqual(env[ENV], str(root))
        self.assertEqual(env["BG3AI_SAVE_DIR"], str(Path(root) / "saves"))


if __name__ == "__main__":
    unittest.main()
