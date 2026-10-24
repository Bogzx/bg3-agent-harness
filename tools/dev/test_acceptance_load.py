"""acceptance core.load_verified: a load counts only when load_game says done and the game view is back (fix-live A1:
a 'TIMEOUT — timed out' load that left the Load page up was a PASS).

    python3 -m unittest tools/dev/test_acceptance_load.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import core

DONE = "load_game: done — loaded=S027-start"
TIMEOUT = (
    "load_game: TIMEOUT — timed out — name=Claude-4810126690__S027-start saves=529 picked=388 stage=verify "
    "pane=10/10/2026 16:14 tried=33"
)


class LoadVerified(unittest.TestCase):
    def verify(self, out: str, screen: str) -> tuple[bool, list[dict]]:
        rows: list[dict] = []
        party = {"region": "Moonrise", "party": [{"name": "Claude"}]}
        with (
            mock.patch.object(core, "set_marker"),
            mock.patch.object(core, "took", return_value=(out, 31.5)),
            mock.patch.object(core, "status", return_value=party),
            mock.patch.object(core, "ui_screen", return_value=screen),
            mock.patch.object(core, "marker_gone", return_value=True),
            mock.patch.object(core, "log"),
            mock.patch.object(core.time, "sleep"),
            mock.patch.object(core, "item", lambda n, v, e: rows.append({"item": n, "verdict": v, "ev": e})),
        ):
            ok = core.load_verified("S027-start", record=True)
        return ok, rows

    def test_a_done_load_on_the_game_view_passes(self):
        ok, rows = self.verify(DONE, "game")
        self.assertTrue(ok)
        self.assertEqual(rows[0]["verdict"], "PASS")

    def test_a_timed_out_load_fails(self):
        # reads.log 20261024-164812: the TIMEOUT left the Load page up, the marker read as gone, the party readable
        ok, rows = self.verify(TIMEOUT, "save_load")
        self.assertFalse(ok)
        self.assertEqual(rows[0]["verdict"], "FAIL")
        self.assertIn("screen=save_load", rows[0]["ev"])

    def test_a_done_load_off_the_game_view_fails(self):
        ok, _ = self.verify(DONE, "save_load")
        self.assertFalse(ok)

    def test_a_timed_out_load_on_the_game_view_still_fails(self):
        ok, _ = self.verify(TIMEOUT, "game")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
