"""acceptance reads.read_cost_probe (V12): the warm side is the median of 5 samples, the cold read one emptied-memo read
(N7 / N8 / N16: one warm sample of the same rebuild read 33, 34 and 57 ms, so the verdict flipped window to window).

    python3 -m unittest tools/dev/test_acceptance_read_cost.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import reads


class ReadCost(unittest.TestCase):
    def run_probe(self, inv: list[float], prep: list[float], inv_cold: float = 59, prep_cold: float = 25):
        """inv / prep: the warm samples in order (the first comes with the cold call)."""
        first = {
            "inv_cold": inv_cold,
            "inv_warm": inv[0],
            "prep_cold": prep_cold,
            "prep_warm": prep[0],
            "items": 194,
            "prepared": 25,
        }
        later = zip(inv[1:], prep[1:], strict=True)
        answers = [json.dumps(first)] + [json.dumps({"inv_warm": i, "prep_warm": p}) for i, p in later]
        codes: list[str] = []

        def lua(code: str) -> str:
            codes.append(code)
            return answers[len(codes) - 1]

        rows: list[dict] = []
        with (
            mock.patch.object(reads, "lua_server", lua),
            mock.patch.object(reads, "item", lambda n, v, e: rows.append({"verdict": v, "ev": e})),
        ):
            reads.read_cost_probe()
        return rows[0], codes

    def test_one_slow_outlier_passes(self):
        # N16's 57 ms as one sample among N8-like ones
        row, codes = self.run_probe([57, 33, 34, 35, 33], [15, 14, 19, 14, 15])
        self.assertEqual(row["verdict"], "PASS", row["ev"])
        self.assertIn("state.inventory rebuild 59 -> median 34 ms [57 33 34 35 33] (194 items)", row["ev"])
        self.assertIn("prep_info (party) 25 -> median 15 ms [15 14 19 14 15]", row["ev"])
        self.assertEqual(len(codes), 5)

    def test_consistently_slow_fails_and_shows_the_spread(self):
        row, _ = self.run_probe([57, 55, 58, 56, 60], [15, 14, 15, 14, 15])
        self.assertEqual(row["verdict"], "FAIL")
        self.assertIn("59 -> median 57 ms [57 55 58 56 60]", row["ev"])
        row, _ = self.run_probe([33, 34, 33, 35, 34], [19, 18, 19, 20, 19])  # N7's prep, every sample
        self.assertEqual(row["verdict"], "FAIL")

    def test_the_cold_read_is_the_first_call_and_only_it_empties_the_memos(self):
        _, codes = self.run_probe([33] * 5, [14] * 5)
        self.assertIn("S.statsFactsMemo,S.itemFactsMemo,G.spellFacts,G.classNames={},{},{},{}", codes[0])
        self.assertTrue(all("Memo" not in c and "inv_cold" not in c for c in codes[1:]))

    def test_threshold_unchanged(self):
        self.assertEqual(reads.read_cost_verdict(59, [35.4]), (True, 35.4))
        self.assertEqual(reads.read_cost_verdict(59, [35.5])[0], False)
        self.assertEqual(reads.read_cost_verdict(20, [15, 15, 16])[0], True)  # the 15 ms floor


if __name__ == "__main__":
    unittest.main()
