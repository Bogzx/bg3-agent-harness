"""Offline tests: bg3ai.tool_classes (the read tools, what a failed result looks like) and the bg3 server sending a
failed text result with is_error set, so a client's events mark it (the evidence video draws it as a failure).

Run: cd bridge && uv run python -m unittest tests/test_tool_classes.py
"""

from __future__ import annotations

import asyncio
import unittest

from bg3ai import tool_classes
from bg3ai.tools import core
from test_turn_macros import Base  # noqa: F401  (a test runtime for the server)


class FailedResults(unittest.TestCase):
    def test_the_failure_prefixes(self):
        for text in (
            "FAILED: no objective o1 now",
            "NOT READY: loading",
            "REFUSED: past the deadline",
            "CHAIN STOPPED after 0 of 4: Lae'zel: not played",
        ):
            self.assertTrue(tool_classes.failed(text), text)
        for text in ("done", "TURN e384 Spiritual Weapon\nCHAIN STOPPED after 1 of 2: x", ""):
            self.assertFalse(tool_classes.failed(text), text)

    def test_the_read_tools_are_the_reporting_layers(self):
        from bg3ai.tools import reporting

        self.assertIs(reporting._READ_TOOLS, tool_classes.READ_TOOLS)
        self.assertIn("get_status", tool_classes.READ_TOOLS)
        self.assertNotIn("combat_turn", tool_classes.READ_TOOLS)

    def test_the_server_sends_a_failed_result_as_an_error(self):
        srv = core.Server("t")

        @srv.tool()
        def act(ok: bool) -> str:
            return "done" if ok else "FAILED: no path"

        good = asyncio.run(srv.call_tool("act", {"ok": True}))
        bad = asyncio.run(srv.call_tool("act", {"ok": False}))
        self.assertFalse(good.is_error)
        self.assertTrue(bad.is_error)
        self.assertEqual(bad.content[0].text, "FAILED: no path")
        self.assertIsInstance(core.mcp, core.Server)


if __name__ == "__main__":
    unittest.main()
