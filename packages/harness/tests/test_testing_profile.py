"""bg3harness.testing.profile: one temp profile per process, forced over whatever the shell exported; and the pytest
plugin that sets it for every pytest run in the venv and stops a run that reaches the real profile (live6 §3)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from bg3harness import steam_paths
from bg3harness.testing.profile import DIR_VARS, ENV, leaks, temp_profile

FAR = str(Path.home() / "bg3ai-not-a-temp-dir")  # outside the temp dir; nothing is created there


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
        self.assertEqual(env["BG3AI_RUNTIME"], str(root / "runtime"))  # renv.RUNTIME reads it once, at import

    def test_a_second_call_reuses_the_first_dir(self) -> None:
        env: dict[str, str] = {}
        root = temp_profile(env)
        self.addCleanup(root.rmdir)
        env["BG3AI_SAVE_DIR"] = "/elsewhere"
        self.assertEqual(temp_profile(env), root)
        self.assertEqual(env[ENV], str(root))
        self.assertEqual(env["BG3AI_SAVE_DIR"], str(Path(root) / "saves"))


class Leaks(unittest.TestCase):
    def test_temp_dirs_pass_and_others_are_named(self) -> None:
        env: dict[str, str] = {}
        root = temp_profile(env)
        self.addCleanup(root.rmdir)
        self.assertEqual(leaks(env, {}), [])
        env["BG3AI_SE_DIR"] = str(steam_paths.se_dir({}))
        del env["BG3AI_SAVE_DIR"]
        self.assertEqual(leaks(env, {}), ["BG3AI_SAVE_DIR=(unset)", f"BG3AI_SE_DIR={steam_paths.se_dir({})}"])

    def test_the_runner_env_module_counts_once_imported(self) -> None:
        env: dict[str, str] = {}
        root = temp_profile(env)
        self.addCleanup(root.rmdir)
        renv = types.SimpleNamespace(SAVE_DIR=root / "saves", SE_BG3AI_DIR=Path(FAR), RUNTIME=root / "runtime")
        self.assertEqual(leaks(env, {"bg3harness.runner.env": renv}), [f"renv.SE_BG3AI_DIR={FAR}"])
        renv.RUNTIME = Path(FAR) / "runtime"  # the repo's runtime/: imported before $BG3AI_RUNTIME was set
        self.assertEqual(leaks(env, {"bg3harness.runner.env": renv})[-1], f"renv.RUNTIME={FAR}/runtime")


class PytestPlugin(unittest.TestCase):
    """A pytest run with none of the repo's conftests (its own rootdir, as `uv run --project packages/playthrough
    pytest` has) and a shell that exports the real profile folders."""

    def pytest(self, test: str) -> subprocess.CompletedProcess[str]:
        d = Path(tempfile.mkdtemp(prefix="bg3ai-plugin-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pytest.ini").write_text("[pytest]\n")
        (d / "test_it.py").write_text(test)
        env = {k: v for k, v in os.environ.items() if k != ENV and not k.startswith("PYTEST_")}
        real = (steam_paths.saves_dir({}), steam_paths.se_dir({}), steam_paths.profile_dir({}))
        env.update(zip(DIR_VARS, map(str, real), strict=True))
        env["BG3AI_RUNTIME"] = str(Path(__file__).resolve().parents[3] / "runtime")  # the repo's own runtime/
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(d)]
        return subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True, timeout=120)

    def test_the_runner_reads_the_temp_profile(self) -> None:
        run = self.pytest(
            "from bg3harness.runner import env as renv\n"
            "def test_dirs():\n"
            "    assert '/bg3ai-test-profile-' in str(renv.SE_BG3AI_DIR), renv.SE_BG3AI_DIR\n"
            "    assert '/bg3ai-test-profile-' in str(renv.SAVE_DIR), renv.SAVE_DIR\n"
            # 2026-10-28: a module importing renv first kept the repo's runtime/ for every later test in the process
            "    assert '/bg3ai-test-profile-' in str(renv.RUNTIME), renv.RUNTIME\n"
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_a_test_that_points_the_runner_at_a_real_dir_stops_the_run(self) -> None:
        run = self.pytest(
            "from pathlib import Path\n"
            "from bg3harness.runner import env as renv\n"
            f"def test_a():\n    renv.SE_BG3AI_DIR = Path({FAR!r})\n"
            "def test_b():\n    pass\n"
        )
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertIn(
            f"test_it.py::test_a teardown: the real game profile is reachable: renv.SE_BG3AI_DIR={FAR}",
            run.stdout + run.stderr,
        )
        self.assertNotIn("2 passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
