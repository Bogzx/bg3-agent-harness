"""Every tracked test file gets its own module name under pytest's prepend import mode (pyproject.toml).

A test file outside a package is imported by its basename, so two such files with the same name (runner/test_choices.py
and packages/bench/tests/test_choices.py, 2026-10-22) break collection of the whole run with "import file mismatch".
Inside a package (directories with __init__.py) the name is the dotted path from the topmost package directory.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module_name(path: str, is_package: Callable[[str], bool]) -> str:
    """The name pytest's prepend mode imports a test file under: its stem, prefixed by its enclosing packages."""
    parts = path.removesuffix(".py").split("/")
    i = len(parts) - 1
    while i > 0 and is_package("/".join(parts[:i])):
        i -= 1
    return ".".join(parts[i:])


def clashes(paths: Iterable[str], is_package: Callable[[str], bool]) -> dict[str, list[str]]:
    """Module name -> the test files that share it, for every name taken more than once."""
    by_name: defaultdict[str, list[str]] = defaultdict(list)
    for p in paths:
        by_name[module_name(p, is_package)].append(p)
    return {name: sorted(ps) for name, ps in by_name.items() if len(ps) > 1}


@pytest.mark.unit
def test_module_name_walks_up_the_packages() -> None:
    packages = {"packages/playthrough/tests", "packages/playthrough/tests/memory"}
    assert module_name("runner/test_choices.py", packages.__contains__) == "test_choices"
    name = module_name("packages/playthrough/tests/memory/test_lint.py", packages.__contains__)
    assert name == "tests.memory.test_lint"


@pytest.mark.unit
def test_clashes_finds_a_shared_basename_outside_packages() -> None:
    paths = ["runner/test_choices.py", "packages/bench/tests/test_choices.py", "tools/test_other.py"]
    assert clashes(paths, lambda _: False) == {
        "test_choices": ["packages/bench/tests/test_choices.py", "runner/test_choices.py"]
    }
    assert clashes(paths, {"packages/bench/tests"}.__contains__) == {}


@pytest.mark.integration
def test_tracked_test_files_have_unique_module_names() -> None:
    out = subprocess.run(["git", "ls-files", "*test_*.py"], cwd=ROOT, capture_output=True, text=True, check=True)
    paths = [p for p in out.stdout.split() if Path(p).name.startswith("test_") and not p.startswith("third_party/")]
    found = clashes(paths, lambda d: (ROOT / d / "__init__.py").is_file())
    assert found == {}, f"test files that pytest imports under the same name (rename one): {found}"
