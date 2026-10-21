"""Offline tests for tools/dev/check_rolllog.py's verdicts (no game needed).

Run: uv run --project bridge python -m unittest tools/dev/test_check_rolllog.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_rolllog

SNAP = {"hooks": {"post_update": True, "requested_create": True, "finished_create": True}}


def rec(i: int, natural: int | None = 12, result: int | None = 15, dc: int | None = 10, **kw):
    r = {
        "event": f"rollcheck{i}",
        "srcs": ["osi", "requested"],
        "natural": natural,
        "result": result,
        "dc": dc,
        "success": None if result is None or dc is None else result >= dc,
        "stream": "Generic",
        "streams": {"Generic": 2},
        "crit": {20: "nat20", 1: "nat1"}.get(natural or 0),
    }
    r.update(kw)
    return {k: v for k, v in r.items() if v is not None}


def verdicts(snap, records, n=3, dc=10):
    return {name: v for v, name, _ in check_rolllog.grade(snap, records, n, dc)}


class GradeTest(unittest.TestCase):
    def test_all_good(self):
        got = verdicts(SNAP, [rec(1), rec(2, natural=20, result=23), rec(3, natural=1, result=4)])
        self.assertEqual(set(got.values()), {"PASS"}, got)

    def test_live3_osi_only_fails(self):
        """research/ovh/live3.md §4: srcs osi only, no natural, streams on the batch's first record only."""
        rs = [{"event": f"rollcheck{i}", "srcs": ["osi"], "success": True} for i in (1, 2, 3)]
        rs[0]["streams"] = {"Generic": 68}
        got = verdicts({"hooks": {"finished_create": True}}, rs)
        self.assertEqual(got["hooks"], "FAIL")
        self.assertEqual(got["natural"], "FAIL")
        self.assertEqual(got["result"], "WARN")
        self.assertEqual(got["streams"], "FAIL")
        self.assertEqual(got["records"], "PASS")

    def test_finished_only_warns_on_result(self):
        rs = [rec(i, result=None, srcs=["finished", "osi"]) for i in (1, 2, 3)]
        got = verdicts(SNAP, rs)
        self.assertEqual((got["natural"], got["result"], got["agree"]), ("PASS", "WARN", "PASS"))

    def test_missing_and_misordered_records(self):
        got = verdicts(SNAP, [rec(2), rec(1)], n=3)
        self.assertEqual(got["records"], "FAIL")
        other = verdicts(SNAP, [rec(3), rec(1), rec(2), {"event": "ambient", "srcs": ["osi"]}])
        self.assertEqual(other["records"], "PASS")

    def test_disagreement_fails(self):
        got = verdicts(SNAP, [rec(1, natural=20), rec(2, success=False), rec(3)])
        self.assertEqual(got["agree"], "FAIL")

    def test_wrong_dc_warns(self):
        self.assertEqual(verdicts(SNAP, [rec(i, dc=15) for i in (1, 2, 3)])["natural"], "WARN")

    def test_tick_fallback_counts_as_hooked(self):
        snap = {"hooks": {"post_update": False, "tick": True, "requested_create": True}}
        self.assertEqual(verdicts(snap, [rec(1), rec(2), rec(3)])["hooks"], "PASS")


if __name__ == "__main__":
    unittest.main()
