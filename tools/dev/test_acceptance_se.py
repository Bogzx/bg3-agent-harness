"""acceptance.py helpers of se's items, offline (live window 20261016-183253 follow-ups).

    uv run --project bridge python -m unittest tools/dev/test_acceptance_se.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "bridge" / "src"))
import acceptance as A  # noqa: E402

LUA = HERE.parents[1] / "mod/Mods/BG3AI/ScriptExtender/Lua/Client/UIActions.lua"
HINT = re.search(r'A\.SAVELOAD_HINT = "(.*)"', LUA.read_text()).group(1)


class UI3Refused(unittest.TestCase):
    # the three mod refusals the item sends, built as Client/UIActions.lua refuseSaveLoad builds them
    MSGS = ["ui.command QuickLoadCommand refused (quickload)" + HINT,
            "F8 is quickload: refused" + HINT,
            "ui.command QuickSaveCommand refused (quicksave)" + HINT]

    def test_each_mod_refusal_counts(self):
        for m in self.MSGS:
            self.assertTrue(A.ui3_refused(m), m)
            self.assertTrue(A.ui3_refused("RPC error: " + m), m)   # the bridge's wrapper prefix

    def test_window1_failure_was_the_cut(self):
        # 10-16 18:32: the old verdict read str(e)[:90], which ends at "go through save_game /"
        cut = self.MSGS[0][:90]
        self.assertTrue(cut.endswith("save_game /"), cut)
        self.assertFalse(A.ui3_refused(cut))

    def test_other_errors_do_not_count(self):
        self.assertFalse(A.ui3_refused("widget not open: *"))
        self.assertFalse(A.ui3_refused("no command 'QuickLoadCommand' on gui::DCHotBar"))


class HoverCompare(unittest.TestCase):
    T = ["rotate_occluded_right", "rotate_occluded_left"]

    def test_window1_shape_fails_now(self):
        # 10-16 18:32 e7: new 19 tries 1.60 s with two occluded turns, old 1 try 0.12 s with none: it PASSed then
        ok, why = A.hover_compare(((904, 742), 1, 0.12, None, []), ((904, 742), 19, 1.60, "cursor", self.T))
        self.assertFalse(ok)
        self.assertIn("turned the camera", why)

    def test_same_first_point(self):
        self.assertEqual(A.hover_compare(((1, 1), 1, 0.12, None, []), ((1, 1), 1, 0.13, "cursor", [])), (True, ""))

    def test_more_tries_where_old_hit_early(self):
        ok, why = A.hover_compare(((1, 1), 3, 0.4, None, []), ((1, 1), 5, 0.5, "cursor", []))
        self.assertFalse(ok)
        self.assertIn("more tries", why)

    def test_occluded_target_new_may_turn(self):
        ok, _ = A.hover_compare(((1, 1), 82, 12.6, None, self.T), ((1, 1), 44, 5.4, "cursor", self.T))
        self.assertTrue(ok)

    def test_new_miss_or_slower(self):
        self.assertFalse(A.hover_compare(((1, 1), 1, 0.1, None, []), (None, 50, 8.0, "cursor", self.T))[0])
        self.assertFalse(A.hover_compare(((1, 1), 30, 2.0, None, []), ((1, 1), 30, 2.6, "frames", []))[0])


class UndoCamera(unittest.TestCase):
    def test_net_turn_and_zoom_undone(self):
        from unittest import mock
        from bg3ai import clicker
        inp = mock.Mock()
        inp.window_size.return_value = (1920, 1080)
        with mock.patch.object(clicker, "_turn") as turn, mock.patch.object(clicker, "settle_camera"), \
                mock.patch.object(A, "S", mock.Mock()):
            A.undo_search_camera(inp, "u", ["zoom_out", "rotate_occluded_right", "rotate_occluded_left"])
        turn.assert_called_once()
        self.assertEqual(turn.call_args.args[-1], 1)          # +1 - 2 = -1 net -> turn back +1
        inp.wheel.assert_called_once_with(2, 960, 540)

    def test_nothing_to_undo(self):
        from unittest import mock
        from bg3ai import clicker
        inp = mock.Mock()
        with mock.patch.object(clicker, "_turn") as turn:
            A.undo_search_camera(inp, "u", ["center", "rotate_left"])
        turn.assert_not_called()
        inp.wheel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
