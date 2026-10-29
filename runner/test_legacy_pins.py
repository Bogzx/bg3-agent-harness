"""runner/legacy_series.py current_pins (a legacy series run's pins) on a fake Steam library in a temp dir: a game copy
whose files match the pinned build's fingerprint (download_depot) pins the pinned build, as the bench's run pins do."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir)
import play  # noqa: F401  (puts the runner's packages on sys.path)
import legacy_series as lser
from bg3harness import game_fingerprint
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv


class CurrentPins(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        self.game = self.tmp / "steamapps" / "common" / "Baldurs Gate 3"
        (self.game / "bin").mkdir(parents=True)
        (self.game / "bin" / "bg3").write_bytes(b"pinned exe")
        self.acf = self.tmp / "steamapps" / "appmanifest_1086940.acf"
        (self.tmp / "tools" / "dev").mkdir(parents=True)
        (self.tmp / "tools" / "dev" / "first_launch.sh").write_text("WANT_BUILD=25605617\n")
        fp = game_fingerprint.make(self.game, ["bin/bg3"])
        (self.tmp / "tools" / "dev" / "game_fingerprints.json").write_text(json.dumps({"25605617": fp}))
        for p in (
            mock.patch.object(renv, "REPO", self.tmp),
            mock.patch.object(rcode, "MOD_DIR", self.tmp / "mod"),
            mock.patch.object(rcode, "MOD_LIVE", self.tmp / "mod-live"),
            mock.patch.dict(os.environ, {"BG3_DIR": str(self.game), "BG3AI_BG3LE_LIB": str(self.tmp / "d" / "x.so")}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def manifest(self, build: str) -> None:
        self.acf.write_text(f'"AppState"\n{{\n\t"buildid"\t\t"{build}"\n}}\n')

    def test_a_steam_install_of_the_pin(self):
        self.manifest("25605617")
        pins = lser.current_pins()
        self.assertEqual((pins["game_build"], pins["game_build_by"]), ("25605617", "appmanifest"))
        self.assertIn("versions", pins)

    def test_a_depot_copy_of_the_pinned_build(self):
        self.manifest("26000000")  # Steam's manifest names the build it updated to
        pins = lser.current_pins()
        self.assertEqual((pins["game_build"], pins["game_build_by"]), ("25605617", "fingerprint"))
        self.acf.unlink()  # a copy outside any Steam library
        self.assertEqual(lser.current_pins()["game_build"], "25605617")

    def test_other_files_are_the_manifest_build(self):
        self.manifest("26000000")
        (self.game / "bin" / "bg3").write_bytes(b"other  exe")
        pins = lser.current_pins()
        self.assertEqual((pins["game_build"], pins["game_build_by"]), ("26000000", "appmanifest"))


if __name__ == "__main__":
    unittest.main()
