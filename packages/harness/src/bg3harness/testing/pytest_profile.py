"""pytest plugin (the pytest11 entry point in packages/harness/pyproject.toml): every pytest run in a venv that has
bg3harness, whatever its rootdir and conftests (`uv run --project packages/playthrough pytest` loads no repo-root
conftest), gets a temp game profile before it collects anything (bg3harness.testing.profile), and stops at the first
test that would start or end with a profile folder outside the temp dir (live6 §3: a run reached the real Script
Extender BG3AI folder and moved the live game's nav files away).

Only in the source tree (bg3harness.SOURCE_TREE: this repo's workspace) or with BG3HARNESS_TEMP_PROFILE=1: a venv that
installed the wheel keeps its own pytest runs' environment unless it asks."""

from __future__ import annotations

import os

import pytest

import bg3harness
from bg3harness.testing.profile import leaks, temp_profile

ACTIVE = bg3harness.SOURCE_TREE or os.environ.get("BG3HARNESS_TEMP_PROFILE") == "1"
if ACTIVE:
    temp_profile()


def _guard(item: pytest.Item, when: str) -> None:
    bad = leaks() if ACTIVE else []
    if bad:
        pytest.exit(f"{item.nodeid} {when}: the real game profile is reachable: {', '.join(bad)}", returncode=3)


def pytest_runtest_setup(item: pytest.Item) -> None:
    _guard(item, "setup")


def pytest_runtest_teardown(item: pytest.Item) -> None:
    _guard(item, "teardown")
