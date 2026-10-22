"""Tests for tools/ci/task.py: its command table matches .github/workflows/ci.yml, and the runner's control flow."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import task

CI_YML = task.ROOT / ".github/workflows/ci.yml"

# ci.yml commands the runner leaves out on purpose, with the reason.
CI_ONLY = {
    "lint": set[str](),
    "test": set[str](),
    "e2e": {
        # writes the Playwright version to $GITHUB_OUTPUT for the browser cache key
        'echo "version=$(uv run python -c \'from importlib.metadata import version; print(version("playwright"))\')"'
        ' >> "$GITHUB_OUTPUT"',
    },
    # prefetches the images; docker run pulls them when missing
    "lua": {"docker pull -q nickblah/lua:5.3", "docker pull -q nickblah/lua:5.4"},
}
# Commands the runner has before ci.yml does (appended to that job's commands); drop an entry once ci.yml runs it.
AHEAD_OF_CI = {"lint": ("uv run python tools/ci/scrub.py --no-local-terms",)}
# Flags CI adds only for GitHub's output.
CI_FLAGS = (" --output-format github",)


def ci_runs(text: str) -> dict[str, list[tuple[str, str]]]:
    """Per job id, its steps' (working directory, run text) in order; a multi-line `run: |` block is one entry."""
    jobs: dict[str, list[tuple[str, str]]] = {}
    job = ""
    cwd = "."
    in_jobs = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line == "jobs:":
            in_jobs = True
        elif in_jobs and (m := re.match(r"^  ([\w-]+):$", line)):
            job = m.group(1)
            jobs[job] = []
        elif re.match(r"^      - ", line):
            cwd = "."
        if not job:
            continue
        if m := re.match(r"^\s+(?:- )?working-directory: (.+)$", line):
            cwd = m.group(1)
        elif m := re.match(r"^(\s+)(?:- )?run: (.*)$", line):
            body = m.group(2)
            if body == "|":
                block: list[str] = []
                while i < len(lines) and (
                    not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > len(m.group(1))
                ):
                    block.append(lines[i].strip())
                    i += 1
                body = "\n".join(block).strip()
            jobs[job].append((cwd, body))
    return jobs


def single_commands(job: str, runs: list[tuple[str, str]]) -> list[tuple[str, tuple[str, ...]]]:
    """The job's one-line commands as (working directory, argv), split at `&&`, without the CI-only ones."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for cwd, body in runs:
        if "\n" in body:
            continue
        for cmd in body.split(" && "):
            for flag in CI_FLAGS:
                cmd = cmd.replace(flag, "")
            if cmd not in CI_ONLY[job]:
                out.append((cwd, tuple(shlex.split(cmd))))
    return out


def table(name: str) -> list[tuple[str, tuple[str, ...]]]:
    return [(s.cwd, s.argv) for s in task.steps(name)]


def block(runs: list[tuple[str, str]], needle: str) -> str:
    return next(body for _, body in runs if "\n" in body and needle in body)


@pytest.fixture(scope="module")
def ci() -> dict[str, list[tuple[str, str]]]:
    return ci_runs(CI_YML.read_text())


@pytest.mark.integration
def test_every_ci_job_has_a_task(ci: dict[str, list[tuple[str, str]]]) -> None:
    assert sorted(ci) == sorted(task.CI_JOB.values())


@pytest.mark.integration
@pytest.mark.parametrize("name", ["check", "test", "lua"])
def test_task_runs_exactly_the_ci_jobs_commands(ci: dict[str, list[tuple[str, str]]], name: str) -> None:
    job = task.CI_JOB[name]
    ahead = [(".", tuple(shlex.split(cmd))) for cmd in AHEAD_OF_CI.get(job, ())]
    assert table(name) == single_commands(job, ci[job]) + ahead


@pytest.mark.integration
def test_ahead_of_ci_lists_only_commands_ci_yml_lacks(ci: dict[str, list[tuple[str, str]]]) -> None:
    for job, cmds in AHEAD_OF_CI.items():
        assert not set(cmds) & {body for _, body in ci[job]}, f"ci.yml runs it now: drop it from AHEAD_OF_CI[{job!r}]"


@pytest.mark.integration
def test_e2e_leaves_out_only_the_sudo_install_of_system_libraries(ci: dict[str, list[tuple[str, str]]]) -> None:
    assert table("e2e") == single_commands("e2e", ci["e2e"])
    # CI's one multi-line step here installs system libraries with sudo, which the runner leaves to the user
    assert "playwright install-deps chromium" in block(ci["e2e"], "install-deps")


@pytest.mark.integration
def test_quick_is_the_check_jobs_ruff_steps_and_the_test_jobs_pytest_collected(
    ci: dict[str, list[tuple[str, str]]],
) -> None:
    check = single_commands("lint", ci["lint"])
    pytest_run = next(argv for _, argv in single_commands("test", ci["test"]) if "pytest" in argv)
    marker = pytest_run[pytest_run.index("-m") : pytest_run.index("-m") + 2]
    assert [(s.cwd, s.argv) for s in task.QUICK[:2]] == check[1:3]
    assert task.QUICK[2].argv == ("uv", "run", "pytest", "--collect-only", "-qq", *marker)
    assert task.steps("quick") == task.QUICK and "quick" not in task.expand(["ci"])


@pytest.mark.integration
def test_ts_runs_each_ci_check_in_every_package(ci: dict[str, list[tuple[str, str]]]) -> None:
    script = block(ci["typescript"], "npx tsc --noEmit")
    for argv in task.TS_CHECKS:
        assert shlex.join(argv) in script
    find = block(ci["typescript"], "find packages tools")
    assert "-name package.json -not -path '*/node_modules/*'" in find
    dirs = task.ts_packages(task.ROOT)
    assert "packages/playthrough/web" in dirs
    assert [s.cwd for s in task.steps("ts")] == [d for d in dirs for _ in task.TS_CHECKS]


@pytest.mark.unit
def test_ci_runs_parses_steps_blocks_and_working_directories() -> None:
    text = """jobs:
  a:
    steps:
      - run: uv sync --locked
      - name: build
        working-directory: web
        run: npm ci && npm run build
      - name: multi
        run: |
          for d in x; do
            echo $d
          done
  b:
    steps:
      - run: bash x.sh
