"""The 1000-line cap (TARGET-ARCHITECTURE.md "Engineering standards", user 2026-10-21).

Every tracked source or test file (.py .ts .tsx .js .lua .sh) has at most 1000 lines, unless
tools/ci/line_cap_allowlist.txt lists it. The allowlist has two kinds of lines:

    <lines> <path>             a legacy file over the cap, at exactly its current length: it may only shrink
    exempt <path>  # <reason>  a file the cap doesn't apply to (a generated data table); the reason is required

The list must match reality. A listed file that grew fails; one that shrank fails until its number is lowered; one
at or below the cap, or gone, fails until its line is removed. So the list only gets shorter, and no file joins it
except by an edit a reviewer sees.

    uv run python tools/ci/line_cap.py            # check the tree (git ls-files)
    uv run python tools/ci/line_cap.py --current  # print the over-cap files as allowlist lines
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("line_cap_allowlist.txt")
CAP = 1000
SUFFIXES = (".py", ".ts", ".tsx", ".js", ".lua", ".sh")


@dataclass(frozen=True)
class Allowlist:
    legacy: dict[str, int]  # path -> recorded length
    exempt: dict[str, str]  # path -> reason


def parse_allowlist(text: str) -> Allowlist:
    """Parse the allowlist; raises ValueError on a malformed line, a duplicate path or an exemption without reason."""
    legacy: dict[str, int] = {}
    exempt: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        body, _, comment = raw.partition("#")
        fields = body.split()
        if not fields:
            continue
        if len(fields) != 2:
            raise ValueError(f"line {n}: expected '<lines> <path>' or 'exempt <path>  # <reason>': {raw!r}")
        kind, path = fields
        if path in legacy or path in exempt:
            raise ValueError(f"line {n}: {path} is listed twice")
        if kind == "exempt":
            if not comment.strip():
                raise ValueError(f"line {n}: exemption of {path} needs a reason after '#'")
            exempt[path] = comment.strip()
        elif kind.isdigit():
            legacy[path] = int(kind)
        else:
            raise ValueError(f"line {n}: expected a line count or 'exempt': {raw!r}")
    return Allowlist(legacy, exempt)


def count_lines(data: bytes) -> int:
    """Lines as an editor shows them: a last line without a newline still counts."""
    return len(data.splitlines())


def check(lengths: Mapping[str, int], allow: Allowlist, cap: int = CAP) -> list[str]:
    """Problems with the tree (path -> line count of every capped file) against the allowlist."""
    problems: list[str] = []
    for path, n in sorted(lengths.items()):
        if path in allow.exempt:
            continue
        recorded = allow.legacy.get(path)
        if recorded is None:
            if n > cap:
                problems.append(f"{path}: {n} lines, over the {cap}-line cap: split it")
        elif n <= cap:
            problems.append(f"{path}: {n} lines, at or below the cap now: remove it from the allowlist")
        elif n > recorded:
            problems.append(f"{path}: grew from {recorded} to {n} lines: listed files may only shrink")
        elif n < recorded:
            problems.append(f"{path}: shrank from {recorded} to {n} lines: lower its allowlist entry to {n}")
    for path in sorted((allow.legacy.keys() | allow.exempt.keys()) - lengths.keys()):
        problems.append(f"{path}: listed but not a tracked capped file: remove it from the allowlist")
    return problems


def tracked_lengths(root: Path, suffixes: Iterable[str] = SUFFIXES) -> dict[str, int]:
    """Line count of every git-tracked file under root with one of the suffixes (submodule contents excluded)."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    wanted = tuple(suffixes)
    lengths: dict[str, int] = {}
    for name in out.decode().split("\0"):
        path = root / name
        if name.endswith(wanted) and path.is_file():
            lengths[name] = count_lines(path.read_bytes())
    return lengths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--current", action="store_true", help="print the over-cap files as allowlist lines")
    args = ap.parse_args(argv)
    lengths = tracked_lengths(ROOT)
    if args.current:
        for path, n in sorted(lengths.items(), key=lambda kv: (-kv[1], kv[0])):
            if n > CAP:
                print(f"{n} {path}")
        return 0
    allow = parse_allowlist(ALLOWLIST.read_text())
    problems = check(lengths, allow)
    for p in problems:
        print(p)
    print(f"line cap: {len(problems)} problems" if problems else f"line cap: OK ({len(lengths)} files)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
