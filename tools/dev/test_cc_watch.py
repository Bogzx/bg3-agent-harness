"""Offline: acceptance.cc_verdict on the recorded create_character shapes. Run: python3 tools/dev/test_cc_watch.py"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance  # noqa: E402


def rec(t, ok=True, **kw):
    r = {"t": t, "op": "create_character", "ok": ok, "pacing": "state", "apply_ms": 1400, "naming_ms": 900,
         "world_ms": 8100, "total_ms": 10400, "how": "predicted", "popups": 1, "naming_stages": {}}
    r.update(kw)
    return r


class CCVerdict(unittest.TestCase):
    def test_no_new_records_skip(self):
        # the naut series' only records: ui.cc_apply stage lines, no create_character op
        old = [{"t": 1791900029.2, "op": "ui.cc_apply", "status": "done", "elapsed_ms": 6107, "stages": "..."}]
        self.assertEqual(acceptance.cc_verdict(old)[0], "SKIP")

    def test_one_fast_attempt_passes(self):
        v, ev = acceptance.cc_verdict([rec(100)])
        self.assertEqual(v, "PASS", ev)
        self.assertIn("1 call(s) (0 failed)", ev)

    def test_naut_runs_1_2_shape_fails(self):
        # two failed calls, then the third one worked (2026-10-13 runs 1 and 2)
        err = "the name box never reported the cursor over it (window 1920x1080, 22 probes down the centre line)"
        recs = [rec(100, ok=False, error=err), rec(110, ok=False, error=err), rec(125)]
        v, ev = acceptance.cc_verdict(recs)
        self.assertEqual(v, "FAIL")
        self.assertIn("3 call(s) (2 failed; the name box never", ev)

    def test_naut_run_3_timing_fails(self):
        v, _ = acceptance.cc_verdict([rec(100, apply_ms=6100, naming_ms=5200, total_ms=22100)])
        self.assertEqual(v, "FAIL")

    def test_only_the_last_creation_counts(self):
        recs = [rec(100, ok=False, error="x"), rec(5000)]
        self.assertEqual(acceptance.cc_verdict(recs)[0], "PASS")


if __name__ == "__main__":
    unittest.main()
