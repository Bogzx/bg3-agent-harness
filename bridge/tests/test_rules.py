"""Offline tests: the assembled rules (packages/harness/prompts) served to any MCP client (instructions, the first
briefing of a session, the rules prompt/resource); and the server-side stop after deadline + grace (rules.py).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_memtools as TM  # (sets BG3AI_RUNTIME before importing server)
from test_turn_macros import Base

from bg3ai import memtools, rules  # noqa: E402
from bg3ai.tools import core, world_tools, memory  # noqa: E402


class Served(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ("BG3AI_RULES_PATH", "BG3AI_PROMPT_CONDITION"):
            os.environ.pop(k, None)

    def test_the_default_is_the_assembled_rules(self):
        text = rules.load()
        self.assertEqual(text, rules.prompts().assemble("rules").text.strip())
        self.assertTrue(text.startswith("# You are playing Baldur's Gate 3"))
        self.assertIn("## Fairness and sandbox (hard rules)", text)
        self.assertNotIn("## Session lifecycle", text)          # coaching: not in the rules condition
        for client_only in ("mcp__bg3__", "ToolSearch", "Claude"):
            self.assertNotIn(client_only, text)

    def test_condition_and_file_overrides(self):
        coached = rules.prompts().assemble("rules+coaching").text.strip()
        self.assertEqual(rules.load(condition="rules+coaching"), coached)
        os.environ["BG3AI_PROMPT_CONDITION"] = "rules+coaching"
        self.assertEqual(rules.load(), coached)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "rules.md"
            f.write_text("MY RULES\n", encoding="utf-8")
            os.environ["BG3AI_RULES_PATH"] = str(f)
            self.assertEqual(rules.load(), "MY RULES")

    def test_every_tool_the_rules_name_is_a_server_tool(self):
        # bench.md §2.6 test c against the live registry (the prompts' own lint uses tools/pi's dump of it)
        import asyncio
        import re
        tools = {t.name for t in asyncio.run(core.mcp.list_tools())}
        named = {m.group(1) for m in re.finditer(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)", rules.load())}
        events = {"job_finished", "queue_done", "queue_interrupted", "my_turn", "in_combat", "hp_below",
                  "item_present", "target_visible", "in_dialogue", "dialogue_line"}
        self.assertEqual(named - tools - events, set())

    def test_missing_file_serves_nothing(self):
        self.assertEqual(rules.load(rules.REPO / "no-such-CLAUDE.md"), "")
        self.assertEqual(rules.instructions("base", ""), "base")

    def test_instructions_carry_the_rules_unless_turned_off(self):
        with mock.patch.dict(os.environ, {"BG3AI_SERVE_RULES": "1"}):
            self.assertIn("=== RULES ===\nR", rules.instructions("base", "R"))
        with mock.patch.dict(os.environ, {"BG3AI_SERVE_RULES": "0"}):
            self.assertEqual(rules.instructions("base", "R"), "base")

    def test_server_instructions(self):
        self.assertTrue(core.mcp.instructions.startswith(core.INSTRUCTIONS))
        if rules.serve_enabled():
            self.assertIn("## Fairness and sandbox", core.mcp.instructions)

    def test_once_per_session(self):
        once = rules.Once("RULES TEXT")
        with mock.patch.dict(os.environ, {"BG3AI_SERVE_RULES": "1"}):
            self.assertIn("RULES TEXT", once.wrap("brief", "1"))
            self.assertEqual(once.wrap("brief", "1"), "brief")       # a repeat (after compaction): briefing only
            self.assertIn("RULES TEXT", once.wrap("brief", "1", force=True))
            self.assertIn("RULES TEXT", once.wrap("brief", "2"))     # the next session on a long-lived server
        with mock.patch.dict(os.environ, {"BG3AI_SERVE_RULES": "0"}):
            self.assertEqual(rules.Once("R").wrap("brief", "1"), "brief")
            self.assertIn("R", rules.Once("R").wrap("brief", "1", force=True))   # asked for explicitly


class StopPolicy(unittest.TestCase):
    D = 1_000_000.0

    def test_before_the_grace_ends_everything_runs(self):
        self.assertEqual(rules.stop_refusal("move_to", self.D, now=self.D + 299, grace=300), "")
        self.assertEqual(rules.stop_refusal("move_to", 0.0, now=self.D * 2), "")   # no deadline: manual session

    def test_after_the_grace_game_actions_are_refused(self):
        out = rules.stop_refusal("move_to", self.D, now=self.D + 400, grace=300)
        self.assertTrue(out.startswith("REFUSED: the session ended 6 min ago"))
        self.assertIn("call end_session now", out)

    def test_reads_memory_and_wrapup_still_work(self):
        for tool in ("memory_write", "note", "save_game", "end_session", "session_start", "get_status"):
            self.assertEqual(rules.stop_refusal(tool, self.D, {"get_status"}, now=self.D + 3600, grace=300), "", tool)


class ServerBase(Base):
    """As test_memtools.ServerMemoryTools: a scratch playthrough memory, no runner env, manual-mode lifecycle."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)   # restored after the test
        env.start()
        self.addCleanup(env.stop)
        for k in TM.RUNNER_ENV:
            os.environ.pop(k, None)
        os.environ["BG3_SNAPSHOTS"] = "0"
        root = Path(tempfile.mkdtemp(prefix="bg3rules-"))
        self.addCleanup(shutil.rmtree, root, True)
        _play, mem, k = TM.make_tree(root)
        saved = (core.MEMORY, core.KNOWLEDGE, core.LIFE, core.DEADLINE)
        self.addCleanup(self._restore_mem, saved)
        core.MEMORY, core.KNOWLEDGE, core.DEADLINE = mem, k, 0.0
        core.LIFE = memtools.Lifecycle(lambda: core.MEMORY, auto=True)
        core._clock_shown.update(at=0.0, stuck=None)

    def _restore_mem(self, saved) -> None:
        core.MEMORY, core.KNOWLEDGE, core.LIFE, core.DEADLINE = saved


