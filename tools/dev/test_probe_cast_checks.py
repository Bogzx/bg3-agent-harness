"""Offline tests for tools/dev/probe_cast_checks.py (no game needed): the plans, verdicts and the summary.

Run: uv run pytest -q tools/dev/test_probe_cast_checks.py
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_cast_checks as probe


def args(**kw) -> argparse.Namespace:
    base = {
        "target": "e7",
        "heavy": "e14",
        "caster": "Karlach",
        "hold_caster": "Shadowheart",
        "attack": "Main Hand Attack",
        "immobile": "ENSNARED",
        "rager": "Karlach",
        "surger": "Lae'zel",
    }
    base.update(kw)
    return argparse.Namespace(**base)


class ProbeTest(unittest.TestCase):
    def test_plans_name_each_review_case(self):
        combat = probe.plan("combat", args())
        self.assertEqual(
            [c["case"] for c in combat], ["control", "hold_person", "heavy_shove", "prone_attack", "jump_webbed"]
        )
        self.assertEqual(combat[1]["who"], "Shadowheart")
        self.assertEqual(combat[2]["target"], "e14")
        self.assertEqual(combat[3]["status"], "PRONE")
        self.assertEqual(combat[4]["status"], "ENSNARED")
        ooc = probe.plan("ooc", args(surger="Tav"))
        self.assertEqual(
            [(c["case"], c["who"], c["need"]) for c in ooc],
            [("rage_ooc", "Karlach", "no_combat"), ("surge_ooc", "Tav", "no_combat")],
        )
        with self.assertRaises(ValueError):
            probe.plan("nope", args())

    def test_verdict_reads_the_first_line(self):
        self.assertEqual(probe.verdict("use_ability: done — target={e7}\nEVENTS\n  x FAILED"), "cast")
        self.assertEqual(probe.verdict("use_ability: FAILED — the game refused the cast (no reason given)"), "refused")
        self.assertEqual(probe.verdict("use_ability: FAILED — target is out of range (6.1 m, range 1.5 m)"), "guard")
        self.assertEqual(probe.verdict("REFUSED: enemy too close"), "guard")

    def test_summary_says_what_each_case_means(self):
        rows = [
            {"case": "control", "mode": "off", "verdict": "cast"},
            {"case": "control", "mode": "on", "verdict": "cast"},
            {"case": "hold_person", "mode": "off", "verdict": "cast"},
            {"case": "hold_person", "mode": "on", "verdict": "refused"},
            {"case": "rage_ooc", "mode": "off", "verdict": "cast"},
            {"case": "rage_ooc", "mode": "on", "verdict": "cast"},
            {"case": "surge_ooc", "mode": "on", "verdict": "not run"},
        ]
        lines = probe.summary(rows)
        self.assertIn("OK: still works", lines[0])
        self.assertIn("the engine re-checks once the flags are gone", lines[1])
        self.assertIn("needs the Lua check", lines[2])
        self.assertIn("inconclusive", lines[3])
        broken = probe.summary([{"case": "control", "mode": "on", "verdict": "refused"}])
        self.assertIn("BROKEN", broken[0])


if __name__ == "__main__":
    unittest.main()
