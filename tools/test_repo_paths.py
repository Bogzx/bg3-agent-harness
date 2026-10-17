#!/usr/bin/env python3
"""Tests for tools/repo_paths.py and the log tools' default runtime.  Run: python3 -m unittest tools/test_repo_paths.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repo_paths as RP  # noqa: E402

# module: the attribute that holds its default runtime path (all must come from repo_paths.runtime_dir)
DEFAULTS = {"vis/overlap": "SESSIONS", "vis/series_report": "sessions_root()", "logs/review_session": "RUNTIME",
            "logs/fights": "SESSIONS", "logs/mine_session": "SESS", "logs/live_tests": "repo_paths.runtime_dir()",
            "logs/session_watch": "runtime_dir()", "logs/story_stalls": "RUNTIME", "logs/context_cost": "RUNTIME",
            "logs/context_model": "RUNTIME", "logs/gap_mix": "RUNTIME", "logs/hand_clicks": "RUNTIME",
            "logs/tool_latency": "RUNTIME", "logs/trim_estimate": "RUNTIME", "logs/think_by_situation": "RUNTIME"}


class RuntimeDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main, self.wt = Path(tmp.name) / "main", Path(tmp.name) / "wt"
        (self.main / ".git/worktrees/wt").mkdir(parents=True)
        self.wt.mkdir()
        (self.wt / ".git").write_text(f"gitdir: {self.main}/.git/worktrees/wt\n")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BG3AI_RUNTIME", None)

    def test_worktree_reads_the_main_trees_runtime(self):
        (self.main / "runtime/sessions").mkdir(parents=True)
        self.assertEqual(RP.main_tree(self.wt), self.main)
        self.assertEqual(RP.runtime_dir(self.wt), self.main / "runtime")
        self.assertEqual(RP.runtime_dir(self.main), self.main / "runtime")

    def test_own_runtime_with_sessions_wins(self):
        (self.main / "runtime/sessions").mkdir(parents=True)
        (self.wt / "runtime/sessions").mkdir(parents=True)
        self.assertEqual(RP.runtime_dir(self.wt), self.wt / "runtime")

    def test_env_wins_like_the_runner(self):
        (self.main / "runtime/sessions").mkdir(parents=True)
        os.environ["BG3AI_RUNTIME"] = "/elsewhere/rt"
        self.assertEqual(RP.runtime_dir(self.wt), Path("/elsewhere/rt"))

    def test_no_sessions_anywhere_keeps_the_own_path(self):
        self.assertEqual(RP.runtime_dir(self.wt), self.wt / "runtime")

    def test_every_log_tool_defaults_to_runtime_dir(self):
        """Each tool's default (read at import) follows $BG3AI_RUNTIME: series_report found 0 runs from a worktree."""
        rt = Path(tempfile.mkdtemp(prefix="bg3ai-test-rt-"))
        self.addCleanup(rt.rmdir)
        code = ("import json, sys, importlib; sys.path[:0] = [sys.argv[1] + '/vis', sys.argv[1] + '/logs']; out = {}\n"
                "for m, attr in json.loads(sys.argv[2]).items():\n"
                "    mod = importlib.import_module(m.split('/')[1])\n"
                "    out[m] = str(eval(attr, vars(mod)))\n"
                "print(json.dumps(out))")
        r = subprocess.run([sys.executable, "-c", code, str(HERE), json.dumps(DEFAULTS)], capture_output=True,
                           check=False, text=True, env={**os.environ, "BG3AI_RUNTIME": str(rt)}, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        for m, path in json.loads(r.stdout).items():
            self.assertTrue(path.startswith(str(rt)), f"{m}: {path}")


if __name__ == "__main__":
    unittest.main()
