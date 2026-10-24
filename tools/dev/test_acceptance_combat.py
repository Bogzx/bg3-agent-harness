"""The acceptance combat section's fight source (live2/live9: the walk to the shadow pack started no fight, so the
section played no turn), offline with a fake game.

    uv run --project bridge python -m unittest tools/dev/test_acceptance_combat.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))
from acceptance import combat, core  # noqa: E402


class FightSource(unittest.TestCase):
    def _run(self, active: bool) -> tuple[list, list, list]:
        loads, calls, got = [], [], []
        reads = iter([{"active": active}])  # the read after the load; then the fight is over (no turn loop)

        def try_rpc(op, *a, **k):
            return next(reads, {}) if op == "state.combat" else {}

        def call(name, **kw):
            calls.append(name)
            return f"{name}: done"

        fake_s = mock.Mock()
        fake_s.game.try_rpc = try_rpc
        with (
            mock.patch.object(combat, "S", fake_s),
            mock.patch.object(combat, "load_verified", lambda label, record=False: loads.append(label) or True),
            mock.patch.object(combat, "call", call),
            mock.patch.object(combat, "item", lambda *a: got.append(a)),
            mock.patch.object(core, "item", lambda *a: got.append(a)),
            mock.patch.object(combat, "combat_compaction_check", lambda raws: None),
            mock.patch.object(combat, "melee_probe", lambda: None),
            mock.patch("bg3ai.jobpush.shared"),
        ):
            combat.sec_combat()
        return loads, calls, got

    def test_the_fight_save_is_loaded_and_played(self):
        loads, calls, _ = self._run(active=True)
        self.assertEqual(loads, [core.FIGHT_SAVE, core.BASE])  # BASE reloaded after
        self.assertIn("get_combat_state", calls)
        self.assertNotIn("travel_to", calls)

    def test_no_fight_after_the_load_skips_and_names_the_save(self):
        loads, calls, got = self._run(active=False)
        self.assertEqual(loads, [core.FIGHT_SAVE, core.BASE])
        self.assertEqual(calls, [])
        self.assertIn(("combat_turn on a safe fight", "SKIP", f"{core.FIGHT_SAVE} loaded with no fight on"), got)

    def test_fight_save_checked_by_check(self):
        self.assertEqual(core.SAVE_NEEDS[core.FIGHT_SAVE]["level"], "CTY_Main_A")


if __name__ == "__main__":
    unittest.main()
