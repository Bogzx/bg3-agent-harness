# pyright: basic
"""A do_actions that stopped at its time budget (bg3ai.actq.BATCH_BUDGET_S) did partial work: no failure, so the run
stays clean. Before the budget, 6 waits of 30 s outran the client's 110 s MCP timeout and that call was a
tool_timeout, a harness failure that voided the run."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bg3harness import runs, triage

# the bridge's do_actions result for 6 waits of 30 s (bridge tests/test_actq.py test_a_batch_stops_at_its_time_budget)
BUDGET_STOP = (
    "1. wait: waited 30.0s\n2. wait: waited 30.0s\n3. wait: waited 30.0s\n"
    "STOPPED: time budget (90 s per call), 3 steps left; not run: wait, wait, wait"
)
CUT_WAIT = "1. wait: waited 28.0s\n2. wait: waited 6.0s of 25s (time budget)"


def _session(d: Path, text: str, is_error: bool = False) -> Path:
    """S001 with one do_actions call and its result (events.jsonl v1); returns the stream path."""
    evs = [
        {
            "v": 1,
            "seq": 1,
            "t": 1793383465.9,
            "src": "client",
            "kind": "tool_call",
            "tool": "do_actions",
            "args": {"actions": [{"tool": "wait", "seconds": 30}] * 6},
            "call_id": "c1",
        },
        {
            "v": 1,
            "seq": 2,
            "t": 1793383555.9,
            "src": "client",
            "kind": "tool_result",
            "text": text,
            "call_id": "c1",
            "is_error": is_error,
        },
    ]
    (d / "S001.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    stream = d / "S001.jsonl"
    stream.write_text("")
    return stream


class BatchBudget(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-budget-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_a_budget_stop_is_no_failure(self) -> None:
        for text in (BUDGET_STOP, CUT_WAIT):
            self.assertEqual(triage.failures("do_actions", {}, text), [])
            fails = runs.stream_failures(_session(self.d, text))
            self.assertEqual(fails, [])
            self.assertTrue(runs.is_clean("reached", fails, []))

    def test_the_client_timeout_it_replaces_was_a_harness_failure(self) -> None:
        fails = runs.stream_failures(_session(self.d, "MCP request timed out after 110000ms", is_error=True))
        self.assertEqual([(f["kind"], f["class"]) for f in fails], [("tool_timeout", runs.HARNESS)])
