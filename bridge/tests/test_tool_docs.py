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
from bg3ai.tools import core  # noqa: E402

TOOLS_MD = Path(__file__).resolve().parents[2] / "play" / "knowledge" / "tools.md"


class ToolDocs(unittest.TestCase):
    def test_every_tool_is_in_tools_md(self):
        tools = asyncio.run(core.mcp.list_tools())
        doc = TOOLS_MD.read_text()
        missing = [t.name for t in tools if not re.search(r"`" + re.escape(t.name) + r"\(", doc)]
        self.assertEqual(missing, [], f"document these in {TOOLS_MD} as `name(args)`")

    def test_always_load_is_a_subset(self):
        tools = asyncio.run(core.mcp.list_tools())
        names = {t.name for t in tools}
        al = {t.name for t in tools if (getattr(t, "meta", None) or {}).get("anthropic/alwaysLoad")}
        self.assertTrue(al and al <= names and len(al) < len(names), (len(al), len(names)))

    def test_tool_text_names_no_playthrough_specifics(self):
        """Fairness audit C6/C7/C25 (user, 2026-10-18): harness-news.md, tools.md and the tool docstrings describe tool
        behaviour only. Names from claude-run-2 (a puzzle's answer, a route to a boss, an item pairing) are coaching."""
        names = re.compile(r"Mausoleum|Moonrise Towers|Yurgir|Silent Library|Riddle of the Night|Teachings of Loss|"
                           r"Syringe|Moonlantern|\bOrin\b|Yoonce|Avatar form|Cazador|Viconia|Keene|The Watch|Wyrm|"
                           r"Rivington|Shadow-Cursed", re.I)
        news = TOOLS_MD.parent / "harness-news.md"
        texts = {"tools.md": TOOLS_MD.read_text(), "harness-news.md": news.read_text()}
        for t in asyncio.run(core.mcp.list_tools()):
            texts[t.name] = t.description or ""
        hits = {k: sorted(set(names.findall(v))) for k, v in texts.items() if names.search(v)}
        self.assertEqual(hits, {})

    def test_agent_text_gives_no_puzzle_answers_or_oath_guard(self):
        """User, 2026-10-18: play/knowledge keeps deadlines, points of no return and game-over traps, never a
        puzzle's or an encounter's answer; and the Oath of Devotion guard is gone, so no text describes it."""
        answers = re.compile(r"dark moons|lit disc|glyph rows|put out \*\*every\*\* light|copies spawn weak|"
                             r"floor mosaic before the bowl|hidden clause|farthest cells first|"
                             r"outside the\s+room \(more than 18 m|crest that disarms|sluice chamber when the counter\s+"
                             r"ends, or|put all damage on the|Watch disabled", re.I)
        oath = re.compile(r"Oath of Devotion guard|Oath of Devotion: |paladin's blow that could kill|"
                          r"force=true strikes anyway|Non-Lethal Attacks on skips", re.I)
        play = TOOLS_MD.parent.parent
        texts = {str(f.relative_to(play)): f.read_text() for f in [play / "CLAUDE.md", *play.glob("knowledge/**/*.md")]}
        for t in asyncio.run(core.mcp.list_tools()):
            texts[t.name] = t.description or ""
        hits = {k: sorted(set(answers.findall(v) + oath.findall(v))) for k, v in texts.items()
                if answers.search(v) or oath.search(v)}
        self.assertEqual(hits, {})

if __name__ == "__main__":
    unittest.main()
