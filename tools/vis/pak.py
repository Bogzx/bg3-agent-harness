"""Minimal LSPK v18 (BG3 .pak) reader: list and extract files matching a regex.

usage: python -I pak.py <pak> <regex> <outdir|->
Needs the lz4 and zstandard modules (run in a throwaway container, see gen_marker_index.sh).
"""
import os
import re
import struct
import sys

import lz4.block
import zstandard


def main(path, pattern, outdir):
    with open(path, "rb") as f:
        sig, _ver, foff = struct.unpack("<4sIQ", f.read(16))
        assert sig == b"LSPK", sig
        f.seek(foff)
        nfiles, csize = struct.unpack("<II", f.read(8))
        table = lz4.block.decompress(f.read(csize), uncompressed_size=nfiles * 272)
        rx = re.compile(pattern)
        for i in range(nfiles):
            e = table[i * 272:(i + 1) * 272]
            name = e[:256].split(b"\0")[0].decode()
            if not rx.search(name):
                continue
            off = struct.unpack("<I", e[256:260])[0] | (struct.unpack("<H", e[260:262])[0] << 32)
            arch, flags = e[262], e[263]
            size, usize = struct.unpack("<II", e[264:272])
            print(name, arch, flags, size, usize)
            if outdir == "-" or arch != 0:
                continue
            f.seek(off)
            raw = f.read(size)
            method = flags & 0xF
            if method == 0:
                data = raw
            elif method == 2:
                data = lz4.block.decompress(raw, uncompressed_size=usize)
            elif method == 3:
                data = zstandard.ZstdDecompressor().decompress(raw, max_output_size=usize)
            else:
                print("  unknown compression", flags)
                continue
            with open(os.path.join(outdir, name.replace("/", "__")), "wb") as out:
                out.write(data)


if __name__ == "__main__":
    main(*sys.argv[1:4])
