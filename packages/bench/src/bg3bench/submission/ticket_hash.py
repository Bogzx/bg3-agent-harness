"""The running hash of a ticketed run (docs on the site's side: RUN-TICKETS.md): pure, no clock, no I/O.

The record streams are facts.jsonl, rolls.jsonl and every play process's events log. Each has its own hash, seeded
with the ticket's nonce and fed the stream's scrubbed lines, and a root joins them:

    h(name, n) = sha256("bg3bench.ticket.v1\\0" + nonce + "\\0" + name + "\\0" + the first n scrubbed lines, each + "\\n")
    root       = sha256(canonical JSON of [[name, n, hex h(name, n)], ...] sorted by name)

Only complete lines count (through a newline): a partial last line waits for the next beat. The client hashes each
line as the run writes it (scrub.Scrubber.line first) and the validator recomputes from the bundle's files, which
hold the same scrubbed lines.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Mapping

from bg3bench.submission import bundle

DOMAIN = "bg3bench.ticket.v1"
FACTS = "facts.jsonl"
ROLLS = "rolls.jsonl"


def is_stream(name: str) -> bool:
    return name in (FACTS, ROLLS) or bundle.EVENTS.fullmatch(name) is not None


class RunningHash:
    """One stream's hash: feed it complete lines, without their newline."""

    def __init__(self, nonce: str, name: str) -> None:
        self.name, self.lines = name, 0
        self._h = hashlib.sha256(f"{DOMAIN}\0{nonce}\0{name}\0".encode())

    def feed(self, line: bytes) -> None:
        self._h.update(line + b"\n")
        self.lines += 1

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def root(entries: Iterable[tuple[str, int, str]]) -> str:
    """The root over (name, lines, hex digest) of each stream."""
    joined = [list(e) for e in sorted(entries)]
    return hashlib.sha256(bundle.canonical_bytes(joined)).hexdigest()


def complete_lines(data: bytes) -> list[bytes]:
    """A stream's complete lines, without their newlines; what follows the last newline isn't a line yet."""
    return data.split(b"\n")[:-1]


def digests_at(nonce: str, name: str, data: bytes, counts: Collection[int]) -> dict[int, str]:
    """{n: hex h(name, n)} for each n in counts, in one pass over data. ValueError for a count the stream hasn't got."""
    lines = complete_lines(data)
    if bad := [n for n in counts if not 0 <= n <= len(lines)]:
        raise ValueError(f"{name}: {max(bad)} lines counted, the file has {len(lines)}")
    h, out = RunningHash(nonce, name), dict[int, str]()
    if 0 in counts:
        out[0] = h.hexdigest()
    for i, line in enumerate(lines, 1):
        h.feed(line)
        if i in counts:
            out[i] = h.hexdigest()
    return out


def root_at(nonce: str, files: Mapping[str, bytes], lines: Mapping[str, int]) -> str:
    """The root at the line counts `lines`, recomputed from the streams' bytes (a stream not in files is empty).
    ValueError for a count the file hasn't got."""
    return root((n, c, digests_at(nonce, n, files.get(n, b""), {c})[c]) for n, c in lines.items())


def counts(files: Mapping[str, bytes]) -> dict[str, int]:
    """Every stream among files with its number of complete lines."""
    return {n: len(complete_lines(d)) for n, d in sorted(files.items()) if is_stream(n)}
