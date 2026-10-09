#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lz4>=4.0", "zstandard>=0.22"]
# ///
"""Read and write Baldur's Gate 3 LSPK v18 packages (.pak / .lsv).

    uv run tools/pak.py info    PAK
    uv run tools/pak.py list    PAK [-g GLOB]
    uv run tools/pak.py extract PAK OUTDIR [-g GLOB]
    uv run tools/pak.py create  SRCDIR OUT.pak [-c none|lz4|lz4hc|lz4max] [--priority N] [--hash]

`create` mirrors LSLib's PackageWriter_V15<LSPKHeader16, FileEntry18> (LSLib/LS/PackageWriter.cs):

    0   'LSPK'
    4   LSPKHeader16 (36 bytes, packed): u32 Version=18, u64 FileListOffset, u32 FileListSize,
        u8 Flags, u8 Priority, u8 Md5[16], u16 NumParts
    40  file data; after every file, pad with 0xAD so (pos - 40) % 64 == 0
    FileListOffset: u32 NumFiles, u32 CompressedSize, LZ4 block (no size prefix) of
        NumFiles x FileEntry18 (272 bytes): char Name[256] (UTF-8, NUL padded), u32 OffsetLo,
        u16 OffsetHi, u8 ArchivePart, u8 Flags, u32 SizeOnDisk, u32 UncompressedSize
        (UncompressedSize is 0 for stored files)
    FileListSize = 8 + CompressedSize; the file list ends the archive.

Per-file Flags = method | level: method 0 none, 1 zlib, 2 LZ4 block, 3 zstd; level 0x10 fast,
0x20 default, 0x40 max. Md5 is all zeros unless --hash (LSLib: MD5 over the uncompressed file
contents in table order, then every byte + 1). Divine's defaults are lz4hc (0x22) and priority 0;
the game's own paks use 0x42 and Md5 = 0.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

import lz4.block

SIGNATURE = b"LSPK"
VERSION = 18
HEADER = struct.Struct("<IQIBB16sH")  # LSPKHeader16, follows the signature
ENTRY = struct.Struct("<256sIHBBII")  # FileEntry18
DATA_START = len(SIGNATURE) + HEADER.size  # 40
PADDING = 0x40
PAD_BYTE = 0xAD
MAX_NAME = 255  # 256-byte field, NUL terminated
MAX_PART_SIZE = 0x100000000

METHOD_NONE, METHOD_ZLIB, METHOD_LZ4, METHOD_ZSTD = 0, 1, 2, 3
LEVEL_FAST, LEVEL_DEFAULT, LEVEL_MAX = 0x10, 0x20, 0x40

FLAG_ALLOW_MEMORY_MAPPING, FLAG_SOLID, FLAG_PRELOAD = 0x02, 0x04, 0x08

# Divine -c names -> (method, level). lz4hc is Divine's default; lz4max is what the game ships.
COMPRESSION = {
    "none": (METHOD_NONE, LEVEL_DEFAULT),
    "lz4": (METHOD_LZ4, LEVEL_FAST),
    "lz4hc": (METHOD_LZ4, LEVEL_DEFAULT),
    "lz4max": (METHOD_LZ4, LEVEL_MAX),
}
# LSLib stores these uncompressed regardless of the requested method.
NO_COMPRESS_EXT = {".gts", ".gtp", ".wem", ".bnk", ".bk2"}


class PakError(Exception):
    pass


@dataclass
class Header:
    version: int
    file_list_offset: int
    file_list_size: int
    flags: int
    priority: int
    md5: bytes
    num_parts: int


@dataclass
class Entry:
    name: str
    offset: int
    part: int
    flags: int
    size_on_disk: int
    uncompressed_size: int  # 0 for stored files, as on disk

    @property
    def method(self) -> int:
        return self.flags & 0x0F

    @property
    def size(self) -> int:
        return self.size_on_disk if self.method == METHOD_NONE else self.uncompressed_size


# ---------------------------------------------------------------- compression

def compress(data: bytes, method: int, level: int) -> bytes:
    if method == METHOD_NONE:
        return data
    if method == METHOD_LZ4:
        # LSLib: K4os LZ4Codec.Encode, i.e. a raw LZ4 block. Fast = L00_FAST, Default = L10_OPT, Max = L12_MAX.
        if level == LEVEL_FAST:
            return lz4.block.compress(data, mode="default", store_size=False)
        hc = 12 if level == LEVEL_MAX else 10
        return lz4.block.compress(data, mode="high_compression", compression=hc, store_size=False)
    if method == METHOD_ZLIB:
        return zlib.compress(data, {LEVEL_FAST: 1, LEVEL_MAX: 9}.get(level, 6))
    raise PakError(f"unsupported compression method for writing: {method}")


def decompress(data: bytes, flags: int, size: int) -> bytes:
    method = flags & 0x0F
    if method == METHOD_NONE:
        return data
    if method == METHOD_ZLIB:
        return zlib.decompress(data)
    if method == METHOD_LZ4:
        return lz4.block.decompress(data, uncompressed_size=size)
    if method == METHOD_ZSTD:
        import zstandard  # only savegames use zstd
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
    raise PakError(f"unknown compression flags {flags:#x}")


# ---------------------------------------------------------------- reading

def part_path(path: Path, part: int) -> Path:
    """LSLib Package.MakePartFilename: Foo.pak, Foo_1.pak, Foo_2.pak, ..."""
    return path if part == 0 else path.with_name(f"{path.stem}_{part}{path.suffix}")


class Package:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            head = f.read(DATA_START)
            if len(head) < DATA_START or head[:4] != SIGNATURE:
                raise PakError(f"{self.path}: not an LSPK v15+ package (bad signature)")
            self.header = Header(*HEADER.unpack_from(head, 4))
            if self.header.version != VERSION:
                raise PakError(f"{self.path}: package version {self.header.version}, only v18 is supported")
            if self.header.flags & FLAG_SOLID:
                raise PakError(f"{self.path}: solid packages are not supported")
            f.seek(self.header.file_list_offset)
            num_files, compressed_size = struct.unpack("<II", f.read(8))
            table = lz4.block.decompress(f.read(compressed_size), uncompressed_size=num_files * ENTRY.size)
        self.entries = [self._entry(table, i) for i in range(num_files)]

    @staticmethod
    def _entry(table: bytes, i: int) -> Entry:
        name, lo, hi, part, flags, disk, usize = ENTRY.unpack_from(table, i * ENTRY.size)
        return Entry(name.split(b"\0", 1)[0].decode("utf-8"), lo | (hi << 32), part, flags, disk, usize)

    def find(self, glob: str = "*") -> list[Entry]:
        return [e for e in self.entries if fnmatch.fnmatchcase(e.name, glob)]

    def read(self, entry: Entry) -> bytes:
        with open(part_path(self.path, entry.part), "rb") as f:
            f.seek(entry.offset)
            data = f.read(entry.size_on_disk)
        if len(data) != entry.size_on_disk:
            raise PakError(f"{entry.name}: truncated data")
        out = decompress(data, entry.flags, entry.uncompressed_size)
        if len(out) != entry.size:
            raise PakError(f"{entry.name}: decompressed {len(out)} bytes, expected {entry.size}")
        return out

    def extract(self, outdir: str | os.PathLike, glob: str = "*") -> list[Path]:
        root = Path(outdir).resolve()
        written = []
        for e in self.find(glob):
            dest = (root / e.name).resolve()
            if not dest.is_relative_to(root) or dest == root:
                raise PakError(f"refusing to extract unsafe path {e.name!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.read(e))
            written.append(dest)
        return written


# ---------------------------------------------------------------- writing

def collect(srcdir: str | os.PathLike) -> list[tuple[str, Path]]:
    """Files under srcdir as (archive name, path), sorted; skips anything with a dot-prefixed
    path component like LSLib's ExcludeHidden."""
    root = Path(srcdir)
    if not root.is_dir():
        raise PakError(f"{root} is not a directory")
    files = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if p.is_file() and not any(part.startswith(".") for part in rel.parts):
            files.append((rel.as_posix(), p))
    return sorted(files)


