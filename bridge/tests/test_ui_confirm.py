"""Offline tests (S005 triage): roll_check raised 'Error executing tool roll_check' x3 (_BatchState had no
.get), the confirm tool for Yes/No boxes, long_rest(confirm=false), and the POPUP line's answer hint.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import render, server  # noqa: E402
from bg3ai.game import JobResult  # noqa: E402


class RollCheck(Base):
    def test_roll_then_continue_in_one_call(self):
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append((op, dict(args)))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": True, "result": 15}})
            return JobResult("done", {"screen": "dialogue"})
        self.g.run_job = run_job
        out = server.roll_check()
        self.assertNotIn("Error", out)
        self.assertEqual([c[0] for c in calls], ["ui.roll", "ui.roll"])  # rolled, then Continue

    def test_reroll_on_fail(self):
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": False}})
            return JobResult("done", {"roll": {"phase": "result", "success": True}})
        self.g.run_job = run_job
        server.roll_check(reroll_on_fail=True)
        self.assertEqual(calls[1], {"reroll": True})
        self.assertEqual(len(calls), 3)

    def test_batch_state_get(self):
        self.assertEqual(server._batch.get("nope", 7), 7)
        self.assertIn(server._batch.get("last"), ("done", "failed", "interrupted", "running", None))


class Confirm(Base):
    def test_confirm_sends_ui_confirm(self):
        seen = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            seen.append((op, args, context))
            return JobResult("done", {"pressed": "Yes", "buttons": ["Yes", "No"]})
        self.g.run_job = run_job
        out = server.confirm("yes")
        self.assertEqual(seen, [("ui.confirm", {"answer": "yes"}, "client")])
        self.assertIn("pressed=Yes", out)
        self.assertIn("confirm", server.ALWAYS_LOAD)

    def test_long_rest_confirm_flag(self):
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(args) or JobResult("done", {"rest": "full"}))
        server.long_rest()
        server.long_rest(confirm=False)
        self.assertEqual(seen, [{}, {"confirm": False}])

    def test_popup_line_says_how_to_answer(self):
        lines = render.render_ui({"screen": "popup", "popup": {"title": "[ForceUpdate]", "buttons": ["Yes", "No"],
                                                               "decision": True}}, server.H)
        self.assertTrue(any(line.startswith("POPUP") and "confirm(" in line for line in lines), lines)
        lines = render.render_ui({"screen": "popup", "popup": {"title": "Info", "buttons": ["OK"],
                                                               "dismissable": True}}, server.H)
        self.assertFalse(any("confirm(" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