"""
    assert ci_runs(text) == {
        "a": [(".", "uv sync --locked"), ("web", "npm ci && npm run build"), (".", "for d in x; do\necho $d\ndone")],
        "b": [(".", "bash x.sh")],
    }


@pytest.mark.unit
def test_ts_packages_skips_node_modules(tmp_path: Path) -> None:
    for p in (
        "packages/a/package.json",
        "tools/b/package.json",
        "tools/b/node_modules/c/package.json",
        "x/package.json",
    ):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("{}")
    assert task.ts_packages(tmp_path) == ["packages/a", "tools/b"]


@pytest.mark.unit
def test_a_job_stops_at_its_first_failure_and_ci_keeps_going(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(task.JOBS, "check", (task.Step(("ok",)), task.Step(("bad",)), task.Step(("never",))))
    monkeypatch.setitem(task.JOBS, "lua", (task.Step(("ok", "2"), "sub"),))
    ran: list[task.Step] = []
    lines: list[str] = []

    def runner(step: task.Step) -> int:
        ran.append(step)
        return 1 if step.argv == ("bad",) else 0

    assert task.run_tasks(["check", "lua"], runner, lines.append, tmp_path) == 1
    assert [s.argv for s in ran] == [("ok",), ("bad",), ("ok", "2")]
    assert "[check] FAILED (exit 1): bad" in lines
    assert "[lua] $ (cd sub && ok 2)" in lines
    assert lines[-1] == "summary: check FAILED, lua ok"


@pytest.mark.unit
def test_all_passing_exits_zero_without_a_summary_for_one_task(tmp_path: Path) -> None:
    lines: list[str] = []
    assert task.run_tasks(["lua"], lambda _: 0, lines.append, tmp_path) == 0
    assert lines == ["[lua] $ bash tools/ci/lua_suites.sh"]


@pytest.mark.unit
def test_ci_expands_to_every_task_in_ci_order() -> None:
    assert task.expand(["ci"]) == ["check", "test", "e2e", "lua", "ts"]
    assert task.expand(["lua", "check", "lua"]) == ["lua", "check"]


@pytest.mark.unit
def test_list_prints_commands_and_no_task_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert task.main(["--list", "lua"]) == 0
    assert capsys.readouterr().out == "lua: bash tools/ci/lua_suites.sh\n"
    assert task.main([]) == 2
