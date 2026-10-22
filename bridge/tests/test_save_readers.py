"""The save-format readers (bg3ai.lspk, bg3ai.lsf) and their LZ4 decoder, on tiny synthetic packages."""

import struct
import unittest

from bg3ai import lsf, lspk


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
GLOBALS = lsf.build([("Globals", -1)], [("Path", 23, 0, s("C:/x"))])
INFO = b'{"Save Name": "S007"}'
STORY = b"\0Osi story\0"


def package() -> bytes:
    return lspk.build(
        [
            ("meta.lsf", META, "none"),
            ("SaveInfo.json", INFO, "zlib"),
            ("StorySave.bin", STORY, "lz4"),
            ("Globals.lsf", GLOBALS, "zlib"),
        ]
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
        pkg = lspk.parse(package())
        self.assertEqual([e.name for e in pkg.entries], ["meta.lsf", "SaveInfo.json", "StorySave.bin", "Globals.lsf"])
        self.assertEqual([e.method for e in pkg.entries], ["none", "zlib", "lz4", "zlib"])
        self.assertEqual(pkg.read(pkg.entries[2]), STORY)
        self.assertEqual(pkg.read(pkg.entries[1]), INFO)

    def test_rejects(self):
        with self.assertRaises(lspk.LspkError):
            lspk.parse(b"PK\x03\x04" + b"\0" * 64)
        bad = bytearray(package())
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

    def test_raw_values_in_file_order(self):
        got = list(lsf.raw_values(lsf.sections(META)))
        self.assertEqual(len(got), len(lsf.attributes(META)))
        self.assertEqual(got[0], (23, s("Claude")))
        self.assertEqual(got[-1], (23, s("4.1.1.7631656")))

    def test_not_lsf(self):
        with self.assertRaises(lsf.LsfError):
            lsf.attributes(b"LSOX" + b"\0" * 80)


if __name__ == "__main__":
    unittest.main()
