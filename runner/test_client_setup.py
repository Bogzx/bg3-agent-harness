#!/usr/bin/env python3
"""runner/client_setup.py: --profile resolution, the API key, the expected tool count.

python3 -m unittest runner/test_client_setup.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_setup as CS

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bridge" / "src"))
PI = str(REPO / "profiles" / "deepseek-flash.toml")
CLAUDE = str(REPO / "profiles" / "opus-5.5-claude-code.toml")
TOOLS = REPO / "tools" / "pi" / "conformance" / "tools.json"


class Resolve(unittest.TestCase):
    def test_without_profile(self):
        self.assertEqual(CS.resolve(None, None, None, None), CS.ClientChoice("claude", "opus", None))
        self.assertEqual(CS.resolve("codex", None, "gpt", "low"), CS.ClientChoice("codex", "gpt", "low"))
        with self.assertRaisesRegex(CS.SetupRefused, "needs --profile"):
            CS.resolve("pi", None, None, None)

    def test_pi_profile(self):
        c = CS.resolve(None, PI, None, None)
        self.assertEqual((c.client, c.model, c.effort), ("pi", "deepseek-flash", "high"))
        self.assertEqual(CS.resolve("pi", PI, "deepseek-flash", "low").effort, "low")  # --effort wins
        for args, why in (
            ((PI, "opus", None), "plays deepseek-flash"),
            ((PI, None, None, None, True), "--no-keeper"),
            ((PI, None, None, "other"), "--fix-model"),
            (("/nope.toml", None, None), "nope"),
        ):
            with self.assertRaisesRegex(CS.SetupRefused, why):
                CS.resolve(None, *args)
        with self.assertRaisesRegex(CS.SetupRefused, "pi profile"):
            CS.resolve("claude", PI, None, None)

    def test_claude_profile(self):
        c = CS.resolve(None, CLAUDE, None, None, keeper=True)
        self.assertEqual(c.client, "claude")


class ApiKey(unittest.TestCase):
    def setUp(self):
        self.prof = CS.resolve(None, PI, None, None).profile
        self.dotenv = Path(tempfile.mkdtemp(prefix="bg3ai-env-")) / ".env"
        self.addCleanup(lambda: (self.dotenv.unlink(missing_ok=True), self.dotenv.parent.rmdir()))

    def test_from_env_or_dotenv(self):
        self.assertEqual(CS.api_key_env(self.prof, {"DEEPSEEK_API_KEY": "x"}, self.dotenv), {})
        self.dotenv.write_text('# keys\nexport DEEPSEEK_API_KEY="sk-1"\nOTHER=2\n')
        self.assertEqual(CS.api_key_env(self.prof, {}, self.dotenv), {"DEEPSEEK_API_KEY": "sk-1"})
        self.assertEqual(CS.api_key_env(None, {}, self.dotenv), {})

    def test_missing_is_refused_without_the_value(self):
        self.dotenv.write_text("DEEPSEEK_API_KEY=\n")
        with self.assertRaisesRegex(CS.SetupRefused, "DEEPSEEK_API_KEY"):
            CS.api_key_env(self.prof, {}, self.dotenv)


class ToolCount(unittest.TestCase):
    def test_expected(self):
        names = CS.tool_names(TOOLS)
        self.assertEqual(CS.expected_tools(names, {}), len(names))
        self.assertEqual(CS.expected_tools(names, {"BG3AI_TOOLS_OFF": "wiki"}), len(names) - 2)
        from bg3ai import capabilities

        grouped = sum(len(g) for g in capabilities.GROUPS.values())
        self.assertEqual(CS.expected_tools(names, {"BG3AI_TOOLS": "game,saves"}), len(names) - grouped + 2)
        with self.assertRaises(ValueError):
            CS.expected_tools(names, {"BG3AI_TOOLS": "nope"})

    def test_problem(self):
        self.assertIsNone(CS.tool_count_problem(94, 94, {}))
        self.assertIsNone(CS.tool_count_problem(94, None, {}))
        self.assertEqual(
            CS.tool_count_problem(94, 76, {"BG3AI_TOOLS": "game"}),
            "the bg3 MCP server gave 94 tools, expected 76 (BG3AI_TOOLS=game)",
        )

    def test_dump_server_leaves_the_tool_selection_out(self):
        seen = {}

        def run(cmd, **kw):
            seen.update(cmd=cmd, env=kw["env"])

        import os

        os.environ["BG3AI_TOOLS"] = "game"
        self.addCleanup(os.environ.pop, "BG3AI_TOOLS", None)
        CS.dump_server(Path("/s.txt"), Path("/t.json"), REPO, run=run)
        self.assertNotIn("BG3AI_TOOLS", seen["env"])
        self.assertEqual(seen["cmd"][-4:], ["--system", "/s.txt", "--tools", "/t.json"])


if __name__ == "__main__":
    unittest.main()