def _pad(f) -> None:
    f.write(bytes([PAD_BYTE]) * ((PADDING - (f.tell() - DATA_START) % PADDING) % PADDING))


def write_package(out: str | os.PathLike, files: list[tuple[str, bytes]], compression: str = "lz4hc",
                  priority: int = 0, flags: int = 0, hash_contents: bool = False) -> Header:
    """Write an LSPK v18 package from (archive name, contents) pairs, in the given order."""
    method, level = COMPRESSION[compression]
    if not 0 <= priority <= 255:
        raise PakError("priority must be 0..255")
    if flags & FLAG_SOLID:
        raise PakError("solid packages are not supported")
    seen = set()
    for name, _ in files:
        encoded = name.encode("utf-8")
        if not name or "\\" in name or name.startswith("/") or ".." in name.split("/"):
            raise PakError(f"bad archive name {name!r}")
        if len(encoded) > MAX_NAME:
            raise PakError(f"archive name longer than {MAX_NAME} bytes: {name}")
        if name in seen:
            raise PakError(f"duplicate archive name {name}")
        seen.add(name)

    table = bytearray()
    with open(out, "wb") as f:
        f.write(SIGNATURE + bytes(HEADER.size))  # header placeholder, rewritten at the end
        for name, data in files:
            m, lv = method, level
            if not data or Path(name).suffix.lower() in NO_COMPRESS_EXT:
                m = METHOD_NONE
            packed = compress(data, m, lv)
            entry_flags = 0 if m == METHOD_NONE else m | lv
            offset = f.tell()
            if offset + len(packed) > MAX_PART_SIZE:
                raise PakError("package exceeds 4 GiB; multi-part writing is not supported")
            f.write(packed)
            _pad(f)
            table += ENTRY.pack(name.encode("utf-8"), offset & 0xFFFFFFFF, (offset >> 32) & 0xFFFF, 0,
                                entry_flags, len(packed), 0 if m == METHOD_NONE else len(data))

        compressed_table = compress(bytes(table), METHOD_LZ4, LEVEL_DEFAULT)
        file_list_offset = f.tell()
        f.write(struct.pack("<II", len(files), len(compressed_table)))
        f.write(compressed_table)

        md5 = bytes(16)
        if hash_contents:
            md5 = bytes((b + 1) & 0xFF for b in hashlib.md5(b"".join(d for _, d in files)).digest())
        header = Header(VERSION, file_list_offset, 8 + len(compressed_table), flags, priority, md5, 1)
        f.seek(len(SIGNATURE))
        f.write(HEADER.pack(header.version, header.file_list_offset, header.file_list_size,
                            header.flags, header.priority, header.md5, header.num_parts))
    return header