class ServerRules(ServerBase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["BG3AI_SERVE_RULES"] = "1"
        saved = core.RULES
        core.RULES = rules.Once("RULES TEXT")
        self.addCleanup(setattr, core, "RULES", saved)

    def test_first_session_start_carries_the_rules(self):
        out = memory.session_start()
        self.assertTrue(out.startswith("=== RULES"))
        self.assertIn("session S001", out)
        again = memory.session_start()
        self.assertNotIn("RULES TEXT", again)
        self.assertIn("RULES TEXT", memory.session_start(rules=True))

    def test_manual_first_call_carries_rules_and_briefing(self):
        out = memory.memory_list("knowledge")
        self.assertIn("RULES TEXT", out)
        self.assertIn("session S001", out)
        self.assertNotIn("RULES TEXT", memory.session_start())   # already shown this session

    def test_off_for_claude_code(self):
        os.environ["BG3AI_SERVE_RULES"] = "0"
        self.assertNotIn("RULES TEXT", memory.session_start())

    def test_prompt_and_resource(self):
        self.assertEqual(memory.rules_prompt(), "RULES TEXT")
        self.assertEqual(memory.rules_resource(), "RULES TEXT")


class ServerStop(ServerBase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["BG3AI_SESSION"] = "S001"   # runner mode: nothing opens a session
        p = mock.patch.object(core, "_stuck", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def test_past_deadline_plus_grace(self):
        core.DEADLINE = time.time() - rules.STOP_GRACE_S - 60
        out = world_tools.move_to("e1")
        self.assertTrue(out.startswith("REFUSED: the session ended"), out)
        self.assertIn("end_session", out)
        self.assertFalse(memory.memory_list("knowledge").startswith("REFUSED"))
        self.assertFalse(memory.note("[note] late").startswith("REFUSED"))

    def test_within_grace_the_old_refusal(self):
        core.DEADLINE = time.time() - 30   # over, inside the grace: _act's own "session is over" text
        out = world_tools.move_to("e1")
        self.assertNotIn("call end_session now", out)


if __name__ == "__main__":
    unittest.main()
