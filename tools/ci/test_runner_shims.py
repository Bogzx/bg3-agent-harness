"""Tests for tools/ci/runner_shims.py: shim detection, each kind of use, and the inventory of the real tree."""

from __future__ import annotations

from pathlib import Path

import pytest
import runner_shims

ALIAS = '"""Shim: bg3harness/events.py."""\n\nfrom _harness import alias\n\nalias(__name__, "events")\n'
WIRED = '"""The judge.\n\nShim: it lives in bg3harness.judge.\n"""\nfrom bg3harness.judge import cap, OK\n'


@pytest.mark.unit
def test_find_shim_kinds() -> None:
    assert runner_shims.find_shim("events", ALIAS) == runner_shims.Shim("events", "alias", "bg3harness.events")
    assert runner_shims.find_shim("judge", WIRED) == runner_shims.Shim("judge", "re-export", "bg3harness.judge")
    assert (
        runner_shims.find_shim("runs", '"""Benchmark runs; parts moved to bg3bench."""\nfrom bg3bench import series\n')
        is None
    )


@pytest.mark.unit
def test_bare_imports_count_only_with_runner_on_sys_path() -> None:
    src = "import events\nfrom events import Event\nimport importlib\nimportlib.import_module('events')\n"
    assert [(u.line, u.kind) for u in runner_shims.python_uses("runner/play.py", src, ["events"])] == [
        (1, "import"),
        (2, "import"),
        (4, "import"),
    ]
    assert runner_shims.python_uses("bridge/src/bg3ai/x.py", src, ["events"]) == []
    inserted = 'import sys\nsys.path.insert(0, str(REPO / "runner"))\nimport events as E\n'
    assert [u.kind for u in runner_shims.python_uses("tools/vis/x.py", inserted, ["events"])] == ["import"]


@pytest.mark.unit
def test_a_same_named_module_next_to_the_file_is_not_the_shim() -> None:
    src = 'import sys\nsys.path.insert(0, "runner")\nimport events\n'
    assert runner_shims.python_uses("tools/logs/x.py", src, ["events"], local=["events"]) == []


@pytest.mark.unit
def test_dotted_path_and_mention_uses() -> None:
    src = (
        '"""Reads runner/events.py\'s stream."""\n'
        "import runner.events\n"
        "from runner import events\n"
        "from runner.events import Event\n"
        'P = REPO / "runner" / "events.py"\n'
        'ARGV = ["python3", "runner/events.py"]\n'
    )
    got = [(u.line, u.kind) for u in runner_shims.python_uses("tools/x.py", src, ["events"])]
    assert got == [(1, "mention"), (2, "dotted"), (3, "dotted"), (4, "dotted"), (5, "path"), (6, "path")]
    assert runner_shims.text_uses(
        "docs/A.md", "see runner/events.py\nnot xrunner/events.py nor a/runner/events.py\n", ["events"]
    ) == [runner_shims.Use("events", "docs/A.md", 1, "mention")]


@pytest.mark.unit
def test_scan_skips_the_shims_own_files_and_tests() -> None:
    files = {
        "runner/events.py": ALIAS,
        "runner/test_events.py": "import events\n",
        "runner/play.py": "import events\n",
        "README.md": "python3 runner/events.py --help\n",
    }
    shims, uses = runner_shims.scan(files)
    assert shims == [runner_shims.Shim("events", "alias", "bg3harness.events")]
    assert [(u.path, u.kind) for u in uses] == [
        ("README.md", "mention"),
        ("runner/play.py", "import"),
        ("runner/test_events.py", "import"),
    ]


@pytest.mark.integration
def test_the_tree_s_alias_shims_point_at_existing_package_modules() -> None:
    shims, uses = runner_shims.scan(runner_shims.tracked())
    aliases = [s for s in shims if s.kind == "alias"]
    assert aliases, "no alias shims found: runner/_harness.py's convention changed? update runner_shims.py"
    for s in aliases:
        module = s.target.removeprefix("bg3harness.")
        assert (runner_shims.ROOT / "packages/harness/src/bg3harness" / f"{module}.py").is_file(), s
    assert {u.shim for u in uses} <= {s.name for s in shims}
    assert all(Path(u.path).name != f"{u.shim}.py" or not u.path.startswith("runner/") for u in uses)
