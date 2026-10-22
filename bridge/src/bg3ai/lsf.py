"""A small read-only LSF (Larian binary resource, "LSOF") reader, enough to list a save's attributes.

Layout (LSLib LSFReader, versions 6-7 as BG3 writes): "LSOF", u32 version, u64 engine version, then sizes
(uncompressed, on disk) of the strings, keys, nodes, attributes and values sections, u8 compression flags,
3 bytes, u32 metadata format (1 = keys and adjacency: 16-byte node/attribute entries, else 12-byte ones). Sections
follow in the order strings, nodes, attributes, values, keys; each LZ4- or zlib-compressed by the flags (size on
disk 0 = stored as is). Strings: u32 buckets, each u16 count of (u16 length, bytes). A name reference is
bucket << 16 | index.
"""

from __future__ import annotations

import struct
import uuid as uuidlib
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from bg3ai.lspk import LZ4_FRAME, lz4_block, lz4_frame

MAGIC = b"LSOF"
META = struct.Struct("<IIIIIIIIIIBBHI")
STRING_TYPES = {20, 21, 22, 23, 29, 30}
SCALARS = {
    1: "<B",
    2: "<h",
    3: "<H",
    4: "<i",
    5: "<I",
    6: "<f",
    7: "<d",
    19: "<?",
    24: "<Q",
    26: "<q",
    27: "<b",
    32: "<q",
}


class LsfError(ValueError):
    pass


@dataclass(frozen=True)
class Attr:
    path: str  # node names from the root, "/"-joined
    name: str
    type: int
    value: object


@dataclass(frozen=True)
class Sections:
    version: int
    engine: int
    strings: bytes
    nodes: bytes
    attributes: bytes
    values: bytes
    long_entries: bool


def _section(data: bytes, pos: int, size: int, on_disk: int, flags: int) -> tuple[bytes, int]:
    if on_disk == 0:
        return data[pos : pos + size], pos + size
    raw = data[pos : pos + on_disk]
    method = flags & 0x0F
    if method == 0:
        out = raw
    elif method == 1:
        out = zlib.decompress(raw)
    elif method == 2:
        out = lz4_frame(raw) if raw[:4] == LZ4_FRAME else lz4_block(raw, size)
    else:
        raise LsfError(f"LSF section compression {method} not supported")
    return out, pos + on_disk


def sections(data: bytes) -> Sections:
    if data[:4] != MAGIC:
        raise LsfError("not an LSF resource (no LSOF signature)")
    version, engine = struct.unpack_from("<IQ", data, 4)
    if version < 6:
        raise LsfError(f"LSF version {version}: only 6+ (BG3)")
    (s_size, s_disk, _k_size, _k_disk, n_size, n_disk, a_size, a_disk, v_size, v_disk, flags, _, _, fmt) = (
        META.unpack_from(data, 16)
    )
    pos = 16 + META.size
    strings, pos = _section(data, pos, s_size, s_disk, flags)
    nodes, pos = _section(data, pos, n_size, n_disk, flags)
    attributes, pos = _section(data, pos, a_size, a_disk, flags)
    values, pos = _section(data, pos, v_size, v_disk, flags)
    return Sections(version, engine, strings, nodes, attributes, values, fmt == 1)


def names(strings: bytes) -> list[list[str]]:
    (buckets,) = struct.unpack_from("<I", strings, 0)
    pos, out = 4, []
    for _ in range(buckets):
        (count,) = struct.unpack_from("<H", strings, pos)
        pos += 2
        bucket = []
        for _ in range(count):
            (n,) = struct.unpack_from("<H", strings, pos)
            bucket.append(strings[pos + 2 : pos + 2 + n].decode("utf-8", "replace"))
            pos += 2 + n
        out.append(bucket)
    return out


def _value(t: int, raw: bytes) -> object:
    if t in STRING_TYPES:
        return raw.rstrip(b"\0").decode("utf-8", "replace")
    if t in SCALARS:
        return struct.unpack_from(SCALARS[t], raw)[0]
    if t == 31 and len(raw) == 16:
        return str(uuidlib.UUID(bytes_le=raw))
    if t in (28, 33) and len(raw) >= 6:  # TranslatedString: u16 version, i32 length, handle
        (n,) = struct.unpack_from("<i", raw, 2)
        return {"version": struct.unpack_from("<H", raw)[0], "handle": raw[6 : 6 + n].rstrip(b"\0").decode()}
    return raw.hex()


def attributes(data: bytes) -> list[Attr]:
    """Every attribute with its node path, in file order."""
    s = sections(data)
    nm = names(s.strings)

    def name(ref: int) -> str:
        return nm[ref >> 16][ref & 0xFFFF]

    node_names, parents, firsts = [], [], []
    step = 16 if s.long_entries else 12
    for k in range(0, len(s.nodes), step):
        if s.long_entries:
            ref, parent, _, first = struct.unpack_from("<Iiii", s.nodes, k)
        else:
            ref, first, parent = struct.unpack_from("<Iii", s.nodes, k)
        node_names.append(name(ref))
        parents.append(parent)
        firsts.append(first)

    def path(i: int) -> str:
        parts = []
        while i >= 0 and len(parts) < 64:
            parts.append(node_names[i])
            i = parents[i]
        return "/".join(reversed(parts))

    out = []
    if s.long_entries:
        owner = {}
        entries = [struct.unpack_from("<IIiI", s.attributes, k) for k in range(0, len(s.attributes), 16)]
        for node, first in enumerate(firsts):
            a = first
            while 0 <= a < len(entries) and a not in owner:
                owner[a] = node
                a = entries[a][2]
        for a, (ref, tl, _, off) in enumerate(entries):
            t, n = tl & 0x3F, tl >> 6
            out.append(Attr(path(owner.get(a, -1)), name(ref), t, _value(t, s.values[off : off + n])))
    else:
        off = 0
        for k in range(0, len(s.attributes), 12):
            ref, tl, node = struct.unpack_from("<IIi", s.attributes, k)
            t, n = tl & 0x3F, tl >> 6
            out.append(Attr(path(node), name(ref), t, _value(t, s.values[off : off + n])))
            off += n
    return out


def raw_values(s: Sections) -> Iterator[tuple[int, bytes]]:
    """(type, raw bytes) of every attribute value, in file order: no names or paths (fast, for scanning)."""
    off, step = 0, 16 if s.long_entries else 12
    for k in range(0, len(s.attributes), step):
        if s.long_entries:
            _, tl, _, off = struct.unpack_from("<IIiI", s.attributes, k)
        else:
            _, tl, _ = struct.unpack_from("<IIi", s.attributes, k)
        n = tl >> 6
        yield tl & 0x3F, s.values[off : off + n]
        off += n


def build(nodes: list[tuple[str, int]], attrs: list[tuple[str, int, int, bytes]]) -> bytes:
    """A version 7 LSF with 12-byte entries, sections stored uncompressed (for tests). nodes: (name, parent);
    attrs: (name, type, node, raw value), grouped by node."""
    names: list[str] = []

    def ref(n: str) -> int:
        if n not in names:
            names.append(n)
        return names.index(n)

    node_tab = bytearray()
    first: dict[int, int] = {}
    for k, (_, _, node, _) in enumerate(attrs):
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
    meta = META.pack(len(strings), 0, 0, 0, len(node_tab), 0, len(attr_tab), 0, len(values), 0, 0, 0, 0, 0)
    return MAGIC + struct.pack("<IQ", 7, 0) + meta + strings + node_tab + attr_tab + values
