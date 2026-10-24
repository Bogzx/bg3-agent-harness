"""Offline tests for tools/dev/probe_roll_sources.py's summary (no game needed).

Run: uv run pytest -q tools/dev/test_probe_roll_sources.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_roll_sources as probe


def rec(src: str, comp: str, *fields: str) -> dict:
    return {"src": src, "comp": comp, "fields": list(fields), "raw": "{}"}


class SummaryTest(unittest.TestCase):
    def test_a_source_with_natural_and_dc_is_named(self):
        dump = {
            "hooks": {"create:SavingThrowRolledEvent": True, "post:ServerRoll": "unknown system", "tick": True},
            "frames": 120,
            "recs": [
                rec(
                    "post:ServerStats",
                    "SavingThrowRolledEvent",
                    "ConditionRoll.Difficulty=13",
                    "ConditionRoll.Roll.StatsRoll.Result.NaturalRoll=7",
                    "Success=false",
                ),
                rec("tick", "RequestedRoll", "NaturalRoll=15", "DC=10"),
                rec("osi", "RollResult", "1=GAMEPLAY_SpellSavingThrowSave"),
                rec("tick", "SpellCastRolls"),
            ],
            "trace": {"esv::stats::SavingThrowRolledEventOneFrameComponent": 2},
        }
        lines = probe.summarize(dump)
        save = next(line for line in lines if "SavingThrowRolledEvent" in line)
        self.assertTrue(save.startswith("NATURAL+DC"), save)
        self.assertIn("natural [7] dc [13]", save)
        self.assertTrue(next(line for line in lines if "RollResult" in line).startswith("-"))
        self.assertTrue(next(line for line in lines if "SpellCastRolls" in line).startswith("-"))
        self.assertIn("post:ServerRoll", lines[-2])  # a hook that failed is listed
        self.assertIn("SavingThrowRolledEventOneFrameComponent", lines[-1])

    def test_second_pass_sources(self):
        """The re-read SpellCastRolls and the applied status name their natural/DC; the status read counts show."""
        lines = probe.summarize(
            {
                "recs": [
                    rec("tick", "SpellCastRolls"),
                    rec(
                        "change",
                        "SpellCastRolls",
                        "Rolls.1.Casts.1.Hit.ConditionRolls#=1",
                        "Rolls.1.Casts.1.Hit.ConditionRolls.1.Difficulty=12",
                        "Rolls.1.Casts.1.Hit.ConditionRolls.1.Roll.Result.NaturalRoll=14",
                    ),
                    rec(
                        "status",
                        "COMMAND_HALT",
                        "Status.ConditionRolls.1.Difficulty=13",
                        "Status.ConditionRolls.1.Roll.Result.NaturalRoll=3",
                    ),
                ],
                "status": {"read": 5, "rolls": 1, "failed": 2, "error": "no status X"},
            }
        )
        change = next(line for line in lines if line.split()[1] == "change")
        self.assertTrue(change.startswith("NATURAL+DC"), change)
        self.assertIn("natural [14] dc [12]", change)
        self.assertIn("natural [3] dc [13]", next(line for line in lines if "COMMAND_HALT" in line))
        self.assertIn("5 read, 1 with ConditionRolls, 2 failed (last error: no status X)", lines[-3])

    def test_zero_is_no_value(self):
        """A component's unset NaturalRoll/DC reads 0: not a natural."""
        lines = probe.summarize(
            {
                "recs": [
                    rec(
                        "create",
                        "HitResultEvent",
                        "Hit.ConditionRolls.1.Difficulty=0",
                        "Hit.ConditionRolls.1.Roll.Result.NaturalRoll=0",
                    )
                ]
            }
        )
        self.assertTrue(lines[0].startswith("-"), lines[0])

    def test_not_installed(self):
        self.assertEqual(probe.summarize({"error": "not installed"}), ["ERROR not installed"])


if __name__ == "__main__":
    unittest.main()
