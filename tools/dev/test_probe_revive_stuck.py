"""Offline tests for tools/dev/probe_revive_stuck.py (no game needed): the Lua it fills in, the diff and watch lines.

Run: uv run pytest -q tools/dev/test_probe_revive_stuck.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_revive_stuck as probe


class ProbeTest(unittest.TestCase):
    def test_move_code_walks_to_a_member_or_along_x(self):
        code = probe.move_code("Tav", "Shadowheart", 3.0)
        self.assertIn('local c = P.who("Tav")', code)
        self.assertIn('if true then\n    x, y, z = Osi.GetPosition(P.who("Shadowheart"))', code)
        code = probe.move_code("Tav", None, 2.5)
        self.assertIn("if false then", code)
        self.assertIn("x, y, z = px + 2.5, py, pz", code)

    def test_forms_code_puts_the_uuid_in_and_names_the_call(self):
        code = probe.forms_code("TemplateAddTo", "Shadowheart", [[probe.SCROLL_TEMPLATE, "$c", 1, 0], ["$c"]])
        self.assertIn(f'local forms = {{{{"{probe.SCROLL_TEMPLATE}", "$c", 1, 0}}, {{"$c"}}}}', code)
        self.assertIn('pcall(Osi["TemplateAddTo"], table.unpack(args))', code)

    def test_jump_code_has_both_forms(self):
        code = probe.jump_code("Tav", 250.0, -0.3, 195.0)
        self.assertIn('"Projectile_Jump", 250.0, -0.3, 195.0, 0)', code)
        self.assertIn('"Projectile_Jump", 250.0, -0.3, 195.0)', code)

    def test_diff_names_components_and_changed_fields(self):
        a = {"label": "base", "comps": ["CanMove", "Health"], "action": "-,-,-", "hp": 18, "pos": [1, 2, 3]}
        b = {
            "label": "revived",
            "comps": ["Health", "JumpFollow"],
            "action": "TeleportFalling,-,-",
            "hp": 18,
            "pos": [4, 5, 6],
        }
        self.assertEqual(
            probe.diff_snaps(a, b),
            [
                "components added: JumpFollow",
                "components removed: CanMove",
                'action: "-,-,-" -> "TeleportFalling,-,-"',
            ],
        )
        self.assertEqual(probe.diff_snaps(a, dict(a, pos=[0, 0, 0])), ["no difference"])

    def test_read_snaps_keys_by_label_last_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snaps.jsonl"
            p.write_text("\n".join(json.dumps(s) for s in [{"label": "a", "hp": 1}, {"label": "a", "hp": 2}]) + "\n")
            self.assertEqual(probe.read_snaps(p)["a"]["hp"], 2)

    def test_watch_lines_time_and_component_changes(self):
        dump = {
            "t0": 1000,
            "recs": [{"t": 3500, "c": "0123456789ab", "sig": "hp=0 dead=1", "add": ["Death"], "rem": ["CanMove"]}],
        }
        self.assertEqual(probe.watch_lines(dump), ["+   2.50s 01234567 hp=0 dead=1 +[Death] -[CanMove]"])

    def test_moved_m(self):
        self.assertEqual(probe.moved_m([0, 0, 0], [3, 0, 4]), 5.0)


if __name__ == "__main__":
    unittest.main()
