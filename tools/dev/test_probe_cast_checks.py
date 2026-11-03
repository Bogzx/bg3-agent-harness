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
        "area": "Thunderwave",
        "area_caster": "Gale",
        "two_step": "Revivify",
        "two_step_who": "Shadowheart",
        "two_step_target": "",
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

    def test_switch_per_mode(self):
        self.assertEqual(probe.switch("off"), "false")
        self.assertEqual(probe.switch("all"), "true")
        self.assertEqual(probe.switch("IgnoreTargetChecks"), '{"IgnoreTargetChecks"}')
        self.assertEqual(probe.switch("default"), "Mods.BG3AI.BG3AI.Actions.CAST_DROP_CHECKS")
        with self.assertRaises(ValueError):
            probe.switch("on")

    def test_summary_per_mode_reads_cases_only_when_the_control_casts(self):
        rows = [
            {"case": "control", "mode": "off", "verdict": "cast"},
            {"case": "control", "mode": "IgnoreTargetChecks", "verdict": "cast"},
            {"case": "control", "mode": "IgnoreCastChecks", "verdict": "refused"},
            {"case": "hold_person", "mode": "off", "verdict": "cast"},
            {"case": "hold_person", "mode": "IgnoreTargetChecks", "verdict": "refused"},
            {"case": "hold_person", "mode": "IgnoreCastChecks", "verdict": "refused"},
            {"case": "rage_ooc", "mode": "off", "verdict": "cast"},
            {"case": "rage_ooc", "mode": "IgnoreTargetChecks", "verdict": "cast"},
        ]
        lines = probe.summary(rows)
        self.assertEqual(lines[0], "mode IgnoreTargetChecks: control cast -> usable")
        self.assertIn("the engine re-checks once IgnoreTargetChecks is gone", lines[1])
        self.assertIn("the guard's Lua check is the fix", lines[2])
        self.assertEqual(
            lines[3], "mode IgnoreCastChecks: control refused -> can't be dropped (it stops a normal cast)"
        )
        self.assertIn("not read (the mode isn't usable)", lines[4])

    def test_live_plan_rage_first_then_the_fight_optional_cases_need_their_args(self):
        live = probe.plan("live", args(heavy=""))
        self.assertEqual(
            [c["case"] for c in live], ["rage_ooc", "control", "shout", "area", "plain", "turn_undead", "hide"]
        )
        self.assertEqual((live[0]["need"], live[0]["expect"]), ("no_combat", "refused"))
        self.assertEqual(live[1]["need"], "any")  # out of combat the control attack starts the fight
        full = probe.plan("live", args(two_step_target="e9"))
        self.assertEqual(full[-2]["case"], "two_step")
        self.assertEqual((full[-2]["target"], full[-2]["pos2"]), ("e9", True))
        self.assertEqual((full[-1]["case"], full[-1]["expect"]), ("heavy_shove", "refused"))

    def test_expected_marks_pass_fail_and_the_engine_reason(self):
        rows = [
            {
                "case": "rage_ooc",
                "expect": "refused",
                "verdict": "refused",
                "text": "use_ability: FAILED — the game refused the cast: it can't be cast now (its requirements aren't"
                " met) (the game's CantCast)",
            },
            {"case": "control", "expect": "cast", "verdict": "cast", "text": "use_ability: done"},
            {
                "case": "shout",
                "expect": "cast",
                "verdict": "refused",
                "text": "use_ability: FAILED — the game refused the cast (no reason given)",
            },
            {"case": "heavy_shove", "expect": "refused", "verdict": "cast", "text": "use_ability: done"},
            {"case": "hide", "expect": "cast", "verdict": "not run", "text": "not its turn"},
            {"case": "x", "mode": "off", "verdict": "cast"},
        ]
        lines = probe.expected(rows)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("PASS    rage_ooc") and lines[0].endswith("(engine reason read)"))
        self.assertTrue(lines[1].startswith("PASS    control"))
        self.assertTrue(lines[2].startswith("FAIL    shout") and lines[2].endswith("(no engine reason read)"))
        self.assertIn("only the guard's CanShoveWeight stops it", lines[3])
        self.assertTrue(lines[4].startswith("not run hide"))


if __name__ == "__main__":
    unittest.main()
