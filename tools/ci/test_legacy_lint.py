"""Tests for tools/ci/legacy_lint.py: the stale-entry logic, and the ruff runs on a tmp tree."""

from __future__ import annotations

from pathlib import Path

import legacy_lint
import pytest


@pytest.mark.unit
def test_clean_file_must_leave_the_ignore_list() -> None:
    problems = legacy_lint.stale_ignores({"a.py": ["E501"]}, {})
    assert problems == ["ruff per-file-ignores: a.py is clean now: remove its entry"]


@pytest.mark.unit
def test_code_no_longer_broken_must_leave_the_entry() -> None:
    problems = legacy_lint.stale_ignores({"a.py": ["E501", "F401"]}, {"a.py": {"E501"}})
    assert problems == ["ruff per-file-ignores: a.py no longer breaks F401: remove them"]


@pytest.mark.unit
def test_exact_ignores_and_new_violations_are_not_stale() -> None:
    # A violation missing from the list is ruff's own failure, not a stale entry.
    assert legacy_lint.stale_ignores({"a.py": ["E501"]}, {"a.py": {"E501"}, "b.py": {"F401"}}) == []


@pytest.mark.unit
def test_stale_paths() -> None:
    assert legacy_lint.stale_paths("pyright ignore", ["a.py", "b.py"], {"b.py", "c.py"}) == [
        "pyright ignore: a.py no longer needs the exemption: remove it"
    ]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "ruff.toml").write_text('line-length = 120\n[lint]\nselect = ["E", "F", "RUF"]\n')
    (tmp_path / "unused.py").write_text("import os\n")
    (tmp_path / "noqa.py").write_text("import os  # noqa: F401\nimport sys\n")
    (tmp_path / "ugly.py").write_text("x = {  'a':1 }\n")
    (tmp_path / "clean.py").write_text('X = {"a": 1}\n')
    monkeypatch.setattr(legacy_lint, "ROOT", tmp_path)
    return tmp_path


@pytest.mark.integration
def test_ruff_needed_on_a_tree(tree: Path) -> None:
    assert legacy_lint.ruff_needed({}) == {"unused.py": {"F401"}, "noqa.py": {"F401"}}


@pytest.mark.integration
def test_ruff_needed_keeps_the_unused_noqa_an_ignore_causes(tree: Path) -> None:
    # With F401 ignored for noqa.py, its noqa comment is unused (RUF100): that entry is needed, not stale.
    needed = legacy_lint.ruff_needed({"noqa.py": ["F401", "RUF100"]})
    assert needed["noqa.py"] == {"F401", "RUF100"}


@pytest.mark.integration
def test_format_needed_on_a_tree(tree: Path) -> None:
    assert legacy_lint.format_needed() == {"ugly.py"}
