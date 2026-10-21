"""Incremental file tailing by byte offset (copy of runner/dashboard/feeds.py read_new; delete that in Phase D)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def read_new(path: Path, offset: int, initial_bytes: int = 64 * 1024,
             max_bytes: int = 512 * 1024) -> tuple[int, str, bool]:
    """Returns (new_offset, complete_lines_text, reset).

    offset < 0 means "start near the end" (the last `initial_bytes`, from a line boundary). If the file shrank
    (rotated or truncated), restart from 0. `reset` tells the reader to drop what it shows. Only complete lines are
    returned; a trailing partial line is left for the next call.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0, "", offset > 0
    reset = offset < 0 or offset > size
    if offset < 0:
        start = max(0, size - initial_bytes)
    elif offset > size:
        start = 0
    else:
        start = offset
    skip_partial = offset < 0 and start > 0
    if start >= size:
        return start, "", reset
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(max_bytes)
    if skip_partial:
        nl = data.find(b"\n")
        if nl < 0:
            return start, "", reset
        start += nl + 1
        data = data[nl + 1:]
    cut = data.rfind(b"\n")
    if cut < 0:
        # No newline at all: wait for more, unless one giant line fills the window (skip it).
        return (start + len(data) if len(data) >= max_bytes else start), "", reset
    chunk = data[: cut + 1]
    return start + len(chunk), chunk.decode("utf-8", errors="replace"), reset


@dataclass
class Tailer:
    """One file followed from near its end; `lines()` returns the complete lines added since the last call."""

    path: Path
    initial_bytes: int = 64 * 1024
    offset: int = -1

    def lines(self) -> tuple[list[str], bool]:
        """(new lines, reset): reset is True on the first read and when the file was truncated or replaced."""
        self.offset, text, reset = read_new(self.path, self.offset, self.initial_bytes)
        return [ln for ln in text.splitlines() if ln.strip()], reset
