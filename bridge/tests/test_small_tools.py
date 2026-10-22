"""Offline tests for four tools that had none: ability_preview, get_level_up_options, delete_marker,
end_combat_report (the mod op each sends, what it writes, its text).

Run: cd bridge && uv run python -m unittest discover -s tests -p test_small_tools.py
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest import mock

from bg3ai.tools import combat, core, progression, session, uitools
from test_turn_macros import GOB, P1, Base


class ReadTools(Base):
    def answer(self, op: str, reply: Any) -> None:
        real = self.g.rpc

        def rpc(o: str, args: dict | None = None, context: str = "server", timeout: float = 12.0) -> Any:
            if o == op:
                self.g.calls.append((o, dict(args or {}, _context=context)))
                return reply
            return real(o, args, context, timeout)

        self.g.rpc = rpc  # type: ignore[method-assign]

    def sent(self, op: str) -> list[dict]:
        return [a for o, a in self.g.calls if o == op]

    def test_ability_preview_asks_the_server_for_the_active_member_and_the_target(self):
        self.answer("state.preview", {"hit_chance": 65, "damage": "1d8+3", "in_range": True})
        out = uitools.ability_preview("Target_MainHandAttack", core.H.get(GOB))
        self.assertEqual(
            self.sent("state.preview"),
            [{"char": P1, "spell": "Target_MainHandAttack", "target": GOB, "_context": "server"}],
        )
        self.assertIn("65", out)
        self.assertIn("1d8+3", out)
        self.assertNotIn(GOB, out)  # the result names the target by its short id, never its uuid

    def test_get_level_up_options_reads_the_client_screen_for_who(self):
        self.answer("ui.level_up_options", {"class": "Wizard", "spells": ["Magic Missile", "Shield"]})
        out = progression.get_level_up_options()
        self.assertEqual(self.sent("ui.level_up_options"), [{"uuid": P1, "_context": "client"}])
        self.assertIn("Magic Missile", out)


class MemoryTools(Base):
    def test_delete_marker_removes_only_that_marker(self):
        markers = [{"id": "m1", "label": "chest", "pos": [1, 0, 1]}, {"id": "m2", "label": "door", "pos": [2, 0, 2]}]
        (core.MEMORY / "markers.json").write_text(json.dumps(markers))
        out = session.delete_marker("m1")
        self.assertTrue(out.startswith("deleted m1"), out)
        self.assertEqual([m["id"] for m in json.loads((core.MEMORY / "markers.json").read_text())], ["m2"])
        out = session.delete_marker("m9")  # unknown: nothing goes, and the result says so
        self.assertEqual(out.splitlines()[0], "FAILED: no marker m9 (yours: m2)")
        self.assertEqual([m["id"] for m in json.loads((core.MEMORY / "markers.json").read_text())], ["m2"])

    def test_end_combat_report_appends_to_the_session_log(self):
        (core.MEMORY / "sessions").mkdir()
        log = core.MEMORY / "sessions" / "S007.md"
        log.write_text("# S007\n")
        with mock.patch.dict(os.environ, {"BG3AI_SESSION": "S007"}):
            out = combat.end_combat_report("Bless first", "kill the archer sooner", "loot the camp")
        self.assertTrue(out.startswith("Logged to S007.md."), out)
        line = log.read_text().splitlines()[-1]
        self.assertRegex(
            line,
            r"^- \d\d:\d\d COMBAT REPORT worked: Bless first \| change: kill the archer sooner "
            r"\| next: loot the camp$",
        )

    def test_end_combat_report_without_a_session_log(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BG3AI_SESSION", None)
            out = combat.end_combat_report("a", "b", "c")
        self.assertTrue(out.startswith("Logged to (no session log)."), out)


if __name__ == "__main__":
    unittest.main()
