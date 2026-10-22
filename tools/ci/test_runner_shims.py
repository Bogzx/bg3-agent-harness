"""Tests for tools/ci/runner_shims.py: shim detection, each kind of use, and the inventory of the real tree."""

from __future__ import annotations

from pathlib import Path

import pytest
import runner_shims

ALIAS = '"""Shim: bg3harness/oldmod.py."""\n\nfrom _harness import alias\n\nalias(__name__, "oldmod")\n'
WIRED = '"""The judge.\n\nShim: it lives in bg3harness.judge.\n"""\nfrom bg3harness.judge import cap, OK\n'


@pytest.mark.unit
def test_find_shim_kinds() -> None:
    assert runner_shims.find_shim("oldmod", ALIAS) == runner_shims.Shim("oldmod", "alias", "bg3harness.oldmod")
    assert runner_shims.find_shim("judge", WIRED) == runner_shims.Shim("judge", "re-export", "bg3harness.judge")
    assert (
        runner_shims.find_shim("runs", '"""Benchmark runs; parts moved to bg3bench."""\nfrom bg3bench import series\n')
        is None
    )


@pytest.mark.unit
def test_bare_imports_count_only_with_runner_on_sys_path() -> None:
    src = "import oldmod\nfrom oldmod import Event\nimport importlib\nimportlib.import_module('oldmod')\n"
    assert [(u.line, u.kind) for u in runner_shims.python_uses("runner/play.py", src, ["oldmod"])] == [
        (1, "import"),
        (2, "import"),
        (4, "import"),
    ]
    assert runner_shims.python_uses("bridge/src/bg3ai/x.py", src, ["oldmod"]) == []
    inserted = 'import sys\nsys.path.insert(0, str(REPO / "runner"))\nimport oldmod as E\n'
    assert [u.kind for u in runner_shims.python_uses("tools/vis/x.py", inserted, ["oldmod"])] == ["import"]


@pytest.mark.unit
def test_a_same_named_module_next_to_the_file_is_not_the_shim() -> None:
    src = 'import sys\nsys.path.insert(0, "runner")\nimport oldmod\n'
    assert runner_shims.python_uses("tools/logs/x.py", src, ["oldmod"], local=["oldmod"]) == []


@pytest.mark.unit
def test_dotted_path_and_mention_uses() -> None:
    src = (
        '"""Reads runner/oldmod.py\'s stream."""\n'
        "import runner.oldmod\n"
        "from runner import oldmod\n"
        "from runner.oldmod import Event\n"
        'P = REPO / "runner" / "oldmod.py"\n'
        'ARGV = ["python3", "runner/oldmod.py"]\n'
    )
    got = [(u.line, u.kind) for u in runner_shims.python_uses("tools/x.py", src, ["oldmod"])]
    assert got == [(1, "mention"), (2, "dotted"), (3, "dotted"), (4, "dotted"), (5, "path"), (6, "path")]
    assert runner_shims.text_uses(
        "docs/A.md", "see runner/oldmod.py\nnot xrunner/oldmod.py nor a/runner/oldmod.py\n", ["oldmod"]
    ) == [runner_shims.Use("oldmod", "docs/A.md", 1, "mention")]


@pytest.mark.unit
def test_scan_skips_the_shims_own_files_and_tests() -> None:
    files = {
        "runner/oldmod.py": ALIAS,
        "runner/test_oldmod.py": "import oldmod\n",
        "runner/play.py": "import oldmod\n",
        "README.md": "python3 runner/oldmod.py --help\n",
    }
    shims, uses = runner_shims.scan(files)
    assert shims == [runner_shims.Shim("oldmod", "alias", "bg3harness.oldmod")]
    assert [(u.path, u.kind) for u in uses] == [
        ("README.md", "mention"),
        ("runner/play.py", "import"),
        ("runner/test_oldmod.py", "import"),
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


@pytest.mark.unit
def test_the_apps_legacy_paths_load_is_an_import() -> None:
    src = 'from .adapters import legacy_paths\nP = legacy_paths.load("oldmod")\nQ = legacy_paths.load("bg3ai.x")\n'
    got = runner_shims.python_uses("packages/playthrough/src/bg3play/live.py", src, ["oldmod"])
    assert [(u.line, u.kind) for u in got] == [(2, "import")]


@pytest.mark.unit
def test_deleted_shims_have_no_uses_left() -> None:
    files = {
        "runner/play.py": "import gone\nimport kept\n",
        "tools/x.py": 'import sys\nsys.path.insert(0, "runner")\nfrom gone import f\n',
        "docs/A.md": "see runner/gone.py\n",
        "packages/harness/src/bg3harness/gone.py": '"""The module."""\n',
    }
    got = [(u.path, u.line, u.kind) for u in runner_shims.deleted_uses(files, ["gone"])]
    assert got == [("docs/A.md", 1, "mention"), ("runner/play.py", 1, "import"), ("tools/x.py", 3, "import")]
    back = runner_shims.deleted_uses({**files, "runner/gone.py": ALIAS}, ["gone"])
    assert ("runner/gone.py", 1, "file") in [(u.path, u.line, u.kind) for u in back]
    assert runner_shims.deleted_uses({"runner/play.py": "import kept\n"}, ["gone"]) == []


@pytest.mark.integration
def test_no_new_use_of_a_deleted_shim() -> None:
    gone = runner_shims.deleted_uses(runner_shims.tracked())
    assert gone == [], "use the package module instead (runner_shims.DELETED): " + ", ".join(
        f"{u.path}:{u.line} {u.kind} {u.shim}" for u in gone
    )
    for target in runner_shims.DELETED.values():
        module = target.removeprefix("bg3harness.")
        assert (runner_shims.ROOT / "packages/harness/src/bg3harness" / f"{module}.py").is_file(), target
