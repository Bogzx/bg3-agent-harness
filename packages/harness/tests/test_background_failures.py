# pyright: basic
"""A tool result's EVENTS block is what else happened meanwhile, not the call's own failures (bench diverse-2026-10-25,
Opus x act3_gold_max: a straggler's background walk-back `leg 2: move_to: TIMEOUT` echoed in the leader's wait_for,
which returned done, was counted a tool_timeout of that wait_for and voided the run as harness_failure)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bg3harness import runs, triage

# the wait_for result, verbatim: events.jsonl seq 1611 of runtime/sessions/diverse-2026-10-25-act3_gold_max-
# claude-opus-5-5-claude-medium-rules/S001
WAIT_FOR_RESULT = (Path(__file__).resolve().parent / "fixtures" / "results" / "act3_gold_max_wait_for.txt").read_text()


def _session(d: Path, text: str) -> Path:
    """S001 with one wait_for call and its result (events.jsonl v1); returns the stream path."""
    evs = [
        {
            "v": 1,
            "seq": 1,
            "t": 1792925416.657,
            "src": "client",
            "kind": "tool_call",
            "tool": "wait_for",
            "args": {"target": "queue", "timeout_s": 60},
            "call_id": "c1",
        },
        {"v": 1, "seq": 2, "t": 1792925461.019, "src": "client", "kind": "tool_result", "text": text, "call_id": "c1"},
    ]
    (d / "S001.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    stream = d / "S001.jsonl"
    stream.write_text("")
    return stream


class BackgroundFailures(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-bgfail-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_an_events_timeout_is_not_the_calls_failure(self) -> None:
        self.assertEqual(triage.failures("wait_for", {}, WAIT_FOR_RESULT), [])
        bg = triage.background_failures({}, WAIT_FOR_RESULT)
        self.assertEqual([s for s, _ in bg], ["move_to"])
        self.assertTrue(bg[0][1].startswith("TIMEOUT: timed out — @(0,26,-89) purged_tasks=1"))

    def test_the_calls_own_timeout_before_its_events_still_counts(self) -> None:
        text = "2. end_turn: TIMEOUT — still my turn\nEVENTS\n  died e1353 Conjured Spider\nNOW x"
        self.assertEqual(triage.failures("combat_turn", {}, text), [("end_turn", "TIMEOUT: still my turn")])
        self.assertEqual(triage.background_failures({}, text), [])

    def test_a_background_timeout_is_listed_but_is_no_harness_failure(self) -> None:
        fails = runs.stream_failures(_session(self.d, WAIT_FOR_RESULT))
        self.assertEqual([(f["kind"], f["class"]) for f in fails], [("bg_timeout", runs.UNKNOWN)])
        self.assertIn("S001: move_to: TIMEOUT: timed out", fails[0]["detail"])
        self.assertTrue(runs.is_clean("reached", fails, []))

    def test_triage_lists_it_apart(self) -> None:
        _session(self.d, WAIT_FOR_RESULT)
        md = triage.build(self.d / "S001")
        self.assertIn("## Failed tool calls (0 of 1 calls; 0 failures incl. batch/queue steps)", md)
        self.assertIn("## Background failures in EVENTS (1; not the calls' own)", md)
        self.assertIn("**move_to** in `wait_for`'s EVENTS: TIMEOUT: timed out", md)

