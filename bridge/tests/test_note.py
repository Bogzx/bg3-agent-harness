"""Offline tests: note() and note= on the action tools append memory lines to the session log at once (no
Read/Edit round trip while the party stands idle; S023-S026: 20-30 s per session of memory I/O in gaps).

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import re
import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai.tools import memory, batch, session  # noqa: E402

LINE = re.compile(r"^- \d{2}:\d{2} \[[\w -]+\] ")


class NoteLines(unittest.TestCase):
    def test_tags_times_and_dashes(self):
        lines = memory._note_lines("[quest] Got the Infernal Iron\n\n- 08:54 [wiki] puzzle order\nplain text",
                                   tag="npc")
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^- \d{2}:\d{2} \[quest\] Got the Infernal Iron$")
        self.assertEqual(lines[1], "- 08:54 [wiki] puzzle order")
        self.assertRegex(lines[2], r"^- \d{2}:\d{2} \[npc\] plain text$")
        self.assertEqual(memory._note_lines("08:56-09:00 [stuck] puzzle")[0], "- 08:56-09:00 [stuck] puzzle")
        self.assertEqual(memory._note_lines("  \n "), [])
        self.assertRegex(memory._note_lines("x", tag="]bad[")[0], r"\[bad\] x$")


class NoteTool(Base):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "sessions").mkdir()
        self.log = self.tmp / "sessions" / "S030.md"
        self.log.write_text("# Session S030\n\n## Log\n- 08:00 [note] start")  # no trailing newline
        env = mock.patch.dict(os.environ, {"BG3AI_SESSION": "S030"})
        env.start()
        self.addCleanup(env.stop)

    def body(self) -> list[str]:
        return self.log.read_text().splitlines()

    def test_note_appends_at_once(self):
        out = memory.note("[decision] Bought the key\nSkipped the trapped chest", tag="loot")
        self.assertIn("Noted 2 lines in S030.md", out)
        lines = self.body()
        self.assertEqual(lines[3], "- 08:00 [note] start")  # the missing newline was added, nothing joined
        self.assertRegex(lines[4], r"\[decision\] Bought the key$")
        self.assertRegex(lines[5], r"\[loot\] Skipped the trapped chest$")
        self.assertTrue(all(LINE.match(x) for x in lines[3:]))

    def test_empty_and_no_log(self):
        self.assertIn("Nothing noted", memory.note("  "))
        with mock.patch.object(session, "_session_log", return_value=None):
            self.assertIn("NOT SAVED", memory.note("x"))

    def test_note_on_queue_actions(self):
        batch.queue_actions('[{"tool":"wait","seconds":0}]', note="[plan] then the hatch")
        self.assertRegex(self.body()[-1], r"\[plan\] then the hatch$")

    def test_note_on_do_actions_queued(self):
        batch.do_actions('[{"tool":"wait","seconds":0}]', wait=False, note="[npc] Arabella at camp")
        self.assertRegex(self.body()[-1], r"\[npc\] Arabella at camp$")

    def test_note_is_always_loaded(self):
        self.assertIn("note", session.ALWAYS_LOAD)


if __name__ == "__main__":
    unittest.main()
