import os
import tempfile
import time
import unittest
from pathlib import Path

from bg3ai import saves


class SavesTest(unittest.TestCase):
    def test_owner_of(self):
        self.assertEqual(saves.owner_of("Claude-45912619919__S001-end"), "Claude")
        self.assertEqual(saves.owner_of("Claude-3291261994__S001-start"), "Claude")
        self.assertEqual(saves.owner_of("Tav-12912619935__QuickSave_1"), "Tav")
        self.assertEqual(saves.owner_of("Shadow-Heart-12__x"), "Shadow-Heart")

    def test_load_args_index_and_date(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = time.mktime((2026, 10, 9, 22, 0, 0, 0, 0, -1))
            for name, minute in [("Claude-1__AutoSave_0", 29), ("Claude-2__S001-start", 32),
                                 ("Claude-3__S001-imps-done", 39), ("Claude-4__S001-end", 45),
                                 ("Tav-5__S002-end", 50)]:
                d = root / name
                d.mkdir()
                os.utime(d, (base + minute * 60, base + minute * 60))
            a = saves.load_args(root, "Claude-3__S001-imps-done")
            self.assertEqual(a["owner"], "Claude")
            self.assertEqual(a["index"], 2)  # newest Claude save first; Tav's newer save doesn't count
            self.assertEqual(a["date"], {"y": 2026, "m": 10, "d": 9, "H": 22, "M": 39})
            self.assertEqual(a["action"], "load")
            self.assertEqual(saves.load_args(root, "Claude-4__S001-end")["index"], 1)
            self.assertNotIn("index", saves.load_args(root, "Claude-9__gone"))

    def test_save_file_and_mtime_from_the_lsv(self):
        # 'Claude-4101260944__S017-pre-gut/S017-pre-gut.lsv': named after the label, not the folder
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "Claude-4101260944__S017-pre-gut"
            d.mkdir()
            lsv = d / "S017-pre-gut.lsv"
            lsv.write_bytes(b"x")
            self.assertEqual(saves.save_file(d), lsv)
            os.utime(lsv, (1000, 1000))
            os.utime(d, (5000, 5000))
            self.assertEqual(saves._mtime(d), 1000)
            odd = root / "Claude-1__x"
            odd.mkdir()
            (odd / "renamed.lsv").write_bytes(b"x")
            self.assertEqual(saves.save_file(odd).name, "renamed.lsv")
            empty = root / "Claude-2__y"
            empty.mkdir()
            self.assertFalse(saves.save_file(empty).exists())


if __name__ == "__main__":
    unittest.main()
