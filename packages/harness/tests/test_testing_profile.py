"""bg3harness.testing.profile: one temp profile per process, forced over whatever the shell exported; and the pytest
plugin that sets it for every pytest run in the venv and stops a run that reaches the real profile (live6 §3)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from bg3harness import steam_paths
from bg3harness.testing import liveport
from bg3harness.testing.profile import DEAD_PORT, DIR_VARS, ENV, PORT_VAR, SHELL_PORT_VAR, leaks, temp_profile

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

    def test_the_debug_port_is_a_dead_one(self) -> None:
        env = {PORT_VAR: "9998"}  # the live game's, exported by the shell
        self.addCleanup(temp_profile(env).rmdir)
        self.assertEqual(env[PORT_VAR], DEAD_PORT)
        self.assertEqual(env[SHELL_PORT_VAR], "9998")  # kept for the guard, whichever caller runs first
        temp_profile(env)  # a second call (the plugin after the root conftest) keeps it
        self.assertEqual(env[SHELL_PORT_VAR], "9998")


class LivePort(unittest.TestCase):
    def test_the_default_and_the_shells_port_are_guarded(self) -> None:
        self.assertEqual(liveport.guarded_ports(None), {9998})
        self.assertEqual(liveport.guarded_ports("4321"), {9998, 4321})
        self.assertEqual(liveport.guarded_ports(DEAD_PORT), {9998})  # BG3LE_DEBUG_PORT=1 exported, or a nested run
        with mock.patch.object(liveport, "ports", liveport.guarded_ports("4321")):
            self.assertTrue(liveport.blocked(("127.0.0.1", 9998)))
            self.assertTrue(liveport.blocked(("::1", 4321, 0, 0)))
            self.assertFalse(liveport.blocked(("127.0.0.1", 4322)))
            self.assertTrue(liveport.blocked("\0bg3ai-input"))  # the live game's input shim (input.py)
            self.assertFalse(liveport.blocked("\0bg3ai-input-test-7"))  # a test's own shim (native/bg3sdl/test)
            with mock.patch.object(liveport, "allowed", True):  # a test marked live
                self.assertFalse(liveport.blocked(("127.0.0.1", 9998)))


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

    def pytest(
        self, test: str, extra: dict[str, str] | None = None, args: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        d = Path(tempfile.mkdtemp(prefix="bg3ai-plugin-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pytest.ini").write_text("[pytest]\nmarkers = live\n")
        (d / "test_it.py").write_text(test)
        env = {k: v for k, v in os.environ.items() if k != ENV and not k.startswith("PYTEST_")}
        real = (steam_paths.saves_dir({}), steam_paths.se_dir({}), steam_paths.profile_dir({}))
        env.update(zip(DIR_VARS, map(str, real), strict=True))
        env["BG3AI_RUNTIME"] = str(Path(__file__).resolve().parents[3] / "runtime")  # the repo's own runtime/
        env.update(extra or {})
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args, str(d)]
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

    def test_a_plain_test_that_reaches_the_game_port_fails_and_sends_nothing(self) -> None:
        # 2026-11-02: test_batch_args SameText, a plain TestCase, sent act.watch to the live game through the bridge's
        # real Game. Here the "game" is a listener on the port the shell's $BG3LE_DEBUG_PORT names.
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        accepted: list[socket.socket] = []
        threading.Thread(target=lambda: accepted.append(srv.accept()[0]), daemon=True).start()
        port = srv.getsockname()[1]
        run = self.pytest(
            "import os, socket, unittest, pytest\n"
            "from bg3ai.game import Game\n"
            "from bg3harness.testing import liveport\n"
            f"PORT = {port}\n"
            "class Plain(unittest.TestCase):\n"
            "    def test_game(self):\n"
            "        Game(port=PORT).try_rpc('act.watch')\n"
            "def test_the_default_port_is_guarded_and_the_env_one_dead():\n"
            "    assert liveport.blocked(('127.0.0.1', 9998)) and os.environ['BG3LE_DEBUG_PORT'] == '1'\n"
            "@pytest.mark.live\n"
            "def test_live_may_connect():\n"
            "    socket.create_connection(('127.0.0.1', PORT), 5).close()\n",
            {PORT_VAR: str(port)},
        )
        out = run.stdout + run.stderr
        self.assertEqual(run.returncode, 1, out)
        self.assertIn(
            f"test_it.py::Plain::test_game: connect to 127.0.0.1:{port} refused: a test reached the real "
            "game's debug port (mark it live to opt in)",
            out,
        )
        self.assertIn("3 passed, 1 error", out)  # Plain::test_game's call passes, its teardown is the error
        self.assertEqual(len(accepted), 1)  # the live test's; the plain one never got a connection

    def test_the_root_conftest_guards_the_port_with_plugin_autoload_off(self) -> None:
        # review-v010-human-gap #4: with PYTEST_DISABLE_PLUGIN_AUTOLOAD the root conftest set the dead port but
        # installed no socket guard, so code that hard-codes 9998 reached the game. Here only the root conftest loads.
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        accepted: list[socket.socket] = []
        threading.Thread(target=lambda: accepted.append(srv.accept()[0]), daemon=True).start()
        port = srv.getsockname()[1]
        root = str(Path(__file__).resolve().parents[3])
        run = self.pytest(
            f"import socket\ndef test_plain():\n    socket.create_connection(('127.0.0.1', {port}), 2).close()\n",
            {PORT_VAR: str(port), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": root},
            ("-p", "conftest"),
        )
        out = run.stdout + run.stderr
        self.assertIn(f"connect to 127.0.0.1:{port} refused: a test reached the real game's debug port", out)
        self.assertEqual(accepted, [])


if __name__ == "__main__":
    unittest.main()
