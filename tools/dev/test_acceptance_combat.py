"""The acceptance combat section's fight source (live2/live9: the walk to the shadow pack started no fight, so the
section played no turn), offline with a fake game.

    uv run --project bridge python -m unittest tools/dev/test_acceptance_combat.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast
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


class HudEndReads(unittest.TestCase):
    def test_counted_names_the_hud_ends_reads(self):
        """Live window 2026-10-24: 'batched step checks' counted the HUD end's check before its press and its polls
        (an end the game ignored) as step checks. counted() now returns them apart."""
        from bg3ai.tools import combat as ct

        fake_s = mock.Mock()
        fake_s.game.rpc = lambda op, *a, **k: {}

        def hud(char, r):  # its check before the press, the press, two polls
            for op in ("state.combat", "ui.command", "state.combat", "state.combat"):
                combat.S.game.rpc(op)
            return r

        def took(name, **kw):
            combat.S.game.rpc("state.combat")  # end_turn's own read
            ct._end_turn_by_hud("c", cast(Any, "r"))
            combat.S.game.rpc("state.combat")  # the NEXT line's
            return "out", 0.1

        with (
            mock.patch.object(combat, "S", fake_s),
            mock.patch.object(combat, "took", took),
            mock.patch.object(ct, "_end_turn_by_hud", hud),
        ):
            out, _, counts, hud_n = combat.counted("combat_turn")
            self.assertIs(ct._end_turn_by_hud, hud)  # restored after the call
        self.assertEqual((out, counts["state.combat"], counts["ui.command"], hud_n), ("out", 5, 1, 3))


if __name__ == "__main__":
    unittest.main()
