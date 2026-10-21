# pyright: basic
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bg3play.memory import memlib, new_playthrough

from tests.memory._testutil import isolate_git


class NewPlaythroughTest(unittest.TestCase):
    def setUp(self):
        isolate_git()
        self.root = Path(tempfile.mkdtemp(prefix="bg3root-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(memlib.TEMPLATE_DIR, self.root / "memory-template")
        (self.root / "play").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = new_playthrough.main([*args, "--repo-root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()

    def test_creates_memory_repo_and_symlink(self):
        rc, _out, err = self.run_main("run1")
        self.assertEqual(rc, 0, err)
        mem = self.root / "playthroughs" / "run1" / "memory"
        for f in memlib.REQUIRED_FILES:
            self.assertTrue((mem / f).is_file(), f)
        for d in memlib.REQUIRED_DIRS:
            self.assertTrue((mem / d).is_dir(), d)
        self.assertTrue((mem / ".gitignore").is_file())
        self.assertIn("# Playthrough: run1", (mem / "PLAYTHROUGH.md").read_text())
        self.assertNotIn("{{", (mem / "PLAYTHROUGH.md").read_text())
        log = memlib.git(mem, "log", "--format=%s").stdout.split("\n")
        self.assertEqual(log[0], "S000 init run1")
        self.assertEqual(memlib.git(mem, "status", "--porcelain").stdout, "")
        link = self.root / "play" / "memory"
        self.assertTrue(link.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(link)))  # relative, so the repo can move
        self.assertEqual(link.resolve(), mem.resolve())

    def test_replaces_existing_symlink(self):
        self.run_main("run1")
        rc, out, _ = self.run_main("run2")
        self.assertEqual(rc, 0)
        self.assertIn("replaced play/memory symlink", out)
        self.assertEqual(
            (self.root / "play" / "memory").resolve(), (self.root / "playthroughs" / "run2" / "memory").resolve()
        )

    def test_refuses_real_directory_at_link(self):
        (self.root / "play" / "memory").mkdir()
        rc, _, err = self.run_main("run1")
        self.assertEqual(rc, 1)
        self.assertIn("not a symlink", err)

    def test_refuses_existing_nonempty_dest(self):
        self.run_main("run1")
        rc, _, err = self.run_main("run1")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)

    def test_custom_memory_dir_and_no_link(self):
        dest = self.root / "elsewhere" / "mem"
        rc, _, err = self.run_main("run1", "--memory-dir", str(dest), "--no-link")
        self.assertEqual(rc, 0, err)
        self.assertTrue((dest / "HANDOFF.md").exists())
        self.assertFalse((self.root / "play" / "memory").exists())

    def test_invalid_name(self):
        rc, _, _ = self.run_main("../evil")
        self.assertEqual(rc, 2)

    def test_fresh_memory_lints_clean(self):
        from bg3play.memory import lint

        self.run_main("run1")
        r = lint.lint(self.root / "play" / "memory")
        self.assertEqual(r.errors, [])


if __name__ == "__main__":
    unittest.main()
