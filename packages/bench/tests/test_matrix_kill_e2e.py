"""The matrix runner killed mid-run (research/ovh/pkg-bench-analysis.md E3): a real `bg3bench.matrix` process runs a
pi job then a Claude Code job and is SIGKILLed while the Claude run is in progress (a start with no end in
queue.jsonl); a new Matrix on the same files closes that attempt as interrupted, charges the budget nothing for it
(a subscription run is never billed, whatever its max_cost) and finishes the plan. No sleeps: the child says when the
Claude run has started, on its stdout."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bg3bench import matrix
from bg3bench.matrix import RunResult

pytestmark = pytest.mark.e2e

MODELS = [{"slug": "deepseek-flash", "client": "pi"}, {"slug": "opus", "client": "claude"}]
PI_JOB, CLAUDE_JOB = "deepseek-flash~nautiloid~rules~r1", "opus~nautiloid~rules~r1"
PI_COST = 0.5

CHILD = """
import json, sys
from pathlib import Path
from bg3bench import matrix
from bg3bench.matrix import RunResult

root, budget = Path(sys.argv[1]), Path(sys.argv[2])
jobs = json.loads(sys.argv[3])

def run_one(job):
    if job["client"] == "pi":
        return RunResult("pi-1", True, True, float(sys.argv[4]))
    print("running", job["id"], flush=True)
    sys.stdin.read()  # the Claude run "plays" until the parent kills this process
    raise AssertionError("not killed")

t = [0.0]
m = matrix.Matrix(root, run_one, budget, now=lambda: t[0], sleep=lambda s: None)
m.create("kill", jobs)
m.run()
"""


def test_a_matrix_killed_mid_claude_run_resumes_without_charging_it(tmp_path: Path) -> None:
    jobs = matrix.plan(MODELS, ["nautiloid"], ["rules"], 1, max_cost={"nautiloid": 2.0})
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"cap": 3.0, "spent": 0.0}))
    root = tmp_path / "m"
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(root), str(budget), json.dumps(jobs), str(PI_COST)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().split() == ["running", CLAUDE_JOB]
    finally:
        child.kill()
        child.wait(timeout=30)
    assert child.returncode == -9
    assert json.loads(budget.read_text())["spent"] == PI_COST

    calls: list[str] = []

    def run_one(job: dict[str, Any]) -> RunResult:
        calls.append(job["id"])
        return RunResult("opus-1", True, True, 0.7, charged=True)  # as session_run reports a Claude Code run

    m = matrix.Matrix(root, run_one, budget, now=lambda: 1.0, sleep=lambda s: None)
    m.create("kill", jobs)  # the same plan: resume
    assert m.state()[CLAUDE_JOB]["status"] == "interrupted"
    assert m.run() == matrix.FINISHED
    assert calls == [CLAUDE_JOB]
    atts = m.state()[CLAUDE_JOB]["attempts"]
    assert [a["result"]["invalid_reason"] for a in atts] == ["interrupted", None]
    assert json.loads(budget.read_text())["spent"] == PI_COST  # was PI_COST + 2.0: the Claude job's max_cost
    progress = json.loads((root / "progress.json").read_text())
    assert (progress["state"], progress["done"], progress["invalid_runs"]) == ("finished", 2, 1)
