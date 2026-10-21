"""Package boundaries import-linter can't see (research/arch/harness.md 3.4): sys.path edits and path literals.

The repo crosses package lines by path and subprocess, not only by import (`sys.path.insert(... "tools/memory")`,
`REPO / "tools" / "memory" / "session.py"`). This scans the packaged sources (packages/*/src and bridge/src) and
reports:

- any `sys.path` edit (insert, append, extend, assignment): packaged code imports through its dependencies;
- any string literal, or `"a" / "b"` chain of literals, that names a path owned by a package the source must not
  depend on (FORBIDDEN below). Docstrings are not scanned.

REPORT MODE until Phase D (ENFORCE = False): the test prints the violations as a warning and passes. To enforce,
set ENFORCE = True (or BG3AI_BOUNDARIES_ENFORCE=1 in the CI step); then any violation fails the test.

    uv run python tools/ci/test_boundaries.py      # print the violations
"""

from __future__ import annotations

import ast
import os
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENFORCE = os.environ.get("BG3AI_BOUNDARIES_ENFORCE") == "1"

# Paths each side owns (TARGET-ARCHITECTURE.md, research/arch/harness.md 3.3).
APP = ("playthroughs/", "memory-template/", "tools/memory", "tools/logs", "packages/app")
BENCH = ("scenarios/", "packages/bench")
# Source root (relative to ROOT) -> path fragments it may not name.
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "bridge/src": APP + BENCH,
    "packages/harness/src": APP + BENCH,
    "packages/bench/src": APP,
    "packages/app/src": BENCH,
}
SYS_PATH_EDITS = {"insert", "append", "extend", "remove", "clear", "pop", "__setitem__"}


def _is_sys_path(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _div_chain(node: ast.BinOp) -> str | None:
    """'tools/memory' for `X / "tools" / "memory"`: the string literals of a `/` chain joined, None if fewer than 2."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        if isinstance(cur.right, ast.Constant) and isinstance(cur.right.value, str):
            parts.append(cur.right.value)
        cur = cur.left
    if isinstance(cur, ast.Constant) and isinstance(cur.value, str):
        parts.append(cur.value)
    return "/".join(reversed(parts)) + "/" if len(parts) >= 2 else None


def violations(source: str, filename: str, forbidden: tuple[str, ...]) -> list[str]:
    """One message per sys.path edit or forbidden path literal in the source, in line order."""
    found = list(_find(source, filename, forbidden))
    return sorted(found, key=lambda m: int(m.split(":")[1]))


def _find(source: str, filename: str, forbidden: tuple[str, ...]) -> Iterator[str]:
    tree = ast.parse(source, filename)
    docs = _docstrings(tree)
    chained: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _is_sys_path(node.func.value):
            if node.func.attr in SYS_PATH_EDITS:
                yield f"{filename}:{node.lineno}: sys.path.{node.func.attr}(...)"
        elif isinstance(node, ast.Assign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if _is_sys_path(t) or (isinstance(t, ast.Subscript) and _is_sys_path(t.value)):
                    yield f"{filename}:{node.lineno}: assignment to sys.path"
        elif isinstance(node, ast.BinOp) and id(node) not in chained and (joined := _div_chain(node)):
            for sub in ast.walk(node):
                chained.add(id(sub))
            hits = [f for f in forbidden if f.rstrip("/") + "/" in joined]
            if hits:
                yield f"{filename}:{node.lineno}: path {joined.rstrip('/')!r} names {hits[0].rstrip('/')}"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and id(node) not in chained
        ):
            hits = [f for f in forbidden if f in node.value]
            if hits:
                yield f"{filename}:{node.lineno}: literal names {hits[0].rstrip('/')}"


def scan(root: Path = ROOT) -> list[str]:
    """Violations in every packaged source file under root."""
    found: list[str] = []
    for src, forbidden in FORBIDDEN.items():
        for path in sorted((root / src).rglob("*.py")):
            found += violations(path.read_text(), path.relative_to(root).as_posix(), forbidden)
    return found


@pytest.mark.unit
def test_sys_path_edits_are_found() -> None:
    src = "import sys\nsys.path.insert(0, 'x')\nsys.path.append('y')\nsys.path = []\n"
    src += "sys.path[0] = 'z'\nsys.path += ['w']\n"
    assert violations(src, "m.py", ()) == [
        "m.py:2: sys.path.insert(...)",
        "m.py:3: sys.path.append(...)",
        "m.py:4: assignment to sys.path",
        "m.py:5: assignment to sys.path",
        "m.py:6: assignment to sys.path",
    ]


@pytest.mark.unit
def test_reading_sys_path_is_fine() -> None:
    assert violations("import sys\nprint(sys.path[0], list(sys.path))\n", "m.py", ()) == []


@pytest.mark.unit
def test_path_literals_and_chains_are_found() -> None:
    src = 'P = "tools/memory/session.py"\nQ = REPO / "tools" / "memory" / "lint.py"\nR = REPO / "tools" / "dev"\n'
    assert violations(src, "m.py", APP) == [
        "m.py:1: literal names tools/memory",
        "m.py:2: path 'tools/memory/lint.py' names tools/memory",
    ]


@pytest.mark.unit
def test_docstrings_and_allowed_paths_are_fine() -> None:
    src = '"""Uses tools/memory/lint.py."""\ndef f():\n    """See playthroughs/x."""\n    return "tools/dev/x.py"\n'
    assert violations(src, "m.py", APP) == []


@pytest.mark.unit
def test_scan_uses_each_roots_own_rules(tmp_path: Path) -> None:
    (tmp_path / "packages/bench/src/b").mkdir(parents=True)
    (tmp_path / "packages/bench/src/b/m.py").write_text('S = "scenarios/naut.json"\nA = "packages/app/x"\n')
    assert scan(tmp_path) == ["packages/bench/src/b/m.py:2: literal names packages/app"]


@pytest.mark.integration
def test_packaged_sources_respect_the_boundaries() -> None:
    found = scan()
    if found and not ENFORCE:
        warnings.warn("REPORT MODE, boundary violations:\n" + "\n".join(found), stacklevel=1)
        return
    assert found == []


if __name__ == "__main__":
    report = scan()
    print("\n".join(report) or "boundaries: OK")
    print(f"boundaries: {len(report)} violations ({'enforced' if ENFORCE else 'REPORT MODE'})")
    sys.exit(1 if report and ENFORCE else 0)
