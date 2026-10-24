"""Offline tests (S005 triage): roll_check raised 'Error executing tool roll_check' x3 (_BatchState had no
.get), the confirm tool for Yes/No boxes, long_rest(confirm=false), and the POPUP line's answer hint.

Run: cd bridge && uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing server)

from bg3ai import render  # noqa: E402
from bg3ai.tools import core, reporting, dialogue, inventory, uitools, macro_tools, session  # noqa: E402
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
        out = dialogue.roll_check()
        self.assertNotIn("Error", out)
        self.assertEqual([c[0] for c in calls], ["ui.roll", "ui.roll"])  # rolled, then Continue

    def test_boost_not_offered_rolls_without_it(self):
        # S035 19:24:20: roll_check(boosts="Guidance") on a roll with no boosts failed; the agent rolled bare next.
        from bg3ai.game import GameError
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                raise GameError("no boost 'Guidance' on this roll (the roll offers no boosts)")
            if len(calls) == 2:
                return JobResult("done", {"roll": {"phase": "result", "success": True, "result": 15}})
            return JobResult("done", {"screen": "dialogue"})
        self.g.run_job = run_job
        out = dialogue.roll_check(boosts="Guidance")
        self.assertEqual(calls[1], {"reroll": False})
        self.assertEqual(len(calls), 3)
        self.assertTrue(out.startswith("(rolled without Guidance: the roll offers no boosts)"), out)

    def test_reroll_on_fail(self):
        calls = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": False}})
            return JobResult("done", {"roll": {"phase": "result", "success": True}})
        self.g.run_job = run_job
        dialogue.roll_check(reroll_on_fail=True)
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
        out = dialogue.roll_check(reroll_on_fail=True)
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
        out = dialogue.roll_check(reroll_on_fail=True)
        self.assertIn("the game kept the first result", out)
        self.assertEqual(len(calls), 3)
        calls.clear()

        def run_job2(op, args, context="server", wait_s=25.0, **kw):
            calls.append(dict(args))
            if len(calls) == 1:
                return JobResult("done", {"roll": {"phase": "result", "success": False, "result": 2, "inspiration": 0}})
            return JobResult("done", {"screen": "game"})
        self.g.run_job = run_job2
        out = dialogue.roll_check(reroll_on_fail=True)
        self.assertIn("no Inspiration points left", out)
        self.assertEqual(len(calls), 2)  # no reroll request, straight to Continue

    def test_batch_state_get(self):
        self.assertEqual(reporting._batch.get("nope", 7), 7)
        self.assertIn(reporting._batch.get("last"), ("done", "failed", "interrupted", "running", None))


class TalkNoWindow(Base):
    def test_talk_that_never_opens_a_window_says_so(self):
        # S028 15:50: "talk_to: done — dialogue" and nothing else; the agent read get_dialogue to find out.
        saved = macro_tools.AUTO_DIALOGUE
        macro_tools.AUTO_DIALOGUE = True
        self.addCleanup(setattr, macro_tools, "AUTO_DIALOGUE", saved)
        orig = macro_tools._dialogue_autopilot
        macro_tools._dialogue_autopilot = lambda open_s=2.5, **kw: (0, [], "conversation ended")
        self.addCleanup(setattr, macro_tools, "_dialogue_autopilot", orig)
        out = reporting._auto_dialogue("talk_to", "talk_to: done — dialogue", "")
        self.assertIn("no conversation window opened", out)
        # lines were heard (events), or another tool: nothing added
        self.assertEqual(reporting._auto_dialogue("talk_to", "talk_to: done — dialogue", "dialogue_line text=Hi"), "")
        self.assertEqual(reporting._auto_dialogue("interact", "interact: done — used", ""), "")


class Confirm(Base):
    def test_confirm_sends_ui_confirm(self):
        seen = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            seen.append((op, args, context))
            return JobResult("done", {"pressed": "Yes", "buttons": ["Yes", "No"]})
        self.g.run_job = run_job
        out = uitools.confirm("yes")
        self.assertEqual(seen, [("ui.confirm", {"answer": "yes"}, "client")])
        self.assertIn("pressed=Yes", out)
        self.assertIn("confirm", session.ALWAYS_LOAD)

    def test_long_rest_confirm_flag(self):
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(args) or JobResult("done", {"rest": "full"}))
        inventory.long_rest()
        inventory.long_rest(confirm=False)
        self.assertEqual(seen, [{"repress": False}, {"confirm": False, "repress": False}])

    def test_long_rest_failure_says_where_the_request_died(self):
        # S028/S029: "rest screen didn't open" x7 with nothing to go on.
        def run_job(op, args, context="server", wait_s=25.0, **kw):
            return JobResult("failed", {"step": "open"}, "the rest screen didn't open (nothing for 4.0s)")
        self.g.run_job = run_job
        orig = self.g.try_rpc
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"steps": ["requested"], "story": {"DB_GoingToCamp": 1}} if op == "camp.rest_flow" else orig(op, args, **kw))
        out = inventory.long_rest()
        self.assertIn("REST FLOW requested: the game took the rest request but never answered it", out)
        self.assertNotIn("DB_GoingToCamp", out)  # story flags go to metrics only
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"steps": [], "story": {}} if op == "camp.rest_flow" else orig(op, args, **kw))
        self.assertIn("never registered the rest request", inventory.long_rest())

    def test_long_rest_refused_on_the_games_rest_check(self):
        # S028-S030: 10 x "rest screen didn't open" (8 s each). CanDoRest.LongRestErrorFlags names the blocker
        # (the greyed HUD button's tooltip): refuse at once, with the reason, before pressing anything.
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(op) or JobResult("done", {"rest": "full"}))
        orig = self.g.try_rpc
        blocked = {"blocked": [{"name": "Karlach", "flags": ["FTB"]}, {"name": "Gale", "flags": ["Location"]}],
                   "global_disabled": 0}
        self.g.try_rpc = lambda op, args=None, **kw: blocked if op == "camp.can_rest" else orig(op, args, **kw)
        with mock.patch.object(time, "sleep"):
            out = inventory.long_rest()
        self.assertIn("doesn't allow a long rest", out)
        self.assertIn("Karlach: turn-based mode is on", out)
        self.assertNotIn("Gale", out)  # Location alone only explains a failure
        self.assertEqual(seen, [])
        # only soft flags (Location, supplies): the rest goes on
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"blocked": [{"name": "Gale", "flags": ["Location", "NotEnoughResources"]}]} if op == "camp.can_rest"
            else orig(op, args, **kw))
        self.assertIn("done", inventory.long_rest())
        self.assertEqual(seen, ["ui.long_rest"])

    def test_long_rest_refused_right_after_a_rest_at_this_camp(self):
        # Live 2026-10-24 T3/T3b/PROBE-DONATE: a long_rest after a finished one: 'requested', never answered, with the
        # camp story's DB_CAMP_JustWokeUp set, until a load (REST FLOW 'a stuck camp state'). Refused before pressing.
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(op) or JobResult("done", {"rest": "full"}))
        orig = self.g.try_rpc
        story = {"DB_CAMP_JustWokeUp": 1, "DB_GoingToCamp": 0, "DB_PlayerInCamp": 4}

        def try_rpc(op, args=None, **kw):
            if op == "camp.can_rest":
                return {"blocked": [], "global_disabled": 0}
            if op == "camp.rest_flow":
                return {"steps": [], "story": dict(story)}
            return orig(op, args, **kw)
        self.g.try_rpc = try_rpc
        out = inventory.long_rest(partial=True)
        self.assertIn("not pressed: the party woke up from a long rest at this camp", out)
        self.assertIn('camp(action="leave")', out)
        self.assertNotIn("DB_CAMP", out)
        self.assertEqual(seen, [])
        story["DB_CAMP_JustWokeUp"] = 0  # left camp since (or no rest yet): pressed as before
        self.assertIn("done", inventory.long_rest())
        self.assertEqual(seen, ["ui.long_rest"])

    def test_long_rest_waits_for_the_end_day_answer(self):
        # S030 16:30: requested -> gathered, the job gave up at 8 s, end_day_failed came ~10 s after the press;
        # the hint said "requested: loading a save clears it" and the agent reloaded (25 s) for nothing.
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "failed", {"step": "open"}, "the rest screen didn't open (nothing for 4.0s)")
        orig = self.g.try_rpc
        polls = []

        def try_rpc(op, args=None, **kw):
            if op == "camp.can_rest":
                return {"blocked": []}
            if op == "camp.rest_flow":
                polls.append(1)
                steps = ["requested", "gathered", "requested"] + (["end_day_failed"] if len(polls) >= 3 else [])
                return {"steps": steps, "story": {}, "can_rest": {"blocked": [{"name": "Claude", "flags": ["Location"]}]}}
            return orig(op, args, **kw)
        self.g.try_rpc = try_rpc
        with mock.patch.object(time, "sleep"):
            out = inventory.long_rest()
        self.assertEqual(len(polls), 3)
        self.assertIn("REST FLOW requested → gathered → requested → end_day_failed: the game refused to end the day", out)
        self.assertIn("the game's rest check: Claude: not at a place where you can rest", out)
        self.assertNotIn("loading a save clears it", out)

    def test_long_rest_failure_names_dead_members(self):
        # S058 07:58: gather_failed + "Wyll: not at a place where you can rest" — Wyll and Shadowheart were dead.
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: JobResult(
            "failed", {"step": "open"}, "the rest screen didn't open (nothing for 4.0s)")
        orig = self.g.try_rpc
        dead = [True]

        def try_rpc(op, args=None, **kw):
            if op == "camp.can_rest":
                return {"blocked": []}
            if op == "camp.rest_flow":
                return {"steps": ["requested", "gather_failed"],
                        "can_rest": {"blocked": [{"name": "Wyll", "flags": ["Location"]}]}}
            if op == "state.status":
                return {"party": [{"name": "Claude"}, {"name": "Wyll", "dead": dead[0]},
                                  {"name": "Spiritual Weapon", "dead": True, "summon": True}]}
            return orig(op, args, **kw)
        self.g.try_rpc = try_rpc
        with mock.patch.object(time, "sleep"):
            out = inventory.long_rest()
        self.assertIn("gather_failed", out)
        self.assertIn("DEAD: Wyll — the game won't gather a dead party member", out)
        self.assertNotIn("Spiritual Weapon", out)
        dead[0] = False
        with mock.patch.object(time, "sleep"):
            self.assertNotIn("DEAD:", inventory.long_rest())

    def test_long_rest_refused_before_pressing_on_dead_members(self):
        # S058 07:58: can_rest flagged dead Wyll + Shadowheart (Location) before the press; the press then failed
        # gather_failed after 4 s. Now: refused at once, nothing pressed, the dead named with what to do.
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(op) or JobResult("done", {"rest": "full"}))
        orig = self.g.try_rpc
        party = [{"name": "Claude"}, {"name": "Wyll", "dead": True}, {"name": "Shadowheart", "dead": True}]

        def try_rpc(op, args=None, **kw):
            if op == "camp.can_rest":
                return {"blocked": [{"name": "Wyll", "flags": ["Location"]},
                                    {"name": "Shadowheart", "flags": ["Location"]}], "global_disabled": 0}
            if op == "state.status":
                return {"party": party}
            return orig(op, args, **kw)
        self.g.try_rpc = try_rpc
        out = inventory.long_rest(partial=True)
        self.assertIn("not pressed", out)
        self.assertIn("DEAD: Wyll, Shadowheart", out)
        self.assertEqual(seen, [])
        # alive but flagged Location (soft): pressed as before
        party[1:] = [{"name": "Wyll"}, {"name": "Shadowheart"}]
        self.assertIn("done", inventory.long_rest())
        self.assertEqual(seen, ["ui.long_rest"])

    def test_long_rest_presses_again_only_when_the_press_never_arrived(self):
        # S030: the job's own 4 s re-press landed in the engine's ~10 s answer window. Now the bridge re-presses
        # only when the server's rest flow shows nothing for this call.
        calls = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            calls.append(dict(args)) or JobResult("failed", {"step": "open"}, "the rest screen didn't open (nothing for 4.0s)"))
        orig = self.g.try_rpc
        flows = []

        def try_rpc(op, args=None, **kw):
            if op == "camp.can_rest":
                return {"blocked": []}
            if op == "camp.rest_flow":
                flows.append(args)
                return {"steps": ["requested", "gathered", "end_day_failed"]}
            return orig(op, args, **kw)
        self.g.try_rpc = try_rpc
        with mock.patch.object(time, "sleep"):
            out = inventory.long_rest()
        self.assertEqual(calls, [{"repress": False}])  # reached the game: no second press
        self.assertIn("end_day_failed", out)
        self.assertTrue(all(0 < f["since_ms"] < 20000 for f in flows))  # only this call's steps
        calls.clear()
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"blocked": []} if op == "camp.can_rest" else {"steps": []} if op == "camp.rest_flow" else orig(op, args, **kw))
        with mock.patch.object(time, "sleep"):
            out = inventory.long_rest()
        self.assertEqual(len(calls), 2)  # never reached the story: one more press
        self.assertIn("never registered the rest request", out)

    def test_rest_flow_names_the_ready_check(self):
        # S031 17:15: can_rest clean, requested -> gathered, then nothing for 14 s and no box.
        flow = {"steps": ["requested", "gathered"], "can_rest": {"blocked": []}}
        out = inventory._rest_flow_hint(flow)
        self.assertIn("the party gathered", out)
        self.assertIn("no ready-check result either", out)
        out = inventory._rest_flow_hint({"steps": ["requested", "gathered", "ready_failed:EndTheDay_1"]})
        self.assertIn("ready check (the \"End the day?\" question) failed (a story ready check)", out)
        self.assertNotIn("EndTheDay_1", out)  # engine ids never reach the agent (S044 fairness LEAK)
        out = inventory._rest_flow_hint({"steps": ["requested", "gathered", "ready_ok:MOO_Assault_ReadyCheck",
                                                "ready_failed:ReadyCheckContent_GoToNight"]})
        self.assertIn("ready_ok (a story ready check) → ready_failed (end the day)", out)
        self.assertNotIn("MOO_", out)
        self.assertNotIn("ReadyCheckContent", out)
        out = inventory._rest_flow_hint({"steps": ["requested", "gathered", "ready_ok:x", "end_day_failed"]})
        self.assertIn("refused to end the day", out)
        self.assertNotIn("no ready-check result", out)

    def test_long_rest_late_yes_carries_on(self):
        calls = []
        argl = []

        def run_job(op, args, context="server", wait_s=25.0, **kw):
            calls.append(op)
            argl.append(dict(args))
            if len(calls) == 1:
                return JobResult("failed", {"step": "open"}, "the rest screen didn't open (nothing for 4.0s)")
            return JobResult("done", {"rest": "full"})
        self.g.run_job = run_job
        orig = self.g.try_rpc
        self.g.try_rpc = lambda op, args=None, **kw: (
            {"blocked": []} if op == "camp.can_rest" else
            {"steps": ["requested", "gathered", "end_day_ok"]} if op == "camp.rest_flow" else orig(op, args, **kw))
        out = inventory.long_rest()
        self.assertEqual(calls, ["ui.long_rest", "ui.long_rest"])
        self.assertTrue(argl[1].get("no_press"))  # a press at night = "leave night mode" in the story
        self.assertIn("long_rest: done", out)

    def test_split_box_line_and_amount(self):
        # S028 15:51: a Supply Pack split box; confirm("Confirm") failed and the agent clicked by hand.
        lines = render.render_ui({"screen": "popup", "page": "SplitItem",
                                  "popup": {"title": "Supply Pack", "text": "/3", "buttons": ["Cancel", "Confirm"],
                                            "decision": True, "split": True}}, core.H)
        line = next(x for x in lines if x.startswith("POPUP"))
        self.assertIn("Supply Pack (1-3)", line)
        self.assertIn('confirm("yes", amount=N)', line)
        seen = []
        self.g.run_job = lambda op, args, context="server", wait_s=25.0, **kw: (
            seen.append(args) or JobResult("done", {"pressed": "Confirm"}))
        uitools.confirm("yes", amount=2)
        self.assertEqual(seen[-1], {"answer": "yes", "amount": 2})

    def test_popup_line_says_how_to_answer(self):
        lines = render.render_ui({"screen": "popup", "popup": {"title": "[ForceUpdate]", "buttons": ["Yes", "No"],
                                                               "decision": True}}, core.H)
        self.assertTrue(any(line.startswith("POPUP") and "confirm(" in line for line in lines), lines)
        lines = render.render_ui({"screen": "popup", "popup": {"title": "Info", "buttons": ["OK"],
                                                               "dismissable": True}}, core.H)
        self.assertFalse(any("confirm(" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