def create(srcdir: str | os.PathLike, out: str | os.PathLike, **kw) -> Header:
    files = [(name, p.read_bytes()) for name, p in collect(srcdir)]
    if not files:
        raise PakError(f"no files under {srcdir}")
    return write_package(out, files, **kw)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BG3 LSPK v18 package tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="print the package header")
    p.add_argument("pak")
    p = sub.add_parser("list", help="list files: name, size on disk, size, flags")
    p.add_argument("pak")
    p.add_argument("-g", "--glob", default="*")
    p = sub.add_parser("extract", help="extract files")
    p.add_argument("pak")
    p.add_argument("outdir")
    p.add_argument("-g", "--glob", default="*")
    p = sub.add_parser("create", help="package a directory tree (e.g. one containing Mods/<Name>/meta.lsx)")
    p.add_argument("srcdir")
    p.add_argument("out")
    p.add_argument("-c", "--compression", choices=sorted(COMPRESSION), default="lz4hc")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--hash", action="store_true", help="fill the MD5 field like LSLib's Hash option")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "create":
            h = create(a.srcdir, a.out, compression=a.compression, priority=a.priority, hash_contents=a.hash)
            n = len(Package(a.out).entries)
            print(f"wrote {a.out}: {n} files, {os.path.getsize(a.out)} bytes, priority {h.priority}")
            return 0
        pak = Package(a.pak)
        if a.cmd == "info":
            h = pak.header
            print(f"version={h.version} flags={h.flags:#x} priority={h.priority} parts={h.num_parts} "
                  f"files={len(pak.entries)} file_list_offset={h.file_list_offset} "
                  f"file_list_size={h.file_list_size} md5={h.md5.hex()}")
        elif a.cmd == "list":
            for e in pak.find(a.glob):
                print(f"{e.name}\t{e.size_on_disk}\t{e.size}\t{e.flags:#04x}")
        elif a.cmd == "extract":
            print(f"extracted {len(pak.extract(a.outdir, a.glob))} files to {a.outdir}")
    except (PakError, OSError) as exc:
        print(f"pak.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
