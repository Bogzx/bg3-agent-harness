"""Offline tests for tools/dev/check_se.py's log verdicts and helpers (no game needed).

Run: uv run --project bridge python -m unittest tools/dev/test_check_se.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_se as C  # noqa: E402

HOOKED = "noesis: View at 0x7f0000001000\nlua: client state ready\n"


class LogVerdictTest(unittest.TestCase):
    def test_hooked(self):
        self.assertEqual(C.check_log_text(HOOKED)[0], "PASS")

    def test_not_hooked(self):
        v, d = C.check_log_text(HOOKED + "noesis: View::Update not hooked; Ext.UI will scan for the View\n")
        self.assertEqual(v, "FAIL")
        self.assertIn("not hooked", d)

    def test_no_frame_yet(self):
        self.assertEqual(C.check_log_text("bg3le: started\n")[0], "WARN")

    def test_crash(self):
        v, d = C.check_log_text(HOOKED + "crash: signal 11 at pc bg3+0x782a040\n")
        self.assertEqual(v, "FAIL")
        self.assertIn("signal 11", d)

    def test_latest_log(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()
            old = logs / "bg3le-20261009-100000.log"
            new = logs / "bg3le-20261009-110000.log.4242"
            old.write_text("a")
            new.write_text("b")
            os.utime(old, (time.time() - 100, time.time() - 100))
            self.assertEqual(C.latest_log(Path(d)), new)
            self.assertIsNone(C.latest_log(Path(d) / "none"))

    def test_buildinfo_sha(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "BUILDINFO").write_text("bg3le commit: x\nsha256: abc123\n")
            self.assertEqual(C.buildinfo_sha(Path(d)), "abc123")
            self.assertIsNone(C.buildinfo_sha(Path(d) / "none"))

    def test_no_game(self):
        self.assertFalse(C.alive(None))
        self.assertIsNone(C.loaded_lib(None))


if __name__ == "__main__":
    unittest.main()
