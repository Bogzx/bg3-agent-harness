"""The old tools/memory paths and runner/memory_keeper.py are shims over bg3play.memory (packages/playthrough).

The real tests live in packages/playthrough/tests/memory (`uv run --project packages/playthrough pytest`). These
check what the runner relies on: `import memlib` etc. give the package's module objects (so mock.patch on them
patches the code that runs), and the scripts still run from their old paths.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "runner"))


class Shims(unittest.TestCase):
    def test_imports_are_the_package_modules(self):
        import lint
        import memlib
        import memory_keeper
        import new_playthrough
        import session
        import snapshots
        from bg3play import memory

        self.assertIs(memlib, memory.memlib)
        self.assertIs(lint, memory.lint)
        self.assertIs(session, memory.session)
        self.assertIs(snapshots, memory.snapshots)
        self.assertIs(new_playthrough, memory.new_playthrough)
        self.assertIs(memory_keeper, memory.keeper)
        self.assertEqual(memlib.REPO_ROOT, REPO)
        self.assertEqual(memory_keeper.REPO, REPO)

    def test_testutil_for_the_runner_tests(self):
        import _testutil
        import new_playthrough

        self.assertIs(_testutil.new_playthrough, new_playthrough)
        self.assertTrue(callable(_testutil.isolate_git))

    def test_scripts_run_from_the_old_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            for script in ("lint.py", "session.py", "snapshots.py", "new_playthrough.py"):
                p = subprocess.run(
                    [sys.executable, str(HERE / script), "--help"],
                    capture_output=True,
                    text=True,
                    cwd=tmp,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(p.returncode, 0, script + p.stderr)
                self.assertIn("usage", p.stdout.lower(), script)
            p = subprocess.run(
                [sys.executable, str(HERE / "lint.py"), str(Path(tmp) / "nope")],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("does not exist", p.stdout + p.stderr)

    def test_clock_fast_path_skips_the_package(self):
        env = {**os.environ, "BG3AI_SESSION_DEADLINE": "1"}
        p = subprocess.run(
            [sys.executable, "-X", "importtime", str(HERE / "session.py"), "clock"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("bg3play", p.stderr)


if __name__ == "__main__":
    unittest.main()
