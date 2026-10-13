"""The hooks' git calls in the memory repo must not run anything the repo itself names (FAIR-1).

    python3 -m unittest discover -s tools/memory -p 'test_git_safety.py'
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lint as L  # noqa: E402
import memlib as M  # noqa: E402
import session as S  # noqa: E402
from _testutil import MemoryTestCase  # noqa: E402


class GitSafetyTest(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.pwned = self.tmp / "pwned"
        self.pwned.mkdir()
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()
        (self.elsewhere / "secret.txt").write_text("host file\n")
        mark = lambda name: f"sh -c 'touch {self.pwned}/{name}; cat'"  # noqa: E731
        with (self.mem / ".git" / "config").open("a") as f:
            f.write(f"[core]\n\tfsmonitor = \"sh -c 'touch {self.pwned}/fsmonitor'\"\n"
                    f"[filter \"evil\"]\n\tclean = \"{mark('clean')}\"\n\tsmudge = \"{mark('smudge')}\"\n"
                    f"\trequired = true\n"
                    f"[diff \"evil\"]\n\ttextconv = \"{mark('textconv')}\"\n"
                    f"[diff]\n\texternal = \"sh -c 'touch {self.pwned}/extdiff'\"\n"
                    f"[log]\n\tshowSignature = true\n[gpg]\n\tprogram = \"sh -c 'touch {self.pwned}/gpg'\"\n")
        hooks = self.mem / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "commit-msg", "post-commit", "post-index-change", "reference-transaction"):
            (hooks / name).write_text(f"#!/bin/sh\ntouch {self.pwned}/{name}\n")
            (hooks / name).chmod(0o755)
        (self.mem / ".gitattributes").write_text("* filter=evil diff=evil\n")

    def assert_nothing_ran(self):
        self.assertEqual(sorted(p.name for p in self.pwned.iterdir()), [])

    def test_commit_lint_and_status_run_nothing_from_the_repo(self):
        self.append("npcs.md", "\n- Zevlor [seen] (S001)\n")
        h = M.git_commit_all(self.mem, "S001")
        self.assertTrue(h)
        self.assertEqual(self.git_log()[0], "S001")
        self.assertIn("npcs.md", M.git(self.mem, "show", "--stat", "--format=", "HEAD").stdout)
        self.append("npcs.md", "\n- Alfira [seen] (S002)\n")
        M.last_touched_session(self.mem, "npcs.md")
        L.lint(self.mem, "any", 2)
        M.git(self.mem, "diff", "HEAD", "--", "npcs.md")
        M.git(self.mem, "log", "-1", "--format=%s")
        self.assert_nothing_ran()

    def test_core_worktree_cannot_point_elsewhere(self):
        M.git(self.mem, "config", "core.worktree", str(self.elsewhere))
        self.append("npcs.md", "\n- Zevlor [seen] (S001)\n")
        M.git_commit_all(self.mem, "S001")
        files = M.git(self.mem, "show", "--stat", "--format=", "HEAD").stdout
        self.assertIn("npcs.md", files)
        self.assertNotIn("secret.txt", files)   # host files never get committed into the agent's memory

    def test_session_end_commits_without_hooks(self):
        self.open_session(1)
        self.write("HANDOFF.md", self.good_handoff(1))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(["end", "--memory-dir", str(self.mem), "--human-override"]), 0)
        self.assertEqual(self.git_log()[0], "S001")
        self.assert_nothing_ran()

    def test_driver_overrides_cover_configured_drivers(self):
        argv = M.git_argv(self.mem, "status")
        self.assertIn("filter.evil.clean=", argv)
        self.assertIn("filter.evil.required=false", argv)
        self.assertIn("--no-ext-diff", M.git_argv(self.mem, "diff", "HEAD"))
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn(f"--work-tree={self.mem.resolve()}", argv)


if __name__ == "__main__":
    unittest.main()
