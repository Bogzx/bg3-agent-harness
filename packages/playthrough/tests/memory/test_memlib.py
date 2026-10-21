import os
import unittest
from pathlib import Path
from unittest import mock

from bg3play.memory import memlib

from tests.memory._testutil import MemoryTestCase


class ResolveTest(unittest.TestCase):
    def test_flag_then_env_then_project_then_cwd(self):
        env = {"BG3_MEMORY_DIR": "/m", "CLAUDE_PROJECT_DIR": "/p"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(memlib.resolve_memory_dir("/a"), Path("/a"))
            self.assertEqual(memlib.resolve_memory_dir(None), Path("/m"))
        with mock.patch.dict(os.environ, {"BG3_MEMORY_DIR": "", "CLAUDE_PROJECT_DIR": "/p"}):
            self.assertEqual(memlib.resolve_memory_dir(None), Path("/p/memory"))
        with mock.patch.dict(os.environ, {"BG3_MEMORY_DIR": "", "CLAUDE_PROJECT_DIR": ""}):
            self.assertEqual(memlib.resolve_memory_dir(None), Path("memory"))


class StateAndGitTest(MemoryTestCase):
    def test_broken_state_file_is_no_state(self):
        (self.mem / memlib.STATE_FILE).write_text("{oops")
        self.assertEqual(memlib.load_state(self.mem), {})

    def test_last_touched_session(self):
        self.assertEqual(memlib.last_touched_session(self.mem, "npcs.md"), 0)  # the "S000 init" commit
        self.assertIsNone(memlib.last_touched_session(self.mem, "no-such-file.md"))  # no commit touched it
        self.append("npcs.md", "- Halsin [seen] (S002)\n")
        self.assertEqual(memlib.last_touched_session(self.mem, "npcs.md"), -1)  # uncommitted: the open session's
        self.assertIsNotNone(memlib.git_commit_all(self.mem, "S002 end: notes"))
        self.assertEqual(memlib.last_touched_session(self.mem, "npcs.md"), 2)
        self.assertIsNone(memlib.git_commit_all(self.mem, "S003 end: nothing changed"))

    def test_no_git_history_is_unknown(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(memlib.last_touched_session(plain, "npcs.md"))

    def test_reusable_session_needs_its_log(self):
        self.open_session(4)
        (self.mem / "sessions" / "S004.md").unlink()
        self.assertIsNone(memlib.reusable_session(self.mem))


if __name__ == "__main__":
    unittest.main()
