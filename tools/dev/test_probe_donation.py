"""Offline tests for tools/dev/probe_donation.py (no game needed): the item name it sends, the summary lines.

Run: uv run pytest -q tools/dev/test_probe_donation.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_donation as probe


class ProbeTest(unittest.TestCase):
    def test_code_fills_the_item_name(self):
        self.assertIn('local want = "Beastmaster\'s Chain"', probe.probe_code("Beastmaster's Chain"))
        self.assertIn("local want = nil", probe.probe_code(None))

    def test_summary_sets_window_against_bags(self):
        r = {
            "dc": ["DonateSourceInventories:userdata"],
            "sources": [{"owner": "Vessa", "invs": [{"count": 12, "slots": [{"index": 0}, {"index": 1}]}]}],
            "groups": [{"slots": 2, "scores": [{"owner": "Vessa", "hit": 0, "miss": 2}]}],
            "bags": [{"owner": "Vessa", "slots": [{"index": 7, "name": "Beastmaster's Chain"}]}],
            "lists": [[{"index": 0}, {"index": 1}]],
            "item": {"uuid": "u1", "bag": "Vessa", "slot": 7, "member_slot": 7, "found": False, "err": "not here"},
        }
        lines = probe.summarize(r)
        self.assertEqual(lines[0], "dc: DonateSourceInventories:userdata")
        self.assertEqual(lines[1], "source Vessa: 1 TradeInventories, slots 12 occupied 2 indexes [0, 1]")
        self.assertEqual(lines[2], "group (2 slots) -> NO MATCH; hit/miss: Vessa 0/2")
        self.assertEqual(lines[3], "bag Vessa: 7=Beastmaster's Chain")
        self.assertEqual(lines[4], "list 1: indexes [0, 1]")
        self.assertEqual(lines[5], "item u1 in Vessa slot 7 (member slot 7): findVMItem FAILED: not here")

    def test_error(self):
        self.assertEqual(probe.summarize({"error": "no donation window is open"}), ["ERROR no donation window is open"])


if __name__ == "__main__":
    unittest.main()
