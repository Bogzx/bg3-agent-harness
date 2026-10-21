"""Offline tests: the bg3 server's keeper mode (BG3AI_KEEPER_MODE=1, server.keeper_server + memtools.KeeperSandbox),
which runs the runner's memory keeper pass (runner/memory_keeper.py) for any client: the tool list, the task as
instructions, and the keeper's write rules enforced server-side.

Run: cd bridge && uv run python -m unittest tests/test_keeper_mode.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
from test_memtools import make_tree  # noqa: E402

from mcp.server.mcpserver import MCPServer
from bg3ai import memtools, server  # noqa: E402
from bg3ai.tools import core  # noqa: E402
from bg3ai.memtools import Refused  # noqa: E402

SECTION = "## From the logs (memory keeper)"
WRITABLE = ["objectives.md", "quests.md", "npcs.md", "HANDOFF.md", "CHARACTER.md", "chapters/"]


class KeeperBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bg3keeper-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.play, self.mem, self.k = make_tree(self.root)
        (self.mem / "sessions").mkdir(exist_ok=True)
        (self.mem / "sessions" / "S001.md").write_text("# S001\n- found the key\n")
        (self.mem / "HANDOFF.md").write_text("# Handoff\nSave to load: Quick-1\n")
        self.sb = memtools.KeeperSandbox(lambda: self.mem, lambda: self.k, WRITABLE, SECTION)

    def refused(self, fn, *a) -> str:
        with self.assertRaises(Refused) as cm:
            fn(self.sb, *a)
        return str(cm.exception)


class Sandbox(KeeperBase):
    def test_writable_files_and_dirs(self):
        self.assertIn("memory/objectives.md (1 lines)", memtools.write_file(self.sb, "memory/objectives.md", "# O\n"))
        self.assertIn("chapters/C01", memtools.write_file(self.sb, "memory/chapters/C01_S001-S001_x.md", "# C\n"))

    def test_sessions_read_only(self):
        self.assertIn("found the key", memtools.read_file(self.sb, "memory/sessions/S001.md"))
        self.assertIn("read-only", self.refused(memtools.append_file, "memory/sessions/S001.md", "- more"))
        self.assertIn("read-only", self.refused(memtools.write_file, "memory/sessions/S099.md", "# new"))
        self.assertEqual((self.mem / "sessions" / "S001.md").read_text(), "# S001\n- found the key\n")

    def test_knowledge_read_only(self):
        self.assertIn("Focus fire", memtools.read_file(self.sb, "knowledge/playbooks/combat.md"))
        self.assertIn("read-only", self.refused(memtools.write_file, "knowledge/tools.md", "x"))

    def test_other_memory_files_refused(self):
        msg = self.refused(memtools.write_file, "memory/notes.md", "x")
        self.assertIn("may write only", msg)
        self.assertIn("objectives.md", msg)
        self.refused(memtools.write_file, "memory/.gitattributes", "x")   # the player's rules still apply

    def test_handoff_only_its_last_section(self):
        out = memtools.append_file(self.sb, "memory/HANDOFF.md", f"\n{SECTION}\n- door key in the crypt [seen]")
        self.assertIn("Appended", out)
        out = memtools.edit_file(self.sb, "memory/HANDOFF.md", "door key", "iron key")
        self.assertIn("Edited", out)
        self.assertIn("player's", self.refused(memtools.edit_file, "memory/HANDOFF.md", "Quick-1", "Quick-2"))
        self.assertIn("player's", self.refused(memtools.write_file, "memory/HANDOFF.md", f"# Handoff\n\n{SECTION}\n"))
        text = (self.mem / "HANDOFF.md").read_text()
        self.assertTrue(text.startswith("# Handoff\nSave to load: Quick-1\n"))
        self.assertIn("iron key", text)

    def test_nothing_writable_without_env(self):
        sb = memtools.KeeperSandbox.from_env(lambda: self.mem, lambda: self.k, env={})
        with self.assertRaises(Refused):
            memtools.write_file(sb, "memory/objectives.md", "# O\n")
        sb = memtools.KeeperSandbox.from_env(lambda: self.mem, lambda: self.k, env={
            "BG3AI_KEEPER_WRITABLE": "objectives.md,chapters/", "BG3AI_KEEPER_HANDOFF_SECTION": SECTION})
        self.assertEqual(sb.writable, ["objectives.md", "chapters/"])
        self.assertEqual(sb.handoff_section, SECTION)

    def test_player_sandbox_unchanged(self):
        sb = memtools.Sandbox(lambda: self.mem, lambda: self.k)
        self.assertIn("Edited", memtools.edit_file(sb, "memory/HANDOFF.md", "Quick-1", "Quick-2"))
        self.assertIn("Appended", memtools.append_file(sb, "memory/sessions/S001.md", "- more"))


class Server(KeeperBase):
    def setUp(self):
        super().setUp()
        self.srv = server.keeper_server("KEEPER TASK: update objectives.md", self.sb)

    def call(self, name: str, **args) -> str:
        res = asyncio.run(self.srv.call_tool(name, args))
        return "".join(getattr(c, "text", "") for c in res.content)

    def test_tool_list_is_the_memory_tools(self):
        names = [t.name for t in asyncio.run(self.srv.list_tools())]
        self.assertEqual(sorted(names), sorted(server.KEEPER_TOOLS))
        for game_tool in ("get_status", "move_to", "note", "session_start", "end_session"):
            self.assertNotIn(game_tool, names)
        self.assertEqual(self.srv.instructions, "KEEPER TASK: update objectives.md")

    def test_calls_and_refusals_are_text(self):
        self.assertIn("lint", self.call("memory_write", path="memory/objectives.md", content="# Objectives\n"))
        self.assertTrue(self.call("memory_append", path="memory/sessions/S001.md", text="- x").startswith("REFUSED"))
        self.assertTrue(self.call("memory_write", path="knowledge/tools.md", content="x").startswith("REFUSED"))
        self.assertIn("found the key", self.call("memory_search", query="key", path="memory/sessions"))
        self.assertIn("sessions/", self.call("memory_list"))

    def test_task_from_file(self):
        p = self.root / "task.md"
        p.write_text("THE TASK\n")
        self.assertEqual(server.keeper_task({"BG3AI_KEEPER_TASK": str(p)}), "THE TASK")
        self.assertEqual(server.keeper_task({"BG3AI_KEEPER_TASK": str(self.root / "missing")}),
                         server.KEEPER_DEFAULT_TASK)
        self.assertEqual(server.keeper_task({}), server.KEEPER_DEFAULT_TASK)

    def test_main_serves_the_keeper_server(self):
        import os
        from unittest import mock
        served = {}
        with mock.patch.dict(os.environ, {"BG3AI_KEEPER_MODE": "1", "BG3AI_KEEPER_WRITABLE": "objectives.md"}), \
                mock.patch.object(MCPServer, "run", lambda srv, transport, **kw: served.update(srv=srv)):
            server.main([])
        self.assertIsNot(served["srv"], core.mcp)
        self.assertEqual(sorted(t.name for t in asyncio.run(served["srv"].list_tools())), sorted(server.KEEPER_TOOLS))


if __name__ == "__main__":
    unittest.main()
