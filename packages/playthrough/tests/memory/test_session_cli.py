"""session.py's status and memlint commands and the CLI dispatch, in process (test_session.py has the rest)."""

import json
import os
import unittest
from unittest import mock

from bg3play.memory import session

from tests.memory._testutil import MemoryTestCase
from tests.memory.test_session import run_quiet


class StatusTest(MemoryTestCase):
    def test_missing_memory_dir(self):
        rc, out = run_quiet(session.cmd_status, self.tmp / "nope")
        self.assertEqual(rc, 1)
        self.assertIn("nope", out)

    def test_no_session_yet(self):
        rc, out = run_quiet(session.cmd_status, self.mem)
        self.assertEqual((rc, out.strip()), (0, "No session has been started yet."))

    def test_open_session_past_wrap_time(self):
        self.open_session(3, minutes_ago=55)
        with mock.patch.dict(os.environ, {"BG3_WRAP_MINUTES": "50", "BG3_SESSION_MINUTES": "60"}):
            rc, out = run_quiet(session.cmd_status, self.mem)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("S003 open; elapsed 55 min of 60"), out)
        self.assertIn("WRAP-UP NOW", out)

    def test_closed_session_has_no_flags(self):
        self.open_session(2, minutes_ago=55)
        st = self.state()
        st["open"] = False
        session.memlib.save_state(self.mem, st)
        _, out = run_quiet(session.cmd_status, self.mem)
        self.assertTrue(out.startswith("S002 closed;"), out)
        self.assertNotIn("WRAP-UP NOW", out)


class MemlintHookTest(MemoryTestCase):
    def hook(self, payload: object, mem=None) -> tuple[int, str]:
        return run_quiet(session.cmd_memlint, mem or self.mem, stdin=json.dumps(payload))

    def test_edit_of_a_file_with_errors_adds_context(self):
        self.open_session(1)
        self.append("npcs.md", "- untagged\n")
        rc, out = self.hook({"tool_input": {"file_path": str(self.mem / "npcs.md")}})
        self.assertEqual(rc, 0)
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertTrue(ctx["additionalContext"].startswith("Memory lint, npcs.md:"))

    def test_silent_without_a_path_or_memory(self):
        self.assertEqual(self.hook({}), (0, ""))
        self.assertEqual(self.hook({"tool_input": {"file_path": 3}}), (0, ""))
        self.assertEqual(self.hook({"tool_input": {}}, mem=self.tmp / "nope"), (0, ""))

    def test_broken_hook_input_is_no_input(self):
        rc, out = run_quiet(session.cmd_memlint, self.mem, stdin="{not json")
        self.assertEqual((rc, out), (0, ""))


class MainTest(MemoryTestCase):
    def main(self, *args: str, stdin: str = "{}") -> tuple[int, str]:
        return run_quiet(session.main, [*args, "--memory-dir", str(self.mem)], stdin=stdin)

    def test_dispatch(self):
        self.assertEqual(self.main("status")[1].strip(), "No session has been started yet.")
        rc, out = self.main("start")
        self.assertEqual(rc, 0)
        self.assertIn("session S001", out)
        self.assertEqual(self.main("memlint"), (0, ""))
        self.assertEqual(self.main("stop")[0], 0)
        self.assertEqual(self.main("clock")[0], 0)
        self.assertEqual(self.main("session-end")[0], 0)
        self.assertIn("S001 open", self.main("status")[1])


if __name__ == "__main__":
    unittest.main()
