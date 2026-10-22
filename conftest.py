"""Root pytest hooks: tests that can't run on a clean checkout (CI's GitHub-hosted runner) are skipped with a reason.

Each entry is a test that needs something a fresh clone doesn't have. The condition is checked, not assumed: on a
machine that has the thing (the test machine with its local files) the test runs. Fix the dependency and drop the
entry; never add a test here to hide a failure that happens with the dependency present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

# node id, or a directory ending in "/" for every test under it -> (runs when true, reason)
NEEDS: dict[str, tuple[bool, str]] = {
    "runner/test_play.py::SessionReview::test_real_review_after_the_keeper": (
        (ROOT / "docs" / "SESSION-WATCH.md").is_file(),
        "needs the local, gitignored docs/SESSION-WATCH.md (tools/logs/session_watch.py parse_watch reads it)",
    ),
    # Browser tests of the built web app. Run alone (uv run --project packages/playthrough pytest -m e2e) they fail
    # loudly without the build; under the root run they wait for it.
    "packages/playthrough/tests/e2e/": (
        (ROOT / "packages" / "playthrough" / "web" / "dist" / "index.html").is_file(),
        "needs the built web app: cd packages/playthrough/web && npm ci && npm run build",
    ),
}


def _need(nodeid: str) -> tuple[bool, str] | None:
    for key, need in NEEDS.items():
        if nodeid == key or (key.endswith("/") and nodeid.startswith(key)):
            return need
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        need = _need(item.nodeid)
        if need is not None and not need[0]:
            item.add_marker(pytest.mark.skip(reason=need[1]))
