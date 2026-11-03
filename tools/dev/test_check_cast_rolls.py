"""Offline tests for tools/dev/check_cast_rolls.py's verdict (no game needed).

Run: uv run pytest -q tools/dev/test_check_cast_rolls.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_cast_rolls as check


def row(kind: str, damage: int, text: str = "use_ability: done", failed: bool = False) -> dict:
    return {"kind": kind, "damage": damage, "text": text, "failed": failed}


class VerdictTest(unittest.TestCase):
    def test_pass_when_attacks_miss_and_a_save_is_made(self):
        lines = check.verdict([row("attack", 0), row("attack", 9), row("save", 0), row("save", 11)])
        self.assertIn("attack 2/2 cast, 1 did damage, 1 did none; 0 failed 'without its roll'", lines)
        self.assertEqual(lines[-1], "PASS")

    def test_fail_when_every_attack_hits(self):
        lines = check.verdict([row("attack", 7)] * 6)
        self.assertIn("attack: every cast hit at +20 AC: still forced", lines)
        self.assertEqual(lines[-1], "FAIL")

    def test_fail_when_a_cast_went_without_its_roll(self):
        text = "use_ability: FAILED — the game cast it without its roll (IgnoreSpellRolls stayed set: ...)"
        lines = check.verdict([row("attack", 0, text, True), row("attack", 0)])
        self.assertIn("1 failed 'without its roll'", lines[0])
        self.assertEqual(lines[-1], "FAIL")

    def test_saves_all_failed_is_reported_not_failed(self):
        lines = check.verdict([row("attack", 0), row("save", 9), row("save", 8)])
        self.assertTrue(any(line.startswith("save: no save made in 2") for line in lines), lines)
        self.assertEqual(lines[-1], "PASS")

    def test_nothing_cast_fails(self):
        lines = check.verdict([row("attack", 0, "use_ability: FAILED — not your turn", True)])
        self.assertEqual(lines[-1], "FAIL")
