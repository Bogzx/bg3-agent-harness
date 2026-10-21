"""Offline tests for the save inspector (tools/saves): LSPK/LSF/LZ4 readers and the report, on tiny synthetic saves.

Run: python3 -m unittest discover -s tools/saves -p 'test_*.py'
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

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))

import lsf
import lspk

# inspect.py shares its name with the stdlib module: load it under another name.
_spec = importlib.util.spec_from_file_location("saves_inspect", HERE / "inspect.py")
inspect_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inspect_mod)

STEAM = "76561198000000042"


def lsf_bytes(nodes: list[tuple[str, int]], attrs: list[tuple[str, int, int, bytes]]) -> bytes:
    """A version 7 LSF with 12-byte entries, sections stored uncompressed. nodes: (name, parent);
    attrs: (name, type, node, raw value)."""
    names: list[str] = []

    def ref(n: str) -> int:
        if n not in names:
            names.append(n)
        return names.index(n)

    node_tab = bytearray()
    first = {}
    for k, (_, node, _, _) in enumerate(attrs):
        first.setdefault(node, k)
    for i, (n, parent) in enumerate(nodes):
        node_tab += struct.pack("<Iii", ref(n), first.get(i, -1), parent)
    attr_tab, values = bytearray(), bytearray()
    for n, t, node, raw in attrs:
        attr_tab += struct.pack("<IIi", ref(n), t | (len(raw) << 6), node)
        values += raw
    strings = bytearray(struct.pack("<IH", 1, len(names)))
    for n in names:
        strings += struct.pack("<H", len(n)) + n.encode()
    meta = lsf.META.pack(len(strings), 0, 0, 0, len(node_tab), 0, len(attr_tab), 0, len(values), 0, 0, 0, 0, 0)
    return b"LSOF" + struct.pack("<IQ", 7, 0) + meta + strings + node_tab + attr_tab + values


def s(text: str) -> bytes:
    return text.encode() + b"\0"


META = lsf_bytes(
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
GLOBALS = lsf_bytes([("Globals", -1)], [("Path", 23, 0, s("/home/someone/.local/share/Larian Studios/x"))])
SAVEINFO = json.dumps(
    {
        "Save Name": "S007",
        "Game Version": "4.1.1.7631656",
        "Platform": "Steam",
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


class Lz4Test(unittest.TestCase):
    def test_overlapping_match(self):
        # literal "ab", then a match at offset 2 of length 6 (copies its own output)
        block = bytes([0x22]) + b"ab" + struct.pack("<H", 2)
        self.assertEqual(lspk.lz4_block(block), b"abababab")

    def test_long_literal_run(self):
        data = bytes(range(256)) * 3
        self.assertEqual(lspk.lz4_block(lspk.lz4_literals(data), len(data)), data)

    def test_size_mismatch_and_bad_offset(self):
        with self.assertRaises(lspk.LspkError):
            lspk.lz4_block(lspk.lz4_literals(b"abc"), 4)
        with self.assertRaises(lspk.LspkError):
            lspk.lz4_block(bytes([0x10]) + b"a" + struct.pack("<H", 9))

    def test_frame_with_linked_blocks(self):
        b1 = lspk.lz4_literals(b"hello ")
        b2 = bytes([0x01]) + struct.pack("<H", 6)  # no literals, a 5-byte match back into block 1: "hello"
        stored = b"!!"
        frame = (
            lspk.LZ4_FRAME
            + bytes([0x40, 0x40, 0x00])
            + struct.pack("<I", len(b1))
            + b1
            + struct.pack("<I", len(b2))
            + b2
            + struct.pack("<I", 0x80000000 | len(stored))
            + stored
            + struct.pack("<I", 0)
        )
        self.assertEqual(lspk.lz4_frame(frame), b"hello hello!!")


class PackageTest(unittest.TestCase):
    def test_members(self):
        pkg = lspk.parse(synthetic())
        self.assertEqual([e.name for e in pkg.entries], ["meta.lsf", "SaveInfo.json", "StorySave.bin", "Globals.lsf"])
        self.assertEqual([e.method for e in pkg.entries], ["none", "zlib", "lz4", "zlib"])
        self.assertEqual(pkg.read(pkg.entries[2]), STORY)
        self.assertEqual(pkg.read(pkg.entries[1]), SAVEINFO)

    def test_rejects(self):
        with self.assertRaises(lspk.LspkError):
            lspk.parse(b"PK\x03\x04" + b"\0" * 64)
        bad = bytearray(synthetic())
        bad[4:8] = struct.pack("<I", 16)
        with self.assertRaises(lspk.LspkError):
            lspk.parse(bytes(bad))

    @unittest.skipUnless(lspk.zstd, "zstd needs Python 3.14+")
    def test_zstd_member(self):
        pkg = lspk.parse(lspk.build([("Globals.lsf", GLOBALS, "zstd")]))
        self.assertEqual(pkg.read(pkg.entries[0]), GLOBALS)

    def test_zstd_unread_without_it(self):
        entry = lspk.Entry("x.lsf", 0, 0, 3, 0, 0)
        pkg = lspk.Package(18, 0, 0, 1, (entry,), b"")
        if lspk.zstd is None:
            self.assertIsNone(pkg.read(entry))


class LsfTest(unittest.TestCase):
    def test_attributes_with_paths(self):
        got = {(a.path, a.name): a.value for a in lsf.attributes(META)}
        self.assertEqual(got[("MetaData/MetaData", "LeaderName")], "Claude")
        self.assertEqual(got[("MetaData/MetaData", "Seed")], 1071409710)
        self.assertIs(got[("MetaData/MetaData", "Modded")], False)
        self.assertEqual(got[("MetaData/MetaData/ModuleShortDesc", "Name")], "GustavX")
        self.assertEqual(got[("MetaData/MetaData/ModuleShortDesc", "UUID")], "03020100-0504-0706-0809-0a0b0c0d0e0f")
        self.assertEqual(got[("MetaData/MetaData/GameVersions/GameVersion", "Object")], "4.1.1.7631656")

    def test_not_lsf(self):
        with self.assertRaises(lsf.LsfError):
            lsf.attributes(b"LSOX" + b"\0" * 80)


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


if __name__ == "__main__":
    unittest.main()
