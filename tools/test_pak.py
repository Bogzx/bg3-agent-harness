#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lz4>=4.0", "zstandard>=0.22"]
# ///
"""Tests for tools/pak.py.  Run: uv run tools/test_pak.py   (or: uv run --with lz4 --with zstandard -m pytest tools/test_pak.py)

The structural checks also run read-only against real game paks when the Steam install is present
(override with BG3_DATA=/path/to/Data).
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import lz4.block

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pak  # noqa: E402

GAME_DATA = Path(os.environ.get(
    "BG3_DATA", Path.home() / ".local/share/Steam/steamapps/common/Baldurs Gate 3/Data"))
GAME_PAKS = ["Localization/English.pak", "GamePlatform.pak", "Patch8_HotFix10.pak"]


def check_structure(test: unittest.TestCase, path: Path) -> list[pak.Entry]:
    """Invariants that hold for the game's own v18 paks and for everything LSLib writes."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        raw = f.read(pak.DATA_START)
        test.assertEqual(raw[:4], b"LSPK")
        version, flo, fls, flags, prio, md5, parts = pak.HEADER.unpack_from(raw, 4)
        test.assertEqual(version, 18)
        test.assertEqual(parts, 1)
        test.assertEqual(flags & pak.FLAG_SOLID, 0)
        test.assertEqual(flo + fls, size, "file list must end the archive")
        test.assertEqual((flo - pak.DATA_START) % pak.PADDING, 0, "file list starts on a padded boundary")
        f.seek(flo)
        n, csz = struct.unpack("<II", f.read(8))
        test.assertEqual(fls, 8 + csz)
        table = lz4.block.decompress(f.read(csz), uncompressed_size=n * pak.ENTRY.size)
        entries = [pak.Package._entry(table, i) for i in range(n)]
        spans = []
        for e in entries:
            test.assertGreaterEqual(e.offset, pak.DATA_START)
            test.assertEqual((e.offset - pak.DATA_START) % pak.PADDING, 0, e.name)
            test.assertLessEqual(e.offset + e.size_on_disk, flo)
            test.assertEqual(e.part, 0)
            if e.method == pak.METHOD_NONE:
                test.assertEqual(e.flags, 0, f"{e.name}: stored files carry no level bits")
                test.assertEqual(e.uncompressed_size, 0, f"{e.name}: stored files have usize 0")
            else:
                test.assertIn(e.flags & 0xF0, (pak.LEVEL_FAST, pak.LEVEL_DEFAULT, pak.LEVEL_MAX))
            spans.append((e.offset, e.offset + e.size_on_disk))
        spans.sort()
        for (a0, a1), (b0, _) in zip(spans, spans[1:]):
            test.assertLessEqual(a1, b0, "file data must not overlap")
            f.seek(a1)
            test.assertTrue(set(f.read(min(b0 - a1, 64))) <= {pak.PAD_BYTE}, "gaps are 0xAD padding")
    return entries


def make_tree(root: Path) -> dict[str, bytes]:
    files = {
        "Mods/BG3AI/meta.lsx": b'<?xml version="1.0" encoding="UTF-8"?>\n<save><version major="4"/></save>\n',
        "Mods/BG3AI/ScriptExtender/Config.json": b'{"RequiredVersion": 1, "ModTable": "BG3AI", "FeatureFlags": ["Lua"]}',
        "Mods/BG3AI/ScriptExtender/Lua/BootstrapServer.lua": b"-- server\n" + b"Ext.Utils.Print('hi')\n" * 400,
        "Mods/BG3AI/ScriptExtender/Lua/Server/State.lua": os.urandom(3000),  # incompressible
        "Mods/BG3AI/ScriptExtender/Lua/empty.lua": b"",
        "Mods/BG3AI/Localization/Ünïcode name.txt": "größe".encode(),
        "Mods/BG3AI/Sounds/x.wem": b"RIFF" * 100,  # never compressed, like LSLib
    }
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "Mods/BG3AI/.git").mkdir()
    (root / "Mods/BG3AI/.git/HEAD").write_bytes(b"ref")  # hidden: excluded
    (root / "Mods/BG3AI/.notes.md").write_bytes(b"hidden")  # hidden: excluded
    return files


class PakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "src"
        self.files = make_tree(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, name="BG3AI.pak", **kw) -> Path:
        out = self.dir / name
        pak.create(self.src, out, **kw)
        return out

    def test_round_trip_all_compressions(self):
        for comp in pak.COMPRESSION:
            with self.subTest(compression=comp):
                out = self.build(f"{comp}.pak", compression=comp)
                p = pak.Package(out)
                self.assertEqual(sorted(e.name for e in p.entries), sorted(self.files))
                for e in p.entries:
                    self.assertEqual(p.read(e), self.files[e.name], e.name)
                check_structure(self, out)

    def test_flags_and_sizes(self):
        p = pak.Package(self.build(compression="lz4max"))
        by = {e.name: e for e in p.entries}
        lua = by["Mods/BG3AI/ScriptExtender/Lua/BootstrapServer.lua"]
        self.assertEqual(lua.flags, 0x42)  # same as the game's own paks
        self.assertLess(lua.size_on_disk, lua.uncompressed_size)
        for name in ("Mods/BG3AI/ScriptExtender/Lua/empty.lua", "Mods/BG3AI/Sounds/x.wem"):
            self.assertEqual((by[name].flags, by[name].uncompressed_size), (0, 0), name)
        self.assertEqual(pak.Package(self.build("d.pak")).find("*BootstrapServer.lua")[0].flags, 0x22)  # Divine default
        stored = pak.Package(self.build("n.pak", compression="none")).entries
        self.assertTrue(all(e.flags == 0 and e.uncompressed_size == 0 for e in stored))

    def test_header_fields(self):
        out = self.build(priority=7)
        h = pak.Package(out).header
        self.assertEqual((h.version, h.flags, h.priority, h.num_parts, h.md5), (18, 0, 7, 1, bytes(16)))
        first = min(e.offset for e in pak.Package(out).entries)
        self.assertEqual(first, 40)

    def test_md5_matches_lslib_rule(self):
        out = self.build(hash_contents=True)
        entries = pak.Package(out).entries
        digest = hashlib.md5(b"".join(self.files[e.name] for e in entries)).digest()
        self.assertEqual(pak.Package(out).header.md5, bytes((b + 1) & 0xFF for b in digest))

    def test_hidden_files_excluded_and_sorted(self):
        names = [n for n, _ in pak.collect(self.src)]
        self.assertEqual(names, sorted(self.files))
        self.assertFalse(any("/." in n for n in names))

    def test_deterministic(self):
        a, b = self.build("a.pak"), self.build("b.pak")
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_extract_round_trip(self):
        out = self.build()
        dest = self.dir / "out"
        pak.Package(out).extract(dest)
        for name, data in self.files.items():
            self.assertEqual((dest / name).read_bytes(), data)
        self.assertEqual(len(pak.Package(out).find("*.lua")), 3)

    def test_rejects_bad_names(self):
        for bad in ("../evil.txt", "/abs.txt", "a\\b.txt", "x" * 256, ""):
            with self.subTest(name=bad), self.assertRaises(pak.PakError):
                pak.write_package(self.dir / "bad.pak", [(bad, b"x")])
        with self.assertRaises(pak.PakError):
            pak.write_package(self.dir / "dup.pak", [("a", b"1"), ("a", b"2")])
        pak.write_package(self.dir / "ok.pak", [("é" * 127, b"x")])  # 254 UTF-8 bytes fits

    def test_extract_refuses_traversal(self):
        # Hand-craft a package whose table holds a '../' name (bypassing write_package's checks).
        out = self.dir / "evil.pak"
        pak.write_package(out, [("a/zz.txt", b"payload")], compression="none")
        blob = bytearray(out.read_bytes())
        h = pak.Package(out).header
        table = bytearray(pak.ENTRY.size)
        pak.ENTRY.pack_into(table, 0, b"../zz.txt", pak.DATA_START, 0, 0, 0, 7, 0)
        ctable = lz4.block.compress(bytes(table), store_size=False)
        blob[h.file_list_offset:] = struct.pack("<II", 1, len(ctable)) + ctable
        struct.pack_into("<I", blob, 4 + 12, 8 + len(ctable))
        out.write_bytes(blob)
        with self.assertRaises(pak.PakError):
            pak.Package(out).extract(self.dir / "x")
        self.assertFalse((self.dir / "zz.txt").exists())

    def test_rejects_garbage(self):
        bad = self.dir / "garbage.pak"
        bad.write_bytes(b"NOPE" + bytes(100))
        with self.assertRaises(pak.PakError):
            pak.Package(bad)

    def test_cli(self):
        out = self.dir / "cli.pak"
        self.assertEqual(pak.main(["create", str(self.src), str(out), "-c", "lz4max", "--priority", "1"]), 0)
        self.assertEqual(pak.main(["list", str(out), "-g", "*.lua"]), 0)
        self.assertEqual(pak.main(["extract", str(out), str(self.dir / "cli_out")]), 0)
        self.assertEqual((self.dir / "cli_out/Mods/BG3AI/meta.lsx").read_bytes(), self.files["Mods/BG3AI/meta.lsx"])


@unittest.skipUnless(GAME_DATA.is_dir(), "BG3 Data directory not found")
class GamePakTests(unittest.TestCase):
    """Read-only comparison against the game's own packages."""

    def test_game_paks_match_structure(self):
        for rel in GAME_PAKS:
            path = GAME_DATA / rel
            if not path.exists():
                continue
            with self.subTest(pak=rel):
                entries = check_structure(self, path)
                h = pak.Package(path).header
                self.assertEqual(h.md5, bytes(16))
                self.assertTrue(entries)

    def test_reads_game_file(self):
        path = GAME_DATA / "GamePlatform.pak"
        if not path.exists():
            self.skipTest("GamePlatform.pak missing")
        p = pak.Package(path)
        credits = p.find("Public/Game/Credits/credits.txt")[0]
        self.assertEqual(credits.flags, 0x42)
        data = p.read(credits)
        self.assertEqual(len(data), credits.uncompressed_size)

    def test_repack_game_pak_round_trips(self):
        path = GAME_DATA / "GamePlatform.pak"
        if not path.exists():
            self.skipTest("GamePlatform.pak missing")
        src = pak.Package(path)
        files = [(e.name, src.read(e)) for e in src.entries]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "repack.pak"
            pak.write_package(out, files, compression="lz4max", priority=src.header.priority)
            check_structure(self, out)
            again = pak.Package(out)
            self.assertEqual([e.name for e in again.entries], [n for n, _ in files])
            self.assertEqual([again.read(e) for e in again.entries], [d_ for _, d_ in files])
            orig = {e.name: e.flags for e in src.entries}
            self.assertEqual({e.name: e.flags for e in again.entries}, orig)


if __name__ == "__main__":
    unittest.main(verbosity=2)
