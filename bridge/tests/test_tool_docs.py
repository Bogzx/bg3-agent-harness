"""Every registered MCP tool is documented in the agent's cheat-sheet (audit AGT-1: get_layout was registered but
missing from play/knowledge/tools.md, and the docs' tool counts had drifted: 63, ~45, 43, "all 83" vs 93)."""
from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_turn_macros import Base  # noqa: E402,F401  (sets BG3AI_RUNTIME before importing server)
from bg3ai import server  # noqa: E402

TOOLS_MD = Path(__file__).resolve().parents[2] / "play" / "knowledge" / "tools.md"


class ToolDocs(unittest.TestCase):
    def test_every_tool_is_in_tools_md(self):
        tools = asyncio.run(server.mcp.list_tools())
        doc = TOOLS_MD.read_text()
        missing = [t.name for t in tools if not re.search(r"`" + re.escape(t.name) + r"\(", doc)]
        self.assertEqual(missing, [], f"document these in {TOOLS_MD} as `name(args)`")

    def test_always_load_is_a_subset(self):
        tools = asyncio.run(server.mcp.list_tools())
        names = {t.name for t in tools}
        al = {t.name for t in tools if (getattr(t, "meta", None) or {}).get("anthropic/alwaysLoad")}
        self.assertTrue(al and al <= names and len(al) < len(names), (len(al), len(names)))


if __name__ == "__main__":
    unittest.main()
