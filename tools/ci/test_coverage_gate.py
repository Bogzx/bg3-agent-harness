"""Tests for tools/ci/coverage_gate.py: parsing the floors, the check, main over a tmp pyproject, and measure on a
real .coverage file of a tiny tmp package."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import coverage_gate
import pytest


def pyproject(mins: dict[str, object]) -> dict[str, Any]:
    return {"tool": {"bg3ci": {"coverage-min": mins}, "coverage": {"run": {"source": ["a", "b"]}}}}


@pytest.mark.unit
def test_floors_parse_percents() -> None:
    assert coverage_gate.floors(pyproject({"a": 87, "b/src": 98.8})) == {"a": 87.0, "b/src": 98.8}
    assert coverage_gate.floors({"tool": {}}) == {}


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["90", True, -1, 101])
def test_floors_reject_a_non_percent(bad: object) -> None:
    with pytest.raises(ValueError, match="expected a percent"):
        coverage_gate.floors(pyproject({"a": bad}))


@pytest.mark.unit
def test_check_passes_at_or_above_the_floor() -> None:
    assert coverage_gate.check({"a": 87.3, "b": 99.0}, {"a": 87.3, "b": 98.8}) == []


@pytest.mark.unit
def test_check_fails_below_the_floor_and_without_data() -> None:
    assert coverage_gate.check({"a": 87.2, "b": None}, {"a": 87.3, "b": 50}) == [
        "a: 87.2 % is below its floor of 87.3 %",
        "b: no measured code (floor 50 %): fix the path or the --cov source",
    ]


@pytest.mark.unit
def test_main_reports_every_path_and_fails_on_a_floor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["a", "b"]\n[tool.bg3ci.coverage-min]\n"b" = 90\n"c" = 10\n'
    )
    seen: list[list[str]] = []

    def fake(paths: list[str]) -> dict[str, float | None]:
        seen.append(paths)
        return {"a": 50.0, "b": 89.9, "c": 12.0}

    assert coverage_gate.main(fake, tmp_path) == 1
    assert seen == [["a", "b", "c"]]
    out = capsys.readouterr().out
    assert "a                             50.0 %\n" in out
    assert "b: 89.9 % is below its floor of 90 %" in out
    assert "c: " not in out.split("(floor 10 %)")[1]


@pytest.mark.integration
def test_measure_reads_the_coverage_data_per_path(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.coverage.run]\nbranch = true\n")
    for name in ("full", "half"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("")
    (tmp_path / "full" / "m.py").write_text("def f():\n    return 1\n")
    (tmp_path / "half" / "m.py").write_text("def g(x):\n    if x:\n        return 1\n    return 2\n")
    (tmp_path / "run.py").write_text("from full.m import f\nfrom half.m import g\nf()\ng(1)\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE")}  # not under pytest-cov's tracer
    subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--rcfile=pyproject.toml", "run.py"],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    totals = coverage_gate.measure(["full", "half", "none"], tmp_path)
    assert totals["full"] == 100.0
    assert totals["half"] is not None and 0 < totals["half"] < 100
    assert totals["none"] is None
