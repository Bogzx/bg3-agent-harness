"""The Bench panel (Stats, Timeline) over the recorded bench runs (tests/fixtures/bench), and their payloads."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from ..conftest import FIXTURE
from .conftest import Site
from .contract import payloads, type_errors
from .test_web import no_overflow

pytestmark = pytest.mark.e2e

BENCH = FIXTURE.parent / "bench"


@pytest.fixture(scope="module")
def benched(site: Site) -> list[str]:
    """The two recorded bench runs as playthroughs of the e2e server, and a third with its manifest only."""
    runs = site["store"].runtime / "sessions"
    for run in sorted(BENCH.iterdir()):
        shutil.copytree(run, runs / run.name)
    (runs / "e2e-naut-ungraded").mkdir()
    shutil.copy(BENCH / "e2e-naut-claude-r001" / "manifest.json", runs / "e2e-naut-ungraded")
    names = ["e2e-naut-claude-r001", "e2e-naut-pi-r001", "e2e-naut-ungraded"]
    for name in names:
        (runs / name / "runner.json").write_text(json.dumps({"playthrough": name, "history": []}))
    return names


def test_stats_shows_a_graded_benchmark_run(page: Page, site: Site, benched: list[str]):
    page.goto(f"{site['url']}/#/stats/e2e-naut-claude-r001")
    grade = page.get_by_label("Benchmark grade")
    expect(grade.locator(".verdict")).to_have_text("pass")
    expect(grade).to_contain_text("naut")
    expect(grade).to_contain_text("via claude")
    expect(grade).to_contain_text("goal reached at 17m 23s")
    expect(grade.locator(".kpi")).to_have_count(9)
    curve = grade.get_by_role("img", name="Progress curve of gold")
    expect(curve.locator("circle")).to_have_count(1)  # gold 4 at 10 min; the 20 and 30 min checkpoints missed
    expect(curve.locator("text.tick.missed")).to_have_count(2)
    no_overflow(page, 1440)


def test_a_failed_bench_run_and_an_unreadable_one(page: Page, site: Site, benched: list[str]):
    page.goto(f"{site['url']}/#/stats/e2e-naut-pi-r001")
    expect(page.get_by_label("Benchmark grade").locator(".verdict")).to_have_text("fail (over budget)")
    page.goto(f"{site['url']}/#/timeline/e2e-naut-ungraded")
    expect(page.get_by_label("Benchmark grade").locator(".error")).to_contain_text("result.json is missing")


def test_the_bench_runs_payloads_match_the_clients_types(site: Site, benched: list[str], tmp_path: Path):
    found: dict[str, tuple[str, Any]] = {}
    for name in benched:
        found |= {f"{name} {k}": v for k, v in payloads(site["url"], name).items()}
    assert "manifest" in found["e2e-naut-claude-r001 bench"][1]["bench"]
    errors = type_errors(found, tmp_path)
    assert errors == "", errors
