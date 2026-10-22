"""Offline lint of what every client is served besides the rules (docs/BENCHMARK-BIAS.md): the tool descriptions
(golden/tools_list.json, which test_tools_golden keeps equal to the server's tools/list) and the server instructions.
They reach every model in every condition, so they carry no session anecdotes (Claude's history read as the model's
own), no client-only wording, and no argument that must be JSON inside a string (D15).

Run: cd bridge && uv run python -m unittest discover -s tests -p test_served_text.py
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai.tools import core

TOOLS = json.loads((Path(__file__).resolve().parent / "golden" / "tools_list.json").read_text(encoding="utf-8"))
# S023, claude-run-2 ...; a session log's path (memory/sessions/S012.md) is a file name, not a story
ANECDOTE_RE = re.compile(r"(?<!sessions/)\bS\d{3}\b|claude-run|runtime/|playthroughs/")
CLIENT_ONLY_RE = re.compile(
    r"mcp__|ToolSearch|alwaysLoad|end(s)? your turn|text-only|context compaction|Claude Code"
    r"|CLAUDE\.md",
    re.IGNORECASE,
)
# coaching that used to open the instructions (packages/harness/prompts/coaching/pace.md and efficiency.md have it)
COACHING_RE = re.compile(r"clicking ahead|keep planning|(wait_for|screenshot) only when", re.IGNORECASE)


def _types(schema: dict) -> set[str]:
    return {o.get("type") for o in schema.get("anyOf", [schema])}


class ServedText(unittest.TestCase):
    def test_tool_descriptions_tell_no_session_stories(self):
        for t in TOOLS:
            self.assertIsNone(ANECDOTE_RE.search(t["description"] or ""), t["name"])

    def test_tool_descriptions_and_instructions_are_client_neutral(self):
        for t in TOOLS:
            self.assertIsNone(CLIENT_ONLY_RE.search(t["description"] or ""), t["name"])
        self.assertIsNone(CLIENT_ONLY_RE.search(core.INSTRUCTIONS))
        self.assertIsNone(ANECDOTE_RE.search(core.INSTRUCTIONS))

    def test_instructions_carry_no_coaching(self):
        self.assertIsNone(COACHING_RE.search(core.INSTRUCTIONS))

    def test_an_argument_described_as_json_takes_the_real_value(self):
        # "plan: JSON like {...}" on a string-only schema made a model escape JSON inside a string
        for t in TOOLS:
            for arg, schema in t["input_schema"].get("properties", {}).items():
                if re.search(rf"\b{arg}: [^.]{{0,40}}\bJSON\b", t["description"] or ""):
                    self.assertTrue(_types(schema) & {"object", "array"}, f"{t['name']}({arg})")


if __name__ == "__main__":
    unittest.main()
