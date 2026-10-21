"""Root pytest hooks: tests that can't run on a clean checkout (CI's GitHub-hosted runner) are skipped with a reason.

Each entry is a test that needs something a fresh clone doesn't have. The condition is checked, not assumed: on a
machine that has the thing (dev2 with its local files) the test runs. Fix the dependency and drop the entry; never
add a test here to hide a failure that happens with the dependency present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

# node id -> (runs when true, reason)
NEEDS: dict[str, tuple[bool, str]] = {
    "runner/test_play.py::SessionReview::test_real_review_after_the_keeper": (
        (ROOT / "docs" / "SESSION-WATCH.md").is_file(),
        "needs the local, gitignored docs/SESSION-WATCH.md (tools/logs/session_watch.py parse_watch reads it)",
    ),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        need = NEEDS.get(item.nodeid)
        if need is not None and not need[0]:
            item.add_marker(pytest.mark.skip(reason=need[1]))
