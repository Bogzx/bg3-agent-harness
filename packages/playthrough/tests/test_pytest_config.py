"""The package's own pytest config (used by `uv run --project packages/playthrough pytest`, and by any run that names
a path under packages/playthrough: pytest's rootdir is then this package) keeps the repo root's guards: the same
markers, strict markers and config, and the hang timeout."""

import tomllib
from pathlib import Path
from typing import cast

import pytest

PKG = Path(__file__).resolve().parents[1]


def pytest_ini(pyproject: Path) -> dict[str, object]:
    return tomllib.loads(pyproject.read_text())["tool"]["pytest"]["ini_options"]


def names(markers: object) -> set[str]:
    assert isinstance(markers, list)
    return {str(m).split(":")[0] for m in cast(list[object], markers)}


@pytest.mark.unit
def test_the_package_config_keeps_the_roots_guards():
    root, pkg = pytest_ini(PKG.parents[1] / "pyproject.toml"), pytest_ini(PKG / "pyproject.toml")
    assert names(pkg["markers"]) == names(root["markers"])
    assert (pkg["timeout"], pkg["timeout_method"]) == (root["timeout"], root["timeout_method"])
    assert {"--strict-markers", "--strict-config"} <= set(str(pkg["addopts"]).split())
