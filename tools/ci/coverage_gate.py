"""Coverage per package, and the floors CI holds them to (pyproject.toml [tool.bg3ci.coverage-min]).

Reads the .coverage data of a `pytest --cov` run, prints one total (line + branch, as `coverage report` counts it)
for each path of [tool.coverage.run] source and each floored path, and exits 1 when a floored path is below its floor
or has no measured code. Floors only go up: raise one when a package's measured value passes it by more than 1 %.

    uv run pytest -m "not live and not e2e" --cov --cov-report= && uv run python tools/ci/coverage_gate.py
"""

from __future__ import annotations

import io
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def floors(pyproject: Mapping[str, Any]) -> dict[str, float]:
    """The floored paths of a parsed pyproject: path -> minimum percent."""
    table: dict[str, object] = pyproject.get("tool", {}).get("bg3ci", {}).get("coverage-min", {})
    out: dict[str, float] = {}
    for path, floor in table.items():
        if not isinstance(floor, int | float) or isinstance(floor, bool) or not 0 <= floor <= 100:
            raise ValueError(f"coverage-min {path!r}: expected a percent, got {floor!r}")
        out[path] = float(floor)
    return out


def check(totals: Mapping[str, float | None], mins: Mapping[str, float]) -> list[str]:
    """The failures: a floored path below its floor, or without measured code (None)."""
    errors: list[str] = []
    for path, floor in mins.items():
        got = totals.get(path)
        if got is None:
            errors.append(f"{path}: no measured code (floor {floor:g} %): fix the path or the --cov source")
        elif got < floor:
            errors.append(f"{path}: {got:.1f} % is below its floor of {floor:g} %")
    return errors


def measure(paths: list[str], root: Path = ROOT) -> dict[str, float | None]:
    """Total percent per path from root/.coverage; None where nothing under the path was measured."""
    import coverage
    from coverage.exceptions import NoDataError

    cov = coverage.Coverage(data_file=str(root / ".coverage"), config_file=str(root / "pyproject.toml"))
    cov.load()
    out: dict[str, float | None] = {}
    for path in paths:
        try:
            out[path] = cov.report(include=[str(root / path / "*")], file=io.StringIO())
        except NoDataError:
            out[path] = None
    return out


def main(measure: Callable[[list[str]], dict[str, float | None]] = measure, root: Path = ROOT) -> int:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    mins = floors(pyproject)
    source: list[str] = pyproject.get("tool", {}).get("coverage", {}).get("run", {}).get("source", [])
    paths = list(dict.fromkeys([*source, *mins]))
    totals = measure(paths)
    for path in paths:
        got = totals[path]
        floor = f"  (floor {mins[path]:g} %)" if path in mins else ""
        print(f"{path:28} {'-' if got is None else f'{got:5.1f} %'}{floor}")
    errors = check(totals, mins)
    for e in errors:
        print(f"::error::coverage {e}" if "GITHUB_ACTIONS" in os.environ else f"FAIL {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
