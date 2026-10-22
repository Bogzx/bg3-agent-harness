"""Run what CI runs (.github/workflows/ci.yml), job by job, before you open a pull request.

    uv run tools/ci/task.py ci          # every job below, in CI order; keeps going and prints a summary
    uv run tools/ci/task.py check       # job "lint": ruff, ruff format, legacy lists, pyright, import rules, line cap
    uv run tools/ci/task.py test        # job "test": pytest -m "not live and not e2e" with the coverage floors
    uv run tools/ci/task.py e2e         # job "e2e": builds the web app, installs pi, pytest -m "e2e and not live"
    uv run tools/ci/task.py lua         # job "lua": the Lua suites in Docker
    uv run tools/ci/task.py ts          # job "typescript": npm ci, tsc --noEmit, check, lint, vitest per package
    uv run tools/ci/task.py --list [job...]   # print the commands without running them

Each command is the one in ci.yml (tools/ci/test_task.py fails when the two drift apart). Not run here: CI's
`playwright install-deps chromium` and the ffmpeg install (system packages that need sudo; the e2e job's tests say
what is missing). The test and e2e jobs are heavy: on a shared machine cap them, for example
`systemd-run --user --scope -p MemoryMax=8G nice -n 15 uv run tools/ci/task.py test`.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NPM_CI = ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    cwd: str = "."  # relative to the repo root

    def show(self) -> str:
        cmd = shlex.join(self.argv)
        return cmd if self.cwd == "." else f"(cd {self.cwd} && {cmd})"


SYNC = Step(("uv", "sync", "--locked"))


def uv(*args: str) -> Step:
    return Step(("uv", "run", *args))


JOBS: dict[str, tuple[Step, ...]] = {
    "check": (
        SYNC,
        uv("ruff", "check", "."),
        uv("ruff", "format", "--check", "."),
        uv("python", "tools/ci/legacy_lint.py"),
        uv("pyright"),
        uv("python", "tools/ci/import_contracts.py"),
        uv("python", "tools/ci/test_boundaries.py"),
        uv("python", "tools/ci/line_cap.py"),
    ),
    "test": (
        SYNC,
        uv("pytest", "-m", "not live and not e2e", "--cov", "--cov-report="),
        uv("python", "tools/ci/coverage_gate.py"),
    ),
    "e2e": (
        SYNC,
        Step(NPM_CI, "packages/playthrough/web"),
        Step(("npm", "run", "build"), "packages/playthrough/web"),
        Step(NPM_CI, "tools/pi"),
        uv("playwright", "install", "chromium"),
        uv("pytest", "-m", "e2e and not live"),
    ),
    "lua": (Step(("bash", "tools/ci/lua_suites.sh")),),
}

# CI job id (ci.yml) of each task; "ts" steps are built per package (ts_steps).
CI_JOB = {"check": "lint", "test": "test", "e2e": "e2e", "lua": "lua", "ts": "typescript"}
TS_CHECKS: tuple[tuple[str, ...], ...] = (
    NPM_CI,
    ("npx", "tsc", "--noEmit"),
    ("npm", "run", "--if-present", "check"),
    ("npm", "run", "--if-present", "lint"),
    ("npm", "run", "--if-present", "test"),
)


def ts_packages(root: Path) -> list[str]:
    """The TypeScript packages CI checks: every package.json under packages/ or tools/ outside node_modules."""
    found = {
        p.parent.relative_to(root).as_posix()
        for top in ("packages", "tools")
        for p in (root / top).rglob("package.json")
        if "node_modules" not in p.parts
    }
    return sorted(found)


def ts_steps(root: Path) -> tuple[Step, ...]:
    return tuple(Step(argv, d) for d in ts_packages(root) for argv in TS_CHECKS)


def steps(task: str, root: Path = ROOT) -> tuple[Step, ...]:
    return ts_steps(root) if task == "ts" else JOBS[task]


TASKS = (*JOBS, "ts")


Runner = Callable[[Step], int]


def run_step(step: Step) -> int:
    return subprocess.run(step.argv, cwd=ROOT / step.cwd, check=False).returncode


def run_task(task: str, steps_: Sequence[Step], runner: Runner, out: Callable[[str], None]) -> bool:
    """Runs a job's steps in order and stops at the first failure, as a CI job does. True when all passed."""
    for step in steps_:
        out(f"[{task}] $ {step.show()}")
        rc = runner(step)
        if rc != 0:
            out(f"[{task}] FAILED (exit {rc}): {step.show()}")
            return False
    return True


def run_tasks(tasks: Sequence[str], runner: Runner, out: Callable[[str], None], root: Path = ROOT) -> int:
    """Runs every task (each to its first failure, like CI's independent jobs) and prints a summary. Exit 1 on any."""
    results = {task: run_task(task, steps(task, root), runner, out) for task in tasks}
    if len(tasks) > 1:
        out("summary: " + ", ".join(f"{t} {'ok' if ok else 'FAILED'}" for t, ok in results.items()))
    return 0 if all(results.values()) else 1


def expand(names: Sequence[str]) -> list[str]:
    return list(TASKS) if not names or "ci" in names else list(dict.fromkeys(names))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks", nargs="*", choices=[*TASKS, "ci"], metavar="task", help="check, test, e2e, lua, ts, ci")
    ap.add_argument("--list", action="store_true", help="print the commands instead of running them")
    args = ap.parse_args(argv)
    tasks = expand(args.tasks)
    if args.list:
        for task in tasks:
            for step in steps(task):
                print(f"{task}: {step.show()}")
        return 0
    if not args.tasks:
        ap.print_usage()
        return 2
    return run_tasks(tasks, run_step, lambda line: print(line, flush=True))


if __name__ == "__main__":
    sys.exit(main())
