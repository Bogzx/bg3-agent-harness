"""The memory backend contract (memtools.MemoryBackend): the bridge imports no app code. The backend comes from the
one "bg3ai.memory" entry point (bg3play registers bg3play.memory.bridge_backend); with none the server serves no
memory tools and never imports bg3play.

Run: cd bridge && uv run python -m unittest discover -s tests -p test_memory_backend.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from unittest import mock

from bg3ai import capabilities, memtools
from bg3play.memory import bridge_backend

_CONFORMS: memtools.MemoryBackend = bridge_backend  # pyright checks bg3play's module against the contract

NO_BACKEND = """
import json, sys
from importlib import metadata
metadata.entry_points = lambda **kw: []  # no package registers bg3ai.memory
from bg3ai import memtools
from bg3ai.tools import core, session
names = {t.name for t in core.mcp._tool_manager.list_tools()}
print(json.dumps({"active": memtools.active(), "tools": sorted(names),
                  "bg3play": any(m.split(".")[0] == "bg3play" for m in sys.modules)}))
"""


class Discovery(unittest.TestCase):
    def test_the_installed_backend_is_bg3plays(self):
        self.assertIs(memtools.discover(), bridge_backend)
        self.assertIs(memtools.backend(), bridge_backend)

    def test_two_registered_backends_are_refused(self):
        eps = [metadata.EntryPoint(n, f"pkg_{n}.memory", memtools.ENTRY_POINT_GROUP) for n in ("a", "b")]
        with (
            mock.patch.object(metadata, "entry_points", lambda **kw: eps),
            self.assertRaisesRegex(RuntimeError, "more than one bg3ai.memory"),
        ):
            memtools.discover()

    def test_without_a_backend_the_server_serves_no_memory_tools_and_imports_no_app_code(self):
        with tempfile.TemporaryDirectory(prefix="bg3ai-test-") as rt:
            env = {**os.environ, "BG3AI_RUNTIME": rt}
            env.pop("BG3AI_TOOLS_OFF", None)
            env.pop("BG3AI_TOOLS", None)
            out = subprocess.run(
                [sys.executable, "-c", NO_BACKEND], env=env, capture_output=True, text=True, timeout=120, check=True
            )
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertFalse(got["active"])
        self.assertFalse(got["bg3play"])
        self.assertFalse(set(capabilities.GROUPS["memory"]) & set(got["tools"]))
        self.assertIn("get_status", got["tools"])

    def test_without_a_backend_no_session_opens(self):
        saved = memtools._backend
        self.addCleanup(memtools.configure, saved)
        memtools.configure(None)
        runner_env = {k: v for k, v in os.environ.items() if k not in ("BG3AI_SESSION", "BG3AI_FIX_NOTES")}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, runner_env, clear=True):
            (Path(d) / "PLAYTHROUGH.md").write_text("# run1\n")
            life = memtools.Lifecycle(lambda: Path(d), auto=True)
            life.ensure()  # a real-looking memory dir: with a backend this would open a session
            self.assertIsNone(life.opened)
        with self.assertRaisesRegex(RuntimeError, "no memory backend"):
            memtools.backend()


if __name__ == "__main__":
    unittest.main()
