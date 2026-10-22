"""tests/e2e/webbuild.py: the e2e tests refuse a web/dist that is missing or older than a source file."""

import os
from pathlib import Path

import pytest

from .e2e.webbuild import build_problem


def web(tmp_path: Path) -> Path:
    for f in ("src/lib/api.ts", "src/screens/Launch.svelte", "index.html", "package.json", "dist/index.html"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    for f in tmp_path.rglob("*"):
        os.utime(f, (1000, 1000))
    os.utime(tmp_path / "dist" / "index.html", (2000, 2000))
    return tmp_path


@pytest.mark.unit
def test_a_build_newer_than_its_sources_is_fine(tmp_path: Path):
    assert build_problem(web(tmp_path)) is None


@pytest.mark.unit
def test_a_source_changed_after_the_build_names_the_file(tmp_path: Path):
    w = web(tmp_path)
    os.utime(w / "src" / "screens" / "Launch.svelte", (3000, 3000))
    assert build_problem(w) == (
        "the built web app is older than src/screens/Launch.svelte: run `npm ci && npm run build` in "
        "packages/playthrough/web"
    )
    os.utime(w / "src" / "screens" / "Launch.svelte", (1000, 1000))
    os.utime(w / "package.json", (3000, 3000))
    assert (build_problem(w) or "").startswith("the built web app is older than package.json")


@pytest.mark.unit
def test_no_build(tmp_path: Path):
    w = web(tmp_path)
    (w / "dist" / "index.html").unlink()
    assert (build_problem(w) or "").startswith(f"no built web app at {w / 'dist'}")
