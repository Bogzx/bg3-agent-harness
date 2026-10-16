#!/usr/bin/env python3
"""Tests for tools/steam_paths.py against fake Steam layouts.  Run: python3 -m unittest tools/test_steam_paths.py"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import steam_paths as sp  # noqa: E402

VDF = """"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"%(root)s"
\t\t"apps"
\t\t{
\t\t\t"1628350"\t\t"0"
\t\t}
\t}
\t"1"
\t{
\t\t"path"\t\t"%(lib)s"
\t\t"apps"
\t\t{
\t\t\t"1086940"\t\t"0"
\t\t}
\t}
}
"""


def manifest(path: Path, appid: str, installdir: str, buildid: str = "25605617"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"installdir"\t\t"{installdir}"\n'
                    f'\t"buildid"\t\t"{buildid}"\n\t"InstalledDepots"\n\t{{\n\t}}\n}}\n')


class SteamPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def debian_layout(self):
        """Debian's steam-installer: ~/.steam/steam -> ~/.steam/debian-installation, game in a 2nd library."""
        root = self.home / ".steam/debian-installation"
        lib = Path(self.tmp.name) / "SteamLibrary"
        (root / "config").mkdir(parents=True)
        (root / "steamapps/common/SteamLinuxRuntime_sniper").mkdir(parents=True)
        manifest(root / "steamapps/appmanifest_1628350.acf", "1628350", "SteamLinuxRuntime_sniper")
        manifest(lib / "steamapps/appmanifest_1086940.acf", "1086940", "Baldurs Gate 3")
        (lib / "steamapps/common/Baldurs Gate 3").mkdir(parents=True)
        (root / "config/libraryfolders.vdf").write_text(VDF % {"root": root, "lib": lib})
        (self.home / ".steam/steam").symlink_to(root)
        return root, lib

    def test_parse_vdf(self):
        d = sp.parse_vdf('"a"\n{\n\t"b"\t"c"\n\t"d"\n\t{\n\t\t"e"\t"f\\\\g"\n\t}\n}\n')
        self.assertEqual(d, {"a": {"b": "c", "d": {"e": "f\\g"}}})

    def test_libraryfolders_second_library(self):
        root, lib = self.debian_layout()
        self.assertEqual(sp.game_dir({}, self.home), lib / "steamapps/common/Baldurs Gate 3")
        self.assertEqual(sp.sniper_dir({}, self.home), root / "steamapps/common/SteamLinuxRuntime_sniper")
        self.assertEqual(sp.manifest({}, self.home), lib / "steamapps/appmanifest_1086940.acf")
        self.assertEqual(sp.buildid({}, self.home), "25605617")

    def test_env_overrides(self):
        self.debian_layout()
        self.assertEqual(sp.game_dir({"BG3_DIR": "/g"}, self.home), Path("/g"))
        self.assertEqual(sp.game_dir({"BG3AI_GAME_DIR": "/h"}, self.home), Path("/h"))
        self.assertEqual(sp.game_dir({"BG3_DIR": "/g", "BG3AI_GAME_DIR": "/h"}, self.home), Path("/g"))
        self.assertEqual(sp.sniper_dir({"BG3AI_SNIPER_DIR": "/s"}, self.home), Path("/s"))

    def test_steamapps_vdf_location(self):
        """~/.steam/steam/steamapps/libraryfolders.vdf (written by Steam alongside config/)."""
        lib = Path(self.tmp.name) / "lib2"
        root = self.home / ".local/share/Steam"
        (root / "steamapps").mkdir(parents=True)
        manifest(lib / "steamapps/appmanifest_1086940.acf", "1086940", "Baldurs Gate 3")
        (root / "steamapps/libraryfolders.vdf").write_text(VDF % {"root": root, "lib": lib})
        self.assertEqual(sp.game_dir({}, self.home), lib / "steamapps/common/Baldurs Gate 3")
        # no sniper manifest anywhere: next to the game
        self.assertEqual(sp.sniper_dir({}, self.home), lib / "steamapps/common/SteamLinuxRuntime_sniper")

    def test_default_when_nothing(self):
        self.assertEqual(sp.game_dir({}, self.home),
                         self.home / ".local/share/Steam/steamapps/common/Baldurs Gate 3")
        self.assertEqual(sp.buildid({}, self.home), "")

    def test_dir_without_manifest(self):
        root = self.home / ".local/share/Steam"
        (root / "steamapps/common/Baldurs Gate 3").mkdir(parents=True)
        self.assertEqual(sp.game_dir({}, self.home), root / "steamapps/common/Baldurs Gate 3")

    def test_profile_saves_se(self):
        prof = self.home / ".local/share/Larian Studios/Baldur's Gate 3"
        self.assertEqual(sp.profile_dir({}, self.home), prof)
        self.assertEqual(sp.saves_dir({}, self.home), prof / "PlayerProfiles/Public/Savegames/Story")
        self.assertEqual(sp.se_dir({}, self.home), prof / "Script Extender")
        env = {"BG3AI_PROFILE_DIR": "/p"}
        self.assertEqual(sp.saves_dir(env, self.home), Path("/p/PlayerProfiles/Public/Savegames/Story"))
        self.assertEqual(sp.se_dir(env, self.home), Path("/p/Script Extender"))
        env |= {"BG3AI_SAVE_DIR": "/s", "BG3AI_SE_DIR": "/e"}
        self.assertEqual((sp.saves_dir(env, self.home), sp.se_dir(env, self.home)), (Path("/s"), Path("/e")))

    def test_mirrors_agree(self):
        """server.py (the bridge doesn't import tools/) and launch_game.sh resolve the profile like profile_dir()."""
        repo = Path(sp.__file__).resolve().parents[1]
        server = (repo / "bridge/src/bg3ai/server.py").read_text()
        for needle in ("BG3AI_SAVE_DIR", "BG3AI_PROFILE_DIR", str(sp.PROFILE_REL), str(sp.SAVES_REL)):
            self.assertIn(needle, server)
        self.assertIn("steam_paths.py\" profile", (repo / "runner/launch_game.sh").read_text())

    def test_cli(self):
        root, lib = self.debian_layout()
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        out = subprocess.run([sys.executable, str(Path(sp.__file__)), "data"], env=env,
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, str(lib / "steamapps/common/Baldurs Gate 3/Data"))
        out = subprocess.run([sys.executable, str(Path(sp.__file__)), "saves"], env=env | {"BG3AI_PROFILE_DIR": "/p"},
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "/p/PlayerProfiles/Public/Savegames/Story")


if __name__ == "__main__":
    unittest.main()
