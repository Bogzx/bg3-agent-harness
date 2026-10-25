"""bg3bench.regrade: a run voided by a stream failure today's classifier no longer counts is reported valid, and the
run's files are left as they were."""

from __future__ import annotations

import json
from pathlib import Path

from bg3bench import regrade

RESULT = (
    "queue e4 Tav: done (2 steps)\nEVENTS\n  queue_interrupted why=leg 2: move_to: TIMEOUT — timed out who=e3\nNOW x"
)


def test_a_background_timeout_no_longer_voids_the_run(tmp_path: Path) -> None:
    evs = [
        {"v": 1, "seq": 1, "t": 10.0, "src": "client", "kind": "tool_call", "tool": "wait_for", "call_id": "c1"},
        {"v": 1, "seq": 2, "t": 11.0, "src": "client", "kind": "tool_result", "text": RESULT, "call_id": "c1"},
    ]
    (tmp_path / "S001.events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in evs))
    (tmp_path / "S001.jsonl").write_text("")
    rec = {
        "run": 1,
        "outcome": "timeout",
        "started": 0,
        "harness_failures": 1,
        "manifest": {"seed": None},
        "failures": [
            {
                "t": 11.0,
                "kind": "tool_timeout",
                "class": "harness",
                "detail": "S001: move_to: TIMEOUT: timed out who=#id",
            },
            {"t": 20.0, "kind": "timeout", "class": "unknown", "detail": "the run's time ran out before the milestone"},
        ],
    }
    (tmp_path / "runs.jsonl").write_text(json.dumps(rec) + "\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    out = regrade.report(tmp_path)
    assert out[0].endswith("CHANGED: harness_failures 1 -> 0, valid False (harness_failure) -> True (None)")
    assert "  + bg_timeout [unknown] S001: move_to: TIMEOUT: timed out who=#id" in out
    assert "  + timeout [agent] the run's time ran out before the milestone" in out
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
