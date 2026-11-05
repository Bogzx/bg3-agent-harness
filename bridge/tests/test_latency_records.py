"""Offline tests: the metrics record covers the time before a tool's own t0 (latency survey 2026-11-04 §3/§5: 1.03 h
of launch-v1's calls ran before any record's t0, screenshot's record was 0 ms after its capture, talk_to's 1.5 s of
bridge-side time had no stage).

Run: cd bridge && uv run python -m unittest tests.test_latency_records
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from test_turn_macros import Base  # sets BG3AI_RUNTIME before importing the server

# isort: split
from bg3ai.tools import core, observe, reporting, saving, travel


class Records(Base):
    def setUp(self) -> None:
        super().setUp()
        self.recs: list[dict] = []
        real = core.METRICS.tool_done

        def tool_done(*a, **kw):
            rec = real(*a, **kw)
            if rec is not None:
                self.recs.append(rec)
            return rec

        p = mock.patch.object(core.METRICS, "tool_done", tool_done)
        p.start()
        self.addCleanup(p.stop)


class GuardTimes(Records):
    def test_the_record_has_the_guard_entry_and_the_save_wait(self):
        def slow_enter(timeout=None):
            time.sleep(0.05)  # a background save's menus
            return 0.05

        @reporting._guard
        def probe_tool() -> str:
            return reporting._finish("probe_tool", {}, "done", time.monotonic(), events=False)

        with mock.patch.object(saving.SAVER, "enter", slow_enter):
            probe_tool()
        rec = self.recs[-1]
        self.assertEqual(rec["save_wait_ms"], 50.0)
        # the save wait ran before the tool's own t0: the guard's entry is at least that much earlier
        self.assertGreaterEqual(rec["t0"] - rec["guard_t0"], 0.045)

    def test_an_ungated_tool_has_no_save_wait(self):
        @reporting._guard
        def memory_list() -> str:
            return "nothing"

        memory_list()
        rec = self.recs[-1]
        self.assertIn("guard_t0", rec)
        self.assertNotIn("save_wait_ms", rec)

    def test_a_nested_tool_keeps_its_own_and_the_outer_keeps_its_own(self):
        @reporting._guard
        def inner() -> str:
            return reporting._finish("inner", {}, "done", time.monotonic(), events=False)

        @reporting._guard
        def outer() -> str:
            t0 = time.monotonic()
            time.sleep(0.02)
            inner()
            return reporting._finish("outer", {}, "done", t0, events=False)

        outer()
        by = {r["tool"]: r for r in self.recs}
        self.assertGreater(by["inner"]["guard_t0"], by["outer"]["guard_t0"])
        self.assertLessEqual(by["outer"]["guard_t0"], by["outer"]["t0"] + 0.001)


class CallsWithTwoRecords(Records):
    """w11 L6: (a) pickpocket's _click_act step writes a 'pickpocket' record before the call's own, and the call's
    own record had no guard_t0 / save_wait_ms; (b) route(go=true)'s travel_to timed its record from route's start
    (reporting.call_t0, its walk budget), 70 ms before its own guard."""

    def test_every_record_of_one_call_has_its_guard_t0_and_the_save_wait_once(self):
        @reporting._guard
        def pickpocket() -> str:
            t0 = time.monotonic()
            reporting._finish("pickpocket", {}, "pickpocket: done — opened", time.monotonic(), events=False)  # step
            time.sleep(0.02)
            return reporting._finish("pickpocket", {}, "pickpocket: done", t0, events=False)

        with mock.patch.object(saving.SAVER, "enter", lambda timeout=None: 0.0):
            pickpocket()
        step, call = self.recs[-2:]
        self.assertEqual(step["guard_t0"], call["guard_t0"])
        self.assertEqual(step["save_wait_ms"], 0.0)
        self.assertNotIn("save_wait_ms", call)

    def test_a_record_never_starts_before_its_own_guard(self):
        @reporting._guard
        def travel_to() -> str:
            return reporting._finish("travel_to", {}, "travel_to: done", reporting.call_t0(), events=False)

        @reporting._guard
        def route() -> str:
            reporting._finish("route", {}, "ROUTE", time.monotonic(), events=False)
            time.sleep(0.07)
            return travel_to()

        route()
        rec = next(r for r in self.recs if r["tool"] == "travel_to")
        self.assertGreaterEqual(rec["t0"] + 0.002, rec["guard_t0"])
        self.assertLess(rec["ms"], 60.0)  # its own time, not route's 70 ms before it


class ScreenshotTime(Records):
    def test_the_record_covers_the_capture(self):
        class Cap:
            def capture(self, max_width):
                time.sleep(0.05)
                return b"jpg", SimpleNamespace(size=(2560, 1440))

        saved = core._screen, core._last_full_frame
        self.addCleanup(lambda: setattr(core, "_screen", saved[0]) or setattr(core, "_last_full_frame", saved[1]))
        core._screen = Cap()
        observe.screenshot()
        rec = next(r for r in self.recs if r["tool"] == "screenshot")
        self.assertGreaterEqual(rec["ms"], 50.0)


class TalkStages(Records):
    def test_talk_to_has_per_stage_times(self):
        choice = {
            "screen": "dialogue",
            "dialogue": {"text": "Choose.", "options": [{"i": 1, "text": "Yes"}, {"i": 2, "text": "No"}]},
        }
        self.g.ui_seq = [
            {"screen": "game"},
            {"screen": "dialogue", "dialogue": {"text": "Hello there.", "continue": True}},
            choice,
        ]
        out = travel.talk_to(self.gob)
        self.assertIn("STOPPED: a real choice (2 options)", out)
        rec = next(r for r in self.recs if r["tool"] == "talk_to")
        st = rec["talk"]
        self.assertEqual(set(st), {"act_ms", "events_ms", "auto_dialogue_ms", "events2_ms", "rest_ms"})
        self.assertLessEqual(sum(st.values()), rec["ms"] + 1.0)
        self.assertNotIn("act_ms", out)  # numbers for the record, not the agent's text

    def test_other_tools_have_none(self):
        @reporting._guard
        def probe_tool() -> str:
            return reporting._finish("probe_tool", {}, "done", time.monotonic())

        probe_tool()
        self.assertNotIn("talk", self.recs[-1])


if __name__ == "__main__":
    unittest.main()
