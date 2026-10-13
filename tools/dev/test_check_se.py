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

HOOKED = ("noesis: 18 of 18 View input entry points hooked\n"
          "noesis: View at 0x7f0000001000\nlua: client state ready\n")


class LogVerdictTest(unittest.TestCase):
    def test_hooked(self):
        self.assertEqual(C.check_log_text(HOOKED)[0], "PASS")

    def test_not_hooked(self):
        v, d = C.check_log_text(HOOKED + "noesis: View::Update not hooked; Ext.UI will scan for the View\n")
        self.assertEqual(v, "FAIL")
        self.assertIn("not hooked", d)

    def test_input_hooks(self):
        # 0003: the input entry points hold the UI lock; missing or partial is a WARN
        self.assertEqual(C.check_log_text("noesis: View at 0x7f0000001000\n")[0], "WARN")
        v, d = C.check_log_text(HOOKED.replace("18 of 18", "16 of 18"))
        self.assertEqual(v, "WARN")
        self.assertIn("16 of 18", d)

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



class ChildrenAndLootTest(unittest.TestCase):
    def test_children_verdicts(self):
        self.assertEqual(C.children_verdict(None)[0], "FAIL")
        v, d = C.children_verdict({"ui": False, "mod": False})
        self.assertEqual(v, "FAIL")
        self.assertIn("not loaded", d)
        v, d = C.children_verdict({"ui": False, "mod": True})
        self.assertEqual(v, "FAIL")
        self.assertIn("UI.lua", d)
        self.assertIn("Mods.BG3AI.BG3AI", C.CHILDREN_LUA)  # evals run in the global env: no bare BG3AI there
        self.assertEqual(C.children_verdict({"ui": True, "patched": False})[0], "WARN")
        st = {"ui": True, "patched": True, "cr": "Grid", "count": 25, "collection": 50, "visual": 3}
        self.assertEqual(C.children_verdict(st)[0], "PASS")
        v, d = C.children_verdict(dict(st, collection=0))
        self.assertEqual(v, "WARN")
        self.assertIn("PANEL_TYPES", d)

    def test_loot_target_prefers_corpse_within_reach(self):
        near = {"characters": [{"uuid": "c-far", "dead": True, "dist": 4.0, "relation": "hostile", "pos": [4, 0, 0]},
                               {"uuid": "c-near", "dead": True, "dist": 2.0, "relation": "hostile", "pos": [2, 0, 0]},
                               {"uuid": "alive", "dead": False, "dist": 1.0, "pos": [1, 0, 0]}],
                "items": [{"uuid": "box", "kind": "container", "dist": 1.0, "opened": True, "pos": [1, 0, 0]},
                          {"uuid": "owned", "kind": "container", "dist": 0.5, "owned": True, "pos": [0.5, 0, 0]}]}
        self.assertEqual(C.loot_target(near, [0, 0, 0])["uuid"], "c-near")
        near["characters"] = []
        self.assertEqual(C.loot_target(near, [0, 0, 0])["uuid"], "box")  # opened ones count; owned never
        near["items"] = near["items"][1:]
        self.assertIsNone(C.loot_target(near, [0, 0, 0]))

class BuildCheckTest(unittest.TestCase):
    """hook.build (UI-1): a dist whose BUILDINFO lacks a patches/bg3le patch FAILs."""

    def run_check(self, text: str | None) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as d:
            if text is not None:
                Path(d, "BUILDINFO").write_text(text)
            C.results.clear()
            C.check_build(Path(d))
            return C.results[-1]

    def test_missing_patch_fails(self):
        names = sorted(p.name for p in C.bg3le_buildinfo.PATCHES.glob("*.patch"))
        text = "".join(f"patch: {n} {C.bg3le_buildinfo.repo_patches()[n]}\n" for n in names[:-1])
        v, name, detail = self.run_check(text)
        self.assertEqual((v, name), ("FAIL", "hook.build"))
        self.assertIn(f"missing {names[-1]}", detail)
        self.assertIn("tools/build_bg3le.sh", detail)

    def test_current_passes(self):
        text = "".join(f"patch: {n} {h}\n" for n, h in C.bg3le_buildinfo.repo_patches().items())
        self.assertEqual(self.run_check(text)[0], "PASS")

    def test_no_buildinfo_fails(self):
        self.assertEqual(self.run_check(None)[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
