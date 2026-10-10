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

    def test_reroll_shows_both_rolls_plainly(self):
        # S013-S026: the reroll returned the same failed roll twice and the outcome was a missing key.
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": False, "result": 4, "bonus": 7,
                                                   "dc": 21, "skill": "Persuasion", "inspiration": 4},
                                          "phases": "WaitForStart@0 StartRoll@300 ResultReady@6500"})
            if len(calls) == 2:
                return JobResult("done", {"roll": {"phase": "result", "success": True, "result": 17, "bonus": 7,
                                                   "dc": 21, "skill": "Persuasion", "inspiration": 3},
                                          "rerolled": True})
            return JobResult("done", {"screen": "dialogue"})
        self.g.run_job = run_job
        out = server.roll_check(reroll_on_fail=True)
        self.assertIn("roll: Persuasion DC 21: d20 4 + 7 = 11 → FAILED (inspiration 4)", out)
        self.assertIn("reroll (1 Inspiration): Persuasion DC 21: d20 17 + 7 = 24 → SUCCESS", out)
        self.assertNotIn("phases", out)
        self.assertEqual(out.count("⏱"), min(out.count("⏱"), 1))

    def test_reroll_ignored_and_no_inspiration(self):
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": False, "result": 2, "bonus": 1,
                                                   "inspiration": 2}})
            if len(calls) == 2:
                return JobResult("done", {"roll": {"phase": "result", "success": False, "result": 2},
                                          "reroll_ignored": True})
            return JobResult("done", {"screen": "game"})
        self.g.run_job = run_job
        out = server.roll_check(reroll_on_fail=True)
        self.assertIn("the game kept the first result", out)
        self.assertEqual(len(calls), 3)
        calls.clear()

        def run_job2(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": False, "result": 2, "inspiration": 0}})
            return JobResult("done", {"screen": "game"})
        self.g.run_job = run_job2
        out = server.roll_check(reroll_on_fail=True)
        self.assertIn("no Inspiration points left", out)
        self.assertEqual(len(calls), 2)  # no reroll request, straight to Continue

    def test_batch_state_get(self):
        self.assertEqual(server._batch.get("nope", 7), 7)
        self.assertIn(server._batch.get("last"), ("done", "failed", "interrupted", "running", None))


class TalkNoWindow(Base):
    def test_talk_that_never_opens_a_window_says_so(self):
        # S028 15:50: "talk_to: done — dialogue" and nothing else; the agent read get_dialogue to find out.
        saved = server.AUTO_DIALOGUE
        server.AUTO_DIALOGUE = True
        self.addCleanup(setattr, server, "AUTO_DIALOGUE", saved)
        orig = server._dialogue_autopilot
        server._dialogue_autopilot = lambda open_s=2.5, **kw: (0, [], "conversation ended")
        self.addCleanup(setattr, server, "_dialogue_autopilot", orig)
        out = server._auto_dialogue("talk_to", "talk_to: done — dialogue", "")
        self.assertIn("no conversation window opened", out)
        # lines were heard (events), or another tool: nothing added
        self.assertEqual(server._auto_dialogue("talk_to", "talk_to: done — dialogue", "dialogue_line text=Hi"), "")
        self.assertEqual(server._auto_dialogue("interact", "interact: done — used", ""), "")


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

    def test_long_rest_failure_says_where_the_request_died(self):
        # S028/S029: "rest screen didn't open" x7 with nothing to go on.
        def run_job(op, args, context="server", wait_s=25.0, **kw):
            return JobResult("failed", {"step": "open"}, "the rest screen didn't open (nothing for 4.0s)")
        self.g.run_job = run_job
        orig = self.g.try_rpc
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"steps": ["requested"], "story": {"DB_GoingToCamp": 1}} if op == "camp.rest_flow" else orig(op, args, **kw))
        out = server.long_rest()
        self.assertIn("REST FLOW requested: the game took the rest request but never answered it", out)
        self.assertNotIn("DB_GoingToCamp", out)  # story flags go to metrics only
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"steps": [], "story": {}} if op == "camp.rest_flow" else orig(op, args, **kw))
        self.assertIn("never registered the rest request", server.long_rest())

    def test_split_box_line_and_amount(self):
        # S028 15:51: a Supply Pack split box; confirm("Confirm") failed and the agent clicked by hand.
        lines = render.render_ui({"screen": "popup", "page": "SplitItem",
                                  "popup": {"title": "Supply Pack", "text": "/3", "buttons": ["Cancel", "Confirm"],
                                            "decision": True, "split": True}}, server.H)
        line = next(x for x in lines if x.startswith("POPUP"))
        self.assertIn("Supply Pack (1-3)", line)
        self.assertIn('confirm("yes", amount=N)', line)
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(args) or JobResult("done", {"pressed": "Confirm"}))
        server.confirm("yes", amount=2)
        self.assertEqual(seen[-1], {"answer": "yes", "amount": 2})

    def test_popup_line_says_how_to_answer(self):
        lines = render.render_ui({"screen": "popup", "popup": {"title": "[ForceUpdate]", "buttons": ["Yes", "No"],
                                                               "decision": True}}, server.H)
        self.assertTrue(any(line.startswith("POPUP") and "confirm(" in line for line in lines), lines)
        lines = render.render_ui({"screen": "popup", "popup": {"title": "Info", "buttons": ["OK"],
                                                               "dismissable": True}}, server.H)
        self.assertFalse(any("confirm(" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
