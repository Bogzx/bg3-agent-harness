"""A small read-only LSPK (Larian package, .lsv/.pak) reader: the v18 layout BG3 writes.

Layout (LSLib PackageReader, v18): "LSPK", u32 version, u64 file list offset, u32 file list size, u8 flags,
u8 priority, 16-byte md5, u16 parts. The file list is u32 count, u32 compressed size, then an LZ4 block of
`count` 272-byte entries: name[256], u32 offset low, u16 offset high, u8 part, u8 flags, u32 size on disk,
u32 uncompressed size. Entry flags & 0x0F is the compression: 0 none, 1 zlib, 2 LZ4 block, 3 zstd.
zstd needs Python 3.14+ (`compression.zstd`); without it those members are listed but not read.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

try:
    from compression import zstd  # pyright: ignore[reportMissingImports]  (Python 3.14+)
except ImportError:  # pragma: no cover - depends on the interpreter
    zstd = None

MAGIC = b"LSPK"
HEADER = struct.Struct("<4sIQIBB16sH")
ENTRY = struct.Struct("<256sIHBBII")
METHODS = {0: "none", 1: "zlib", 2: "lz4", 3: "zstd"}


class LspkError(ValueError):
    pass


def lz4_block(src: bytes, size: int | None = None, history: bytearray | None = None) -> bytes:
    """Decode one raw LZ4 block (no frame). With `history`, matches may reach back into it (linked frame blocks)
    and the output is appended to it."""
    dst = history if history is not None else bytearray()
    base = len(dst)
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i : i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off == 0 or start < 0:
            raise LspkError(f"bad LZ4 match offset {off} at {i}")
        if off >= ml:
            dst += dst[start : start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    if size is not None and len(dst) - base != size:
        raise LspkError(f"LZ4 block gave {len(dst) - base} bytes, expected {size}")
    return bytes(dst[base:])


LZ4_FRAME = b"\x04\x22\x4d\x18"


def lz4_frame(src: bytes) -> bytes:
    """Decode an LZ4 frame (LSF sections of version 6+ are framed; checksums are skipped, not verified)."""
    if src[:4] != LZ4_FRAME:
        raise LspkError("not an LZ4 frame")
    flg = src[4]
    pos = 6 + (8 if flg & 0x08 else 0) + (4 if flg & 0x01 else 0) + 1
    out = bytearray()
    while True:
        (n,) = struct.unpack_from("<I", src, pos)
        pos += 4
        if n == 0:
            break
        size, stored = n & 0x7FFFFFFF, bool(n & 0x80000000)
        block = src[pos : pos + size]
        pos += size + (4 if flg & 0x10 else 0)
        if stored:
            out += block
        else:
            lz4_block(block, history=out)
    return bytes(out)


@dataclass(frozen=True)
class Entry:
    name: str
    offset: int
    part: int
    flags: int
    size_on_disk: int
    size: int

    @property
    def method(self) -> str:
        return METHODS.get(self.flags & 0x0F, f"unknown({self.flags & 0x0F})")


@dataclass(frozen=True)
class Package:
    version: int
    flags: int
    priority: int
    parts: int
    entries: tuple[Entry, ...]
    data: bytes

    def read(self, entry: Entry) -> bytes | None:
        """The member's bytes, or None when its compression can't be read here (zstd before Python 3.14)."""
        if entry.part != 0:
            raise LspkError(f"{entry.name}: in archive part {entry.part} (only single-part packages)")
        raw = self.data[entry.offset : entry.offset + entry.size_on_disk]
        method = entry.method
        if method == "none":
            return raw
        if method == "zlib":
            return zlib.decompress(raw)
        if method == "lz4":
            return lz4_block(raw, entry.size)
        if method == "zstd":
            return zstd.decompress(raw) if zstd else None
        raise LspkError(f"{entry.name}: unknown compression {entry.flags:#x}")


def parse(data: bytes) -> Package:
    if len(data) < HEADER.size or data[:4] != MAGIC:
        raise LspkError("not an LSPK package (no LSPK signature)")
    _, version, list_off, list_size, flags, priority, _, parts = HEADER.unpack_from(data)
    if version != 18:
        raise LspkError(f"LSPK version {version}: only v18 (BG3) is supported")
    if list_off + 8 > len(data):
        raise LspkError("file list offset past the end")
    count, csize = struct.unpack_from("<II", data, list_off)
    if csize + 8 > list_size or list_off + 8 + csize > len(data):
        raise LspkError("file list size past the end")
    table = lz4_block(data[list_off + 8 : list_off + 8 + csize], count * ENTRY.size)
    entries = []
    for k in range(count):
        name, off1, off2, part, eflags, on_disk, size = ENTRY.unpack_from(table, k * ENTRY.size)
        entries.append(
            Entry(name.split(b"\0", 1)[0].decode("utf-8", "replace"), off1 | (off2 << 32), part, eflags, on_disk, size)
        )
    return Package(version, flags, priority, parts, tuple(entries), data)


def lz4_literals(data: bytes) -> bytes:
    """`data` as a valid LZ4 block made of literals only (for building test packages)."""
    out = bytearray()
    n = len(data)
    lit = n
    out.append(min(lit, 15) << 4)
    if lit >= 15:
        lit -= 15
        while lit >= 255:
            out.append(255)
            lit -= 255
        out.append(lit)
    out += data
    return bytes(out)


def build(members: list[tuple[str, bytes, str]]) -> bytes:
    """A v18 package of (name, content, method) members, method one of METHODS' names (for tests)."""
    encode = {
        "none": lambda b: b,
        "zlib": zlib.compress,
        "lz4": lz4_literals,
        "zstd": lambda b: zstd.compress(b),  # pyright: ignore[reportOptionalMemberAccess]  (tests skip it without)
    }
    codes = {v: k for k, v in METHODS.items()}
    body = bytearray()
    table = bytearray()
    for name, content, method in members:
        stored = encode[method](content)
        off = HEADER.size + len(body)
        table += ENTRY.pack(name.encode(), off & 0xFFFFFFFF, off >> 32, 0, codes[method], len(stored), len(content))
        body += stored
    lz = lz4_literals(bytes(table))
    list_off = HEADER.size + len(body)
    head = HEADER.pack(MAGIC, 18, list_off, 8 + len(lz), 0, 0, b"\0" * 16, 1)
    return head + bytes(body) + struct.pack("<II", len(members), len(lz)) + lz
