import os
import struct
import tempfile
import time
import unittest
from pathlib import Path

from bg3ai import lsf, lspk, saves


def at(y, mo, d, h, mi) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def meta_lsf(leader: str, saved: float) -> bytes:
    """meta.lsf as BG3 writes it: LeaderName and SaveTime (u64 epoch) on MetaData/MetaData."""
    return lsf.build(
        [("MetaData", -1), ("MetaData", 0)],
        [("TimeStamp", 5, 1, struct.pack("<I", 10490)), ("LeaderName", 23, 1, leader.encode() + b"\0"),
         ("SaveTime", 24, 1, struct.pack("<Q", int(saved)))],
    )


def make_save(root: Path, folder: str, mtime: float, meta: bytes | None = None, method: str = "none") -> Path:
    """A save folder whose .lsv is a v18 package with meta.lsf (then a filler member), or junk without meta."""
    d = root / folder
    d.mkdir()
    lsv = d / f"{folder.split('__', 1)[-1]}.lsv"
    filler = ("StorySave.bin", b"\0" * 4096, "none")
    lsv.write_bytes(lspk.build([filler, ("meta.lsf", meta, method)]) if meta is not None else b"junk")
    os.utime(lsv, (mtime, mtime))
    return d


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

    def test_read_meta(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saved = at(2026, 10, 10, 9, 48)
            d = make_save(root, "Claude-1__S027-start", at(2026, 10, 21, 23, 8), meta_lsf("Claude", saved))
            self.assertEqual(saves.read_meta(saves.save_file(d)), ("Claude", saved))
            self.assertIsNone(saves.read_meta(saves.save_file(make_save(root, "Claude-2__junk", 0))))
            self.assertIsNone(saves.read_meta(root / "Claude-9__gone" / "gone.lsv"))
            no_leader = lsf.build([("MetaData", -1), ("MetaData", 0)], [("SaveTime", 24, 1, struct.pack("<Q", 5))])
            self.assertIsNone(saves.read_meta(saves.save_file(make_save(root, "Claude-3__x", 0, no_leader))))
            if lspk.zstd is not None:  # BG3 4.x stores meta.lsf with zstd
                z = make_save(root, "Claude-4__z", 0, meta_lsf("Tav", at(2026, 10, 1, 1, 1)), "zstd")
                self.assertEqual(saves.read_meta(saves.save_file(z)), ("Tav", at(2026, 10, 1, 1, 1)))

    def test_load_args_from_meta_live5(self):
        # live5 §0.5: the page (sorted by SaveTime, grouped by LeaderName) had S027-start at 377, the mtime/prefix
        # count said 376: two probe copies with mtime 23:08 but saved 22:26/22:33, and a 'PROBE-…' folder whose
        # leader is Claude.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            copied = at(2026, 10, 21, 23, 8)
            make_save(root, "Claude-262112619943__PROBE-l3base", copied, meta_lsf("Claude", at(2026, 10, 21, 22, 26)))
            make_save(root, "Claude-332112619940__PROBE-l4seed", copied, meta_lsf("Claude", at(2026, 10, 21, 22, 33)))
            make_save(root, "PROBE-49111260919__S049-end", copied, meta_lsf("Claude", at(2026, 10, 11, 3, 49)))
            s030 = at(2026, 10, 10, 10, 14)
            make_save(root, "Claude-1__S030-end", s030, meta_lsf("Claude", s030))
            # an older save copied last (mtime newest of all): its meta date puts it last on the page
            make_save(root, "Claude-2__S027-start", at(2026, 10, 22, 1, 0), meta_lsf("Claude", at(2026, 10, 10, 9, 48)))
            make_save(root, "Tav-3__S002-end", at(2026, 10, 22, 2, 0), meta_lsf("Tav", at(2026, 10, 22, 2, 0)))
            a = saves.load_args(root, "Claude-2__S027-start")
            self.assertEqual((a["owner"], a["index"]), ("Claude", 5))
            self.assertEqual(a["date"], {"y": 2026, "m": 10, "d": 10, "H": 9, "M": 48})
            p = saves.load_args(root, "PROBE-49111260919__S049-end")
            self.assertEqual((p["owner"], p["index"]), ("Claude", 3))  # the page's group, not the folder prefix
            self.assertEqual(saves.load_args(root, "Claude-262112619943__PROBE-l3base")["index"], 2)
            # the old count (prefix + mtime) put S027-start first and left PROBE-… out of the group
            no_meta = saves.load_args(root, "Claude-2__S027-start", meta=lambda _: None)
            self.assertEqual(no_meta["index"], 1)

    def test_load_args_mixes_meta_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_save(root, "Claude-1__a", at(2026, 10, 9, 22, 0), meta_lsf("Claude", at(2026, 10, 9, 22, 0)))
            make_save(root, "Claude-2__unreadable", at(2026, 10, 9, 22, 30))  # no meta: folder prefix + mtime
            make_save(root, "Claude-3__b", at(2026, 10, 9, 21, 0), meta_lsf("Claude", at(2026, 10, 9, 23, 0)))
            self.assertEqual(saves.load_args(root, "Claude-2__unreadable")["index"], 2)
            self.assertEqual(saves.load_args(root, "Claude-3__b")["index"], 1)
            self.assertEqual(saves.load_args(root, "Claude-1__a")["index"], 3)


if __name__ == "__main__":
    unittest.main()
