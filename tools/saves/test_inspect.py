"""Offline tests for the save inspector's report (tools/saves/inspect.py), on tiny synthetic saves. The readers it
uses (bg3ai.lspk, bg3ai.lsf) are tested in bridge/tests/test_save_readers.py.

Run: uv run python -m unittest discover -s tools/saves -p 'test_*.py'
"""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bg3ai import lsf, lspk

HERE = Path(__file__).resolve().parent

# inspect.py shares its name with the stdlib module: load it under another name.
_spec = importlib.util.spec_from_file_location("saves_inspect", HERE / "inspect.py")
inspect_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inspect_mod)

STEAM = "76561198000000042"


def s(text: str) -> bytes:
    return text.encode() + b"\0"


META = lsf.build(
    # the real layout: region MetaData, its node MetaData, Mods under ModuleSettings, versions under GameVersions
    [("MetaData", -1), ("MetaData", 0), ("ModuleShortDesc", 1), ("GameVersions", 1), ("GameVersion", 3)],
    [
        ("LeaderName", 23, 1, s("Claude")),
        ("GameID", 22, 1, s("8d52e638-ad8a-6549-ff43-28e78da3a8af")),
        ("Seed", 4, 1, struct.pack("<i", 1071409710)),
        ("Modded", 19, 1, b"\0"),
        ("Name", 23, 2, s("GustavX")),
        ("UUID", 31, 2, bytes(range(16))),
        ("Object", 23, 4, s("4.1.1.7631656")),
    ],
)
GLOBALS = lsf.build([("Globals", -1)], [("Path", 23, 0, s("/home/someone/.local/share/Larian Studios/x"))])
SAVEINFO = json.dumps(
    {
        "Save Name": "S007",
        "Game Version": "4.1.1.7631656",
        "Platform": "Steam",
        "Difficulty": ["DifficultyMedium", "RulesetLarian"],  # live6's Balanced saves
        "Active Party": {"Characters": [{"Origin": "Gale", "Race": "Human", "Level": 1, "Classes": []}]},
        "Contact": "someone@example.org",
    }
).encode()
STORY = b"\0Osi " + bytes(b ^ 0xAD for b in f"PlayerId={STEAM};".encode()) + b"\0"


def synthetic(extra: list | None = None) -> bytes:
    return lspk.build(
        [
            ("meta.lsf", META, "none"),
            ("SaveInfo.json", SAVEINFO, "zlib"),
            ("StorySave.bin", STORY, "lz4"),
            ("Globals.lsf", GLOBALS, "zlib"),
        ]
        + (extra or [])
    )


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "x.lsv"
        self.path.write_bytes(synthetic())

    def tearDown(self):
        self.tmp.cleanup()

    def test_report(self):
        r = inspect_mod.inspect_save(self.path)
        self.assertEqual(len(r["members"]), 4)
        self.assertEqual(r["meta"]["mods"][0]["Name"], "GustavX")
        self.assertEqual(r["meta"]["fields"]["LeaderName"], "Claude")
        self.assertEqual(r["meta"]["game_versions"], ["4.1.1.7631656"])
        self.assertEqual(r["save_info"]["Game Version"], "4.1.1.7631656")
        self.assertEqual(r["save_info"]["party"][0]["Origin"], "Gale")
        self.assertEqual(r["save_info"]["difficulty"], "Balanced")  # live6: DifficultyMedium is Balanced
        self.assertIsNone(inspect_mod.save_info(b'{"Difficulty": ["RulesetLarian"]}')["difficulty"])
        kinds = {(h["kind"], h["value"]): h["members"] for h in r["findings"]}
        self.assertEqual(kinds[("steam_id", "7656***********42")], ["StorySave.bin"])  # masked story string
        self.assertEqual(kinds[("user_path", "/home/<user>")], ["Globals.lsf"])
        self.assertEqual(kinds[("email", "s***@example.org")], ["SaveInfo.json"])
        self.assertNotIn(STEAM, json.dumps(r))

    def test_show_and_shallow(self):
        r = inspect_mod.inspect_save(self.path, show=True)
        self.assertIn(STEAM, [h["value"] for h in r["findings"]])
        shallow = inspect_mod.inspect_save(self.path, deep=False)
        self.assertEqual({h["kind"] for h in shallow["findings"]}, {"email"})

    def test_refuses_the_game_folder(self):
        game = Path(self.tmp.name) / "Larian Studios" / "x.lsv"
        game.parent.mkdir()
        game.write_bytes(synthetic())
        self.assertEqual(inspect_mod.main([str(game)]), 2)

    def test_script_runs_despite_its_stdlib_name(self):
        out = subprocess.run(
            [sys.executable, str(HERE / "inspect.py"), str(self.path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["meta"]["fields"]["LeaderName"], "Claude")


# live6 rows 19-22, bytes from the start saves' level caches: a Translate vec3 whose float bytes read `hB@j.rD`, a UUID
# ending `i@` right before the string `Cnst.Perception.Discovered`, and a NewAge ScratchBuffer holding both the float
# bytes and the game's own install path next to a heap pointer. Real data in a string or a buffer still counts.
INSTALL = b"\x80\xf1\xe4\xbdl\x04\0\0eamLibrary/steamapps/common/Baldurs Gate 3/Data/Mods/MainUI/GUI/Pages/x"
LEVEL = lsf.build(
    [("Templates", -1), ("GameObjects", 0)],
    [
        ("Translate", 12, 1, b"\xbc3\xddD\xfehB@j.rD"),
        ("MapKey", 31, 1, b"\xdc\xe9\xf2\x80,\xb1\xe6@\x87\xbeuL\xcc\xeci@"),
        ("Name", 22, 1, s("Cnst.Perception.Discovered")),
        ("NewAge", 25, 1, b"LSMF\xff\xff\xbc3\xddD\xfehB@j.rD\0\0" + INSTALL + b"\0"),
    ],
)
REAL = lsf.build(
    [("Templates", -1), ("GameObjects", 0)],
    [
        ("Name", 22, 1, s("Mail Jane@Example.ORG")),
        ("NewAge", 25, 1, b"LSMF\xfe" + b"x@y.org\0/media/someone/SteamLibrary\0" + STEAM.encode()),
    ],
)


class LevelCacheTest(unittest.TestCase):
    def inspect(self, level: bytes) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.lsv"
            path.write_bytes(lspk.build([("LevelCache/CTY_Main_A.lsf", level, "zlib")]))
            return inspect_mod.inspect_save(path, show=True)

    def test_binary_bytes_are_no_findings(self):
        r = self.inspect(LEVEL)
        self.assertEqual(r["findings"], [])
        self.assertEqual([h["value"] for h in r["install_paths"]], ["steamapps"])

    def test_real_strings_still_count(self):
        hits = {(h["kind"], h["value"]) for h in self.inspect(REAL)["findings"]}
        self.assertEqual(
            hits,
            {
                ("email", "Jane@Example.ORG"),  # a text field: any case
                ("email", "x@y.org"),
                ("user_path", "/media/someone"),
                ("steam_id", STEAM),
            },
        )


if __name__ == "__main__":
    unittest.main()
